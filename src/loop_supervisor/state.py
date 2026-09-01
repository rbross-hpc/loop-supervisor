"""Resumable supervisor state, persisted under the repository's shared Git
metadata directory (`git rev-parse --git-common-dir`), not inside the
tracked worktree. This keeps run state out of the project's own history
while remaining local to the clone and shared across linked worktrees.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import stat
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from .contracts import ArchitectResult, AuditorResult, BuilderResult, PlannerResult
from .phases import (
    ALL_PHASES,
    PHASE_OPERATIONAL_FAILURE,
    RETRY_TARGET_PHASES,
)

# Reset to 1 (backlog item 30): this project has no users and no
# production installs, so every RunState document ever persisted was
# created by this same development codebase. The prior v1->v2->v3
# migration history was pure carrying cost -- real code, real tests, and
# a real audit surface purchased for compatibility nobody needed -- and
# has been deleted rather than carried forward again. There is
# deliberately no migration path into this version: any document that
# does not already carry schema_version == 1 in this exact shape is
# rejected, and starting a new run is the only supported recovery. If
# this project ever acquires a real installed user base whose in-flight
# run state must survive an upgrade, that is the point to start taking
# migrations seriously again -- see backlog item 30's resolution note.
STATE_SCHEMA_VERSION = 1

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_RUN_ID_LENGTH = 128


class StateError(RuntimeError):
    """Raised for invalid or inconsistent resumable state."""


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def validate_run_id(run_id: object) -> str:
    """Validate a run ID before it is ever used to construct a filesystem
    path. Applied to every caller-supplied ID (CLI/TUI arguments) and every
    embedded ``RunState.run_id`` before it can be used as a save/load
    target, so a crafted ID can never escape the runs directory or
    overwrite an unrelated file.

    Accepts the generator's 12-character lowercase hex form, but is not
    restricted to it: existing tests and any historically saved runs may
    use other readable identifiers (e.g. ``run-1``). Rejects anything that
    could act as a path component boundary or traversal segment.
    """
    if not isinstance(run_id, str) or not run_id:
        raise StateError(f"run_id must be a non-empty string, got {run_id!r}")
    if len(run_id) > _MAX_RUN_ID_LENGTH:
        raise StateError(f"run_id exceeds maximum length of {_MAX_RUN_ID_LENGTH}: {run_id!r}")
    if run_id in (".", ".."):
        raise StateError(f"run_id must not be '.' or '..': {run_id!r}")
    if not _RUN_ID_RE.match(run_id):
        raise StateError(
            f"run_id {run_id!r} contains characters that are not allowed "
            "(must start with an alphanumeric character and contain only "
            "letters, digits, '.', '_', or '-')"
        )
    return run_id


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class RunOptions:
    """Immutable run-defining configuration, captured once at
    `start_new_run()` and never changed by resume. Resume must reconstruct
    its behavior entirely from these persisted values, not from whatever
    CLI flags happen to be passed to `resume`."""

    max_accepted_tasks: int
    max_revisions_per_task: int
    max_replans_per_task: int
    max_architect_retries: int
    malformed_output_retries: int
    role_timeout: float
    worktree_root: str | None
    require_decision_approval: bool
    opencode_executable: str
    opencode_startup_timeout: float
    provision_commands: tuple[str, ...]
    provision_timeout: float
    verify_commands: tuple[str, ...]
    verify_timeout: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunOptions:
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise StateError(f"run options contain unknown fields: {sorted(unknown)}")
        missing = known - set(data)
        if missing:
            raise StateError(f"run options are missing required fields: {sorted(missing)}")

        # Options are immutable safety limits (ADR 0006). Persisting them
        # without validating their *values* would not enforce that
        # guarantee: a tampered or corrupted file could carry a negative
        # limit, a non-finite timeout, or a wrong-typed flag that only
        # surfaces much later, inside a limit comparison or a Path()/HTTP
        # call, long after OpenCode has been started.
        for name in (
            "max_accepted_tasks",
            "max_revisions_per_task",
            "max_replans_per_task",
            "max_architect_retries",
            "malformed_output_retries",
        ):
            value = data[name]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise StateError(
                    f"run option {name!r} must be a non-negative integer, got {value!r}"
                )

        for name in ("role_timeout", "opencode_startup_timeout"):
            value = data[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise StateError(f"run option {name!r} must be a number, got {value!r}")
            if not math.isfinite(value) or value <= 0:
                raise StateError(
                    f"run option {name!r} must be a finite positive number, got {value!r}"
                )

        if not isinstance(data["require_decision_approval"], bool):
            raise StateError(
                "run option 'require_decision_approval' must be a bool, got "
                f"{data['require_decision_approval']!r}"
            )

        executable = data["opencode_executable"]
        if not isinstance(executable, str) or not executable:
            raise StateError("run option 'opencode_executable' must be a non-empty string")

        worktree_root = data["worktree_root"]
        if worktree_root is not None and (not isinstance(worktree_root, str) or not worktree_root):
            raise StateError("run option 'worktree_root' must be null or a non-empty string")

        for name in ("provision_commands", "verify_commands"):
            value = data[name]
            if not isinstance(value, (list, tuple)) or not all(
                isinstance(item, str) and item for item in value
            ):
                raise StateError(
                    f"run option {name!r} must be a list of non-empty strings, got {value!r}"
                )
            data[name] = tuple(value)

        for name in ("provision_timeout", "verify_timeout"):
            value = data[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise StateError(f"run option {name!r} must be a number, got {value!r}")
            if not math.isfinite(value) or value <= 0:
                raise StateError(
                    f"run option {name!r} must be a finite positive number, got {value!r}"
                )

        return cls(**data)


@dataclass(frozen=True)
class DecisionRequest:
    """A durable, source-independent record of an escalated design
    decision. Represented explicitly rather than derived from
    `planner_result`/`auditor_result` so that architecting, approval, and
    the post-decision continuation all agree on where the decision came
    from, across process restarts."""

    origin: str  # "planner" or "auditor"
    question: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DecisionRequest:
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise StateError(f"decision request contains unknown fields: {sorted(unknown)}")
        missing = known - set(data)
        if missing:
            raise StateError(f"decision request is missing required fields: {sorted(missing)}")
        if data["origin"] not in ("planner", "auditor"):
            raise StateError(f"decision request has invalid origin: {data['origin']!r}")
        for name in ("question", "rationale"):
            if not isinstance(data[name], str) or not data[name]:
                raise StateError(f"decision request field {name!r} must be a non-empty string")
        return cls(**data)


@dataclass(frozen=True)
class OperationalErrorRecord:
    """A sanitized, durable record of an operational failure.

    Never contains tracebacks, full request payloads, or authorization
    headers. Known-secret environment variable values and common
    credential formats in `message` are redacted on a best-effort basis
    by supervisor.py's `_sanitize_message()`; this is not a guarantee
    against arbitrary sensitive content (see ADR 0009's Consequences).
    """

    error_id: str
    kind: str
    operation: str
    failed_phase: str
    retry_phase: str | None
    exception_type: str
    message: str
    retryable: bool
    requires_repair: bool
    recovery_hint: str | None
    occurred_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OperationalErrorRecord:
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise StateError(f"error record contains unknown fields: {sorted(unknown)}")
        missing = known - set(data)
        if missing:
            raise StateError(f"error record is missing required fields: {sorted(missing)}")

        def _require_str(name: str, *, allow_empty: bool = False) -> None:
            value = data[name]
            if not isinstance(value, str) or (not allow_empty and not value):
                raise StateError(f"error record field {name!r} must be a non-empty string")

        for name in ("error_id", "kind", "operation", "exception_type", "message"):
            _require_str(name)

        failed_phase = data["failed_phase"]
        if failed_phase not in ALL_PHASES:
            raise StateError(f"error record has unknown failed_phase: {failed_phase!r}")

        for name in ("retryable", "requires_repair"):
            value = data[name]
            if not isinstance(value, bool):
                raise StateError(f"error record field {name!r} must be a bool, got {value!r}")

        retryable = data["retryable"]
        requires_repair = data["requires_repair"]
        if requires_repair and not retryable:
            raise StateError("error record has requires_repair=True but retryable=False")

        operation = data["operation"]
        if operation != failed_phase:
            raise StateError("error record operation must match failed_phase")

        retry_phase = data["retry_phase"]
        if retryable:
            if retry_phase is None:
                raise StateError("error record is retryable but has no retry_phase")
            if retry_phase not in RETRY_TARGET_PHASES:
                raise StateError(
                    f"error record retry_phase {retry_phase!r} is not a valid retry target "
                    "(operational_failure and terminal phases are never valid retry targets)"
                )
            if retry_phase != failed_phase:
                raise StateError("error record retry_phase must match failed_phase")
        elif retry_phase is not None:
            raise StateError(
                f"error record is not retryable but has retry_phase={retry_phase!r}; "
                "a nonretryable record must have retry_phase=None"
            )

        recovery_hint = data["recovery_hint"]
        if recovery_hint is not None and (not isinstance(recovery_hint, str) or not recovery_hint):
            raise StateError("error record field 'recovery_hint' must be None or a non-empty str")

        occurred_at = data["occurred_at"]
        if not isinstance(occurred_at, str) or not occurred_at:
            raise StateError("error record field 'occurred_at' must be a non-empty string")
        try:
            parsed = datetime.fromisoformat(occurred_at)
        except ValueError as exc:
            raise StateError(
                f"error record field 'occurred_at' is not a valid ISO-8601 timestamp: "
                f"{occurred_at!r}"
            ) from exc
        if parsed.tzinfo is None:
            raise StateError("error record field 'occurred_at' must include a timezone offset")

        return cls(**data)


_TASK_IDENTITY_FIELDS = (
    "original_task_id",
    "task_worktree_path",
    "task_branch",
    "task_base_commit",
)


@dataclass
class RunState:
    schema_version: int
    run_id: str
    git_common_dir: str
    integration_path: str
    integration_branch: str
    integration_commit_at_start: str
    options: RunOptions
    integration_expected_head: str
    integration_status_snapshot: str
    original_task_id: str | None = None
    task_worktree_path: str | None = None
    task_branch: str | None = None
    task_base_commit: str | None = None
    task_expected_head: str | None = None
    task_status_snapshot: str | None = None
    phase: str = "planning"
    planner_result: dict[str, Any] | None = None
    architect_result: dict[str, Any] | None = None
    builder_result: dict[str, Any] | None = None
    verification_result: dict[str, Any] | None = None
    auditor_result: dict[str, Any] | None = None
    decision_request: dict[str, Any] | None = None
    accepted_task_count: int = 0
    revision_count: int = 0
    replan_count: int = 0
    architect_retry_count: int = 0
    pending_question: dict[str, Any] | None = None
    last_task_head: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    # v3 additions
    last_error: dict[str, Any] | None = None
    pending_worktree_path: str | None = None
    pending_worktree_branch: str | None = None
    pending_worktree_base: str | None = None
    pending_adr_path: str | None = None
    pending_adr_hash: str | None = None
    merge_pre_head: str | None = None
    merge_task_head: str | None = None
    merge_commit: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["options"] = self.options.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        version = data.get("schema_version")
        # Type-strict version comparison: bool is an int subclass and
        # float(1.0) == 1, so plain equality would accept True or 1.0 as a
        # valid version. The loaded state must always carry a real integer
        # schema version.
        if type(version) is not int:
            raise StateError(f"state schema_version must be an integer, got {version!r}")
        if version != STATE_SCHEMA_VERSION:
            raise StateError(
                f"state schema_version {version!r} is not supported "
                f"(expected {STATE_SCHEMA_VERSION}); there is no migration path "
                "into the current schema -- start a new run instead"
            )

        data = dict(data)

        # Strict, exact field set. Dataclass defaults are appropriate for
        # constructing *new* in-memory states, but must never implicitly
        # repair a persisted document by inventing values for omitted
        # fields (which would silently reset counters, timestamps, or task
        # checkpoints and change the run's meaning).
        known = {f.name for f in fields(cls)}
        keys = set(data)
        unknown = keys - known
        if unknown:
            raise StateError(f"state contains unknown fields: {sorted(unknown)}")
        missing = known - keys
        if missing:
            raise StateError(f"state is missing required fields: {sorted(missing)}")

        data["run_id"] = validate_run_id(data.get("run_id"))

        options_data = data.get("options")
        if not isinstance(options_data, dict):
            raise StateError("state is missing required 'options' object")
        data["options"] = RunOptions.from_dict(options_data)

        _validate_scalar_types(data)
        _validate_timestamps(data)
        _validate_nested_results(data)
        _validate_pending_question(data)
        _validate_task_identity(data, worktree_absent=_worktree_is_absent_phase(data))
        _validate_phase_invariants(data)

        try:
            return cls(**data)
        except TypeError as exc:
            # After exact-field validation this should be unreachable, but
            # normalize any residual constructor error to StateError so the
            # runtime's fail-closed "cannot load run" path always applies.
            raise StateError(f"state could not be constructed: {exc}") from exc


def _validate_scalar_types(data: dict[str, Any]) -> None:
    """Validate the persisted scalar fields so malformed values fail
    closed at load time rather than much later inside Path()/Git/limit
    comparisons."""

    def _require_str(name: str) -> None:
        value = data.get(name)
        if not isinstance(value, str) or not value:
            raise StateError(f"state field {name!r} must be a non-empty string, got {value!r}")

    for name in (
        "run_id",
        "git_common_dir",
        "integration_path",
        "integration_branch",
        "integration_commit_at_start",
        "integration_expected_head",
    ):
        _require_str(name)

    if not isinstance(data.get("integration_status_snapshot"), str):
        raise StateError("state field 'integration_status_snapshot' must be a string")

    for name in (
        "accepted_task_count",
        "revision_count",
        "replan_count",
        "architect_retry_count",
    ):
        value = data.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise StateError(f"state field {name!r} must be a non-negative integer, got {value!r}")

    for name in ("created_at", "updated_at"):
        value = data.get(name)
        if not isinstance(value, str) or not value:
            raise StateError(f"state field {name!r} must be a non-empty string")

    # Optional string checkpoints/intent: null or non-empty string.
    for name in (
        "task_expected_head",
        "last_task_head",
        "pending_worktree_path",
        "pending_worktree_branch",
        "pending_worktree_base",
        "pending_adr_path",
        "pending_adr_hash",
        "merge_pre_head",
        "merge_task_head",
        "merge_commit",
    ):
        value = data.get(name)
        if value is not None and (not isinstance(value, str) or not value):
            raise StateError(f"state field {name!r} must be null or a non-empty string")

    # task_status_snapshot: null or a string (empty string means clean).
    snapshot = data.get("task_status_snapshot")
    if snapshot is not None and not isinstance(snapshot, str):
        raise StateError("state field 'task_status_snapshot' must be null or a string")


def _parse_timestamp(data: dict[str, Any], name: str) -> datetime:
    value = data[name]
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StateError(
            f"state field {name!r} is not a valid ISO-8601 timestamp: {value!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StateError(f"state field {name!r} must include a timezone offset")
    return parsed


def _validate_timestamps(data: dict[str, Any]) -> None:
    created = _parse_timestamp(data, "created_at")
    updated = _parse_timestamp(data, "updated_at")
    if updated < created:
        raise StateError("state field 'updated_at' must not precede 'created_at'")


def _validate_role_result(data: dict[str, Any], field_name: str, model: type[BaseModel]) -> Any:
    value = data.get(field_name)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise StateError(f"state field {field_name!r} must be an object or null")
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise StateError(f"state field {field_name!r} failed contract validation: {exc}") from exc


def _validate_nested_results(data: dict[str, Any]) -> None:
    planner = _validate_role_result(data, "planner_result", PlannerResult)
    _validate_role_result(data, "architect_result", ArchitectResult)
    builder = _validate_role_result(data, "builder_result", BuilderResult)
    auditor = _validate_role_result(data, "auditor_result", AuditorResult)
    _validate_verification_result(data.get("verification_result"))

    planner_task_id = getattr(planner, "task_id", None)
    if builder is not None and planner_task_id is not None:
        builder_task_id = builder.task_id
        if builder_task_id != planner_task_id:
            raise StateError(
                "state field 'builder_result' task_id "
                f"{builder_task_id!r} does not match planner_result task_id {planner_task_id!r}"
            )
    # A REPLAN auditor result is intentionally historical while a replacement
    # planner result scopes the next attempt. Other dispositions describe the
    # current planner task and must retain exact identity.
    if auditor is not None and planner_task_id is not None:
        disposition = auditor.disposition.value
        auditor_task_id = auditor.task_id
        if disposition != "REPLAN" and auditor_task_id != planner_task_id:
            raise StateError(
                "state field 'auditor_result' task_id "
                f"{auditor_task_id!r} does not match planner_result task_id {planner_task_id!r}"
            )


def _validate_verification_result(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise StateError("state field 'verification_result' must be an object or null")
    if set(value) != {"ok", "commands"}:
        raise StateError(
            "state field 'verification_result' must contain exactly 'ok' and 'commands'"
        )
    aggregate_ok = value["ok"]
    commands = value["commands"]
    if not isinstance(aggregate_ok, bool):
        raise StateError("state field 'verification_result.ok' must be a bool")
    if not isinstance(commands, list):
        raise StateError("state field 'verification_result.commands' must be a list")

    expected_fields = {
        "command",
        "ok",
        "returncode",
        "timed_out",
        "duration",
        "output_path",
        "summary",
    }
    command_statuses: list[bool] = []
    for index, command in enumerate(commands):
        prefix = f"state field 'verification_result.commands[{index}]'"
        if not isinstance(command, dict):
            raise StateError(f"{prefix} must be an object")
        if set(command) != expected_fields:
            raise StateError(f"{prefix} must contain exactly {sorted(expected_fields)}")
        for name in ("command", "output_path"):
            scalar = command[name]
            if not isinstance(scalar, str) or not scalar:
                raise StateError(f"{prefix}.{name} must be a non-empty string")
        if not isinstance(command["summary"], str):
            raise StateError(f"{prefix}.summary must be a string")
        for name in ("ok", "timed_out"):
            if not isinstance(command[name], bool):
                raise StateError(f"{prefix}.{name} must be a bool")
        duration = command["duration"]
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise StateError(f"{prefix}.duration must be a number")
        if not math.isfinite(duration) or duration < 0:
            raise StateError(f"{prefix}.duration must be finite and non-negative")
        returncode = command["returncode"]
        if returncode is not None and (
            not isinstance(returncode, int) or isinstance(returncode, bool)
        ):
            raise StateError(f"{prefix}.returncode must be an integer or null")
        timed_out = command["timed_out"]
        if timed_out and returncode is not None:
            raise StateError(f"{prefix}.returncode must be null when timed_out is true")
        expected_ok = not timed_out and returncode == 0
        if command["ok"] is not expected_ok:
            raise StateError(f"{prefix}.ok is inconsistent with timed_out and returncode")
        command_statuses.append(expected_ok)
    if aggregate_ok is not all(command_statuses):
        raise StateError(
            "state field 'verification_result' aggregate ok is inconsistent with commands"
        )


def _validate_pending_question(data: dict[str, Any]) -> None:
    pending = data.get("pending_question")
    if pending is None:
        return
    if not isinstance(pending, dict):
        raise StateError("state field 'pending_question' must be an object or null")
    allowed_fields = {"kind", "message", "context", "answer"}
    if not set(pending) <= allowed_fields or not {"kind", "message", "context"} <= set(pending):
        raise StateError(
            "state field 'pending_question' must contain kind, message, context, "
            "and optional answer"
        )
    kind = pending["kind"]
    contexts = {
        "architect_input": ({"question"}, None),
        "decision_approval": ({"title", "decision"}, None),
        "builder_guidance": ({"status"}, {"BLOCKED", "INCOMPLETE"}),
    }
    if kind not in contexts:
        raise StateError(f"state field 'pending_question.kind' is unknown: {kind!r}")
    if not isinstance(pending["message"], str) or not pending["message"]:
        raise StateError("state field 'pending_question.message' must be a non-empty string")
    context = pending["context"]
    expected_context, allowed_statuses = contexts[kind]
    if not isinstance(context, dict) or set(context) != expected_context:
        raise StateError(
            f"state field 'pending_question.context' for {kind!r} must contain exactly "
            f"{sorted(expected_context)}"
        )
    if not all(isinstance(item, str) and item for item in context.values()):
        raise StateError("state field 'pending_question.context' values must be non-empty strings")
    if allowed_statuses is not None and context["status"] not in allowed_statuses:
        raise StateError(
            "state field 'pending_question.context.status' must be 'BLOCKED' or 'INCOMPLETE'"
        )
    if "answer" in pending and not isinstance(pending["answer"], str):
        raise StateError("state field 'pending_question.answer' must be a string when present")


def _worktree_is_absent_phase(data: dict[str, Any]) -> bool:
    """True when the run's effective phase is one where the task worktree
    has been intentionally removed (cleanup_branch, or an operational
    failure whose retry target is cleanup_branch). In that state the task
    status snapshot is legitimately null."""
    phase = data.get("phase")
    if phase == "cleanup_branch":
        return True
    if phase == PHASE_OPERATIONAL_FAILURE:
        raw_error = data.get("last_error")
        if isinstance(raw_error, dict) and raw_error.get("retry_phase") == "cleanup_branch":
            return True
    return False


def _validate_task_identity(data: dict[str, Any], *, worktree_absent: bool) -> None:
    """Task identity must be either entirely absent (all None) or entirely
    present as non-empty strings; and when present, the resume-critical
    checkpoints must be well-typed. Empty strings are invalid, not
    'absent': resume treats identity presence with ``is not None``, so an
    all-empty-string identity would be an active task pointing at Path("")
    and empty branches/commits."""
    values = {f: data.get(f) for f in _TASK_IDENTITY_FIELDS}
    present = [f for f, v in values.items() if v is not None]
    if not present:
        for extra in ("task_expected_head", "task_status_snapshot"):
            if data.get(extra) is not None:
                raise StateError(f"state has task checkpoint {extra!r} without any task identity")
        return

    if len(present) != len(_TASK_IDENTITY_FIELDS):
        missing = [f for f in _TASK_IDENTITY_FIELDS if f not in present]
        raise StateError(f"state has partial task identity: present {present}, missing {missing}")
    for f in _TASK_IDENTITY_FIELDS:
        if not isinstance(values[f], str) or not values[f]:
            raise StateError(f"task identity field {f!r} must be a non-empty string")

    expected_head = data.get("task_expected_head")
    snapshot = data.get("task_status_snapshot")
    if worktree_absent:
        # The task worktree has been removed (cleanup_branch); its last known
        # verified HEAD is retained while the status snapshot is cleared.
        if not isinstance(expected_head, str) or not expected_head:
            raise StateError(
                "an active cleanup task requires a non-empty task_expected_head checkpoint"
            )
        if snapshot is not None and not isinstance(snapshot, str):
            raise StateError("state field 'task_status_snapshot' must be null or a string")
    elif not isinstance(expected_head, str) or not expected_head:
        raise StateError("an active task requires a non-empty task_expected_head checkpoint")
    elif not isinstance(snapshot, str):
        raise StateError(
            "an active task requires a task_status_snapshot string (empty string means clean)"
        )


def _validate_phase_invariants(data: dict[str, Any]) -> None:
    phase = data.get("phase", "")
    if phase not in ALL_PHASES:
        raise StateError(f"state has unknown phase: {phase!r}")

    raw_error = data.get("last_error")
    if raw_error is not None:
        if not isinstance(raw_error, dict):
            raise StateError("state field 'last_error' must be an object or null")
        # Parse whenever present, not only in operational_failure/failed: a
        # tampered or malformed record must never load successfully just
        # because it happens to be attached to some other phase.
        record = OperationalErrorRecord.from_dict(raw_error)
        if phase == PHASE_OPERATIONAL_FAILURE and not record.retryable:
            raise StateError(
                "phase 'operational_failure' requires a retryable last_error "
                "(a nonretryable failure must be phase 'failed', not "
                "'operational_failure')"
            )
        if phase == "failed" and record.retryable:
            raise StateError(
                "phase 'failed' requires a nonretryable last_error "
                "(a retryable failure must be phase 'operational_failure', not 'failed')"
            )

    # A present decision_request must be structurally valid regardless of
    # phase, for the same reason last_error is validated whenever present.
    raw_decision = data.get("decision_request")
    if raw_decision is not None:
        if not isinstance(raw_decision, dict):
            raise StateError("state field 'decision_request' must be an object or null")
        DecisionRequest.from_dict(raw_decision)

    if phase == "operational_failure" and raw_error is None:
        raise StateError("phase 'operational_failure' requires last_error")

    if phase == "failed" and raw_error is None:
        raise StateError("phase 'failed' requires a nonretryable last_error")

    if phase == "recording_decision":
        decision_request = data.get("decision_request")
        if not isinstance(decision_request, dict):
            raise StateError("phase 'recording_decision' requires a decision_request object")
        DecisionRequest.from_dict(decision_request)

    effective_phase = phase
    if phase == PHASE_OPERATIONAL_FAILURE:
        assert raw_error is not None
        effective_phase = OperationalErrorRecord.from_dict(raw_error).retry_phase or ""
    _validate_effective_phase_requirements(data, effective_phase)
    _validate_pending_question_phase(data, effective_phase)


def _validate_pending_question_phase(data: dict[str, Any], phase: str) -> None:
    pending = data.get("pending_question")
    if pending is None or phase == "awaiting_input":
        return
    allowed_phase = phase
    if phase == "failed":
        raw_error = data.get("last_error")
        if isinstance(raw_error, dict):
            allowed_phase = raw_error.get("failed_phase", "")
    if "answer" not in pending or allowed_phase not in {"architecting", "building"}:
        raise StateError(
            "state field 'pending_question' may only be retained outside awaiting_input "
            "as answered architect or builder guidance"
        )
    expected_kind = "architect_input" if allowed_phase == "architecting" else "builder_guidance"
    if pending["kind"] != expected_kind:
        raise StateError(f"phase {phase!r} cannot retain pending_question kind {pending['kind']!r}")
    _validate_pending_context_matches_source(data, pending, phase)


def _require_result(data: dict[str, Any], phase: str, field_name: str) -> dict[str, Any]:
    value = data.get(field_name)
    if not isinstance(value, dict):
        raise StateError(f"phase {phase!r} requires {field_name}")
    return value


def _validate_decision_relationships(
    data: dict[str, Any], phase: str, *, require_architect_answer: bool = True
) -> None:
    decision = _require_result(data, phase, "decision_request")
    planner = _require_result(data, phase, "planner_result")
    if decision["origin"] == "planner":
        if not planner["decision_required"]:
            raise StateError(
                f"phase {phase!r} has a planner decision_request but planner_result "
                "does not require a decision"
            )
        if (
            decision["question"] != planner["decision_question"]
            or decision["rationale"] != planner["decision_rationale"]
        ):
            raise StateError(f"phase {phase!r} decision_request does not match planner_result")
    else:
        auditor = _require_result(data, phase, "auditor_result")
        if auditor["task_id"] != planner["task_id"]:
            raise StateError(
                f"phase {phase!r} auditor decision_request has an auditor_result task_id "
                "that does not match planner_result task_id"
            )
        if not auditor["decision_required"]:
            raise StateError(
                f"phase {phase!r} has an auditor decision_request but auditor_result "
                "does not require a decision"
            )
        if (
            decision["question"] != auditor["decision_question"]
            or decision["rationale"] != auditor["decision_rationale"]
        ):
            raise StateError(f"phase {phase!r} decision_request does not match auditor_result")

    architect = data.get("architect_result")
    if (
        require_architect_answer
        and isinstance(architect, dict)
        and architect["question"] != decision["question"]
    ):
        raise StateError(
            "state field 'architect_result.question' does not match decision_request question"
        )


def _validate_pending_context_matches_source(
    data: dict[str, Any], pending: dict[str, Any], phase: str
) -> None:
    kind = pending["kind"]
    context = pending["context"]
    if kind == "builder_guidance":
        builder = _require_result(data, phase, "builder_result")
        if context["status"] != builder["status"]:
            raise StateError(
                "state field 'pending_question.context.status' does not match builder_result status"
            )
        return

    _validate_decision_relationships(data, phase)
    decision = _require_result(data, phase, "decision_request")
    if kind == "architect_input":
        if context["question"] != decision["question"]:
            raise StateError(
                "state field 'pending_question.context.question' does not match decision_request"
            )
        return

    architect = _require_result(data, phase, "architect_result")
    adr = architect["adr"]
    assert isinstance(adr, dict)
    for field_name in ("title", "decision"):
        if context[field_name] != adr[field_name]:
            raise StateError(
                f"state field 'pending_question.context.{field_name}' does not match "
                "architect_result ADR"
            )


def _validate_verification_for_phase(data: dict[str, Any], phase: str) -> None:
    configured_commands = data["options"].verify_commands
    result = data.get("verification_result")
    pre_verification_phases = {
        "planning",
        "creating_worktree",
        "architecting",
        "recording_decision",
        "building",
        "awaiting_input",
    }
    if phase in pre_verification_phases:
        if result is not None:
            raise StateError(f"phase {phase!r} cannot contain verification_result")
        return
    post_build_phases = {"verifying", "auditing", "merging", "cleanup_worktree", "cleanup_branch"}
    if phase not in post_build_phases:
        return
    if phase == "verifying":
        if not configured_commands:
            raise StateError("phase 'verifying' requires configured verify_commands")
        if result is not None:
            raise StateError("phase 'verifying' must not already have verification_result")
        return

    present = result is not None
    if bool(configured_commands) != present:
        raise StateError(
            f"phase {phase!r} requires verification_result exactly when verify_commands "
            "are configured"
        )
    if isinstance(result, dict):
        persisted_commands = [item["command"] for item in result["commands"]]
        if persisted_commands != list(configured_commands):
            raise StateError(
                "state field 'verification_result.commands' must match configured "
                "verify_commands in order"
            )


def _commit_ids_match(reported: str, canonical: str) -> bool:
    """Compare persisted builder and verified commit identities.

    Runtime commit verification accepts a bare 7-40 character hexadecimal
    abbreviation and persists the canonical HEAD separately (ADR 0013). State
    loading has no repository available for rev-parse, so it can only enforce
    the corresponding safe prefix relationship.
    """
    if reported == canonical:
        return True
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", reported):
        return False
    if not re.fullmatch(r"[0-9a-fA-F]{40}", canonical):
        return False
    return canonical.lower().startswith(reported.lower())


def _validate_effective_phase_requirements(data: dict[str, Any], phase: str) -> None:
    if phase == "creating_worktree":
        for field_name in (
            "pending_worktree_path",
            "pending_worktree_branch",
            "pending_worktree_base",
        ):
            if not data.get(field_name):
                raise StateError(f"phase 'creating_worktree' requires {field_name}")
        if any(data.get(name) is not None for name in _TASK_IDENTITY_FIELDS) or any(
            data.get(name) is not None for name in ("task_expected_head", "task_status_snapshot")
        ):
            raise StateError(
                "phase 'creating_worktree' cannot contain active task identity or task checkpoints"
            )
        planner_result = data.get("planner_result")
        if not isinstance(planner_result, dict) or not planner_result.get("task_id"):
            raise StateError(
                "phase 'creating_worktree' requires a planner_result with a task_id "
                "to recover the original task identity for reconciliation"
            )
    if phase == "merging":
        if data.get("merge_pre_head") is None or data.get("merge_task_head") is None:
            raise StateError("phase 'merging' requires merge_pre_head and merge_task_head")
        _require_full_task_identity(data, "merging")
    if phase in ("cleanup_worktree", "cleanup_branch"):
        for field_name in ("merge_pre_head", "merge_task_head", "merge_commit"):
            if data.get(field_name) is None:
                raise StateError(f"phase {phase!r} requires {field_name}")
        _require_full_task_identity(data, phase)
    if phase == "recording_decision":
        if data.get("pending_adr_path") is None or data.get("pending_adr_hash") is None:
            raise StateError(
                "phase 'recording_decision' requires pending_adr_path and pending_adr_hash"
            )

    if phase in {
        "planning",
        "creating_worktree",
        "architecting",
        "recording_decision",
        "building",
        "awaiting_input",
    }:
        _validate_verification_for_phase(data, phase)

    if phase == "awaiting_input":
        _require_full_task_identity(data, phase)
        planner = _require_result(data, phase, "planner_result")
        if planner["status"] != "READY":
            raise StateError("phase 'awaiting_input' requires a READY planner_result")
        pending = data.get("pending_question")
        if not isinstance(pending, dict):
            raise StateError("phase 'awaiting_input' requires a valid pending_question")
        kind = pending["kind"]
        if kind == "builder_guidance":
            builder = _require_result(data, phase, "builder_result")
            if builder["status"] not in ("BLOCKED", "INCOMPLETE"):
                raise StateError(
                    "phase 'awaiting_input' builder_guidance requires a blocked or incomplete "
                    "builder_result"
                )
        elif kind == "architect_input":
            architect = _require_result(data, phase, "architect_result")
            if architect["status"] not in ("NEEDS_INPUT", "DECIDED"):
                raise StateError(
                    "phase 'awaiting_input' architect_input contradicts architect_result status"
                )
        else:
            architect = _require_result(data, phase, "architect_result")
            if architect["status"] != "DECIDED":
                raise StateError(
                    "phase 'awaiting_input' decision_approval requires a DECIDED architect_result"
                )
        _validate_pending_context_matches_source(data, pending, phase)
        return

    if phase == "creating_worktree":
        planner = _require_result(data, phase, "planner_result")
        if planner["status"] != "READY":
            raise StateError("phase 'creating_worktree' requires a READY planner_result")
        return

    task_phases = {
        "architecting",
        "recording_decision",
        "building",
        "verifying",
        "auditing",
        "merging",
        "cleanup_worktree",
        "cleanup_branch",
    }
    if phase in task_phases:
        _require_full_task_identity(data, phase)

    if phase in task_phases:
        planner = _require_result(data, phase, "planner_result")
        if planner["status"] != "READY":
            raise StateError(f"phase {phase!r} requires a READY planner_result")

    if phase in {"architecting", "recording_decision"}:
        # Entering architecting may retain the answer to an earlier, distinct
        # decision while a newly active request is awaiting its first response.
        # Once input/approval or decision recording exists, the architect result
        # is established as the answer and must match exactly.
        _validate_decision_relationships(
            data,
            phase,
            require_architect_answer=phase == "recording_decision"
            or data.get("pending_question") is not None,
        )

    if phase == "recording_decision":
        architect = _require_result(data, phase, "architect_result")
        if architect["status"] != "DECIDED":
            raise StateError("phase 'recording_decision' requires a DECIDED architect_result")

    if phase in {"merging", "cleanup_worktree", "cleanup_branch"}:
        if data["merge_task_head"] != data["last_task_head"]:
            raise StateError(
                "state field 'merge_task_head' must match last_task_head during merge and cleanup"
            )

    if phase in {"verifying", "auditing", "merging", "cleanup_worktree", "cleanup_branch"}:
        builder = _require_result(data, phase, "builder_result")
        if builder["status"] != "COMPLETE":
            raise StateError(f"phase {phase!r} requires a COMPLETE builder_result")
        last_task_head = data.get("last_task_head")
        if last_task_head is None:
            raise StateError(f"phase {phase!r} requires last_task_head")
        reported_commit = builder["commit"]
        assert isinstance(reported_commit, str)
        if not _commit_ids_match(reported_commit, last_task_head):
            raise StateError(
                "state field 'last_task_head' must identify builder_result.commit "
                "(an unambiguous abbreviated builder commit is allowed)"
            )
        if data.get("task_expected_head") != last_task_head:
            raise StateError(
                "state field 'task_expected_head' must match last_task_head after building"
            )
        _validate_verification_for_phase(data, phase)

    if phase in {"merging", "cleanup_worktree", "cleanup_branch"}:
        auditor = _require_result(data, phase, "auditor_result")
        if auditor["disposition"] != "ACCEPT":
            raise StateError(f"phase {phase!r} requires an ACCEPT auditor_result")

    if phase == "done" and any(data.get(name) is not None for name in _TASK_IDENTITY_FIELDS):
        raise StateError("phase 'done' cannot retain an active task identity")


def _require_full_task_identity(data: dict[str, Any], phase: str) -> None:
    for field_name in _TASK_IDENTITY_FIELDS:
        if not data.get(field_name):
            raise StateError(
                f"phase {phase!r} requires complete task identity; missing {field_name}"
            )


def state_dir(git_common_dir: Path) -> Path:
    return git_common_dir / "loop-supervisor" / "runs"


def state_path(git_common_dir: Path, run_id: str) -> Path:
    """Resolve the on-disk path for a run ID.

    Validates the run ID first so a caller-supplied or persisted value can
    never traverse outside the runs directory or resolve to a path with
    unexpected separators (e.g. ``../../etc/passwd`` or an absolute path).
    """
    validated = validate_run_id(run_id)
    return state_dir(git_common_dir) / f"{validated}.json"


def _required_open_flag(name: str) -> int:
    """Return a required secure-open flag, or fail closed if unavailable."""
    value = getattr(os, name, None)
    if not isinstance(value, int):
        raise StateError(f"secure state storage requires os.{name}; this platform is unsupported")
    return value


@contextlib.contextmanager
def _open_state_directory(git_common_dir: Path, *, create: bool):
    """Yield the runs directory descriptor without following storage symlinks."""
    directory_flag = _required_open_flag("O_DIRECTORY")
    nofollow_flag = _required_open_flag("O_NOFOLLOW")
    supervisor = git_common_dir / "loop-supervisor"
    runs = supervisor / "runs"
    try:
        if create:
            try:
                os.mkdir(supervisor, 0o700)
            except FileExistsError:
                pass
        supervisor_fd = os.open(
            supervisor,
            os.O_RDONLY | directory_flag | nofollow_flag,
        )
    except OSError as exc:
        raise StateError(
            f"cannot use state directory {supervisor}; refusing symbolic link or unsafe path: {exc}"
        ) from exc
    try:
        try:
            if create:
                try:
                    os.mkdir("runs", 0o700, dir_fd=supervisor_fd)
                except FileExistsError:
                    pass
            runs_fd = os.open(
                "runs",
                os.O_RDONLY | directory_flag | nofollow_flag,
                dir_fd=supervisor_fd,
            )
        except OSError as exc:
            raise StateError(
                f"cannot use state directory {runs}; refusing symbolic link or unsafe path: {exc}"
            ) from exc
        try:
            if not stat.S_ISDIR(os.fstat(runs_fd).st_mode):
                raise StateError(f"state directory {runs} is not a directory")
            yield runs_fd
        finally:
            os.close(runs_fd)
    finally:
        os.close(supervisor_fd)


def _reject_state_symlink(directory_fd: int, name: str, path: Path) -> None:
    try:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise StateError(f"state file {path} is a symbolic link; refusing to use it")


def save_state(git_common_dir: Path, state: RunState) -> None:
    state.updated_at = _now()
    # Validates state.run_id, so a state object whose run_id was tampered
    # with after construction (or a caller-crafted RunState) can never be
    # saved outside the runs directory or under an unsafe filename.
    target = state_path(git_common_dir, state.run_id)
    tmp_name = f".tmp-{uuid.uuid4().hex}.json"
    try:
        with _open_state_directory(git_common_dir, create=True) as directory_fd:
            _reject_state_symlink(directory_fd, target.name, target)
            fd = os.open(
                tmp_name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | _required_open_flag("O_NOFOLLOW"),
                0o600,
                dir_fd=directory_fd,
            )
            fd_owned = True
            try:
                os.fchmod(fd, 0o600)
                handle = os.fdopen(fd, "w")
                fd_owned = False
                with handle:
                    json.dump(state.to_dict(), handle, indent=2, sort_keys=True)
                    handle.write("\n")
                os.replace(tmp_name, target.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            except BaseException:
                try:
                    os.unlink(tmp_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                raise
            finally:
                if fd_owned:
                    os.close(fd)
    except StateError:
        raise
    except OSError as exc:
        raise StateError(f"state file for run {state.run_id!r} could not be saved: {exc}") from exc


def load_state(git_common_dir: Path, run_id: str) -> RunState:
    """Load the state saved for ``run_id``.

    ``run_id`` is validated before it is used to construct a path, and the
    loaded state's own embedded ``run_id`` is required to match the
    requested ID exactly. This closes two distinct attacks: a crafted
    ``run_id`` argument resolving outside the runs directory, and a state
    *file* (however it got there) claiming to be a different run than the
    filename under which it was requested.
    """
    validated_id = validate_run_id(run_id)
    path = state_path(git_common_dir, validated_id)
    try:
        with _open_state_directory(git_common_dir, create=False) as directory_fd:
            _reject_state_symlink(directory_fd, path.name, path)
            try:
                fd = os.open(
                    path.name,
                    os.O_RDONLY | _required_open_flag("O_NOFOLLOW"),
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                raise StateError(f"no saved state for run {validated_id!r} at {path}") from None
            with os.fdopen(fd) as handle:
                data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise StateError(f"state file for run {validated_id!r} is not valid JSON: {exc}") from exc
    except StateError:
        raise
    except OSError as exc:
        raise StateError(f"state file for run {validated_id!r} could not be read: {exc}") from exc
    if not isinstance(data, dict):
        raise StateError(f"state file for run {validated_id!r} does not contain a JSON object")
    state = RunState.from_dict(data)
    if state.run_id != validated_id:
        raise StateError(
            f"state file requested as run {validated_id!r} contains embedded "
            f"run_id {state.run_id!r}; refusing to load mismatched identity"
        )
    return state


def list_runs(git_common_dir: Path) -> list[str]:
    """List saved run IDs.

    Skips (rather than raises on) any filename whose stem is not itself a
    valid run ID, so one unrelated or corrupted file in the runs directory
    cannot break the run browser for every other valid run.
    """
    directory = state_dir(git_common_dir)
    if not directory.exists():
        return []
    valid_ids = []
    for path in directory.glob("*.json"):
        try:
            valid_ids.append(validate_run_id(path.stem))
        except StateError:
            continue
    return sorted(valid_ids)
