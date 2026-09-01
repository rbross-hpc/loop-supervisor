"""Tests for src/loop_supervisor/locking.py"""

import json
import multiprocessing
import os
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

import loop_supervisor.locking as locking_mod
from loop_supervisor.locking import (
    LockError,
    MalformedLockError,
    RemoteLockError,
    StaleLockError,
    SupervisorLock,
    _guard_path,
    _lock_path,
    _pid_is_alive,
)


def _make_lock(tmp_path: Path, **kwargs) -> SupervisorLock:
    return SupervisorLock(
        tmp_path,
        operation=kwargs.get("operation", "run"),
        run_id=kwargs.get("run_id"),
        integration_path=kwargs.get("integration_path", str(tmp_path)),
        recover_stale=kwargs.get("recover_stale", False),
    )


def test_lock_path_is_under_common_dir(tmp_path):
    path = _lock_path(tmp_path)
    assert str(path).startswith(str(tmp_path))
    assert path.name == "supervisor.lock"


def test_acquire_creates_lock_file(tmp_path):
    lock = _make_lock(tmp_path)
    lock.acquire()
    try:
        assert _lock_path(tmp_path).exists()
    finally:
        lock.release()


def test_lock_file_mode_is_0600(tmp_path):
    lock = _make_lock(tmp_path)
    lock.acquire()
    try:
        mode = oct(_lock_path(tmp_path).stat().st_mode)[-3:]
        assert mode == "600"
    finally:
        lock.release()


def test_lock_file_mode_is_0600_under_restrictive_umask(tmp_path):
    (tmp_path / "loop-supervisor").mkdir(mode=0o700)
    previous_umask = os.umask(0o777)
    lock = _make_lock(tmp_path)
    try:
        lock.acquire()
        assert _lock_path(tmp_path).stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(previous_umask)
        lock.release()


def test_lock_file_contains_required_fields(tmp_path):
    lock = _make_lock(tmp_path, run_id="abc123", integration_path="/repo")
    lock.acquire()
    try:
        data = json.loads(_lock_path(tmp_path).read_text())
        assert data["schema_version"] == 1
        assert isinstance(data["token"], str) and data["token"]
        assert data["pid"] == os.getpid()
        assert data["hostname"] == socket.gethostname()
        assert data["operation"] == "run"
        assert data["run_id"] == "abc123"
        assert data["integration_path"] == "/repo"
    finally:
        lock.release()


def test_release_removes_lock_file(tmp_path):
    lock = _make_lock(tmp_path)
    lock.acquire()
    lock.release()
    assert not _lock_path(tmp_path).exists()


def test_context_manager_releases_on_exit(tmp_path):
    with _make_lock(tmp_path):
        assert _lock_path(tmp_path).exists()
    assert not _lock_path(tmp_path).exists()


def test_context_manager_releases_on_exception(tmp_path):
    with pytest.raises(RuntimeError):
        with _make_lock(tmp_path):
            raise RuntimeError("test")
    assert not _lock_path(tmp_path).exists()


def test_concurrent_acquisition_only_one_wins(tmp_path):
    """While one thread holds the lock, all others must fail."""
    holder_ready = threading.Event()
    holder_release = threading.Event()
    results: list[bool] = []
    errors: list[Exception] = []

    def holder():
        lock = _make_lock(tmp_path)
        lock.acquire()
        holder_ready.set()
        holder_release.wait(timeout=5)
        lock.release()

    def try_acquire():
        holder_ready.wait(timeout=5)
        lock = _make_lock(tmp_path)
        try:
            lock.acquire()
            results.append(True)
            lock.release()
        except LockError:
            results.append(False)
        except Exception as exc:
            errors.append(exc)

    h = threading.Thread(target=holder)
    h.start()
    contenders = [threading.Thread(target=try_acquire) for _ in range(4)]
    for t in contenders:
        t.start()
    for t in contenders:
        t.join()
    holder_release.set()
    h.join()

    assert not errors
    assert results.count(True) == 0


def test_live_local_owner_rejected(tmp_path):
    lock1 = _make_lock(tmp_path)
    lock1.acquire()
    try:
        lock2 = _make_lock(tmp_path)
        with pytest.raises(LockError):
            lock2.acquire()
    finally:
        lock1.release()


def test_live_local_owner_cannot_be_force_recovered(tmp_path):
    lock1 = _make_lock(tmp_path)
    lock1.acquire()
    try:
        lock2 = _make_lock(tmp_path, recover_stale=True)
        with pytest.raises(LockError):
            lock2.acquire()
    finally:
        lock1.release()


