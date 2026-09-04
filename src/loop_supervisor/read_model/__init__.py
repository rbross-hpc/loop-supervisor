"""Presentation-independent readers for supervisor-owned disk artifacts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .discovery import RunSummary
    from .history import HistoryDiagnostic, HistoryEntry, HistoryLoad, HistoryStatus
    from .lock_observation import ActivityLabel, LockActivity, LockObservation, RunActivity

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
    "discover_runs",
    "load_history",
    "observe_lock",
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
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
