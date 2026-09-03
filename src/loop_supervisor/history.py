"""Always-on, append-only per-phase history capture for headless `run`/
`resume` (ADR 0034).

`RunState` keeps exactly one slot per role (`planner_result`,
`builder_result`, `auditor_result`, ...), overwritten on every re-entry
of that phase: a task revised three times leaves only the third
`builder_result` on disk, and the only surviving evidence a phase ran
more than once is a bare counter (`revision_count`, `replan_count`,
...). This module fixes that by writing one small JSON record per
`Supervisor.advance()` call to:

    <git-common-dir>/loop-supervisor/runs/<run_id>/NNNN-<phase>.json

`<run_id>.json` (the existing latest-state snapshot, unchanged) and
`<run_id>/` (this module's per-execution history) are siblings under
`runs/`; `state.list_runs()` globs `*.json` files only, so the bare
history directory is invisible to it and can never register as a
phantom run.

Deliberately cheap and observational (Q1 "outcome-only" from the
design discussion): each record captures only what `AdvanceOutcome`
and `RunState` already carry -- the phase that ran, its outcome, the
run's counters, and the role-result object it just wrote -- never the
literal agent prompt (that is available, in full, per invocation, from
the OpenCode session database; see ADR 0033's `-v`/`-vv` diagnostics
for the closest in-repo analogue). Verification is a special case:
only the small structured `verification_result` summary is captured,
never the full command output already written under
`verification/<run_id>/<commit>/` (ADR 0027, ADR 0028) -- duplicating
that here was explicitly ruled out.

Installed as a plain `on_advance` callback (see `Supervisor.run`'s
docstring): a raising or slow recorder can never abort or delay a run.
`PhaseHistoryRecorder.on_advance` never raises on its own -- a write
failure is reported to stderr and that one record is dropped, exactly
the same "observability must not be able to fail the run" contract
`VerboseReporter.on_advance` (ADR 0033) already follows.

This is genuinely log output, not something the supervisor reads back
and interprets (unlike `state.py`'s `save_state`/`load_state`), so it
deliberately does not carry that module's `O_NOFOLLOW`/`dir_fd`/atomic-
replace hardening: a plain `mkdir` + `write_text` under a 0700
directory is proportionate here.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .locking import lock_is_present
from .state import OperationalErrorRecord, RunState, list_runs, load_state, validate_run_id
from .supervisor import AdvanceOutcome, _redact_secrets

_VERIFICATION_DIR_NAME = "verification"

# Role-result field on RunState keyed by the phase that produced it.
# awaiting_input/creating_worktree/recording_decision/merging/
# cleanup_worktree/cleanup_branch/operational_failure have no result
# object of their own -- their outcome is fully described by
# phase_before/phase_after/status/error alone.
_RESULT_FIELD_BY_PHASE: dict[str, str] = {
    "planning": "planner_result",
    "architecting": "architect_result",
    "building": "builder_result",
    "verifying": "verification_result",
    "auditing": "auditor_result",
}

_COUNTER_FIELDS = (
    "accepted_task_count",
    "revision_count",
    "replan_count",
    "architect_retry_count",
    "builder_guidance_count",
)

_SEQ_WIDTH = 4


def history_dir(git_common_dir: Path, run_id: str) -> Path:
    """The per-execution history directory for `run_id`.

    Validates `run_id` first (reusing `state.py`'s check), so a crafted
    or corrupted run ID can never be used to construct a path outside
    `runs/`.
    """
    validated = validate_run_id(run_id)
    return git_common_dir / "loop-supervisor" / "runs" / validated


def _redact(value: Any) -> Any:
    """Recursively apply the existing secret redaction to every string
    reachable from `value`. Reuses `supervisor.py`'s `_redact_secrets`
    (the same best-effort scrubbing already used for
    `OperationalErrorRecord.message`) rather than a second
    implementation, applied broadly here since a captured record may
    otherwise carry free text from any agent's response."""
    if isinstance(value, str):
        return _redact_secrets(value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    return value


def _existing_seq_numbers(directory: Path) -> list[int]:
    if not directory.exists():
        return []
    numbers = []
    for path in directory.glob("*.json"):
        prefix = path.stem.split("-", 1)[0]
        if prefix.isdigit():
            numbers.append(int(prefix))
    return numbers


@dataclass
class PhaseHistoryRecorder:
    """`on_advance` callback that appends one history record per
    `advance()` call. One instance is scoped to a single run (its
    `_next_seq` cache is per run_id); construct a fresh instance per
    `RunSession`/CLI invocation, same as `VerboseReporter`.
    """

    _next_seq: dict[str, int] = field(default_factory=dict)

    def on_advance(self, outcome: AdvanceOutcome) -> None:
        try:
            self._write_record(outcome)
        except Exception as exc:
            print(
                f"loop-supervisor: could not record phase history: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    def _write_record(self, outcome: AdvanceOutcome) -> None:
        state = outcome.state
        git_common_dir = Path(state.git_common_dir)
        directory = history_dir(git_common_dir, state.run_id)

        seq = self._next_seq.get(state.run_id)
        if seq is None:
            existing = _existing_seq_numbers(directory)
            seq = (max(existing) + 1) if existing else 1
        self._next_seq[state.run_id] = seq + 1

        record = self._build_record(outcome, seq=seq)

        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        target = directory / f"{seq:0{_SEQ_WIDTH}d}-{outcome.phase_before}.json"
        target.write_text(json.dumps(record, indent=2, sort_keys=True, default=str) + "\n")
        os.chmod(target, 0o600)

    def _build_record(self, outcome: AdvanceOutcome, *, seq: int) -> dict[str, Any]:
        state = outcome.state
        phase = outcome.phase_before

        # For `verifying`, `state.verification_result` (built by
        # `_summarize_verification`) is already the compact form: a
        # per-command bool/returncode/duration/output_path plus a
        # bounded, redacted summary string, never the full command
        # output -- that always lives only on disk under
        # verification/<run_id>/<commit>/ (ADR 0027, ADR 0028).
        # Capturing it verbatim therefore never duplicates the log.
        result: Any = None
        result_field = _RESULT_FIELD_BY_PHASE.get(phase)
        if result_field is not None:
            result = getattr(state, result_field, None)

        error: dict[str, Any] | None = None
        if outcome.error is not None:
            if isinstance(state.last_error, dict):
                try:
                    error = OperationalErrorRecord.from_dict(state.last_error).to_dict()
                except Exception:
                    error = {"message": str(outcome.error)}
            else:
                error = {"message": str(outcome.error)}

        record: dict[str, Any] = {
            "seq": seq,
            "run_id": state.run_id,
            "phase": phase,
            "phase_after": outcome.phase_after,
            "status": outcome.status.value,
            "recorded_at": datetime.now(UTC).isoformat(),
            "original_task_id": state.original_task_id,
            "counters": {name: getattr(state, name) for name in _COUNTER_FIELDS},
            "result": result,
            "error": error,
        }
        return _redact(record)


@dataclass(frozen=True)
class PruneCandidate:
    """One run selected (or skipped) by `select_prune_candidates`."""

    run_id: str
    updated_at: str
    phase: str
    has_history: bool
    has_verification: bool


class PruneError(RuntimeError):
    """Raised when pruning cannot proceed safely."""


def select_prune_candidates(
    git_common_dir: Path,
    *,
    keep_last: int | None = None,
    older_than_days: float | None = None,
    run_ids: list[str] | None = None,
) -> list[PruneCandidate]:
    """Select which saved runs `runs prune` would remove.

    Exactly one selection mode: an explicit `run_ids` list, or
    `keep_last`/`older_than_days` (either or both, applied together --
    a run must satisfy *both* supplied conditions to be selected) over
    every saved run, newest-first by `updated_at`. Never raises for an
    empty result; the caller decides what an empty selection means.
    """
    all_ids = list_runs(git_common_dir)
    loaded: list[tuple[str, RunState]] = []
    for run_id in all_ids:
        try:
            loaded.append((run_id, load_state(git_common_dir, run_id)))
        except Exception:
            # A corrupted or unreadable run state is not this command's
            # concern to repair; skip it rather than fail the whole
            # selection (list_runs() already tolerates this at the
            # filename level -- this extends the same tolerance to a
            # file that parses as a name but not as valid state).
            continue
    loaded.sort(key=lambda pair: pair[1].updated_at, reverse=True)

    if run_ids is not None:
        wanted = set(run_ids)
        selected = [(rid, state) for rid, state in loaded if rid in wanted]
    else:
        selected = loaded
        if keep_last is not None:
            selected = selected[keep_last:]
        if older_than_days is not None:
            cutoff = datetime.now(UTC).timestamp() - older_than_days * 86400
            selected = [
                (rid, state)
                for rid, state in selected
                if datetime.fromisoformat(state.updated_at).timestamp() < cutoff
            ]

    candidates = []
    for run_id, state in selected:
        candidates.append(
            PruneCandidate(
                run_id=run_id,
                updated_at=state.updated_at,
                phase=state.phase,
                has_history=history_dir(git_common_dir, run_id).exists(),
                has_verification=(
                    git_common_dir / "loop-supervisor" / _VERIFICATION_DIR_NAME / run_id
                ).exists(),
            )
        )
    return candidates


def prune_runs(
    git_common_dir: Path,
    candidates: list[PruneCandidate],
    *,
    include_verification: bool = False,
) -> list[str]:
    """Delete the state file, history directory, and (optionally)
    verification logs for each candidate. Refuses outright if a
    supervisor lock is present anywhere in this repository: a run
    currently in progress must never have its history or state removed
    out from under it, and a lock's `run_id` is not always populated
    (a fresh `run` never records one -- see `locking.py`), so this
    check is deliberately repository-wide rather than per-run.

    Returns the list of run IDs actually removed.
    """
    if lock_is_present(git_common_dir):
        raise PruneError(
            "a supervisor lock is present; refusing to prune while a run may be active"
        )

    removed = []
    runs_root = git_common_dir / "loop-supervisor" / "runs"
    verification_root = git_common_dir / "loop-supervisor" / _VERIFICATION_DIR_NAME
    for candidate in candidates:
        run_id = validate_run_id(candidate.run_id)
        state_path = runs_root / f"{run_id}.json"
        history_path = runs_root / run_id
        state_path.unlink(missing_ok=True)
        if history_path.exists():
            shutil.rmtree(history_path)
        if include_verification:
            verification_path = verification_root / run_id
            if verification_path.exists():
                shutil.rmtree(verification_path)
        removed.append(run_id)
    return removed
