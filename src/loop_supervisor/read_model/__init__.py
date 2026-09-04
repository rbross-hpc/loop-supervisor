"""Presentation-independent readers for supervisor-owned disk artifacts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .current_run import CurrentRun
    from .discovery import RunSummary
    from .history import HistoryDiagnostic, HistoryEntry, HistoryLoad, HistoryStatus
    from .lock_observation import ActivityLabel, LockActivity, LockObservation, RunActivity
    from .project import ProjectResolution, ProjectResolutionError
    from .snapshot import ProjectSnapshot, SnapshotDiagnostic
    from .verification import (
        LogContent,
        LogReference,
        VerificationAttempt,
        VerificationDiagnostic,
        VerificationDiscovery,
    )

__all__ = [
    "CurrentRun",
    "HistoryDiagnostic",
    "HistoryEntry",
    "HistoryLoad",
    "HistoryStatus",
    "ActivityLabel",
    "LockActivity",
    "LockObservation",
    "RunActivity",
    "ProjectResolution",
    "ProjectResolutionError",
    "ProjectSnapshot",
    "RunSummary",
    "SnapshotDiagnostic",
    "LogContent",
    "LogReference",
    "VerificationAttempt",
    "VerificationDiagnostic",
    "VerificationDiscovery",
    "build_snapshot",
    "discover_runs",
    "discover_verification",
    "load_current_run",
    "load_history",
    "observe_lock",
    "read_log",
    "resolve_project",
    "scan_project",
]


def __getattr__(name: str) -> Any:
    """Lazily expose readers so state can import the JSON utility without a cycle."""
    if name in {"CurrentRun", "load_current_run"}:
        from . import current_run

        return getattr(current_run, name)
    if name in {"RunSummary", "discover_runs"}:
        from . import discovery

        return getattr(discovery, name)
    if name in {
        "HistoryDiagnostic",
        "HistoryEntry",
        "HistoryLoad",
        "HistoryStatus",
        "load_history",
    }:
        from . import history

        return getattr(history, name)
    if name in {"ActivityLabel", "LockActivity", "LockObservation", "RunActivity", "observe_lock"}:
        from . import lock_observation

        return getattr(lock_observation, name)
    if name in {"ProjectResolution", "ProjectResolutionError", "resolve_project"}:
        from . import project

        return getattr(project, name)
    if name in {"ProjectSnapshot", "SnapshotDiagnostic", "build_snapshot", "scan_project"}:
        from . import snapshot

        return getattr(snapshot, name)
    if name in {
        "LogContent",
        "LogReference",
        "VerificationAttempt",
        "VerificationDiagnostic",
        "VerificationDiscovery",
        "discover_verification",
        "read_log",
    }:
        from . import verification

        return getattr(verification, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
