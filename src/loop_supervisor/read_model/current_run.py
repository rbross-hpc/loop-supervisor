"""Authoritative, presentation-independent current run detail."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..state import StateError, load_state


@dataclass(frozen=True)
class CurrentRun:
    """The validated current state needed by the run-detail summary.

    An unloadable value intentionally has no inferred durable state.  This keeps
    a concurrent disappearance or invalid snapshot visible without inventing
    phase, timing, or task information from an unchecked file.
    """

    run_id: str
    loadable: bool
    phase: str | None
    created_at: str | None
    updated_at: str | None
    integration_branch: str | None
    current_task_id: str | None
    accepted_task_count: int | None
    revision_count: int | None
    replan_count: int | None
    architect_retry_count: int | None
    builder_guidance_count: int | None
    pending_question: str | None
    latest_operational_error: str | None
    diagnostic: str | None

    @classmethod
    def degraded(cls, run_id: str, diagnostic: str) -> CurrentRun:
        """Represent a snapshot that the authoritative reader rejected."""
        return cls(
            run_id=run_id,
            loadable=False,
            phase=None,
            created_at=None,
            updated_at=None,
            integration_branch=None,
            current_task_id=None,
            accepted_task_count=None,
            revision_count=None,
            replan_count=None,
            architect_retry_count=None,
            builder_guidance_count=None,
            pending_question=None,
            latest_operational_error=None,
            diagnostic=diagnostic,
        )


def load_current_run(git_common_dir: Path, run_id: str) -> CurrentRun:
    """Load one current state solely through the authoritative state reader.

    Diagnostics deliberately summarize known reader failures rather than relay
    parser or persisted-file text, which may be untrusted.  No snapshot bytes
    are read or interpreted at this boundary.
    """
    try:
        state = load_state(git_common_dir, run_id)
    except (StateError, OSError) as exc:
        return CurrentRun.degraded(run_id, _safe_diagnostic(exc))

    pending_question = state.pending_question
    pending_message = pending_question["message"] if pending_question is not None else None
    last_error = state.last_error
    error_message = last_error["message"] if last_error is not None else None
    return CurrentRun(
        run_id=state.run_id,
        loadable=True,
        phase=state.phase,
        created_at=state.created_at,
        updated_at=state.updated_at,
        integration_branch=state.integration_branch,
        current_task_id=state.original_task_id,
        accepted_task_count=state.accepted_task_count,
        revision_count=state.revision_count,
        replan_count=state.replan_count,
        architect_retry_count=state.architect_retry_count,
        builder_guidance_count=state.builder_guidance_count,
        pending_question=pending_message,
        latest_operational_error=error_message,
        diagnostic=None,
    )


def _safe_diagnostic(error: Exception) -> str:
    """Map authoritative reader failures to fixed, actionable public text."""
    reason = str(error).lower()
    if "symbolic link" in reason:
        detail = "the snapshot is a symbolic link"
    elif "not a regular file" in reason:
        detail = "the snapshot is not a regular file"
    elif "oversized" in reason or "limit" in reason:
        detail = "the snapshot exceeds supported input limits"
    elif "schema_version" in reason or "schema version" in reason:
        detail = "the snapshot uses an unsupported schema"
    elif "mismatched identity" in reason:
        detail = "the snapshot identity does not match its requested run"
    elif "no such file" in reason or "filenotfounderror" in reason:
        detail = "the snapshot is unavailable"
    elif "malformed" in reason or "json" in reason or "does not contain" in reason:
        detail = "the snapshot is malformed"
    else:
        detail = "the snapshot could not be validated"
    return f"Current run details are unavailable because {detail}."
