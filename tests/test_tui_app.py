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
from typing import Any

import pytest

import loop_supervisor.runtime as rt
import loop_supervisor.tui.app as app_mod
from loop_supervisor.git import GitRepo
from loop_supervisor.locking import LockError, _lock_path
from loop_supervisor.opencode import InvocationRef
from loop_supervisor.state import RunOptions, load_state
from loop_supervisor.supervisor import PHASE_OPERATIONAL_FAILURE
from loop_supervisor.tui.app import (
    _SHUTDOWN_RETRY_INTERVAL_SECONDS,
    LoopSupervisorApp,
    RunBrowserScreen,
    RunScreen,
)


def _run_git(args, cwd):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _static_text(widget: app_mod.Static) -> str:
    """Extract the current text content of a Static widget for assertions.

    ``str()`` on the stored renderable is only useful when it is itself
    a plain string (``render_pending_input``/``render_operational_
    failure``); a ``rich.table.Table`` (``render_durable_summary``/
    ``render_live_summary``) stringifies to its Python repr instead, so
    those are rendered through a real Rich ``Console`` to get their
    displayed text.
    """
    from rich.console import Console
    from rich.table import Table

    content = getattr(widget, "_Static__content", "")
    if isinstance(content, Table):
        console = Console(record=True, width=100)
        console.print(content)
        return console.export_text()
    return str(content)


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
    defaults: dict[str, Any] = dict(
        max_accepted_tasks=1,
        max_revisions_per_task=1,
        max_replans_per_task=1,
        max_architect_retries=1,
        max_builder_guidance_attempts=1,
        malformed_output_retries=0,
        role_timeout=60.0,
        worktree_root=None,
        require_decision_approval=False,
        opencode_executable="opencode",
        opencode_startup_timeout=5.0,
        provision_commands=(),
        provision_timeout=600.0,
        verify_commands=(),
        verify_timeout=900.0,
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

    def active_invocations(self) -> list[InvocationRef]:
        return []

    def reconcile_invocation(
        self, ref: InvocationRef
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        raise AssertionError("no active fake invocation")


def _patch_server(monkeypatch, *, fail: bool = False, call_log=None, factory=None):
    """Patch OpenCodeServer where it is actually constructed.

    Since RunSession.start_server() (via RunSession.__enter__) is now
    what constructs the server -- app.py no longer imports OpenCodeServer
    itself -- the patch target is loop_supervisor.runtime.OpenCodeServer,
    not app_mod. This is the one funnel point for all 30+ tests below
    that use this helper; no other test in this file references
    app_mod.OpenCodeServer directly.
    """
    if factory is not None:
        monkeypatch.setattr(rt, "OpenCodeServer", factory)
        return
    log = call_log if call_log is not None else []

    def _factory(project_dir, config):
        return _FakeServer(project_dir, config, fail=fail, call_log=log)

    monkeypatch.setattr(rt, "OpenCodeServer", _factory)


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
        assert screen._session is not None
        assert _lock_path(repo.common_dir()).exists()
        screen.action_request_shutdown()
        for _ in range(50):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)
        assert not _lock_path(repo.common_dir()).exists()


@pytest.mark.asyncio
async def test_refresh_durable_shows_denied_permissions_from_session(tmp_path, monkeypatch):
    """RunSession.denied_permission_count/_summary (runtime.py) are read
    every time _refresh_durable() runs; they are not part of RunState.
    Backlog item 31 and ADR 0021 note the TUI already runs the same
    headless permission denier via start_server() without previously
    surfacing what it denied."""
    repo = _init_repo(tmp_path / "repo")
    _patch_server(monkeypatch)

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
        assert screen._session is not None

        content_before = _static_text(screen.query_one("#durable-content", app_mod.Static))
        assert "Denied permissions" not in content_before

        # No live PermissionDenier exists in this fake-server setup
        # (base_url stays None, per _FakeServer.start()), so the
        # count/summary RunSession falls back to are these two private
        # snapshot attributes -- exactly what close() itself populates
        # once a real denier has run (runtime.py:1263-1267).
        monkeypatch.setattr(screen._session, "_denied_permission_count", 2)
        monkeypatch.setattr(screen._session, "_denied_permission_summary", ["bash", "webfetch"])

        screen._refresh_durable()
        content_after = _static_text(screen.query_one("#durable-content", app_mod.Static))
        assert "Denied permissions" in content_after
        assert "2" in content_after
        assert "bash" in content_after
        assert "webfetch" in content_after

        screen.action_request_shutdown()
        for _ in range(50):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)


@pytest.mark.asyncio
async def test_sse_gap_notice_is_visible(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    _patch_server(monkeypatch)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        screen = RunScreen(tmp_path / "repo", run_id=None)
        app.push_screen(screen)
        await pilot.pause()
        await asyncio.to_thread(
            screen._on_sse_notice,
            "SSE disconnected; activity during disconnect may be missing. Reconnecting in 1.0s.",
        )
        await pilot.pause()

        content = _static_text(screen.query_one("#live-content", app_mod.Static))
        assert "activity during disconnect may be missing" in content

        screen.action_request_shutdown()
        for _ in range(50):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)


