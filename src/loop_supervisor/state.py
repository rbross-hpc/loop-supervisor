"""Resumable supervisor state, persisted under the repository's shared Git
metadata directory (`git rev-parse --git-common-dir`), not inside the
tracked worktree. This keeps run state out of the project's own history
while remaining local to the clone and shared across linked worktrees.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATE_SCHEMA_VERSION = 2


class StateError(RuntimeError):
    """Raised for invalid or inconsistent resumable state."""


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


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

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["options"] = self.options.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        version = data.get("schema_version")
        if version == 1:
            raise StateError(
                "state schema_version 1 cannot be resumed safely: it lacks "
                "immutable run options and Git checkpoints introduced in "
                "schema_version 2. Start a new run instead."
            )
        if version != STATE_SCHEMA_VERSION:
            raise StateError(
                f"state schema_version {version!r} is not supported "
                f"(expected {STATE_SCHEMA_VERSION})"
            )
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise StateError(f"state contains unknown fields: {sorted(unknown)}")

        data = dict(data)
        options_data = data.get("options")
        if not isinstance(options_data, dict):
            raise StateError("state is missing required 'options' object")
        data["options"] = RunOptions.from_dict(options_data)

        present = [f for f in _TASK_IDENTITY_FIELDS if data.get(f) not in (None, "")]
        if present and len(present) != len(_TASK_IDENTITY_FIELDS):
            missing = [f for f in _TASK_IDENTITY_FIELDS if f not in present]
            raise StateError(
                f"state has partial task identity: present {present}, missing {missing}"
            )

        return cls(**data)


def state_dir(git_common_dir: Path) -> Path:
    return git_common_dir / "loop-supervisor" / "runs"


def state_path(git_common_dir: Path, run_id: str) -> Path:
    return state_dir(git_common_dir) / f"{run_id}.json"


def save_state(git_common_dir: Path, state: RunState) -> None:
    state.updated_at = _now()
    directory = state_dir(git_common_dir)
    directory.mkdir(parents=True, exist_ok=True)
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
    path = state_path(git_common_dir, run_id)
    if not path.exists():
        raise StateError(f"no saved state for run {run_id!r} at {path}")
    with path.open() as handle:
        data = json.load(handle)
    return RunState.from_dict(data)


def list_runs(git_common_dir: Path) -> list[str]:
    directory = state_dir(git_common_dir)
    if not directory.exists():
        return []
    return sorted(p.stem for p in directory.glob("*.json"))
