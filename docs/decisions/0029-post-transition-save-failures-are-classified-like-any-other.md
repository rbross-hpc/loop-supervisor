# Post-transition save failures are classified like any other operational failure

## Status

Accepted

## Context

`advance()` dispatches exactly one phase per call inside a `try` block
whose `except` clauses classify a fixed set of exceptions
(`AgentInvocationError`, `PhaseTimeoutError`, `GitError`,
`DecisionError`, `ContractError`, `ProvisioningError`) into a durable,
retryable `OperationalErrorRecord` via `_handle_operational_failure`.
But three `_save()` calls -- the `PHASE_AWAITING_INPUT` save, and the
two calls on the generic success path (`INPUT_REQUIRED` and
`ADVANCED`) -- sat *outside* that `try` block. `_save()` calls
`_checkpoint()`, which calls `repo.head_commit()`/
`repo.status_snapshot()`, both of which can raise `GitError`. `GitError`
was already one of the classified exception types; it was simply
unreachable from these three call sites, because the `try` block had
already exited by the time they ran.

The practical consequence: a `_save()` failure immediately after an
otherwise-successful phase transition escaped as a raw, unclassified
exception, out through `RunSession.advance()`/`run_to_completion()`,
all the way to the CLI's generic error handler. No
`OperationalErrorRecord` was written, no retry classification
happened, and the transition that had just completed in memory (e.g.
`_do_planning` moving `state.phase` from `planning` to
`creating_worktree`) was lost -- the next `resume` would reload the
last *persisted* state, which still showed the old phase, silently
discarding real completed work.

A naive fix -- wrap all three calls in the same classification, using
`state.phase` (the phase the dispatch just transitioned into) as the
retry target -- has a hidden defect: `_do_planning` can transition
`state.phase` straight to the terminal `PHASE_DONE` (when
`max_accepted_tasks` is exhausted, or the planner returns `COMPLETE`)
before falling through to the generic success-path save. Terminal
phases are excluded from `RETRY_TARGET_PHASES` by construction (a
terminal phase is never resumed into), so classifying a save failure
against `PHASE_DONE` would construct an `OperationalErrorRecord` that
`OperationalErrorRecord.from_dict`'s own validation rejects the next
time it is loaded -- turning a transient Git error into a permanently
unloadable run.

## Decision

A new `Supervisor._save_after_transition()` helper wraps `_save()` in
the same exception tuple `advance()`'s in-`try` dispatch catches
(`_OPERATIONAL_FAILURE_EXCEPTIONS`, now a shared module-level
constant instead of an inline tuple literal), and on failure routes to
the existing `_handle_operational_failure()` -- the identical path any
other operational failure discovered during dispatch already takes.
All three success-path `_save()` calls in `advance()` now go through
this helper instead of calling `self._save(state)` directly.

The classification target is `state.phase` (the phase already
transitioned into), **except** when that phase is terminal, in which
case `phase_before` (the phase the dispatch ran from) is used instead.
`phase_before` is guaranteed non-terminal (`advance()` returns early
for a terminal `phase_before` before the `try` block is ever entered)
and is always a valid retry target. Retrying `phase_before` after a
terminal transition re-runs the dispatch that just produced that
transition (e.g. re-invoking the planner); this is safe because
`planning` is not a durable side-effect phase and produces an
equivalent result on retry, the same property `_do_verifying` already
relies on for its own retry safety.

## Consequences

- A `_save()` failure at any of the three previously-unprotected call
  sites is now a durable, retryable `OperationalErrorRecord`
  targeting the correct phase, exactly like any other operational
  failure -- no raw exception escapes `advance()`, and no completed
  transition is silently lost.
- The one case where the naive fix would have produced an invalid,
  permanently-unloadable state (a save failure immediately after
  `_do_planning` reaches `PHASE_DONE`) instead correctly retries from
  `planning`, at the cost of one redundant planner invocation if the
  retry succeeds -- an acceptable tradeoff for a transient-failure
  path that is expected to be rare.
- `_OPERATIONAL_FAILURE_EXCEPTIONS` is now defined once and reused by
  both `advance()`'s in-`try` dispatch and `_save_after_transition()`,
  removing a duplication that would otherwise have to be kept in sync
  by hand if the classified exception set ever changes.
- No new tests were needed for `PHASE_AWAITING_INPUT`'s specific save
  call beyond the general coverage `_save_after_transition()` now
  gets: it shares the exact same helper and exception handling as the
  other two call sites, so a single set of regression tests (ordinary
  post-transition failure; the terminal-phase edge case) exercises
  the shared code path.
</content>
