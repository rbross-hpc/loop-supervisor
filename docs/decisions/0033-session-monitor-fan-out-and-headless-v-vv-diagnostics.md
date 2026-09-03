# SessionMonitor fans out `/global/event`; headless `-v`/`-vv` add diagnostics only

## Status

Accepted

## Context

`PermissionDenier` was the headless path's sole subscriber to OpenCode's
`GET /global/event` SSE stream, and it was single-purpose: its `_on_event`
normalized every incoming event only to immediately discard everything except
`permission.asked`. Every other event type the stream carries --
`message.part.updated` deltas, `session.idle`/`session.error`, tool-state
transitions -- reached that same callback and was thrown away.

Operating a real run against `puba-project` surfaced a concrete gap this
caused: a headless run prints exactly one line for the whole invocation
("permission denier watching ...") and then nothing until the phase either
finishes or exhausts `role_timeout` (default 1800s). There is no way to tell
a slow-but-working session apart from one that has genuinely stopped
producing output -- the TUI's live activity indicator can go dark while the
same information, over the same stream, is available to the headless path
and simply unused. A prior investigation (see the session notes on Sonnet 5
occasionally stopping mid-invocation) confirmed the data needed to observe
this -- `message.part.updated` deltas, `session.idle`/`session.error`,
tool-state -- already flows through `PermissionDenier`'s own callback; only
the behavior to act on it was missing.

Two options were considered for surfacing this: (1) OpenCode's own
`provider.<name>.options.chunkTimeout`, which aborts a request after a
configurable gap between streamed chunks -- a transport-level, config-only
lever with no supervisor code involved; and (2) supervisor-side
observability. This decision covers only (2). It deliberately does **not**
add any actuation: no session is aborted, retried, or otherwise bounded by
anything introduced here. `role_timeout` remains the only thing that ever
stops a phase. The purpose of this change is to let an operator *see* what
is happening during a run, not to make the supervisor act differently based
on what it sees -- that is a materially larger, separately-considered change
(a stall watchdog), left for a future decision if the diagnostics here show
it is warranted.

## Decision

`permissions.PermissionDenier` is split into two collaborators:

- `SessionMonitor` owns the single `GET /global/event` SSE subscription (as
  `PermissionDenier` did) and fans every normalized event out to a list of
  attached consumers, each implementing `SessionEventConsumer.on_event(raw,
  event)`. A consumer that raises is caught and reported as a notice; it
  never breaks another consumer's turn and never fails the run, mirroring
  `sse.py`'s own "SSE failure is strictly non-fatal" contract.
- `PermissionPolicy` is the first consumer, carrying the auto-reject
  behavior over unchanged: same reply contract, same directory-scoping
  (ADR 0016), same `denied_count`/`denied_summary` diagnostic surface.

`RunSession.start_server()` constructs one `SessionMonitor` per run and
attaches `PermissionPolicy` to it always, plus any additional consumers the
caller supplied (`session_event_consumers`). `RunSession.close()` tears the
monitor down the same way it tore the denier down (before the server
itself, snapshotting the policy's counters first).

A new headless-only diagnostics module, `verbosity.py`, adds cumulative,
ssh-style `-v`/`-vv` flags to `run`/`resume` (counted, per-invocation like
`--step`/`--max-steps` -- not persisted into `RunOptions`, safe on
`resume`):

- `-v`: a `VerboseReporter` prints one timestamped (`[HH:MM:SS]`) stderr
  line per agent invocation start/finish (with the task id/objective,
  truncated to 80 chars, via `_task_label`) and one per phase transition
  (`phase_before -> phase_after`, skipped when unchanged). Invocation lines
  attach via the existing `InvocationObserver` hook
  (`OpenCodeServer.add_observer`, now threaded into `run_new`/`run_resume`
  as `server_observer`, which those two functions did not previously
  expose); phase-transition lines attach via a new `on_advance` callback
  parameter on `Supervisor.run()`, invoked with each `AdvanceOutcome`
  immediately after every `advance()` call, purely as an observation hook
  with no influence on `run()`'s own control flow.
- `-vv` (implies `-v`): additionally attaches a `StatsConsumer` to the
  `SessionMonitor` (via the new `session_event_consumers` parameter on
  `RunSession`/`new_run_session`/`resume_run_session`/`run_new`/
  `run_resume`), which tracks two streaming (O(1), no retained history)
  gap statistics per active session: the gap between *any* two consecutive
  events ("all-event gap", overall liveness) and the gap between
  consecutive `message.part.updated` events carrying `delta` text ("delta
  gap", token-level output). Divergence between the two is the diagnostic
  signal: a large delta gap with a small all-event gap usually means a
  tool call is running (tool-state events keep arriving; no new tokens
  do); a large gap in both is the closer approximation of a genuine stall.
  `StatsReportingObserver` bridges the consumer's per-session accumulation
  to the `InvocationObserver` finish callback so exactly one summary line
  prints per invocation, interleaved with `VerboseReporter`'s own lines via
  `CompositeInvocationObserver` (which `OpenCodeServer.add_observer` takes
  only one observer, so `-vv` needs to fan its own two observers into one).

All `-v`/`-vv` output goes to stderr; `run`/`resume`'s existing stdout
lines (`run_id: ...`, `final phase: ...`) are unchanged, keeping stdout
machine-parseable regardless of verbosity level.

## Consequences

- Renaming `PermissionDenier` touched every reference across `runtime.py`
  (field names, the two `denied_permission_*` properties, `start_server`/
  `close`, `_report_denied_permissions`'s docstring), `tui/renderers.py`'s
  comment, and `cli.py`'s SIGTERM-bridge docstring. `denied_permission_count`
  and `denied_permission_summary`'s public names, and the CLI-facing
  "denied N permission request(s) (...)" diagnostic wording, are unchanged
  -- this is an internal rename, not a behavior or output-format change for
  anything that already existed.
- Historical ADRs (0015, 0016, 0021, 0024) and the dated backlog/plan
  document under `docs/plans/` still say "denier"; per this project's ADR
  convention, past decisions are not rewritten, so those references stand
  as the historical record of what the component was called when those
  decisions were made.
- `Supervisor.run()`'s new `on_advance` parameter is optional and additive;
  every existing caller (and every test's `FakeSupervisor`/`_boom_run`/
  `_fake_run`/`_ok_run` stand-in for it) needed `on_advance=None` added to
  its signature purely to remain call-compatible with
  `RunSession.run_to_completion()`, which now always passes it as a
  keyword. No caller's behavior changes when `on_advance` is omitted.
- `run_new`/`run_resume` gain three new optional keyword parameters
  (`server_observer`, `session_event_consumers`, `on_advance`); the TUI
  does not use these two functions (it drives `RunSession` directly via
  `new_run_session`/`resume_run_session`, which already had
  `server_observer`) and is unaffected.
- `-vv`'s statistics are diagnostic-only. Nothing in this change reads
  them to make a decision, set a timeout, or abort a session -- a future
  stall-detection mechanism (e.g. acting on a large all-event gap) is
  explicitly out of scope here and would need its own decision, given the
  materially different risk profile of an observer that can affect a
  session versus one that only reports on it.
- `chunkTimeout` (the complementary OpenCode-side, config-only lever for
  aborting a genuinely silent stream) is unaffected by and independent of
  this change; adopting it, if desired, requires no supervisor code and is
  not covered by this ADR.