@pytest.mark.asyncio
async def test_reconnect_reconciles_active_invocation_state(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")

    class ReconciliationServer(_FakeServer):
        def __init__(self, project_dir, config, *, call_log=None):
            super().__init__(project_dir, config, call_log=call_log)
            self.ref = InvocationRef(
                "session-exact", "loop-builder", tmp_path / "repo", time.monotonic()
            )

        def active_invocations(self) -> list[InvocationRef]:
            return [self.ref]

        def reconcile_invocation(
            self, ref: InvocationRef
        ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            assert ref == self.ref
            self.call_log.append((ref.session_id, str(ref.directory)))
            return (
                {"type": "busy"},
                [
                    {
                        "info": {
                            "id": "message-exact",
                            "sessionID": ref.session_id,
                            "role": "assistant",
                        },
                        "parts": [
                            {
                                "id": "part-exact",
                                "sessionID": ref.session_id,
                                "messageID": "message-exact",
                                "type": "text",
                                "text": "restored after reconnect",
                            }
                        ],
                    }
                ],
            )

    call_log: list[Any] = []
    _patch_server(
        monkeypatch,
        factory=lambda project_dir, config: ReconciliationServer(
            project_dir, config, call_log=call_log
        ),
    )
    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        screen = RunScreen(tmp_path / "repo", run_id=None)
        app.push_screen(screen)
        await pilot.pause()
        assert screen._session is not None
        refs = screen._session.active_invocations()
        assert len(refs) == 1
        screen.on_invocation_started(app_mod.InvocationStarted(refs[0]))
        screen.on_live_connection_changed(
            app_mod.LiveConnectionChanged(app_mod.SSEConnectionState.LIVE, "connected")
        )
        screen.on_live_connection_changed(
            app_mod.LiveConnectionChanged(app_mod.SSEConnectionState.RECONNECTING, "retrying")
        )
        screen.on_live_connection_changed(
            app_mod.LiveConnectionChanged(app_mod.SSEConnectionState.LIVE, "connected")
        )
        content = ""
        for _ in range(50):
            await pilot.pause(0.05)
            content = _static_text(screen.query_one("#live-content", app_mod.Static))
            if "restored after reconnect" in content:
                break

        assert ("session-exact", str(tmp_path / "repo")) in call_log
        assert "restored after reconnect" in content

        screen.action_request_shutdown()
        for _ in range(50):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)


@pytest.mark.asyncio
async def test_reconciliation_failure_is_visible_and_nonfatal(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")

    class FailingReconciliationServer(_FakeServer):
        ref = InvocationRef("session-fail", "loop-builder", tmp_path / "repo", time.monotonic())

        def active_invocations(self) -> list[InvocationRef]:
            return [self.ref]

        def reconcile_invocation(
            self, ref: InvocationRef
        ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            raise RuntimeError("status endpoint unavailable")

    _patch_server(
        monkeypatch,
        factory=lambda project_dir, config: FailingReconciliationServer(project_dir, config),
    )
    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        screen = RunScreen(tmp_path / "repo", run_id=None)
        app.push_screen(screen)
        await pilot.pause()
        screen.on_live_connection_changed(
            app_mod.LiveConnectionChanged(app_mod.SSEConnectionState.LIVE, "connected")
        )
        screen.on_live_connection_changed(
            app_mod.LiveConnectionChanged(app_mod.SSEConnectionState.RECONNECTING, "retrying")
        )
        screen.on_live_connection_changed(
            app_mod.LiveConnectionChanged(app_mod.SSEConnectionState.LIVE, "connected")
        )
        content = ""
        for _ in range(50):
            await pilot.pause(0.05)
            content = _static_text(screen.query_one("#live-content", app_mod.Static))
            if "status endpoint unavailable" in content:
                break

        assert "reconciliation failed" in content.lower()
        assert "status endpoint" in content
        assert "unavailable" in content
        assert screen._state is not None

        screen.action_request_shutdown()
        for _ in range(50):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)


@pytest.mark.asyncio
async def test_on_advance_completed_does_not_reloop_on_input_unavailable(tmp_path, monkeypatch):
    """INPUT_UNAVAILABLE must stop on_advance_completed()'s own status
    match, not merely rely on _start_advance()'s separate
    PHASE_AWAITING_INPUT guard to swallow a redundant call -- the two
    are currently coincident (INPUT_UNAVAILABLE always leaves the
    state in PHASE_AWAITING_INPUT), but the status match should not
    depend on that coincidence to be correct. See ADR 0021."""
    from loop_supervisor.phases import PHASE_AWAITING_INPUT
    from loop_supervisor.state import STATE_SCHEMA_VERSION, RunState
    from loop_supervisor.supervisor import AdvanceOutcome, AdvanceStatus
    from loop_supervisor.tui.messages import AdvanceCompleted

    repo = _init_repo(tmp_path / "repo")
    _patch_server(monkeypatch)

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
        assert screen._state is not None

        started_again: list[bool] = []
        monkeypatch.setattr(screen, "_start_advance", lambda: started_again.append(True))

        awaiting_state = RunState(
            schema_version=STATE_SCHEMA_VERSION,
            run_id=screen._state.run_id,
            git_common_dir=screen._state.git_common_dir,
            integration_path=screen._state.integration_path,
            integration_branch=screen._state.integration_branch,
            integration_commit_at_start=screen._state.integration_commit_at_start,
            options=screen._state.options,
            integration_expected_head=screen._state.integration_expected_head,
            integration_status_snapshot=screen._state.integration_status_snapshot,
            phase=PHASE_AWAITING_INPUT,
        )
        outcome = AdvanceOutcome(
            status=AdvanceStatus.INPUT_UNAVAILABLE,
            state=awaiting_state,
            phase_before=PHASE_AWAITING_INPUT,
            phase_after=PHASE_AWAITING_INPUT,
        )
        screen.on_advance_completed(AdvanceCompleted(outcome))
        assert started_again == []

        screen.action_request_shutdown()
        for _ in range(50):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)


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
    assert state.last_error is not None
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
    assert first.last_error is not None
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
    assert second.last_error is not None
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
        assert screen._session is not None
        assert screen._session._supervisor is not None
        real_advance = screen._session._supervisor.advance

        def blocked_advance(state):
            release_advance.wait(timeout=10)
            return real_advance(state)

        monkeypatch.setattr(screen._session._supervisor, "advance", blocked_advance)
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
        assert screen._session is not None
        assert screen._session._supervisor is not None
        real_advance = screen._session._supervisor.advance
        gate = threading.Event()

        def counting_advance(state):
            advance_calls.append(1)
            gate.wait(timeout=10)
            return real_advance(state)

        monkeypatch.setattr(screen._session._supervisor, "advance", counting_advance)

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
        assert screen._session is not None

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
        assert screen._session is not None
        assert screen._session._supervisor is not None
        real_advance = screen._session._supervisor.advance

        def blocked_advance(state):
            release_advance.wait(timeout=10)
            return real_advance(state)

        monkeypatch.setattr(screen._session._supervisor, "advance", blocked_advance)
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
async def test_app_exit_during_failed_initialization_cleanup_waits_for_its_attempt(
    tmp_path, monkeypatch
):
    """Exit requested while failed-init cleanup is active must await the
    concrete shutdown attempt that waits behind that cleanup."""
    repo = _init_repo(tmp_path / "repo")
    stop_entered = threading.Event()
    release_stop = threading.Event()

    class _BlockingStopServer(_FakeServer):
        def stop(self) -> None:
            stop_entered.set()
            release_stop.wait(timeout=10)

    def factory(project_dir, config):
        return _BlockingStopServer(project_dir, config)

    def _fail_after_server_start(self: RunScreen) -> None:
        assert self._session is not None
        self._session.start_server()
        raise RuntimeError("simulated post-start initialization failure")

    _patch_server(monkeypatch, factory=factory)
    monkeypatch.setattr(RunScreen, "_do_initialize_locked", _fail_after_server_start)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        screen = RunScreen(tmp_path / "repo", run_id=None)
        app.push_screen(screen)
        for _ in range(100):
            if stop_entered.is_set():
                break
            await pilot.pause(0.02)
        assert stop_entered.is_set()
        assert _lock_path(repo.common_dir()).exists()

        app.exit()
        attempt = None
        for _ in range(100):
            attempt = screen._shutdown_attempt
            if attempt is not None:
                break
            await asyncio.sleep(0.02)
        assert attempt is not None
        assert not attempt.completion.is_set()
        assert _lock_path(repo.common_dir()).exists()

        release_stop.set()
        for _ in range(100):
            if attempt.completion.is_set() and not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)

        assert attempt.completion.is_set()
        assert screen.shutdown_clean
        assert not _lock_path(repo.common_dir()).exists()


