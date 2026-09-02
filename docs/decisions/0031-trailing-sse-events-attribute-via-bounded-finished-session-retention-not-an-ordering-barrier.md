# Trailing SSE events attribute via bounded retention, not an ordering barrier

## Status

Accepted

## Context

LiveActivityReducer (tui/live.py) accumulates ephemeral OpenCode SSE
telemetry and attributes each session-bearing event under ADR 0019's
exact-match rule (session ID registered AND event.directory ==
registered directory; no prefix matching). Invocation lifetime is
driven by run_agent() (opencode.py), which returns when the HTTP
prompt response completes and, in its finally block, immediately pops
the session and notifies observers, posting InvocationFinished and
calling reducer.unregister_invocation. SSE is a separate asynchronous,
best-effort, lossy channel (GET /global/event). Trailing events for a
just-finished invocation (message.part.updated/delta, session.diff, a
late session.idle) routinely arrive after unregister; post-unregister
they either fail _is_attributed and are dropped, or are buffered in
_pending_events and evicted unrendered because the session can never
re-register. This is backlog item 15, and existing tests pin the
drop-on-finish behavior, so a change is an ADR-level decision touching
the reducer's concurrency, attribution, and memory invariants. Two
candidate contracts were considered: an ordering barrier delaying
invocation-finish until SSE drains, versus bounded retention of
finished-session attribution.

## Decision

Resolve trailing-event attribution with bounded finished-session
retention, not an ordering barrier. When an invocation is
unregistered, the reducer moves its (session_id -> exact directory)
attribution into a separate, bounded 'recently finished' map rather
than discarding it. While an entry is retained: (1) recognized
session-bearing events are still accepted only under ADR 0019's
unchanged exact-match rule — the event's session_id must match the
retained finished session AND its directory must exactly equal that
session's registered directory (no prefix/glob/containment matching;
file.edited directory-only attribution is unaffected); (2) accepted
trailing events update the invocation displayed with status 'done' —
the invocation is NOT resurrected to active/running, is not
re-counted against active _MAX_INVOCATIONS tracking, and does not
re-enter _active_invocations. When an attribution is evicted, its
session ID moves into a second oldest-first, four-entry tombstone
queue. Events for a tombstoned session are dropped rather than
buffered, so they cannot later replay into a registration that reuses
the session ID and directory; an explicit registration clears that
session's tombstone. Once a session ID leaves both bounded windows it
is again treated as an unknown session that may be awaiting first
registration. Both bounds are fixed architect-chosen constants,
consistent with ADR 0019, and are not operator-configurable.
run_agent()'s return, InvocationFinished, and unregister timing are
unchanged: the durable invocation-finish signal is never gated on the
ephemeral SSE channel (ADR 0020).

## Consequences

- ADR 0019's exact-attribution boundary is preserved verbatim:
  attribution still requires exact session ID AND exact directory
  match, and no prefix/child-path matching is introduced. Retention
  widens WHEN a session is eligible for attribution (a bounded grace
  after finish), never HOW an event is matched.
- Two fixed four-entry bounds — recently-finished attribution and
  evicted-session tombstones — join the existing _MAX_* constants in
  tui/live.py, each with bounded-lifecycle test coverage, keeping the
  reducer's memory footprint bounded. Retained entries hold only
  session-ID/directory metadata or session IDs, never event payloads.
- SSE remains strictly ephemeral and never gates durable state:
  run_agent()'s return and lock/phase semantics (ADR 0020) are
  untouched. The reducer absorbs the race entirely on the display
  side, so no coupling of durable invocation-finish to a lossy channel
  is introduced.
- The two tests that currently pin drop-on-finish
  (test_unregistered_session_events_after_finish_ignored,
  test_integration_delta_after_invocation_finished_ignored) must be
  updated to reflect the new contract: an event within the retention
  window and matching exactly is now attributed to the done
  invocation; an event past the bound, or with a mismatched directory,
  is still dropped. New tests must cover retention eviction,
  exact-directory rejection of a trailing event, and the
  no-resurrection-to-active property.
- Attribution is best-effort and time-bounded, not guaranteed: a
  trailing event arriving after the retention bound is intentionally
  dropped. This is an accepted limitation, matching the existing
  treatment of SSE as lossy (reconnect gaps, ADR 0019; upstream
  buffering, item 7.3). It resolves the common in-flight-at-finish
  case without promising completeness.
- The rejected alternative (an ordering barrier / delayed unregister
  until SSE drains) is documented as considered and declined: SSE
  offers no reliable drain signal, so a barrier degenerates into a
  timeout — bounded retention in disguise — while additionally
  inverting the SSE-never-gates-durable-phase separation of ADR 0020.
- Any future change that widens attribution matching, removes the
  retention bound, or lets a retained finished session re-enter active
  tracking contradicts this ADR and ADR 0019 and must be escalated to
  the architect rather than made silently.
