"""Supervisor repository lock.

Prevents two mutating supervisor processes (run, resume, tui) from racing
against the same Git repository. The lock record is stored at:

    <git-common-dir>/loop-supervisor/supervisor.lock

It is a plain JSON file with mode 0600. Acquisition uses a link(2)-based
atomic create-if-absent strategy.

Stale-lock recovery is always explicit: the caller must pass
``recover_stale=True``. Remote-hostname and malformed locks are never
auto-recovered.

The lock does NOT integrate with Git itself; Git is unaware of it.

Read-only operations (listing runs, reading run details) do not require
the lock.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import socket
import stat
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .state import validate_run_id


class LockError(RuntimeError):
    """Raised when lock acquisition fails."""


class StaleLockError(LockError):
    """Raised when a stale lock is detected but recovery was not requested."""


class RemoteLockError(LockError):
    """Raised when the lock is held by a process on another host."""


class MalformedLockError(LockError):
    """Raised when the lock file exists but cannot be parsed."""


_SCHEMA_VERSION = 1
_VALID_OPERATIONS = frozenset({"run", "resume", "tui"})
_LOCK_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "token",
        "pid",
        "hostname",
        "started_at",
        "operation",
        "run_id",
        "integration_path",
    }
)
# Matches time.strftime("%Y-%m-%dT%H:%M:%SZ", ...): a fixed-width UTC
# timestamp, deliberately not full ISO-8601 parsing since this is the
# exact (and only) format acquire() ever writes.
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _lock_path(git_common_dir: Path) -> Path:
    return git_common_dir / "loop-supervisor" / "supervisor.lock"


def _guard_path(git_common_dir: Path) -> Path:
    return git_common_dir / "loop-supervisor" / "supervisor.lock.guard"


def _required_open_flag(name: str) -> int:
    """Return a required secure-open flag, or fail closed if unavailable."""
    value = getattr(os, name, None)
    if not isinstance(value, int):
        raise LockError(f"secure lock storage requires os.{name}; this platform is unsupported")
    return value


def _open_lock_directory(git_common_dir: Path) -> int:
    """Open the lock storage directory without following its leaf.

    All subsequent lock and guard operations are relative to this descriptor,
    so replacing or redirecting the pathname cannot move an in-progress
    critical section outside Git metadata.
    """
    directory_flag = _required_open_flag("O_DIRECTORY")
    nofollow_flag = _required_open_flag("O_NOFOLLOW")
    directory = git_common_dir / "loop-supervisor"
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    flags = os.O_RDONLY | directory_flag | nofollow_flag
    try:
        fd = os.open(directory, flags)
    except OSError as exc:
        raise LockError(
            f"cannot use lock directory {directory}; refusing symbolic link or unsafe path: {exc}"
        ) from exc
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise LockError(f"lock directory {directory} is not a directory")
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def _guarded(git_common_dir: Path) -> Iterator[int]:
    """Serialize lock create/recover/release critical sections.

    Uses a persistent mode-0600 guard file with an exclusive ``flock(2)``
    to ensure that acquisition, stale-lock recovery, and release never
    interleave across processes. Without this, a check-then-act race
    (e.g. read stale token, then unlink) could let one process delete a
    successor's freshly acquired lock.

    Opened with O_NOFOLLOW (where supported) and verified to be a regular
    file before flock/chmod are applied: if the guard path were replaced
    with a symlink, following it would both flock and chmod(0600) an
    attacker-chosen target file, and then treat that unrelated inode as
    this process's synchronization primitive.
    """
    guard_path = _guard_path(git_common_dir)
    directory_fd = _open_lock_directory(git_common_dir)
    try:
        try:
            fd = _open_no_follow(
                Path(guard_path.name), os.O_CREAT | os.O_RDWR, 0o600, dir_fd=directory_fd
            )
        except OSError as exc:
            raise LockError(f"cannot open lock guard file {guard_path}: {exc}") from exc
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield directory_fd
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def _pid_is_alive(pid: int) -> bool:
    """Return True if the local PID appears to be alive."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _open_no_follow(path: Path, flags: int, mode: int = 0o600, *, dir_fd: int | None = None) -> int:
    """Open a path with mandatory symlink-following refusal and verify the
    resulting descriptor refers to a regular file. Platforms without
    ``O_NOFOLLOW`` are rejected rather than silently weakening this contract.
    Used for both the guard file and the lock file itself:
    neither acquisition, inspection, nor release must ever be tricked into
    operating on an arbitrary attacker-controlled target reached via a
    symlink placed at the expected path."""
    nofollow = _required_open_flag("O_NOFOLLOW")
    fd = os.open(str(path), flags | nofollow, mode, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise LockError(f"{path} exists but is not a regular file; refusing to use it")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _validate_lock_record(data: Any) -> dict[str, Any]:
    """Strictly validate a lock record's schema and semantics.

    Shared by both the on-disk reader (_read_lock) and prospective
    outgoing records (SupervisorLock.acquire), so the rules a record must
    satisfy to be *read* can never drift from the rules a record must
    satisfy to be *written*. Raises MalformedLockError on any problem:
    missing/unknown fields, wrong types, an unsupported schema version, an
    invalid operation, an invalid run_id, or a non-absolute
    integration_path.
    """
    if not isinstance(data, dict):
        raise MalformedLockError("lock record must be a JSON object")

    unknown = set(data) - _LOCK_RECORD_FIELDS
    if unknown:
        raise MalformedLockError(f"lock record contains unknown fields: {sorted(unknown)}")
    missing = _LOCK_RECORD_FIELDS - set(data)
    if missing:
        raise MalformedLockError(f"lock record is missing required fields: {sorted(missing)}")

    if data.get("schema_version") != _SCHEMA_VERSION:
        raise MalformedLockError(
            f"lock record has unsupported schema_version {data.get('schema_version')!r} "
            f"(expected {_SCHEMA_VERSION})"
        )

    token = data.get("token")
    if not isinstance(token, str) or not token:
        raise MalformedLockError("lock record field 'token' must be a non-empty string")

    pid = data.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise MalformedLockError(f"lock record has invalid pid {pid!r}")

    hostname = data.get("hostname")
    if not isinstance(hostname, str) or not hostname:
        raise MalformedLockError("lock record field 'hostname' must be a non-empty string")

    started_at = data.get("started_at")
    if not isinstance(started_at, str) or not _TIMESTAMP_RE.match(started_at):
        raise MalformedLockError("lock record field 'started_at' is not a valid timestamp")

    operation = data.get("operation")
    if operation not in _VALID_OPERATIONS:
        raise MalformedLockError(f"lock record has invalid operation {operation!r}")

    run_id = data.get("run_id")
    if run_id is not None:
        try:
            validate_run_id(run_id)
        except Exception as exc:
            raise MalformedLockError(f"lock record has invalid run_id {run_id!r}: {exc}") from exc

    integration_path = data.get("integration_path")
    if integration_path is not None:
        if not isinstance(integration_path, str) or not integration_path:
            raise MalformedLockError(
                "lock record field 'integration_path' must be null or a non-empty string"
            )
        if not os.path.isabs(integration_path):
            raise MalformedLockError(
                f"lock record field 'integration_path' must be absolute: {integration_path!r}"
            )

    return data


def _read_lock(path: Path, *, directory_fd: int | None = None) -> dict[str, Any]:
    """Read, parse, and strictly validate the lock file.

    Raises MalformedLockError on any problem: missing/unknown fields,
    wrong types, an unsupported schema version, an invalid operation, an
    invalid run_id, or a non-absolute integration_path. A record that
    fails this validation is never treated as a recoverable dead-owner
    lock (see _inspect_existing_lock) — it always fails closed, exactly
    like invalid JSON or a non-object body already did.
    """
    try:
        fd = _open_no_follow(
            Path(path.name) if directory_fd is not None else path,
            os.O_RDONLY,
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise MalformedLockError(f"cannot open lock file: {exc}") from exc
    except LockError as exc:
        raise MalformedLockError(str(exc)) from exc
    try:
        with os.fdopen(fd, "r") as handle:
            text = handle.read()
    except OSError as exc:
        raise MalformedLockError(f"cannot read lock file: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedLockError(f"lock file is not valid JSON: {exc}") from exc

    return _validate_lock_record(data)


def _write_lock_file(path: Path, record: dict[str, Any], *, directory_fd: int) -> None:
    """Atomically write the lock record, enforcing mode 0600."""
    tmp_name = f".tmp-lock-{uuid.uuid4().hex}.json"
    fd = _open_no_follow(
        Path(tmp_name), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=directory_fd
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle, indent=2)
            handle.write("\n")
        os.link(
            tmp_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
    finally:
        try:
            os.unlink(tmp_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


class SupervisorLock:
    """Context manager that acquires and releases the supervisor lock.

    Usage::

        with SupervisorLock(git_common_dir, operation="run") as lock:
            ...

    Parameters
    ----------
    git_common_dir:
        The Git common directory (``git rev-parse --git-common-dir``).
    operation:
        Human-readable label for what this process is doing ("run", "resume",
        "tui").
    run_id:
        Optional run ID being operated on.
    integration_path:
        Absolute path to the integration worktree.
    recover_stale:
        If True and the lock is held by a demonstrably dead local PID, remove
        the stale lock and retry. Never auto-recovers remote or malformed locks.
    """

    def __init__(
        self,
        git_common_dir: Path,
        *,
        operation: str,
        run_id: str | None = None,
        integration_path: str | None = None,
        recover_stale: bool = False,
    ) -> None:
        self._path = _lock_path(git_common_dir)
        self._operation = operation
        self._run_id = run_id
        self._integration_path = integration_path
        self._recover_stale = recover_stale
        self._token: str | None = None

    def __enter__(self) -> SupervisorLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()

    def acquire(self) -> None:
        """Acquire the lock. Raises LockError if the lock cannot be acquired.

        The entire create/inspect/recover sequence runs under the guard
        file's exclusive flock, so concurrent acquirers and stale-lock
        recoverers are fully serialized: no other process can observe or
        mutate the lock path while this call is deciding what to do.
        """
        token = uuid.uuid4().hex
        record: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "token": token,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "operation": self._operation,
            "run_id": self._run_id,
            "integration_path": self._integration_path,
        }

        # Validate the prospective record against the same strict schema
        # used to read on-disk locks, *before* touching the filesystem.
        # Otherwise invalid caller-supplied metadata (a bad operation, a
        # traversal run_id, a relative integration_path) would be written
        # to disk and then be unreadable/unreleasable by this very object:
        # release() re-reads and strictly validates, so it would refuse to
        # remove the record it just wrote, leaving a malformed lock that
        # deliberately cannot be stale-recovered — a persistent local DoS.
        try:
            _validate_lock_record(record)
        except MalformedLockError as exc:
            self._token = None
            raise LockError(f"refusing to acquire lock with invalid metadata: {exc}") from exc

        with _guarded(self._path.parent.parent) as directory_fd:
            while True:
                try:
                    _write_lock_file(self._path, record, directory_fd=directory_fd)
                    self._token = token
                    return
                except FileExistsError:
                    pass

                existing = self._inspect_existing_lock(directory_fd=directory_fd)
                if existing is None:
                    continue

                self._token = None
                raise existing

    def _inspect_existing_lock(self, *, directory_fd: int) -> LockError | None:
        """Inspect the existing lock and decide what to do.

        Returns None if the lock disappeared (retry), or a LockError
        if acquisition should fail.

        Uses os.path.lexists rather than Path.exists: the latter follows
        symlinks and reports False for a dangling symlink at the lock
        path, which would make this method say "disappeared, retry" for
        something that is actually present. acquire()'s retry loop would
        then spin forever re-attempting os.link() against the same
        dangling symlink (link(2) does not follow symlinks, so it always
        fails with FileExistsError) while holding the guard flock for the
        whole repository. lexists reports True for a dangling symlink, so
        it falls through to _read_lock below, whose _open_no_follow
        raises OSError("too many levels of symbolic links"), which is
        already wrapped as a MalformedLockError."""
        try:
            os.stat(self._path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

        try:
            data = _read_lock(self._path, directory_fd=directory_fd)
        except MalformedLockError as exc:
            return MalformedLockError(
                f"lock file at {self._path} is malformed and cannot be auto-recovered: {exc}"
            )

        holder_host = data.get("hostname", "")
        holder_pid = data.get("pid")
        holder_token = data.get("token", "")
        started_at = data.get("started_at", "unknown")
        operation = data.get("operation", "unknown")

        if holder_host != socket.gethostname():
            return RemoteLockError(
                f"lock is held by process {holder_pid} on host {holder_host!r} "
                f"(started {started_at}, operation={operation!r}); "
                "remote locks are never auto-recovered"
            )

        if not isinstance(holder_pid, int) or isinstance(holder_pid, bool):
            return MalformedLockError(f"lock at {self._path} has invalid pid {holder_pid!r}")

        if _pid_is_alive(holder_pid):
            return LockError(
                f"lock is held by local process {holder_pid} "
                f"(started {started_at}, operation={operation!r}); "
                "pass --recover-stale-lock only for demonstrably dead processes"
            )

        if not self._recover_stale:
            return StaleLockError(
                f"stale lock from dead process {holder_pid} "
                f"(started {started_at}, operation={operation!r}); "
                "pass --recover-stale-lock to remove it and retry"
            )

        try:
            current_data = _read_lock(self._path, directory_fd=directory_fd)
            if current_data.get("token") != holder_token:
                return None
        except MalformedLockError:
            return None

        try:
            os.unlink(self._path.name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass

        return None

    def release(self) -> None:
        """Release the lock. No-op if the lock was never acquired.

        Verifies ownership by token before deleting to prevent one process
        from releasing another process's lock. The check-then-unlink
        sequence runs under the guard flock so a successor cannot install
        a new lock between the token check and the unlink.

        Only forgets this instance's ownership token (self._token = None)
        once a *definitive* outcome has been reached: the path is already
        absent, the on-disk record names a different token, or the unlink
        of our own record succeeded. Any transient failure along the way
        (the guard cannot be opened, the lock file cannot be read, the
        record fails strict validation, or the unlink itself fails for a
        reason other than the file already being gone) instead raises
        LockError while *keeping* the token, so the caller can legitimately
        retry release() rather than the object silently forgetting it ever
        held the lock. Silently discarding ownership on a transient error
        would let a caller believe cleanup succeeded and, e.g., proceed to
        report shutdown as complete while the lock file is still present
        and still ours.
        """
        if self._token is None:
            return
        token = self._token

        try:
            with _guarded(self._path.parent.parent) as directory_fd:
                # lexists, not exists: see _inspect_existing_lock's
                # docstring. A dangling symlink at the lock path is not
                # "already gone" and must not make this instance silently
                # discard its ownership token.
                try:
                    os.stat(self._path.name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    self._token = None
                    return

                try:
                    data = _read_lock(self._path, directory_fd=directory_fd)
                except MalformedLockError as exc:
                    raise LockError(
                        f"cannot verify ownership of {self._path} before release: "
                        f"the on-disk record is malformed ({exc}); the lock may still "
                        "be held and requires manual inspection"
                    ) from exc

                if data.get("token") != token:
                    # A different (or absent) token means this instance no
                    # longer owns whatever is currently at this path —
                    # either a successor already recovered it as stale, or
                    # something else is direly wrong. Either way there is
                    # nothing this instance can meaningfully unlink.
                    self._token = None
                    return

                try:
                    os.unlink(self._path.name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise LockError(f"cannot remove lock file {self._path}: {exc}") from exc
        except LockError:
            raise
        except OSError as exc:
            raise LockError(f"cannot release lock {self._path}: {exc}") from exc
        else:
            self._token = None
