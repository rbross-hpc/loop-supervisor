# Decision requests are durable and origin-aware; approval never re-invokes the architect

## Status

Accepted

## Context

ADR 0003 established that either the planner or the auditor can escalate
a design question to `loop-architect`, and that the supervisor (not the
architect) writes the approved ADR. The first implementation of that flow
had two gaps:

- The auditor's `decision_required` flag on a `REPLAN` disposition was
  accepted by the contract but never actually routed to the architect —
  it silently behaved like an ordinary replan. There was also no
  persisted record of *which* role asked *which* question, so a resumed
  run had no reliable way to know where a pending decision came from or
  where it should continue to afterward.
- Approving a `DECIDED` proposal under `--require-decision-approval`
  routed back into the same code path that invokes the architect, so a
  paused-and-resumed approval could re-invoke the architect instead of
  simply recording the already-proposed, already-reviewed ADR. This
  meant "what was reviewed" and "what got written" were not guaranteed
  to be the same text across a process restart.

## Decision

- A `DecisionRequest` (`origin`, `question`, `rationale`) is persisted in
  run state independently of `planner_result`/`auditor_result` whenever
  either role escalates. `origin` is `"planner"` or `"auditor"` and
  determines the continuation once the decision is resolved:
  planner-originated decisions continue to the **builder**;
  auditor-originated decisions return to the **planner**, on the same
  preserved task worktree/branch, with the auditor's findings/design
  observations and the recorded decision included in its prompt.
- The auditor's contract now requires `decision_required: true` to be
  paired with disposition `REPLAN` (mirroring the planner's existing
  requirement that it be paired with status `READY`); other pairings are
  a contract violation, not a silently-ignored flag.
- The architect must answer the exact question in the active
  `DecisionRequest`; a mismatched `question` field is treated as a
  contract violation rather than accepted.
- Approval of a `DECIDED` result is a separate transition from
  requesting one. When approval is required, the proposal is saved to
  run state as soon as the architect returns it; the pending question
  asks the operator to approve that already-persisted result. Answering
  "yes" — even after a full pause/resume cycle — writes the exact
  persisted ADR and advances to the correct continuation without calling
  the architect again. Only rejecting (with feedback) invokes the
  architect for a new attempt.
- The planner returning `COMPLETE` while a task worktree is still active
  is treated as a supervisor-level error rather than silently completing
  the run out from under an unresolved task.

## Consequences

- A decision escalated by the auditor genuinely reaches the architect,
  and the task is genuinely replanned afterward instead of resuming
  as-is — closing a gap where the documented behavior and the actual
  behavior had diverged.
- "What the operator approved" and "what got committed as an ADR" are
  provably the same text, including across a process restart, because
  approval never triggers a new model call.
- Rejecting a decision proposal is now clearly distinguished from
  resuming a pending approval: only the former causes another architect
  invocation.
