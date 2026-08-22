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

## Consequences

- Supervisor core remains testable without any Textual dependency.
- Worker threads do not share state with the event loop; all UI mutation is
  serialized through the Textual message queue.
- SSE failure leaves the durable UI fully usable; no run is failed due to
  an SSE transport error.
- `loop-supervisor tui --project PATH` is the new entry point for the UI.
- Textual and Rich are added as runtime dependencies; `pytest-asyncio` is
  added as a dev dependency for Textual pilot tests.
