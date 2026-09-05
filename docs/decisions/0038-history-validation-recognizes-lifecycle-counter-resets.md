# History validation recognizes lifecycle counter resets

## Status

Accepted

## Context

History contradiction detection compares counters in successive phase records. The five persisted counters do not share one invariant. accepted_task_count is cumulative, but revision_count and replan_count are per-task limits, architect_retry_count is scoped to an unresolved decision, and builder_guidance_count counts consecutive non-COMPLETE builder results. The supervisor therefore resets four counters during documented lifecycle transitions. Treating every decrease as corruption marks valid histories incomplete, while changing all counters to cumulative values would alter established writer and policy semantics.

## Decision

History validation is transition-aware rather than applying blanket monotonicity to all counters. accepted_task_count must never decrease. A decrease in any other counter is valid only when the following value is exactly zero and the record containing that value represents a documented reset transition: revision_count may reset on planning to building or architecting, creating_worktree to building or architecting, or cleanup_branch to planning; replan_count may reset only on cleanup_branch to planning; architect_retry_count may reset on recording_decision to building or planning, or cleanup_branch to planning; and builder_guidance_count may reset on planning to building or architecting, creating_worktree to building or architecting, a successful building transition to verifying or auditing, or cleanup_branch to planning. Every decrease to a nonzero value, every decrease of accepted_task_count, and every decrease outside these combinations is a counter regression diagnostic. The combinations are evaluated from the phase and phase_after of the record whose counters decreased; validation does not consult current RunState. Persisted counter meanings and writer behavior are not redesigned.

## Consequences

- Valid histories spanning task cleanup, replanning, decision completion, worktree creation, or successful recovery from builder guidance remain complete when no other contradiction exists.
- The reader still diagnoses unauthorized decreases independently for each counter and preserves every validated history entry unchanged.
- Tests must cover each permitted reset family, including cleanup_branch to planning and successful building to verifying or auditing, plus decreases to nonzero values and decreases on unauthorized transitions.
- Future writer changes that add or move a reset must update this transition table and its tests; a future need for lifetime totals should add separately named cumulative metrics rather than changing these counters' established meanings.