def test_dead_local_owner_rejected_without_flag(tmp_path):
    dead_pid = _get_dead_pid()
    _write_lock_record(
        tmp_path,
        pid=dead_pid,
        hostname=socket.gethostname(),
        token="tok1",
    )
    lock = _make_lock(tmp_path, recover_stale=False)
    with pytest.raises(StaleLockError):
        lock.acquire()


def test_dead_local_owner_recovered_with_flag(tmp_path):
    dead_pid = _get_dead_pid()
    _write_lock_record(
        tmp_path,
        pid=dead_pid,
        hostname=socket.gethostname(),
        token="tok1",
    )
    lock = _make_lock(tmp_path, recover_stale=True)
    lock.acquire()
    try:
        data = json.loads(_lock_path(tmp_path).read_text())
        assert data["pid"] == os.getpid()
    finally:
        lock.release()


def test_remote_host_lock_never_recovered(tmp_path):
    _write_lock_record(
        tmp_path,
        pid=12345,
        hostname="other-host",
        token="tok2",
    )
    lock = _make_lock(tmp_path, recover_stale=True)
    with pytest.raises(RemoteLockError):
        lock.acquire()


def test_malformed_lock_never_recovered(tmp_path):
    lock_path = _lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("not json")
    lock = _make_lock(tmp_path, recover_stale=True)
    with pytest.raises(MalformedLockError):
        lock.acquire()


def test_release_verifies_token(tmp_path):
    lock = _make_lock(tmp_path)
    lock.acquire()
    _write_lock_record(tmp_path, pid=os.getpid(), hostname=socket.gethostname(), token="other")
    lock.release()
    assert _lock_path(tmp_path).exists()
    _lock_path(tmp_path).unlink()


def test_release_noop_when_not_acquired(tmp_path):
    lock = _make_lock(tmp_path)
    lock.release()


def test_release_noop_when_lock_file_missing(tmp_path):
    lock = _make_lock(tmp_path)
    lock.acquire()
    _lock_path(tmp_path).unlink()
    lock.release()


def test_hard_crashed_child_leaves_stale_lock(tmp_path):
    dead_pid = _get_dead_pid()
    _write_lock_record(
        tmp_path,
        pid=dead_pid,
        hostname=socket.gethostname(),
        token="crashed-token",
    )

    assert _lock_path(tmp_path).exists()
    assert not _pid_is_alive(dead_pid)

    parent_lock = _make_lock(tmp_path, recover_stale=True)
    parent_lock.acquire()
    try:
        data = json.loads(_lock_path(tmp_path).read_text())
        assert data["pid"] == os.getpid()
    finally:
        parent_lock.release()


def test_guard_file_mode_is_0600(tmp_path):
    lock = _make_lock(tmp_path)
    lock.acquire()
    try:
        mode = oct(_guard_path(tmp_path).stat().st_mode)[-3:]
        assert mode == "600"
    finally:
        lock.release()


def _stale_recovery_contender(
    tmp_path: str, barrier: "multiprocessing.synchronize.Barrier", result_queue: Any
) -> None:
    barrier.wait(timeout=10)
    lock = SupervisorLock(
        Path(tmp_path),
        operation="run",
        integration_path=tmp_path,
        recover_stale=True,
    )
    try:
        lock.acquire()
        result_queue.put(("ok", os.getpid()))
        import time as _time

        _time.sleep(1.0)
    except LockError as exc:
        result_queue.put((type(exc).__name__, str(exc)))
        return


def test_concurrent_stale_recovery_never_deletes_successor_lock(tmp_path):
    """Two processes racing to recover the same stale lock must be
    serialized: exactly one wins, and the loser must never delete the
    winner's freshly acquired lock (the classic check-then-unlink race)."""
    dead_pid = _get_dead_pid()
    _write_lock_record(
        tmp_path,
        pid=dead_pid,
        hostname=socket.gethostname(),
        token="crashed-token",
    )

    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    result_queue: multiprocessing.Queue = ctx.Queue()

    procs = [
        ctx.Process(
            target=_stale_recovery_contender,
            args=(str(tmp_path), barrier, result_queue),
        )
        for _ in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=10)

    results = [result_queue.get(timeout=5) for _ in range(2)]
    winners = [r for r in results if r[0] == "ok"]
    losers = [r for r in results if r[0] != "ok"]

    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0][0] == "LockError"

    assert _lock_path(tmp_path).exists()
    data = json.loads(_lock_path(tmp_path).read_text())
    assert data["pid"] == winners[0][1]


