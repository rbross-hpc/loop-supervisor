"""Read-only, bounded observation of the repository supervisor lock."""

from __future__ import annotations

import contextlib
import os
import re
import socket
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..locking import _pid_is_alive
from ..state import StateError, load_state, validate_run_id
from .discovery import RunSummary
from .json_reader import BoundedJsonError, read_bounded_json


class LockActivity(StrEnum):
    """Repository-level lock evidence, in ADR 0036 precedence order."""

    ABSENT = "absent"
    MALFORMED = "malformed"
    REMOTE = "remote"
    STALE = "stale"
    MISMATCHED = "mismatched"
    FRESH_RUN_UNASSOCIATED = "fresh_run_unassociated"
    UNASSOCIATED = "unassociated"
    LOCAL_LIVE_ASSOCIATED = "local_live_associated"


class ActivityLabel(StrEnum):
    """The only per-run activity labels supported by present lock evidence."""

    RUNNING = "running"
    NOT_EVIDENCED_RUNNING = "not_evidenced_running"


@dataclass(frozen=True)
class RunActivity:
    """Activity evidence assigned to one discovered run."""

    run_id: str
    label: ActivityLabel


@dataclass(frozen=True)
class LockObservation:
    """Safe repository lock evidence, deliberately excluding its ownership token."""

    activity: LockActivity
    activities: tuple[RunActivity, ...]
    pid: int | None
    hostname: str | None
    started_at: str | None
    operation: str | None
    run_id: str | None
    integration_path: str | None
    diagnostic: str | None


