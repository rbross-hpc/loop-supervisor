# Builder guidance circuit breaker escalates instead of failing outright

## Status

Accepted

## Context

`revision_count`, `replan_count`, and `architect_retry_count` all bound a
loop that could otherwise run forever, and all three raise `LoopError`
on exhaustion, taking the run straight to the terminal `failed` phase
(ADR 0009's "revision/replan/architect retry limits" category). No such
bound existed for a builder that repeatedly reports `BLOCKED` or
`INCOMPLETE`: `_do_building`'s non-`COMPLETE` branch always parked at
`awaiting_input` and, once the operator supplied any answer other than
`replan`, always routed straight back to `PHASE_BUILDING` with no
counter checked. An operator (or an unattended script feeding
`--recover-stale-lock`-style boilerplate answers) repeatedly typing
"keep going" could loop indefinitely on a task the builder was never
going to complete, burning role-timeout budget one `role_timeout` at a
time with no circuit breaker at all.

A second, related gap: the `builder_guidance` answer `replan` already
existed as an operator escape hatch (`_try_resolve_pending_input`,
sending the task back to the planner exactly like an auditor `REPLAN`
disposition), but it did not increment `replan_count`. An operator could
bypass `max_replans_per_task` entirely simply by answering `replan` to
guidance prompts often enough, defeating the limit's purpose for exactly
the operator-driven path where it matters most.

The three existing counters all fail closed the same way: raise
`LoopError`, land in `failed`, done. That is the wrong shape for this
case. `BLOCKED`/`INCOMPLETE` is squarely the builder reporting it needs
different instructions or a different task, not a policy violation --
the task worktree and its intermediate work are still worth
recovering, and a `replan` is very often the correct next step, not a
failure. Failing the task outright the moment the limit is hit would
discard that recoverability for what is frequently a completely benign
situation (a task whose scope turned out to be wrong, not a broken run).

## Decision

A new `builder_guidance_count` on `RunState`, bounded by
`RunOptions.max_builder_guidance_attempts` (default 3), counts
*consecutive* non-`COMPLETE` builder results -- `BLOCKED` and
`INCOMPLETE` share one counter, since both represent the same
underlying "the builder cannot finish as directed" outcome. It is reset
to zero the moment the builder produces a verified `COMPLETE` commit
(`_do_building`'s `COMPLETE` branch), not only at task boundaries: a
builder that just landed a real commit has demonstrably not exhausted
its ability to make progress, however many `INCOMPLETE`/`BLOCKED`
rounds preceded it earlier in the same task. Without this reset, a
long task that legitimately needs several builder rounds -- each
producing a genuine commit, with an auditor `REVISE` in between --
would eventually be circuit-broken purely for taking many rounds to
finish, exactly the healthy-but-long case this feature must not
penalize. Only an unbroken run of `max_builder_guidance_attempts`
non-`COMPLETE` results with no intervening `COMPLETE` trips the limit.

Below the limit, behavior is unchanged: `_do_building` parks a
`builder_guidance` question and, on any answer other than `replan`,
re-invokes the builder. At the limit, `_do_building` does **not**
re-invoke the builder. Instead it parks a new `pending_question.kind =
"builder_escalation"`, still at `awaiting_input`, with exactly two legal
answers:

- `replan` -- sends the task back to the planner, exactly like ordinary
  guidance's `replan` path, preserving the task worktree/branch and
  their intermediate commits.
- `abandon` -- raises `LoopError`, taking the run to the terminal
  `failed` phase. The task worktree and branch are left in place for
  manual inspection, the same as any other terminal failure (ADR 0009):
  nothing about exhausting the guidance limit warrants destroying work
  the operator may still want to salvage or diagnose by hand.

Any other answer re-asks the same escalation question rather than
silently falling through to either path -- garbage input must not be
interpreted as either "keep trying" (which would defeat the circuit
breaker) or "abandon" (which is destructive and should require an
unambiguous answer).

The two `replan` paths (ordinary `builder_guidance` and
`builder_escalation`) share one helper,
`_replan_from_awaiting_input`, which now increments and bounds-checks
`replan_count` exactly like an auditor `REPLAN` disposition does. This
closes the operator-replan bypass described above: an operator answering
`replan` to guidance, whether or not the guidance limit has been
reached, now counts against the same `max_replans_per_task` limit an
auditor-driven replan does.

`builder_guidance_count` also resets at the same two points
`revision_count` already resets when a task's builder-attempt cycle
restarts (`_do_planning`'s existing-worktree branch, taken on an
auditor `REPLAN`; and `_do_creating_worktree`'s worktree-creation
branch, taken when a brand-new task starts), plus
`_finish_task_cleanup` when a task is fully accepted. These are
redundant with the `COMPLETE`-branch reset for the ordinary REPLAN
path (a REPLAN is always preceded by a `COMPLETE` verification, which
already zeroed the counter) but are kept anyway, both for the
brand-new-task case (where there is no preceding `COMPLETE` to reset
from) and as defense in depth against a future code path reaching
`PHASE_BUILDING` some other way.

Adding `builder_escalation` to the `pending_question` vocabulary
required touching all three of `state.py`'s strict validators (recorded
here because their names alone don't make this obvious): the `contexts`
dict in `_validate_pending_question` (kind + context-shape allowlist),
the `if/elif/else` in `_validate_effective_phase_requirements`'s
`awaiting_input` block (which single-branch dispatches on kind), and
`_validate_pending_context_matches_source` (context-to-source
cross-check). `builder_escalation` reuses `builder_guidance`'s exact
context shape (`{"status": "BLOCKED" | "INCOMPLETE"}`) and is folded
into the same branch in each validator rather than added as a fourth
independent kind, since the two questions differ only in what answers
are legal and what happens next, not in what they're about.

`pending_question` must be cleared before either of the two `LoopError`
raises this feature introduces (`abandon`, and the replan-count
exhaustion inside `_replan_from_awaiting_input`). Per
`_validate_pending_question_phase`, a `failed` state may only retain a
pending question that is an *answered* `architect_input` or
`builder_guidance` -- the `failed_phase` recorded for both of these
paths is `awaiting_input`, which is not among the phases that
validator allows to retain one. A `builder_escalation` question left in
place would make the resulting terminal state permanently unloadable,
which is a materially worse outcome than the failure it was trying to
record in the first place.

This is a schema change with no migration path (ADR 0024): every
existing persisted `RunState` document is missing `options.
max_builder_guidance_attempts` and `builder_guidance_count`, and will be
rejected as an unknown/missing field the next time it is loaded. As with
every schema change made since ADR 0024, this is accepted as the cost of
a plain field addition, not treated as a defect to route around with
migration machinery this project has explicitly chosen not to carry.

## Consequences

- An unattended or careless operator can no longer loop the builder
  indefinitely on a task it cannot complete: after
  `max_builder_guidance_attempts` *consecutive* non-`COMPLETE` results,
  the supervisor refuses to re-invoke the builder and forces an
  explicit `replan` or `abandon` decision instead.
- A task that keeps making genuine progress -- every `INCOMPLETE`/
  `BLOCKED` round followed by a real verified commit, however many such
  rounds the task needs in total -- never trips the limit, because each
  `COMPLETE` resets the counter. The circuit breaker fires only on an
  unbroken run of failures, matching what the escalation's own message
  ("reported ... N times in a row") tells the operator.
- `replan`, from either path, now correctly costs one unit of
  `max_replans_per_task` budget, closing the bypass an operator
  previously had around that limit.
- Exhausting the guidance limit does not, by itself, fail the task: the
  default and only immediately-available recovery (`replan`) preserves
  the task worktree/branch and its intermediate commits, matching how
  every other `REPLAN`-shaped transition in this codebase already
  behaves. Only an explicit `abandon` answer fails the task, and even
  then the worktree/branch survive for inspection.
- Every persisted run created before this change becomes unloadable the
  next time schema validation runs, per ADR 0024's precedent; there is
  no migration path into this schema version and none is planned.
