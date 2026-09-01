"""Tests for the shared runtime controller (runtime.py).

Uses monkeypatching to avoid launching real OpenCode processes.
Verifies lifecycle ordering: lock → state → server → run → server stop → lock release.
"""

from __future__ import annotations

import ast
import contextlib
import os
import subprocess
from pathlib import Path
from typing import Any
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

    def run(self, state: _FakeState, *, max_steps: int | None = None) -> _FakeState:
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

        def run(self, state, *, max_steps=None):
            call_log.append("run")
            state.phase = final_phase
            return state

        @property
        def runner(self):
            return None

        @runner.setter
        def runner(self, value):
            call_log.append("runner_set")

    @contextlib.contextmanager
    def _ctx():
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(rt, "GitRepo", FakeGitRepo)
            mp.setattr(rt, "OpenCodeServer", FakeOCServer)
            mp.setattr(rt, "Supervisor", FakeSupervisor)
            yield

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
        mp = pytest.MonkeyPatch()
        mp.setattr(rt._LockLease, "release", patched_release)
        try:
            run_new(tmp_path / "repo", _make_options())
        finally:
            mp.undo()

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
        mp = pytest.MonkeyPatch()
        mp.setattr(SupervisorLock, "acquire", patched_acquire)
        rt.load_state = patched_load_state
        try:
            try:
                run_resume(tmp_path / "repo", run_id)
            except Exception:
                pass
        finally:
            mp.undo()
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

        def run(self, state, *, max_steps=None):
            called.append("run")
            return state

        @property
        def runner(self):
            return None

        @runner.setter
        def runner(self, v):
            pass

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

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "GitRepo", FakeGitRepo)
    mp.setattr(rt, "OpenCodeServer", FakeOCServer)
    mp.setattr(rt, "Supervisor", SpySupervisor)
    try:
        run_new(tmp_path / "repo", _make_options())
    finally:
        mp.undo()

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

        def run(self, s, *, max_steps=None):
            called.append("run")
            return s

        @property
        def runner(self):
            return None

        @runner.setter
        def runner(self, v):
            pass

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

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "GitRepo", FakeGitRepo)
    mp.setattr(rt, "OpenCodeServer", FakeOCServer)
    mp.setattr(rt, "Supervisor", SpySupervisor)
    try:
        run_resume(tmp_path / "repo", run_id)
    except Exception:
        pass
    finally:
        mp.undo()

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

    class FailingOCServer:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            raise ServerStartupError("simulated startup failure")

        def stop(self):
            pass

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", FailingOCServer)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_:
            pass
    finally:
        mp.undo()

    runs = list_run_ids(tmp_path / "repo")
    assert len(runs) == 1
    state = load_state(repo.common_dir(), runs[0])
    assert state.phase == "operational_failure"
    assert state.last_error is not None
    assert state.last_error["kind"] == "opencode_startup"


def test_run_new_exhausted_malformed_output_persists_and_reports_cleanly(tmp_path):
    """End-to-end: a role that always returns malformed output must not
    let ContractError escape run_new() raw. Supervisor.run() converts the
    OPERATIONAL_FAILURE outcome to LoopError (one of the CLI's expected
    error types, see cli.py's _EXPECTED_CLI_ERRORS), and the durable
    operational_failure record is already persisted by the time it
    propagates, exactly like the other operational failure kinds covered
    above."""
    from loop_supervisor.state import load_state
    from loop_supervisor.supervisor import LoopError

    repo = _init_repo(tmp_path / "repo")

    import loop_supervisor.runtime as rt

    class MalformedOutputServer:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            pass

        def stop(self):
            pass

        def add_observer(self, obs):
            pass

        def run_agent(self, **kwargs):
            return "this is not json at all"

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", MalformedOutputServer)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError as exc:
            assert "Traceback" not in str(exc)
    finally:
        mp.undo()

    runs = list_run_ids(tmp_path / "repo")
    assert len(runs) == 1
    state = load_state(repo.common_dir(), runs[0])
    assert state.phase == "operational_failure"
    assert state.last_error is not None
    assert state.last_error["kind"] == "contract"
    assert state.last_error["retryable"] is True


def test_run_new_reports_denied_permissions_even_on_operational_failure(tmp_path, capsys):
    """_report_denied_permissions() must run even when run_to_completion()
    raises LoopError (the operational-failure path), not only on its
    successful-return path. Regression for backlog item 44: a headless
    run that fails because an agent gave up after having permission
    requests denied previously had no diagnostic connecting the two,
    since Supervisor.run() re-raises LoopError for OPERATIONAL_FAILURE
    before the summary line was ever reached."""
    from loop_supervisor.supervisor import LoopError

    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []

    import loop_supervisor.runtime as rt

    fake_server = _FakeServer(call_log)

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
            state.phase = "operational_failure"
            state.options = _make_options()
            return state

        def run(self, state, *, max_steps=None):
            raise LoopError("simulated operational failure")

        @property
        def runner(self):
            return None

        @runner.setter
        def runner(self, value):
            pass

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "GitRepo", FakeGitRepo)
    mp.setattr(rt, "OpenCodeServer", FakeOCServer)
    mp.setattr(rt, "Supervisor", FakeSupervisor)
    mp.setattr(
        rt,
        "PermissionDenier",
        _fake_denier_class(call_log, denied_count=3, denied_summary=["external_directory"]),
    )
    try:
        with pytest.raises(LoopError):
            run_new(tmp_path / "repo", _make_options())
    finally:
        mp.undo()

    captured = capsys.readouterr()
    assert "denied 3 permission request(s)" in captured.err
    assert "external_directory" in captured.err


def test_run_resume_reports_denied_permissions_even_on_operational_failure(tmp_path, capsys):
    """Same regression as the run_new() variant above, for run_resume():
    _report_denied_permissions() must run even when run_to_completion()
    raises LoopError."""
    from loop_supervisor.supervisor import LoopError

    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []

    import loop_supervisor.runtime as rt

    fake_server = _FakeServer(call_log)

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

    fake_state = MagicMock()
    fake_state.run_id = "fake-run"
    fake_state.phase = "operational_failure"
    fake_state.options = _make_options()

    class FakeSupervisor:
        def __init__(self, *a, **kw):
            pass

        def resume(self, state):
            return state

        def run(self, state, *, max_steps=None):
            raise LoopError("simulated operational failure")

        @property
        def runner(self):
            return None

        @runner.setter
        def runner(self, value):
            pass

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "GitRepo", FakeGitRepo)
    mp.setattr(rt, "OpenCodeServer", FakeOCServer)
    mp.setattr(rt, "Supervisor", FakeSupervisor)
    mp.setattr(rt, "load_state", lambda *a, **k: fake_state)
    mp.setattr(
        rt,
        "PermissionDenier",
        _fake_denier_class(call_log, denied_count=1, denied_summary=["bash"]),
    )
    try:
        with pytest.raises(LoopError):
            run_resume(tmp_path / "repo", "fake-run")
    finally:
        mp.undo()

    captured = capsys.readouterr()
    assert "denied 1 permission request(s)" in captured.err
    assert "bash" in captured.err


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

    class FailingOCServer:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            raise ServerStartupError("simulated startup failure")

        def stop(self):
            pass

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", FailingOCServer)
    try:
        try:
            run_resume(tmp_path / "repo", run_id)
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_:
            pass
    finally:
        mp.undo()

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

    def _make_failing_server(message: str):
        class FailingOCServer:
            def __init__(self, *a, **kw):
                pass

            def start(self):
                raise ServerStartupError(message)

            def stop(self):
                pass

        return FailingOCServer

    mp = pytest.MonkeyPatch()
    try:
        # First failure: interrupts a run that has not yet reached
        # PHASE_BUILDING; the resume path's own validation determines the
        # actual phase at the point OpenCode would have started. Use the
        # fresh run's phase (planning) as the true interrupted phase by
        # failing on run_new instead, then simulate a second failure via
        # run_resume against the resulting operational_failure state.
        mp.setattr(rt, "OpenCodeServer", _make_failing_server("first failure"))
        try:
            run_resume(tmp_path / "repo", run_id)
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_:
            pass

        first = load_state(repo.common_dir(), run_id)
        assert first.phase == "operational_failure"
        assert first.last_error is not None
        first_retry_phase = first.last_error["retry_phase"]
        assert first_retry_phase != "operational_failure"

        # Second failure on the same still-unrecovered run: retry_phase
        # must be unchanged, not overwritten with "operational_failure".
        mp.setattr(rt, "OpenCodeServer", _make_failing_server("second failure"))
        try:
            run_resume(tmp_path / "repo", run_id)
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_:
            pass

        second = load_state(repo.common_dir(), run_id)
        assert second.phase == "operational_failure"
        assert second.last_error is not None
        assert second.last_error["retry_phase"] == first_retry_phase
        assert second.last_error["retry_phase"] != "operational_failure"
        assert "second failure" in second.last_error["message"]

        # A third failure continues to preserve it.
        mp.setattr(rt, "OpenCodeServer", _make_failing_server("third failure"))
        try:
            run_resume(tmp_path / "repo", run_id)
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_:
            pass

        third = load_state(repo.common_dir(), run_id)
        assert third.last_error is not None
        assert third.last_error["retry_phase"] == first_retry_phase
    finally:
        mp.undo()

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

    class FailingStopServer:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            pass

        def stop(self):
            raise RuntimeError("simulated stop cleanup failure")

        def add_observer(self, obs):
            pass

    def _boom_run(self, state, *, max_steps=None):
        raise LoopError("simulated supervisor failure")

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", FailingStopServer)
    mp.setattr(Supervisor, "run", _boom_run)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError as exc:
            assert "simulated supervisor failure" in str(exc)
    finally:
        mp.undo()

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

    class FailingStopServer:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            pass

        def stop(self):
            raise RuntimeError("simulated stop cleanup failure")

        def add_observer(self, obs):
            pass

    def _fake_run(self, state, *, max_steps=None):
        state.phase = "done"
        return state

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", FailingStopServer)
    mp.setattr(Supervisor, "run", _fake_run)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_ as exc:
            assert "cleanup could not be confirmed" in str(exc)
    finally:
        mp.undo()

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

    def _fake_run(self, state, *, max_steps=None):
        state.phase = "done"
        return state

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", FailingStopServer)
    mp.setattr(Supervisor, "run", _fake_run)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_ as exc:
            assert "cleanup could not be confirmed" in str(exc)
            assert "unprintable _UnprintableStopError" in str(exc)
    finally:
        mp.undo()

    assert _lock_path(repo.common_dir()).exists()


