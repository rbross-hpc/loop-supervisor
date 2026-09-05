# Gate newest-history comparisons by persistence order

## Status

Accepted

## Context

The history reader validates records against one another but does not yet compare the
newest valid record with authoritative current RunState. Such a comparison must tolerate
normal writer ordering and incomplete observability. Each advance persists RunState first
and invokes the best-effort history recorder afterward, so a matching history record
normally has a recorded_at later than the state's updated_at. A later state transition can
also be persisted while its history callback is pending or can remain permanently
unrecorded after a callback failure. ADR 0038 additionally establishes that
accepted_task_count is cumulative while the other four counters can reset only at
lifecycle-specific transitions. A phase-blind comparison would therefore report valid
mid-lifecycle states as contradictions.

## Decision

The snapshot coordinator loads a run's bounded history before loading the immutable
authoritative current-state view used for this comparison. No cross-source comparison is
made when current state is unloadable or there is no valid history entry. Parse both
timestamps as instants. If the newest valid entry's recorded_at is strictly later than
RunState.updated_at, treat the entry as claiming the same persisted post-transition state:
report a current-state disagreement when phase_after differs from RunState.phase, and
report one disagreement for each of the five counters whose values are not equal. Exact
counter equality is required in this window because every reset permitted by ADR 0038 has
already been applied to both snapshots by the recorded transition; this is not a
phase-blind monotonicity test.

If RunState.updated_at is equal to or later than the newest recorded_at, conservatively
treat RunState as possibly containing one or more later transitions. In that case, do not
compare phase and do not diagnose differences in revision_count, replan_count,
architect_retry_count, or builder_guidance_count, because an omitted later transition may
legitimately change or reset them. Still report accepted_task_count when the newest
history value exceeds the current value, since that lifetime-cumulative counter may never
decrease; a current value equal to or greater than history is valid. A newest recorded_at
strictly after RunState.updated_at is only the same-transition comparison gate and is
never itself a diagnostic. The comparison does not use lock state, phase-graph
reachability, or history completeness to infer whether a later transition is pending.
Every valid history entry is retained, and RunState remains authoritative; diagnostics
never repair or override it.

## Consequences

- A normal history record written after its corresponding state save does not produce a
  timestamp diagnostic.
- Reading history before current state prevents a record created concurrently after an
  earlier current-state read from being misclassified as a contradiction.
- Phase and resettable-counter checks are suppressed whenever the current snapshot may be
  later, including equal timestamps, missing newest records, recorder failures, and valid
  lifecycle resets.
- accepted_task_count retains a useful cross-generation invariant because ADR 0038
  forbids it from decreasing under every transition.
- Tests must cover coherent same-transition records, a pending later state transition,
  omitted or malformed newest records, every ADR 0038 reset family, equal timestamps,
  unloadable or absent sources, and genuine same-transition phase or counter disagreement.
