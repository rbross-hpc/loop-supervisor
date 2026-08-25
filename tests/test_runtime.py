"""Tests for the shared runtime controller (runtime.py).

Uses monkeypatching to avoid launching real OpenCode processes.
Verifies lifecycle ordering: lock → state → server → run → server stop → lock release.
"""

from __future__ import annotations

import ast
import contextlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

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

    original_release = rt._LockLease.release

    def patched_release(self):
        original_release(self)
        call_log.append("lock_released")
        lock_released.append("lock_released")

    with _patch_runtime(repo, call_log=call_log):
        rt._LockLease.release = patched_release
        try:
            run_new(tmp_path / "repo", _make_options())
        finally:
            rt._LockLease.release = original_release

    assert lock_released == ["lock_released"]
    assert call_log.index("server_stop") < call_log.index("lock_released")


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
    from loop_supervisor.locking import SupervisorLock

    original_acquire = SupervisorLock.acquire
    original_load_state = rt.load_state

    def patched_acquire(self):
        original_acquire(self)
        load_order.append("lock_acquired")

    def patched_load_state(common_dir, rid):
        load_order.append("load_state")
        return original_load_state(common_dir, rid)

    call_log: list[str] = []
    with _patch_runtime(repo, call_log=call_log):
        SupervisorLock.acquire = patched_acquire
        rt.load_state = patched_load_state
        try:
            try:
                run_resume(tmp_path / "repo", run_id)
            except Exception:
                pass
        finally:
            SupervisorLock.acquire = original_acquire
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


def test_run_new_successful_run_failed_stop_with_unprintable_str_retains_lock(tmp_path):
    """A successful supervisor.run() followed by a server.stop() that
    raises an exception with a broken/adversarial __str__ must still
    produce a deterministic RuntimeError_ message (falling back to an
    "unprintable ..." rendering) rather than crashing while composing
    the unresolved-cleanup diagnostic."""
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.runtime import RuntimeError_
    from loop_supervisor.supervisor import Supervisor

    repo = _init_repo(tmp_path / "repo")

    import loop_supervisor.runtime as rt

    original_oc_server = rt.OpenCodeServer

    class _UnprintableStopError(RuntimeError):
        def __str__(self) -> str:
            raise RuntimeError("simulated str failure in stop error")

    class FailingStopServer:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            pass

        def stop(self):
            raise _UnprintableStopError("simulated stop cleanup failure")

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
            assert "unprintable _UnprintableStopError" in str(exc)
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

    original_run_to_completion = rt.RunSession.run_to_completion

    def _capture(self):
        captured["record"] = json.loads(_lock_path(repo.common_dir()).read_text())
        return original_run_to_completion(self)

    monkeypatch.setattr(rt.RunSession, "run_to_completion", _capture)

    call_log: list[str] = []
    with _patch_runtime(repo, call_log=call_log):
        monkeypatch.chdir(tmp_path)
        run_new(Path("repo"), _make_options())

    record = captured["record"]
    assert record["integration_path"] == str(repo.root)
    assert Path(record["integration_path"]).is_absolute()


# --- Step 3: headless cleanup completeness -----------------------------


def _flaky_stop_server(*, fail_times: int, stop_exc_factory=None):
    """Build an OpenCodeServer stand-in whose stop() fails `fail_times`
    times (raising RuntimeError by default, or whatever
    `stop_exc_factory()` returns) and then succeeds, recording every
    stop() call in `.stop_calls`."""

    class FlakyStopServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"
            self.stop_calls = 0

        def start(self) -> None:
            pass

        def stop(self) -> None:
            self.stop_calls += 1
            if self.stop_calls <= fail_times:
                if stop_exc_factory is not None:
                    raise stop_exc_factory()
                raise RuntimeError(f"simulated transient stop failure #{self.stop_calls}")

        def add_observer(self, obs) -> None:
            pass

    return FlakyStopServer


def test_confirm_server_stopped_retries_exact_count_and_backoff(monkeypatch):
    """_confirm_server_stopped must retry up to _CLEANUP_ATTEMPTS times
    with the documented bounded backoff between attempts."""
    import loop_supervisor.runtime as rt

    sleeps: list[float] = []
    monkeypatch.setattr(rt.time, "sleep", lambda s: sleeps.append(s))

    class AlwaysFailsServer:
        def __init__(self) -> None:
            self.stop_calls = 0

        def stop(self) -> None:
            self.stop_calls += 1
            raise RuntimeError(f"fail {self.stop_calls}")

    server = AlwaysFailsServer()
    outcome = rt._confirm_server_stopped(server)

    assert outcome.confirmed is False
    assert outcome.attempts == rt._CLEANUP_ATTEMPTS
    assert server.stop_calls == rt._CLEANUP_ATTEMPTS
    assert sleeps == [
        rt._CLEANUP_BACKOFF_SECONDS * (i + 1) for i in range(rt._CLEANUP_ATTEMPTS - 1)
    ]


def test_confirm_server_stopped_succeeds_after_transient_failures(monkeypatch):
    import loop_supervisor.runtime as rt

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    Server = _flaky_stop_server(fail_times=rt._CLEANUP_ATTEMPTS - 1)
    server = Server()
    outcome = rt._confirm_server_stopped(server)

    assert outcome.confirmed is True
    assert outcome.last_error is None
    assert server.stop_calls == rt._CLEANUP_ATTEMPTS


def test_confirm_server_stopped_never_discards_server_handle(monkeypatch):
    """Every retry attempt must call stop() again on the exact same
    server instance -- the handle must never be dropped/replaced between
    attempts."""
    import loop_supervisor.runtime as rt

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    seen_instances: list[int] = []

    class TrackedServer:
        def __init__(self) -> None:
            self.calls = 0

        def stop(self) -> None:
            seen_instances.append(id(self))
            self.calls += 1
            if self.calls < rt._CLEANUP_ATTEMPTS:
                raise RuntimeError("transient")

    server = TrackedServer()
    outcome = rt._confirm_server_stopped(server)
    assert outcome.confirmed is True
    assert len(set(seen_instances)) == 1
    assert seen_instances[0] == id(server)


def test_run_new_startup_transient_stop_failure_then_success_releases_lock(tmp_path, monkeypatch):
    """A startup failure whose defense-in-depth stop() retry fails once
    and then succeeds must still confirm cleanup and release the lock."""
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.opencode import ServerStartupError
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    original_oc_server = rt.OpenCodeServer

    Server = _flaky_stop_server(fail_times=1)

    class FailingStartServer(Server):
        def start(self) -> None:
            raise ServerStartupError("simulated startup failure")

    rt.OpenCodeServer = FailingStartServer
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_:
            pass
    finally:
        rt.OpenCodeServer = original_oc_server

    assert not _lock_path(repo.common_dir()).exists()


def test_run_new_startup_failure_retry_exhaustion_retains_lock(tmp_path, monkeypatch):
    """A startup failure whose defense-in-depth stop() retries are all
    exhausted must retain the lock and mention unresolved cleanup."""
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.opencode import ServerStartupError
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    original_oc_server = rt.OpenCodeServer

    class FailingEverythingServer:
        def __init__(self, *a, **kw) -> None:
            pass

        def start(self) -> None:
            raise ServerStartupError("simulated startup failure")

        def stop(self) -> None:
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


def test_run_new_startup_failure_retry_exhaustion_with_unprintable_cleanup_error(
    tmp_path, monkeypatch
):
    """If every defense-in-depth stop() retry after a startup failure
    raises an exception whose __str__ itself raises, composing the
    retained-lock diagnostic must not itself crash: it must fall back to
    a deterministic "unprintable ..." rendering rather than propagating
    the cleanup exception's own broken __str__ in place of the startup
    failure."""
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.opencode import ServerStartupError
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    original_oc_server = rt.OpenCodeServer

    class _UnprintableCleanupError(RuntimeError):
        def __str__(self) -> str:
            raise RuntimeError("simulated str failure in cleanup error")

    class FailingEverythingServer:
        def __init__(self, *a, **kw) -> None:
            pass

        def start(self) -> None:
            raise ServerStartupError("simulated startup failure")

        def stop(self) -> None:
            raise _UnprintableCleanupError("unprintable")

    rt.OpenCodeServer = FailingEverythingServer
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_ as exc:
            assert "cleanup could not be confirmed" in str(exc)
            assert "unprintable _UnprintableCleanupError" in str(exc)
    finally:
        rt.OpenCodeServer = original_oc_server

    assert _lock_path(repo.common_dir()).exists()


def test_run_new_startup_failure_persistence_error_with_unprintable_str(tmp_path):
    """If record_external_failure() raises a persistence exception whose
    __str__ itself raises, the resulting RuntimeError_ message must still
    be composed successfully (falling back to an "unprintable ..."
    rendering) rather than letting the broken __str__ escape and replace
    the startup failure being reported."""
    from loop_supervisor.opencode import ServerStartupError
    from loop_supervisor.runtime import RuntimeError_

    _init_repo(tmp_path / "repo")

    import loop_supervisor.runtime as rt
    import loop_supervisor.supervisor as sup_module

    original_oc_server = rt.OpenCodeServer
    original_record = sup_module.Supervisor.record_external_failure

    class _UnprintablePersistError(RuntimeError):
        def __str__(self) -> str:
            raise RuntimeError("simulated str failure in persistence error")

    class FailingOCServer:
        def __init__(self, *a, **kw) -> None:
            pass

        def start(self) -> None:
            raise ServerStartupError("simulated startup failure")

        def stop(self) -> None:
            pass

    def _boom_record(self, state, *, exc, phase):
        raise _UnprintablePersistError("cannot persist")

    rt.OpenCodeServer = FailingOCServer
    sup_module.Supervisor.record_external_failure = _boom_record
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_ as exc:
            assert "could not be persisted" in str(exc)
            assert "unprintable _UnprintablePersistError" in str(exc)
    finally:
        rt.OpenCodeServer = original_oc_server
        sup_module.Supervisor.record_external_failure = original_record


