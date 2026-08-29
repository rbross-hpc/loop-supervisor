# 0008 — Textual TUI and execution model

## Status

Accepted

## Context

The headless supervisor needed a responsive UI that could display durable
supervisor state alongside ephemeral live OpenCode activity, collect pending
operator inputs, and survive transient SSE disconnects without losing the
authoritative run record.

## Decision

Use **Textual 6.x** and **Rich** for the terminal UI, following established
patterns from the Falda analysis UI.

The `Supervisor` class remains synchronous. Each `advance()` call runs in a
Textual **thread worker** (`run_worker(thread=True)`). Worker threads
communicate back to the Textual event thread exclusively via
`app.call_from_thread` and typed `Message` subclasses; widgets are never
mutated from a background thread directly.

The persisted `RunState` is the authoritative source for durable supervisor
phase, task identity, counters, and the last error. OpenCode SSE telemetry
(`GET /global/event`) is treated as ephemeral live telemetry only. The
blocking final prompt response in `opencode.py` remains authoritative for
role completion and structured output.

SSE subscribes to `/global/event` (not per-directory `/event`) because
planner, architect, builder, and auditor sessions may run in different
worktree directories and the global stream captures all of them.

The UI clearly separates **Durable supervisor state** from
**Live OpenCode activity — ephemeral** so operators are never confused about
which source is authoritative.

Resource lifecycle (lock, OpenCode server, `Supervisor`) is owned by a
single `runtime.RunSession` per `RunScreen`, not by ad hoc attributes on
the screen — see ADR 0009 for `RunSession` itself. `RunScreen` constructs
and enters the session on its own initialization worker thread and
closes it on its own shutdown worker thread; these are two different
background threads over the lifetime of one screen, so the session is
never used as a `with` block the way the headless CLI uses it. Between
those two calls, the session's `advance()` is driven from a third,
per-transition worker thread spawned on demand, matching the existing
one-`advance()`-per-thread-worker model above. `RunSession`'s own
concurrency contract (ADR 0009) — a re-entrant state lock serializing
`close()` against `stop_server()`, and a quiescence barrier ensuring the
lock is never released mid-transition — is what makes this safe across
three threads that are never coordinated by Textual's own message queue.

`LiveActivityReducer` (`tui/live.py`) — the mutable accumulator behind
the ephemeral live-activity snapshot — is owned exclusively by the
Textual event-loop thread, not by any worker thread. It is constructed
with `owner_thread=threading.current_thread()` at `RunScreen.__init__`
(captured on the event-loop thread, before any worker exists), and
every one of its methods calls `_assert_owner()` first, raising
`RuntimeError` if invoked from any thread other than that one. This is
consistent with, and not an exception to, the UI/widget-state rule
above: worker callbacks (SSE reception, `advance()` completion) must
post typed `Message`s and let the event-loop-thread message handler
invoke the reducer, exactly as they must for any other widget
mutation. See `docs/decisions/0019-live-activity-reducer-bounds-and-
event-filtering.md` for the reducer's own bounds and event-attribution
invariants.

## Consequences

- Supervisor core remains testable without any Textual dependency.
- UI/widget state is not shared with worker threads: no widget is ever
  mutated from a background thread, and all UI mutation is serialized
  through the Textual message queue. Lifecycle state is a deliberate
  exception to this — the init, advance, and shutdown worker threads
  share `RunScreen._session` plus a handful of `threading.Event`/`bool`
  fields (`_shutdown_requested`, `_shutdown_clean`, `_init_done_event`,
  `_advance_done_event`) directly, guarded by `RunSession`'s own
  concurrency primitives (ADR 0009) rather than by the message queue.
  `LiveActivityReducer` is not part of this exception: it is
  event-loop-thread-only, enforced by its own runtime assertion
  (`test_owner_thread_assertion` in `tests/test_live_reducer.py`).
- SSE failure leaves the durable UI fully usable; no run is failed due to
  an SSE transport error.
- `loop-supervisor tui --project PATH` is the new entry point for the UI.
- Textual and Rich are added as runtime dependencies; `pytest-asyncio` is
  added as a dev dependency for Textual pilot tests.
- `RunScreen` shares its entire resource-lifecycle implementation with the
  headless CLI via `RunSession` (ADR 0009) rather than re-implementing
  lock/server/supervisor ownership; the TUI-specific code left on
  `RunScreen` is limited to SSE, Textual messaging, and translating
  `RunSession`'s raising `close()` into the non-raising `shutdown_clean`
  signal the app's exit-retry loop polls.