def _get_dead_pid() -> int:
    ctx = multiprocessing.get_context("fork")
    p = ctx.Process(target=lambda: None)
    p.start()
    p.join()
    assert p.pid is not None  # always set once start() has returned
    return p.pid


def _write_lock_record(tmp_path: Path, *, pid: int, hostname: str, token: str) -> None:
    lock_path = _lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": 1,
        "token": token,
        "pid": pid,
        "hostname": hostname,
        "started_at": "2026-01-01T00:00:00Z",
        "operation": "run",
        "run_id": None,
        "integration_path": str(tmp_path),
    }
    lock_path.write_text(json.dumps(data))
    os.chmod(str(lock_path), 0o600)


def _write_raw_lock(tmp_path: Path, data: dict) -> None:
    lock_path = _lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(data))
    os.chmod(str(lock_path), 0o600)


_VALID_RECORD = {
    "schema_version": 1,
    "token": "tok1",
    "pid": 1,
    "hostname": "somehost",
    "started_at": "2026-01-01T00:00:00Z",
    "operation": "run",
    "run_id": None,
    "integration_path": "/repo",
}


# -- strict lock schema validation --------------------------------------------


def test_malformed_lock_rejects_unknown_field(tmp_path):
    record = dict(_VALID_RECORD, hostname=socket.gethostname())
    record["extra"] = "surprise"
    _write_raw_lock(tmp_path, record)
    lock = _make_lock(tmp_path, recover_stale=True)
    with pytest.raises(MalformedLockError):
        lock.acquire()


def test_malformed_lock_rejects_missing_field(tmp_path):
    record = dict(_VALID_RECORD, hostname=socket.gethostname())
    del record["started_at"]
    _write_raw_lock(tmp_path, record)
    lock = _make_lock(tmp_path, recover_stale=True)
    with pytest.raises(MalformedLockError):
        lock.acquire()


def test_malformed_lock_rejects_unsupported_schema_version(tmp_path):
    record = dict(_VALID_RECORD, hostname=socket.gethostname(), schema_version=999)
    _write_raw_lock(tmp_path, record)
    lock = _make_lock(tmp_path, recover_stale=True)
    with pytest.raises(MalformedLockError):
        lock.acquire()


@pytest.mark.parametrize("bad_pid", [True, -1, 0, "123", 1.5, None])
def test_malformed_lock_rejects_invalid_pid(tmp_path, bad_pid):
    record = dict(_VALID_RECORD, hostname=socket.gethostname(), pid=bad_pid)
    _write_raw_lock(tmp_path, record)
    lock = _make_lock(tmp_path, recover_stale=True)
    with pytest.raises(MalformedLockError):
        lock.acquire()


def test_malformed_lock_rejects_empty_token(tmp_path):
    record = dict(_VALID_RECORD, hostname=socket.gethostname(), token="")
    _write_raw_lock(tmp_path, record)
    lock = _make_lock(tmp_path, recover_stale=True)
    with pytest.raises(MalformedLockError):
        lock.acquire()


def test_malformed_lock_rejects_empty_hostname(tmp_path):
    record = dict(_VALID_RECORD, hostname="")
    _write_raw_lock(tmp_path, record)
    lock = _make_lock(tmp_path, recover_stale=True)
    with pytest.raises(MalformedLockError):
        lock.acquire()


def test_malformed_lock_rejects_invalid_timestamp(tmp_path):
    record = dict(_VALID_RECORD, hostname=socket.gethostname(), started_at="not-a-timestamp")
    _write_raw_lock(tmp_path, record)
    lock = _make_lock(tmp_path, recover_stale=True)
    with pytest.raises(MalformedLockError):
        lock.acquire()


def test_malformed_lock_rejects_unknown_operation(tmp_path):
    record = dict(_VALID_RECORD, hostname=socket.gethostname(), operation="delete-everything")
    _write_raw_lock(tmp_path, record)
    lock = _make_lock(tmp_path, recover_stale=True)
    with pytest.raises(MalformedLockError):
        lock.acquire()


def test_malformed_lock_rejects_unsafe_run_id(tmp_path):
    record = dict(_VALID_RECORD, hostname=socket.gethostname(), run_id="../../evil")
    _write_raw_lock(tmp_path, record)
    lock = _make_lock(tmp_path, recover_stale=True)
    with pytest.raises(MalformedLockError):
        lock.acquire()