def test_run_new_startup_keyboard_interrupt_preserves_identity(tmp_path):
    """A KeyboardInterrupt raised from server.start() must propagate as
    the exact same object, never wrapped in RuntimeError_, and must not
    be persisted as an operational failure."""
    import pytest

    from loop_supervisor.state import load_state

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    original_oc_server = rt.OpenCodeServer
    the_interrupt = KeyboardInterrupt()

    class InterruptingServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"

        def start(self) -> None:
            raise the_interrupt

        def stop(self) -> None:
            pass

        def add_observer(self, obs) -> None:
            pass

    rt.OpenCodeServer = InterruptingServer
    try:
        with pytest.raises(KeyboardInterrupt) as excinfo:
            run_new(tmp_path / "repo", _make_options())
        assert excinfo.value is the_interrupt
    finally:
        rt.OpenCodeServer = original_oc_server

    runs = list_run_ids(tmp_path / "repo")
    assert len(runs) == 1
    state = load_state(repo.common_dir(), runs[0])
    # Startup KeyboardInterrupt must not be recorded as an operational
    # failure: the run remains at its pre-startup phase.
    assert state.phase != "operational_failure"


def _traceback_frames(exc: BaseException):
    tb = exc.__traceback__
    while tb is not None:
        yield tb
        tb = tb.tb_next


def _assert_exact_startup_traceback(
    exc: BaseException, *, entry_func: str, entry_lineno: int, tail_func: str
) -> None:
    """Assert that `exc`'s traceback contains exactly one frame for
    `entry_func` (the run_new()/run_resume() function whose
    `server.start()` call originally raised), at exactly `entry_lineno`
    (the line of that `server.start()` call, not a later line such as a
    `_startup_failure(...)`/`_finalize_interrupted_startup(...)` call
    site), and that its final (deepest) frame is `tail_func` (the fake
    server's `start()`). This distinguishes a true bare re-raise (which
    adds no frame at all) from a redispatch via `raise exc`/a helper call
    (which would either duplicate `entry_func` at a later line or insert
    an extra frame for the helper itself)."""
    frames = [(tb.tb_frame.f_code.co_name, tb.tb_lineno) for tb in _traceback_frames(exc)]
    entry_frames = [(name, lineno) for name, lineno in frames if name == entry_func]
    assert entry_frames == [(entry_func, entry_lineno)], frames
    assert frames[-1][0] == tail_func, frames


def test_run_new_startup_keyboard_interrupt_preserves_exact_traceback(tmp_path):
    """A KeyboardInterrupt raised from server.start() must propagate with
    its original traceback: no frame from _startup_failure,
    _finalize_interrupted_startup, or a redispatching `raise exc` may be
    inserted, and run_new() must appear exactly once, at its original
    `server.start()` call site."""
    import pytest

    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    original_oc_server = rt.OpenCodeServer
    the_interrupt = KeyboardInterrupt()

    class InterruptingServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"

        def start(self) -> None:
            raise the_interrupt

        def stop(self) -> None:
            pass

        def add_observer(self, obs) -> None:
            pass

    rt.OpenCodeServer = InterruptingServer
    try:
        with pytest.raises(KeyboardInterrupt) as excinfo:
            run_new(tmp_path / "repo", _make_options())
        assert excinfo.value is the_interrupt
        frame_names = [tb.tb_frame.f_code.co_name for tb in _traceback_frames(excinfo.value)]
        assert "_startup_failure" not in frame_names, frame_names
        assert "_finalize_interrupted_startup" not in frame_names, frame_names
        _assert_exact_startup_traceback(
            excinfo.value,
            entry_func="run_new",
            entry_lineno=rt._start_server_call_lineno(rt.run_new),
            tail_func="start",
        )
    finally:
        rt.OpenCodeServer = original_oc_server


def test_run_resume_startup_keyboard_interrupt_preserves_exact_traceback(tmp_path):
    """The run_resume() counterpart of the above: the bare `raise` inside
    run_resume()'s own `except` clause must not add a duplicate
    `run_resume` frame at the `_finalize_interrupted_startup(...)`/
    `_startup_failure(...)` call site."""
    import pytest

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
    the_interrupt = KeyboardInterrupt()

    class InterruptingServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"

        def start(self) -> None:
            raise the_interrupt

        def stop(self) -> None:
            pass

        def add_observer(self, obs) -> None:
            pass

    rt.OpenCodeServer = InterruptingServer
    try:
        with pytest.raises(KeyboardInterrupt) as excinfo:
            run_resume(tmp_path / "repo", run_id)
        assert excinfo.value is the_interrupt
        frame_names = [tb.tb_frame.f_code.co_name for tb in _traceback_frames(excinfo.value)]
        assert "_startup_failure" not in frame_names, frame_names
        assert "_finalize_interrupted_startup" not in frame_names, frame_names
        _assert_exact_startup_traceback(
            excinfo.value,
            entry_func="run_resume",
            entry_lineno=rt._start_server_call_lineno(rt.run_resume),
            tail_func="start",
        )
    finally:
        rt.OpenCodeServer = original_oc_server


def test_run_new_startup_system_exit_preserves_identity(tmp_path):
    import pytest

    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    original_oc_server = rt.OpenCodeServer
    the_exit = SystemExit(2)

    class ExitingServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"

        def start(self) -> None:
            raise the_exit

        def stop(self) -> None:
            pass

        def add_observer(self, obs) -> None:
            pass

    rt.OpenCodeServer = ExitingServer
    try:
        with pytest.raises(SystemExit) as excinfo:
            run_new(tmp_path / "repo", _make_options())
        assert excinfo.value is the_exit
    finally:
        rt.OpenCodeServer = original_oc_server


def test_run_new_startup_system_exit_preserves_exact_traceback(tmp_path):
    """SystemExit must be preserved with the same exact-traceback
    guarantee as KeyboardInterrupt: both are direct BaseExceptions
    handled by the same run_new()/_finalize_interrupted_startup() path."""
    import pytest

    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    original_oc_server = rt.OpenCodeServer
    the_exit = SystemExit(2)

    class ExitingServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"

        def start(self) -> None:
            raise the_exit

        def stop(self) -> None:
            pass

        def add_observer(self, obs) -> None:
            pass

    rt.OpenCodeServer = ExitingServer
    try:
        with pytest.raises(SystemExit) as excinfo:
            run_new(tmp_path / "repo", _make_options())
        assert excinfo.value is the_exit
        frame_names = [tb.tb_frame.f_code.co_name for tb in _traceback_frames(excinfo.value)]
        assert "_startup_failure" not in frame_names, frame_names
        assert "_finalize_interrupted_startup" not in frame_names, frame_names
        _assert_exact_startup_traceback(
            excinfo.value,
            entry_func="run_new",
            entry_lineno=rt._start_server_call_lineno(rt.run_new),
            tail_func="start",
        )
    finally:
        rt.OpenCodeServer = original_oc_server


def test_run_new_startup_keyboard_interrupt_with_unresolved_cleanup_has_note(tmp_path):
    """If cleanup retries are exhausted after a startup KeyboardInterrupt,
    the exact interrupt object must still propagate, with a retained-lock
    note attached (not replaced by a RuntimeError_)."""
    import pytest

    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    original_oc_server = rt.OpenCodeServer
    the_interrupt = KeyboardInterrupt()

    class InterruptingServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"

        def start(self) -> None:
            raise the_interrupt

        def stop(self) -> None:
            raise RuntimeError("simulated cleanup failure")

        def add_observer(self, obs) -> None:
            pass

    rt.OpenCodeServer = InterruptingServer
    try:
        with pytest.raises(KeyboardInterrupt) as excinfo:
            run_new(tmp_path / "repo", _make_options())
        assert excinfo.value is the_interrupt
        frame_names = [tb.tb_frame.f_code.co_name for tb in _traceback_frames(excinfo.value)]
        assert "_startup_failure" not in frame_names, frame_names
        assert "_finalize_interrupted_startup" not in frame_names, frame_names
        _assert_exact_startup_traceback(
            excinfo.value,
            entry_func="run_new",
            entry_lineno=rt._start_server_call_lineno(rt.run_new),
            tail_func="start",
        )
    finally:
        rt.OpenCodeServer = original_oc_server

    notes = getattr(the_interrupt, "__notes__", [])
    assert any("cleanup" in n.lower() and "retained" in n.lower() for n in notes), notes
    assert _lock_path(repo.common_dir()).exists()


def test_run_new_runner_assignment_failure_still_cleans_started_server(tmp_path):
    """If the runner-assignment step itself raises, the started server
    must still be cleaned up and the lock lease governed exactly like a
    run failure."""
    from loop_supervisor.supervisor import LoopError, Supervisor

    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    original_oc_server = rt.OpenCodeServer
    call_log: list[str] = []

    class RecordingServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"

        def start(self) -> None:
            call_log.append("server_start")

        def stop(self) -> None:
            call_log.append("server_stop")

        def add_observer(self, obs) -> None:
            pass

    class BoomOnRunnerSupervisor(Supervisor):
        @property
        def runner(self):
            return self._runner

        @runner.setter
        def runner(self, value):
            if getattr(value, "base_url", None) is not None:
                raise LoopError("simulated runner-assignment failure")
            self._runner = value

    rt.OpenCodeServer = RecordingServer
    original_supervisor = rt.Supervisor
    rt.Supervisor = BoomOnRunnerSupervisor
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError:
            pass
    finally:
        rt.OpenCodeServer = original_oc_server
        rt.Supervisor = original_supervisor

    assert "server_start" in call_log
    assert "server_stop" in call_log


