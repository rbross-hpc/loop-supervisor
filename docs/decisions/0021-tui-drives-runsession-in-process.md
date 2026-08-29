# TUI drives RunSession in-process; subprocess/observer rejected

## Status

Accepted

## Context

The TUI (`loop-supervisor tui`) and the headless CLI (`loop-supervisor
run`/`resume`) both drive a `RunSession` to completion. A natural
question, revisited periodically, is whether the TUI should instead
spawn the headless CLI as a subprocess and act as a pure observer of
its progress — the way, for example, a log-tailing dashboard watches a
worker process it does not itself drive. This ADR records that this
was considered and rejected, and documents the actual sharing/
duplication boundary between the two front-ends so the question does
not need to be re-litigated from scratch each time it comes up.

`cmd_tui` (`cli.py:352-368`) imports `LoopSupervisorApp` in-process and
calls Textual's own `app.run()` — the TUI runs in the same Python
process as the CLI invocation that launched it, and today spawns
nothing itself except the OpenCode server, via the same shared
`RunSession.start_server()` the headless CLI uses.

## Decision

**Keep the TUI in-process.** It calls `new_run_session()`/
`resume_run_session()` directly (`tui/app.py:131, 482, 490`) rather
than the headless-only `run_new`/`run_resume` wrappers (`runtime.py:
1473, 1508`), because acquisition and release happen on two different
background threads (init worker vs. shutdown worker — see ADR 0008),
so the session cannot be used as a `with` block the way the headless
CLI uses it.

**What is genuinely shared (single implementation):** everything owned
by `RunSession` — lock acquisition/release policy and `_LockLease`,
OpenCode server lifecycle (see ADR 0020), state create/load/resume-
validation, and confirmed-stop cleanup ordering (`runtime.py:528-
1369`) — plus all of `Supervisor.advance()` and every `_do_*` phase
handler (`supervisor.py:462-1350`). Neither front-end reimplements any
phase logic.

**The one real duplication is the step loop**, and only the step loop:

- Headless: `Supervisor.run()`, a synchronous `while` loop
  (`supervisor.py:718-735`) reached via `RunSession.run_to_completion()`
  (`runtime.py:1122`).
- TUI: `RunScreen._start_advance()` → `_advance_worker()` →
  `on_advance_completed()` (`tui/app.py:689-750`), a Textual message
  cycle that re-arms itself on each non-terminal outcome.

These two loops currently diverge in ways worth naming explicitly,
since they were found while writing this ADR and are not yet fixed:
`max_steps` has no TUI equivalent at all; `_report_denied_permissions`
(`runtime.py:1449`) is only ever called from `run_new`/`run_resume`,
so a permission denied under the TUI is silently unreported even
though the denier itself does run there (see the correction to
backlog item 31, below); and the TUI hardcodes `_DEFAULT_OPTIONS`
(`tui/app.py:159-170`) instead of accepting the same nine
run-behavior flags `run` does. These are tracked as backlog item 17
(options parity) and addressed partially by a following change (denial
visibility, `INPUT_UNAVAILABLE` handling).

**Why a subprocess/observer architecture was rejected:** the IPC
surface it would require does not exist today, and building it is a
larger and riskier undertaking than it first appears.

- The OpenCode server's port is ephemeral (`_free_port()` binds
  `127.0.0.1:0`, `opencode.py:138-141`) and is recovered by
  regex-scraping the child's stdout (`_READY_RE`, `opencode.py:28`,
  matched at `opencode.py:847-853`). It exists only as an in-memory
  `base_url` attribute (`opencode.py:518`) — it is in neither the
  lock record (`_LOCK_RECORD_FIELDS`, `locking.py:58-69`) nor
  `RunState` (`state.py:326-366`). An out-of-process observer has no
  way to find the SSE endpoint at all without new plumbing (a port
  file, a lock-record schema bump, or the child re-emitting it).