def test_malformed_lock_rejects_relative_integration_path(tmp_path):
    record = dict(_VALID_RECORD, hostname=socket.gethostname(), integration_path="relative/path")
    _write_raw_lock(tmp_path, record)
    lock = _make_lock(tmp_path, recover_stale=True)
    with pytest.raises(MalformedLockError):
        lock.acquire()


def test_malformed_lock_with_dead_pid_still_never_auto_recovered(tmp_path):
    """A structurally malformed record must never be treated as a
    recoverable dead-owner lock, even when it (loosely) names a dead PID
    and the local hostname; strict validation happens first."""
    dead_pid = _get_dead_pid()
    record = dict(_VALID_RECORD, hostname=socket.gethostname(), pid=dead_pid)
    record["extra_junk"] = True
    _write_raw_lock(tmp_path, record)
    lock = _make_lock(tmp_path, recover_stale=True)
    with pytest.raises(MalformedLockError):
        lock.acquire()


# -- self-poisoning: invalid outgoing metadata --------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"operation": "delete-everything"},
        {"run_id": "../../evil"},
        {"run_id": "."},
        {"run_id": ".."},
        {"run_id": "a" * 200},
        {"integration_path": "relative/path"},
        {"integration_path": ""},
    ],
)
def test_acquire_rejects_invalid_metadata_without_creating_lock(tmp_path, kwargs):
    """Invalid caller-supplied metadata must be rejected before any lock
    file is created, so the repository can never be self-poisoned with a
    record its own release() would then refuse to parse."""
    lock = _make_lock(tmp_path, **kwargs)
    with pytest.raises(LockError):
        lock.acquire()
    assert not _lock_path(tmp_path).exists()
    assert lock._token is None


def test_invalid_metadata_cannot_self_poison_repository_lock(tmp_path):
    """Full acquire/fail/valid-acquire sequence: an invalid attempt must
    leave the repository immediately available to a subsequent valid
    acquisition, not stuck behind an unrecoverable malformed lock."""
    bad = _make_lock(tmp_path, run_id="../../evil")
    with pytest.raises(LockError):
        bad.acquire()
    assert not _lock_path(tmp_path).exists()

    good = _make_lock(tmp_path, run_id="run-1")
    good.acquire()
    try:
        assert _lock_path(tmp_path).exists()
    finally:
        good.release()
    assert not _lock_path(tmp_path).exists()


# -- guard/lock symlink safety ------------------------------------------------


def test_guard_symlink_is_rejected_and_target_untouched(tmp_path):
    target_dir = tmp_path.parent / "guard-symlink-target"
    target_dir.mkdir()
    target_file = target_dir / "unrelated.txt"
    target_file.write_text("do not touch\n")
    original_mode = target_file.stat().st_mode & 0o777

    guard_path = _guard_path(tmp_path)
    guard_path.parent.mkdir(parents=True, exist_ok=True)
    guard_path.symlink_to(target_file)

    lock = _make_lock(tmp_path)
    with pytest.raises(LockError):
        lock.acquire()

    assert target_file.read_text() == "do not touch\n"
    assert (target_file.stat().st_mode & 0o777) == original_mode


def test_lock_path_symlink_is_rejected_and_target_untouched(tmp_path):
    target_dir = tmp_path.parent / "lock-symlink-target"
    target_dir.mkdir()
    target_file = target_dir / "unrelated.txt"
    target_file.write_text("do not touch\n")

    lock_path = _lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.symlink_to(target_file)

    lock = _make_lock(tmp_path, recover_stale=True)
    with pytest.raises(MalformedLockError):
        lock.acquire()

    assert target_file.read_text() == "do not touch\n"


def _acquire_dangling_symlink_target(tmp_path_str: str, result_queue: Any) -> None:
    try:
        _make_lock(Path(tmp_path_str)).acquire()
    except MalformedLockError:
        result_queue.put("MalformedLockError")
    except LockError as exc:
        result_queue.put(f"other LockError: {exc}")
    except BaseException as exc:  # pragma: no cover - diagnostic aid
        result_queue.put(f"unexpected: {exc!r}")
    else:
        result_queue.put("returned")