def test_run_new_successful_run_transient_cleanup_failure_then_success(tmp_path, monkeypatch):
    """A successful run whose stop() fails transiently and then succeeds
    on retry must confirm cleanup and release the lock without operator
    action."""
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.supervisor import Supervisor

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    original_oc_server = rt.OpenCodeServer
    Server = _flaky_stop_server(fail_times=1)

    original_run = Supervisor.run

    def _fake_run(self, state):
        state.phase = "done"
        return state

    rt.OpenCodeServer = Server
    Supervisor.run = _fake_run
    try:
        run_new(tmp_path / "repo", _make_options())
    finally:
        rt.OpenCodeServer = original_oc_server
        Supervisor.run = original_run

    assert not _lock_path(repo.common_dir()).exists()


def test_run_new_successful_run_cleanup_exhaustion_retains_lock(tmp_path, monkeypatch):
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.runtime import RuntimeError_
    from loop_supervisor.supervisor import Supervisor

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    original_oc_server = rt.OpenCodeServer
    Server = _flaky_stop_server(fail_times=999)

    original_run = Supervisor.run

    def _fake_run(self, state):
        state.phase = "done"
        return state

    rt.OpenCodeServer = Server
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


def test_run_new_failed_run_transient_cleanup_failure_then_success(tmp_path, monkeypatch):
    """A failed supervisor.run() whose stop() retry succeeds after one
    transient failure must release the lock even though the run itself
    failed (the run failure still propagates)."""
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.supervisor import LoopError, Supervisor

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    original_oc_server = rt.OpenCodeServer
    Server = _flaky_stop_server(fail_times=1)

    original_run = Supervisor.run

    def _boom_run(self, state):
        raise LoopError("simulated supervisor failure")

    rt.OpenCodeServer = Server
    Supervisor.run = _boom_run
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError:
            pass
    finally:
        rt.OpenCodeServer = original_oc_server
        Supervisor.run = original_run

    assert not _lock_path(repo.common_dir()).exists()


def test_run_new_failed_run_cleanup_exhaustion_retains_exact_primary_with_notes(
    tmp_path, monkeypatch
):
    """A failed supervisor.run() whose cleanup retries are all exhausted
    must still raise the exact original run exception (not a wrapped
    RuntimeError_), with a retained-lock note attached, and the lock must
    remain on disk."""
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.supervisor import LoopError, Supervisor

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    original_oc_server = rt.OpenCodeServer
    Server = _flaky_stop_server(fail_times=999)

    original_run = Supervisor.run
    the_failure = LoopError("simulated supervisor failure")

    def _boom_run(self, state):
        raise the_failure

    rt.OpenCodeServer = Server
    Supervisor.run = _boom_run
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError as exc:
            assert exc is the_failure
    finally:
        rt.OpenCodeServer = original_oc_server
        Supervisor.run = original_run

    notes = getattr(the_failure, "__notes__", [])
    assert any("cleanup" in n.lower() and "retained" in n.lower() for n in notes), notes
    assert _lock_path(repo.common_dir()).exists()


def test_run_new_run_time_keyboard_interrupt_preserves_identity(tmp_path):
    import pytest

    from loop_supervisor.supervisor import Supervisor

    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    original_oc_server = rt.OpenCodeServer

    class RecordingServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def add_observer(self, obs) -> None:
            pass

    original_run = Supervisor.run
    the_interrupt = KeyboardInterrupt()

    def _boom_run(self, state):
        raise the_interrupt

    rt.OpenCodeServer = RecordingServer
    Supervisor.run = _boom_run
    try:
        with pytest.raises(KeyboardInterrupt) as excinfo:
            run_new(tmp_path / "repo", _make_options())
        assert excinfo.value is the_interrupt
    finally:
        rt.OpenCodeServer = original_oc_server
        Supervisor.run = original_run


def test_run_new_cleanup_time_keyboard_interrupt_does_not_replace_primary(tmp_path):
    """If supervisor.run() raises an ordinary failure AND server.stop()
    itself raises KeyboardInterrupt during cleanup, the original run
    failure must still be what propagates -- the cleanup-time interrupt
    must never replace it."""
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.supervisor import LoopError, Supervisor

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    original_oc_server = rt.OpenCodeServer

    class InterruptingStopServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"

        def start(self) -> None:
            pass

        def stop(self) -> None:
            raise KeyboardInterrupt()

        def add_observer(self, obs) -> None:
            pass

    original_run = Supervisor.run
    the_failure = LoopError("simulated supervisor failure")

    def _boom_run(self, state):
        raise the_failure

    rt.OpenCodeServer = InterruptingStopServer
    Supervisor.run = _boom_run
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError as exc:
            assert exc is the_failure
    finally:
        rt.OpenCodeServer = original_oc_server
        Supervisor.run = original_run

    assert _lock_path(repo.common_dir()).exists()


def test_run_new_persistence_failure_plus_cleanup_failure(tmp_path, monkeypatch):
    """If both record_external_failure() and stop() fail during a
    startup failure, the resulting message must mention both the
    persistence failure and the unresolved cleanup, and the lock must be
    retained."""
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.opencode import ServerStartupError
    from loop_supervisor.runtime import RuntimeError_
    from loop_supervisor.supervisor import Supervisor

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    original_oc_server = rt.OpenCodeServer

    class FailingEverythingServer:
        def __init__(self, *a, **kw) -> None:
            pass

        def start(self) -> None:
            raise ServerStartupError("simulated startup failure")

        def stop(self) -> None:
            raise RuntimeError("simulated cleanup failure")

    original_record_external_failure = Supervisor.record_external_failure

    def _boom_record_external_failure(self, *a, **kw):
        raise RuntimeError("simulated persistence failure")

    rt.OpenCodeServer = FailingEverythingServer
    Supervisor.record_external_failure = _boom_record_external_failure
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_ as exc:
            message = str(exc)
            assert "could not be persisted" in message
            assert "cleanup could not be confirmed" in message
    finally:
        rt.OpenCodeServer = original_oc_server
        Supervisor.record_external_failure = original_record_external_failure

    assert _lock_path(repo.common_dir()).exists()


def test_run_new_lock_release_failure_with_existing_primary_attaches_note(tmp_path, monkeypatch):
    """If the run itself fails (cleanup confirmed OK) and the subsequent
    lock release also fails, the original run exception must still be
    what propagates, with a note describing the lock-release failure."""
    from loop_supervisor.locking import LockError
    from loop_supervisor.supervisor import LoopError, Supervisor

    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    original_oc_server = rt.OpenCodeServer

    class CleanServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def add_observer(self, obs) -> None:
            pass

    original_run = Supervisor.run
    the_failure = LoopError("simulated supervisor failure")

    def _boom_run(self, state):
        raise the_failure

    original_lock_release = rt._LockLease.release

    def _boom_release(self):
        raise LockError("simulated lock-release failure")

    rt.OpenCodeServer = CleanServer
    Supervisor.run = _boom_run
    monkeypatch.setattr(rt._LockLease, "release", _boom_release)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError as exc:
            assert exc is the_failure
    finally:
        rt.OpenCodeServer = original_oc_server
        Supervisor.run = original_run
        rt._LockLease.release = original_lock_release

    notes = getattr(the_failure, "__notes__", [])
    assert any("lock could not be released" in n.lower() for n in notes), notes


def test_run_new_lock_release_failure_with_unprintable_str_attaches_safe_note(
    tmp_path, monkeypatch
):
    """If the lock-release failure itself has a broken/adversarial
    __str__, annotating the primary run failure must not itself crash or
    let that broken __str__ propagate: the note must fall back to a
    deterministic "unprintable ..." rendering."""
    from loop_supervisor.locking import LockError
    from loop_supervisor.supervisor import LoopError, Supervisor

    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    original_oc_server = rt.OpenCodeServer

    class CleanServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def add_observer(self, obs) -> None:
            pass

    original_run = Supervisor.run
    the_failure = LoopError("simulated supervisor failure")

    def _boom_run(self, state):
        raise the_failure

    class _UnprintableLockError(LockError):
        def __str__(self) -> str:
            raise RuntimeError("simulated str failure in lock error")

    original_lock_release = rt._LockLease.release

    def _boom_release(self):
        raise _UnprintableLockError("simulated lock-release failure")

    rt.OpenCodeServer = CleanServer
    Supervisor.run = _boom_run
    monkeypatch.setattr(rt._LockLease, "release", _boom_release)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError as exc:
            assert exc is the_failure
    finally:
        rt.OpenCodeServer = original_oc_server
        Supervisor.run = original_run
        rt._LockLease.release = original_lock_release

    notes = getattr(the_failure, "__notes__", [])
    assert any(
        "lock could not be released" in n.lower() and "unprintable" in n.lower() for n in notes
    ), notes


def test_lock_file_released_only_after_confirmed_cleanup(tmp_path, monkeypatch):
    """The lock file must remain present at every point before stop()
    finally confirms success, and only disappear afterward."""
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.supervisor import Supervisor

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    original_oc_server = rt.OpenCodeServer

    lock_present_during_stop: list[bool] = []

    class ObservingServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"
            self.calls = 0

        def start(self) -> None:
            pass

        def stop(self) -> None:
            self.calls += 1
            lock_present_during_stop.append(_lock_path(repo.common_dir()).exists())
            if self.calls == 1:
                raise RuntimeError("transient")

        def add_observer(self, obs) -> None:
            pass

    original_run = Supervisor.run

    def _fake_run(self, state):
        state.phase = "done"
        return state

    rt.OpenCodeServer = ObservingServer
    Supervisor.run = _fake_run
    try:
        run_new(tmp_path / "repo", _make_options())
    finally:
        rt.OpenCodeServer = original_oc_server
        Supervisor.run = original_run

    assert lock_present_during_stop == [True, True]
    assert not _lock_path(repo.common_dir()).exists()


