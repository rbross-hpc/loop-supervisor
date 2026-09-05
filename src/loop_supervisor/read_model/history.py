"""Secure, typed best-effort loading of append-only phase history."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ValidationError

from ..contracts import ArchitectResult, AuditorResult, BuilderResult, PlannerResult
from ..phases import ALL_PHASES
from ..state import (
    OperationalErrorRecord,
    StateError,
    validate_run_id,
    validate_verification_result,
)
from ..supervisor import AdvanceStatus
from .json_reader import BoundedJsonError, read_bounded_json
from .record_detail import format_opinionated_content, serialize_raw_json

_HISTORY_NAME_RE = re.compile(r"^(?P<seq>[0-9]{4,})-(?P<phase>[a-z_]+)\.json$")
_MAX_SEQUENCE_DIGITS = 128
"""Maximum filename sequence digits accepted for bounded numeric processing."""
_MAX_HISTORY_LEAVES = 10_000
"""Maximum directory entries inspected for a single run's history."""
_MAX_DIAGNOSTIC_ARTIFACT_LENGTH = 256
_COUNTER_FIELDS = frozenset(
    {
        "accepted_task_count",
        "revision_count",
        "replan_count",
        "architect_retry_count",
        "builder_guidance_count",
    }
)
_RESET_TRANSITIONS_BY_COUNTER: dict[str, frozenset[tuple[str, str]]] = {
    "revision_count": frozenset(
        {
            ("planning", "building"),
            ("planning", "architecting"),
            ("creating_worktree", "building"),
            ("creating_worktree", "architecting"),
            ("cleanup_branch", "planning"),
        }
    ),
    "replan_count": frozenset({("cleanup_branch", "planning")}),
    "architect_retry_count": frozenset(
        {
            ("recording_decision", "building"),
            ("recording_decision", "planning"),
            ("cleanup_branch", "planning"),
        }
    ),
    "builder_guidance_count": frozenset(
        {
            ("planning", "building"),
            ("planning", "architecting"),
            ("creating_worktree", "building"),
            ("creating_worktree", "architecting"),
            ("building", "verifying"),
            ("building", "auditing"),
            ("cleanup_branch", "planning"),
        }
    ),
}
_RECORD_FIELDS = frozenset(
    {
        "seq",
        "run_id",
        "phase",
        "phase_after",
        "status",
        "recorded_at",
        "original_task_id",
        "counters",
        "result",
        "error",
    }
)
_RESULT_VALIDATORS: dict[str, type[BaseModel]] = {
    "planning": PlannerResult,
    "architecting": ArchitectResult,
    "building": BuilderResult,
    "auditing": AuditorResult,
}


class HistoryStatus(StrEnum):
    """The available history evidence, without inferring omitted transitions."""

    ABSENT = "absent"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class HistoryEntry:
    """One validated phase outcome record."""

    seq: int
    phase: str
    phase_after: str
    status: AdvanceStatus
    recorded_at: str
    counters: Mapping[str, int]
    original_task_id: str | None
    has_result: bool
    has_error: bool
    result_detail: str | None
    error_detail: str | None
    raw_json: str
    raw_json_truncated: bool


@dataclass(frozen=True)
class HistoryDiagnostic:
    """A safe logical artifact name and reason for omitted history evidence."""

    artifact: str
    reason: str
    seq: int | None = None


@dataclass(frozen=True)
class HistoryLoad:
    """Validated history entries and the completeness of their disk evidence."""

    entries: tuple[HistoryEntry, ...]
    completeness: HistoryStatus
    diagnostics: tuple[HistoryDiagnostic, ...]


