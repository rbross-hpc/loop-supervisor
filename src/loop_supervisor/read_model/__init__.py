"""Presentation-independent readers for supervisor-owned disk artifacts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .discovery import RunSummary
    from .history import HistoryDiagnostic, HistoryEntry, HistoryLoad, HistoryStatus
    from .lock_observation import ActivityLabel, LockActivity, LockObservation, RunActivity
    from .verification import (
        LogContent,
        LogReference,
        VerificationAttempt,
        VerificationDiagnostic,
        VerificationDiscovery,
    )

__all__ = [
    "HistoryDiagnostic",
    "HistoryEntry",
    "HistoryLoad",
    "HistoryStatus",
    "ActivityLabel",
    "LockActivity",
    "LockObservation",
    "RunActivity",
    "RunSummary",
    "LogContent",
    "LogReference",
    "VerificationAttempt",
    "VerificationDiagnostic",
    "VerificationDiscovery",
    "discover_runs",
    "discover_verification",
    "load_history",
    "observe_lock",
    "read_log",
]


def __getattr__(name: str) -> Any:
    """Lazily expose readers so state can import the JSON utility without a cycle."""
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
