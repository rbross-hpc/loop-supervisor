"""Canonical run-phase vocabulary, shared by state.py and supervisor.py.

Kept in its own module (rather than defined in supervisor.py and imported
by state.py) so that state.py — which is responsible for validating
persisted data before any supervisor logic runs — never needs to import
the state-machine implementation itself.
"""

from __future__ import annotations

PHASE_PLANNING = "planning"
PHASE_ARCHITECTING = "architecting"
PHASE_BUILDING = "building"
PHASE_AUDITING = "auditing"
PHASE_AWAITING_INPUT = "awaiting_input"
PHASE_DONE = "done"
PHASE_FAILED = "failed"
PHASE_CREATING_WORKTREE = "creating_worktree"
PHASE_RECORDING_DECISION = "recording_decision"
PHASE_MERGING = "merging"
PHASE_CLEANUP_WORKTREE = "cleanup_worktree"
PHASE_CLEANUP_BRANCH = "cleanup_branch"
PHASE_OPERATIONAL_FAILURE = "operational_failure"

TERMINAL_PHASES = frozenset({PHASE_DONE, PHASE_FAILED})

DURABLE_SIDE_EFFECT_PHASES = frozenset(
    {
        PHASE_CREATING_WORKTREE,
        PHASE_RECORDING_DECISION,
        PHASE_MERGING,
        PHASE_CLEANUP_WORKTREE,
        PHASE_CLEANUP_BRANCH,
    }
)

# Every phase a persisted RunState.phase may legitimately hold.
ALL_PHASES = frozenset(
    {
        PHASE_PLANNING,
        PHASE_ARCHITECTING,
        PHASE_BUILDING,
        PHASE_AUDITING,
        PHASE_AWAITING_INPUT,
        PHASE_DONE,
        PHASE_FAILED,
        PHASE_CREATING_WORKTREE,
        PHASE_RECORDING_DECISION,
        PHASE_MERGING,
        PHASE_CLEANUP_WORKTREE,
        PHASE_CLEANUP_BRANCH,
        PHASE_OPERATIONAL_FAILURE,
    }
)

# Phases an OperationalErrorRecord.retry_phase may legitimately name.
# Excludes operational_failure itself (a retryable record can never point
# back to the wrapper state — that would erase the real interrupted phase
# on the next failure) and the two terminal phases (never resumed into).
RETRY_TARGET_PHASES = frozenset(ALL_PHASES - TERMINAL_PHASES - {PHASE_OPERATIONAL_FAILURE})
