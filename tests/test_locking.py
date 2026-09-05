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
    IdentityStatus,
    LockError,
    MalformedLockError,
    RemoteLockError,
    StaleLockError,
    SupervisorLock,
    _guard_path,
    _lock_path,
    _pid_is_alive,
    classify_local_owner_identity,
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
        assert data["schema_version"] == 2
        assert isinstance(data["token"], str) and data["token"]
        assert isinstance(data["owner_boot_id"], str) and data["owner_boot_id"]
        assert isinstance(data["owner_process_start"], str) and data["owner_process_start"]
        assert data["pid"] == os.getpid()
        assert data["hostname"] == socket.gethostname()
        assert data["operation"] == "run"
        assert data["run_id"] == "abc123"
        assert data["integration_path"] == "/repo"
    finally:
        lock.release()


def test_acquire_records_schema_v2_kernel_owner_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(
        locking_mod,
        "_read_kernel_owner_identity",
        lambda pid: (f"boot-id-for-{pid}", "opaque-start-ticks"),
    )
    lock = _make_lock(tmp_path)
    lock.acquire()
    try:
        data = json.loads(_lock_path(tmp_path).read_text())
        assert data["schema_version"] == 2
        assert data["owner_boot_id"] == f"boot-id-for-{os.getpid()}"
        assert data["owner_process_start"] == "opaque-start-ticks"
    finally:
        lock.release()


def test_acquire_fails_closed_when_kernel_owner_identity_is_unavailable(tmp_path, monkeypatch):
    def _unavailable_identity(pid: int) -> tuple[str, str]:
        raise LockError(f"cannot read kernel owner identity for PID {pid}")

    monkeypatch.setattr(locking_mod, "_read_kernel_owner_identity", _unavailable_identity)

    with pytest.raises(LockError, match="cannot read kernel owner identity"):
        _make_lock(tmp_path).acquire()

    assert not _lock_path(tmp_path).exists()


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


def test_acquire_normalizes_lock_directory_creation_failure(tmp_path, monkeypatch):
    original_mkdir = os.mkdir

    def _fail_lock_directory(path, *args, **kwargs):
        if Path(path) == tmp_path / "loop-supervisor":
            raise PermissionError("simulated lock directory denial")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(locking_mod.os, "mkdir", _fail_lock_directory)
    lock = _make_lock(tmp_path)

    with pytest.raises(LockError, match="cannot use lock directory") as caught:
        lock.acquire()

    assert isinstance(caught.value.__cause__, PermissionError)
    assert lock._token is None


@pytest.mark.parametrize("failure_stage", ["fchmod", "fdopen"])
def test_acquire_closes_temporary_fd_and_normalizes_setup_failure(
    tmp_path, monkeypatch, failure_stage
):
    original_open = os.open
    original_close = os.close
    original_fchmod = os.fchmod
    original_fdopen = os.fdopen
    temporary_fd: list[int] = []
    close_count = 0

    def _recording_open(path, *args, **kwargs):
        fd = original_open(path, *args, **kwargs)
        if str(path).startswith(".tmp-lock-"):
            temporary_fd.append(fd)
        return fd

    def _fchmod(fd, mode):
        if failure_stage == "fchmod" and temporary_fd == [fd]:
            raise OSError("simulated lock temporary fchmod failure")
        return original_fchmod(fd, mode)

    def _fdopen(fd, *args, **kwargs):
        if failure_stage == "fdopen" and temporary_fd == [fd]:
            raise OSError("simulated lock temporary fdopen failure")
        return original_fdopen(fd, *args, **kwargs)

    def _recording_close(fd):
        nonlocal close_count
        if temporary_fd == [fd]:
            close_count += 1
        return original_close(fd)

    monkeypatch.setattr(locking_mod.os, "open", _recording_open)
    monkeypatch.setattr(locking_mod.os, "fchmod", _fchmod)
    monkeypatch.setattr(locking_mod.os, "fdopen", _fdopen)
    monkeypatch.setattr(locking_mod.os, "close", _recording_close)
    lock = _make_lock(tmp_path)

    with pytest.raises(LockError, match="cannot acquire lock") as caught:
        lock.acquire()

    assert isinstance(caught.value.__cause__, OSError)
    assert len(temporary_fd) == 1
    assert close_count == 1
    assert lock._token is None
    assert not _lock_path(tmp_path).exists()
    assert not list((tmp_path / "loop-supervisor").glob(".tmp-lock-*"))

    monkeypatch.setattr(locking_mod.os, "fchmod", original_fchmod)
    monkeypatch.setattr(locking_mod.os, "fdopen", original_fdopen)
    lock.acquire()
    lock.release()
    assert lock._token is None


