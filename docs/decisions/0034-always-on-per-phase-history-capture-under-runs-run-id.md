# Always-on per-phase history capture under `runs/<run_id>/`

## Status

Accepted

## Context

`RunState` keeps exactly one slot per role result (`planner_result`,
`architect_result`, `builder_result`, `verification_result`,
`auditor_result`), overwritten every time that phase re-runs. A task
revised three times (`REVISE`) leaves only the third `builder_result`
on disk; the second-to-last auditor verdict that caused the second
revision is gone entirely. The only surviving evidence a phase ran more
than once is a bare counter (`revision_count`, `replan_count`,
`architect_retry_count`, `builder_guidance_count`) -- present, but not
informative about *what happened* on each pass.

This was noticed while inspecting a live run in `pub-analysis` from
outside the supervisor process: the persisted `<run_id>.json` snapshot
could only describe the run's *current* state, not its history, even
though the run had visibly gone through multiple builder/auditor
cycles. Reconstructing what actually happened on each pass required
either the OpenCode session database (full transcripts, but keyed by
session, not by supervisor phase, and not scoped to this project's own
on-disk conventions) or nothing at all.

Two things were deliberately ruled out while designing this:

- Duplicating verification command output. `verifying`'s full
  stdout/stderr already lives, permanently, under
  `verification/<run_id>/<commit>/NN.log` (ADR 0027, ADR 0028), reached
  from `state.verification_result`'s `output_path` field. A second copy
  under history would be pure waste with no new information.
- Capturing the literal agent prompt. The richest input a phase saw is
  the prompt string built by `_build_planner_prompt` /
  `_build_builder_prompt` / etc., but those are local to each `_do_*`
  handler, not present on `AdvanceOutcome`. The full prompt and
  response for every invocation already exists, per invocation, in the
  OpenCode session database (the same source ADR 0033's `-v`/`-vv`
  diagnostics point an operator at for finer detail than a summary
  line can give). Reaching into `_do_*` to also capture it here would
  add real complexity for content already available elsewhere.

## Decision

`history.py` adds an always-on `PhaseHistoryRecorder`, installed as a
second `on_advance` callback beneath `-v`/`-vv`'s existing one
(`cli._build_on_advance`, replacing `_build_verbosity_hooks` at the two
`cmd_run`/`cmd_resume` call sites): the recorder runs at every
verbosity level, including the default (0), independent of whether
`-v` is passed. It writes one small JSON record per `advance()` call
to:

```
<git-common-dir>/loop-supervisor/runs/<run_id>/NNNN-<phase>.json
```

`NNNN` is a zero-padded, monotonically increasing sequence number
(`0001`, `0002`, ...) giving total order across the whole run; `<phase>`
is `phase_before` (the phase that ran). The sequence continues across
resume: a fresh `PhaseHistoryRecorder` instance (no in-memory state
from a prior process) computes its next number by scanning the
existing directory for the highest `NNNN-*.json` prefix, so resuming a
paused run never restarts numbering at 1 and overwrites earlier
records.

Each record is deliberately cheap ("outcome-only"): everything in it
is already available on the `AdvanceOutcome` and `RunState` the
callback receives, never anything an in-`_do_*` handler would need to
be re-wired to expose. It contains `seq`, `run_id`, `phase`,
`phase_after`, `status`, `recorded_at`, `original_task_id`, the five
loop counters (`accepted_task_count`, `revision_count`,
`replan_count`, `architect_retry_count`, `builder_guidance_count`),
`result` (the role-result field keyed by `phase`, i.e. exactly
`state.planner_result` / `state.builder_result` / etc. -- for
`verifying`, `state.verification_result` verbatim, which is already
the compact per-command summary and never the full log body), and
`error` (the `OperationalErrorRecord`, when `outcome.error` is set).
Every free-text string reachable from the record is passed through the
same `_redact_secrets` best-effort scrubbing `OperationalErrorRecord`
already uses, applied broadly (recursively over every string in the
record) since a captured role result may carry an agent's free-text
response.

This is genuinely log output, not something the supervisor reads back
and interprets the way `state.py` reads back `RunState`, so the writer
deliberately skips `save_state`'s `O_NOFOLLOW`/`dir_fd`/atomic-replace
hardening: a plain `mkdir(mode=0o700)` + `write_text` + `chmod(0o600)`
is proportionate. `PhaseHistoryRecorder.on_advance` never raises on its
own -- a write failure prints a stderr notice and drops that one
record -- matching the "an observability hook must not be able to fail
the run" contract `VerboseReporter.on_advance` already follows (ADR
0033); `Supervisor.run()`'s own `try`/`except` around every installed
`on_advance` callback is a second, independent backstop.