def test_run_new_failed_run_and_failed_stop_preserves_run_exception_and_retains_lock(tmp_path):
    """If supervisor.run() raises AND server.stop() also fails, the
    original run exception must be what propagates, and the lock must be
    retained (not released) since cleanup was never confirmed."""
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.supervisor import LoopError, Supervisor

    repo = _init_repo(tmp_path / "repo")

    import loop_supervisor.runtime as rt

    class FailingStopServer:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            pass

        def stop(self):
            raise RuntimeError("simulated stop cleanup failure")

        def add_observer(self, obs):
            pass

    def _boom_run(self, state, *, max_steps=None):
        raise LoopError("simulated supervisor failure")

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", FailingStopServer)
    mp.setattr(Supervisor, "run", _boom_run)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError as exc:
            assert "simulated supervisor failure" in str(exc)
    finally:
        mp.undo()

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

    class FailingEverythingServer:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            raise ServerStartupError("simulated startup failure")

        def stop(self):
            raise RuntimeError("simulated cleanup-retry failure")

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", FailingEverythingServer)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_ as exc:
            assert "cleanup could not be confirmed" in str(exc)
    finally:
        mp.undo()

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

    class FailingStopServer:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            pass

        def stop(self):
            raise RuntimeError("simulated stop cleanup failure")

        def add_observer(self, obs):
            pass

    def _fake_run(self, state, *, max_steps=None):
        state.phase = "done"
        return state

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", FailingStopServer)
    mp.setattr(Supervisor, "run", _fake_run)
    try:
        try:
            run_resume(tmp_path / "repo", run_id)
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_ as exc:
            assert "cleanup could not be confirmed" in str(exc)
    finally:
        mp.undo()

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

    class FailingEverythingServer:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            raise ServerStartupError("simulated startup failure")

        def stop(self):
            raise RuntimeError("simulated cleanup-retry failure")

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", FailingEverythingServer)
    try:
        try:
            run_resume(tmp_path / "repo", run_id)
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_ as exc:
            assert "cleanup could not be confirmed" in str(exc)
    finally:
        mp.undo()

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

    def _capture(self, *, max_steps=None):
        captured["record"] = json.loads(_lock_path(repo.common_dir()).read_text())
        return original_run_to_completion(self, max_steps=max_steps)

    monkeypatch.setattr(rt.RunSession, "run_to_completion", _capture)

    call_log: list[str] = []
    with _patch_runtime(repo, call_log=call_log):
        monkeypatch.chdir(tmp_path)
        run_new(Path("repo"), _make_options())

    record = captured["record"]
    assert isinstance(record, dict)
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
    # AlwaysFailsServer is a duck-typed fake (only .stop()), not a real
    # OpenCodeServer; _confirm_server_stopped only ever calls .stop() on it.
    outcome = rt._confirm_server_stopped(server)  # type: ignore[arg-type]

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
    # TrackedServer is a duck-typed fake (only .stop()), not a real
    # OpenCodeServer; _confirm_server_stopped only ever calls .stop() on it.
    outcome = rt._confirm_server_stopped(server)  # type: ignore[arg-type]
    assert outcome.confirmed is True
    assert len(set(seen_instances)) == 1
    assert seen_instances[0] == id(server)


def test_confirm_server_stopped_interrupt_during_backoff_reported_not_raised(monkeypatch):
    """A KeyboardInterrupt/SystemExit delivered while waiting out the
    inter-attempt backoff (not while stop() itself is running) must be
    reported via last_error, exactly like an interrupt raised by stop()
    itself -- not escape and bypass the structured-outcome contract this
    function documents ("returning structured success/failure information
    instead of raising")."""
    import loop_supervisor.runtime as rt

    the_interrupt = KeyboardInterrupt()

    def _sleep_raises(seconds: float) -> None:
        raise the_interrupt

    monkeypatch.setattr(rt.time, "sleep", _sleep_raises)

    class AlwaysFailsServer:
        def __init__(self) -> None:
            self.stop_calls = 0

        def stop(self) -> None:
            self.stop_calls += 1
            raise RuntimeError(f"fail {self.stop_calls}")

    server = AlwaysFailsServer()
    outcome = rt._confirm_server_stopped(server)  # type: ignore[arg-type]

    assert outcome.confirmed is False
    assert outcome.last_error is the_interrupt
    # The interrupt arrives during the backoff after the first failed
    # attempt, so stop() must have been called exactly once -- the retry
    # loop must not proceed to a second attempt.
    assert server.stop_calls == 1
    assert outcome.attempts == 1


def test_confirm_server_stopped_system_exit_during_backoff_reported_not_raised(monkeypatch):
    """SystemExit gets the identical treatment as KeyboardInterrupt when
    delivered during backoff."""
    import loop_supervisor.runtime as rt

    the_exit = SystemExit(1)

    def _sleep_raises(seconds: float) -> None:
        raise the_exit

    monkeypatch.setattr(rt.time, "sleep", _sleep_raises)

    class AlwaysFailsServer:
        def stop(self) -> None:
            raise RuntimeError("fail")

    outcome = rt._confirm_server_stopped(AlwaysFailsServer())  # type: ignore[arg-type]

    assert outcome.confirmed is False
    assert outcome.last_error is the_exit
    assert outcome.attempts == 1


def test_confirm_server_stopped_interrupt_on_final_attempt_reported_not_raised():
    """An interrupt raised by stop() itself on the last attempt (budget
    about to be exhausted, no further backoff would occur even without
    an interrupt) must still be reported via last_error, not raised."""
    import loop_supervisor.runtime as rt

    the_interrupt = KeyboardInterrupt()

    class InterruptsOnLastAttempt:
        def __init__(self) -> None:
            self.calls = 0

        def stop(self) -> None:
            self.calls += 1
            if self.calls == rt._CLEANUP_ATTEMPTS:
                raise the_interrupt
            raise RuntimeError(f"fail {self.calls}")

    server = InterruptsOnLastAttempt()
    outcome = rt._confirm_server_stopped(server)  # type: ignore[arg-type]

    assert outcome.confirmed is False
    assert outcome.last_error is the_interrupt
    assert outcome.attempts == rt._CLEANUP_ATTEMPTS
    assert server.calls == rt._CLEANUP_ATTEMPTS


def test_run_new_startup_transient_stop_failure_then_success_releases_lock(tmp_path, monkeypatch):
    """A startup failure whose defense-in-depth stop() retry fails once
    and then succeeds must still confirm cleanup and release the lock."""
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.opencode import ServerStartupError
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)

    Server = _flaky_stop_server(fail_times=1)

    # Server is a class object returned from a runtime factory, not a
    # statically known type, so mypy cannot verify it as a base class.
    class FailingStartServer(Server):  # type: ignore[misc,valid-type]
        def start(self) -> None:
            raise ServerStartupError("simulated startup failure")

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", FailingStartServer)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_:
            pass
    finally:
        mp.undo()

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

    class FailingEverythingServer:
        def __init__(self, *a, **kw) -> None:
            pass

        def start(self) -> None:
            raise ServerStartupError("simulated startup failure")

        def stop(self) -> None:
            raise RuntimeError("simulated cleanup-retry failure")

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", FailingEverythingServer)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_ as exc:
            assert "cleanup could not be confirmed" in str(exc)
    finally:
        mp.undo()

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

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", FailingEverythingServer)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_ as exc:
            assert "cleanup could not be confirmed" in str(exc)
            assert "unprintable _UnprintableCleanupError" in str(exc)
    finally:
        mp.undo()

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

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", FailingOCServer)
    mp.setattr(sup_module.Supervisor, "record_external_failure", _boom_record)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_ as exc:
            assert "could not be persisted" in str(exc)
            assert "unprintable _UnprintablePersistError" in str(exc)
    finally:
        mp.undo()


def test_run_new_startup_keyboard_interrupt_preserves_identity(tmp_path):
    """A KeyboardInterrupt raised from server.start() must propagate as
    the exact same object, never wrapped in RuntimeError_, and must not
    be persisted as an operational failure."""
    import pytest

    from loop_supervisor.state import load_state

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

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

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", InterruptingServer)
    try:
        with pytest.raises(KeyboardInterrupt) as excinfo:
            run_new(tmp_path / "repo", _make_options())
        assert excinfo.value is the_interrupt
    finally:
        mp.undo()

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

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", InterruptingServer)
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
        mp.undo()


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

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", InterruptingServer)
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
        mp.undo()


def test_run_new_startup_system_exit_preserves_identity(tmp_path):
    import pytest

    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

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

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", ExitingServer)
    try:
        with pytest.raises(SystemExit) as excinfo:
            run_new(tmp_path / "repo", _make_options())
        assert excinfo.value is the_exit
    finally:
        mp.undo()


def test_run_new_startup_system_exit_preserves_exact_traceback(tmp_path):
    """SystemExit must be preserved with the same exact-traceback
    guarantee as KeyboardInterrupt: both are direct BaseExceptions
    handled by the same run_new()/_finalize_interrupted_startup() path."""
    import pytest

    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

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

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", ExitingServer)
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
        mp.undo()


def test_run_new_startup_keyboard_interrupt_with_unresolved_cleanup_has_note(tmp_path):
    """If cleanup retries are exhausted after a startup KeyboardInterrupt,
    the exact interrupt object must still propagate, with a retained-lock
    note attached (not replaced by a RuntimeError_)."""
    import pytest

    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

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

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", InterruptingServer)
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
        mp.undo()

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

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", RecordingServer)
    mp.setattr(rt, "Supervisor", BoomOnRunnerSupervisor)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError:
            pass
    finally:
        mp.undo()

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
    Server = _flaky_stop_server(fail_times=1)

    def _fake_run(self, state, *, max_steps=None):
        state.phase = "done"
        return state

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", Server)
    mp.setattr(Supervisor, "run", _fake_run)
    try:
        run_new(tmp_path / "repo", _make_options())
    finally:
        mp.undo()

    assert not _lock_path(repo.common_dir()).exists()


def test_run_new_successful_run_cleanup_exhaustion_retains_lock(tmp_path, monkeypatch):
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.runtime import RuntimeError_
    from loop_supervisor.supervisor import Supervisor

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    Server = _flaky_stop_server(fail_times=999)

    def _fake_run(self, state, *, max_steps=None):
        state.phase = "done"
        return state

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", Server)
    mp.setattr(Supervisor, "run", _fake_run)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_ as exc:
            assert "cleanup could not be confirmed" in str(exc)
    finally:
        mp.undo()

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
    Server = _flaky_stop_server(fail_times=1)

    def _boom_run(self, state, *, max_steps=None):
        raise LoopError("simulated supervisor failure")

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", Server)
    mp.setattr(Supervisor, "run", _boom_run)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError:
            pass
    finally:
        mp.undo()

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
    Server = _flaky_stop_server(fail_times=999)

    the_failure = LoopError("simulated supervisor failure")

    def _boom_run(self, state, *, max_steps=None):
        raise the_failure

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", Server)
    mp.setattr(Supervisor, "run", _boom_run)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError as exc:
            assert exc is the_failure
    finally:
        mp.undo()

    notes = getattr(the_failure, "__notes__", [])
    assert any("cleanup" in n.lower() and "retained" in n.lower() for n in notes), notes
    assert _lock_path(repo.common_dir()).exists()


