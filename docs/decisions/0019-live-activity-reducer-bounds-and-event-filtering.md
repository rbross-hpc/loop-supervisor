# Live-activity reducer bounds and event-filtering invariants

## Status

Accepted

## Context

`LiveActivityReducer` (`src/loop_supervisor/tui/live.py`) accumulates
ephemeral live OpenCode telemetry — active invocations, message text/
reasoning tails, tool calls, touched files, and a feed of status
events — into an immutable `LiveActivitySnapshot` the TUI renders
alongside the durable `RunState`. Two classes of invariant this code
actually enforces had no ADR home before this one, despite both being
concrete, load-bearing, and already covered by tests:

1. **Unbounded growth.** A long-running TUI session subscribes to
   `GET /global/event` for the lifetime of a run. Without bounds, text
   tails, tool lists, touched-file lists, the event feed, and the
   dedup window for already-seen event IDs would all grow without
   limit for the life of the process. Backlog item 20 asked only for
   the reducer's single-owner-thread contract to be documented (now
   ADR 0008's Consequences section); this ADR additionally captures
   the bounds themselves, which were previously only visible as module
   constants with no accompanying rationale.
2. **Event attribution.** SSE subscribes globally
   (`GET /global/event`, per ADR 0008), so a single stream carries
   events for every OpenCode instance the supervisor is driving —
   planner at the project root, and architect/builder/auditor each in
   their own task worktree. The reducer must attribute each event to
   the correct registered invocation, and must never let an event
   meant for one directory be attributed to another merely because one
   path happens to be a string prefix of the other (e.g. a sibling
   task worktree `project-task-01` vs `project-task-010`, or, more
   acutely, the integration project root vs a task worktree nested
   under it). This is a real security/correctness boundary, not a
   cosmetic one: it decides which invocation's live pane a given
   tool result or touched-file path is shown against.

## Decision

**Bounds** (module constants in `tui/live.py`, each with a
corresponding `test_*_bounded` test in `tests/test_live_reducer.py`):

| Constant | Value | Bounds |
|---|---|---|
| `_MAX_INVOCATIONS` | 4 | Concurrently displayed invocations (oldest evicted first) |
| `_MAX_FINISHED_INVOCATIONS` | 4 | Finished session-ID/directory attributions retained for trailing events (oldest evicted first) |
| `_MAX_FINISHED_SESSION_TOMBSTONES` | 4 | Evicted finished session IDs remembered so their events are dropped rather than buffered (oldest evicted first) |
| `_MAX_FEED_RECORDS` | 200 | Status/feed event history (`collections.deque(maxlen=...)`) |
| `_MAX_TEXT_TAIL` | 16 KiB | Each message's text and reasoning tail, independently |
| `_MAX_TOOLS` | 100 | Tool calls retained per message (oldest evicted first) |
| `_MAX_TOUCHED_FILES` | 200 | Distinct touched-file paths retained |
| `_MAX_TOOL_RESULT_SUMMARY` | 1 KiB | Each tool's stored result summary |
| `_MAX_EVENT_IDS` | 2048 | Deduplication window (`collections.deque(maxlen=...)`) |
| `_MAX_PENDING_EVENTS` | 256 | Session-bearing events awaiting invocation registration |
| `_MAX_NOTICES` | 20 | Visible ephemeral transport/reconciliation notices |

`_tail()` truncates to a byte bound while re-decoding as UTF-8 (never
splitting a multi-byte character), so `_MAX_TEXT_TAIL` is an exact
byte ceiling, not an approximate one.

**Event attribution — exact match only, never prefix matching:**

- `_is_attributed()` (session-bearing events: `session.status`,
  `session.idle`, `session.error`, `message.*`, `todo.updated`,
  `session.diff`) accepts an event only if its `session_id` is a
  currently active invocation or one of the four most recently finished
  invocations **and** its `directory` is exactly equal (`==`) to that
  invocation's registered directory. Finished attribution retains only
  session-ID/directory metadata, never event payloads; fifth and later
  finishes evict the oldest retained attribution.
- `_is_directory_attributed()` (`file.edited`, which carries no
  session ID) accepts an event only if its `directory` is exactly
  equal to one of the currently active invocations' directories.
- Neither method performs prefix, glob, or path-containment matching
  of any kind. `test_file_edited_prefix_sibling_ignored` pins this
  directly: an invocation registered at `/repo` must not accept a
  `file.edited` event whose directory is `/repo-other`, even though
  `/repo` is a string prefix of `/repo-other`.
- A recognized session-bearing event whose session is not yet registered is
  retained in the bounded pending-event queue. Registration replays only
  entries whose session ID **and** directory exactly match the new invocation;
  a mismatched directory is rejected and retained only until ordinary bounded
  eviction. Once a session is registered, mismatched-directory events are
  dropped immediately. This closes the observer-to-Textual-message race without
  weakening exact attribution or allowing unbounded state.
- Unregistration removes an invocation from active tracking immediately, marks
  its displayed record `done`, and retains its exact session-ID/directory pair
  in a separate oldest-first four-entry map. Matching trailing session-bearing
  events can still update that displayed record, but status events cannot
  resurrect it from `done`; directory-only `file.edited` events still require
  an   an active invocation. An unknown session remains pending only for possible
  future registration. Eviction moves the finished session ID into a separate
  oldest-first four-entry tombstone queue, so its later events are dropped
  rather than entering the pending-registration queue and cannot be replayed
  into a reused registration. Registering that session ID explicitly clears
  its tombstone. Once both bounded windows have evicted a session ID it is
  indistinguishable from a genuinely unknown pre-registration session. This is
  a bounded attribution grace based on finish count, not a timer or an SSE
  drain barrier; see ADR 0031.
- Reconnect reconciliation is attempted only for the server's currently active,
  supervisor-owned `InvocationRef` values. Both status and message requests use
  each reference's exact directory, and the message request uses its exact
  session ID. Returned message records and parts must repeat that session ID
  before they become reducer events. Reconciliation remains ephemeral and never
  changes durable supervisor phase.

**Ownership** (see also ADR 0008, which this ADR is a companion to):
every reducer method calls `_assert_owner()` first and raises
`RuntimeError` if called from any thread other than the one captured
at construction (`RunScreen.__init__`, on the Textual event-loop
thread). Worker threads must post typed `Message`s and let the
event-loop-thread handler invoke the reducer; they must never call it
directly.

## Consequences

- These bounds and the exact-match rule are now a documented contract,
  not just inferred from reading `live.py`'s source: a future change
  that widens matching (e.g. "helpfully" supporting path prefixes) or
  removes a bound is now a decision that contradicts this ADR and
  should be escalated to the architect rather than made silently.
- The original bounds and filtering rules were already true when this ADR was
  accepted. Task 048 extends the same invariants to pending registration events,
  visible notices, and reconnect reconciliation. Task 049 adds the bounded
  recently-finished lifecycle from ADR 0031 without widening how events match.
  Backlog item 20 remains closed; backlog items 14 and 15 are resolved without
  weakening the attribution boundary or making SSE gate durable progress.
- The bounds are fixed constants, not configurable. If a future need
  arises for a longer-lived TUI session to retain more history (or a
  memory-constrained environment to retain less), that would be a new
  decision, not an extension of this one.
