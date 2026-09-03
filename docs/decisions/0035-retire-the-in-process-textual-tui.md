# Retire the in-process Textual TUI; `tui` becomes a no-op stub

## Status

Accepted

## Context

ADR 0008 established the Textual TUI and its in-process execution model; ADR
0021 elaborated that model in depth: the TUI drives `RunSession` directly on
a worker thread, owns a `LiveActivityReducer` that mutates in response to
live `GET /global/event` SSE traffic, and renders both durable `RunState`
and ephemeral live activity in separate panes. ADR 0019 documents the
reducer's bounds and event-filtering contract.

That model shipped and was exercised (`src/loop_supervisor/tui/`: `app.py`,
`live.py`, `messages.py`, `renderers.py`, roughly 1,983 lines, about 20% of
the source), but it never reached parity with the headless `run`/`resume`
path and accumulated a long tail of deferred gaps tracked in
`docs/plans/2026-08-22-post-lifecycle-fix-backlog.md` (see its "Deferred:
TUI work" note and items 16, 17, 18, 40, 42, 52): no `max_steps`/`--step`
equivalent, no `loop-supervisor.toml` `[provision]`/`[verify]` wiring, no
SIGTERM bridge, divergent permission-denial reporting, and a module layout
that never matched its own design plan. It was also the least-tested
segment of the codebase relative to its size.

The repository owner has decided to rebuild the TUI around a materially
different model: reading from the same on-disk run state the headless path
already produces and persists -- the per-phase history capture under
`runs/<run_id>/NNNN-<phase>.json` (ADR 0034) and the verification logs under
`verification/<run_id>/<commit>/` -- rather than sharing live, in-process
`RunSession`/`Supervisor` state via an event-driven reducer. This is a
disk-driven, likely poll-or-tail-based design, not an evolution of the
in-process one. Continuing to carry the old implementation, its tests, and
its backlog forward would not save meaningful work toward that design and
would keep dead weight (an unused `LiveActivityReducer`, screens, and
renderers) in the tree in the meantime.

Per this project's ADR convention (see `docs/decisions/README.md`), ADRs
are never rewritten; a later decision that reverses an earlier one records
that here, referencing the superseded ADRs by number, rather than editing
0008, 0019, or 0021.

## Decision

The entire `src/loop_supervisor/tui/` package (`__init__.py`, `app.py`,
`live.py`, `messages.py`, `renderers.py`) is deleted, along with its direct
test coverage (`tests/test_tui_app.py`, `tests/test_tui_renderers.py`,
`tests/test_live_reducer.py`). This supersedes the execution model
described in ADR 0008/0019/0021 for the codebase as it exists today; those
ADRs are left untouched as the historical record of that model.

`loop-supervisor tui` remains a recognized subcommand -- it is not removed
from the CLI -- but `cmd_tui` is now a no-op stub: it prints a notice that
the interactive TUI is being rebuilt and to use `run`/`resume` in the
meantime, and returns exit code `0`. The `tui` subparser is trimmed to
accept only `--project` (accepted and currently ignored); `--recover-stale-
lock` is removed, since the stub acquires no lock. Keeping the subcommand
name, rather than deleting it outright, gives the rebuilt TUI a stable
invocation to land on and avoids a confusing "unrecognized command" error
for anyone with `tui` in muscle memory during the interim.

`locking.py`'s `_VALID_OPERATIONS` keeps `"tui"` in its vocabulary, purely
for backward compatibility: a lock record left on disk by an older `tui`
process must still pass `_validate_lock_record` on read, or a stale lock
from a mixed-version environment would become unreadable
(`MalformedLockError`) instead of recoverable via `--recover-stale-lock`.
Nothing currently writes `"tui"` as an operation label; `RunSession`'s
generic `operation` override parameter (see `runtime.py`) is retained as-is
since it is harmless, general-purpose API surface that a future TUI can use
again.

The `textual`, `rich`, and `pytest-asyncio` dependencies are **kept** in
`pyproject.toml` even though nothing in the tree currently imports them,
because the planned rebuild is expected to need at least `textual` again
shortly; dropping and re-adding them would be pure churn.

User-facing documentation (`README.md`, `docs/INSTALLING.md`, the
`_skeleton/README.md.tmpl` shipped to bootstrapped projects, and the
`_skills/` reference docs) is updated to describe `tui` as a temporary
no-op stub rather than describing the removed feature set. The archived
`docs/plans/archive/2026-08-21-tui-vertical-slice.md` plan is deleted
outright, since it described the now-removed implementation's original
scope and has no further historical value once that implementation is
gone. `docs/plans/2026-08-22-post-lifecycle-fix-backlog.md` is not rewritten
-- consistent with its own "Corrections to prior commit messages" append-
only convention -- but gets a dated note appended to its "Deferred: TUI
work" section, plus a short "Moot" note appended to each of items 16, 17,
18, 40, 42, and 52, pointing back at this ADR.

## Consequences

- `loop-supervisor --help` still lists `tui`, but invoking it does nothing
  beyond printing a notice and exiting `0`. Any script or muscle-memory
  invocation of `loop-supervisor tui` continues to "succeed" without
  launching an interactive session; callers that depend on the old
  interactive behavior must switch to `run`/`resume`.
- `RunSession`'s multi-thread support (documented in `runtime.py`'s
  module docstring, motivated by the TUI's `advance()`-worker-plus-
  shutdown-worker pattern per ADR 0008) is currently exercised by no
  in-tree caller. It is left in place rather than simplified, since the
  planned disk-driven TUI may still need a background poller thread
  alongside the main thread, and removing then re-adding synchronization
  primitives would be premature.
- The two `tests/test_runtime.py` cases pinning the `operation=` override
  mechanism now use `"tui"` purely as a vocabulary probe (the only
  non-default value `_VALID_OPERATIONS` accepts), not as evidence that a
  TUI exists; their docstrings were updated to say so.
- `docs/decisions/0009`, `0011`, `0015`, `0016`, `0020`, `0022`, `0026`,
  `0031`, `0033`, and `0034` all contain historical references to the TUI
  (as a consumer, a comparison point, or a divergence to track). Per this
  project's ADR convention those are left byte-for-byte unchanged; they
  describe accurately what was true when written.
- The next TUI design is not specified here. This ADR only retires the old
  implementation and its execution model; the disk-driven replacement
  (what it polls or tails, how it presents `runs/<run_id>/NNNN-<phase>.json`
  history and verification logs, whether it still uses Textual) is future
  work and will need its own decision(s) once designed.
