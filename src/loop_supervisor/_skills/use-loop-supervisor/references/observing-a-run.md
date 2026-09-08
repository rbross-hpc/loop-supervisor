# Observing a detached run

Once a run is started detached (see `SKILL.md`), there are two
complementary ways to watch it — pick based on when you're looking:

- **`-v`/`-vv`** (see `verbose-output.md`) — a live, push-based stream
  of timestamped lines to stderr, chosen at launch. Cheapest option and
  usually sufficient; add it to the detach command up front.
- **The persisted files** (see `on-disk-layout.md`) — a pull-based,
  durable view: the current run-state JSON and the append-only phase
  history. Necessary when you didn't start the run with `-v`, when it's
  someone else's run, or when you need history predating whenever you
  started watching.

Whichever you used, check `/tmp/run.log` (or wherever you redirected
`> ... 2>&1`) first — it's the cheapest signal and, with `-v`, is where
the live timeline actually is.

## The run-state file, quickly

For a one-off check without reading the full schema in
`on-disk-layout.md`:

```bash
python3 -c "
import json
d = json.load(open('<git-common-dir>/loop-supervisor/runs/<run_id>.json'))
print('phase:', d['phase'])
print('task:', d['original_task_id'])
"
```

`phase` being `awaiting_input` or `operational_failure` means stop
polling and go read `pending_question` or `last_error` respectively
(field details in `on-disk-layout.md`) — both usually need a human
decision, not more waiting.

## Telling slow-but-healthy apart from stuck

- Long gaps with no new `-v` lines (or no new lines in `/tmp/run.log`
  at all without `-v`) are normal — an agent call can legitimately take
  several minutes, and there is no requirement that one produce
  intermediate output. `-vv`'s per-invocation stats line, once it
  prints, is the more precise version of this signal (see
  `verbose-output.md`'s note on divergence between `all-gap` and
  `delta-gap`).
- The task worktree's file mtimes advancing (`find <task-worktree>
  -newer <some-reference> -not -path '*/.git/*'`) is a reasonable
  signal that a builder invocation is actively writing files, not
  hung.
- The supervisor process itself still existing (`ps aux | grep
  loop-supervisor`) is the strongest local signal: if it's gone and the
  run state's `phase` is not `done`/`failed`, the process died
  mid-phase — see `recovering-an-interrupted-run.md`. Bare PID existence
  is not proof by itself, though: after a crash and reboot (or simply
  enough process churn), the recorded PID number can be reused by an
  unrelated process. `<git-common-dir>/loop-supervisor/supervisor.lock`
  additionally records `owner_boot_id` and `owner_process_start`
  (schema version 2) precisely so that case can be told apart from a
  still-live supervisor — see ADR 0037 for the exact comparison.
- Do not `git status`/edit inside the task worktree while a run is
  actively using it "just to look" — if the builder is mid-edit, you
  will observe (and could misinterpret) genuinely transient
  intermediate state.

## If you're a human instead of an agent

`loop-supervisor tui --project .` is a read-only, interactive browser
over the same persisted files described in `on-disk-layout.md` — the
better choice at a terminal, since it navigates and renders the run
state and phase history for you. It's not scriptable and can't be
driven by an agent in a tool-calling harness, which is why this skill's
guidance above is file- and flag-based instead.