def test_runtime_module_has_no_cli_import():
    """runtime.py must never import from cli.py, at module level or deferred
    inside a function — cli.py is a presentation module and runtime.py is
    the shared controller both the CLI and the TUI depend on. A deferred
    ``from .cli import ...`` inside run_new()/run_resume() would not be
    caught by a plain module-level import check, so this walks the full
    AST looking for any import (module-level or nested) whose module name
    is "cli" or ".cli"."""
    import loop_supervisor.runtime as rt

    source = Path(rt.__file__).read_text()
    tree = ast.parse(source, filename=rt.__file__)

    offending: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in ("cli", ".cli") or (node.module or "").endswith(".cli"):
                offending.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("cli", "loop_supervisor.cli"):
                    offending.append(f"line {node.lineno}: import {alias.name}")

    assert offending == [], f"runtime.py must not import cli.py, found: {offending}"


def test_run_new_uses_supplied_input_provider(tmp_path):
    """run_new() must pass a caller-supplied input_provider straight through
    to Supervisor rather than always constructing its own
    StdinInputProvider — this is the seam the TUI (and other future
    front-ends) needs, so it must be exercised directly rather than only
    inferred from run_new() no longer importing cli."""
    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    received_providers: list[object] = []
    sentinel_provider = object()

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

    class SpySupervisor:
        def __init__(self, *a, **kw):
            received_providers.append(kw.get("input_provider"))

        def start_new_run(self):
            state = MagicMock()
            state.run_id = "x"
            state.phase = "done"
            state.options = _make_options()
            return state

        def run(self, state):
            return state

        @property
        def runner(self):
            return None

        @runner.setter
        def runner(self, v):
            pass

    rt.GitRepo = FakeGitRepo
    rt.OpenCodeServer = FakeOCServer
    rt.Supervisor = SpySupervisor
    try:
        run_new(tmp_path / "repo", _make_options(), input_provider=sentinel_provider)
    finally:
        rt.GitRepo = original_git_repo
        rt.OpenCodeServer = original_oc_server
        rt.Supervisor = original_supervisor

    assert received_providers == [sentinel_provider]


def test_run_new_defaults_to_stdin_input_provider_when_not_supplied(tmp_path):
    """When no input_provider is passed, run_new() must fall back to a real
    StdinInputProvider (preserving prior behavior for existing callers),
    not None and not some other default."""
    from loop_supervisor.input_providers import StdinInputProvider

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    received_providers: list[object] = []

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

    class SpySupervisor:
        def __init__(self, *a, **kw):
            received_providers.append(kw.get("input_provider"))

        def start_new_run(self):
            state = MagicMock()
            state.run_id = "x"
            state.phase = "done"
            state.options = _make_options()
            return state

        def run(self, state):
            return state

        @property
        def runner(self):
            return None

        @runner.setter
        def runner(self, v):
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

    assert len(received_providers) == 1
    assert isinstance(received_providers[0], StdinInputProvider)


# ===================================================================
# Characterization baseline (pre-RunSession-refactor)
#
# These tests pin the observable cleanup contract of the headless
# run/resume paths: how many times stop() is attempted, the opening
# clause of the retained-lock diagnostic, how many notes are attached,
# and whether the repository lock survives on disk.
#
# Scope, stated precisely: the diagnostic assertions use startswith(),
# so they pin the identifying opening clause and the note count, not the
# full sentence. A change to the trailing operator guidance would not be
# caught here. That is weaker than "exact wording" (as an earlier version
# of this comment claimed) but strictly stronger than what it replaced.
#
# They exist because a previous refactor attempt silently changed three
# of these properties while the whole suite stayed green: the existing
# assertions were substring-based ("cleanup" in note and "retained" in
# note), which happily matched a *different*, wrong sentence. Anything
# here that starts failing during a refactor is a behaviour change to be
# reported, not accommodated.
# ===================================================================


def _characterization_server(
    counter: list[int], *, start_exc: BaseException | None = None, stop_fails: bool = True
):
    """OpenCodeServer stand-in that counts every stop() attempt."""

    class CharServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"

        def start(self) -> None:
            if start_exc is not None:
                raise start_exc

        def stop(self) -> None:
            counter[0] += 1
            if stop_fails:
                raise RuntimeError("simulated stop failure")

        def add_observer(self, obs) -> None:
            pass

    return CharServer


def test_characterize_startup_failure_stop_attempts_are_bounded(tmp_path, monkeypatch):
    """A failed server.start() whose stop() also always fails must attempt
    stop() exactly _CLEANUP_ATTEMPTS times -- not more. The bound is a
    documented contract; a refactor that confirms cleanup in two different
    places would silently double it."""
    from loop_supervisor.opencode import ServerStartupError
    from loop_supervisor.runtime import RuntimeError_

    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    counter = [0]
    original_oc_server = rt.OpenCodeServer
    rt.OpenCodeServer = _characterization_server(
        counter, start_exc=ServerStartupError("simulated startup failure")
    )
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_:
            pass
    finally:
        rt.OpenCodeServer = original_oc_server

    assert counter[0] == rt._CLEANUP_ATTEMPTS


def test_characterize_run_failure_stop_attempts_are_bounded(tmp_path, monkeypatch):
    """Same bound on the run-failure path."""
    from loop_supervisor.supervisor import LoopError, Supervisor

    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    counter = [0]
    original_oc_server = rt.OpenCodeServer
    original_run = Supervisor.run

    def _boom_run(self, state):
        raise LoopError("simulated supervisor failure")

    rt.OpenCodeServer = _characterization_server(counter)
    Supervisor.run = _boom_run
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError:
            pass
    finally:
        rt.OpenCodeServer = original_oc_server
        Supervisor.run = original_run

    assert counter[0] == rt._CLEANUP_ATTEMPTS


def test_characterize_successful_run_stop_attempts_are_bounded(tmp_path, monkeypatch):
    """Same bound on the successful-run path."""
    from loop_supervisor.runtime import RuntimeError_
    from loop_supervisor.supervisor import Supervisor

    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    counter = [0]
    original_oc_server = rt.OpenCodeServer
    original_run = Supervisor.run

    def _ok_run(self, state):
        state.phase = "done"
        return state

    rt.OpenCodeServer = _characterization_server(counter)
    Supervisor.run = _ok_run
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_:
            pass
    finally:
        rt.OpenCodeServer = original_oc_server
        Supervisor.run = original_run

    assert counter[0] == rt._CLEANUP_ATTEMPTS


def test_characterize_run_failure_unresolved_cleanup_note_exact_wording(tmp_path, monkeypatch):
    """A failed run with unresolved cleanup attaches EXACTLY ONE note, and
    that note opens with "the run failed and the ...".

    Asserted on the exact prefix rather than a loose substring: a previous
    refactor replaced this sentence with one claiming the run "completed",
    nested inside a close()-during-__exit__ wrapper, and the substring
    assertions did not notice."""
    from loop_supervisor.supervisor import LoopError, Supervisor

    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    counter = [0]
    original_oc_server = rt.OpenCodeServer
    original_run = Supervisor.run
    the_failure = LoopError("simulated supervisor failure")

    def _boom_run(self, state):
        raise the_failure

    rt.OpenCodeServer = _characterization_server(counter)
    Supervisor.run = _boom_run
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError as exc:
            assert exc is the_failure
    finally:
        rt.OpenCodeServer = original_oc_server
        Supervisor.run = original_run

    notes = getattr(the_failure, "__notes__", [])
    assert len(notes) == 1, notes
    assert notes[0].startswith("the run failed and the OpenCode server cleanup could not be"), (
        notes[0]
    )


def test_characterize_startup_failure_unresolved_cleanup_note_exact_wording(tmp_path, monkeypatch):
    """A failed startup with unresolved cleanup attaches EXACTLY ONE note
    opening with "startup failed and the ...", and the raised RuntimeError_
    message itself names the unresolved cleanup."""
    from loop_supervisor.opencode import ServerStartupError
    from loop_supervisor.runtime import RuntimeError_

    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    counter = [0]
    original_oc_server = rt.OpenCodeServer
    rt.OpenCodeServer = _characterization_server(
        counter, start_exc=ServerStartupError("simulated startup failure")
    )
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_ as exc:
            notes = getattr(exc, "__notes__", [])
            assert len(notes) == 1, notes
            assert notes[0].startswith(
                "startup failed and the OpenCode server cleanup could not be"
            ), notes[0]
            assert str(exc).startswith("failed to start OpenCode server:"), str(exc)
    finally:
        rt.OpenCodeServer = original_oc_server


def test_characterize_successful_run_unresolved_cleanup_message_exact_wording(
    tmp_path, monkeypatch
):
    """A successful run with unresolved cleanup raises RuntimeError_ whose
    MESSAGE opens with "run completed but ..." and attaches NO notes (there
    is no primary exception to annotate)."""
    from loop_supervisor.runtime import RuntimeError_
    from loop_supervisor.supervisor import Supervisor

    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    counter = [0]
    original_oc_server = rt.OpenCodeServer
    original_run = Supervisor.run

    def _ok_run(self, state):
        state.phase = "done"
        return state

    rt.OpenCodeServer = _characterization_server(counter)
    Supervisor.run = _ok_run
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_ as exc:
            assert str(exc).startswith(
                "run completed but OpenCode server cleanup could not be confirmed"
            ), str(exc)
            assert getattr(exc, "__notes__", []) == []
    finally:
        rt.OpenCodeServer = original_oc_server
        Supervisor.run = original_run


def test_characterize_startup_keyboard_interrupt_successful_cleanup_releases_lock(tmp_path):
    """THE REGRESSION GUARD.

    A KeyboardInterrupt from server.start() whose cleanup SUCCEEDS must
    release the repository lock, attempt stop() exactly once, and attach no
    notes. No prior test asserted lock state on this path, which is how a
    refactor that skipped release() on the interrupt path shipped green."""
    import pytest

    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    counter = [0]
    the_interrupt = KeyboardInterrupt()
    original_oc_server = rt.OpenCodeServer
    rt.OpenCodeServer = _characterization_server(counter, start_exc=the_interrupt, stop_fails=False)
    try:
        with pytest.raises(KeyboardInterrupt) as excinfo:
            run_new(tmp_path / "repo", _make_options())
        assert excinfo.value is the_interrupt
    finally:
        rt.OpenCodeServer = original_oc_server

    assert counter[0] == 1
    assert getattr(the_interrupt, "__notes__", []) == []
    assert not _lock_path(repo.common_dir()).exists()


