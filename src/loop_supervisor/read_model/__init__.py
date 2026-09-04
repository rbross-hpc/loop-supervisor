"""Presentation-independent readers for supervisor-owned disk artifacts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .discovery import RunSummary
    from .history import HistoryDiagnostic, HistoryEntry, HistoryLoad, HistoryStatus

__all__ = [
    "HistoryDiagnostic",
    "HistoryEntry",
    "HistoryLoad",
    "HistoryStatus",
    "RunSummary",
    "discover_runs",
    "load_history",
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
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
