"""Textual lifecycle tests for RunScreen/LoopSupervisorApp.

These use real Git repositories and a real Supervisor/state layer, but
fake OpenCodeServer/SSEClient at the module level to avoid spawning real
processes. The focus is lock ownership, shutdown ordering, and
initialization/shutdown races — the properties a purely unit-level test
of the reducer or the runtime controller cannot exercise.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from pathlib import Path

import pytest

import loop_supervisor.tui.app as app_mod
from loop_supervisor.git import GitRepo
from loop_supervisor.locking import _lock_path
from loop_supervisor.state import RunOptions, load_state
from loop_supervisor.supervisor import PHASE_OPERATIONAL_FAILURE
from loop_supervisor.tui.app import _SHUTDOWN_RETRY_INTERVAL_SECONDS, LoopSupervisorApp, RunScreen


def _run_git(args, cwd):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _static_text(widget: app_mod.Static) -> str:
    """Extract the current text content of a Static widget for assertions."""
    return str(getattr(widget, "_Static__content", ""))


def _init_repo(path: Path) -> GitRepo:
    path.mkdir(parents=True)
    _run_git(["init", "-b", "main"], path)
    _run_git(["config", "user.email", "test@example.com"], path)
    _run_git(["config", "user.name", "Test"], path)
    (path / "README.md").write_text("hello\n")
    _run_git(["add", "-A"], path)
    _run_git(["commit", "-m", "initial"], path)
    return GitRepo(path)


def _make_options(**overrides) -> RunOptions:
    defaults = dict(
        max_accepted_tasks=1,
        max_revisions_per_task=1,
        max_replans_per_task=1,
        max_architect_retries=1,
        malformed_output_retries=0,
        role_timeout=60.0,
        worktree_root=None,
        require_decision_approval=False,
        opencode_executable="opencode",
        opencode_startup_timeout=5.0,
    )
    defaults.update(overrides)
    return RunOptions(**defaults)


class _FakeServer:
    """Stands in for OpenCodeServer without spawning a process."""

    def __init__(self, project_dir, config, *, fail: bool = False, call_log=None):
        self._fail = fail
        self.base_url: str | None = None
        self.call_log = call_log if call_log is not None else []

    def add_observer(self, observer) -> None:
        pass

    def start(self) -> None:
        self.call_log.append("server_start")
        if self._fail:
            from loop_supervisor.opencode import ServerStartupError

            raise ServerStartupError("simulated failure")
        self.base_url = None  # no SSE

    def stop(self) -> None:
        self.call_log.append("server_stop")

    def abort_active_sessions(self) -> None:
        self.call_log.append("abort_sessions")


def _patch_server(monkeypatch, *, fail: bool = False, call_log=None, factory=None):
    if factory is not None:
        monkeypatch.setattr(app_mod, "OpenCodeServer", factory)
        return
    log = call_log if call_log is not None else []

    def _factory(project_dir, config):
        return _FakeServer(project_dir, config, fail=fail, call_log=log)

    monkeypatch.setattr(app_mod, "OpenCodeServer", _factory)


@pytest.mark.asyncio
async def test_new_run_acquires_lock_before_state_creation(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    _patch_server(monkeypatch, call_log=call_log)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        app.push_screen(RunScreen(tmp_path / "repo", run_id=None))
        await pilot.pause()
        for _ in range(50):
            screen = app.screen
            if isinstance(screen, RunScreen) and screen._state is not None:
                break
            await pilot.pause(0.05)
        screen = app.screen
        assert isinstance(screen, RunScreen)
        assert screen._lock is not None
        assert _lock_path(repo.common_dir()).exists()
        screen.action_request_shutdown()
        for _ in range(50):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)
        assert not _lock_path(repo.common_dir()).exists()


@pytest.mark.asyncio
async def test_server_startup_failure_persists_operational_failure(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    _patch_server(monkeypatch, fail=True, call_log=call_log)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        app.push_screen(RunScreen(tmp_path / "repo", run_id=None))
        await pilot.pause()
        for _ in range(50):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)

    assert not _lock_path(repo.common_dir()).exists()
    runs = list(repo.common_dir().glob("loop-supervisor/runs/*.json"))
    assert len(runs) == 1
    run_id = runs[0].stem
    state = load_state(repo.common_dir(), run_id)
    assert state.phase == PHASE_OPERATIONAL_FAILURE
    assert state.last_error["kind"] == "opencode_startup"
    assert "server_start" in call_log


@pytest.mark.asyncio
async def test_repeated_tui_startup_failure_preserves_retry_phase(tmp_path, monkeypatch):
    """Resuming an already-operational_failure run through the TUI and
    failing OpenCode startup again must not overwrite the real interrupted
    retry_phase with 'operational_failure' itself."""
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    _patch_server(monkeypatch, fail=True, call_log=call_log)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        app.push_screen(RunScreen(tmp_path / "repo", run_id=None))
        await pilot.pause()
        for _ in range(50):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)

    runs = list(repo.common_dir().glob("loop-supervisor/runs/*.json"))
    assert len(runs) == 1
    run_id = runs[0].stem
    first = load_state(repo.common_dir(), run_id)
    assert first.phase == PHASE_OPERATIONAL_FAILURE
    first_retry_phase = first.last_error["retry_phase"]
    assert first_retry_phase != PHASE_OPERATIONAL_FAILURE

    app2 = LoopSupervisorApp(tmp_path / "repo")
    async with app2.run_test() as pilot:
        app2.push_screen(RunScreen(tmp_path / "repo", run_id=run_id))
        await pilot.pause()
        for _ in range(50):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)

    second = load_state(repo.common_dir(), run_id)
    assert second.phase == PHASE_OPERATIONAL_FAILURE
    assert second.last_error["retry_phase"] == first_retry_phase


@pytest.mark.asyncio
async def test_startup_failure_persistence_error_is_surfaced_not_swallowed(tmp_path, monkeypatch):
    """If record_external_failure() itself fails (e.g. disk full, corrupt
    state directory), the TUI must not silently discard that failure and
    imply a durable operational_failure was recorded when it was not. The
    server must still be cleaned up and the lock still released."""
    from loop_supervisor.supervisor import FailurePersistenceError, Supervisor

    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    _patch_server(monkeypatch, fail=True, call_log=call_log)

    def _boom(self, state, *, exc, phase):
        raise FailurePersistenceError("simulated persistence failure")

    monkeypatch.setattr(Supervisor, "record_external_failure", _boom)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        app.push_screen(RunScreen(tmp_path / "repo", run_id=None))
        await pilot.pause()
        for _ in range(50):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)
        banner_text = ""
        for _ in range(50):
            screen = app.screen
            if isinstance(screen, RunScreen):
                try:
                    banner_text = _static_text(screen.query_one("#banner-text", app_mod.Static))
                except Exception:
                    banner_text = ""
            if "could not be persisted" in banner_text.lower():
                break
            await pilot.pause(0.05)

    assert not _lock_path(repo.common_dir()).exists()
    assert "server_start" in call_log
    assert "server_stop" in call_log
    assert "could not be persisted" in banner_text.lower()

    runs = list(repo.common_dir().glob("loop-supervisor/runs/*.json"))
    assert len(runs) == 1
    run_id = runs[0].stem
    state = load_state(repo.common_dir(), run_id)
    assert state.phase != PHASE_OPERATIONAL_FAILURE


@pytest.mark.asyncio
async def test_resume_validation_failure_releases_lock_without_starting_server(
    tmp_path, monkeypatch
):
    from loop_supervisor.supervisor import Supervisor

    repo = _init_repo(tmp_path / "repo")
    supervisor = Supervisor(
        repo=repo,
        runner=_NullRunner(),
        git_common_dir=repo.common_dir(),
        input_provider=_NullInput(),
        options=_make_options(),
    )
    state = supervisor.start_new_run()
    run_id = state.run_id

    # A dirty integration worktree fails resume validation before any
    # server is started.
    (repo.root / "dirty.txt").write_text("uncommitted\n")

    call_log: list[str] = []
    _patch_server(monkeypatch, call_log=call_log)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        app.push_screen(RunScreen(tmp_path / "repo", run_id=run_id))
        await pilot.pause()
        for _ in range(50):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)

    assert not _lock_path(repo.common_dir()).exists()
    assert "server_start" not in call_log
    reloaded = load_state(repo.common_dir(), run_id)
    assert reloaded.phase == "planning"


@pytest.mark.asyncio
async def test_quit_during_blocked_advance_does_not_release_lock_early(tmp_path, monkeypatch):
    """If advance() is still running when shutdown is requested, the lock
    must remain held until advance() actually completes."""
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    _patch_server(monkeypatch, call_log=call_log)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        app.push_screen(RunScreen(tmp_path / "repo", run_id=None))
        await pilot.pause()
        for _ in range(50):
            screen = app.screen
            if isinstance(screen, RunScreen) and screen._state is not None:
                break
            await pilot.pause(0.05)
        screen = app.screen
        assert isinstance(screen, RunScreen)

        release_advance = threading.Event()
        real_advance = screen._supervisor.advance

        def blocked_advance(state):
            release_advance.wait(timeout=10)
            return real_advance(state)

        monkeypatch.setattr(screen._supervisor, "advance", blocked_advance)
        screen._start_advance()
        await pilot.pause(0.1)
        assert screen._transitioning is True

        screen.action_request_shutdown()
        # Give the shutdown worker a chance to run; the lock must survive
        # because advance() is still blocked.
        time.sleep(0.3)
        assert _lock_path(repo.common_dir()).exists()

        release_advance.set()
        for _ in range(100):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)
        assert not _lock_path(repo.common_dir()).exists()


@pytest.mark.asyncio
async def test_double_submit_starts_only_one_transition(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    _patch_server(monkeypatch, call_log=call_log)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        app.push_screen(RunScreen(tmp_path / "repo", run_id=None))
        await pilot.pause()
        for _ in range(50):
            screen = app.screen
            if isinstance(screen, RunScreen) and screen._state is not None:
                break
            await pilot.pause(0.05)
        screen = app.screen
        assert isinstance(screen, RunScreen)

        advance_calls = []
        real_advance = screen._supervisor.advance
        gate = threading.Event()

        def counting_advance(state):
            advance_calls.append(1)
            gate.wait(timeout=10)
            return real_advance(state)

        monkeypatch.setattr(screen._supervisor, "advance", counting_advance)

        screen._start_advance()
        screen._start_advance()
        screen._start_advance()
        await pilot.pause(0.1)
        assert len(advance_calls) == 1

        gate.set()
        for _ in range(50):
            if not screen._transitioning:
                break
            await pilot.pause(0.05)

        screen.action_request_shutdown()
        for _ in range(50):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)


@pytest.mark.asyncio
async def test_unmount_does_not_bypass_orderly_shutdown(tmp_path, monkeypatch):
    """Popping the screen directly (simulating an unexpected unmount) must
    still go through the same wait-for-init/advance ordering rather than
    releasing resources immediately."""
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    _patch_server(monkeypatch, call_log=call_log)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        app.push_screen(RunScreen(tmp_path / "repo", run_id=None))
        await pilot.pause()
        for _ in range(50):
            screen = app.screen
            if isinstance(screen, RunScreen) and screen._state is not None:
                break
            await pilot.pause(0.05)
        screen = app.screen
        assert isinstance(screen, RunScreen)
        assert screen._lock is not None

        app.pop_screen()
        for _ in range(50):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)
        assert not _lock_path(repo.common_dir()).exists()


@pytest.mark.asyncio
async def test_app_exit_during_blocked_advance_waits_for_cleanup(tmp_path, monkeypatch):
    """App-level exit (app.exit(), not screen.action_request_shutdown())
    must not allow the process to finish exiting while advance() is still
    blocked. The lock must remain held until advance() completes, and the
    server must be stopped and the lock released before app.exit()'s
    caller observes completion."""
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    _patch_server(monkeypatch, call_log=call_log)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        app.push_screen(RunScreen(tmp_path / "repo", run_id=None))
        await pilot.pause()
        for _ in range(50):
            screen = app.screen
            if isinstance(screen, RunScreen) and screen._state is not None:
                break
            await pilot.pause(0.05)
        screen = app.screen
        assert isinstance(screen, RunScreen)

        release_advance = threading.Event()
        real_advance = screen._supervisor.advance

        def blocked_advance(state):
            release_advance.wait(timeout=10)
            return real_advance(state)

        monkeypatch.setattr(screen._supervisor, "advance", blocked_advance)
        screen._start_advance()
        await pilot.pause(0.1)
        assert screen._transitioning is True

        # app.exit() only posts an ExitApp message; the App's message loop
        # (running as a background asyncio task under run_test()) will pick
        # it up and start awaiting RunScreen.await_shutdown_complete(),
        # which is itself blocked on the in-flight advance(). Since that
        # wait happens via run_in_executor rather than blocking the loop,
        # pilot.pause() (which waits for the screen's queued messages to
        # drain) cannot be used here to observe the intermediate blocked
        # state — it would itself block until shutdown finishes. A plain
        # sleep lets the background asyncio task run without depending on
        # the screen's message queue draining.
        app.exit()
        time.sleep(0.3)
        assert _lock_path(repo.common_dir()).exists()

        release_advance.set()
        for _ in range(100):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)
        assert not _lock_path(repo.common_dir()).exists()
        assert "server_stop" in call_log


@pytest.mark.asyncio
async def test_app_exit_during_blocked_initialization_waits_for_cleanup(tmp_path, monkeypatch):
    """App-level exit requested while _do_initialize_locked is still
    running (e.g. still starting the server) must wait for initialization
    to fully unwind before the lock is released, exactly like
    screen.action_request_shutdown()."""
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    release_start = threading.Event()

    start_entered = threading.Event()

    class _BlockingFakeServer(_FakeServer):
        def start(self) -> None:
            start_entered.set()
            release_start.wait(timeout=10)
            super().start()

    def factory(project_dir, config):
        return _BlockingFakeServer(project_dir, config, call_log=call_log)

    _patch_server(monkeypatch, factory=factory)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        app.push_screen(RunScreen(tmp_path / "repo", run_id=None))
        for _ in range(50):
            if start_entered.is_set():
                break
            await pilot.pause(0.02)
        assert start_entered.is_set()

        assert _lock_path(repo.common_dir()).exists()

        # See the comment in test_app_exit_during_blocked_advance_waits_for_cleanup
        # about why a plain sleep, not pilot.pause(), is used to observe the
        # intermediate blocked state here.
        app.exit()
        time.sleep(0.3)
        # Initialization has not finished (server.start() is still
        # blocked), so the lock must still be held.
        assert _lock_path(repo.common_dir()).exists()

        release_start.set()
        for _ in range(100):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)
        assert not _lock_path(repo.common_dir()).exists()


@pytest.mark.asyncio
async def test_app_exit_cleanup_ordering(tmp_path, monkeypatch):
    """Cleanup must happen in the documented order: abort sessions before
    the server is stopped, and the server stopped before the lock is
    released."""
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    _patch_server(monkeypatch, call_log=call_log)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        app.push_screen(RunScreen(tmp_path / "repo", run_id=None))
        await pilot.pause()
        for _ in range(50):
            screen = app.screen
            if isinstance(screen, RunScreen) and screen._state is not None:
                break
            await pilot.pause(0.05)

        app.exit()
        for _ in range(100):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)

    assert not _lock_path(repo.common_dir()).exists()
    assert call_log.index("abort_sessions") < call_log.index("server_stop")


@pytest.mark.asyncio
async def test_concurrent_exit_requests_are_idempotent(tmp_path, monkeypatch):
    """Triggering 'q' and app-level exit close together must still result
    in exactly one cleanup execution: one server stop and one lock
    release, with no errors from double-release."""
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    _patch_server(monkeypatch, call_log=call_log)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        app.push_screen(RunScreen(tmp_path / "repo", run_id=None))
        await pilot.pause()
        for _ in range(50):
            screen = app.screen
            if isinstance(screen, RunScreen) and screen._state is not None:
                break
            await pilot.pause(0.05)
        screen = app.screen
        assert isinstance(screen, RunScreen)

        screen.action_request_shutdown()
        app.exit()
        for _ in range(100):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)

    assert not _lock_path(repo.common_dir()).exists()
    assert call_log.count("server_stop") == 1


@pytest.mark.asyncio
async def test_app_exit_retains_lock_when_server_stop_fails(tmp_path, monkeypatch):
    """If server.stop() raises during cleanup (the process could not be
    confirmed released), the repository lock MUST NOT be released — the
    accepted contract holds the lock through OpenCode shutdown. The
    shutdown *attempt* still completes (so app exit does not hang), but the
    lock is left on disk for explicit --recover-stale-lock."""
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []

    class _FailingStopServer(_FakeServer):
        def stop(self) -> None:
            call_log.append("server_stop_attempted")
            raise RuntimeError("simulated stop failure: process may still be live")

    def factory(project_dir, config):
        return _FailingStopServer(project_dir, config, call_log=call_log)

    _patch_server(monkeypatch, factory=factory)

    screen_ref: dict[str, RunScreen] = {}
    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        screen = RunScreen(tmp_path / "repo", run_id=None)
        screen_ref["s"] = screen
        app.push_screen(screen)
        await pilot.pause()
        for _ in range(50):
            if screen._state is not None:
                break
            await pilot.pause(0.05)

        screen.action_request_shutdown()
        for _ in range(100):
            if screen._shutdown_complete_event.is_set():
                break
            await pilot.pause(0.05)

    assert "server_stop_attempted" in call_log
    # Attempt finished, but not cleanly: lock and server ownership retained.
    assert screen_ref["s"]._shutdown_complete_event.is_set()
    assert screen_ref["s"]._shutdown_clean is False
    assert _lock_path(repo.common_dir()).exists()
    assert screen_ref["s"]._server is not None
    assert screen_ref["s"]._lock is not None


@pytest.mark.asyncio
async def test_app_exit_releases_lock_on_clean_server_stop(tmp_path, monkeypatch):
    """When server.stop() returns cleanly, the lock is released and
    shutdown is marked clean."""
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    _patch_server(monkeypatch, call_log=call_log)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        app.push_screen(RunScreen(tmp_path / "repo", run_id=None))
        await pilot.pause()
        for _ in range(50):
            screen = app.screen
            if isinstance(screen, RunScreen) and screen._state is not None:
                break
            await pilot.pause(0.05)

        app.exit()
        for _ in range(100):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)

    assert not _lock_path(repo.common_dir()).exists()
    assert call_log.count("server_stop") == 1


@pytest.mark.asyncio
async def test_shutdown_during_blocked_initialization_retries_after_failed_stop(
    tmp_path, monkeypatch
):
    """Shutdown requested while server.start() is still blocked must not
    duplicate teardown inside _do_initialize_locked: only the shutdown
    worker's canonical _do_shutdown may touch the server/lock. If the
    first stop() attempt fails, server and lock ownership must be
    retained; retrying cleanup must then succeed and clear both."""
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    release_start = threading.Event()
    start_entered = threading.Event()
    stop_calls = {"n": 0}

    class _BlockingThenFailOnceServer(_FakeServer):
        def start(self) -> None:
            start_entered.set()
            release_start.wait(timeout=10)
            super().start()

        def stop(self) -> None:
            stop_calls["n"] += 1
            call_log.append("server_stop")
            if stop_calls["n"] == 1:
                raise RuntimeError("simulated first stop failure")

    def factory(project_dir, config):
        return _BlockingThenFailOnceServer(project_dir, config, call_log=call_log)

    _patch_server(monkeypatch, factory=factory)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        screen = RunScreen(tmp_path / "repo", run_id=None)
        app.push_screen(screen)
        for _ in range(50):
            if start_entered.is_set():
                break
            await pilot.pause(0.02)
        assert start_entered.is_set()

        screen.action_request_shutdown()
        # Shutdown is waiting on _init_done_event; server.start() is still
        # blocked, so nothing has been torn down yet and _server must
        # still be owned once start() finally returns.
        time.sleep(0.2)
        assert _lock_path(repo.common_dir()).exists()

        release_start.set()
        for _ in range(100):
            if screen._shutdown_complete_event.is_set():
                break
            await pilot.pause(0.05)

        # First attempt failed to stop cleanly: server and lock retained.
        assert screen._shutdown_clean is False
        assert screen._server is not None
        assert screen._lock is not None
        assert _lock_path(repo.common_dir()).exists()

        # Retry: this time stop() succeeds.
        screen.action_request_shutdown()
        for _ in range(100):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)

    assert not _lock_path(repo.common_dir()).exists()
    assert screen._server is None
    assert screen._lock is None
    assert call_log.count("server_stop") == 2


@pytest.mark.asyncio
async def test_return_to_browser_retries_transient_stop_failure(tmp_path, monkeypatch):
    """Pressing 'q'/Return-to-runs after a failed cleanup attempt must
    retry, and succeed once the transient failure clears."""
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    stop_calls = {"n": 0}

    class _FailOnceServer(_FakeServer):
        def stop(self) -> None:
            stop_calls["n"] += 1
            call_log.append("server_stop")
            if stop_calls["n"] == 1:
                raise RuntimeError("simulated transient stop failure")

    def factory(project_dir, config):
        return _FailOnceServer(project_dir, config, call_log=call_log)

    _patch_server(monkeypatch, factory=factory)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        screen = RunScreen(tmp_path / "repo", run_id=None)
        app.push_screen(screen)
        await pilot.pause()
        for _ in range(50):
            if screen._state is not None:
                break
            await pilot.pause(0.05)

        screen.action_request_shutdown()
        for _ in range(100):
            if screen._shutdown_complete_event.is_set():
                break
            await pilot.pause(0.05)

        assert screen._shutdown_clean is False
        assert _lock_path(repo.common_dir()).exists()

        screen.action_request_shutdown()
        for _ in range(100):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)

    assert not _lock_path(repo.common_dir()).exists()
    assert call_log.count("server_stop") == 2


@pytest.mark.asyncio
async def test_app_exit_automatically_retries_fail_once_cleanup(tmp_path, monkeypatch):
    """App-level exit must retry cleanup on its own, without any manual
    'q' press, when the first attempt fails to stop cleanly."""
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    stop_calls = {"n": 0}

    class _FailOnceServer(_FakeServer):
        def stop(self) -> None:
            stop_calls["n"] += 1
            call_log.append("server_stop")
            if stop_calls["n"] == 1:
                raise RuntimeError("simulated transient stop failure")

    def factory(project_dir, config):
        return _FailOnceServer(project_dir, config, call_log=call_log)

    _patch_server(monkeypatch, factory=factory)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        app.push_screen(RunScreen(tmp_path / "repo", run_id=None))
        await pilot.pause()
        for _ in range(50):
            screen = app.screen
            if isinstance(screen, RunScreen) and screen._state is not None:
                break
            await pilot.pause(0.05)

        app.exit()
        for _ in range(200):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)

    assert not _lock_path(repo.common_dir()).exists()
    assert call_log.count("server_stop") == 2


@pytest.mark.asyncio
async def test_app_exit_remains_pending_while_cleanup_stays_unclean(tmp_path, monkeypatch):
    """If server.stop() keeps failing, app.exit() must not complete: the
    lock stays present and the app's internal exit gate does not proceed
    while cleanup remains unclean. Stop is then allowed to succeed so the
    test itself can shut down cleanly (the retry loop is intentionally
    unbounded, so we must let it actually finish before leaving the
    run_test() context, rather than asserting a permanently blocked
    state)."""
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    should_fail = {"v": True}

    class _ToggleableFailingStopServer(_FakeServer):
        def stop(self) -> None:
            call_log.append("server_stop_attempted")
            if should_fail["v"]:
                raise RuntimeError("simulated stop failure")

    def factory(project_dir, config):
        return _ToggleableFailingStopServer(project_dir, config, call_log=call_log)

    _patch_server(monkeypatch, factory=factory)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        screen = RunScreen(tmp_path / "repo", run_id=None)
        app.push_screen(screen)
        await pilot.pause()
        for _ in range(50):
            if screen._state is not None:
                break
            await pilot.pause(0.05)

        # Unlike the blocked-advance/blocked-initialization tests, the
        # retry here happens via asyncio.sleep() inside _on_exit_app on
        # the app's own event loop (the same loop this test coroutine
        # runs on), so a plain (synchronous) time.sleep() here would
        # starve that loop and prevent the retry from ever running. Use
        # asyncio.sleep() directly instead of pilot.pause() (which would
        # additionally wait for message-queue draining via
        # _wait_for_screen, which is unrelated to what we want to
        # observe here).
        app.exit()
        await asyncio.sleep(_SHUTDOWN_RETRY_INTERVAL_SECONDS * 2 + 0.5)
        assert _lock_path(repo.common_dir()).exists()
        assert screen.shutdown_clean is False
        assert len(call_log) >= 2  # at least one retry actually happened

        should_fail["v"] = False
        for _ in range(100):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)

    assert not _lock_path(repo.common_dir()).exists()


@pytest.mark.asyncio
async def test_concurrent_shutdown_requests_do_not_start_overlapping_workers(tmp_path, monkeypatch):
    """Triggering shutdown from multiple call sites in quick succession
    while a slow stop() is in flight must not start a second overlapping
    _shutdown_worker; only one stop() call should be in flight at a time."""
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    concurrent = {"n": 0, "max": 0}
    lock = threading.Lock()

    class _SlowStopServer(_FakeServer):
        def stop(self) -> None:
            with lock:
                concurrent["n"] += 1
                concurrent["max"] = max(concurrent["max"], concurrent["n"])
            call_log.append("server_stop")
            time.sleep(0.3)
            with lock:
                concurrent["n"] -= 1

    def factory(project_dir, config):
        return _SlowStopServer(project_dir, config, call_log=call_log)

    _patch_server(monkeypatch, factory=factory)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        screen = RunScreen(tmp_path / "repo", run_id=None)
        app.push_screen(screen)
        await pilot.pause()
        for _ in range(50):
            if screen._state is not None:
                break
            await pilot.pause(0.05)

        screen.action_request_shutdown()
        screen.action_request_shutdown()
        screen.action_request_shutdown()
        for _ in range(100):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)

    assert not _lock_path(repo.common_dir()).exists()
    assert concurrent["max"] == 1
    assert call_log.count("server_stop") == 1


@pytest.mark.asyncio
async def test_base_textual_exit_occurs_only_after_clean_shutdown(tmp_path, monkeypatch):
    """super()._on_exit_app() (the underlying Textual shutdown sequence)
    must not be invoked until every RunScreen reports a clean shutdown."""
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    stop_calls = {"n": 0}

    class _FailOnceServer(_FakeServer):
        def stop(self) -> None:
            stop_calls["n"] += 1
            call_log.append("server_stop")
            if stop_calls["n"] == 1:
                raise RuntimeError("simulated transient stop failure")

    def factory(project_dir, config):
        return _FailOnceServer(project_dir, config, call_log=call_log)

    _patch_server(monkeypatch, factory=factory)

    base_exit_calls: list[bool] = []
    from textual.app import App as _TextualApp

    original_on_exit_app = _TextualApp._on_exit_app

    screen_ref: dict[str, RunScreen] = {}

    async def _spy_on_exit_app(self):
        screen = screen_ref.get("s")
        base_exit_calls.append(screen.shutdown_clean if screen is not None else True)
        await original_on_exit_app(self)

    monkeypatch.setattr(_TextualApp, "_on_exit_app", _spy_on_exit_app)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        screen = RunScreen(tmp_path / "repo", run_id=None)
        screen_ref["s"] = screen
        app.push_screen(screen)
        await pilot.pause()
        for _ in range(50):
            if screen._state is not None:
                break
            await pilot.pause(0.05)

        app.exit()
        for _ in range(200):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)

    assert not _lock_path(repo.common_dir()).exists()
    # Every time the underlying Textual shutdown sequence was actually
    # invoked, shutdown was already clean — it must never run while
    # cleanup is still unresolved. (run_test()'s own context-manager exit
    # may trigger an additional idempotent call once already clean.)
    assert base_exit_calls
    assert all(base_exit_calls)


class _NullRunner:
    def run_agent(self, **_kwargs):
        raise AssertionError("should not be called")


class _NullInput:
    def request(self, *, kind, message, context):
        return None
