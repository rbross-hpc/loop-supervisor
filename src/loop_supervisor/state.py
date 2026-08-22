"""Resumable supervisor state, persisted under the repository's shared Git
metadata directory (`git rev-parse --git-common-dir`), not inside the
tracked worktree. This keeps run state out of the project's own history
while remaining local to the clone and shared across linked worktrees.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .phases import (
    ALL_PHASES,
    PHASE_OPERATIONAL_FAILURE,
    RETRY_TARGET_PHASES,
    V2_PHASES,
)

STATE_SCHEMA_VERSION = 3

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
        return cls(**data)


@dataclass(frozen=True)
class OperationalErrorRecord:
    """A sanitized, durable record of an operational failure.

    Never contains tracebacks, full request payloads, environment variables,
    authorization headers, or secrets."""

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

        retry_phase = data["retry_phase"]
        if retryable:
            if retry_phase is None:
                raise StateError("error record is retryable but has no retry_phase")
            if retry_phase not in RETRY_TARGET_PHASES:
                raise StateError(
                    f"error record retry_phase {retry_phase!r} is not a valid retry target "
                    "(operational_failure and terminal phases are never valid retry targets)"
                )
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

_V2_FIELDS = {
    "schema_version",
    "run_id",
    "git_common_dir",
    "integration_path",
    "integration_branch",
    "integration_commit_at_start",
    "options",
    "integration_expected_head",
    "integration_status_snapshot",
    "original_task_id",
    "task_worktree_path",
    "task_branch",
    "task_base_commit",
    "task_expected_head",
    "task_status_snapshot",
    "phase",
    "planner_result",
    "architect_result",
    "builder_result",
    "auditor_result",
    "decision_request",
    "accepted_task_count",
    "revision_count",
    "replan_count",
    "architect_retry_count",
    "pending_question",
    "last_task_head",
    "created_at",
    "updated_at",
}

# Fields added in schema v3. A document claiming to be v2 must not contain
# any of these; migration always creates them as None.
_V3_ONLY_FIELDS = frozenset(
    {
        "last_error",
        "pending_worktree_path",
        "pending_worktree_branch",
        "pending_worktree_base",
        "pending_adr_path",
        "pending_adr_hash",
        "merge_pre_head",
        "merge_task_head",
        "merge_commit",
    }
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
        # float(2.0) == 2, so plain equality would accept True as v1 and
        # 2.0 as v2. The migrated/loaded state must always carry a real
        # integer schema version.
        if type(version) is not int:
            raise StateError(f"state schema_version must be an integer, got {version!r}")
        if version == 1:
            raise StateError(
                "state schema_version 1 cannot be resumed safely: it lacks "
                "immutable run options and Git checkpoints introduced in "
                "schema_version 2. Start a new run instead."
            )
        if version not in (2, STATE_SCHEMA_VERSION):
            raise StateError(
                f"state schema_version {version!r} is not supported "
                f"(expected {STATE_SCHEMA_VERSION})"
            )

        data = dict(data)
        source_version = version

        if source_version == 2:
            data = _migrate_v2_to_v3(data)

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
        _validate_task_identity(data, worktree_absent=_worktree_is_absent_phase(data))
        _validate_phase_invariants(data, source_version=source_version)

        try:
            return cls(**data)
        except TypeError as exc:
            # After exact-field validation this should be unreachable, but
            # normalize any residual constructor error to StateError so the
            # runtime's fail-closed "cannot load run" path always applies.
            raise StateError(f"state could not be constructed: {exc}") from exc


def _migrate_v2_to_v3(data: dict[str, Any]) -> dict[str, Any]:
    """Strictly, additively migrate schema v2 to v3.

    The v2 boundary is enforced, not trusted: the document must have
    *exactly* the historical v2 field set (no v3-only fields), and its
    phase must belong to the v2 phase vocabulary. Migration then builds a
    fresh dict from the validated v2 fields and assigns every v3-only
    field to None unconditionally — never preserving a supplied value —
    so a v2 document can never smuggle in v3 crash-reconciliation intent.
    Existing v2 run options, Git checkpoints, and phase are left untouched.
    """
    keys = set(data)
    v3_present = keys & _V3_ONLY_FIELDS
    if v3_present:
        raise StateError(
            "state claims schema_version 2 but contains schema v3-only "
            f"fields: {sorted(v3_present)}"
        )
    unknown = keys - _V2_FIELDS
    if unknown:
        raise StateError(f"schema v2 state contains unknown fields: {sorted(unknown)}")
    missing = _V2_FIELDS - keys
    if missing:
        raise StateError(f"schema v2 state is missing required fields: {sorted(missing)}")

    phase = data.get("phase")
    if phase not in V2_PHASES:
        raise StateError(f"schema v2 state has phase {phase!r}, which did not exist in schema v2")

    result = {key: data[key] for key in _V2_FIELDS}
    result["schema_version"] = STATE_SCHEMA_VERSION
    for key in _V3_ONLY_FIELDS:
        result[key] = None
    return result


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
    if not isinstance(expected_head, str) or not expected_head:
        raise StateError("an active task requires a non-empty task_expected_head checkpoint")
    snapshot = data.get("task_status_snapshot")
    if worktree_absent:
        # The task worktree has been removed (cleanup_branch); the status
        # snapshot is intentionally cleared to null.
        if snapshot is not None and not isinstance(snapshot, str):
            raise StateError("state field 'task_status_snapshot' must be null or a string")
    elif not isinstance(snapshot, str):
        raise StateError(
            "an active task requires a task_status_snapshot string (empty string means clean)"
        )


def _validate_phase_invariants(data: dict[str, Any], *, source_version: int) -> None:
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

    if phase == "creating_worktree":
        for field_name in (
            "pending_worktree_path",
            "pending_worktree_branch",
            "pending_worktree_base",
        ):
            if not data.get(field_name):
                raise StateError(f"phase 'creating_worktree' requires {field_name}")
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
    if phase == "operational_failure" and raw_error is None:
        raise StateError("phase 'operational_failure' requires last_error")

    # A native v3 terminal failure always persists a nonretryable error
    # record. A genuine migrated v2 'failed' state necessarily has no
    # last_error (the field did not exist in v2), and migration adds it as
    # None; that legacy shape is permitted for read/display compatibility.
    if phase == "failed" and raw_error is None and source_version != 2:
        raise StateError("phase 'failed' requires a nonretryable last_error")

    if phase == "recording_decision":
        decision_request = data.get("decision_request")
        if not isinstance(decision_request, dict):
            raise StateError("phase 'recording_decision' requires a decision_request object")
        DecisionRequest.from_dict(decision_request)


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


def save_state(git_common_dir: Path, state: RunState) -> None:
    state.updated_at = _now()
    directory = state_dir(git_common_dir)
    directory.mkdir(parents=True, exist_ok=True)
    # Validates state.run_id, so a state object whose run_id was tampered
    # with after construction (or a caller-crafted RunState) can never be
    # saved outside the runs directory or under an unsafe filename.
    target = state_path(git_common_dir, state.run_id)

    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


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
    if not path.exists():
        raise StateError(f"no saved state for run {validated_id!r} at {path}")
    try:
        with path.open() as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise StateError(f"state file for run {validated_id!r} is not valid JSON: {exc}") from exc
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
