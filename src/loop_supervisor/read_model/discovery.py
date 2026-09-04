"""Secure, typed discovery of persisted supervisor run snapshots."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..state import StateError, load_state, open_state_directory, validate_run_id


@dataclass(frozen=True)
class RunSummary:
    """One discovered current-state candidate, valid or unloadable.

    Unloadable candidates deliberately carry no inferred phase or timestamps;
    ``diagnostic`` explains why the authoritative state reader could not load
    the snapshot.
    """

    run_id: str
    loadable: bool
    phase: str | None
    created_at: str | None
    updated_at: str | None
    diagnostic: str | None


def discover_runs(git_common_dir: Path) -> list[RunSummary]:
    """Securely enumerate and load all valid ``<run_id>.json`` candidates."""
    summaries, _ = discover_runs_bounded(git_common_dir, candidate_limit=None)
    return summaries


def discover_runs_bounded(
    git_common_dir: Path, *, candidate_limit: int | None
) -> tuple[list[RunSummary], bool]:
    """Discover at most ``candidate_limit`` candidates and report excess safely.

    Enumeration occurs through the state directory descriptor, not a glob. A
    candidate observed during enumeration remains represented if it disappears
    or otherwise becomes unloadable before ``load_state`` reads it.
    """
    candidate_ids, has_excess_candidates = _enumerate_candidate_ids(git_common_dir, candidate_limit)
    summaries = [_load_summary(git_common_dir, run_id) for run_id in candidate_ids]
    return _sort_summaries(summaries), has_excess_candidates


def _enumerate_candidate_ids(
    git_common_dir: Path, candidate_limit: int | None
) -> tuple[list[str], bool]:
    candidates: list[str] = []
    has_excess_candidates = False
    try:
        with open_state_directory(git_common_dir, create=False) as directory_fd:
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    name = entry.name
                    if not name.endswith(".json"):
                        continue
                    try:
                        candidates.append(validate_run_id(name.removesuffix(".json")))
                    except StateError:
                        continue
                    if candidate_limit is not None and len(candidates) > candidate_limit:
                        has_excess_candidates = True
                        break
    except StateError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            return [], False
        raise

    candidate_ids = sorted(set(candidates))
    if has_excess_candidates:
        assert candidate_limit is not None
        return candidate_ids[:candidate_limit], True
    return candidate_ids, False


def _load_summary(git_common_dir: Path, run_id: str) -> RunSummary:
    try:
        state = load_state(git_common_dir, run_id)
    except (StateError, OSError) as exc:
        return RunSummary(
            run_id=run_id,
            loadable=False,
            phase=None,
            created_at=None,
            updated_at=None,
            diagnostic=f"Run snapshot {run_id!r} is unloadable: {exc}",
        )
    return RunSummary(
        run_id=state.run_id,
        loadable=True,
        phase=state.phase,
        created_at=state.created_at,
        updated_at=state.updated_at,
        diagnostic=None,
    )


def _sort_summaries(summaries: list[RunSummary]) -> list[RunSummary]:
    """Put loadable rows newest-first; degraded rows follow without invented time."""
    loadable = [summary for summary in summaries if summary.loadable]
    degraded = [summary for summary in summaries if not summary.loadable]
    return sorted(loadable, key=_updated_at_instant, reverse=True) + sorted(
        degraded, key=lambda summary: summary.run_id
    )


def _updated_at_instant(summary: RunSummary) -> datetime:
    """Return the validated, timezone-aware snapshot update instant for ordering."""
    assert summary.updated_at is not None
    return datetime.fromisoformat(summary.updated_at)