def load_history(git_common_dir: Path, run_id: str) -> HistoryLoad:
    """Load one run's history without following directories or artifact leaves.

    A missing history directory is unavailable evidence. Once a real directory
    is observed, every rejected, disappearing, duplicate, or missing sequence
    makes the best-effort result incomplete while valid siblings remain useful.
    """
    validated_run_id = validate_run_id(run_id)
    display_directory = git_common_dir / "loop-supervisor" / "runs" / validated_run_id
    try:
        with _open_history_directory(git_common_dir, validated_run_id) as directory_fd:
            names, has_excess = _enumerate_history_names(directory_fd)
            return _load_enumerated(
                directory_fd, names, has_excess, display_directory, validated_run_id
            )
    except FileNotFoundError:
        return HistoryLoad((), HistoryStatus.ABSENT, ())
    except OSError:
        diagnostic = HistoryDiagnostic(validated_run_id, "history directory is unavailable")
        return HistoryLoad((), HistoryStatus.INCOMPLETE, (diagnostic,))


def _enumerate_history_names(directory_fd: int) -> tuple[list[str], bool]:
    """Return at most the configured leaf count and whether more were observed."""
    names: list[str] = []
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            if len(names) == _MAX_HISTORY_LEAVES:
                return names, True
            names.append(entry.name)
    return names, False


@contextmanager
def _open_history_directory(git_common_dir: Path, run_id: str) -> Iterator[int]:
    flags = os.O_RDONLY | _required_open_flag("O_DIRECTORY") | _required_open_flag("O_NOFOLLOW")
    supervisor_path = git_common_dir / "loop-supervisor"
    try:
        supervisor_fd = os.open(supervisor_path, flags)
    except FileNotFoundError:
        raise
    try:
        runs_fd = os.open("runs", flags, dir_fd=supervisor_fd)
        try:
            directory_fd = os.open(run_id, flags, dir_fd=runs_fd)
            try:
                if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
                    raise OSError("history target is not a directory")
                yield directory_fd
            finally:
                os.close(directory_fd)
        finally:
            os.close(runs_fd)
    finally:
        os.close(supervisor_fd)


def _required_open_flag(name: str) -> int:
    value = getattr(os, name, None)
    if not isinstance(value, int):
        raise OSError(f"secure history reads require os.{name}")
    return value


def _load_enumerated(
    directory_fd: int,
    names: list[str],
    has_excess: bool,
    display_directory: Path,
    run_id: str,
) -> HistoryLoad:
    if not names and not has_excess:
        return HistoryLoad((), HistoryStatus.ABSENT, ())

    diagnostics: list[HistoryDiagnostic] = []
    if has_excess:
        diagnostics.append(HistoryDiagnostic(run_id, "history directory has excess entries"))
    candidates: dict[int, list[tuple[str, str | None]]] = {}
    for name in names:
        artifact = _safe_artifact_name(name)
        match = _HISTORY_NAME_RE.fullmatch(name)
        if match is None:
            diagnostics.append(HistoryDiagnostic(artifact, "invalid history filename"))
            continue
        parsed_seq = _parse_filename_sequence(match["seq"])
        if parsed_seq is None:
            diagnostics.append(HistoryDiagnostic(artifact, "filename sequence is too large"))
            continue
        phase = match["phase"]
        if parsed_seq <= 0:
            diagnostics.append(
                HistoryDiagnostic(artifact, "filename has invalid sequence", parsed_seq)
            )
            continue
        candidates.setdefault(parsed_seq, []).append((name, phase if phase in ALL_PHASES else None))

    entries: list[HistoryEntry] = []
    for seq in sorted(candidates):
        records = candidates[seq]
        if len(records) != 1:
            diagnostics.append(
                HistoryDiagnostic(
                    _duplicate_artifact_name(name for name, _ in records),
                    f"duplicate sequence {seq} conflict",
                    seq,
                )
            )
            continue
        name, phase = records[0]
        if phase is None:
            diagnostics.append(
                HistoryDiagnostic(_safe_artifact_name(name), "filename has unknown phase", seq)
            )
            continue
        try:
            raw = read_bounded_json(directory_fd, name, display_directory / name)
            entries.append(_validate_record(raw, run_id, seq, phase))
        except BoundedJsonError as exc:
            diagnostics.append(
                HistoryDiagnostic(_safe_artifact_name(name), _bounded_json_reason(exc), seq)
            )
        except (StateError, ValueError, ValidationError) as exc:
            diagnostics.append(
                HistoryDiagnostic(_safe_artifact_name(name), _validation_reason(exc), seq)
            )

    _append_gap_diagnostics(entries, diagnostics)
    _append_adjacent_contradiction_diagnostics(entries, diagnostics)
    completeness = HistoryStatus.COMPLETE if not diagnostics else HistoryStatus.INCOMPLETE
    return HistoryLoad(tuple(entries), completeness, tuple(diagnostics))