- `pending_question` *is* already persisted (`state.py:353`) and
  readable lock-free via `load_run()` (`runtime.py:1553-1562`), so an
  observer could see a pending question. But there is no existing
  file-based *answer* channel — both shipped `InputProvider`
  implementations are in-process (`StdinInputProvider`,
  `input_providers.py:14`; `_QueueInputProvider`, `tui/app.py:193`) —
  and `_try_resolve_pending_input` (`supervisor.py:1307-1349`) polls
  the provider exactly once per `advance()` call rather than blocking,
  so a file-based provider would need either a retry loop the driver
  doesn't have today, or the observer would need to write the answer
  file strictly before the driver's next `advance()`.
- `AdvanceOutcome.status` (the `ADVANCED`/`INPUT_REQUIRED`/
  `INPUT_UNAVAILABLE`/`OPERATIONAL_FAILURE`/`TERMINAL` discriminator,
  `supervisor.py:123-138`) is in-memory only. It is largely derivable
  from persisted `phase` and `last_error`, except that
  `INPUT_UNAVAILABLE` and `INPUT_REQUIRED` both leave
  `phase == awaiting_input` and are not otherwise distinguishable from
  outside the process.
- There is no change-notification mechanism in the codebase today (no
  file-watch, no inotify, no polling loop) that an observer could use
  to learn a state file changed; one would need to be built.
- In short: this would mean deleting roughly 788 lines of tested
  in-process lifecycle machinery (`tui/app.py`'s init/advance/shutdown
  worker coordination, ownership registry, cleanup coordinator, and
  exit drain — see ADR 0008) in exchange for writing a comparable or
  larger amount of new, untested IPC protocol, for a benefit that is
  real but narrow (see below).

**What it would have bought, stated honestly:** backlog item 22b — a
real `SIGTERM` against a running `loop-supervisor tui` process today
still terminates at default disposition with the same orphan/
stale-lock exposure ADR 0015 fixed for the headless path, because
ADR 0015 explicitly excluded the TUI (Textual's Linux driver strips
`ISIG` in raw mode, and injecting an externally-raised
`KeyboardInterrupt` into Textual's running asyncio loop was judged an
untested code path that could leave the terminal stuck in raw mode).
If the TUI were a thin observer of a headless-CLI child process, that
child already carries ADR 0015's tested `SIGTERM` bridge, and 22b
would be resolved as a side effect rather than as its own fix. This
benefit was judged real but not sufficient on its own to justify the
re-architecture; 22b should instead be fixed directly, on its own
terms, within the in-process model.

## Consequences

- The sharing/duplication boundary above is now documented rather than
  something that has to be re-derived from reading `tui/app.py` and
  `runtime.py` side by side. A future change that grows the step-loop
  duplication (e.g. adding a new `AdvanceStatus` case) should keep
  both loops' handling of it in sync, or explicitly note in its commit
  message why they diverge.
- Backlog item 31's premise needs correction: it describes the
  headless permission denier as "deliberately scoped to the headless
  path only (`RunSession.start_server()`/`close()`)". `start_server()`
  is exactly what the TUI calls (`tui/app.py:597`), and the denier is
  constructed inside it (`runtime.py:983`) — so the denier already
  runs under the TUI today. Only the *reporting* of what it denied is
  headless-only (`_report_denied_permissions`, called solely from
  `run_new`/`run_resume`). Item 31's remaining question (should the
  TUI itself, as opposed to reusing this denier, decide whether to
  auto-deny on the operator's behalf) is still open, but its premise
  should no longer say the denier itself doesn't run there.
- Backlog item 22b remains open and is not resolved by this ADR; this
  ADR only records why the subprocess architecture that would have
  resolved it as a side effect was not chosen.
- If OpenCode's port, the answer channel, and a change-notification
  mechanism are ever built for some other reason (e.g. multiple
  independent observers of one run), this decision should be
  revisited rather than assumed to still hold — the primary cost
  identified here is that this plumbing does not exist yet, not that
  it is undesirable in principle.
