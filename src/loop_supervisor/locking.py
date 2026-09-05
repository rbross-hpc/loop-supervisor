"""Supervisor repository lock.

Prevents two mutating supervisor processes (run, resume) from racing
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
from enum import StrEnum
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


_SCHEMA_VERSION = 2
# "tui" is retained for back-compat with lock records written by the
# now-retired in-process TUI; nothing currently writes it.
_VALID_OPERATIONS = frozenset({"run", "resume", "tui"})
_LEGACY_LOCK_RECORD_FIELDS = frozenset(
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
_LOCK_RECORD_FIELDS = _LEGACY_LOCK_RECORD_FIELDS | frozenset(
    {"owner_boot_id", "owner_process_start"}
)


def _read_boot_id() -> str:
    """Read Linux's stable per-boot identifier as an opaque string.

    Raises ``LockError`` (never a bare ``OSError``) so every caller
    (acquisition and stale-owner identity classification alike) can rely
    on one exception type for "cannot read local kernel identity".
    """
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError as exc:
        raise LockError(f"cannot read kernel boot ID: {exc}") from exc
    if not boot_id:
        raise LockError("cannot read kernel boot ID: value is empty")
    return boot_id


def _read_process_start(pid: int) -> str:
    """Read ``pid``'s process-start ticks from ``/proc/<pid>/stat``.

    Deliberately opaque: compared exactly, never converted to a wall-clock
    timestamp. Raises ``FileNotFoundError`` when ``pid`` does not currently
    exist; a mid-read ``ProcessLookupError`` is normalized to that same
    absent-PID signal. Callers distinguish "no such process" (stale evidence,
    including PID reuse of the *recorded* PID once it exits) from "process
    exists but its identity could not be read" (unverifiable, never treated
    as proof of either liveness or staleness). Any other read failure raises
    ``LockError``.
    """
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        raise
    except ProcessLookupError as exc:
        raise FileNotFoundError(f"/proc/{pid}/stat") from exc
    except Exception as exc:
        raise LockError(f"cannot read process start ticks for PID {pid}: {exc}") from exc
    _, separator, remainder = stat_text.rpartition(")")
    fields = remainder.split()
    try:
        process_start = fields[19]
    except IndexError as exc:
        raise LockError(f"cannot read process start ticks for PID {pid}: {exc}") from exc
    if not separator or not process_start:
        raise LockError(f"cannot read process start ticks for PID {pid}")
    return process_start


def _read_kernel_owner_identity(pid: int) -> tuple[str, str]:
    """Read Linux's stable boot and process-start identifiers for ``pid``.

    Values are deliberately opaque strings: callers compare them exactly and
    never turn the process-start ticks into a wall-clock timestamp. Used
    only by ``acquire()`` for the acquiring process's own identity, where
    ``pid`` is always ``os.getpid()`` and therefore always exists.
    """
    boot_id = _read_boot_id()
    try:
        process_start = _read_process_start(pid)
    except FileNotFoundError as exc:
        raise LockError(f"cannot read kernel owner identity for PID {pid}: {exc}") from exc
    return boot_id, process_start


class IdentityStatus(StrEnum):
    """The result of comparing a schema-2 lock's immutable owner identity
    against the current local kernel state.

    Only meaningful once a lock record's hostname has already been
    confirmed local; boot ID and process-start ticks are host-local
    identifiers with no cross-host meaning.
    """

    MATCHING = "matching"
    """The named PID exists and both boot ID and process-start ticks
    exactly match the recorded owner identity: the same process that
    wrote this record is still running."""

    STALE = "stale"
    """A boot mismatch, absent PID, or process-start mismatch (including
    PID reuse): the recorded owner cannot still be running."""

    UNVERIFIABLE = "unverifiable"
    """Local kernel identity could not be read or compared. Never treated
    as proof of either liveness or staleness."""


def classify_local_owner_identity(
    pid: int, owner_boot_id: str, owner_process_start: str
) -> IdentityStatus:
    """Classify a schema-2 lock's recorded owner identity against the
    current local kernel state, per ADR 0037's identity-chain test.

    Compares boot ID and process-start ticks as opaque exact values, never
    wall-clock time or recency. Used by stale-lock recovery and the read
    model's activity classification so both apply ADR 0037's identity-chain
    test consistently.
    """
    try:
        current_boot_id = _read_boot_id()
    except LockError:
        return IdentityStatus.UNVERIFIABLE
    if current_boot_id != owner_boot_id:
        return IdentityStatus.STALE
    try:
        current_process_start = _read_process_start(pid)
    except FileNotFoundError:
        return IdentityStatus.STALE
    except LockError:
        return IdentityStatus.UNVERIFIABLE
    if current_process_start != owner_process_start:
        return IdentityStatus.STALE
    return IdentityStatus.MATCHING


# Matches time.strftime("%Y-%m-%dT%H:%M:%SZ", ...): a fixed-width UTC
# timestamp, deliberately not full ISO-8601 parsing since this is the
# exact (and only) format acquire() ever writes.
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _lock_path(git_common_dir: Path) -> Path:
    return git_common_dir / "loop-supervisor" / "supervisor.lock"


def _guard_path(git_common_dir: Path) -> Path:
    return git_common_dir / "loop-supervisor" / "supervisor.lock.guard"


def lock_is_present(git_common_dir: Path) -> bool:
    """True if a lock file exists at all, valid or not, live or stale.

    Deliberately coarser than `SupervisorLock.acquire`'s stale-owner
    recovery logic: a caller that only wants to know "is it safe to
    delete run history right now" (e.g. `loop-supervisor runs prune`)
    should refuse whenever *any* lock record is present, rather than
    attempt to classify it as live/stale/remote/malformed itself and
    risk deleting history for a run whose lock this check misjudged.
    """
    return _lock_path(git_common_dir).exists()


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
    except OSError as exc:
        raise LockError(f"cannot use lock directory {directory}: {exc}") from exc
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

    schema_version = data.get("schema_version")
    # Strict integer identity, not equality: bool is an int subclass and
    # float(1.0) == 1, so plain `==` would accept `schema_version: true` or
    # `schema_version: 2.0` as a valid version. A written or read lock
    # record must always carry a real integer schema version.
    if type(schema_version) is not int:
        raise MalformedLockError(
            f"lock record field 'schema_version' must be an integer, got {schema_version!r}"
        )
    if schema_version == 1:
        expected_fields = _LEGACY_LOCK_RECORD_FIELDS
    elif schema_version == _SCHEMA_VERSION:
        expected_fields = _LOCK_RECORD_FIELDS
    else:
        raise MalformedLockError(
            "lock record has unsupported schema_version "
            f"{schema_version!r} (expected 1 or {_SCHEMA_VERSION})"
        )
    unknown = set(data) - expected_fields
    if unknown:
        raise MalformedLockError(f"lock record contains unknown fields: {sorted(unknown)}")
    missing = expected_fields - set(data)
    if missing:
        raise MalformedLockError(f"lock record is missing required fields: {sorted(missing)}")

    if schema_version == _SCHEMA_VERSION:
        for field in ("owner_boot_id", "owner_process_start"):
            value = data.get(field)
            if not isinstance(value, str) or not value:
                raise MalformedLockError(f"lock record field '{field}' must be a non-empty string")

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
    fails this validation is never treated as a recoverable stale-owner
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


def _write_lock_file(
    path: Path, record: dict[str, Any], *, directory_fd: int, replace: bool = False
) -> None:
    """Atomically create or replace a mode-0600 lock record.

    Creation uses link(2)'s create-if-absent behavior. A guarded owner update
    uses rename replacement instead, so readers see either the complete old
    record or the complete new record, never an unlinked interval or partial
    JSON file.
    """
    tmp_name = f".tmp-lock-{uuid.uuid4().hex}.json"
    fd = _open_no_follow(
        Path(tmp_name), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=directory_fd
    )
    fd_owned = True
    try:
        os.fchmod(fd, 0o600)
        handle = os.fdopen(fd, "w")
        fd_owned = False
        with handle:
            json.dump(record, handle, indent=2)
            handle.write("\n")
        if replace:
            os.replace(tmp_name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        else:
            os.link(
                tmp_name,
                path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
    finally:
        try:
            if fd_owned:
                os.close(fd)
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
        Human-readable label for what this process is doing ("run", "resume").
    run_id:
        Optional run ID being operated on.
    integration_path:
        Absolute path to the integration worktree.
    recover_stale:
        If True and the recorded owner is demonstrably stale, remove the stale
        lock and retry. For schema-2 locks, the recorded owner can be stale
        while the numeric PID names a live successor after PID reuse or a
        reboot that reused the PID. Never auto-recovers remote or malformed
        locks.
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
        self._owner_boot_id: str | None = None
        self._owner_process_start: str | None = None

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
        pid = os.getpid()
        owner_boot_id, owner_process_start = _read_kernel_owner_identity(pid)
        record: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "token": token,
            "pid": pid,
            "owner_boot_id": owner_boot_id,
            "owner_process_start": owner_process_start,
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

        try:
            with _guarded(self._path.parent.parent) as directory_fd:
                while True:
                    try:
                        _write_lock_file(self._path, record, directory_fd=directory_fd)
                        self._token = token
                        self._owner_boot_id = owner_boot_id
                        self._owner_process_start = owner_process_start
                        return
                    except FileExistsError:
                        pass

                    existing = self._inspect_existing_lock(directory_fd=directory_fd)
                    if existing is None:
                        continue

                    self._token = None
                    raise existing
        except LockError:
            self._token = None
            raise
        except OSError as exc:
            self._token = None
            raise LockError(f"cannot acquire lock {self._path}: {exc}") from exc

    def bind_run_id(self, run_id: str) -> None:
        """Bind a newly created run to this lock's existing ownership record.

        The binding is deliberately narrow: under the guard, the current
        record must still name this exact token and immutable kernel identity,
        and may only change from a null run ID to ``run_id``. Repeating the
        same binding is harmless; replacing a different bound ID fails closed.
        """
        if self._token is None:
            raise LockError("cannot bind run ID: this lock is not acquired")
        try:
            validate_run_id(run_id)
        except Exception as exc:
            raise LockError(f"cannot bind invalid run ID {run_id!r}: {exc}") from exc

        token = self._token
        try:
            with _guarded(self._path.parent.parent) as directory_fd:
                try:
                    record = _read_lock(self._path, directory_fd=directory_fd)
                except FileNotFoundError as exc:
                    raise LockError("cannot bind run ID: lock record is absent") from exc
                except MalformedLockError as exc:
                    raise LockError(f"cannot bind run ID: lock record is malformed: {exc}") from exc

                if record["token"] != token:
                    raise LockError("cannot bind run ID: ownership token no longer matches")
                if (
                    record.get("owner_boot_id") != self._owner_boot_id
                    or record.get("owner_process_start") != self._owner_process_start
                ):
                    raise LockError(
                        "cannot bind run ID: immutable owner identity no longer matches"
                    )

                existing_run_id = record["run_id"]
                if existing_run_id == run_id:
                    return
                if existing_run_id is not None:
                    raise LockError(
                        f"cannot bind run ID: lock is already bound to {existing_run_id!r}"
                    )

                replacement = dict(record)
                replacement["run_id"] = run_id
                _write_lock_file(self._path, replacement, directory_fd=directory_fd, replace=True)
        except LockError:
            raise
        except OSError as exc:
            raise LockError(f"cannot bind run ID into lock {self._path}: {exc}") from exc

    def _inspect_existing_lock(self, *, directory_fd: int) -> LockError | None:
        """Inspect the existing lock and decide what to do.

        Returns None if the lock disappeared (retry), or a LockError
        if acquisition should fail. For schema-2 records, stale means the
        recorded owner no longer matches the current kernel identity; its
        numeric PID can therefore name a live successor after PID reuse or a
        reboot that reused the PID. Schema-1 records retain PID-only checks.

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

        is_stale = self._is_holder_stale(data, holder_pid)
        if is_stale is None:
            return LockError(
                f"lock names local process {holder_pid} "
                f"(started {started_at}, operation={operation!r}), but its identity "
                "could not be verified against the current kernel state; refusing to "
                "guess whether it is alive or stale"
            )
        if not is_stale:
            return LockError(
                f"lock is held by local process {holder_pid} "
                f"(started {started_at}, operation={operation!r}); "
                "pass --recover-stale-lock only for a demonstrably stale recorded owner"
            )

        if not self._recover_stale:
            return StaleLockError(
                f"stale lock whose recorded owner was process {holder_pid} "
                f"(started {started_at}, operation={operation!r}); "
                "the PID may now name a live successor after PID reuse or a reboot that reused it; "
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

    @staticmethod
    def _is_holder_stale(data: dict[str, Any], holder_pid: int) -> bool | None:
        """Decide whether ``data``'s recorded owner is demonstrably stale.

        Returns True (stale), False (the recorded owner still matches), or
        None (unverifiable -- never treated as proof of either). Schema-1
        records retain the original PID-only liveness check for backward
        compatibility, since they carry no immutable owner identity to
        compare. Schema-2 records use the full identity chain (ADR 0037): a
        live PID whose boot ID or process-start ticks no longer match the
        recorded owner (including PID reuse or a reboot that reused the PID)
        is stale, not live, even though ``os.kill(pid, 0)`` would otherwise
        succeed.
        """
        if data.get("schema_version") != _SCHEMA_VERSION:
            return not _pid_is_alive(holder_pid)

        owner_boot_id = data.get("owner_boot_id")
        owner_process_start = data.get("owner_process_start")
        assert isinstance(owner_boot_id, str) and isinstance(owner_process_start, str)
        status = classify_local_owner_identity(holder_pid, owner_boot_id, owner_process_start)
        if status is IdentityStatus.UNVERIFIABLE:
            return None
        return status is IdentityStatus.STALE

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