def test_characterize_startup_system_exit_successful_cleanup_releases_lock(tmp_path):
    """SystemExit counterpart of the regression guard above."""
    import pytest

    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    counter = [0]
    the_exit = SystemExit(2)
    original_oc_server = rt.OpenCodeServer
    rt.OpenCodeServer = _characterization_server(counter, start_exc=the_exit, stop_fails=False)
    try:
        with pytest.raises(SystemExit) as excinfo:
            run_new(tmp_path / "repo", _make_options())
        assert excinfo.value is the_exit
    finally:
        rt.OpenCodeServer = original_oc_server

    assert counter[0] == 1
    assert getattr(the_exit, "__notes__", []) == []
    assert not _lock_path(repo.common_dir()).exists()


def test_characterize_startup_interrupt_unresolved_cleanup_retains_lock_with_one_note(tmp_path):
    """The failed-cleanup counterpart: lock RETAINED, exactly one note
    opening with "startup was interrupted; the ...", interrupt identity
    preserved."""
    import pytest

    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    counter = [0]
    the_interrupt = KeyboardInterrupt()
    original_oc_server = rt.OpenCodeServer
    rt.OpenCodeServer = _characterization_server(counter, start_exc=the_interrupt, stop_fails=True)
    try:
        with pytest.raises(KeyboardInterrupt) as excinfo:
            run_new(tmp_path / "repo", _make_options())
        assert excinfo.value is the_interrupt
    finally:
        rt.OpenCodeServer = original_oc_server

    assert counter[0] == rt._CLEANUP_ATTEMPTS
    notes = getattr(the_interrupt, "__notes__", [])
    assert len(notes) == 1, notes
    assert notes[0].startswith(
        "startup was interrupted; the OpenCode server cleanup could not be"
    ), notes[0]
    assert _lock_path(repo.common_dir()).exists()


def test_characterize_runner_handoff_failure_is_raw_and_not_persisted(tmp_path):
    """The runner handoff (supervisor.runner = server) is a DISTINCT stage
    from startup: its failure propagates raw (not wrapped in RuntimeError_),
    is NOT persisted as an operational failure, and the lock is released.

    Pinned because the handoff is scheduled to move into start_server();
    placing it inside the try that routes to _startup_failure would wrap
    and persist it, silently changing all three properties."""
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.state import load_state
    from loop_supervisor.supervisor import LoopError, Supervisor

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    counter = [0]
    original_oc_server = rt.OpenCodeServer
    original_supervisor = rt.Supervisor

    class BoomOnRunnerSupervisor(Supervisor):
        @property
        def runner(self):
            return self._runner

        @runner.setter
        def runner(self, value):
            if getattr(value, "base_url", None) is not None:
                raise LoopError("simulated runner-assignment failure")
            self._runner = value

    rt.OpenCodeServer = _characterization_server(counter, stop_fails=False)
    rt.Supervisor = BoomOnRunnerSupervisor
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError as exc:
            assert not isinstance(exc, rt.RuntimeError_)
            assert "simulated runner-assignment failure" in str(exc)
    finally:
        rt.OpenCodeServer = original_oc_server
        rt.Supervisor = original_supervisor

    runs = list_run_ids(tmp_path / "repo")
    assert len(runs) == 1
    state = load_state(repo.common_dir(), runs[0])
    assert state.phase != "operational_failure"
    assert not _lock_path(repo.common_dir()).exists()


# ===================================================================
# RunSession: direct lifecycle tests
# ===================================================================


@contextlib.contextmanager
def _patched_session_env(repo, *, server_cls=None, supervisor_cls=None, call_log=None):
    """Patch GitRepo/OpenCodeServer/Supervisor for the duration of a block.

    Unlike a factory helper, this keeps the patches active while the
    session is entered and used, which is what RunSession requires.
    """
    import loop_supervisor.runtime as rt

    if call_log is None:
        call_log = []

    class FakeGitRepo:
        def __init__(self, *a, **kw):
            self.root = repo.root

        def common_dir(self):
            return repo.common_dir()

    class DefaultServer:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            call_log.append("server_start")

        def stop(self):
            call_log.append("server_stop")

        def add_observer(self, obs):
            call_log.append("add_observer")

        def abort_active_sessions(self):
            call_log.append("abort_active_sessions")

    class DefaultSupervisor:
        def __init__(self, *a, **kw):
            self._runner = None

        def start_new_run(self):
            state = MagicMock()
            state.run_id = "fake-run"
            state.phase = "planning"
            state.options = _make_options()
            call_log.append("start_new_run")
            return state

        def resume(self, state):
            call_log.append("resume")
            return state

        def advance(self, state):
            call_log.append("advance")
            from loop_supervisor.supervisor import AdvanceOutcome, AdvanceStatus

            state.phase = "done"
            return AdvanceOutcome(
                status=AdvanceStatus.TERMINAL,
                state=state,
                phase_before="planning",
                phase_after="done",
            )

        def run(self, state):
            call_log.append("run")
            state.phase = "done"
            return state

        @property
        def runner(self):
            return self._runner

        @runner.setter
        def runner(self, value):
            call_log.append("runner_set")
            self._runner = value

    originals = (rt.GitRepo, rt.OpenCodeServer, rt.Supervisor)
    rt.GitRepo = FakeGitRepo
    rt.OpenCodeServer = server_cls or DefaultServer
    rt.Supervisor = supervisor_cls or DefaultSupervisor
    try:
        yield call_log
    finally:
        rt.GitRepo, rt.OpenCodeServer, rt.Supervisor = originals


# --- state machine: happy path ---------------------------------------


def test_run_session_factory_is_inert(tmp_path):
    """The factory must acquire nothing: no lock on disk, no server."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")
    session = rt.new_run_session(repo.root, _make_options())

    assert session.state is rt.SessionState.NEW
    assert session.base_url is None
    assert session.run_state is None
    assert not _lock_path(repo.common_dir()).exists()


def test_resume_run_session_factory_is_inert(tmp_path):
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")
    session = rt.resume_run_session(repo.root, "some-run-id")

    assert session.state is rt.SessionState.NEW
    assert not _lock_path(repo.common_dir()).exists()


def test_run_session_state_progression(tmp_path):
    """NEW -> READY -> STARTED -> CLOSED across a normal lifecycle."""
    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo):
        session = rt.new_run_session(repo.root, _make_options())
        assert session.state is rt.SessionState.NEW
        with session:
            assert session.state is rt.SessionState.READY
            session.start_server()
            assert session.state is rt.SessionState.STARTED
            session.run_to_completion()
            assert session.state is rt.SessionState.STARTED
    assert session.state is rt.SessionState.CLOSED


def test_run_session_releases_lock_on_success(tmp_path):
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            session.run_to_completion()
    assert not _lock_path(repo.common_dir()).exists()


def test_run_session_base_url_exposed_after_start(tmp_path):
    """base_url mirrors the server's own attribute, which a real
    OpenCodeServer only populates once started."""
    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")

    class RealisticServer:
        def __init__(self, *a, **kw):
            self.base_url = None

        def start(self):
            self.base_url = "http://127.0.0.1:9999"

        def stop(self):
            self.base_url = None

        def add_observer(self, obs):
            pass

    with _patched_session_env(repo, server_cls=RealisticServer):
        session = rt.new_run_session(repo.root, _make_options())
        assert session.base_url is None
        with session:
            assert session.base_url is None
            session.start_server()
            assert session.base_url == "http://127.0.0.1:9999"


def test_run_session_stops_server_before_releasing_lock(tmp_path):
    """Ordering invariant: the lock must still exist while stop() runs."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")
    lock_present_during_stop: list[bool] = []

    class ObservingServer:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            pass

        def stop(self):
            lock_present_during_stop.append(_lock_path(repo.common_dir()).exists())

        def add_observer(self, obs):
            pass

    with _patched_session_env(repo, server_cls=ObservingServer):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            session.run_to_completion()

    assert lock_present_during_stop == [True]
    assert not _lock_path(repo.common_dir()).exists()


# --- runner handoff ---------------------------------------------------


def test_run_session_start_server_installs_runner(tmp_path):
    """start_server() must hand the server to the supervisor, otherwise
    advance() would dispatch against the _UnstartedRunner placeholder."""
    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo) as call_log:
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            assert session._supervisor.runner is session._server
    assert "runner_set" in call_log
    assert call_log.index("server_start") < call_log.index("runner_set")


