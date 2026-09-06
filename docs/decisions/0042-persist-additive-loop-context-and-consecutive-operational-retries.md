# Persist additive loop context and consecutive operational retries

## Status

Accepted

## Context

Items 31 and 32 add closely related loop-control state: context for the most
recently accepted task, a consecutive automatic operational-retry counter, and
its immutable run limit. Deciding the shapes together avoids two revisions to
persisted `RunState`.

The counter must coexist with ADR 0038's transition-aware history validation
and ADR 0039's persistence-ordered comparison between history and authoritative
current state. Current schema-version-1 state loading otherwise requires exact
field sets, including exact `RunOptions` fields, and ADR 0024 deliberately
provides no general versioned migration path. Existing runs consequently lack
these additive fields.

## Decision

Add `last_completed_task` to `RunState` as an optional object with `task_id`,
`objective`, and `rationale`, or `None`; add non-negative
`operational_retry_count` to `RunState`; and add non-negative
`max_operational_retries` to immutable `RunOptions` and `Limits`. Do not change
`STATE_SCHEMA_VERSION`. New runs default these values to `None`, zero, and
three respectively.

`_finish_task_cleanup` records the just-accepted planner task in
`last_completed_task` immediately before clearing `planner_result`. The next
planner invocation receives this context when it is present. It is context for
exactly one accepted-task boundary: successful acceptance of the next task
replaces it, rather than accumulating a task history. Detecting a deliberately
deferred item that survives more than one accepted task is explicitly out of
scope; this field is not a general deferral tracker.

For this contract, a successful `advance()` is a dispatch that completes its
phase work. Reset `operational_retry_count` to zero on every successful
`advance()`. The administrative `operational_failure`-to-`retry_phase`
unwrapping transition is recovery preparation, not a successful advance, and
retains the count until a subsequently dispatched phase advances successfully.
An input-unavailable no-op likewise does not reset it. This makes the counter
bound consecutive failures rather than the lifetime total.

`Supervisor.run()` automatically retries only an operational failure whose
record is both `retryable` and not `requires_repair`, and only when
`operational_retry_count` is below `max_operational_retries`. Immediately
before each automatic retry it increments and persists the counter, waits for a
short interrupt-safe delay, and continues the run loop so the normal retry
phase is dispatched. Thus the counter increments only for an auto-retried,
retryable, non-requires-repair failure; a human-initiated resume never
increments it. A zero limit disables automatic retries. A repair-required,
non-retryable, or exhausted failure stops for an operator as it does today.

`operational_retry_count` is the sixth history counter. Amend ADR 0038's reset
table so its decrease is valid only to exactly zero on every successful
non-recovery `advance()` transition; the operational-failure unwrapping
transition is not a permitted reset. All other decreases are counter-regression
diagnostics. Amend ADR 0039's same-persisted-transition comparison so this
counter joins exact equality when newest valid history is strictly newer than
current `RunState`. When current state may be later, suppress differences in it
like the other resettable counters; only cumulative `accepted_task_count` keeps
its cross-generation monotonic comparison.

Apply narrowly enumerated same-schema compatibility defaults before exact-field
validation. Missing `RunState.last_completed_task` becomes `None`, missing
`RunState.operational_retry_count` becomes zero, and missing
`RunOptions.max_operational_retries` becomes three. Default each independently,
so state written between the two implementation slices still loads. All
supplied and defaulted values receive normal validation; unknown fields and all
other missing fields remain errors. Saving a loaded legacy run writes the
complete current shape. Legacy history records with exactly the prior five
counters are accepted with `operational_retry_count` normalized to zero; new
records always write six counters. This narrow exception does not make an older
binary load new state or introduce a general migration path.

## Consequences

- The retry budget measures consecutive automatic retries and is exhausted only
  by eligible failures; successful advance clears it while retry unwrapping
  alone does not.
- History validation and current-state disagreement diagnostics cover the sixth
  counter without treating legitimate resets or persistence-order windows as
  corruption.
- Runs and history saved immediately before these additions remain resumable and
  inspectable with deterministic default meanings.
- `RunState`, `RunOptions`, history writing and reading, current-state
  snapshots, disagreement types, TUI counter rendering, planner prompt wiring,
  and `Supervisor.run()` require separate implementation slices and tests.
- The implementation must test permitted and forbidden counter decreases, both
  ADR 0039 timestamp windows, legacy five-counter history, and each legacy
  state-field omission.
- This narrowly qualifies ADR 0024's exact-field statement: explicitly named
  additive fields may be defaulted within the current schema, while unsupported
  schema versions and unrelated malformed state remain rejected.