@pytest.mark.parametrize("failure_stage", ["write", "link"])
def test_acquire_normalizes_lock_temporary_publication_failure(
    tmp_path, monkeypatch, failure_stage
):
    if failure_stage == "write":

        def _fail_write(*args, **kwargs):
            raise OSError("simulated lock temporary write failure")

        monkeypatch.setattr(locking_mod.json, "dump", _fail_write)
    else:

        def _fail_link(*args, **kwargs):
            raise OSError("simulated lock temporary link failure")

        monkeypatch.setattr(locking_mod.os, "link", _fail_link)

    lock = _make_lock(tmp_path)
    with pytest.raises(LockError, match="cannot acquire lock") as caught:
        lock.acquire()

    assert isinstance(caught.value.__cause__, OSError)
    assert lock._token is None
    assert not _lock_path(tmp_path).exists()
    assert not list((tmp_path / "loop-supervisor").glob(".tmp-lock-*"))


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


# -- guarded run-ID binding ----------------------------------------------------


def test_bind_run_id_replaces_null_record_atomically_at_mode_0600(tmp_path):
    lock = _make_lock(tmp_path)
    lock.acquire()
    try:
        lock.bind_run_id("new-run")
        record = json.loads(_lock_path(tmp_path).read_text())
        assert record["run_id"] == "new-run"
        assert _lock_path(tmp_path).stat().st_mode & 0o777 == 0o600
    finally:
        lock.release()


def test_bind_run_id_is_idempotent_for_the_same_run_id(tmp_path):
    lock = _make_lock(tmp_path)
    lock.acquire()
    try:
        lock.bind_run_id("new-run")
        lock.bind_run_id("new-run")
        assert json.loads(_lock_path(tmp_path).read_text())["run_id"] == "new-run"
    finally:
        lock.release()


def test_bind_run_id_rejects_mismatched_ownership_token(tmp_path):
    lock = _make_lock(tmp_path)
    lock.acquire()
    record = json.loads(_lock_path(tmp_path).read_text())
    record["token"] = "different-owner"
    _write_raw_lock(tmp_path, record)

    with pytest.raises(LockError, match="ownership token"):
        lock.bind_run_id("new-run")

    assert json.loads(_lock_path(tmp_path).read_text())["run_id"] is None
    lock.release()
    _lock_path(tmp_path).unlink()


def test_bind_run_id_rejects_differing_existing_run_id(tmp_path):
    lock = _make_lock(tmp_path)
    lock.acquire()
    try:
        lock.bind_run_id("first-run")
        with pytest.raises(LockError, match="already bound"):
            lock.bind_run_id("different-run")
        assert json.loads(_lock_path(tmp_path).read_text())["run_id"] == "first-run"
    finally:
        lock.release()


def test_bind_run_id_fails_closed_on_owner_identity_mismatch(tmp_path):
    lock = _make_lock(tmp_path)
    lock.acquire()
    try:
        record = json.loads(_lock_path(tmp_path).read_text())
        record["owner_boot_id"] = "different-boot-id"
        _write_raw_lock(tmp_path, record)

        with pytest.raises(LockError, match="owner identity"):
            lock.bind_run_id("new-run")

        assert json.loads(_lock_path(tmp_path).read_text())["run_id"] is None
    finally:
        lock.release()


# -- classify_local_owner_identity (ADR 0037 identity-chain test) -------------


def test_classify_local_owner_identity_matching_when_boot_and_start_agree(monkeypatch):
    monkeypatch.setattr(locking_mod, "_read_boot_id", lambda: "boot-1")
    monkeypatch.setattr(locking_mod, "_read_process_start", lambda pid: "start-1")

    status = classify_local_owner_identity(os.getpid(), "boot-1", "start-1")

    assert status is IdentityStatus.MATCHING


def test_classify_local_owner_identity_stale_on_boot_id_mismatch(monkeypatch):
    monkeypatch.setattr(locking_mod, "_read_boot_id", lambda: "current-boot")

    def _unexpected_process_start(pid: int) -> str:
        raise AssertionError("must not read process-start ticks after a boot mismatch")

    monkeypatch.setattr(locking_mod, "_read_process_start", _unexpected_process_start)

    status = classify_local_owner_identity(os.getpid(), "recorded-boot", "start-1")

    assert status is IdentityStatus.STALE


