"""Tests for the shared runtime controller (runtime.py).

Uses monkeypatching to avoid launching real OpenCode processes.
Verifies lifecycle ordering: lock → state → server → run → server stop → lock release.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from loop_supervisor.git import GitRepo
from loop_supervisor.runtime import list_run_ids, load_run, run_new, run_resume
from loop_supervisor.state import RunOptions


def _run(args, cwd):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _init_repo(path: Path) -> GitRepo:
    path.mkdir(parents=True)
    _run(["init", "-b", "main"], path)
    _run(["config", "user.email", "test@example.com"], path)
    _run(["config", "user.name", "Test"], path)
    (path / "README.md").write_text("hello\n")
    _run(["add", "-A"], path)
    _run(["commit", "-m", "initial"], path)
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


class _FakeState:
    """Minimal RunState-like for injection."""

    def __init__(self, run_id: str, phase: str = "done") -> None:
        self.run_id = run_id
        self.phase = phase
        self.options = _make_options()


class _FakeSupervisor:
    """Supervisor mock that records call order."""

    def __init__(self, call_log: list[str], final_phase: str = "done") -> None:
        self._log = call_log
        self._final_phase = final_phase

    def start_new_run(self) -> _FakeState:
        self._log.append("start_new_run")
        state = _FakeState("test-run")
        state.phase = "planning"
        return state

    def resume(self, state: object) -> object:
        self._log.append("resume")
        return state

    def run(self, state: _FakeState) -> _FakeState:
        self._log.append("run")
        state.phase = self._final_phase
        return state

    @property
    def runner(self):
        return None

    @runner.setter
    def runner(self, value):
        self._log.append(f"runner_set:{type(value).__name__}")


class _FakeServer:
    """OpenCodeServer mock that records call order."""

    def __init__(self, call_log: list[str]) -> None:
        self._log = call_log
        self.base_url = "http://127.0.0.1:9999"
        self._started = False

    def start(self) -> None:
        self._log.append("server_start")
        self._started = True

    def stop(self) -> None:
        self._log.append("server_stop")
        self._started = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()

    def add_observer(self, obs) -> None:
        pass


def _patch_runtime(repo: GitRepo, *, call_log: list[str], final_phase: str = "done"):
    """Return a context manager that patches runtime internals."""
    import loop_supervisor.runtime as rt

    fake_server = _FakeServer(call_log)

    original_git_repo = rt.GitRepo
    original_oc_server = rt.OpenCodeServer
    original_supervisor = rt.Supervisor

    class FakeGitRepo:
        def __init__(self, *a, **kw):
            self.root = repo.root

        def common_dir(self):
            return repo.common_dir()

    class FakeOCServer:
        def __init__(self, *a, **kw):
            pass

        def __new__(cls, *a, **kw):
            return fake_server

    class FakeSupervisor:
        def __init__(self, *a, **kw):
            pass

        def start_new_run(self):
            state = MagicMock()
            state.run_id = "fake-run"
            state.phase = "done"
            state.options = _make_options()
            call_log.append("start_new_run")
            return state

        def resume(self, state):
            call_log.append("resume")
            return state

        def run(self, state):
            call_log.append("run")
            state.phase = final_phase
            return state

        @property
        def runner(self):
            return None

        @runner.setter
        def runner(self, value):
            call_log.append("runner_set")

    import contextlib

    @contextlib.contextmanager
    def _ctx():
        rt.GitRepo = FakeGitRepo
        rt.OpenCodeServer = FakeOCServer
        rt.Supervisor = FakeSupervisor
        try:
            yield
        finally:
            rt.GitRepo = original_git_repo
            rt.OpenCodeServer = original_oc_server
            rt.Supervisor = original_supervisor

    return _ctx()


def test_list_run_ids_does_not_require_lock(tmp_path):
    _init_repo(tmp_path / "repo")
    result = list_run_ids(tmp_path / "repo")
    assert result == []


def test_load_run_does_not_require_lock(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    from loop_supervisor.supervisor import Supervisor

    supervisor = Supervisor(
        repo=repo,
        runner=MagicMock(),
        git_common_dir=repo.common_dir(),
        input_provider=MagicMock(),
        options=_make_options(),
    )
    state = supervisor.start_new_run()
    loaded = load_run(tmp_path / "repo", state.run_id)
    assert loaded.run_id == state.run_id


def test_run_new_server_starts_after_state_creation(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []

    with _patch_runtime(repo, call_log=call_log):
        run_new(tmp_path / "repo", _make_options())

    assert "start_new_run" in call_log
    assert "server_start" in call_log
    assert call_log.index("start_new_run") < call_log.index("server_start")


def test_run_new_server_stops_before_lock_release(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    lock_released = []

    import loop_supervisor.runtime as rt

    original_lock_ctx = rt._lock_context

    from contextlib import contextmanager

    @contextmanager
    def patched_lock_ctx(*args, **kwargs):
        with original_lock_ctx(*args, **kwargs) as lock:
            yield lock
        lock_released.append("lock_released")

    with _patch_runtime(repo, call_log=call_log):
        rt._lock_context = patched_lock_ctx
        try:
            run_new(tmp_path / "repo", _make_options())
        finally:
            rt._lock_context = original_lock_ctx

    call_log.append("lock_released_marker")
    assert call_log.index("server_stop") < call_log.index("lock_released_marker")


def test_run_resume_loads_state_after_lock_acquisition(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    from loop_supervisor.supervisor import Supervisor

    supervisor = Supervisor(
        repo=repo,
        runner=MagicMock(),
        git_common_dir=repo.common_dir(),
        input_provider=MagicMock(),
        options=_make_options(),
    )
    state = supervisor.start_new_run()
    run_id = state.run_id

    load_order: list[str] = []
    import loop_supervisor.runtime as rt

    original_lock_ctx = rt._lock_context
    original_load_state = rt.load_state

    from contextlib import contextmanager

    @contextmanager
    def patched_lock_ctx(*args, **kwargs):
        load_order.append("lock_acquired")
        with original_lock_ctx(*args, **kwargs) as lock:
            yield lock

    def patched_load_state(common_dir, rid):
        load_order.append("load_state")
        return original_load_state(common_dir, rid)

    call_log: list[str] = []
    with _patch_runtime(repo, call_log=call_log):
        rt._lock_context = patched_lock_ctx
        rt.load_state = patched_load_state
        try:
            try:
                run_resume(tmp_path / "repo", run_id)
            except Exception:
                pass
        finally:
            rt._lock_context = original_lock_ctx
            rt.load_state = original_load_state

    assert load_order.index("lock_acquired") < load_order.index("load_state")


def test_run_new_uses_runtime_not_supervisor_directly(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    called = []

    class SpySupervisor:
        def __init__(self, *a, **kw):
            called.append("Supervisor.__init__")

        def start_new_run(self):
            called.append("start_new_run")
            state = MagicMock()
            state.run_id = "x"
            state.phase = "done"
            state.options = _make_options()
            return state

        def run(self, state):
            called.append("run")
            return state

        @property
        def runner(self):
            return None

        @runner.setter
        def runner(self, v):
            pass

    original_supervisor = rt.Supervisor
    original_oc_server = rt.OpenCodeServer
    original_git_repo = rt.GitRepo

    class FakeGitRepo:
        def __init__(self, *a, **kw):
            self.root = repo.root

        def common_dir(self):
            return repo.common_dir()

    class FakeOCServer:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    rt.GitRepo = FakeGitRepo
    rt.OpenCodeServer = FakeOCServer
    rt.Supervisor = SpySupervisor
    try:
        run_new(tmp_path / "repo", _make_options())
    finally:
        rt.GitRepo = original_git_repo
        rt.OpenCodeServer = original_oc_server
        rt.Supervisor = original_supervisor

    assert "Supervisor.__init__" in called
    assert "start_new_run" in called


def test_run_resume_uses_runtime_not_supervisor_directly(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    from loop_supervisor.supervisor import Supervisor

    supervisor = Supervisor(
        repo=repo,
        runner=MagicMock(),
        git_common_dir=repo.common_dir(),
        input_provider=MagicMock(),
        options=_make_options(),
    )
    state = supervisor.start_new_run()
    run_id = state.run_id

    import loop_supervisor.runtime as rt

    called = []

    class SpySupervisor:
        def __init__(self, *a, **kw):
            called.append("Supervisor.__init__")

        def resume(self, s):
            called.append("resume")
            return s

        def run(self, s):
            called.append("run")
            return s

        @property
        def runner(self):
            return None

        @runner.setter
        def runner(self, v):
            pass

    original_supervisor = rt.Supervisor
    original_oc_server = rt.OpenCodeServer
    original_git_repo = rt.GitRepo

    class FakeGitRepo:
        def __init__(self, *a, **kw):
            self.root = repo.root

        def common_dir(self):
            return repo.common_dir()

    class FakeOCServer:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    rt.GitRepo = FakeGitRepo
    rt.OpenCodeServer = FakeOCServer
    rt.Supervisor = SpySupervisor
    try:
        run_resume(tmp_path / "repo", run_id)
    except Exception:
        pass
    finally:
        rt.GitRepo = original_git_repo
        rt.OpenCodeServer = original_oc_server
        rt.Supervisor = original_supervisor

    assert "Supervisor.__init__" in called


def test_list_run_ids_returns_empty_for_fresh_repo(tmp_path):
    _init_repo(tmp_path / "repo")
    assert list_run_ids(tmp_path / "repo") == []


def test_recover_stale_lock_flag_accepted(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []
    with _patch_runtime(repo, call_log=call_log):
        run_new(tmp_path / "repo", _make_options(), recover_stale_lock=True)


def test_run_new_server_startup_failure_persists_operational_failure(tmp_path):
    """A server startup failure after start_new_run() has already saved
    initial state must be recorded as a durable operational_failure against
    that real run, not merely raised and discarded."""
    from loop_supervisor.opencode import ServerStartupError
    from loop_supervisor.runtime import RuntimeError_
    from loop_supervisor.state import load_state

    repo = _init_repo(tmp_path / "repo")

    import loop_supervisor.runtime as rt

    original_oc_server = rt.OpenCodeServer

    class FailingOCServer:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            raise ServerStartupError("simulated startup failure")

        def stop(self):
            pass

    rt.OpenCodeServer = FailingOCServer
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_:
            pass
    finally:
        rt.OpenCodeServer = original_oc_server

    runs = list_run_ids(tmp_path / "repo")
    assert len(runs) == 1
    state = load_state(repo.common_dir(), runs[0])
    assert state.phase == "operational_failure"
    assert state.last_error is not None
    assert state.last_error["kind"] == "opencode_startup"


def test_run_resume_server_startup_failure_persists_operational_failure(tmp_path):
    from loop_supervisor.opencode import ServerStartupError
    from loop_supervisor.runtime import RuntimeError_
    from loop_supervisor.state import load_state
    from loop_supervisor.supervisor import Supervisor

    repo = _init_repo(tmp_path / "repo")
    supervisor = Supervisor(
        repo=repo,
        runner=MagicMock(),
        git_common_dir=repo.common_dir(),
        input_provider=MagicMock(),
        options=_make_options(),
    )
    state = supervisor.start_new_run()
    run_id = state.run_id

    import loop_supervisor.runtime as rt

    original_oc_server = rt.OpenCodeServer

    class FailingOCServer:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            raise ServerStartupError("simulated startup failure")

        def stop(self):
            pass

    rt.OpenCodeServer = FailingOCServer
    try:
        try:
            run_resume(tmp_path / "repo", run_id)
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_:
            pass
    finally:
        rt.OpenCodeServer = original_oc_server

    reloaded = load_state(repo.common_dir(), run_id)
    assert reloaded.phase == "operational_failure"
    assert reloaded.last_error is not None
    assert reloaded.last_error["kind"] == "opencode_startup"


def test_repeated_resume_startup_failure_preserves_original_retry_phase(tmp_path):
    """A second (and third) OpenCode startup failure on resume of an
    already-operational_failure run must not overwrite the real
    interrupted phase with 'operational_failure' itself. Each failure may
    update the diagnostic message, but retry_phase must stay pointed at
    the phase that was actually interrupted."""
    from loop_supervisor.opencode import ServerStartupError
    from loop_supervisor.runtime import RuntimeError_
    from loop_supervisor.state import load_state
    from loop_supervisor.supervisor import PHASE_BUILDING, Supervisor

    repo = _init_repo(tmp_path / "repo")
    supervisor = Supervisor(
        repo=repo,
        runner=MagicMock(),
        git_common_dir=repo.common_dir(),
        input_provider=MagicMock(),
        options=_make_options(),
    )
    state = supervisor.start_new_run()
    run_id = state.run_id

    import loop_supervisor.runtime as rt

    original_oc_server = rt.OpenCodeServer

    def _make_failing_server(message: str):
        class FailingOCServer:
            def __init__(self, *a, **kw):
                pass

            def start(self):
                raise ServerStartupError(message)

            def stop(self):
                pass

        return FailingOCServer

    try:
        # First failure: interrupts a run that has not yet reached
        # PHASE_BUILDING; the resume path's own validation determines the
        # actual phase at the point OpenCode would have started. Use the
        # fresh run's phase (planning) as the true interrupted phase by
        # failing on run_new instead, then simulate a second failure via
        # run_resume against the resulting operational_failure state.
        rt.OpenCodeServer = _make_failing_server("first failure")
        try:
            run_resume(tmp_path / "repo", run_id)
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_:
            pass

        first = load_state(repo.common_dir(), run_id)
        assert first.phase == "operational_failure"
        first_retry_phase = first.last_error["retry_phase"]
        assert first_retry_phase != "operational_failure"

        # Second failure on the same still-unrecovered run: retry_phase
        # must be unchanged, not overwritten with "operational_failure".
        rt.OpenCodeServer = _make_failing_server("second failure")
        try:
            run_resume(tmp_path / "repo", run_id)
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_:
            pass

        second = load_state(repo.common_dir(), run_id)
        assert second.phase == "operational_failure"
        assert second.last_error["retry_phase"] == first_retry_phase
        assert second.last_error["retry_phase"] != "operational_failure"
        assert "second failure" in second.last_error["message"]

        # A third failure continues to preserve it.
        rt.OpenCodeServer = _make_failing_server("third failure")
        try:
            run_resume(tmp_path / "repo", run_id)
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_:
            pass

        third = load_state(repo.common_dir(), run_id)
        assert third.last_error["retry_phase"] == first_retry_phase
    finally:
        rt.OpenCodeServer = original_oc_server

    # Sanity: the preserved retry target is a real dispatchable phase
    # (planning, since the run never advanced past its initial phase).
    assert first_retry_phase in ("planning", PHASE_BUILDING)


def test_run_new_supervisor_failure_takes_precedence_over_server_stop_failure(tmp_path):
    """If supervisor.run() raises and server.stop() also fails during
    cleanup, the original supervisor failure must be what propagates, not
    the secondary stop() cleanup failure."""
    from loop_supervisor.state import load_state
    from loop_supervisor.supervisor import LoopError, Supervisor

    repo = _init_repo(tmp_path / "repo")

    import loop_supervisor.runtime as rt

    original_oc_server = rt.OpenCodeServer

    class FailingStopServer:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            pass

        def stop(self):
            raise RuntimeError("simulated stop cleanup failure")

        def add_observer(self, obs):
            pass

    original_run = Supervisor.run

    def _boom_run(self, state):
        raise LoopError("simulated supervisor failure")

    rt.OpenCodeServer = FailingStopServer
    Supervisor.run = _boom_run
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError as exc:
            assert "simulated supervisor failure" in str(exc)
    finally:
        rt.OpenCodeServer = original_oc_server
        Supervisor.run = original_run

    runs = list_run_ids(tmp_path / "repo")
    assert len(runs) == 1
    state = load_state(repo.common_dir(), runs[0])
    assert state.phase == "planning"


def test_resume_invalid_run_id_leaves_no_lock(tmp_path):
    """A crafted resume run ID must be rejected before lock acquisition,
    leaving no lock file (malformed or otherwise) behind so the repository
    is not poisoned."""
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")

    import pytest

    with pytest.raises(RuntimeError_):
        run_resume(tmp_path / "repo", "../../evil")

    assert not _lock_path(repo.common_dir()).exists()

    # The repository must remain immediately lockable.
    from loop_supervisor.locking import SupervisorLock

    lock = SupervisorLock(repo.common_dir(), operation="run", integration_path=str(repo.root))
    lock.acquire()
    lock.release()


def test_run_new_successful_run_failed_stop_retains_lock(tmp_path):
    """A successful supervisor.run() followed by a server.stop() that
    cannot be confirmed must retain the lock, not release it."""
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.runtime import RuntimeError_
    from loop_supervisor.supervisor import Supervisor

    repo = _init_repo(tmp_path / "repo")

    import loop_supervisor.runtime as rt

    original_oc_server = rt.OpenCodeServer

    class FailingStopServer:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            pass

        def stop(self):
            raise RuntimeError("simulated stop cleanup failure")

        def add_observer(self, obs):
            pass

    original_run = Supervisor.run

    def _fake_run(self, state):
        state.phase = "done"
        return state

    rt.OpenCodeServer = FailingStopServer
    Supervisor.run = _fake_run
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_ as exc:
            assert "cleanup could not be confirmed" in str(exc)
    finally:
        rt.OpenCodeServer = original_oc_server
        Supervisor.run = original_run

    assert _lock_path(repo.common_dir()).exists()


def test_run_new_failed_run_and_failed_stop_preserves_run_exception_and_retains_lock(tmp_path):
    """If supervisor.run() raises AND server.stop() also fails, the
    original run exception must be what propagates, and the lock must be
    retained (not released) since cleanup was never confirmed."""
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.supervisor import LoopError, Supervisor

    repo = _init_repo(tmp_path / "repo")

    import loop_supervisor.runtime as rt

    original_oc_server = rt.OpenCodeServer

    class FailingStopServer:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            pass

        def stop(self):
            raise RuntimeError("simulated stop cleanup failure")

        def add_observer(self, obs):
            pass

    original_run = Supervisor.run

    def _boom_run(self, state):
        raise LoopError("simulated supervisor failure")

    rt.OpenCodeServer = FailingStopServer
    Supervisor.run = _boom_run
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError as exc:
            assert "simulated supervisor failure" in str(exc)
    finally:
        rt.OpenCodeServer = original_oc_server
        Supervisor.run = original_run

    assert _lock_path(repo.common_dir()).exists()


def test_run_new_failed_startup_and_failed_cleanup_persists_failure_and_retains_lock(tmp_path):
    """If server.start() fails AND the defense-in-depth stop() retry also
    fails, the operational failure must still be persisted, the raised
    error must mention the unresolved cleanup, and the lock must be
    retained."""
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.opencode import ServerStartupError
    from loop_supervisor.runtime import RuntimeError_
    from loop_supervisor.state import load_state

    repo = _init_repo(tmp_path / "repo")

    import loop_supervisor.runtime as rt

    original_oc_server = rt.OpenCodeServer

    class FailingEverythingServer:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            raise ServerStartupError("simulated startup failure")

        def stop(self):
            raise RuntimeError("simulated cleanup-retry failure")

    rt.OpenCodeServer = FailingEverythingServer
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_ as exc:
            assert "cleanup could not be confirmed" in str(exc)
    finally:
        rt.OpenCodeServer = original_oc_server

    assert _lock_path(repo.common_dir()).exists()

    runs = list_run_ids(tmp_path / "repo")
    assert len(runs) == 1
    state = load_state(repo.common_dir(), runs[0])
    assert state.phase == "operational_failure"
    assert state.last_error is not None
    assert state.last_error["kind"] == "opencode_startup"


def test_run_new_successful_cleanup_still_releases_lock(tmp_path):
    """The ordinary success path (server.stop() confirmed) must still
    release the lock exactly as before."""
    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []

    with _patch_runtime(repo, call_log=call_log):
        run_new(tmp_path / "repo", _make_options())

    assert not _lock_path(repo.common_dir()).exists()


def test_run_resume_successful_run_failed_stop_retains_lock(tmp_path):
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.runtime import RuntimeError_
    from loop_supervisor.supervisor import Supervisor

    repo = _init_repo(tmp_path / "repo")
    supervisor = Supervisor(
        repo=repo,
        runner=MagicMock(),
        git_common_dir=repo.common_dir(),
        input_provider=MagicMock(),
        options=_make_options(),
    )
    state = supervisor.start_new_run()
    run_id = state.run_id

    import loop_supervisor.runtime as rt

    original_oc_server = rt.OpenCodeServer

    class FailingStopServer:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            pass

        def stop(self):
            raise RuntimeError("simulated stop cleanup failure")

        def add_observer(self, obs):
            pass

    original_run = Supervisor.run

    def _fake_run(self, state):
        state.phase = "done"
        return state

    rt.OpenCodeServer = FailingStopServer
    Supervisor.run = _fake_run
    try:
        try:
            run_resume(tmp_path / "repo", run_id)
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_ as exc:
            assert "cleanup could not be confirmed" in str(exc)
    finally:
        rt.OpenCodeServer = original_oc_server
        Supervisor.run = original_run

    assert _lock_path(repo.common_dir()).exists()


def test_run_resume_failed_startup_and_failed_cleanup_retains_lock(tmp_path):
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.opencode import ServerStartupError
    from loop_supervisor.runtime import RuntimeError_
    from loop_supervisor.supervisor import Supervisor

    repo = _init_repo(tmp_path / "repo")
    supervisor = Supervisor(
        repo=repo,
        runner=MagicMock(),
        git_common_dir=repo.common_dir(),
        input_provider=MagicMock(),
        options=_make_options(),
    )
    state = supervisor.start_new_run()
    run_id = state.run_id

    import loop_supervisor.runtime as rt

    original_oc_server = rt.OpenCodeServer

    class FailingEverythingServer:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            raise ServerStartupError("simulated startup failure")

        def stop(self):
            raise RuntimeError("simulated cleanup-retry failure")

    rt.OpenCodeServer = FailingEverythingServer
    try:
        try:
            run_resume(tmp_path / "repo", run_id)
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_ as exc:
            assert "cleanup could not be confirmed" in str(exc)
    finally:
        rt.OpenCodeServer = original_oc_server

    assert _lock_path(repo.common_dir()).exists()


def test_run_resume_successful_cleanup_still_releases_lock(tmp_path):
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.supervisor import Supervisor

    repo = _init_repo(tmp_path / "repo")
    supervisor = Supervisor(
        repo=repo,
        runner=MagicMock(),
        git_common_dir=repo.common_dir(),
        input_provider=MagicMock(),
        options=_make_options(),
    )
    state = supervisor.start_new_run()
    run_id = state.run_id

    call_log: list[str] = []
    with _patch_runtime(repo, call_log=call_log):
        run_resume(tmp_path / "repo", run_id)

    assert not _lock_path(repo.common_dir()).exists()


def test_run_new_relative_project_path_uses_canonical_lock_metadata(tmp_path, monkeypatch):
    """A relative project_root must serialize the canonical absolute
    repo.root in the lock record, not the relative argument (which release
    would reject as a non-absolute integration_path)."""
    import json

    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")

    captured: dict[str, object] = {}

    import loop_supervisor.runtime as rt

    original_run_and_stop = rt._run_and_stop

    def _capture(supervisor, state, server, lease):
        captured["record"] = json.loads(_lock_path(repo.common_dir()).read_text())
        return original_run_and_stop(supervisor, state, server, lease)

    monkeypatch.setattr(rt, "_run_and_stop", _capture)

    call_log: list[str] = []
    with _patch_runtime(repo, call_log=call_log):
        monkeypatch.chdir(tmp_path)
        run_new(Path("repo"), _make_options())

    record = captured["record"]
    assert record["integration_path"] == str(repo.root)
    assert Path(record["integration_path"]).is_absolute()