def test_run_session_advance_dispatches_against_started_server(tmp_path):
    """advance() must dispatch against the STARTED SERVER, not the
    _UnstartedRunner placeholder.

    Uses a supervisor that actually consults its runner (as the real one
    does via runner.run_agent), so that failing to install the runner in
    start_server() surfaces here rather than being masked by a fake that
    ignores the runner entirely."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.supervisor import AdvanceOutcome, AdvanceStatus

    repo = _init_repo(tmp_path / "repo")
    dispatched_against: list[str] = []

    class RunnerSensitiveSupervisor:
        def __init__(self, *a, **kw):
            self._runner = None

        def start_new_run(self):
            state = MagicMock()
            state.run_id = "r"
            state.phase = "planning"
            state.options = _make_options()
            return state

        def advance(self, state):
            # Mirror the real supervisor: the phase handler invokes the
            # runner, so an _UnstartedRunner raises LoopError here.
            self._runner.run_agent(agent="loop-planner")
            dispatched_against.append(type(self._runner).__name__)
            state.phase = "done"
            return AdvanceOutcome(
                status=AdvanceStatus.TERMINAL,
                state=state,
                phase_before="planning",
                phase_after="done",
            )

        @property
        def runner(self):
            return self._runner

        @runner.setter
        def runner(self, v):
            self._runner = v

    class RunnableServer:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            pass

        def stop(self):
            pass

        def add_observer(self, obs):
            pass

        def run_agent(self, **kwargs):
            return "ok"

    with _patched_session_env(
        repo, server_cls=RunnableServer, supervisor_cls=RunnerSensitiveSupervisor
    ):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            outcome = session.advance()

    assert outcome.status is AdvanceStatus.TERMINAL
    assert dispatched_against == ["RunnableServer"]
    assert session.state is rt.SessionState.CLOSED


def test_run_session_advance_updates_run_state(tmp_path):
    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            outcome = session.advance()
            assert session.run_state is outcome.state


def test_run_session_advance_returns_to_started_on_failure(tmp_path):
    """A raising advance() must leave the session usable, not stuck in
    ADVANCING."""
    import pytest

    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")

    class BoomSupervisor:
        def __init__(self, *a, **kw):
            self._runner = None

        def start_new_run(self):
            state = MagicMock()
            state.run_id = "r"
            state.phase = "planning"
            state.options = _make_options()
            return state

        def advance(self, state):
            raise RuntimeError("advance blew up")

        @property
        def runner(self):
            return self._runner

        @runner.setter
        def runner(self, v):
            self._runner = v

    with _patched_session_env(repo, supervisor_cls=BoomSupervisor):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            with pytest.raises(RuntimeError, match="advance blew up"):
                session.advance()
            assert session.state is rt.SessionState.STARTED


# --- state machine: misuse rejection ----------------------------------


def test_run_session_cannot_be_entered_twice(tmp_path):
    import pytest

    import loop_supervisor.runtime as rt
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            with pytest.raises(RuntimeError_, match="only be entered once"):
                session.__enter__()


def test_run_session_start_server_rejected_before_enter(tmp_path):
    import pytest

    import loop_supervisor.runtime as rt
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    session = rt.new_run_session(repo.root, _make_options())
    with pytest.raises(RuntimeError_, match="expected 'ready'"):
        session.start_server()


def test_run_session_start_server_rejected_twice(tmp_path):
    import pytest

    import loop_supervisor.runtime as rt
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            with pytest.raises(RuntimeError_, match="expected 'ready'"):
                session.start_server()


def test_run_session_advance_rejected_before_start_server(tmp_path):
    import pytest

    import loop_supervisor.runtime as rt
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            with pytest.raises(RuntimeError_, match="expected 'started'"):
                session.advance()


def test_run_session_run_to_completion_rejected_before_start_server(tmp_path):
    import pytest

    import loop_supervisor.runtime as rt
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            with pytest.raises(RuntimeError_, match="expected 'started'"):
                session.run_to_completion()


def test_run_session_advance_rejected_after_close(tmp_path):
    import pytest

    import loop_supervisor.runtime as rt
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
        with pytest.raises(RuntimeError_, match="expected 'started'"):
            session.advance()


def test_run_session_close_is_idempotent(tmp_path):
    """close() after close() must be a harmless no-op, so an explicit
    close() inside a with-block does not break __exit__."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo) as call_log:
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            session.close()
            assert session.state is rt.SessionState.CLOSED

    assert call_log.count("server_stop") == 1
    assert not _lock_path(repo.common_dir()).exists()


# --- __enter__ failure points -----------------------------------------


def test_run_session_enter_fails_on_unopenable_repo(tmp_path):
    import pytest

    import loop_supervisor.runtime as rt
    from loop_supervisor.runtime import RuntimeError_

    session = rt.new_run_session(tmp_path / "not-a-repo", _make_options())
    with pytest.raises(RuntimeError_, match="cannot open repository"):
        session.__enter__()
    assert session.state is rt.SessionState.FAILED


def test_run_session_enter_validates_run_id_before_locking(tmp_path):
    """A crafted resume ID must be rejected with no lock left behind."""
    import pytest

    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    session = rt.resume_run_session(repo.root, "../../evil")
    with pytest.raises(RuntimeError_):
        session.__enter__()

    assert session.state is rt.SessionState.FAILED
    assert not _lock_path(repo.common_dir()).exists()


def test_run_session_enter_releases_lock_when_state_creation_fails(tmp_path):
    """__exit__ is never called when __enter__ raises, so __enter__ must
    release the lock itself on any post-lock failure."""
    import pytest

    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")

    class BoomSupervisor:
        def __init__(self, *a, **kw):
            pass

        def start_new_run(self):
            raise RuntimeError("state creation failed")

    with _patched_session_env(repo, supervisor_cls=BoomSupervisor):
        session = rt.new_run_session(repo.root, _make_options())
        with pytest.raises(RuntimeError, match="state creation failed"):
            session.__enter__()

    assert session.state is rt.SessionState.FAILED
    assert not _lock_path(repo.common_dir()).exists()


def test_run_session_enter_lock_release_failure_attaches_note(tmp_path):
    """If the post-lock cleanup release() also fails, the original failure
    must still propagate, with the release failure recorded as a note."""
    import pytest

    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import LockError

    repo = _init_repo(tmp_path / "repo")

    class BoomSupervisor:
        def __init__(self, *a, **kw):
            pass

        def start_new_run(self):
            raise RuntimeError("state creation failed")

    original_release = rt._LockLease.release

    def _boom_release(self):
        raise LockError("release failed")

    with _patched_session_env(repo, supervisor_cls=BoomSupervisor):
        rt._LockLease.release = _boom_release
        try:
            session = rt.new_run_session(repo.root, _make_options())
            with pytest.raises(RuntimeError, match="state creation failed") as excinfo:
                session.__enter__()
        finally:
            rt._LockLease.release = original_release

    notes = getattr(excinfo.value, "__notes__", [])
    assert any("lock could not be released" in n.lower() for n in notes), notes


def test_run_session_second_session_blocked_while_first_holds_lock(tmp_path):
    """The lock is real: a second session cannot enter concurrently."""
    import pytest

    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import LockError

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo):
        first = rt.new_run_session(repo.root, _make_options())
        with first:
            second = rt.new_run_session(repo.root, _make_options())
            with pytest.raises(LockError):
                second.__enter__()
            assert second.state is rt.SessionState.FAILED


# --- cleanup ownership and retry --------------------------------------


def test_run_session_cleanup_unresolved_is_retryable(tmp_path, monkeypatch):
    """Unresolved cleanup must leave the session retryable with the lock
    retained; a later successful close() releases it."""
    import pytest

    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    stop_fails = [True]

    class TogglableServer:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            pass

        def stop(self):
            if stop_fails[0]:
                raise RuntimeError("stop fail")

        def add_observer(self, obs):
            pass

    with _patched_session_env(repo, server_cls=TogglableServer):
        session = rt.new_run_session(repo.root, _make_options())
        with pytest.raises(RuntimeError_, match="run completed but"):
            with session:
                session.start_server()
                session.run_to_completion()

        assert session.state is rt.SessionState.CLEANUP_UNRESOLVED
        assert _lock_path(repo.common_dir()).exists()

        stop_fails[0] = False
        session.close()

    assert session.state is rt.SessionState.CLOSED
    assert not _lock_path(repo.common_dir()).exists()


def test_run_session_cleanup_unresolved_stays_unresolved_on_repeated_failure(tmp_path, monkeypatch):
    """Retrying close() while stop() keeps failing must keep the lock and
    stay retryable, never silently transition to CLOSED."""
    import pytest

    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)

    class AlwaysFailStop:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            pass

        def stop(self):
            raise RuntimeError("always fails")

        def add_observer(self, obs):
            pass

    with _patched_session_env(repo, server_cls=AlwaysFailStop):
        session = rt.new_run_session(repo.root, _make_options())
        with pytest.raises(RuntimeError_):
            with session:
                session.start_server()
                session.run_to_completion()

        for _ in range(2):
            with pytest.raises(RuntimeError_):
                session.close()
            assert session.state is rt.SessionState.CLEANUP_UNRESOLVED
            assert _lock_path(repo.common_dir()).exists()
            # The lease itself must stay unreleasable: an OpenCode process
            # may still be alive, and ADR 0009 forbids releasing the lock
            # before cleanup is confirmed. Asserting only the lock file is
            # not enough -- it is written at acquire time, so a wrongly
            # released lease is invisible until something calls release().
            assert not session._lease.releasable