@pytest.mark.asyncio
async def test_already_clean_shutdown_request_return_and_coordinator_are_noops(
    tmp_path, monkeypatch
):
    """Direct request, Return-to-runs handler, and coordinator need no event
    when presented with an already-clean, initialization-complete screen."""
    app = LoopSupervisorApp(tmp_path)
    async with app.run_test():
        screen = RunScreen(tmp_path, run_id=None)
        screen._app_ref = app
        screen._init_done_event.set()
        screen._shutdown_clean = True
        app.register_run_screen(screen)

        assert screen.action_request_shutdown() is None  # q binding
        assert screen not in app._owned_run_screens

        app.register_run_screen(screen)
        screen.on_return()
        assert screen not in app._owned_run_screens

        app.register_run_screen(screen)
        await asyncio.wait_for(app._run_screen_cleanup_coordinator(screen), timeout=1)
        assert screen not in app._owned_run_screens


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

        attempt = screen.action_request_shutdown()
        assert attempt is not None
        for _ in range(100):
            if attempt.completion.is_set():
                break
            await pilot.pause(0.05)

        # Assert while the screen is still mounted, before the pilot
        # context exits: once this screen unmounts, its on_unmount() will
        # start an automatic retry coordinator (see Step 4 -- detached
        # screens must keep retrying indefinitely), which would otherwise
        # race these assertions against a fresh in-flight attempt.
        assert "server_stop_attempted" in call_log
        # Attempt finished, but not cleanly: lock and server ownership
        # retained -- RunSession never nulls its own references (see
        # RunScreen._session's docstring), so the faithful "not released"
        # signal is its SessionState rather than an identity check.
        assert screen.shutdown_clean is False
        assert _lock_path(repo.common_dir()).exists()
        assert screen._session is not None
        assert screen._session.state is rt.SessionState.CLEANUP_UNRESOLVED

        # Let the automatic retry coordinator run so this screen actually
        # finishes its lifecycle (with a permanently-failing stop(), it
        # will retry forever); stop failing so the test itself can exit
        # cleanly rather than leaving the pilot teardown to await an
        # unbounded coordinator.
        def _eventually_succeeds() -> None:
            call_log.append("server_stop_attempted")

        assert screen._session._server is not None
        screen._session._server.stop = _eventually_succeeds  # type: ignore[method-assign]


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
    worker's canonical _do_shutdown may touch the server/lock. A stop()
    that fails once must not leave the lock stuck: RunSession.close()
    retries server.stop() internally (see ADR 0009), so this single
    external shutdown attempt must still confirm cleanup and clear both
    the server and the lock, without a second action_request_shutdown()
    call being necessary."""
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

        attempt = screen.action_request_shutdown()
        assert attempt is not None
        # Shutdown is waiting on _init_done_event; server.start() is still
        # blocked, so nothing has been torn down yet and the session must
        # still be owned once start() finally returns.
        time.sleep(0.2)
        assert _lock_path(repo.common_dir()).exists()

        release_start.set()
        for _ in range(100):
            if attempt.completion.is_set():
                break
            await pilot.pause(0.05)

        # RunSession.close() retries server.stop() internally (up to
        # rt._CLEANUP_ATTEMPTS times, see ADR 0009): the fail-once
        # server's transient failure is confirmed on its second internal
        # attempt, all within this single external shutdown attempt, so
        # no second action_request_shutdown() call should be necessary.
        assert screen._session is not None
        assert screen.shutdown_clean is True
        assert screen._session.state is rt.SessionState.CLOSED

    assert not _lock_path(repo.common_dir()).exists()
    assert screen._session.state is rt.SessionState.CLOSED
    assert call_log.count("server_stop") == 2


@pytest.mark.asyncio
async def test_return_to_browser_retries_transient_stop_failure(tmp_path, monkeypatch):
    """Pressing 'q'/Return-to-runs must confirm cleanup even if the first
    server.stop() attempt fails transiently: RunSession.close() retries
    internally (see ADR 0009), so the fail-once server's failure is
    confirmed on its second internal attempt within this one press,
    without a second external press being necessary."""
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

        attempt = screen.action_request_shutdown()
        assert attempt is not None
        for _ in range(100):
            if attempt.completion.is_set():
                break
            await pilot.pause(0.05)

        assert screen._shutdown_clean is True

    assert not _lock_path(repo.common_dir()).exists()
    assert call_log.count("server_stop") == 2


@pytest.mark.asyncio
async def test_app_exit_automatically_retries_fail_once_cleanup(tmp_path, monkeypatch):
    """App-level exit must retry cleanup on its own, without any manual
    'q' press, when the first app-level attempt fails to stop cleanly.

    RunSession.close() already retries server.stop() internally up to
    rt._CLEANUP_ATTEMPTS times within a single app-level attempt (see
    ADR 0009), so the server here must fail exactly that many times --
    exhausting the *first* app-level attempt's entire internal retry
    budget -- for this test to actually exercise the app-level retry
    loop itself rather than being satisfied by RunSession's own internal
    retry. Succeeding only on the (rt._CLEANUP_ATTEMPTS + 1)th stop()
    call is what proves a second, distinct app-level attempt occurred.
    """
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    stop_calls = {"n": 0}

    class _FailUntilSecondAppAttemptServer(_FakeServer):
        def stop(self) -> None:
            stop_calls["n"] += 1
            call_log.append("server_stop")
            if stop_calls["n"] <= rt._CLEANUP_ATTEMPTS:
                raise RuntimeError("simulated stop failure")

    def factory(project_dir, config):
        return _FailUntilSecondAppAttemptServer(project_dir, config, call_log=call_log)

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

        app.exit()
        for _ in range(200):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)

    assert not _lock_path(repo.common_dir()).exists()
    # rt._CLEANUP_ATTEMPTS failing calls exhaust the first app-level
    # attempt's entire internal retry budget (confirming it as
    # genuinely unclean, not merely internally retried), plus one more
    # successful call made by the automatic second app-level attempt.
    assert call_log.count("server_stop") == rt._CLEANUP_ATTEMPTS + 1


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
async def test_shutdown_attempt_generations_do_not_cross_signal(tmp_path, monkeypatch):
    """Each cleanup generation owns its completion signal; completing the
    first attempt must not release a waiter for the distinct retry."""
    app = LoopSupervisorApp(tmp_path)
    async with app.run_test():
        screen = RunScreen(tmp_path, run_id=None)
        screen._app_ref = app
        screen._init_done_event.set()
        entered = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]
        calls = {"n": 0}

        def _controlled_shutdown() -> None:
            index = calls["n"]
            calls["n"] += 1
            entered[index].set()
            release[index].wait(timeout=10)

        monkeypatch.setattr(screen, "_do_shutdown", _controlled_shutdown)

        first = screen.action_request_shutdown()
        assert first is not None
        assert screen.action_request_shutdown() is first
        for _ in range(40):
            if entered[0].is_set():
                break
            await asyncio.sleep(0.05)
        assert entered[0].is_set()
        release[0].set()
        await asyncio.wait_for(screen.await_shutdown_complete(first), timeout=2)

        second = screen.action_request_shutdown()
        assert second is not None
        assert second is not first
        assert second.generation == first.generation + 1
        for _ in range(40):
            if entered[1].is_set():
                break
            await asyncio.sleep(0.05)
        assert entered[1].is_set()

        second_wait = asyncio.create_task(screen.await_shutdown_complete(second))
        await asyncio.sleep(0.1)
        assert not second_wait.done()
        assert first.completion.is_set()
        assert not second.completion.is_set()

        release[1].set()
        await asyncio.wait_for(second_wait, timeout=2)


def test_shutdown_worker_registration_failure_allows_fresh_attempt(tmp_path, monkeypatch):
    """A failed worker registration must not publish an attempt that can never run."""
    screen = RunScreen(tmp_path, run_id=None)
    screen._init_done_event.set()
    registration_attempts = []
    first_started = threading.Event()

    class _FailOnceWorkerApp:
        def run_worker(self, callable_, **_kwargs):
            registration_attempts.append(callable_)
            if len(registration_attempts) == 1:

                def _start_first() -> None:
                    first_started.set()
                    callable_()

                first = threading.Thread(target=_start_first, daemon=True)
                first.start()
                assert first_started.wait(timeout=2)
                raise RuntimeError("simulated worker registration failure")

        def finalize_run_screen(self, _screen):
            pass

    shutdown_calls = {"n": 0}

    def _count_shutdown() -> None:
        shutdown_calls["n"] += 1

    monkeypatch.setattr(screen, "_app_ref", _FailOnceWorkerApp())
    monkeypatch.setattr(screen, "_do_shutdown", _count_shutdown)

    with pytest.raises(RuntimeError, match="simulated worker registration failure"):
        screen.action_request_shutdown()

    assert screen._shutdown_attempt is None
    assert screen._shutdown_in_progress is False
    assert shutdown_calls["n"] == 0

    retry = screen.action_request_shutdown()
    assert retry is not None
    assert retry.generation == 2
    assert len(registration_attempts) == 2

    registration_attempts[1]()

    assert retry.completion.is_set()
    assert screen._shutdown_attempt is None
    assert screen._shutdown_in_progress is False
    assert shutdown_calls["n"] == 1


@pytest.mark.asyncio
async def test_clean_in_flight_shutdown_request_returns_same_incomplete_attempt(
    tmp_path, monkeypatch
):
    """Cleanup becoming clean does not finish its worker or its attempt.

    A concurrent requester in that window must receive and await the existing
    handle rather than treating the still-running worker as already complete.
    """
    screen = RunScreen(tmp_path, run_id=None)
    screen._init_done_event.set()
    cleanup_marked_clean = threading.Event()
    release_worker = threading.Event()
    worker_calls = []

    class _WorkerCapturingApp:
        def run_worker(self, callable_, **_kwargs):
            worker_calls.append(callable_)

        def finalize_run_screen(self, _screen):
            pass

    app = _WorkerCapturingApp()
    monkeypatch.setattr(screen, "_app_ref", app)

    def _clean_then_pause() -> None:
        screen._cleanup_resources()
        cleanup_marked_clean.set()
        release_worker.wait(timeout=10)

    monkeypatch.setattr(screen, "_do_shutdown", _clean_then_pause)

    first = screen.action_request_shutdown()
    assert first is not None
    assert len(worker_calls) == 1
    worker = threading.Thread(target=worker_calls[0])
    worker.start()
    assert cleanup_marked_clean.wait(timeout=2)
    assert screen.shutdown_clean
    assert not first.completion.is_set()

    concurrent = screen.action_request_shutdown()
    assert concurrent is not None
    assert concurrent is first
    concurrent_wait = asyncio.create_task(screen.await_shutdown_complete(concurrent))
    await asyncio.sleep(0.1)
    assert not concurrent_wait.done()

    release_worker.set()
    await asyncio.wait_for(concurrent_wait, timeout=2)
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert first.completion.is_set()


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


# --- Step 4: TUI ownership registry ------------------------------------


@pytest.mark.asyncio
async def test_registration_occurs_before_resource_acquisition(tmp_path, monkeypatch):
    """The screen must be registered in the app's lifecycle-ownership
    registry before it acquires any resource (lock/server) -- observed
    here by checking registration happens strictly before the lock file
    is created on disk."""
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    _patch_server(monkeypatch, call_log=call_log)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        screen = RunScreen(tmp_path / "repo", run_id=None)
        app.push_screen(screen)
        # Registration happens synchronously inside on_mount(), before
        # run_worker() schedules _initialize() on a background thread, so
        # it must already be true immediately after push_screen()'s
        # mount dispatch completes on the next pause.
        await pilot.pause()
        assert screen in app._owned_run_screens

        for _ in range(50):
            if screen._state is not None:
                break
            await pilot.pause(0.05)
        screen.action_request_shutdown()
        for _ in range(50):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)

    assert not _lock_path(repo.common_dir()).exists()


@pytest.mark.asyncio
async def test_detached_unclean_screen_remains_registered_and_auto_retried(tmp_path, monkeypatch):
    """A screen that becomes detached (unmounted) while unclean must stay
    in the registry and get an automatic retry coordinator -- with no
    interactive "q" press -- that eventually clears it once stop()
    succeeds."""
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    stop_calls = {"n": 0}

    class _FailTwiceServer(_FakeServer):
        def stop(self) -> None:
            stop_calls["n"] += 1
            call_log.append("server_stop")
            if stop_calls["n"] <= 2:
                raise RuntimeError("simulated stop failure")

    def factory(project_dir, config):
        return _FailTwiceServer(project_dir, config, call_log=call_log)

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

        # Directly pop the screen (simulating an unexpected detach) rather
        # than requesting shutdown first -- on_unmount() itself must both
        # request shutdown and start the automatic retry coordinator.
        app.pop_screen()
        await pilot.pause()

        assert screen in app._owned_run_screens
        assert screen not in app.screen_stack

        for _ in range(200):
            if screen not in app._owned_run_screens:
                break
            await pilot.pause(0.05)

    assert screen not in app._owned_run_screens
    assert not _lock_path(repo.common_dir()).exists()
    assert stop_calls["n"] >= 3


@pytest.mark.asyncio
async def test_stack_removal_unmount_race_cannot_hide_screen_from_exit(tmp_path, monkeypatch):
    """A screen popped from the stack (removed from _screen_stacks) while
    its cleanup is still blocked must still be found and cleaned up by
    app-level exit -- proving the registry, not _screen_stacks, is
    authoritative. A blocked advance() worker keeps the screen unclean
    long enough to observe it missing from every _screen_stacks entry
    while still present in the registry."""
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    _patch_server(monkeypatch, call_log=call_log)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        screen = RunScreen(tmp_path / "repo", run_id=None)
        app.push_screen(screen)
        await pilot.pause()
        for _ in range(50):
            if screen._state is not None:
                break
            await pilot.pause(0.05)

        release_advance = threading.Event()
        assert screen._session is not None
        assert screen._session._supervisor is not None
        real_advance = screen._session._supervisor.advance

        def blocked_advance(state):
            release_advance.wait(timeout=10)
            return real_advance(state)

        monkeypatch.setattr(screen._session._supervisor, "advance", blocked_advance)
        screen._start_advance()
        await pilot.pause(0.1)
        assert screen._transitioning is True

        app.pop_screen()
        await pilot.pause()
        # Confirm this screen is genuinely gone from Textual's own
        # bookkeeping, yet still registered and not yet clean (advance()
        # is still blocked).
        assert all(screen not in stack for stack in app._screen_stacks.values())
        assert screen in app._owned_run_screens
        assert not screen.shutdown_clean

        app.exit()
        time.sleep(0.2)
        assert _lock_path(repo.common_dir()).exists()

        release_advance.set()
        for _ in range(200):
            if not _lock_path(repo.common_dir()).exists():
                break
            await pilot.pause(0.05)

    assert not _lock_path(repo.common_dir()).exists()
    assert "server_stop" in call_log
    assert screen not in app._owned_run_screens


@pytest.mark.asyncio
async def test_detached_clean_finalize_does_not_pop_unrelated_active_screen(tmp_path, monkeypatch):
    """A detached screen that finalizes cleanly after a different screen
    has since become active must not pop that unrelated active screen --
    it may only deregister itself."""
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    _patch_server(monkeypatch, call_log=call_log)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        screen = RunScreen(tmp_path / "repo", run_id=None)
        app.push_screen(screen)
        await pilot.pause()
        for _ in range(50):
            if screen._state is not None:
                break
            await pilot.pause(0.05)

        # Detach the RunScreen and push an unrelated browser screen in its
        # place so it becomes the new active screen.
        app.pop_screen()
        await pilot.pause()
        browser = RunBrowserScreen(tmp_path / "repo")
        app.push_screen(browser)
        await pilot.pause()
        assert app.screen is browser

        # The detached screen is still registered; wait for its automatic
        # coordinator to finalize it.
        for _ in range(200):
            if screen not in app._owned_run_screens:
                break
            await pilot.pause(0.05)

        # The unrelated active screen must still be exactly the one we
        # pushed -- finalize_run_screen() must never have popped it.
        assert app.screen is browser

    assert not _lock_path(repo.common_dir()).exists()


@pytest.mark.asyncio
async def test_detached_lock_release_only_failure_remains_registered_and_retries(
    tmp_path, monkeypatch
):
    """A detached screen whose server.stop() succeeds but whose lock
    release fails must remain registered (and retried) -- the retry must
    skip the already-cleared server and retry only the lock release."""
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    release_calls = {"n": 0}

    _patch_server(monkeypatch, call_log=call_log)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        screen = RunScreen(tmp_path / "repo", run_id=None)
        app.push_screen(screen)
        await pilot.pause()
        for _ in range(50):
            if screen._state is not None:
                break
            await pilot.pause(0.05)

        # RunSession wraps the SupervisorLock in a _LockLease
        # (session._lease._lock); patch the underlying lock's release()
        # directly, one level below RunSession's own retry logic, so this
        # test observes RunSession.close() retrying the release itself
        # rather than a fake shortcut.
        assert screen._session is not None
        assert screen._session._lease is not None
        original_release = screen._session._lease._lock.release

        def _flaky_release():
            release_calls["n"] += 1
            if release_calls["n"] == 1:
                # Must be a LockError specifically: RunSession.close()
                # only treats a release failure as retryable
                # (SessionState.RELEASE_PENDING, skipping stop() on
                # retry) when lease.release() raises LockError -- any
                # other exception type propagates uncaught by that
                # branch, which would make the retry re-invoke stop() on
                # an already-cleared server instead of skipping it, as
                # the assertion below would otherwise incorrectly show.
                raise LockError("simulated transient lock-release failure")
            return original_release()

        monkeypatch.setattr(screen._session._lease._lock, "release", _flaky_release)

        app.pop_screen()
        await pilot.pause()
        assert screen in app._owned_run_screens

        for _ in range(200):
            if screen not in app._owned_run_screens:
                break
            await pilot.pause(0.05)

    assert screen not in app._owned_run_screens
    assert not _lock_path(repo.common_dir()).exists()
    assert release_calls["n"] >= 2
    # The server must have been stopped exactly once -- the retry must
    # not re-invoke stop() on an already-cleared server.
    assert call_log.count("server_stop") == 1


@pytest.mark.asyncio
async def test_app_exit_rechecks_registry_for_screens_added_while_draining(tmp_path, monkeypatch):
    """A new RunScreen registered while _on_exit_app() is already draining
    must also be waited on before the underlying Textual exit proceeds."""
    _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    stop_calls = {"n": 0}

    class _SlowFirstScreenServer(_FakeServer):
        def stop(self) -> None:
            stop_calls["n"] += 1
            call_log.append("server_stop")

    def factory(project_dir, config):
        return _SlowFirstScreenServer(project_dir, config, call_log=call_log)

    _patch_server(monkeypatch, factory=factory)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        first = RunScreen(tmp_path / "repo", run_id=None)
        app.push_screen(first)
        await pilot.pause()
        for _ in range(50):
            if first._state is not None:
                break
            await pilot.pause(0.05)

        second_started = threading.Event()
        real_do_initialize = RunScreen._do_initialize

        def _gate_then_initialize(self):
            second_started.wait(timeout=10)
            real_do_initialize(self)

        # Register a second RunScreen but hold its initialization until
        # after exit has already started draining, so it is only added to
        # the registry via on_mount() concurrently with the drain loop's
        # first iteration.
        second = RunScreen(tmp_path / "repo2", run_id=None)
        monkeypatch.setattr(second, "_do_initialize", _gate_then_initialize.__get__(second))

        app.exit()
        app.push_screen(second)
        # _on_exit_app() runs as part of dispatching the ExitApp message
        # on the App's own message pump, which therefore cannot process
        # any further App-level messages (including callbacks driving
        # pilot.pause()'s idle-wait) until it returns. The pushed screen's
        # own Mount dispatch runs on its own independent message-pump
        # task, though, so a plain asyncio.sleep() (not pilot.pause(),
        # which would itself block on the App's now-busy message pump)
        # can still observe it completing.
        for _ in range(50):
            if second in app._owned_run_screens:
                break
            await asyncio.sleep(0.05)
        assert second in app._owned_run_screens
        second_started.set()

        for _ in range(200):
            if not app._owned_run_screens:
                break
            await asyncio.sleep(0.05)

    assert not app._owned_run_screens


@pytest.mark.asyncio
async def test_on_unmount_and_exit_drain_share_one_coordinator(tmp_path, monkeypatch):
    """on_unmount() and _on_exit_app()'s drain loop must not each start
    their own overlapping coordinator for the same screen -- only one
    stop() call should ever be in flight at a time."""
    _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    concurrent = {"n": 0, "max": 0}
    lock = threading.Lock()

    class _SlowStopServer(_FakeServer):
        def stop(self) -> None:
            with lock:
                concurrent["n"] += 1
                concurrent["max"] = max(concurrent["max"], concurrent["n"])
            call_log.append("server_stop")
            time.sleep(0.2)
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

        app.pop_screen()
        app.exit()
        for _ in range(200):
            if not app._owned_run_screens:
                break
            await pilot.pause(0.05)

    assert not app._owned_run_screens
    assert concurrent["max"] == 1


@pytest.mark.asyncio
async def test_base_exit_never_called_while_registry_non_empty(tmp_path, monkeypatch):
    """super()._on_exit_app() (the underlying Textual shutdown) must never
    be invoked while any screen remains in the app's ownership registry,
    regardless of Textual's own _screen_stacks bookkeeping."""
    _init_repo(tmp_path / "repo")
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

    base_exit_registry_snapshots: list[bool] = []
    from textual.app import App as _TextualApp

    original_on_exit_app = _TextualApp._on_exit_app

    app_ref: dict[str, LoopSupervisorApp] = {}

    async def _spy_on_exit_app(self):
        app = app_ref.get("a")
        base_exit_registry_snapshots.append(bool(app._owned_run_screens) if app else False)
        await original_on_exit_app(self)

    monkeypatch.setattr(_TextualApp, "_on_exit_app", _spy_on_exit_app)

    app = LoopSupervisorApp(tmp_path / "repo")
    app_ref["a"] = app
    async with app.run_test() as pilot:
        screen = RunScreen(tmp_path / "repo", run_id=None)
        app.push_screen(screen)
        await pilot.pause()
        for _ in range(50):
            if screen._state is not None:
                break
            await pilot.pause(0.05)

        app.exit()
        for _ in range(200):
            if not app._owned_run_screens:
                break
            await pilot.pause(0.05)

    assert not app._owned_run_screens
    assert base_exit_registry_snapshots
    # Every time the underlying Textual exit sequence actually ran, the
    # registry must have already been empty.
    assert all(not is_nonempty for is_nonempty in base_exit_registry_snapshots)