def test_dangling_lock_symlink_is_rejected_not_spun_on(tmp_path):
    """A lock-path symlink whose target does not exist must be treated as
    a malformed lock, not as "no lock present". Path.exists() follows
    symlinks and reports False for a dangling target, which previously
    made _inspect_existing_lock() return None ("disappeared, retry")
    forever: acquire()'s retry loop would re-attempt os.link() against
    the same dangling symlink, which always fails with FileExistsError
    (link(2) does not follow symlinks), spinning at 100% CPU while
    holding the guard flock for the whole repository.

    Run in a subprocess with a bounded join: against the current bug,
    calling acquire() directly in this process would hang the test
    process itself with no way to time out, defeating the purpose of a
    regression test. The subprocess can be killed unconditionally."""
    target_dir = tmp_path.parent / "dangling-symlink-target-dir"
    target_dir.mkdir()
    lock_path = _lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.symlink_to(target_dir / "does-not-exist")

    ctx = multiprocessing.get_context("fork")
    result_queue: multiprocessing.Queue = ctx.Queue()
    proc = ctx.Process(target=_acquire_dangling_symlink_target, args=(str(tmp_path), result_queue))
    proc.start()
    proc.join(timeout=5)
    still_alive = proc.is_alive()
    if still_alive:
        proc.terminate()
        proc.join(timeout=5)

    assert not still_alive, "acquire() spun instead of raising on a dangling lock symlink"
    assert result_queue.get_nowait() == "MalformedLockError"
    assert lock_path.is_symlink()
    assert not lock_path.exists()


def test_dangling_lock_symlink_at_release_time_is_not_treated_as_absent(tmp_path):
    """release() has the same exists()-follows-symlinks hazard as
    acquire(): if the lock path becomes a dangling symlink between
    acquire() and release() (e.g. something else replaced the lock file
    with a broken symlink), release() must not silently conclude "the
    lock is already gone" and discard its ownership token. It must
    surface a clear error and leave the symlink in place for inspection.
    """
    lock = _make_lock(tmp_path)
    lock.acquire()
    lock_path = _lock_path(tmp_path)
    lock_path.unlink()
    lock_path.symlink_to(tmp_path / "does-not-exist")

    with pytest.raises(LockError):
        lock.release()

    assert lock._token is not None
    assert lock_path.is_symlink()


@pytest.mark.parametrize("capability", ["O_NOFOLLOW", "O_DIRECTORY"])
def test_acquire_fails_closed_without_required_open_capability(tmp_path, monkeypatch, capability):
    monkeypatch.delattr(locking_mod.os, capability)

    with pytest.raises(LockError, match=rf"requires os\.{capability}"):
        _make_lock(tmp_path).acquire()

    assert not (tmp_path / "loop-supervisor").exists()


def test_missing_no_follow_capability_cannot_modify_symlink_target(tmp_path, monkeypatch):
    outside = tmp_path.parent / "outside-unsupported-lock"
    outside.mkdir()
    outside_guard = outside / "supervisor.lock.guard"
    outside_guard.write_text("outside guard\n")
    outside_guard.chmod(0o644)
    storage = tmp_path / "loop-supervisor"
    storage.symlink_to(outside, target_is_directory=True)
    monkeypatch.delattr(locking_mod.os, "O_NOFOLLOW")

    with pytest.raises(LockError, match=r"requires os\.O_NOFOLLOW"):
        _make_lock(tmp_path).acquire()

    assert storage.is_symlink()
    assert outside_guard.read_text() == "outside guard\n"
    assert outside_guard.stat().st_mode & 0o777 == 0o644
    assert not (outside / "supervisor.lock").exists()


def test_acquire_rejects_symlinked_lock_directory_without_touching_target(tmp_path):
    outside = tmp_path.parent / "outside-lock-acquire"
    outside.mkdir()
    outside_guard = outside / "supervisor.lock.guard"
    outside_guard.write_text("outside guard\n")
    outside_guard.chmod(0o644)

    storage = tmp_path / "loop-supervisor"
    storage.symlink_to(outside, target_is_directory=True)

    lock = _make_lock(tmp_path)
    with pytest.raises(LockError, match="symbolic link"):
        lock.acquire()

    assert storage.is_symlink()
    assert outside_guard.read_text() == "outside guard\n"
    assert outside_guard.stat().st_mode & 0o777 == 0o644
    assert not (outside / "supervisor.lock").exists()