def test_run_new_run_time_keyboard_interrupt_preserves_identity(tmp_path):
    import pytest

    from loop_supervisor.supervisor import Supervisor

    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    class RecordingServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def add_observer(self, obs) -> None:
            pass

    the_interrupt = KeyboardInterrupt()

    def _boom_run(self, state, *, max_steps=None):
        raise the_interrupt

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", RecordingServer)
    mp.setattr(Supervisor, "run", _boom_run)
    try:
        with pytest.raises(KeyboardInterrupt) as excinfo:
            run_new(tmp_path / "repo", _make_options())
        assert excinfo.value is the_interrupt
    finally:
        mp.undo()


def test_run_new_cleanup_time_keyboard_interrupt_does_not_replace_primary(tmp_path):
    """If supervisor.run() raises an ordinary failure AND server.stop()
    itself raises KeyboardInterrupt during cleanup, the original run
    failure must still be what propagates -- the cleanup-time interrupt
    must never replace it."""
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.supervisor import LoopError, Supervisor

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    class InterruptingStopServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"

        def start(self) -> None:
            pass

        def stop(self) -> None:
            raise KeyboardInterrupt()

        def add_observer(self, obs) -> None:
            pass

    the_failure = LoopError("simulated supervisor failure")

    def _boom_run(self, state, *, max_steps=None):
        raise the_failure

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", InterruptingStopServer)
    mp.setattr(Supervisor, "run", _boom_run)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError as exc:
            assert exc is the_failure
    finally:
        mp.undo()

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

    class FailingEverythingServer:
        def __init__(self, *a, **kw) -> None:
            pass

        def start(self) -> None:
            raise ServerStartupError("simulated startup failure")

        def stop(self) -> None:
            raise RuntimeError("simulated cleanup failure")

    def _boom_record_external_failure(self, *a, **kw):
        raise RuntimeError("simulated persistence failure")

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", FailingEverythingServer)
    mp.setattr(Supervisor, "record_external_failure", _boom_record_external_failure)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_ as exc:
            message = str(exc)
            assert "could not be persisted" in message
            assert "cleanup could not be confirmed" in message
    finally:
        mp.undo()

    assert _lock_path(repo.common_dir()).exists()


def test_run_new_lock_release_failure_with_existing_primary_attaches_note(tmp_path, monkeypatch):
    """If the run itself fails (cleanup confirmed OK) and the subsequent
    lock release also fails, the original run exception must still be
    what propagates, with a note describing the lock-release failure."""
    from loop_supervisor.locking import LockError
    from loop_supervisor.supervisor import LoopError, Supervisor

    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    class CleanServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def add_observer(self, obs) -> None:
            pass

    the_failure = LoopError("simulated supervisor failure")

    def _boom_run(self, state, *, max_steps=None):
        raise the_failure

    def _boom_release(self):
        raise LockError("simulated lock-release failure")

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", CleanServer)
    mp.setattr(Supervisor, "run", _boom_run)
    monkeypatch.setattr(rt._LockLease, "release", _boom_release)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError as exc:
            assert exc is the_failure
    finally:
        mp.undo()

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

    class CleanServer:
        def __init__(self, *a, **kw) -> None:
            self.base_url = "http://127.0.0.1:9999"

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def add_observer(self, obs) -> None:
            pass

    the_failure = LoopError("simulated supervisor failure")

    def _boom_run(self, state, *, max_steps=None):
        raise the_failure

    class _UnprintableLockError(LockError):
        def __str__(self) -> str:
            raise RuntimeError("simulated str failure in lock error")

    def _boom_release(self):
        raise _UnprintableLockError("simulated lock-release failure")

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", CleanServer)
    mp.setattr(Supervisor, "run", _boom_run)
    monkeypatch.setattr(rt._LockLease, "release", _boom_release)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError as exc:
            assert exc is the_failure
    finally:
        mp.undo()

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

    def _fake_run(self, state, *, max_steps=None):
        state.phase = "done"
        return state

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", ObservingServer)
    mp.setattr(Supervisor, "run", _fake_run)
    try:
        run_new(tmp_path / "repo", _make_options())
    finally:
        mp.undo()

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

        def run(self, state, *, max_steps=None):
            return state

        @property
        def runner(self):
            return None

        @runner.setter
        def runner(self, v):
            pass

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "GitRepo", FakeGitRepo)
    mp.setattr(rt, "OpenCodeServer", FakeOCServer)
    mp.setattr(rt, "Supervisor", SpySupervisor)
    try:
        # sentinel_provider is a bare object() used only for identity
        # comparison below, not a real InputProvider.
        run_new(
            tmp_path / "repo",
            _make_options(),
            input_provider=sentinel_provider,  # type: ignore[arg-type]
        )
    finally:
        mp.undo()

    assert received_providers == [sentinel_provider]


def test_run_new_defaults_to_stdin_input_provider_when_not_supplied(tmp_path):
    """When no input_provider is passed, run_new() must fall back to a real
    StdinInputProvider (preserving prior behavior for existing callers),
    not None and not some other default."""
    from loop_supervisor.input_providers import StdinInputProvider

    repo = _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    received_providers: list[object] = []

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

        def run(self, state, *, max_steps=None):
            return state

        @property
        def runner(self):
            return None

        @runner.setter
        def runner(self, v):
            pass

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "GitRepo", FakeGitRepo)
    mp.setattr(rt, "OpenCodeServer", FakeOCServer)
    mp.setattr(rt, "Supervisor", SpySupervisor)
    try:
        run_new(tmp_path / "repo", _make_options())
    finally:
        mp.undo()

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
    mp = pytest.MonkeyPatch()
    mp.setattr(
        rt,
        "OpenCodeServer",
        _characterization_server(
            counter, start_exc=ServerStartupError("simulated startup failure")
        ),
    )
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_:
            pass
    finally:
        mp.undo()

    assert counter[0] == rt._CLEANUP_ATTEMPTS


def test_characterize_run_failure_stop_attempts_are_bounded(tmp_path, monkeypatch):
    """Same bound on the run-failure path."""
    from loop_supervisor.supervisor import LoopError, Supervisor

    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    counter = [0]

    def _boom_run(self, state, *, max_steps=None):
        raise LoopError("simulated supervisor failure")

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", _characterization_server(counter))
    mp.setattr(Supervisor, "run", _boom_run)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError:
            pass
    finally:
        mp.undo()

    assert counter[0] == rt._CLEANUP_ATTEMPTS


def test_characterize_successful_run_stop_attempts_are_bounded(tmp_path, monkeypatch):
    """Same bound on the successful-run path."""
    from loop_supervisor.runtime import RuntimeError_
    from loop_supervisor.supervisor import Supervisor

    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    counter = [0]

    def _ok_run(self, state, *, max_steps=None):
        state.phase = "done"
        return state

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", _characterization_server(counter))
    mp.setattr(Supervisor, "run", _ok_run)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected RuntimeError_ to be raised")
        except RuntimeError_:
            pass
    finally:
        mp.undo()

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
    the_failure = LoopError("simulated supervisor failure")

    def _boom_run(self, state, *, max_steps=None):
        raise the_failure

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", _characterization_server(counter))
    mp.setattr(Supervisor, "run", _boom_run)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError as exc:
            assert exc is the_failure
    finally:
        mp.undo()

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
    mp = pytest.MonkeyPatch()
    mp.setattr(
        rt,
        "OpenCodeServer",
        _characterization_server(
            counter, start_exc=ServerStartupError("simulated startup failure")
        ),
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
        mp.undo()


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

    def _ok_run(self, state, *, max_steps=None):
        state.phase = "done"
        return state

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", _characterization_server(counter))
    mp.setattr(Supervisor, "run", _ok_run)
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
        mp.undo()


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
    mp = pytest.MonkeyPatch()
    mp.setattr(
        rt,
        "OpenCodeServer",
        _characterization_server(counter, start_exc=the_interrupt, stop_fails=False),
    )
    try:
        with pytest.raises(KeyboardInterrupt) as excinfo:
            run_new(tmp_path / "repo", _make_options())
        assert excinfo.value is the_interrupt
    finally:
        mp.undo()

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
    mp = pytest.MonkeyPatch()
    mp.setattr(
        rt,
        "OpenCodeServer",
        _characterization_server(counter, start_exc=the_exit, stop_fails=False),
    )
    try:
        with pytest.raises(SystemExit) as excinfo:
            run_new(tmp_path / "repo", _make_options())
        assert excinfo.value is the_exit
    finally:
        mp.undo()

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
    mp = pytest.MonkeyPatch()
    mp.setattr(
        rt,
        "OpenCodeServer",
        _characterization_server(counter, start_exc=the_interrupt, stop_fails=True),
    )
    try:
        with pytest.raises(KeyboardInterrupt) as excinfo:
            run_new(tmp_path / "repo", _make_options())
        assert excinfo.value is the_interrupt
    finally:
        mp.undo()

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

    class BoomOnRunnerSupervisor(Supervisor):
        @property
        def runner(self):
            return self._runner

        @runner.setter
        def runner(self, value):
            if getattr(value, "base_url", None) is not None:
                raise LoopError("simulated runner-assignment failure")
            self._runner = value

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", _characterization_server(counter, stop_fails=False))
    mp.setattr(rt, "Supervisor", BoomOnRunnerSupervisor)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError as exc:
            assert not isinstance(exc, rt.RuntimeError_)
            assert "simulated runner-assignment failure" in str(exc)
    finally:
        mp.undo()

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

        def run(self, state, *, max_steps=None):
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

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rt, "GitRepo", FakeGitRepo)
        mp.setattr(rt, "OpenCodeServer", server_cls or DefaultServer)
        mp.setattr(rt, "Supervisor", supervisor_cls or DefaultSupervisor)
        yield call_log


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


def test_run_session_run_to_completion_passes_max_steps_through(tmp_path):
    """max_steps must reach Supervisor.run() unchanged; it is a
    per-invocation session control, never persisted into RunOptions."""
    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")
    received: list[object] = []

    class RecordingSupervisor:
        def __init__(self, *a, **kw):
            self._runner = None

        def start_new_run(self):
            state = MagicMock()
            state.run_id = "fake-run"
            state.phase = "planning"
            state.options = _make_options()
            return state

        def run(self, state, *, max_steps=None):
            received.append(max_steps)
            state.phase = "done"
            return state

        @property
        def runner(self):
            return self._runner

        @runner.setter
        def runner(self, value):
            self._runner = value

    with _patched_session_env(repo, supervisor_cls=RecordingSupervisor):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            final = session.run_to_completion(max_steps=3)

    assert received == [3]
    assert final.phase == "done"