_LOCK_FIELDS = frozenset(
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
_VALID_OPERATIONS = frozenset({"run", "resume", "tui"})
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def observe_lock(
    git_common_dir: Path, integration_path: Path, runs: tuple[RunSummary, ...] | list[RunSummary]
) -> LockObservation:
    """Read the supervisor lock once and classify it without mutating disk.

    The lock token is validated only as an on-disk schema requirement, then
    discarded before this function constructs any typed result.
    """
    run_list = tuple(runs)
    try:
        selected_path = _canonicalize(integration_path)
    except OSError as exc:
        return _observation(LockActivity.MISMATCHED, run_list, diagnostic=str(exc))

    try:
        record = _read_lock_record(git_common_dir)
    except FileNotFoundError:
        return _observation(LockActivity.ABSENT, run_list)
    except (BoundedJsonError, LockObservationError, OSError) as exc:
        return _observation(LockActivity.MALFORMED, run_list, diagnostic=str(exc))

    # ``_read_lock_record`` has removed token before returning this mapping.
    hostname = record["hostname"]
    pid = record["pid"]
    assert isinstance(hostname, str)
    assert isinstance(pid, int)
    if hostname != socket.gethostname():
        return _observation(LockActivity.REMOTE, run_list, record)
    if not _pid_is_alive(pid):
        return _observation(LockActivity.STALE, run_list, record)

    lock_path = record["integration_path"]
    if lock_path is None:
        return _observation(LockActivity.MISMATCHED, run_list, record)
    assert isinstance(lock_path, str)
    try:
        if _canonicalize(Path(lock_path)) != selected_path:
            return _observation(LockActivity.MISMATCHED, run_list, record)
    except OSError:
        return _observation(LockActivity.MISMATCHED, run_list, record)

    run_id = record["run_id"]
    if run_id is None:
        return _observation(LockActivity.FRESH_RUN_UNASSOCIATED, run_list, record)
    assert isinstance(run_id, str)
    summary = next((item for item in run_list if item.run_id == run_id and item.loadable), None)
    if summary is None:
        return _observation(LockActivity.UNASSOCIATED, run_list, record)

    try:
        state = load_state(git_common_dir, run_id)
        state_path = _canonicalize(Path(state.integration_path))
    except (StateError, OSError):
        return _observation(LockActivity.UNASSOCIATED, run_list, record)
    if state.run_id != run_id or state_path != selected_path:
        return _observation(LockActivity.MISMATCHED, run_list, record)
    return _observation(LockActivity.LOCAL_LIVE_ASSOCIATED, run_list, record, running_id=run_id)


class LockObservationError(RuntimeError):
    """A lock cannot safely support a read-model observation."""


@contextlib.contextmanager
def _open_lock_directory(git_common_dir: Path) -> Iterator[int]:
    directory = git_common_dir / "loop-supervisor"
    try:
        fd = os.open(directory, os.O_RDONLY | _directory_flags())
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            raise
        raise LockObservationError(f"cannot safely open lock directory {directory}: {exc}") from exc
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise LockObservationError(f"lock directory {directory} is not a directory")
        yield fd
    finally:
        os.close(fd)


def _directory_flags() -> int:
    directory = getattr(os, "O_DIRECTORY", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(directory, int) or not isinstance(nofollow, int):
        raise LockObservationError("secure lock reads require O_DIRECTORY and O_NOFOLLOW")
    return directory | nofollow


def _read_lock_record(git_common_dir: Path) -> dict[str, object]:
    """Read and validate one lock record, dropping its token before return."""
    path = git_common_dir / "loop-supervisor" / "supervisor.lock"
    with _open_lock_directory(git_common_dir) as directory_fd:
        data = read_bounded_json(directory_fd, path.name, path)
    if not isinstance(data, dict):
        raise LockObservationError("lock record must be a JSON object")
    _validate_lock_record(data)
    # Do not retain the ownership credential in a typed value or return mapping.
    del data["token"]
    return data


def _validate_lock_record(data: dict[str, Any]) -> None:
    if set(data) != _LOCK_FIELDS:
        raise LockObservationError("lock record has missing or unknown fields")
    if data["schema_version"] != 1:
        raise LockObservationError("lock record has an unsupported schema version")
    if not isinstance(data["token"], str) or not data["token"]:
        raise LockObservationError("lock record has an invalid ownership token")
    pid = data["pid"]
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise LockObservationError("lock record has an invalid pid")
    if not isinstance(data["hostname"], str) or not data["hostname"]:
        raise LockObservationError("lock record has an invalid hostname")
    started_at = data["started_at"]
    if not isinstance(started_at, str) or not _TIMESTAMP_RE.match(started_at):
        raise LockObservationError("lock record has an invalid started_at")
    if data["operation"] not in _VALID_OPERATIONS:
        raise LockObservationError("lock record has an invalid operation")
    run_id = data["run_id"]
    if run_id is not None:
        try:
            validate_run_id(run_id)
        except StateError as exc:
            raise LockObservationError("lock record has an invalid run_id") from exc
    integration_path = data["integration_path"]
    if integration_path is not None and (
        not isinstance(integration_path, str)
        or not integration_path
        or not os.path.isabs(integration_path)
    ):
        raise LockObservationError("lock record has an invalid integration_path")


def _canonicalize(path: Path) -> Path:
    return path.resolve(strict=True)


def _observation(
    activity: LockActivity,
    runs: tuple[RunSummary, ...],
    record: dict[str, object] | None = None,
    *,
    running_id: str | None = None,
    diagnostic: str | None = None,
) -> LockObservation:
    values = record or {}
    pid = values.get("pid")
    hostname = values.get("hostname")
    started_at = values.get("started_at")
    operation = values.get("operation")
    run_id = values.get("run_id")
    integration_path = values.get("integration_path")
    return LockObservation(
        activity=activity,
        activities=tuple(
            RunActivity(
                run_id=run.run_id,
                label=ActivityLabel.RUNNING
                if activity is LockActivity.LOCAL_LIVE_ASSOCIATED and run.run_id == running_id
                else ActivityLabel.NOT_EVIDENCED_RUNNING,
            )
            for run in runs
        ),
        pid=pid if isinstance(pid, int) else None,
        hostname=hostname if isinstance(hostname, str) else None,
        started_at=started_at if isinstance(started_at, str) else None,
        operation=operation if isinstance(operation, str) else None,
        run_id=run_id if isinstance(run_id, str) else None,
        integration_path=integration_path if isinstance(integration_path, str) else None,
        diagnostic=diagnostic,
    )