def test_run_session_close_without_start_releases_lock_without_stopping(tmp_path):
    """A session entered but never started owns no process, so close() must
    release the lock without calling stop()."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo) as call_log:
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            pass

    assert "server_stop" not in call_log
    assert session.state is rt.SessionState.CLOSED
    assert not _lock_path(repo.common_dir()).exists()


def test_run_session_body_exception_is_not_replaced_by_cleanup_failure(tmp_path, monkeypatch):
    """A cleanup failure must never replace a body exception, and must not
    add an __exit__ frame to it."""
    import pytest

    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)

    class FailStopServer:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            pass

        def stop(self):
            raise RuntimeError("stop fail")

        def add_observer(self, obs):
            pass

    the_error = ValueError("body blew up")
    with _patched_session_env(repo, server_cls=FailStopServer):
        session = rt.new_run_session(repo.root, _make_options())
        with pytest.raises(ValueError) as excinfo:
            with session:
                session.start_server()
                raise the_error

    assert excinfo.value is the_error
    frame_names = [tb.tb_frame.f_code.co_name for tb in _traceback_frames(excinfo.value)]
    assert "__exit__" not in frame_names, frame_names
    assert "close" not in frame_names, frame_names


def test_run_session_close_raising_does_not_replace_body_exception(tmp_path):
    """__exit__ must swallow a raising close() when a body exception is
    already propagating.

    Raising out of __exit__ would both replace the primary and stamp an
    __exit__ frame onto it. Here the lock release fails, which is the
    realistic way close() raises on an otherwise-clean shutdown."""
    import pytest

    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import LockError

    repo = _init_repo(tmp_path / "repo")
    the_error = ValueError("body blew up")

    original_release = rt._LockLease.release

    def _boom_release(self):
        raise LockError("release failed")

    with _patched_session_env(repo):
        rt._LockLease.release = _boom_release
        try:
            session = rt.new_run_session(repo.root, _make_options())
            with pytest.raises(ValueError) as excinfo:
                with session:
                    session.start_server()
                    raise the_error
        finally:
            rt._LockLease.release = original_release

    assert excinfo.value is the_error
    frame_names = [tb.tb_frame.f_code.co_name for tb in _traceback_frames(excinfo.value)]
    assert "__exit__" not in frame_names, frame_names
    notes = getattr(the_error, "__notes__", [])
    assert any("lock could not be released" in n.lower() for n in notes), notes


# --- observers and invocation control ---------------------------------


def test_run_session_server_observer_installed_before_start(tmp_path):
    """A server_observer passed to the factory must be attached before
    start(), so no early invocation event is missed."""
    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")
    sentinel = object()
    events: list[str] = []

    class SpyServer:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def add_observer(self, obs):
            events.append(f"add_observer:{obs is sentinel}")

        def start(self):
            events.append("start")

        def stop(self):
            pass

    with _patched_session_env(repo, server_cls=SpyServer):
        session = rt.new_run_session(repo.root, _make_options(), server_observer=sentinel)
        with session:
            session.start_server()

    assert events == ["add_observer:True", "start"]


def test_run_session_add_observer_rejected_before_enter(tmp_path):
    import pytest

    import loop_supervisor.runtime as rt
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    session = rt.new_run_session(repo.root, _make_options())
    with pytest.raises(RuntimeError_, match="entered session"):
        session.add_observer(object())


def test_run_session_abort_active_invocations_is_safe_before_enter(tmp_path):
    """Must be a no-op rather than raising, so a UI can call it blindly."""
    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")
    session = rt.new_run_session(repo.root, _make_options())
    session.abort_active_invocations()


def test_run_session_abort_active_invocations_delegates_to_server(tmp_path):
    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo) as call_log:
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            session.abort_active_invocations()

    assert "abort_active_sessions" in call_log


# --- input provider plumbing ------------------------------------------


def test_run_session_passes_supplied_input_provider(tmp_path):
    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")
    sentinel = object()
    received: list[object] = []

    class SpySupervisor:
        def __init__(self, *a, **kw):
            received.append(kw.get("input_provider"))
            self._runner = None

        def start_new_run(self):
            state = MagicMock()
            state.run_id = "r"
            state.phase = "planning"
            state.options = _make_options()
            return state

        @property
        def runner(self):
            return self._runner

        @runner.setter
        def runner(self, v):
            self._runner = v

    with _patched_session_env(repo, supervisor_cls=SpySupervisor):
        session = rt.new_run_session(repo.root, _make_options(), input_provider=sentinel)
        with session:
            pass

    assert received == [sentinel]


def test_run_session_defaults_to_stdin_input_provider(tmp_path):
    import loop_supervisor.runtime as rt
    from loop_supervisor.input_providers import StdinInputProvider

    repo = _init_repo(tmp_path / "repo")
    received: list[object] = []

    class SpySupervisor:
        def __init__(self, *a, **kw):
            received.append(kw.get("input_provider"))
            self._runner = None

        def start_new_run(self):
            state = MagicMock()
            state.run_id = "r"
            state.phase = "planning"
            state.options = _make_options()
            return state

        @property
        def runner(self):
            return self._runner

        @runner.setter
        def runner(self, v):
            self._runner = v

    with _patched_session_env(repo, supervisor_cls=SpySupervisor):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            pass

    assert len(received) == 1
    assert isinstance(received[0], StdinInputProvider)


# --- traceback-line helper --------------------------------------------


def test_start_server_call_lineno_locates_the_call(tmp_path):
    """The helper must find the real call line, and must return -1 (not a
    plausible-looking number) when it cannot, so a stale traceback
    assertion fails loudly instead of passing vacuously."""
    import inspect

    import loop_supervisor.runtime as rt

    for func in (rt.run_new, rt.run_resume):
        lineno = rt._start_server_call_lineno(func)
        src_lines, start = inspect.getsourcelines(func)
        assert lineno > 0
        assert "session.start_server()" in src_lines[lineno - start]

    def unrelated():
        return 1

    assert rt._start_server_call_lineno(unrelated) == -1


# ===================================================================
# Phase 2b: retry-path characterization
#
# RunSession advertises close() as retryable, but no test exercised a
# FAILED explicit retry. Two defects hid in that gap (both reproduced
# against cfe1aca):
#
#   1. close() returns None -- indistinguishable from success -- when a
#      retry's stop() still fails, provided some earlier failure already
#      attached a retained-lock note.
#   2. A failed lock release marks the session CLOSED, so every later
#      close() no-ops while the lock is still on disk. This contradicts
#      SupervisorLock.release(), which keeps its ownership token
#      specifically so callers can retry (locking.py:426-437).
#
# Both defects were introduced as strict xfails in the preceding commit
# and are fixed here, so these now assert the corrected contract:
#
#   * every close() that ends with the lock still held reports it, by
#     raising or (when a primary is unwinding) by annotating that
#     primary -- a retry is never silently successful;
#   * a confirmed stop() followed by a failed release lands in the
#     non-terminal RELEASE_PENDING state, retryable without re-spending
#     the stop() budget.
# ===================================================================


def _retry_server(*, start_exc=None, stop_fails=True, stop_counter=None):
    """Server stand-in whose stop() failure mode is fixed for its lifetime."""

    class RetryServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"

        def start(self) -> None:
            if start_exc is not None:
                raise start_exc

        def stop(self) -> None:
            if stop_counter is not None:
                stop_counter[0] += 1
            if stop_fails:
                raise RuntimeError("simulated stop failure")

        def add_observer(self, obs) -> None:
            pass

    return RetryServer


# --- Defect 1: a failed retry must not look like success --------------


def test_retry_close_after_startup_failure_still_failing_must_raise(tmp_path, monkeypatch):
    """An explicit close() retry whose stop() still fails must report that.

    Returning None tells a shutdown loop the lock was released when it is
    still held and an OpenCode process may still be alive."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.opencode import ServerStartupError
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)

    server_cls = _retry_server(start_exc=ServerStartupError("startup fail"))
    with _patched_session_env(repo, server_cls=server_cls):
        session = rt.new_run_session(repo.root, _make_options())
        with pytest.raises(RuntimeError_):
            with session:
                session.start_server()

        assert session.state is rt.SessionState.CLEANUP_UNRESOLVED
        assert _lock_path(repo.common_dir()).exists()

        with pytest.raises(RuntimeError_):
            session.close()


def test_retry_close_after_body_failure_still_failing_must_raise(tmp_path, monkeypatch):
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)

    with _patched_session_env(repo, server_cls=_retry_server()):
        session = rt.new_run_session(repo.root, _make_options())
        with pytest.raises(ValueError):
            with session:
                session.start_server()
                raise ValueError("body blew up")

        assert session.state is rt.SessionState.CLEANUP_UNRESOLVED
        assert _lock_path(repo.common_dir()).exists()

        with pytest.raises(RuntimeError_):
            session.close()


def test_retry_close_after_startup_interrupt_still_failing_must_raise(tmp_path, monkeypatch):
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)

    server_cls = _retry_server(start_exc=KeyboardInterrupt())
    with _patched_session_env(repo, server_cls=server_cls):
        session = rt.new_run_session(repo.root, _make_options())
        with pytest.raises(KeyboardInterrupt):
            with session:
                session.start_server()

        assert session.state is rt.SessionState.CLEANUP_UNRESOLVED
        assert _lock_path(repo.common_dir()).exists()

        with pytest.raises(RuntimeError_):
            session.close()


def test_retry_close_after_startup_failure_succeeding_releases_lock(tmp_path, monkeypatch):
    """The positive counterpart: once stop() succeeds, the retry must
    actually release the lock and reach CLOSED. Already correct today."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.opencode import ServerStartupError
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    stop_fails = [True]

    class TogglableServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"

        def start(self) -> None:
            raise ServerStartupError("startup fail")

        def stop(self) -> None:
            if stop_fails[0]:
                raise RuntimeError("stop fail")

        def add_observer(self, obs) -> None:
            pass

    with _patched_session_env(repo, server_cls=TogglableServer):
        session = rt.new_run_session(repo.root, _make_options())
        with pytest.raises(RuntimeError_):
            with session:
                session.start_server()

        assert session.state is rt.SessionState.CLEANUP_UNRESOLVED
        stop_fails[0] = False
        session.close()

    assert session.state is rt.SessionState.CLOSED
    assert not _lock_path(repo.common_dir()).exists()


# --- Defect 2: a failed lock release must stay retryable --------------


def test_retry_close_after_release_failure_releases_lock(tmp_path):
    """A transient LockError from release() must not strand the lock.

    SupervisorLock.release() deliberately retains its ownership token on
    transient failure so the caller can retry; RunSession must not throw
    that away by making itself terminal. The session must land in the
    non-terminal RELEASE_PENDING state, and the failure must surface as
    LockError (the type the parent raised on this path)."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import LockError, _lock_path

    repo = _init_repo(tmp_path / "repo")
    original_release = rt._LockLease.release
    fail_once = [True]

    def flaky_release(self):
        if fail_once[0]:
            fail_once[0] = False
            raise LockError("transient release failure")
        original_release(self)

    with _patched_session_env(repo):
        rt._LockLease.release = flaky_release
        try:
            session = rt.new_run_session(repo.root, _make_options())
            session.__enter__()
            session.start_server()

            with pytest.raises(LockError):
                session.close()
            assert session.state is rt.SessionState.RELEASE_PENDING
            assert _lock_path(repo.common_dir()).exists()

            # release() would now succeed; the session must let it.
            session.close()
        finally:
            rt._LockLease.release = original_release

    assert session.state is rt.SessionState.CLOSED
    assert not _lock_path(repo.common_dir()).exists()