def test_run_new_passes_max_steps_through_to_supervisor_run(tmp_path):
    """The max_steps keyword must reach Supervisor.run() from the module-
    level run_new() convenience wrapper, not just from RunSession directly."""
    received: list[object] = []
    repo = _init_repo(tmp_path / "repo")

    class RecordingSupervisor:
        def __init__(self, *a, **kw):
            self._runner = None

        def start_new_run(self):
            state = MagicMock()
            state.run_id = "fake-run"
            state.phase = "planning"
            state.options = _make_options()
            return state

        def run(self, state, *, max_steps=None):
            received.append(max_steps)
            state.phase = "done"
            return state

        @property
        def runner(self):
            return self._runner

        @runner.setter
        def runner(self, value):
            self._runner = value

    with _patched_session_env(repo, supervisor_cls=RecordingSupervisor):
        run_new(repo.root, _make_options(), max_steps=5)

    assert received == [5]


def test_run_session_run_to_completion_stops_early_without_error_and_cleans_up(tmp_path):
    """A run stopped early by max_steps (non-terminal phase) must not raise,
    and the session's own cleanup (stop the server, release the lock) must
    still run via the normal __exit__ path."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")
    call_log: list[str] = []

    class PausingSupervisor:
        def __init__(self, *a, **kw):
            self._runner = None

        def start_new_run(self):
            state = MagicMock()
            state.run_id = "fake-run"
            state.phase = "planning"
            state.options = _make_options()
            return state

        def run(self, state, *, max_steps=None):
            call_log.append(f"run:max_steps={max_steps}")
            # Simulate stopping one step short of a terminal phase.
            state.phase = "building"
            return state

        @property
        def runner(self):
            return self._runner

        @runner.setter
        def runner(self, value):
            self._runner = value

    with _patched_session_env(repo, supervisor_cls=PausingSupervisor, call_log=call_log):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            final = session.run_to_completion(max_steps=1)

    assert final.phase == "building"
    assert "server_stop" in call_log
    assert not _lock_path(repo.common_dir()).exists()


# --- runner handoff ---------------------------------------------------


def test_run_session_constructs_server_with_venv_path_env(tmp_path):
    """RunSession.__enter__ must build the OpenCodeServer's env via
    build_agent_env(), so agent invocations can find a project-local
    .venv/bin without needing an external_directory permission to look
    for tools elsewhere (see ADR 0014)."""
    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")
    (repo.root / ".venv" / "bin").mkdir(parents=True)
    captured_configs = []

    class CapturingServer:
        def __init__(self, project_root, config, *a, **kw):
            captured_configs.append(config)
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            pass

        def stop(self):
            pass

        def add_observer(self, obs):
            pass

        def abort_active_sessions(self):
            pass

    with _patched_session_env(repo, server_cls=CapturingServer):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            pass

    assert len(captured_configs) == 1
    env = captured_configs[0].env
    assert env is not None
    entries = env["PATH"].split(os.pathsep)
    assert entries[0] == os.path.join(".venv", "bin")
    assert str(repo.root / ".venv" / "bin") in entries


def test_run_session_start_server_installs_runner(tmp_path):
    """start_server() must hand the server to the supervisor, otherwise
    advance() would dispatch against the _UnstartedRunner placeholder."""
    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo) as call_log:
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            assert session._supervisor is not None
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
            self._runner: Any = None

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

    def _boom_release(self):
        raise LockError("release failed")

    with _patched_session_env(repo, supervisor_cls=BoomSupervisor):
        mp = pytest.MonkeyPatch()
        mp.setattr(rt._LockLease, "release", _boom_release)
        try:
            session = rt.new_run_session(repo.root, _make_options())
            with pytest.raises(RuntimeError, match="state creation failed") as excinfo:
                session.__enter__()
        finally:
            mp.undo()

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
            assert session._lease is not None
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

    def _boom_release(self):
        raise LockError("release failed")

    with _patched_session_env(repo):
        mp = pytest.MonkeyPatch()
        mp.setattr(rt._LockLease, "release", _boom_release)
        try:
            session = rt.new_run_session(repo.root, _make_options())
            with pytest.raises(ValueError) as excinfo:
                with session:
                    session.start_server()
                    raise the_error
        finally:
            mp.undo()

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
        # sentinel is a bare object() used only for identity comparison
        # above, not a real InvocationObserver.
        session = rt.new_run_session(
            repo.root,
            _make_options(),
            server_observer=sentinel,  # type: ignore[arg-type]
        )
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
        session.add_observer(object())  # type: ignore[arg-type]


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
        # sentinel is a bare object() used only for identity comparison
        # below, not a real InputProvider.
        session = rt.new_run_session(
            repo.root,
            _make_options(),
            input_provider=sentinel,  # type: ignore[arg-type]
        )
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
        mp = pytest.MonkeyPatch()
        mp.setattr(rt._LockLease, "release", flaky_release)
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
            mp.undo()

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
        mp = pytest.MonkeyPatch()
        mp.setattr(rt._LockLease, "release", flaky_release)
        try:
            session = rt.new_run_session(repo.root, _make_options())
            with pytest.raises(RuntimeError, match="state creation failed"):
                session.__enter__()
            assert _lock_path(repo.common_dir()).exists()

            session.close()
        finally:
            mp.undo()

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
        mp = pytest.MonkeyPatch()
        mp.setattr(rt._LockLease, "release", flaky_release)
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
            mp.undo()

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
    # stop_fails=True so close() runs the full bounded retry before returning.
    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", _retry_server(start_exc=the_interrupt, stop_fails=True))
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
        mp.undo()


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

    original_resume = Supervisor.resume

    def _ok_run(self, state, *, max_steps=None):
        state.phase = "done"
        return state

    def _tracked_resume(self, state):
        events.append("resume")
        return original_resume(self, state)

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", RecordingServer)
    mp.setattr(Supervisor, "run", _ok_run)
    mp.setattr(Supervisor, "resume", _tracked_resume)
    try:
        session = rt.resume_run_session(repo.root, run_id)
        assert session.state is rt.SessionState.NEW
        with session:
            assert session.state is rt.SessionState.READY
            session.start_server()
            assert session.state is rt.SessionState.STARTED
            final = session.run_to_completion()
    finally:
        mp.undo()

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

    def _boom_resume(self, state):
        raise LoopError("integration branch moved")

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", NeverStartedServer)
    mp.setattr(Supervisor, "resume", _boom_resume)
    try:
        session = rt.resume_run_session(repo.root, run_id)
        with pytest.raises(rt.RuntimeError_, match="resume validation failed"):
            session.__enter__()
    finally:
        mp.undo()

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
        mp = pytest.MonkeyPatch()
        mp.setattr(rt._LockLease, "release", flaky_release)
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
            mp.undo()

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
        # (the_error is not currently unwinding here -- pytest.raises()
        # above already caught and released it -- so this exercises the
        # detached-caller shape, not the __exit__-driven one.)
        with pytest.raises(RuntimeError_):
            session.close(outcome=rt._RunOutcome.FAILED, error=the_error)

    assert list(getattr(the_error, "__notes__", [])) == first_notes


# --- close()'s outcome/error split (A4) --------------------------------
#
# `close()` used to take a single `primary: BaseException | None`
# parameter that conflated three separate facts: whether the run
# succeeded, which exception (if any) to annotate, and whether that
# exception is the one actively unwinding through this call. The tests
# below pin the two defects that conflation caused (D1, D2), the
# already-correct behaviour it must continue to preserve, the pure
# wording function in isolation, and the previously-untested corner
# where the two ways of asking "is it unwinding" could disagree.


def test_close_detached_caller_with_release_failure_raises(tmp_path):
    """D2: a caller that already caught `error` (it is not the exception
    currently unwinding) and retries close() explicitly must see the
    release failure raised, not silently swallowed.

    Before this fix, the release-failure branch returned silently
    whenever `error` was merely given, regardless of whether it was
    still unwinding -- so a caller in exactly this shape (the pattern
    app.py's except-handler cleanup uses) could not tell a failed retry
    from a successful one."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import LockError, _lock_path

    repo = _init_repo(tmp_path / "repo")
    the_error = ValueError("body blew up")

    def _always_fail_release(self):
        raise LockError("release still failing")

    with _patched_session_env(repo):
        mp = pytest.MonkeyPatch()
        mp.setattr(rt._LockLease, "release", _always_fail_release)
        try:
            session = rt.new_run_session(repo.root, _make_options())
            session.__enter__()
            session.start_server()

            # `the_error` is caught and released here -- by the time
            # close() is called below, it is no longer unwinding.
            try:
                raise the_error
            except ValueError:
                pass

            with pytest.raises(LockError):
                session.close(outcome=rt._RunOutcome.FAILED, error=the_error)
        finally:
            mp.undo()

    assert session.state is rt.SessionState.RELEASE_PENDING
    assert _lock_path(repo.common_dir()).exists()
    notes = getattr(the_error, "__notes__", [])
    assert any("lock could not be released" in n.lower() for n in notes), notes


def test_close_outcome_failed_with_no_error_uses_run_failed_wording(tmp_path, monkeypatch):
    """D1: outcome=FAILED with error=None (a caller that knows the run
    failed but has nothing further to annotate) must still get the
    "the run failed and the ..." wording, not "run completed but ...".

    Before this fix, wording was selected from whether `error` was given
    rather than from an explicit outcome, so this exact shape -- failure
    without an exception in hand -- was inexpressible and silently
    reported as success."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)

    with _patched_session_env(repo, server_cls=_retry_server()):
        session = rt.new_run_session(repo.root, _make_options())
        session.__enter__()
        session.start_server()

        with pytest.raises(RuntimeError_, match="the run failed and the"):
            session.close(outcome=rt._RunOutcome.FAILED, error=None)

    assert session.state is rt.SessionState.CLEANUP_UNRESOLVED


def test_close_exit_success_path_wording_unchanged(tmp_path, monkeypatch):
    """Regression guard for the split: __exit__ with no body exception
    must still produce "run completed but ..." with zero notes -- the
    default outcome/error values must reproduce the old no-argument
    close() call exactly."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.runtime import RuntimeError_
    from loop_supervisor.supervisor import Supervisor

    _init_repo(tmp_path / "repo")
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    counter = [0]

    def _ok_run(self, state, *, max_steps=None):
        state.phase = "done"
        return state

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", _characterization_server(counter))
    mp.setattr(Supervisor, "run", _ok_run)
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
        mp.undo()


def test_close_exit_failure_path_wording_unchanged(tmp_path, monkeypatch):
    """Regression guard for the split: __exit__ with a body exception
    must still produce exactly one "the run failed and the ..." note on
    that exact exception -- the outcome=FAILED, error=exc_val mapping in
    __exit__ must reproduce the old close(primary=exc_val) call exactly."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.supervisor import LoopError, Supervisor

    _init_repo(tmp_path / "repo")
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    counter = [0]
    the_failure = LoopError("simulated supervisor failure")

    def _boom_run(self, state, *, max_steps=None):
        raise the_failure

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", _characterization_server(counter))
    mp.setattr(Supervisor, "run", _boom_run)
    try:
        try:
            run_new(tmp_path / "repo", _make_options())
            raise AssertionError("expected LoopError to be raised")
        except LoopError as exc:
            assert exc is the_failure
    finally:
        mp.undo()

    notes = getattr(the_failure, "__notes__", [])
    assert len(notes) == 1, notes
    assert notes[0].startswith("the run failed and the OpenCode server cleanup could not be"), (
        notes[0]
    )


def test_cleanup_prefix_truth_table():
    """`_cleanup_prefix` is a pure function of (outcome, startup
    interrupted); exercise all four combinations directly, without
    constructing a session. startup_interrupted must take precedence
    over outcome regardless of outcome's value."""
    import loop_supervisor.runtime as rt

    assert (
        rt._cleanup_prefix(rt._RunOutcome.SUCCEEDED, startup_interrupted=False)
        == "run completed but"
    )
    assert (
        rt._cleanup_prefix(rt._RunOutcome.FAILED, startup_interrupted=False)
        == "the run failed and the"
    )
    assert (
        rt._cleanup_prefix(rt._RunOutcome.SUCCEEDED, startup_interrupted=True)
        == "startup was interrupted; the"
    )
    assert (
        rt._cleanup_prefix(rt._RunOutcome.FAILED, startup_interrupted=True)
        == "startup was interrupted; the"
    )


def test_close_detached_caller_with_startup_interrupt_pending_raises(tmp_path):
    """The startup-interrupt branch has the same unwinding-vs-given
    distinction as the release-failure branch (D2), just checked against
    `self._startup_exception` rather than `error`. Once the interrupt has
    already been caught elsewhere (it is no longer the exception
    currently unwinding) and the caller retries close() explicitly, the
    unresolved-cleanup failure must be raised, not silently swallowed --
    this is the corner no prior test exercised, where the old
    parameter-identity check (`primary is startup_exc`) and the
    currently-unwinding check could disagree: the interrupt object is
    still the same object, but it is no longer unwinding.

    The discriminating call is `close(error=the_interrupt)`: passing the
    interrupt object explicitly while it is *not* the exception currently
    unwinding. A bare `close()` does NOT discriminate here -- under the
    pre-split API that is equivalent to `primary=None`, which the old
    code already raised on, so a bare call passes against both the fixed
    and the unfixed code and proves nothing. (An earlier version of this
    test called bare `close()` for exactly that reason and was vacuous;
    confirmed by running it against the pre-split runtime, where it
    passed despite the bug still being present.)"""
    import loop_supervisor.runtime as rt
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    the_interrupt = KeyboardInterrupt()
    server_cls = _retry_server(start_exc=the_interrupt, stop_fails=True)
    with _patched_session_env(repo, server_cls=server_cls):
        session = rt.new_run_session(repo.root, _make_options())
        session.__enter__()

        # Caught here, not re-raised into an unwinding `with` body: by
        # the time close() is called below, the interrupt is no longer
        # the exception currently unwinding.
        try:
            session.start_server()
            raise AssertionError("expected KeyboardInterrupt to be raised")
        except KeyboardInterrupt as exc:
            assert exc is the_interrupt

        assert session._startup_exception is the_interrupt

        with pytest.raises(RuntimeError_, match="startup was interrupted; the"):
            session.close(error=the_interrupt)

    assert session.state is rt.SessionState.CLEANUP_UNRESOLVED
    notes = getattr(the_interrupt, "__notes__", [])
    assert len(notes) == 1, notes


# --- characterization for TUI-sync work (Phase A) ----------------------
#
# These pin two behaviors that no prior test asserted, both load-bearing
# for upcoming changes:
#
#   1. The lock record's `operation` field, which a future `operation=`
#      constructor parameter must default to preserving exactly.
#   2. The single-threaded baseline for two races that adding
#      synchronisation must fix: advance() unconditionally restoring
#      SessionState.STARTED even when a concurrent close() has moved past
#      it, and close() releasing the lock without waiting for an
#      in-flight advance() to finish mutating state.
#
# The concurrency tests use a supervisor whose advance() blocks on an
# Event until the test releases it, so the race window is deterministic
# rather than timing-dependent.


def test_run_session_new_run_lock_operation_is_run(tmp_path):
    """The lock record for a new run must say operation="run". A future
    operation= constructor parameter needs this pinned as the default it
    must continue to produce when the caller does not override it."""
    import json

    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            record = json.loads(_lock_path(repo.common_dir()).read_text())
            assert record["operation"] == "run"


def test_run_session_resume_lock_operation_is_resume(tmp_path):
    """The lock record for a resume must say operation="resume", the
    other half of the default operation= must preserve.

    Uses _patched_session_env's DefaultSupervisor (a pass-through resume())
    since only the lock record is under test here; the real
    Supervisor.resume() codepath is already covered separately by
    test_resume_run_session_full_lifecycle."""
    import json

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

    import loop_supervisor.runtime as rt

    with _patched_session_env(repo):
        session = rt.resume_run_session(repo.root, run_id)
        with session:
            record = json.loads(_lock_path(repo.common_dir()).read_text())
            assert record["operation"] == "resume"


# --- operation= override (A1) -------------------------------------------
#
# operation= is orthogonal to _RunKind: it only labels the lock record,
# while _RunKind still drives run-id validation and which Supervisor
# construction path runs. The TUI does both new runs and resumes, so it
# needs operation="tui" independent of which kind of session it is.


def test_run_session_new_run_operation_override_labels_lock_as_tui(tmp_path):
    """A new run with operation="tui" writes operation="tui" to the lock
    record, and run_id stays None -- exactly the record app.py's own
    SupervisorLock construction produces today for a new TUI run
    (operation="tui", run_id=self._run_id which is None for a new run).
    This is the parity Phase B's RunSession adoption will depend on."""
    import json

    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo):
        session = rt.new_run_session(repo.root, _make_options(), operation="tui")
        with session:
            record = json.loads(_lock_path(repo.common_dir()).read_text())
            assert record["operation"] == "tui"
            assert record["run_id"] is None