def _parse_filename_sequence(value: str) -> int | None:
    """Return a bounded numeric filename sequence without huge-int conversion."""
    if len(value) > _MAX_SEQUENCE_DIGITS:
        return None
    return int(value)


def _safe_artifact_name(name: str) -> str:
    """Bound a diagnostic's untrusted logical artifact name."""
    if len(name) <= _MAX_DIAGNOSTIC_ARTIFACT_LENGTH:
        return name
    return f"{name[: _MAX_DIAGNOSTIC_ARTIFACT_LENGTH - 3]}..."


def _duplicate_artifact_name(names: Iterator[str]) -> str:
    """Return bounded logical names for a conflicting sequence's artifacts."""
    return _safe_artifact_name(", ".join(sorted(_safe_artifact_name(name) for name in names)))


def _bounded_json_reason(exc: BoundedJsonError) -> str:
    """Classify JSON reader failures without exposing its path-bearing text."""
    message = str(exc)
    if "not a regular file" in message:
        return "history record is not a regular file"
    if "securely open" in message:
        return "history record could not be opened"
    return "malformed history record"


def _validation_reason(exc: StateError | ValueError | ValidationError) -> str:
    """Classify controlled validation failures without rendering untrusted values."""
    if isinstance(exc, ValidationError):
        return "history record has invalid result or error"
    message = str(exc)
    if message.startswith("embedded "):
        return message
    return "malformed history record"


def _append_gap_diagnostics(
    entries: list[HistoryEntry], diagnostics: list[HistoryDiagnostic]
) -> None:
    sequences = sorted(entry.seq for entry in entries)
    if not sequences:
        return
    expected = 1
    for seq in sequences:
        if seq > expected:
            if seq == expected + 1:
                reason = f"sequence gap at {expected}"
            else:
                reason = f"sequence gap from {expected} to {seq - 1}"
            diagnostics.append(HistoryDiagnostic(str(expected), reason, expected))
        expected = seq + 1


def _append_adjacent_contradiction_diagnostics(
    entries: list[HistoryEntry], diagnostics: list[HistoryDiagnostic]
) -> None:
    """Report contradictions between successive valid records without omitting either."""
    for preceding, following in zip(entries, entries[1:], strict=False):
        if following.seq != preceding.seq + 1:
            continue
        if preceding.phase_after != following.phase:
            diagnostics.append(
                HistoryDiagnostic(
                    str(following.seq),
                    f"phase discontinuity after sequence {preceding.seq}",
                    following.seq,
                )
            )
        if _parse_recorded_at(preceding.recorded_at) > _parse_recorded_at(following.recorded_at):
            diagnostics.append(
                HistoryDiagnostic(
                    str(following.seq),
                    f"recorded timestamp reversal after sequence {preceding.seq}",
                    following.seq,
                )
            )
        for field in sorted(_COUNTER_FIELDS):
            is_regression = following.counters[field] < preceding.counters[field]
            if is_regression and not _is_permitted_counter_reset(field, following):
                diagnostics.append(
                    HistoryDiagnostic(
                        str(following.seq),
                        f"counter regression for {field} after sequence {preceding.seq}",
                        following.seq,
                    )
                )


