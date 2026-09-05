"""Fresh, bounded composition of the read model consumed by presentation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .current_run import CurrentRun, load_current_run
from .discovery import RunSummary, discover_runs_bounded
from .history import HistoryLoad, load_history
from .lock_observation import LockObservation, observe_lock
from .project import ProjectResolution, resolve_project
from .verification import VerificationDiscovery, discover_verification

MAX_RUN_CANDIDATES = 10_000


@dataclass(frozen=True)
class SnapshotDiagnostic:
    """A safe project-scan diagnostic that does not belong to one run."""

    code: str
    message: str


@dataclass(frozen=True)
class RunDetailSnapshot:
    """One run's immutable current, history, and verification evidence."""

    summary: RunSummary
    current: CurrentRun
    history: HistoryLoad
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
        current = load_current_run(project.git_common_dir, summary.run_id)
        run_details.append(
            RunDetailSnapshot(
                summary=summary,
                current=current,
                history=load_history(project.git_common_dir, summary.run_id),
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


def scan_project(project_path: Path | str | None = None) -> ProjectSnapshot:
    """Resolve a project and build its fresh read-model snapshot."""
    return build_snapshot(resolve_project(project_path))