def test_run_session_resume_operation_override_labels_lock_as_tui(tmp_path):
    """A resume with operation="tui" writes operation="tui" *and* still
    carries the real run_id -- proving operation= and _RunKind vary
    independently. If operation were driving run_id instead of _RunKind,
    this would regress to run_id=None."""
    import json

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

    import loop_supervisor.runtime as rt

    with _patched_session_env(repo):
        session = rt.resume_run_session(repo.root, run_id, operation="tui")
        with session:
            record = json.loads(_lock_path(repo.common_dir()).read_text())
            assert record["operation"] == "tui"
            assert record["run_id"] == run_id


def test_run_session_invalid_operation_fails_closed_without_writing_lock(tmp_path):
    """An operation outside SupervisorLock's fixed vocabulary must fail
    __enter__ with LockError, leave the session FAILED, and never write a
    lock file -- validation is delegated entirely to SupervisorLock, whose
    acquire() validates the prospective record before any filesystem
    write (locking.py's _validate_lock_record call ahead of
    _write_lock_file), so there is no window in which a malformed
    operation reaches disk."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import LockError, _lock_path

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo):
        session = rt.new_run_session(repo.root, _make_options(), operation="delete-everything")
        # match= pins that this is the metadata-rejection LockError, not
        # some other LockError (e.g. lock-already-held) that happens to
        # reach the same FAILED state through the same code path -- see
        # test_run_session_second_session_blocked_while_first_holds_lock.
        with pytest.raises(LockError, match="invalid operation"):
            session.__enter__()

        assert session.state is rt.SessionState.FAILED
        assert not _lock_path(repo.common_dir()).exists()


# --- stop_server() (A2) --------------------------------------------------
#
# stop_server() lets a caller force a bounded server.stop() attempt ahead
# of close(), without releasing the lock -- for breaking a blocked
# in-flight advance() call by tearing down its HTTP transport. Reuses the
# same _pending_cleanup handoff start_server() already relies on, so the
# bounded _CLEANUP_ATTEMPTS budget is spent once, not twice.
#
# Concurrency (calling this from another thread while advance() is
# in-flight, its actual intended use) is deliberately not tested here:
# RunSession is not thread-safe until A3, so a concurrency test now would
# pin pre-fix racy behavior and need rewriting the moment A3 lands --
# exactly the churn the A0 race test already accepted deliberately for a
# different race. Single-threaded contract only.


def test_stop_server_before_startup_is_a_noop(tmp_path):
    """Calling stop_server() before start_server() must not raise and must
    not touch the server (there isn't one yet)."""
    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo) as call_log:
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.stop_server()
            assert "server_stop" not in call_log


def test_stop_server_retains_the_lock(tmp_path):
    """stop_server() must never release the lock -- only close() does,
    per the module's single-owner contract. This is what makes it safe to
    call as an escalation ahead of the eventual close(): retaining the
    lock while the server is stopped is always ADR-0009-safe, unlike
    releasing it before a confirmed stop."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            session.stop_server()
            assert _lock_path(repo.common_dir()).exists()
            assert session.state is rt.SessionState.STARTED


def test_stop_server_confirmed_outcome_is_consumed_once_by_close(tmp_path):
    """A confirmed stop_server() must hand its outcome to close() via
    _pending_cleanup, so close() does not call server.stop() again --
    the same budget-spent-once handoff start_server() already relies on
    for its own failure path."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo) as call_log:
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            session.stop_server()
            assert call_log.count("server_stop") == 1
            session.close()

        assert call_log.count("server_stop") == 1
        assert session.state is rt.SessionState.CLOSED
        assert not _lock_path(repo.common_dir()).exists()


def test_stop_server_never_raises_on_failure(tmp_path, monkeypatch):
    """A server.stop() that always fails must not escape stop_server():
    the caller (a shutdown path) must be able to proceed to wait for the
    advance() worker regardless. The failure is not lost -- close()
    reports it via the usual CLEANUP_UNRESOLVED path afterwards."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path
    from loop_supervisor.runtime import RuntimeError_

    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)

    class AlwaysFailingServer:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            pass

        def stop(self):
            raise RuntimeError("stop fail")

        def add_observer(self, obs):
            pass

    with _patched_session_env(repo, server_cls=AlwaysFailingServer):
        session = rt.new_run_session(repo.root, _make_options())
        with pytest.raises(RuntimeError_, match="run completed but"):
            with session:
                session.start_server()
                session.stop_server()  # must not raise
                session.run_to_completion()

        assert session.state is rt.SessionState.CLEANUP_UNRESOLVED
        assert _lock_path(repo.common_dir()).exists()


def test_stop_server_is_idempotent(tmp_path):
    """Two consecutive stop_server() calls must not double-spend the
    cleanup budget or corrupt the pending-outcome handoff."""
    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo) as call_log:
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            session.stop_server()
            session.stop_server()
            assert call_log.count("server_stop") == 2
            session.close()

        assert call_log.count("server_stop") == 2
        assert session.state is rt.SessionState.CLOSED


def test_stop_server_does_not_clobber_a_confirmed_outcome_with_a_later_failure(tmp_path):
    """Once stop_server() has confirmed cleanup, a later (redundant)
    stop_server() call that fails must not overwrite that confirmed
    outcome -- otherwise close() would believe cleanup is unresolved for
    a session whose server has already been definitively stopped."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path

    stop_calls = [0]

    class FlakyAfterFirstStop:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            pass

        def stop(self):
            stop_calls[0] += 1
            if stop_calls[0] > 1:
                raise RuntimeError("stop fail on second call")

        def add_observer(self, obs):
            pass

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo, server_cls=FlakyAfterFirstStop):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            session.stop_server()  # confirms (stop_calls -> 1)
            session.stop_server()  # fails every retry (stop_calls -> 4)
            session.close()

        assert session.state is rt.SessionState.CLOSED
        assert not _lock_path(repo.common_dir()).exists()