def test_retry_close_after_enter_release_failure_releases_lock(tmp_path):
    """A post-lock __enter__ failure whose release also fails must leave the
    retained lock retryable rather than terminal."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import LockError, _lock_path

    repo = _init_repo(tmp_path / "repo")

    class BoomSupervisor:
        def __init__(self, *a, **kw):
            pass

        def start_new_run(self):
            raise RuntimeError("state creation failed")

    original_release = rt._LockLease.release
    fail_once = [True]

    def flaky_release(self):
        if fail_once[0]:
            fail_once[0] = False
            raise LockError("transient release failure")
        original_release(self)

    with _patched_session_env(repo, supervisor_cls=BoomSupervisor):
        rt._LockLease.release = flaky_release
        try:
            session = rt.new_run_session(repo.root, _make_options())
            with pytest.raises(RuntimeError, match="state creation failed"):
                session.__enter__()
            assert _lock_path(repo.common_dir()).exists()

            session.close()
        finally:
            rt._LockLease.release = original_release

    assert not _lock_path(repo.common_dir()).exists()


def test_retry_release_does_not_respend_stop_budget(tmp_path):
    """Retrying a failed lock release must not re-run the already-confirmed
    server.stop(). Already correct today; pinned because the fix for
    Defect 2 introduces a new state on exactly this path."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import LockError

    repo = _init_repo(tmp_path / "repo")
    stop_calls = [0]

    class CountingServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"

        def start(self) -> None:
            pass

        def stop(self) -> None:
            stop_calls[0] += 1

        def add_observer(self, obs) -> None:
            pass

    original_release = rt._LockLease.release
    fail_once = [True]

    def flaky_release(self):
        if fail_once[0]:
            fail_once[0] = False
            raise LockError("transient release failure")
        original_release(self)

    with _patched_session_env(repo, server_cls=CountingServer):
        rt._LockLease.release = flaky_release
        try:
            session = rt.new_run_session(repo.root, _make_options())
            session.__enter__()
            session.start_server()
            with pytest.raises(LockError):
                session.close()
            assert session.state is rt.SessionState.RELEASE_PENDING
            after_first = stop_calls[0]
            session.close()
        finally:
            rt._LockLease.release = original_release

    assert after_first == 1, stop_calls
    assert stop_calls[0] == 1, stop_calls
    assert session.state is rt.SessionState.CLOSED


# --- previously unasserted correct behaviour --------------------------


def test_startup_interrupt_traceback_excludes_exit_and_close_frames(tmp_path, monkeypatch):
    """The startup-interrupt traceback must contain no __exit__/close frame
    even when close() does substantial cleanup work.

    Behaviour is already correct; nothing asserted it on the interrupt
    path, which is where the Phase 2 predecessor defect lived."""
    import loop_supervisor.runtime as rt

    _init_repo(tmp_path / "repo")
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)

    the_interrupt = KeyboardInterrupt()
    original_oc_server = rt.OpenCodeServer
    # stop_fails=True so close() runs the full bounded retry before returning.
    rt.OpenCodeServer = _retry_server(start_exc=the_interrupt, stop_fails=True)
    try:
        with pytest.raises(KeyboardInterrupt) as excinfo:
            run_new(tmp_path / "repo", _make_options())
        assert excinfo.value is the_interrupt
        frames = [tb.tb_frame.f_code.co_name for tb in _traceback_frames(excinfo.value)]
        assert "__exit__" not in frames, frames
        assert "close" not in frames, frames
        assert "_confirm_cleanup" not in frames, frames
        assert frames.count("start_server") == 1, frames
        assert frames[-1] == "start", frames
    finally:
        rt.OpenCodeServer = original_oc_server


def test_resume_run_session_full_lifecycle(tmp_path):
    """A valid resume driven through the RunSession API directly.

    The Phase 2 suite covered resume only for inert construction and
    invalid-ID rejection, leaving state loading, Supervisor.resume(),
    server config from persisted options, and cleanup untested through
    the new API."""
    import loop_supervisor.runtime as rt
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
    run_id = supervisor.start_new_run().run_id

    events: list[str] = []

    class RecordingServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"
            events.append("construct")

        def start(self) -> None:
            events.append("start")

        def stop(self) -> None:
            events.append("stop")

        def add_observer(self, obs) -> None:
            events.append("add_observer")

        def run_agent(self, **kwargs):
            return "ok"

    original_oc_server = rt.OpenCodeServer
    original_run = Supervisor.run
    original_resume = Supervisor.resume

    def _ok_run(self, state):
        state.phase = "done"
        return state

    def _tracked_resume(self, state):
        events.append("resume")
        return original_resume(self, state)

    rt.OpenCodeServer = RecordingServer
    Supervisor.run = _ok_run
    Supervisor.resume = _tracked_resume
    try:
        session = rt.resume_run_session(repo.root, run_id)
        assert session.state is rt.SessionState.NEW
        with session:
            assert session.state is rt.SessionState.READY
            session.start_server()
            assert session.state is rt.SessionState.STARTED
            final = session.run_to_completion()
    finally:
        rt.OpenCodeServer = original_oc_server
        Supervisor.run = original_run
        Supervisor.resume = original_resume

    assert final.run_id == run_id
    assert final.phase == "done"
    assert session.state is rt.SessionState.CLOSED
    # Supervisor.resume() must run (it performs the Git validation that
    # makes a tampered resume fail closed) and must precede server startup.
    assert events == ["resume", "construct", "start", "stop"], events
    assert not _lock_path(repo.common_dir()).exists()


def test_resume_run_session_validation_failure_is_wrapped_and_releases_lock(tmp_path):
    """A resume whose Git validation fails must surface as RuntimeError_,
    never start a server, and leave no lock behind."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.supervisor import LoopError, Supervisor

    repo = _init_repo(tmp_path / "repo")
    supervisor = Supervisor(
        repo=repo,
        runner=MagicMock(),
        git_common_dir=repo.common_dir(),
        input_provider=MagicMock(),
        options=_make_options(),
    )
    run_id = supervisor.start_new_run().run_id

    started: list[str] = []

    class NeverStartedServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = None

        def start(self) -> None:
            started.append("start")

        def stop(self) -> None:
            pass

        def add_observer(self, obs) -> None:
            pass

    original_oc_server = rt.OpenCodeServer
    original_resume = Supervisor.resume

    def _boom_resume(self, state):
        raise LoopError("integration branch moved")

    rt.OpenCodeServer = NeverStartedServer
    Supervisor.resume = _boom_resume
    try:
        session = rt.resume_run_session(repo.root, run_id)
        with pytest.raises(rt.RuntimeError_, match="resume validation failed"):
            session.__enter__()
    finally:
        rt.OpenCodeServer = original_oc_server
        Supervisor.resume = original_resume

    assert started == []
    assert session.state is rt.SessionState.FAILED
    assert not _lock_path(repo.common_dir()).exists()


def test_abort_active_invocations_swallows_base_exception(tmp_path):
    """The "never raises" contract must be enforced by RunSession itself.

    OpenCodeServer.abort_active_sessions() suppresses only per-session
    Exceptions, so a KeyboardInterrupt from that call would otherwise
    escape a shutdown path and replace whatever outcome is being
    reported."""
    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")

    class AbortExplodesServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def add_observer(self, obs) -> None:
            pass

        def abort_active_sessions(self) -> None:
            raise KeyboardInterrupt()

    with _patched_session_env(repo, server_cls=AbortExplodesServer):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            # Must not propagate.
            session.abort_active_invocations()


def test_release_pending_after_body_failure_annotates_and_stays_retryable(tmp_path):
    """A release failure while a body exception unwinds must annotate the
    primary (never replace it) and still leave the lock retryable."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import LockError, _lock_path

    repo = _init_repo(tmp_path / "repo")
    the_error = ValueError("body blew up")
    original_release = rt._LockLease.release
    fail_once = [True]

    def flaky_release(self):
        if fail_once[0]:
            fail_once[0] = False
            raise LockError("transient release failure")
        original_release(self)

    with _patched_session_env(repo):
        rt._LockLease.release = flaky_release
        try:
            session = rt.new_run_session(repo.root, _make_options())
            with pytest.raises(ValueError) as excinfo:
                with session:
                    session.start_server()
                    raise the_error

            assert excinfo.value is the_error
            assert session.state is rt.SessionState.RELEASE_PENDING
            assert _lock_path(repo.common_dir()).exists()

            session.close()
        finally:
            rt._LockLease.release = original_release

    notes = getattr(the_error, "__notes__", [])
    assert any("lock could not be released" in n.lower() for n in notes), notes
    assert session.state is rt.SessionState.CLOSED
    assert not _lock_path(repo.common_dir()).exists()


def test_repeated_close_does_not_double_annotate_primary(tmp_path, monkeypatch):
    """Note suppression is identity-scoped: a second close() must not
    annotate the same exception twice, but must still report."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    the_error = ValueError("body blew up")

    with _patched_session_env(repo, server_cls=_retry_server()):
        session = rt.new_run_session(repo.root, _make_options())
        with pytest.raises(ValueError):
            with session:
                session.start_server()
                raise the_error

        first_notes = list(getattr(the_error, "__notes__", []))
        assert len(first_notes) == 1, first_notes

        # An explicit retry reports by raising, and must not re-annotate.
        with pytest.raises(RuntimeError_):
            session.close(primary=the_error)

    assert list(getattr(the_error, "__notes__", [])) == first_notes