def test_classify_local_owner_identity_stale_when_pid_no_longer_exists(monkeypatch):
    """PID reuse and plain process exit both surface the same way here:
    the *recorded* PID no longer names any process at inspection time."""
    monkeypatch.setattr(locking_mod, "_read_boot_id", lambda: "boot-1")

    def _no_such_process(pid: int) -> str:
        raise FileNotFoundError(f"/proc/{pid}/stat")

    monkeypatch.setattr(locking_mod, "_read_process_start", _no_such_process)

    status = classify_local_owner_identity(999_999, "boot-1", "start-1")

    assert status is IdentityStatus.STALE


def test_classify_local_owner_identity_stale_on_process_start_mismatch(monkeypatch):
    """A live PID whose process-start ticks differ from the recorded value
    is PID reuse: a different process now holds that PID number."""
    monkeypatch.setattr(locking_mod, "_read_boot_id", lambda: "boot-1")
    monkeypatch.setattr(locking_mod, "_read_process_start", lambda pid: "a-different-start-value")

    status = classify_local_owner_identity(os.getpid(), "boot-1", "start-1")

    assert status is IdentityStatus.STALE


def test_classify_local_owner_identity_unverifiable_when_boot_id_unreadable(monkeypatch):
    def _unavailable_boot_id() -> str:
        raise LockError("cannot read kernel boot ID: simulated failure")

    monkeypatch.setattr(locking_mod, "_read_boot_id", _unavailable_boot_id)

    status = classify_local_owner_identity(os.getpid(), "boot-1", "start-1")

    assert status is IdentityStatus.UNVERIFIABLE


def test_classify_local_owner_identity_unverifiable_when_process_start_unreadable(monkeypatch):
    monkeypatch.setattr(locking_mod, "_read_boot_id", lambda: "boot-1")

    def _unreadable_process_start(pid: int) -> str:
        raise LockError(f"cannot read process start ticks for PID {pid}: simulated failure")

    monkeypatch.setattr(locking_mod, "_read_process_start", _unreadable_process_start)

    status = classify_local_owner_identity(os.getpid(), "boot-1", "start-1")

    assert status is IdentityStatus.UNVERIFIABLE


# -- schema-aware stale-lock recovery (ADR 0037) ------------------------------


def _write_lock_record_v2(
    tmp_path: Path, *, pid: int, hostname: str, token: str, boot_id: str, process_start: str
) -> None:
    lock_path = _lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": 2,
        "token": token,
        "pid": pid,
        "owner_boot_id": boot_id,
        "owner_process_start": process_start,
        "hostname": hostname,
        "started_at": "2026-01-01T00:00:00Z",
        "operation": "run",
        "run_id": None,
        "integration_path": str(tmp_path),
    }
    lock_path.write_text(json.dumps(data))
    os.chmod(str(lock_path), 0o600)


def test_v2_lock_with_reused_pid_is_recovered_as_stale(tmp_path, monkeypatch):
    """A live local PID whose recorded boot/start identity no longer
    matches the current kernel state is PID reuse, not a live owner --
    schema 2's whole point per ADR 0037. Recovery must succeed even
    though the named PID is alive."""
    _write_lock_record_v2(
        tmp_path,
        pid=os.getpid(),
        hostname=socket.gethostname(),
        token="reused-pid-token",
        boot_id="stale-boot-id",
        process_start="stale-start-ticks",
    )
    monkeypatch.setattr(locking_mod, "_read_boot_id", lambda: "current-boot-id")
    monkeypatch.setattr(locking_mod, "_read_process_start", lambda pid: "current-start-ticks")

    lock = _make_lock(tmp_path, recover_stale=True)
    lock.acquire()
    try:
        data = json.loads(_lock_path(tmp_path).read_text())
        assert data["pid"] == os.getpid()
        assert data["token"] != "reused-pid-token"
    finally:
        lock.release()


def test_v2_lock_with_reused_pid_rejected_without_recover_flag(tmp_path, monkeypatch):
    _write_lock_record_v2(
        tmp_path,
        pid=os.getpid(),
        hostname=socket.gethostname(),
        token="reused-pid-token",
        boot_id="stale-boot-id",
        process_start="stale-start-ticks",
    )
    monkeypatch.setattr(locking_mod, "_read_boot_id", lambda: "current-boot-id")
    monkeypatch.setattr(locking_mod, "_read_process_start", lambda pid: "current-start-ticks")

    lock = _make_lock(tmp_path, recover_stale=False)
    with pytest.raises(StaleLockError):
        lock.acquire()