def test_inspection_and_recovery_reject_symlinked_lock_directory_without_touching_target(
    tmp_path,
):
    outside = tmp_path.parent / "outside-lock-recovery"
    outside.mkdir()
    dead_pid = _get_dead_pid()
    outside_lock = outside / "supervisor.lock"
    outside_lock.write_text(
        json.dumps(
            dict(
                _VALID_RECORD,
                pid=dead_pid,
                hostname=socket.gethostname(),
                token="outside-token",
                integration_path=str(tmp_path),
            )
        )
    )
    outside_lock.chmod(0o640)
    original = outside_lock.read_bytes()

    storage = tmp_path / "loop-supervisor"
    storage.symlink_to(outside, target_is_directory=True)

    lock = _make_lock(tmp_path, recover_stale=True)
    with pytest.raises(LockError, match="symbolic link"):
        lock.acquire()

    assert storage.is_symlink()
    assert outside_lock.read_bytes() == original
    assert outside_lock.stat().st_mode & 0o777 == 0o640
    assert not (outside / "supervisor.lock.guard").exists()


def test_release_rejects_symlinked_lock_directory_without_touching_target(tmp_path):
    lock = _make_lock(tmp_path)
    lock.acquire()
    storage = tmp_path / "loop-supervisor"
    displaced = tmp_path / "real-loop-supervisor"
    storage.rename(displaced)

    outside = tmp_path.parent / "outside-lock-release"
    outside.mkdir()
    outside_guard = outside / "supervisor.lock.guard"
    outside_guard.write_text("outside guard\n")
    outside_guard.chmod(0o644)
    outside_lock = outside / "supervisor.lock"
    outside_lock.write_text("outside lock\n")
    outside_lock.chmod(0o640)
    storage.symlink_to(outside, target_is_directory=True)

    with pytest.raises(LockError, match="symbolic link"):
        lock.release()

    assert lock._token is not None
    assert storage.is_symlink()
    assert outside_guard.read_text() == "outside guard\n"
    assert outside_guard.stat().st_mode & 0o777 == 0o644
    assert outside_lock.read_text() == "outside lock\n"
    assert outside_lock.stat().st_mode & 0o777 == 0o640


# -- retryable release --------------------------------------------------------


def test_release_retries_after_transient_unlink_failure(tmp_path, monkeypatch):
    lock = _make_lock(tmp_path)
    lock.acquire()

    original_unlink = os.unlink
    call_count = [0]

    def _flaky_unlink(path, *a, **kw):
        call_count[0] += 1
        if call_count[0] == 1:
            raise OSError("simulated transient unlink failure")
        return original_unlink(path, *a, **kw)

    monkeypatch.setattr(os, "unlink", _flaky_unlink)

    with pytest.raises(LockError):
        lock.release()
    # Ownership must be retained after a transient failure.
    assert lock._token is not None
    assert _lock_path(tmp_path).exists()

    monkeypatch.setattr(os, "unlink", original_unlink)
    lock.release()
    assert lock._token is None
    assert not _lock_path(tmp_path).exists()


def test_release_retries_after_transient_read_failure(tmp_path, monkeypatch):
    import loop_supervisor.locking as locking_mod

    lock = _make_lock(tmp_path)
    lock.acquire()

    original_read_lock = locking_mod._read_lock
    call_count = [0]

    def _flaky_read_lock(path, *, directory_fd=None):
        call_count[0] += 1
        if call_count[0] == 1:
            raise MalformedLockError("simulated transient read failure")
        return original_read_lock(path, directory_fd=directory_fd)

    monkeypatch.setattr(locking_mod, "_read_lock", _flaky_read_lock)

    with pytest.raises(LockError):
        lock.release()
    assert lock._token is not None
    assert _lock_path(tmp_path).exists()

    monkeypatch.setattr(locking_mod, "_read_lock", original_read_lock)
    lock.release()
    assert lock._token is None


def test_release_clears_token_when_path_missing(tmp_path):
    lock = _make_lock(tmp_path)
    lock.acquire()
    _lock_path(tmp_path).unlink()
    lock.release()
    assert lock._token is None


def test_release_clears_token_on_ownership_loss(tmp_path):
    """If a successor's token now occupies the path (e.g. after this
    instance's stale lock was recovered by someone else), release() must
    recognize it no longer owns anything and clear its token without
    touching the file."""
    lock = _make_lock(tmp_path)
    lock.acquire()
    _write_lock_record(tmp_path, pid=os.getpid(), hostname=socket.gethostname(), token="other")
    lock.release()
    assert lock._token is None
    assert _lock_path(tmp_path).exists()
    _lock_path(tmp_path).unlink()