A new `loop-supervisor runs prune` command (`history.py`'s
`select_prune_candidates`/`prune_runs`, wired as `cmd_runs_prune`)
deletes old runs' state files, history directories, and (opt-in via
`--include-verification`) verification logs. Selection is either
`--run <id>...` (exact IDs) or `--keep-last N`/`--older-than DAYS`
(either or both, applied together, over every saved run sorted
newest-first by `updated_at`). The command always dry-runs (prints what
it would remove) unless `--yes` is passed. Deletion refuses outright
whenever any supervisor lock is present anywhere in the repository
(`locking.lock_is_present`), deliberately coarser than trying to
classify a specific lock as live/stale/remote/matching this run: a
fresh `run` never records a `run_id` in its lock (`locking.py`), so a
per-run match is not reliably available, and a wrong permissive guess
here would delete a live run's own history out from under it.

`--keep-last`/`--older-than` rank runs by `updated_at`, so they only
ever consider runs `load_state` can actually parse. `--run <id>...` is
different: an explicitly-named run is selected even when its state
file exists but fails to load (corrupted, or -- per ADR 0024's
no-migration policy -- written by an older `STATE_SCHEMA_VERSION`),
via `_unloadable_candidate_if_present`, as long as *something* for it
is still on disk (its state file, its history directory, or its
verification directory). Such a candidate is displayed with
`phase=?`/`updated_at=?` and an explicit `[unloadable]` tag rather than
fabricated values, and deletion proceeds by validated run ID alone
(`prune_runs` never needed to read the state it removes). A `--run
<id>` naming nothing on disk at all still selects nothing, silently
(`no runs selected for pruning`) -- that case is indistinguishable from
a typo and is not escalated to an error.

## Consequences

- `runs/<run_id>.json` (the existing latest-state snapshot) and
  `runs/<run_id>/` (this module's per-execution history) are siblings.
  `state.list_runs()` globs `*.json` files only, so the bare history
  directory is invisible to it and can never register as a phantom
  run; no change to `state.py` was needed.
- At verbosity 0 (the default), `on_advance` passed to
  `run_new`/`run_resume` is no longer `None` -- it is always the
  history recorder, possibly chained with the `-v`/`-vv` reporter on
  top. `server_observer`/`session_event_consumers` are unaffected and
  remain `None`/`[]` at verbosity 0.
- The TUI is out of scope for this pass: it drives `RunSession`
  directly via `new_run_session`/`resume_run_session` /
  `run_to_completion`, none of which this change touches, so TUI-driven
  runs silently get no phase history capture, with no in-app signal
  that CLI-driven and TUI-driven runs now diverge in this respect.
  Extending it there, if wanted, is a separate follow-up.
- `runs prune` never deletes anything while any lock file exists in the
  repository, even one unrelated to the run(s) being pruned. This is
  strictly safer than it is precise; an operator who wants to prune
  while a run is genuinely active elsewhere in the same repository must
  wait for that run to finish (or fail) and release its lock first.
- `runs prune`'s lock check (`lock_is_present`) happens once, before
  the delete loop, not held for its duration: a `run`/`resume`/`tui`
  invocation could acquire the lock in the window between that check
  and a given candidate's `shutil.rmtree`, and prune would then delete
  a now-live run's state or history. This is an accepted, narrow
  (single-process, millisecond-scale) race for an explicit, operator-
  initiated command, not a design goal; holding the lock for the whole
  prune was not done since prune's own writes never need `advance()`'s
  guarantees, only its absence of concurrent mutation.
- After a `STATE_SCHEMA_VERSION` bump, every run written under the
  prior schema becomes unloadable (ADR 0024: no migration path) and is
  therefore invisible to `--keep-last`/`--older-than`, which cannot
  rank what they cannot parse. Such runs remain reachable and prunable
  one at a time via explicit `--run <id>` (see Decision); there is no
  bulk "prune everything unloadable" flag, since enumerating unloadable
  runs to select by default risks silently sweeping up something an
  operator did not name.
- Retention is unbounded by default (append-only forever, per project
  preference); disk usage from history accumulates until an operator
  runs `runs prune` explicitly. No automatic pruning was added.
</content>
