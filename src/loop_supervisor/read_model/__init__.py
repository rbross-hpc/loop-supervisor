"""Presentation-independent readers for supervisor-owned disk artifacts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .discovery import RunSummary

__all__ = ["RunSummary", "discover_runs"]


def __getattr__(name: str) -> Any:
    """Lazily expose readers so state can import the JSON utility without a cycle."""
    if name in __all__:
        from . import discovery

        return getattr(discovery, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