@pytest.mark.asyncio
async def test_registry_membership_clears_only_after_quiescence_and_clean_release(
    tmp_path, monkeypatch
):
    """Registry membership must persist through a blocked advance() worker
    and a failed stop() -- clearing only once both quiescence
    (_init_done_event/_advance_done_event) and a clean resource release
    are achieved simultaneously."""
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

        release_advance = threading.Event()
        assert screen._session is not None
        assert screen._session._supervisor is not None
        real_advance = screen._session._supervisor.advance

        def blocked_advance(state):
            release_advance.wait(timeout=10)
            return real_advance(state)

        monkeypatch.setattr(screen._session._supervisor, "advance", blocked_advance)
        screen._start_advance()
        await pilot.pause(0.1)
        assert screen._transitioning is True

        attempt = screen.action_request_shutdown()
        assert attempt is not None
        time.sleep(0.2)
        # Still registered: advance() has not unwound yet.
        assert screen in app._owned_run_screens
        assert not screen.ready_to_finalize

        release_advance.set()
        for _ in range(50):
            if attempt.completion.is_set():
                break
            await pilot.pause(0.05)

        # RunSession.close() retries server.stop() internally (up to
        # rt._CLEANUP_ATTEMPTS times, see ADR 0009): the fail-once
        # server's transient failure is confirmed on its second internal
        # attempt within this single external shutdown attempt, so the
        # screen must already be quiescent and clean, and therefore
        # deregistered, without a second action_request_shutdown() call.
        assert screen.shutdown_clean is True
        for _ in range(100):
            if screen not in app._owned_run_screens:
                break
            await pilot.pause(0.05)

    assert screen not in app._owned_run_screens
    assert not _lock_path(repo.common_dir()).exists()


@pytest.mark.asyncio
async def test_no_leftover_cleanup_task_lock_or_server_after_clean_exit(tmp_path, monkeypatch):
    """After a fully clean app exit, no coordinator task, lock file,
    server owner, or registry entry may remain."""
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    _patch_server(monkeypatch, call_log=call_log)

    app = LoopSupervisorApp(tmp_path / "repo")
    async with app.run_test() as pilot:
        screen = RunScreen(tmp_path / "repo", run_id=None)
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
    assert not app._owned_run_screens
    assert not app._run_screen_cleanup_tasks
    # RunSession never nulls its own server/lock references on a clean
    # close() (see RunScreen._session's docstring); the faithful "fully
    # released" signal is its terminal SessionState.
    assert screen._session is not None
    assert screen._session.state is rt.SessionState.CLOSED
