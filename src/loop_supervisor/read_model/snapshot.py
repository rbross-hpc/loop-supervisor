"""Fresh, bounded composition of the read model consumed by presentation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from .current_run import CurrentRun, load_current_run
from .discovery import RunSummary, discover_runs_bounded
from .history import HistoryLoad, load_history
from .lock_observation import LockObservation, observe_lock
from .project import ProjectResolution, resolve_project
from .verification import VerificationDiscovery, discover_verification

MAX_RUN_CANDIDATES = 10_000
CurrentStateDisagreementField = Literal[
    "phase",
    "accepted_task_count",
    "revision_count",
    "replan_count",
    "architect_retry_count",
    "builder_guidance_count",
]


@dataclass(frozen=True)
class SnapshotDiagnostic:
    """A safe project-scan diagnostic that does not belong to one run."""

    code: str
    message: str


@dataclass(frozen=True)
class CurrentStateDisagreement:
    """A bounded, presentation-independent mismatch with authoritative current state."""

    field: CurrentStateDisagreementField
    history_seq: int


@dataclass(frozen=True)
class RunDetailSnapshot:
    """One run's immutable current, history, and verification evidence."""

    summary: RunSummary
    current: CurrentRun
    history: HistoryLoad
    current_state_disagreements: tuple[CurrentStateDisagreement, ...]
    verification: VerificationDiscovery


@dataclass(frozen=True)
class ProjectSnapshot:
    """One immutable, best-effort view of project artifacts at scan time."""

    project: ProjectResolution
    runs: tuple[RunSummary, ...]
    run_details: tuple[RunDetailSnapshot, ...]
    lock: LockObservation
    diagnostics: tuple[SnapshotDiagnostic, ...]

    def detail_for(self, run_id: str) -> RunDetailSnapshot:
        """Return the scan-time detail metadata for a discovered run."""
        for detail in self.run_details:
            if detail.summary.run_id == run_id:
                return detail
        raise KeyError(run_id)


def build_snapshot(project: ProjectResolution) -> ProjectSnapshot:
    """Build a new bounded snapshot from disk without retaining prior values.

    This deliberately performs no caching or retrying. A candidate beyond the
    scan limit is left for a later refresh and reported as incomplete.
    """
    runs, has_excess_candidates = discover_runs_bounded(
        project.git_common_dir, candidate_limit=MAX_RUN_CANDIDATES
    )
    diagnostics = (
        (
            SnapshotDiagnostic(
                code="run_candidates_incomplete",
                message=(
                    f"Run discovery stopped after {MAX_RUN_CANDIDATES} candidates; "
                    "additional candidates were not scanned."
                ),
            ),
        )
        if has_excess_candidates
        else ()
    )
    run_tuple = tuple(runs)
    run_details: list[RunDetailSnapshot] = []
    for summary in run_tuple:
        history = load_history(project.git_common_dir, summary.run_id)
        current = load_current_run(project.git_common_dir, summary.run_id)
        run_details.append(
            RunDetailSnapshot(
                summary=summary,
                current=current,
                history=history,
                current_state_disagreements=_current_state_disagreements(history, current),
                verification=discover_verification(
                    project.git_common_dir, summary.run_id, current.verification_result
                ),
            )
        )
    return ProjectSnapshot(
        project=project,
        runs=run_tuple,
        run_details=tuple(run_details),
        lock=observe_lock(project.git_common_dir, project.integration_root, run_tuple),
        diagnostics=diagnostics,
    )


_COUNTER_FIELDS: tuple[CurrentStateDisagreementField, ...] = (
    "accepted_task_count",
    "revision_count",
    "replan_count",
    "architect_retry_count",
    "builder_guidance_count",
)


def _current_state_disagreements(
    history: HistoryLoad, current: CurrentRun
) -> tuple[CurrentStateDisagreement, ...]:
    """Compare newest valid history with current state under ADR 0039's ordering gate."""
    if not current.loadable or not history.entries:
        return ()

    newest = history.entries[-1]
    if current.updated_at is None:
        return ()
    if _parse_timestamp(newest.recorded_at) > _parse_timestamp(current.updated_at):
        disagreements: list[CurrentStateDisagreement] = []
        if newest.phase_after != current.phase:
            disagreements.append(CurrentStateDisagreement("phase", newest.seq))
        for field in _COUNTER_FIELDS:
            if newest.counters[field] != getattr(current, field):
                disagreements.append(CurrentStateDisagreement(field, newest.seq))
        return tuple(disagreements)

    accepted_task_count = current.accepted_task_count
    if (
        accepted_task_count is not None
        and newest.counters["accepted_task_count"] > accepted_task_count
    ):
        return (CurrentStateDisagreement("accepted_task_count", newest.seq),)
    return ()


def _parse_timestamp(value: str) -> datetime:
    """Parse an instant already validated by the authoritative source reader."""
    return datetime.fromisoformat(value)


def scan_project(project_path: Path | str | None = None) -> ProjectSnapshot:
    """Resolve a project and build its fresh read-model snapshot."""
    return build_snapshot(resolve_project(project_path))
