"""Fresh, bounded composition of the read model consumed by presentation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .discovery import RunSummary, discover_runs_bounded
from .lock_observation import LockObservation, observe_lock
from .project import ProjectResolution, resolve_project

MAX_RUN_CANDIDATES = 10_000


@dataclass(frozen=True)
class SnapshotDiagnostic:
    """A safe project-scan diagnostic that does not belong to one run."""

    code: str
    message: str


@dataclass(frozen=True)
class ProjectSnapshot:
    """One immutable, best-effort view of project artifacts at scan time."""

    project: ProjectResolution
    runs: tuple[RunSummary, ...]
    lock: LockObservation
    diagnostics: tuple[SnapshotDiagnostic, ...]


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
    return ProjectSnapshot(
        project=project,
        runs=run_tuple,
        lock=observe_lock(project.git_common_dir, project.integration_root, run_tuple),
        diagnostics=diagnostics,
    )


def scan_project(project_path: Path | str | None = None) -> ProjectSnapshot:
    """Resolve a project and build its fresh read-model snapshot."""
    return build_snapshot(resolve_project(project_path))