def test_v2_lock_with_matching_identity_cannot_be_force_recovered(tmp_path, monkeypatch):
    """The exact-identity match case: the same process still holds the
    lock, so recovery must be refused exactly as PID-only schema 1 would
    refuse a live owner."""
    monkeypatch.setattr(locking_mod, "_read_boot_id", lambda: "current-boot-id")
    monkeypatch.setattr(locking_mod, "_read_process_start", lambda pid: "current-start-ticks")
    _write_lock_record_v2(
        tmp_path,
        pid=os.getpid(),
        hostname=socket.gethostname(),
        token="live-token",
        boot_id="current-boot-id",
        process_start="current-start-ticks",
    )

    lock = _make_lock(tmp_path, recover_stale=True)
    with pytest.raises(LockError):
        lock.acquire()

    assert json.loads(_lock_path(tmp_path).read_text())["token"] == "live-token"


def test_v2_lock_recovered_when_pid_has_exited(tmp_path, monkeypatch):
    """Ordinary dead-owner recovery must still work for schema 2, not only
    the PID-reuse case: an exited PID is stale regardless of what boot/
    start identity was recorded for it."""
    dead_pid = _get_dead_pid()
    _write_lock_record_v2(
        tmp_path,
        pid=dead_pid,
        hostname=socket.gethostname(),
        token="dead-owner-token",
        boot_id="whatever-boot-id",
        process_start="whatever-start-ticks",
    )
    monkeypatch.setattr(locking_mod, "_read_boot_id", lambda: "whatever-boot-id")

    lock = _make_lock(tmp_path, recover_stale=True)
    lock.acquire()
    try:
        data = json.loads(_lock_path(tmp_path).read_text())
        assert data["pid"] == os.getpid()
    finally:
        lock.release()


def test_v2_lock_recovery_refused_when_boot_id_unverifiable(tmp_path, monkeypatch):
    """Unverifiable identity must never be treated as proof of staleness:
    acquisition fails closed rather than guessing."""
    _write_lock_record_v2(
        tmp_path,
        pid=os.getpid(),
        hostname=socket.gethostname(),
        token="unverifiable-token",
        boot_id="some-boot-id",
        process_start="some-start-ticks",
    )

    def _unavailable_boot_id() -> str:
        raise LockError("cannot read kernel boot ID: simulated failure")

    monkeypatch.setattr(locking_mod, "_read_boot_id", _unavailable_boot_id)

    lock = _make_lock(tmp_path, recover_stale=True)
    with pytest.raises(LockError):
        lock.acquire()

    assert json.loads(_lock_path(tmp_path).read_text())["token"] == "unverifiable-token"


def test_v1_lock_stale_recovery_remains_pid_only(tmp_path, monkeypatch):
    """Schema-1 records carry no immutable owner identity to compare, so
    the *staleness decision* for an old v1 record must remain governed
    solely by PID liveness -- unaffected by ADR 0037's schema-2 identity
    chain. (acquire() separately reads kernel identity for the *new*
    schema-2 record it is about to write; that is unrelated to how the
    old v1 holder's staleness is decided and is not what this asserts.)"""

    def _unexpected_classification(*args: object, **kwargs: object) -> IdentityStatus:
        raise AssertionError("v1 recovery must not classify identity via kernel comparison")

    monkeypatch.setattr(locking_mod, "classify_local_owner_identity", _unexpected_classification)

    dead_pid = _get_dead_pid()
    _write_lock_record(tmp_path, pid=dead_pid, hostname=socket.gethostname(), token="v1-token")

    lock = _make_lock(tmp_path, recover_stale=True)
    lock.acquire()
    try:
        data = json.loads(_lock_path(tmp_path).read_text())
        assert data["pid"] == os.getpid()
    finally:
        lock.release()


def test_v1_live_local_owner_still_cannot_be_force_recovered(tmp_path):
    """Unchanged v1 behavior: a live local PID is never recoverable,
    regardless of the schema-2 identity machinery added alongside it."""
    lock1 = _make_lock(tmp_path)
    lock1.acquire()
    try:
        record = json.loads(_lock_path(tmp_path).read_text())
        record["schema_version"] = 1
        del record["owner_boot_id"]
        del record["owner_process_start"]
        _write_raw_lock(tmp_path, record)

        lock2 = _make_lock(tmp_path, recover_stale=True)
        with pytest.raises(LockError):
            lock2.acquire()
    finally:
        lock1.release()


# -- strict schema_version type validation ------------------------------------


@pytest.mark.parametrize("bad_schema_version", [True, False, 2.0, 1.0, "2", None, [2]])
def test_malformed_lock_rejects_non_integer_schema_version(tmp_path, bad_schema_version):
    record = dict(_VALID_RECORD, hostname=socket.gethostname())
    record["schema_version"] = bad_schema_version
    _write_raw_lock(tmp_path, record)
    lock = _make_lock(tmp_path, recover_stale=True)
    with pytest.raises(MalformedLockError, match="schema_version"):
        lock.acquire()