def _blocking_advance_supervisor_cls(release_event, entered_advance_event):
    """Build a supervisor class whose advance() signals entry, then
    blocks until the test releases it -- giving a test a deterministic
    window in which to run a concurrent close()."""

    class BlockingAdvanceSupervisor:
        def __init__(self, *a, **kw):
            self._runner = None

        def start_new_run(self):
            state = MagicMock()
            state.run_id = "fake-run"
            state.phase = "planning"
            state.options = _make_options()
            return state

        def advance(self, state):
            from loop_supervisor.supervisor import AdvanceOutcome, AdvanceStatus

            entered_advance_event.set()
            release_event.wait(timeout=5)
            state.phase = "building"
            return AdvanceOutcome(
                status=AdvanceStatus.ADVANCED,
                state=state,
                phase_before="planning",
                phase_after="building",
            )

        @property
        def runner(self):
            return self._runner

        @runner.setter
        def runner(self, value):
            self._runner = value

    return BlockingAdvanceSupervisor


def test_characterize_single_threaded_advance_does_not_race_close(tmp_path):
    """Baseline (single-threaded), not a concurrency test: pins that a
    *successful* advance() leaves the session STARTED, and the ordinary
    non-concurrent sequence then reaches CLOSED with the lock released.

    This gap was otherwise unpinned: test_run_session_state_progression
    only covers STARTED after run_to_completion(), and the only other
    "STARTED after advance()" assertion in this file
    (test_run_session_advance_returns_to_started_on_failure) is on the
    *failing* advance() path, which restores STARTED via a different
    branch (runtime.py's except clause) than the success path this test
    exercises. Serves as the reference point for the actual race test
    below, which asserts the opposite outcome under concurrency."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            session.advance()
            assert session.state is rt.SessionState.STARTED

    assert session.state is rt.SessionState.CLOSED
    assert not _lock_path(repo.common_dir()).exists()


def test_concurrent_close_during_advance_waits_and_does_not_clobber_state(tmp_path):
    """The fixed ordering for the two races A0 reproduced.

    Replaces test_characterize_concurrent_close_during_advance_leaves_
    state_started, which deliberately pinned the pre-fix behavior and
    whose docstring named both assertions expected to flip here:

      Race 1 -- advance() unconditionally restored STARTED on return,
      clobbering a terminal state a concurrent close() had already
      written, resurrecting a lock-released session into something that
      looked live and advanceable. Now advance() only returns to STARTED
      if it is still the session's current activity.

      Race 2 -- close() released the lock while advance() was still in
      flight and could still be mutating Git/state. Now close() waits on
      the quiescence barrier first, so the release is strictly ordered
      after advance() returns.

    Both are asserted against a deterministic, Event-gated advance() so
    the ordering is real rather than timing-dependent: the lock is
    checked to still exist *while* advance() is provably blocked, and
    only then is advance() released.
    """
    import threading

    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")
    release_event = threading.Event()
    entered_advance_event = threading.Event()

    supervisor_cls = _blocking_advance_supervisor_cls(release_event, entered_advance_event)
    with _patched_session_env(repo, supervisor_cls=supervisor_cls):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()

            advance_result: list[object] = []
            advance_error: list[BaseException] = []

            def _run_advance() -> None:
                try:
                    advance_result.append(session.advance())
                except BaseException as exc:  # noqa: BLE001 - captured for the assert below
                    advance_error.append(exc)

            close_returned = threading.Event()
            close_error: list[BaseException] = []

            def _run_close() -> None:
                try:
                    session.close()
                except BaseException as exc:  # noqa: BLE001 - captured for the assert below
                    close_error.append(exc)
                finally:
                    close_returned.set()

            advance_thread = threading.Thread(target=_run_advance)
            advance_thread.start()
            close_thread = threading.Thread(target=_run_close)
            try:
                assert entered_advance_event.wait(timeout=5), "advance() never entered"

                # advance() is now blocked inside the supervisor call.
                # Start a close() that must NOT complete while it is.
                close_thread.start()

                # Race 2: close() must still be waiting, and crucially the
                # lock must still be on disk. A bounded negative wait is
                # the right shape here -- if close() returns while
                # advance() is provably still blocked, the invariant is
                # broken no matter how long we waited.
                assert not close_returned.wait(timeout=1.0), (
                    "close() returned while advance() was still in flight; the lock "
                    "may have been released with a transition still able to mutate "
                    "Git/state"
                )
                assert _lock_path(repo.common_dir()).exists()
            finally:
                # Always unblock and join both workers, even if an
                # assertion above failed, so a broken assumption degrades
                # to a normal test failure rather than leaving threads
                # running past this test's lifetime.
                release_event.set()
                advance_thread.join(timeout=5)
                if close_thread.is_alive() or close_thread.ident is not None:
                    close_thread.join(timeout=5)

            assert not advance_thread.is_alive()
            assert not close_thread.is_alive()
            assert advance_error == [], advance_error
            assert close_error == [], close_error
            assert len(advance_result) == 1

            # Race 1: advance() returned after close() had gone terminal,
            # and must NOT have resurrected STARTED. Asserted inside the
            # `with` block, before __exit__ runs a second close() that
            # would reach CLOSED again and mask a clobber.
            assert session.state is rt.SessionState.CLOSED
            # And the release did happen -- once advance() finished.
            assert not _lock_path(repo.common_dir()).exists()


# --- thread-safety and cleanup-retry semantics (A3) ---------------------


def test_advance_does_not_resurrect_a_closed_session(tmp_path):
    """Focused unit form of Race 1, without the timing setup: a session
    already driven terminal must stay terminal when an advance() that was
    in flight finishes. Complements the concurrency test above by pinning
    the guard itself (_restore_started_unless_closed) rather than the
    scenario that motivated it."""
    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            session.close()
            assert session.state is rt.SessionState.CLOSED

            # Simulate the tail of an advance() that was already in flight
            # when close() ran: it must not write STARTED back.
            session._restore_started_unless_closed()
            assert session.state is rt.SessionState.CLOSED


def test_advance_does_not_write_run_state_after_close(tmp_path):
    """A3.2: the run_state analogue of
    test_advance_does_not_resurrect_a_closed_session.

    advance()'s SessionState restore was guarded from the start (A3), but
    its run_state write-back at the same call site was not -- it wrote
    self._run_state = outcome.state unconditionally, even though
    run_to_completion() gained the equivalent guard
    (_store_run_state_unless_closed) in A3.1. Reproduced against the
    unguarded code: a session already driven CLOSED still had a stale
    advance() write land in its run_state after the fact. Calls the
    actual method under test, the same way the SessionState test above
    calls _restore_started_unless_closed() directly, rather than
    re-implementing the guard's condition inline."""
    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            run_state_before = session.run_state
            session.close()
            assert session.state is rt.SessionState.CLOSED

            sentinel = MagicMock()
            sentinel.run_id = "should-not-be-written"
            with session._state_lock:
                session._store_run_state_unless_closed(sentinel)
            assert session.run_state is run_state_before
            assert session.state is rt.SessionState.CLOSED


def test_concurrent_close_during_advance_does_not_leak_run_state(tmp_path):
    """A3.2's end-to-end companion to test_advance_does_not_write_run_
    state_after_close: the real race, not just the guard in isolation.

    Reproduced against the pre-fix code: with the SessionState restore
    guarded (A3) but the run_state write unguarded, a close() that
    completes while advance() is still blocked would still see its
    result land in session.run_state once advance() finally returned --
    even though the session was already CLOSED and the lock already
    released. advance()'s outcome() returns a distinct sentinel state
    object (unlike _blocking_advance_supervisor_cls's shared-state
    helper) specifically so a leaked write is unambiguous: run_state
    identity must be exactly what it was before advance() ever ran.
    """
    import threading

    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path

    entered_advance_event = threading.Event()
    release_event = threading.Event()

    class LeakCheckSupervisor:
        def __init__(self, *a, **kw):
            self._runner = None

        def start_new_run(self):
            state = MagicMock()
            state.run_id = "original"
            state.phase = "planning"
            state.options = _make_options()
            return state

        def advance(self, state):
            from loop_supervisor.supervisor import AdvanceOutcome, AdvanceStatus

            entered_advance_event.set()
            release_event.wait(timeout=5)
            sentinel = MagicMock()
            sentinel.run_id = "leaked-after-close"
            sentinel.phase = "building"
            return AdvanceOutcome(
                status=AdvanceStatus.ADVANCED,
                state=sentinel,
                phase_before="planning",
                phase_after="building",
            )

        @property
        def runner(self):
            return self._runner

        @runner.setter
        def runner(self, value):
            self._runner = value

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo, supervisor_cls=LeakCheckSupervisor):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            run_state_before = session.run_state

            advance_thread = threading.Thread(target=session.advance)
            advance_thread.start()

            close_returned = threading.Event()

            def _run_close() -> None:
                session.close()
                close_returned.set()

            close_thread = threading.Thread(target=_run_close)
            try:
                assert entered_advance_event.wait(timeout=5), "advance() never entered"

                close_thread.start()

                # close() must block behind the quiescence barrier (A3):
                # it must NOT return while advance() is still in flight.
                # This is what makes the window below real rather than
                # incidental -- without it, close() could complete before
                # advance() ever finishes, and the assertion after
                # release_event.set() would pass for the wrong reason (as
                # a synchronous, non-threaded session.close() call did in
                # an earlier version of this test, which passed even
                # against the unguarded code because it never actually
                # observed the in-flight window).
                assert not close_returned.wait(timeout=1.0), (
                    "close() returned while advance() was still in flight"
                )
                assert _lock_path(repo.common_dir()).exists()
            finally:
                release_event.set()
                advance_thread.join(timeout=5)
                close_thread.join(timeout=5)

            assert not advance_thread.is_alive()
            assert not close_thread.is_alive()
            assert session.state is rt.SessionState.CLOSED
            assert not _lock_path(repo.common_dir()).exists()
            # The assertion that actually distinguishes fixed from
            # unfixed: after advance() has returned (post-release, post-
            # close), run_state must still be exactly what it was before
            # advance() ever ran -- not the sentinel advance() tried to
            # write back into a session that no longer owned it.
            assert session.run_state is run_state_before