def _is_permitted_counter_reset(field: str, following: HistoryEntry) -> bool:
    """Return whether a decreased counter is a documented reset on this transition."""
    transition = (following.phase, following.phase_after)
    if following.counters[field] != 0 or transition not in _RESET_TRANSITIONS_BY_COUNTER.get(
        field, frozenset()
    ):
        return False
    return not (
        field == "builder_guidance_count"
        and transition in {("building", "verifying"), ("building", "auditing")}
        and following.status is not AdvanceStatus.ADVANCED
    )


def _parse_recorded_at(recorded_at: str) -> datetime:
    """Parse a timestamp already validated while constructing a history entry."""
    return datetime.fromisoformat(recorded_at)


def _validate_record(raw: Any, run_id: str, filename_seq: int, filename_phase: str) -> HistoryEntry:
    if not isinstance(raw, dict) or set(raw) != _RECORD_FIELDS:
        raise ValueError("record must be an object with exactly the required fields")
    seq = raw["seq"]
    if not _positive_int(seq):
        raise ValueError("seq must be a positive integer")
    if seq != filename_seq:
        raise ValueError("embedded seq does not match filename")
    if raw["run_id"] != run_id:
        raise ValueError("embedded run_id does not match selected run")
    phase = _validated_phase(raw["phase"], "phase")
    if phase != filename_phase:
        raise ValueError("embedded phase does not match filename")
    phase_after = _validated_phase(raw["phase_after"], "phase_after")
    try:
        status = AdvanceStatus(raw["status"])
    except (TypeError, ValueError) as exc:
        raise ValueError("status is not a known advance status") from exc
    recorded_at = raw["recorded_at"]
    if not isinstance(recorded_at, str) or not recorded_at:
        raise ValueError("recorded_at must be a non-empty ISO-8601 string")
    try:
        parsed_at = datetime.fromisoformat(recorded_at)
    except ValueError as exc:
        raise ValueError("recorded_at must be ISO-8601") from exc
    if parsed_at.tzinfo is None or parsed_at.utcoffset() is None:
        raise ValueError("recorded_at must be timezone-aware")
    original_task_id = raw["original_task_id"]
    if original_task_id is not None and (
        not isinstance(original_task_id, str) or not original_task_id
    ):
        raise ValueError("original_task_id must be null or a non-empty string")
    counters = raw["counters"]
    if not isinstance(counters, dict) or set(counters) != _COUNTER_FIELDS:
        raise ValueError("counters must contain exactly the five known counters")
    if not all(_non_negative_int(value) for value in counters.values()):
        raise ValueError("counters must be non-negative integers")
    result = raw["result"]
    error = raw["error"]
    _validate_result(phase, result)
    if error is not None:
        if not isinstance(error, dict):
            raise ValueError("error must be an object or null")
        OperationalErrorRecord.from_dict(error)
    raw_json, raw_json_truncated = serialize_raw_json(raw)
    return HistoryEntry(
        seq=seq,
        phase=phase,
        phase_after=phase_after,
        status=status,
        recorded_at=recorded_at,
        counters=MappingProxyType(dict(counters)),
        original_task_id=original_task_id,
        has_result=result is not None,
        has_error=error is not None,
        result_detail=format_opinionated_content(result) if result is not None else None,
        error_detail=format_opinionated_content(error) if error is not None else None,
        raw_json=raw_json,
        raw_json_truncated=raw_json_truncated,
    )


def _validated_phase(value: object, name: str) -> str:
    if not isinstance(value, str) or value not in ALL_PHASES:
        raise ValueError(f"{name} is not a known phase")
    return value


def _validate_result(phase: str, result: object) -> None:
    validator = _RESULT_VALIDATORS.get(phase)
    if validator is None:
        if phase == "verifying":
            if result is not None:
                validate_verification_result(result)
        elif result is not None:
            raise ValueError(f"phase {phase!r} cannot have a result")
        return
    if result is not None:
        if not isinstance(result, dict):
            raise ValueError("result must be an object or null")
        validator.model_validate(result)


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
