# `-v`/`-vv`: the live, push-based view

`-v`/`--verbose` (repeatable, `-vv`) is the cheapest way to watch a run
while it's happening, and it costs nothing to add: it is per-invocation
only, never persisted into the run's saved `options`, so it is safe on
`resume` even if the original `run` didn't use it. Add it to the detach
command in `SKILL.md`:

```bash
nohup loop-supervisor run --project . --max-tasks 1 -v > /tmp/run.log 2>&1 &
```

Everything `-v`/`-vv` prints goes to **stderr**. `run`/`resume`'s two
stdout lines (`run_id: ...`, `final phase: ...`) are unchanged and stay
machine-parseable regardless of verbosity — redirect them separately if
you want one without the other.

## `-v`: four line kinds

Every line is timestamped `[HH:MM:SS]` (local wall clock, no date, no
timezone):

```
[14:22:05] planning -> creating_worktree  task-001: Add the frobnicator
[14:22:07] loop-builder started
[14:24:19] loop-builder finished (132.4s)
[14:31:02] automatic operational retry 2/3  task-001: Add the frobnicator
```

- **Phase transition** — `<phase_before> -> <phase_after>`, with the
  active task's id/objective appended after two spaces when one is
  available (truncated to 80 characters). Printed once per `advance()`
  call; skipped entirely when the phase didn't change (the
  `awaiting_input` re-loop case).
- **Invocation started** — `<agent> started`, where `<agent>` is the
  OpenCode agent id (`loop-planner`, `loop-architect`, `loop-builder`,
  `loop-auditor`).
- **Invocation finished** — `<agent> finished (<N>.<N>s)` on success, or
  `<agent> failed (<N>.<N>s): <ExceptionType>: <message>` on error (a
  timeout, a malformed-output retry exhaustion, etc.). Elapsed time is
  wall-clock for that one invocation, not the whole phase or run.
- **Automatic operational retry** — `automatic operational retry
  <n>/<max>`, printed separately from phase-transition lines when the
  supervisor retries a retryable operational failure on its own (see
  `--max-operational-retries`). Distinguishing this from a phase
  transition matters: it means the *same* phase is being reattempted,
  not that one just completed.

There are **no token counts, cost, or model name** anywhere in `-v`
output — "verbose" here means invocation/phase timing, not usage
accounting.

## `-vv`: adds one stats line per invocation

`-vv` implies `-v` and additionally prints one summary line when each
invocation finishes, built from the same SSE stream `PermissionDenier`
already consumes:

```
[14:24:19] loop-builder events=842 parts=810 tool_states=31 all-gap[n=841 min/mean/max=0.0/0.2/12.7s] delta-gap[n=771 min/mean/max=0.0/0.1/9.4s] silence_at_end=0.3s
```

- `events`/`parts`/`tool_states` — raw counts for that invocation's
  session: total normalized events, events carrying a message part, and
  of those, how many carried tool-call state.
- `all-gap[...]` — min/mean/max seconds between *any* two consecutive
  events for that session (overall liveness).
- `delta-gap[...]` — the same, but only between consecutive
  `message.part.updated` events that carried streamed text (token-level
  output progress).
- `silence_at_end` — seconds since the last event when the invocation
  finished (or when the summary was requested for a session with no
  matching finish yet).

**The diagnostic signal is the divergence between the two gaps, not
either one alone:** a large delta-gap with a small all-gap usually means
a tool call is running (tool-state events keep arriving even though no
new tokens do) — normal and not stuck. A large gap in *both* is the
closer approximation of a genuine stall. This heuristic is the main
reason to reach for `-vv` over `-v`.

If a session produced zero events, no stats line is printed for it at
all — this is normal for very short invocations, not a sign of failure.

`-vv`'s statistics are purely diagnostic. Nothing here changes when a
phase is aborted or retried; `--role-timeout` remains the only thing
that ever stops a phase, and `--max-operational-retries` remains the
only thing that governs automatic retry. Watching a large `all-gap`
grow is a cue to keep watching (or eventually intervene manually via the
process/lock checks in `observing-a-run.md`), not a signal the
supervisor itself acts on.

## When `-v`/`-vv` isn't enough

`-v`/`-vv` is only useful while attached to the log you redirected it
into. It tells you nothing about a run you didn't start with it, a run
someone else started, or what happened before you started watching. For
that, see `on-disk-layout.md` — the persisted run state and phase
history are the same information restated as durable, resumable files
rather than a live stream.