def test_raising_advance_still_releases_the_quiescence_barrier(tmp_path):
    """A failing advance() must set _advance_done in its finally, or a
    close() waiting on the barrier would block forever. Without the
    finally this test hangs rather than fails, so close() is run with a
    hard timeout on a worker thread."""
    import threading

    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")

    class BoomAdvanceSupervisor:
        def __init__(self, *a, **kw):
            self._runner = None

        def start_new_run(self):
            state = MagicMock()
            state.run_id = "fake-run"
            state.phase = "planning"
            state.options = _make_options()
            return state

        def advance(self, state):
            raise RuntimeError("advance boom")

        @property
        def runner(self):
            return self._runner

        @runner.setter
        def runner(self, value):
            self._runner = value

    with _patched_session_env(repo, supervisor_cls=BoomAdvanceSupervisor):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            with pytest.raises(RuntimeError, match="advance boom"):
                session.advance()

            barrier_released = session._advance_done.is_set()
            if not barrier_released:
                # Release it by hand before asserting. Otherwise the
                # AssertionError below would unwind through __exit__ ->
                # close() -> _advance_done.wait(), which blocks forever on
                # the very defect being reported -- turning a clean test
                # failure into a hung suite.
                session._advance_done.set()
            assert barrier_released, (
                "a raising advance() left the quiescence barrier clear; any close() "
                "would now block forever"
            )

            done = threading.Event()

            def _close() -> None:
                session.close()
                done.set()

            t = threading.Thread(target=_close)
            t.start()
            assert done.wait(timeout=5), "close() blocked after a raising advance()"
            t.join(timeout=5)

        assert session.state is rt.SessionState.CLOSED
        assert not _lock_path(repo.common_dir()).exists()


def test_stop_server_during_blocked_advance_does_not_deadlock(tmp_path):
    """The scenario stop_server() exists for, and the direct check that
    _state_lock is not over-scoped: a shutdown thread must be able to
    stop the server while advance() is blocked. If _state_lock were held
    across supervisor.advance(), this would deadlock -- so the call is
    made on a worker thread with a hard timeout, making a regression a
    test failure rather than a hung suite."""
    import threading

    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")
    release_event = threading.Event()
    entered_advance_event = threading.Event()

    supervisor_cls = _blocking_advance_supervisor_cls(release_event, entered_advance_event)
    with _patched_session_env(repo, supervisor_cls=supervisor_cls) as call_log:
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()

            advance_thread = threading.Thread(target=session.advance)
            advance_thread.start()
            try:
                assert entered_advance_event.wait(timeout=5), "advance() never entered"

                stopped = threading.Event()

                def _stop() -> None:
                    session.stop_server()
                    stopped.set()

                stop_thread = threading.Thread(target=_stop)
                stop_thread.start()
                assert stopped.wait(timeout=5), (
                    "stop_server() blocked while advance() was in flight -- _state_lock "
                    "is held across supervisor.advance(), which deadlocks the exact "
                    "escalation stop_server() exists to perform"
                )
                stop_thread.join(timeout=5)
                assert "server_stop" in call_log
            finally:
                release_event.set()
                advance_thread.join(timeout=5)

            assert not advance_thread.is_alive()


def test_unconfirmed_stop_server_is_retried_by_close(tmp_path, monkeypatch):
    """An unconfirmed stop_server() must NOT make the following close()
    skip its own attempt.

    stop_server() is typically called precisely because a transition was
    still blocked; the retry that matters happens after that transition
    unwinds, when stop() is no longer racing a live HTTP transport.
    Consuming the stale failure instead made close() call stop() zero
    times and report the session unresolved on the strength of an attempt
    already known to be obsolete."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    stop_calls = [0]
    fail_until = [rt._CLEANUP_ATTEMPTS]

    class RecoveringServer:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            pass

        def stop(self):
            stop_calls[0] += 1
            if stop_calls[0] <= fail_until[0]:
                raise RuntimeError("stop fail")

        def add_observer(self, obs):
            pass

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo, server_cls=RecoveringServer):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            session.stop_server()
            assert stop_calls[0] == rt._CLEANUP_ATTEMPTS
            # The blocking condition clears, exactly as it would once a
            # stuck advance() unwinds.
            fail_until[0] = 0
            session.close()

        assert stop_calls[0] == rt._CLEANUP_ATTEMPTS + 1, (
            "close() did not retry stop() after an unconfirmed stop_server(); it "
            "consumed the stale failed outcome instead"
        )
        assert session.state is rt.SessionState.CLOSED
        assert not _lock_path(repo.common_dir()).exists()


def test_confirmed_stop_server_is_still_not_retried_by_close(tmp_path):
    """The other side of the retry rule: a *confirmed* stop_server()
    outcome is still consumed as-is, so the budget-spent-once handoff A2
    established is not lost to the fix above."""
    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo) as call_log:
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            session.stop_server()
            session.close()

        assert call_log.count("server_stop") == 1
        assert session.state is rt.SessionState.CLOSED


def test_startup_failure_handoff_is_still_not_retried(tmp_path, monkeypatch):
    """start_server()'s handoff must remain non-retryable even though
    stop_server()'s is retryable: the close() that consumes it runs
    immediately afterwards via __exit__, so retrying there would spend
    the documented _CLEANUP_ATTEMPTS budget twice for one failure
    sequence.

    This is the distinction the retryable flag encodes; an earlier
    version of the fix keyed the decision off confirmed-ness alone and
    doubled the startup budget to 6, which the pre-existing
    test_characterize_startup_failure_stop_attempts_are_bounded caught."""
    import loop_supervisor.runtime as rt
    from loop_supervisor.opencode import ServerStartupError
    from loop_supervisor.runtime import RuntimeError_

    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    stop_calls = [0]

    class FailingStartServer:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            raise ServerStartupError("simulated startup failure")

        def stop(self):
            stop_calls[0] += 1
            raise RuntimeError("stop fail")

        def add_observer(self, obs):
            pass

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo, server_cls=FailingStartServer):
        session = rt.new_run_session(repo.root, _make_options())
        with pytest.raises(RuntimeError_):
            with session:
                session.start_server()

    assert stop_calls[0] == rt._CLEANUP_ATTEMPTS


# --- run_to_completion() thread-safety (A3.1) ----------------------------
#
# A3 gave advance() a state guard and a quiescence barrier, but
# run_to_completion() has its own separate code path -- Supervisor.run()
# loops calling *its own* advance() internally, never going through
# RunSession.advance() -- so it inherited none of that protection. The
# module's concurrency contract already claimed run_to_completion() was
# covered; it was not. These tests close that gap the same way A3 closed
# advance()'s.


def _blocking_run_supervisor_cls(release_event, entered_run_event):
    """Build a supervisor class whose run() signals entry, then blocks
    until the test releases it -- the run_to_completion() analogue of
    _blocking_advance_supervisor_cls."""

    class BlockingRunSupervisor:
        def __init__(self, *a, **kw):
            self._runner = None

        def start_new_run(self):
            state = MagicMock()
            state.run_id = "fake-run"
            state.phase = "planning"
            state.options = _make_options()
            return state

        def run(self, state, *, max_steps=None):
            entered_run_event.set()
            release_event.wait(timeout=5)
            state.phase = "done"
            return state

        @property
        def runner(self):
            return self._runner

        @runner.setter
        def runner(self, value):
            self._runner = value

    return BlockingRunSupervisor


def test_run_to_completion_clears_the_quiescence_barrier(tmp_path):
    """Contract test for the gap itself: the barrier must be clear while
    run_to_completion() is in flight, exactly as it is during advance().
    This is the assertion whose absence let the gap through -- all of
    A3's tests exercised advance(), none exercised run_to_completion()."""
    import threading

    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")
    release_event = threading.Event()
    entered_run_event = threading.Event()

    supervisor_cls = _blocking_run_supervisor_cls(release_event, entered_run_event)
    with _patched_session_env(repo, supervisor_cls=supervisor_cls):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()

            run_thread = threading.Thread(target=session.run_to_completion)
            run_thread.start()
            try:
                assert entered_run_event.wait(timeout=5), "run_to_completion() never entered"
                assert not session._advance_done.is_set(), (
                    "the quiescence barrier was not cleared during run_to_completion(); "
                    "a concurrent close() would not wait for it"
                )
            finally:
                release_event.set()
                run_thread.join(timeout=5)

            assert not run_thread.is_alive()
            assert session._advance_done.is_set()


def test_raising_run_to_completion_still_releases_the_quiescence_barrier(tmp_path):
    """The run_to_completion() analogue of
    test_raising_advance_still_releases_the_quiescence_barrier: a failing
    run_to_completion() must set _advance_done in its finally, or a
    close() waiting on the barrier would block forever. Without the
    finally this test hangs rather than fails, so close() is run with a
    hard timeout on a worker thread, and the barrier is released by hand
    before asserting so a failed assertion does not itself hang inside
    __exit__ -> close() -> _advance_done.wait()."""
    import threading

    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")

    class BoomRunSupervisor:
        def __init__(self, *a, **kw):
            self._runner = None

        def start_new_run(self):
            state = MagicMock()
            state.run_id = "fake-run"
            state.phase = "planning"
            state.options = _make_options()
            return state

        def run(self, state, *, max_steps=None):
            raise RuntimeError("run boom")

        @property
        def runner(self):
            return self._runner

        @runner.setter
        def runner(self, value):
            self._runner = value

    with _patched_session_env(repo, supervisor_cls=BoomRunSupervisor):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            with pytest.raises(RuntimeError, match="run boom"):
                session.run_to_completion()

            barrier_released = session._advance_done.is_set()
            if not barrier_released:
                session._advance_done.set()
            assert barrier_released, (
                "a raising run_to_completion() left the quiescence barrier clear; any "
                "close() would now block forever"
            )

            done = threading.Event()

            def _close() -> None:
                session.close()
                done.set()

            t = threading.Thread(target=_close)
            t.start()
            assert done.wait(timeout=5), "close() blocked after a raising run_to_completion()"
            t.join(timeout=5)

        assert session.state is rt.SessionState.CLOSED
        assert not _lock_path(repo.common_dir()).exists()


def test_concurrent_close_during_run_to_completion_waits(tmp_path):
    """The run_to_completion() analogue of
    test_concurrent_close_during_advance_waits_and_does_not_clobber_state:
    close() must not release the lock while a run_to_completion() call is
    still in flight and could still be mutating Git/state."""
    import threading

    import loop_supervisor.runtime as rt
    from loop_supervisor.locking import _lock_path

    repo = _init_repo(tmp_path / "repo")
    release_event = threading.Event()
    entered_run_event = threading.Event()

    supervisor_cls = _blocking_run_supervisor_cls(release_event, entered_run_event)
    with _patched_session_env(repo, supervisor_cls=supervisor_cls):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()

            run_result: list[object] = []
            run_error: list[BaseException] = []

            def _run_run() -> None:
                try:
                    run_result.append(session.run_to_completion())
                except BaseException as exc:  # noqa: BLE001 - captured for the assert below
                    run_error.append(exc)

            close_returned = threading.Event()
            close_error: list[BaseException] = []

            def _run_close() -> None:
                try:
                    session.close()
                except BaseException as exc:  # noqa: BLE001 - captured for the assert below
                    close_error.append(exc)
                finally:
                    close_returned.set()

            run_thread = threading.Thread(target=_run_run)
            run_thread.start()
            close_thread = threading.Thread(target=_run_close)
            try:
                assert entered_run_event.wait(timeout=5), "run_to_completion() never entered"

                close_thread.start()

                assert not close_returned.wait(timeout=1.0), (
                    "close() returned while run_to_completion() was still in flight; the "
                    "lock may have been released with a transition still able to mutate "
                    "Git/state"
                )
                assert _lock_path(repo.common_dir()).exists()
            finally:
                release_event.set()
                run_thread.join(timeout=5)
                if close_thread.is_alive() or close_thread.ident is not None:
                    close_thread.join(timeout=5)

            assert not run_thread.is_alive()
            assert not close_thread.is_alive()
            assert run_error == [], run_error
            assert close_error == [], close_error
            assert len(run_result) == 1

            assert session.state is rt.SessionState.CLOSED
            assert not _lock_path(repo.common_dir()).exists()


def test_run_to_completion_does_not_write_run_state_after_close(tmp_path):
    """Focused unit form of the run_state write's terminal-aware guard,
    without the timing setup: a session already driven terminal must not
    have its run_state overwritten by the tail of a run_to_completion()
    that was already in flight when close() ran.

    Complements the concurrency test above by pinning the guard itself
    (_store_run_state_unless_closed) rather than the scenario that
    motivated it -- calling the actual method under test, the same way
    test_advance_does_not_resurrect_a_closed_session calls
    _restore_started_unless_closed() directly, rather than
    re-implementing the ``if self._state_is(...)`` check inline. An
    inline reimplementation would pass even if the guard inside
    run_to_completion() itself were missing or wrong, since it would
    never call the method it means to be verifying."""
    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")
    with _patched_session_env(repo):
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()
            run_state_before = session.run_state
            session.close()
            assert session.state is rt.SessionState.CLOSED

            sentinel = MagicMock()
            sentinel.run_id = "should-not-be-written"
            with session._state_lock:
                session._store_run_state_unless_closed(sentinel)
            assert session.run_state is run_state_before
            assert session.state is rt.SessionState.CLOSED


def test_stop_server_during_blocked_run_to_completion_does_not_deadlock(tmp_path):
    """The run_to_completion() analogue of
    test_stop_server_during_blocked_advance_does_not_deadlock: a shutdown
    thread must be able to stop the server while run_to_completion() is
    blocked. If _state_lock were held across Supervisor.run(), this would
    deadlock -- so the call is made on a worker thread with a hard
    timeout, making a regression a test failure rather than a hung
    suite."""
    import threading

    import loop_supervisor.runtime as rt

    repo = _init_repo(tmp_path / "repo")
    release_event = threading.Event()
    entered_run_event = threading.Event()

    supervisor_cls = _blocking_run_supervisor_cls(release_event, entered_run_event)
    with _patched_session_env(repo, supervisor_cls=supervisor_cls) as call_log:
        session = rt.new_run_session(repo.root, _make_options())
        with session:
            session.start_server()

            run_thread = threading.Thread(target=session.run_to_completion)
            run_thread.start()
            try:
                assert entered_run_event.wait(timeout=5), "run_to_completion() never entered"

                stopped = threading.Event()

                def _stop() -> None:
                    session.stop_server()
                    stopped.set()

                stop_thread = threading.Thread(target=_stop)
                stop_thread.start()
                assert stopped.wait(timeout=5), (
                    "stop_server() blocked while run_to_completion() was in flight -- "
                    "_state_lock is held across Supervisor.run(), which deadlocks the "
                    "exact escalation stop_server() exists to perform"
                )
                stop_thread.join(timeout=5)
                assert "server_stop" in call_log
            finally:
                release_event.set()
                run_thread.join(timeout=5)

            assert not run_thread.is_alive()


# --- PermissionDenier wiring (headless permission-ask hang, backlog #27) ---


def _fake_denier_class(call_log: list[str], *, denied_count: int = 0, denied_summary=None):
    """A stand-in for permissions.PermissionDenier that records its own
    lifecycle calls in `call_log` without any real HTTP/SSE activity."""

    class FakeDenier:
        def __init__(self, base_url: str) -> None:
            call_log.append(f"denier_init:{base_url}")
            self._denied_count = denied_count
            self._denied_summary = list(denied_summary or [])

        def start(self) -> None:
            call_log.append("denier_start")

        def stop(self) -> None:
            call_log.append("denier_stop")

        @property
        def denied_count(self) -> int:
            return self._denied_count

        @property
        def denied_summary(self) -> list[str]:
            return list(self._denied_summary)

    return FakeDenier


def test_start_server_starts_permission_denier_with_server_base_url(tmp_path):
    """start_server() must construct and start a PermissionDenier against
    the server's own base_url once it is known -- this is the sole seam
    that closes the headless permission.asked hang (backlog #27); SSE is
    otherwise TUI-only."""
    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    call_log: list[str] = []
    fake_server = _FakeServer(call_log)

    class FakeOCServer:
        def __init__(self, *a, **kw):
            pass

        def __new__(cls, *a, **kw):
            return fake_server

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", FakeOCServer)
    mp.setattr(rt, "PermissionDenier", _fake_denier_class(call_log))
    try:
        session = rt.new_run_session(tmp_path / "repo", _make_options())
        with session:
            session.start_server()
            assert f"denier_init:{fake_server.base_url}" in call_log
            assert "denier_start" in call_log
    finally:
        mp.undo()

    assert "denier_stop" in call_log
    assert call_log.index("denier_start") < call_log.index("denier_stop")


def test_close_stops_permission_denier_before_server(tmp_path):
    """The denier's SSE subscription must be torn down before the
    server itself is stopped, not after: stopping the server first
    would leave the denier's SSE client trying to read from a socket
    the server has already closed."""
    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    call_log: list[str] = []
    fake_server = _FakeServer(call_log)

    class FakeOCServer:
        def __init__(self, *a, **kw):
            pass

        def __new__(cls, *a, **kw):
            return fake_server

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", FakeOCServer)
    mp.setattr(rt, "PermissionDenier", _fake_denier_class(call_log))
    try:
        session = rt.new_run_session(tmp_path / "repo", _make_options())
        with session:
            session.start_server()
    finally:
        mp.undo()

    assert call_log.index("denier_stop") < call_log.index("server_stop")


def test_denied_permission_count_and_summary_readable_after_close(tmp_path):
    """denied_permission_count/_summary must remain readable from the
    session after the `with` block (and thus close()) has already run,
    since callers inspect them after run_to_completion() returns and
    the `with` exits -- close() must snapshot before tearing the denier
    down."""
    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    call_log: list[str] = []
    fake_server = _FakeServer(call_log)

    class FakeOCServer:
        def __init__(self, *a, **kw):
            pass

        def __new__(cls, *a, **kw):
            return fake_server

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", FakeOCServer)
    mp.setattr(
        rt,
        "PermissionDenier",
        _fake_denier_class(call_log, denied_count=2, denied_summary=["bash", "edit"]),
    )
    session = rt.new_run_session(tmp_path / "repo", _make_options())
    try:
        with session:
            session.start_server()
            assert session.denied_permission_count == 2
            assert session.denied_permission_summary == ["bash", "edit"]
    finally:
        mp.undo()

    assert session.denied_permission_count == 2
    assert session.denied_permission_summary == ["bash", "edit"]


def test_denied_permission_count_zero_before_start_server(tmp_path):
    """Before start_server() has ever run (no denier constructed yet),
    denied_permission_count/_summary must report empty defaults rather
    than raising."""
    import loop_supervisor.runtime as rt

    _init_repo(tmp_path / "repo")
    session = rt.new_run_session(tmp_path / "repo", _make_options())
    assert session.denied_permission_count == 0
    assert session.denied_permission_summary == []


def test_run_new_prints_denial_diagnostic_to_stderr(tmp_path, capsys):
    """run_new() must report a nonzero denial count/summary on stderr
    after the run completes -- the CLI-facing half of backlog #27 (the
    headless path previously had no diagnostic at all for a stall
    caused by permission.asked)."""
    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    call_log: list[str] = []

    class FakeOCServer:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            pass

        def stop(self):
            pass

        def add_observer(self, obs):
            pass

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", FakeOCServer)
    mp.setattr(
        rt,
        "PermissionDenier",
        _fake_denier_class(call_log, denied_count=3, denied_summary=["bash"]),
    )
    try:
        run_new(tmp_path / "repo", _make_options(max_accepted_tasks=0), max_steps=1)
    finally:
        mp.undo()

    captured = capsys.readouterr()
    assert "denied 3 permission request(s)" in captured.err
    assert "bash" in captured.err


def test_run_new_prints_nothing_when_no_permissions_were_denied(tmp_path, capsys):
    """When the denier never sees a permission.asked event, run_new()
    must not print a denial line at all."""
    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    call_log: list[str] = []

    class FakeOCServer:
        def __init__(self, *a, **kw):
            self.base_url = "http://127.0.0.1:9999"

        def start(self):
            pass

        def stop(self):
            pass

        def add_observer(self, obs):
            pass

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", FakeOCServer)
    mp.setattr(rt, "PermissionDenier", _fake_denier_class(call_log))
    try:
        run_new(tmp_path / "repo", _make_options(max_accepted_tasks=0), max_steps=1)
    finally:
        mp.undo()

    captured = capsys.readouterr()
    assert "denied" not in captured.err


def test_denier_start_failure_does_not_fail_start_server(tmp_path, capsys):
    """A PermissionDenier that raises from start() must not prevent
    start_server() from succeeding -- a denier fault must never fail an
    otherwise-healthy run, matching sse.py's own non-fatal contract. But
    the failure must still be visible on stderr: a silently-swallowed
    denier-start failure is indistinguishable from a healthy denier that
    simply saw no asks, which is precisely the ambiguity that made an
    early live verification run inconclusive."""
    _init_repo(tmp_path / "repo")
    import loop_supervisor.runtime as rt

    call_log: list[str] = []
    fake_server = _FakeServer(call_log)

    class FakeOCServer:
        def __init__(self, *a, **kw):
            pass

        def __new__(cls, *a, **kw):
            return fake_server

    class BoomDenier:
        def __init__(self, base_url: str) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("denier boom")

        def stop(self) -> None:
            pass

    mp = pytest.MonkeyPatch()
    mp.setattr(rt, "OpenCodeServer", FakeOCServer)
    mp.setattr(rt, "PermissionDenier", BoomDenier)
    try:
        session = rt.new_run_session(tmp_path / "repo", _make_options())
        with session:
            session.start_server()
            assert session.state == rt.SessionState.STARTED
            assert session.denied_permission_count == 0
    finally:
        mp.undo()

    captured = capsys.readouterr()
    assert "permission denier failed to start" in captured.err
    assert "denier boom" in captured.err
