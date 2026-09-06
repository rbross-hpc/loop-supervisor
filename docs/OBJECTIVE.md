# Objective

This is the `loop-supervisor` project itself: a headless supervisor that
drives an OpenCode planner/architect/builder/auditor loop over Git worktrees.

The read-only `loop-supervisor tui` run browser has shipped, including
PID-reuse-resistant active-run identity (ADR 0037) on both the writer and
reader sides. A post-delivery audit of that work found remaining defects
and one unimplemented requirement; this document records the delivered
product contract and the remaining post-audit remediation work still in
progress (see "Ordered priorities").

## Product model

The TUI is a disk-backed run-data explorer. It must read supervisor-owned
artifacts beneath the selected project's Git common directory:

- current snapshots at `loop-supervisor/runs/<run_id>.json`;
- ordered phase history at
  `loop-supervisor/runs/<run_id>/NNNN-<phase>.json`;
- verification logs under
  `loop-supervisor/verification/<run_id>/<commit>/`.

Disk is the source of truth. The TUI must not restore the retired
architecture in which Textual drove `RunSession` directly or consumed
in-process OpenCode events. It must not read OpenCode's private database or
depend on undocumented OpenCode storage.

`loop-supervisor tui --project PATH` opens the selected project. When
`--project` is omitted, the current directory is used. The path must resolve
to a supported Git repository and its Git common directory; invalid input
must produce a clear error without entering the TUI.

The interface is strictly read-only. It must not start or resume runs, answer
pending questions, stop processes, edit state files, prune runs, recover
locks, or otherwise mutate supervisor or repository state.

Data is refreshed only when the user explicitly requests it. There is no
automatic polling in this objective.

## Required user experience

The landing screen is a run browser showing all discovered runs, newest
`updated_at` first. One unloadable or malformed run must not prevent other
runs from being displayed; it remains visible as a degraded row with an
actionable error.

The browser must separate durable workflow state from evidence of process
activity instead of treating every nonterminal phase as "running."

Durable state includes:

- current phase;
- `done`;
- `failed`;
- `awaiting input`;
- `operational failure`;
- unloadable state.

Activity labels must be evidence-based:

- "running" may be shown only when a validated local live lock can be
  associated with that run;
- absence of a lock may be reported as inactive at the time of inspection;
- an unassociated, remote, stale, or malformed lock must be shown
  conservatively and must not cause a particular run to be called running;
- a fresh-run lock without a run ID is repository-level activity with an
  unknown run association.

`running` requires the complete schema-2 identity chain (local hostname,
boot ID, live PID, matching process-start ticks, matching integration path,
and an associated loadable `RunState`), per ADR 0037. A schema-1 lock never
satisfies `running`. This classification must not be approximated by
recency heuristics.

Selecting a run opens a run-detail screen containing:

1. A current summary: run ID, durable phase, created/updated timestamps,
   integration branch, current task, loop counters, pending question, latest
   operational error, and conservative activity/lock information.
2. A chronological workflow timeline built from phase-history records. Each
   entry shows sequence, phase, resulting phase, outcome, recorded
   timestamp, counters, and whether a result or error is available.
   Repeated planning, building, verification, auditing, revision, and
   replan cycles must remain visible as distinct entries.
3. An opinionated detail view for the selected state/history record, plus an
   expandable escaped raw-JSON view for troubleshooting and forward
   compatibility.
4. Verification summaries and an explicitly opened, bounded log viewer.
   Full verification output is unredacted and may be sensitive, so it must
   not load automatically and the interface must say that clearly.

The application must support keyboard-only navigation, including selecting a
run, opening timeline details, returning to the browser, refreshing the
current snapshot, and quitting cleanly. Keyboard focus and the highlighted
row, record, or log must be preserved by stable identity across Back and
across both successful and failed refresh; the interface must never lose
the user's place or jump to a different row.

## Data and safety requirements

Current `RunState` must be loaded through the supervisor's validated state
reader rather than by naively parsing snapshot files.

Add a tested, presentation-independent read model for history, verification
logs, and lock observations. It must:

- validate run IDs and filename structure;
- treat the latest `RunState` as authoritative and history as best-effort;
- tolerate missing history, sequence gaps, disappearing files, pruned logs,
  and a partially written newest history record;
- prevent path traversal and reject symlink or non-regular-file log targets;
- constrain verification reads to the expected repository-owned
  verification tree;
- bound file sizes and rendered content;
- escape Rich/Textual markup in all repository-, agent-, and
  command-controlled text;
- avoid exposing the lock ownership token;
- keep one malformed run or record from crashing the application;
- never surface raw exception text (`str(exc)`/`repr(exc)`) in any rendered
  diagnostic or CLI error path; use fixed, safe classifications instead.

Missing history must be represented as unavailable or incomplete, never as
proof that a phase did not run. `updated_at` and history `recorded_at` are
persisted update/completion times, not phase-start times; the TUI must not
fabricate elapsed-time or stall information.

`ProjectSnapshot` is the single disk-derived read for a project: it is built
once at initial load and rebuilt only on an explicit refresh, and it must
carry immutable current-state, history, and verification metadata for every
bounded run. All navigation between the browser, a run's detail, and a
record's detail must read only the already-built snapshot; disk is read
again only by an explicit refresh action or by explicitly opening a
verification log's content.

## Lock owner-identity robustness

ADR 0037 decided PID-reuse-resistant lock ownership. Lock schema version 2
(`owner_boot_id`, `owner_process_start`) and the full identity-chain contract
are shipped end to end: writer-side acquisition and stale-lock recovery
(`src/loop_supervisor/locking.py`), and reader-side classification consumed
by `observe_lock` and the TUI (`src/loop_supervisor/read_model/
lock_observation.py`). A live PID with mismatched boot/start identity (PID
reuse) is correctly treated as stale, not as a live owner, on both sides.
The prior writer-side audit gaps (absent-PID `OSError` subtypes, non-UTF-8
`comm` decode failures, misleading "dead process" wording) are closed. This
section is retained only as a pointer to that contract for future changes;
it is not an open work item.

## Ordered priorities

Items 1 through 32 are delivered: the initial vertical slice, ADR
0036/0037, the Textual-independent read model, writer/reader
lock-identity hardening, a post-delivery audit's fixes for the TUI
refresh crash, record-selection reconciliation, lock-diagnostic and
startup sanitization, keyboard-focus preservation, the newest-history/
current-state disagreement diagnostic (ADR 0039), the sequence-gap
false positive, diagnostic hygiene, skeleton-prompt parity, and the
observable `0.2.0` version bump; a second-pass audit's hardening of a
recurring TUI list-widget crash family, importability without
installed distribution metadata, and rendering of the ADR 0039
disagreement diagnostics that were previously computed but never
shown; a builder-managed scratch directory (item 30) closing a
recurring `external_directory` denial the loop hit repeatedly while
delivering items 16-29; and, sharing ADR 0042, planner deferred-work
carry-forward (item 31) and automatic retry of transient operational
failures (item 32). ADR 0041 additionally superseded ADR 0040:
verification logs are write-once (`_summarize_verification` in
`supervisor.py`), so the same-size in-place-rewrite gap ADR 0040
flagged is not a mutation shape this project's writer can produce, and
the byte-comparison slice it proposed was not implemented.

A post-delivery audit of items 31-32 found that ADR 0042 itself decides
more than the two implementation slices built: it states plainly that
`operational_retry_count` is the sixth history counter, amends ADR
0038's reset table and ADR 0039's comparison to cover it, and requires
that new history records always write six counters, none of which
reached the code. This is latent, not active -- the writer still emits
five counters, so nothing currently breaks -- but it is a documented
decision the code does not yet match, and leaving it unaddressed would
let a future change follow the ADR's own instruction to widen the
writer before the reader tolerates it. Item 33 below closes that gap.
No new ADR is required; ADR 0042 is not rewritten, only implemented.

31. Give the planner a narrow, mechanical way to carry a just-completed
    task's stated rationale into its next invocation, and require it to
    check that rationale for a named, still-unresolved deferral before
    considering the objective complete. This item was previously held
    back in "Deferred work" below; it is promoted here because it
    shares an architect decision and a `RunState` shape change with item
    32, and doing both in one ADR avoids two separate state-shape
    revisions for closely related loop-control changes. Two
    independently mergeable slices:
    - Add an optional `last_completed_task` field to `RunState`
      (`task_id`, `objective`, `rationale`; `None` by default, no schema
      version bump), populated by `_finish_task_cleanup` immediately
      before it clears `planner_result`, and included by
      `_build_planner_prompt` on the next planning invocation whenever it
      is set. This carries forward exactly one task's worth of context
      across an accepted-task boundary; it does not need to persist
      beyond that.
    - Update the planner agent prompt
      (`.opencode/agents/loop-planner.md`) to: (a) treat a carried-forward
      rationale that names a deliberately deferred portion as a lead to
      verify against the current repository state, not as proof, and
      select that portion first if it is still genuinely absent; (b)
      require, before returning status COMPLETE, that each bullet of this
      document's "Completion criteria" be checked against the repository
      rather than inferred from memory of prior invocations, returning
      READY for the smallest slice closing any bullet found unmet; and
      (c) require that a deliberately deferred portion be named in the
      returned `rationale` field, not only in a worktree commit message,
      since only `rationale` is ever visible to a future invocation.
    Note the field above carries context for exactly one task boundary.
    A deferral that survives more than one accepted task between when it
    is named and when it is next picked up will not be caught by this
    mechanism; closing that gap, if it proves necessary in practice, is
    intentionally left for a later decision rather than solved
    speculatively here.
32. Automatically retry a transient operational failure instead of
    always stopping for a human. `Supervisor.run()` unconditionally
    raises on `AdvanceStatus.OPERATIONAL_FAILURE`, even though the
    persisted error record already distinguishes retryable transient
    failures (e.g. `AgentInvocationError` from a denied
    `external_directory` request, `PhaseTimeoutError`, a plain
    `GitError`) from failures that require human repair (a merge
    conflict, a dirty `cleanup_worktree`, an unresolved `DecisionError`)
    -- `retryable`/`requires_repair` on `OperationalErrorRecord` and
    `_do_retry_operational_failure()` already know how to resume the
    former; nothing currently calls that path automatically. This
    recurred four times across the runs that delivered items 16-29,
    each requiring a manual `resume` that did nothing a loop iteration
    could not have done itself. Two independently mergeable slices:
    - Record an ADR deciding: a new persisted, run-scoped
      `operational_retry_count` counter, reset to zero on every
      successful `advance()` (so it bounds *consecutive* failures, not
      a run's lifetime total, matching how `max_builder_guidance_attempts`
      already behaves) and incremented only on an auto-retried
      operational failure; a `max_operational_retries` limit (default
      3) on `Limits`/`RunOptions`, gated so only `retryable and not
      requires_repair` failures auto-retry -- a failure requiring
      repair must still stop for a human exactly as today; whether and
      how the new counter joins the five counters ADR 0038's reset
      table already enumerates, and whether (and how) it participates
      in ADR 0039's newest-history/current-state comparison; and how
      `RunOptions` accepts a run resumed from state saved before this
      field existed, since `RunOptions.from_dict` currently raises
      `StateError` on any unknown field.
    - Implement the decision: the new counter and limit; auto-retry in
      `Supervisor.run()`'s `OPERATIONAL_FAILURE` branch, incrementing
      the counter, persisting state, and continuing the loop (so the
      next `advance()` reaches `_do_retry_operational_failure()`)
      instead of raising, until the limit is reached or the failure is
      classified as requiring repair; a short, interrupt-safe delay
      between automatic retries so a genuinely flapping dependency
      cannot exhaust the budget in a tight loop; and a `-v` line on
      each automatic retry distinguishing it from a human-initiated
      `resume`, so a log reader is never left wondering which happened.
33. Implement ADR 0042's sixth-history-counter contract, which the
    runs delivering items 31-32 decided but did not build. ADR 0042
    states that `operational_retry_count` is the sixth history
    counter, amends ADR 0038's reset table and ADR 0039's
    same-persisted-transition comparison to cover it, and requires
    that new history records always write six counters. None of that
    reached the code: `history.py`'s `_COUNTER_FIELDS`,
    `read_model/history.py`'s `_COUNTER_FIELDS` and
    `_RESET_TRANSITIONS_BY_COUNTER`, `read_model/snapshot.py`'s
    `CurrentStateDisagreementField`, `read_model/current_run.py`, and
    `tui/browser.py` all still know only the original five. This is
    latent rather than active: the writer also still emits five, so
    the reader's exact-set check currently passes. The decision is
    already recorded and must not be re-litigated -- no new ADR is
    required for this item.
    Three slices, which MUST merge in this order:
    - Reader tolerance first. `_validate_record` in
      `read_model/history.py` currently rejects any record whose
      counters are not exactly the five known fields; widen it to
      accept either five or six, normalizing an absent
      `operational_retry_count` to zero per ADR 0042, and carry the
      field on `HistoryEntry.counters`. Merging this alone changes no
      observable behavior, because the writer still emits five.
    - Writer and diagnostics second, never before the slice above:
      add `operational_retry_count` to `history.py`'s `_COUNTER_FIELDS`
      so new records write six; extend ADR 0038 regression checking
      to the new counter; and extend ADR 0039's comparison so it joins
      exact equality when newest valid history is strictly newer than
      current `RunState`, and is suppressed like the other resettable
      counters when current state may be later, leaving
      `accepted_task_count` as the only cross-generation monotonic
      comparison. Note this counter's reset rule is a predicate, not
      an enumerable transition set: unlike the other four it resets on
      nearly every successful advance, so it does not belong in
      `_RESET_TRANSITIONS_BY_COUNTER`'s tuple table. A decrease is a
      permitted reset exactly when the new value is zero, the
      record's `phase` is not `operational_failure`, and its `status`
      is not `INPUT_UNAVAILABLE` -- mirroring the writer condition in
      `_finalize_advance` and the `_InputRequiredSignal` handler.
    - Presentation third, and independently mergeable relative to the
      two above since it reads `RunState` rather than history: surface
      the counter on `read_model/current_run.py`'s `CurrentRun`
      (including its `degraded` constructor), add it to the
      `CurrentStateDisagreementField` literal, and render it in
      `tui/browser.py`'s current-summary counter block, history-entry
      counter line, and disagreement labels. Without this an operator
      cannot see how much of a run's retry budget has been consumed;
      the `-v` line added by item 32 only helps someone watching a
      live log.
    Tests must cover a legacy five-counter record loading with the
    counter normalized to zero, a six-counter record round-tripping,
    a permitted reset on an ordinary successful advance, forbidden
    decreases on an operational-failure unwrapping record and on an
    input-unavailable record, a decrease to a nonzero value, and both
    ADR 0039 timestamp windows for the new counter.
34. Add a `--max-operational-retries` flag to `run`, defaulting to 3
    and threaded through `cmd_run`'s `RunOptions.from_dict` call
    alongside the three limit flags already there, so ADR 0042's
    limit is operator-settable and a zero value can disable automatic
    retry from the command line. It is currently reachable only via
    its dataclass default.
35. Sanitize `ProjectResolutionError` at its source rather than at its
    sole consumer. `read_model/project.py`'s `resolve_project` builds
    the message `f"Cannot resolve project {path}: {exc}"`, embedding
    the underlying Git command's stdout/stderr in the exception
    object. `cmd_tui` (item 19's fix) no longer prints it, so nothing
    currently discloses it, but the containment is at the caller: a
    second caller, or an unhandled propagation, would expose it again.
    Replace the interpolated `{exc}` with a fixed, safe
    classification chosen from the caught type -- the same approach
    `read_model/current_run.py`'s `_safe_diagnostic` already uses for
    `StateError`/`OSError` -- keeping the resolved path, which is
    operator-supplied and already OS-bounded. Update
    `tests/test_read_model_project.py`'s `match="Cannot resolve
    project"` assertion and add one proving underlying Git output does
    not reach the exception's string form.
36. Route the three remaining TUI render sites through the shared
    rendered-output-ceiling helper that item 24's consolidation
    introduced, so no rendered site bypasses it: the project path line
    (`_compose_browser`'s `f"Project: {...integration_root}"`), the
    record-list row label (`_record_label`'s `f"Sequence {record.seq}:
    {record.phase} ..."`), and the verification log-list row label.
    Each currently interpolates a value bounded separately upstream --
    an OS-bounded path, an ordinal capped at 128 digits, a phase name
    from a fixed set -- so this is a consistency gap, not a reachable
    overflow, and the change should be behavior-preserving for every
    in-bounds value. Fixing it removes the standing need to re-derive
    that upstream-bounded argument at each site during future audits.
37. Close two gaps in ADR 0039's current-state disagreement test
    coverage. First,
    `test_build_snapshot_suppresses_resettable_disagreements_when_current_is_later`
    is parametrized over ADR 0038's four reset-family transitions but
    still passes if every transition is replaced with an unrelated
    phase pair, because suppression in that window is driven entirely
    by the timestamp gate in `_current_state_disagreements`, not by
    the transition -- the parametrization asserts nothing it appears
    to. Either assert something transition-specific or state plainly
    that the suppression is transition-blind and reduce the
    parametrization to match, so the test does not imply coverage it
    lacks. Second, add the missing ordinary post-merge case where
    history's `accepted_task_count` exceeds current `RunState`'s --
    a lagging or failed history write -- which is the one comparison
    ADR 0039 keeps active in the current-may-be-later window and
    which no test currently exercises.

## Deferred work (not yet scheduled)

There is no work currently held back from scheduling.

## Completion criteria

The objective is complete when:

- `loop-supervisor tui [--project PATH]` launches the real TUI;
- all runs are listed newest first, including degraded unloadable entries;
- durable state and evidence-based activity are clearly distinguished;
- selecting a run exposes its summary and ordered workflow;
- phase results, errors, raw records, verification summaries, and
  explicitly selected bounded logs are inspectable;
- manual refresh reflects changed, added, removed, or newly malformed
  files without restarting or crashing, on every project shape including
  zero discovered runs;
- the TUI acquires no mutating supervisor lock and performs no writes;
- Textual tests cover navigation and rendering;
- read-model tests cover malformed, partial, missing, pruned, symlinked,
  traversal, oversized, and changing data;
- CLI tests prove the browser launches and current-directory project resolution
  works;
- `running` requires the complete schema-2 identity chain and a schema-1
  lock never satisfies it;
- run detail reflects only the manually built/refreshed snapshot; a failed
  refresh preserves the prior snapshot and selection (run and record) and
  reports failure without crashing; keyboard focus is preserved across both
  successful and failed refresh;
- a project-level scan failure or resolution failure, at startup or on
  refresh, is a clean, sanitized, bounded error on every relevant exception
  type, not a traceback and not raw exception text;
- browser rows and diagnostics, including lock-observation diagnostics, are
  fully sanitized (no raw exception text anywhere) and stay within the
  existing rendered-output bounds on every rendered diagnostic and row;
- history contradictions (phase discontinuity, timestamp reversal, counter
  regression, current-state disagreement) surface as rendered diagnostics
  without overriding current `RunState`, and a sequence gap does not also
  produce a spurious adjacent-record contradiction;
- ADR 0041 records the accepted verification-log mutation-detection scope:
  the shipped metadata-and-reopen check detects a replacement whenever the
  reopened target's numeric inode number (`st_ino`) changed and an in-place
  rewrite of the opened inode whenever its size or `mtime_ns` changed; it
  does not detect a same-`st_ino`, different-`st_dev` replacement or a
  genuine same-size, same-`mtime_ns` in-place rewrite, and no further
  content-comparison work is scheduled because this project's writer
  cannot produce that in-place shape;
- the distribution version is bumped to `0.2.0` and observable at runtime
  via both `loop-supervisor --version` and `doctor`, sourced from package
  metadata rather than duplicated in source; the release tag itself is
  left for a human to create against the merged `main` commit;
- `operational_retry_count` is written and read as the sixth history
  counter, participates in ADR 0038 reset validation and ADR 0039
  current-state comparison, and is visible in the TUI's current
  summary and timeline; history records written before it existed
  still load with it normalized to zero;
- the operational retry limit is settable from the command line;
- no rendered diagnostic or exception message embeds raw underlying
  command or exception text, at the source and not merely at the
  caller;
- the configured Ruff, formatting, mypy, and pytest gates pass.

## Out of scope

The following are explicitly excluded:

- starting, resuming, stopping, pruning, or repairing runs;
- answering pending questions;
- automatic refresh or background polling;
- direct `RunSession` ownership;
- SSE/token/tool activity;
- direct access to OpenCode's private database or logs;
- fabricated "running," phase-start, elapsed-time, heartbeat, or stall
  data;
- migration or raw interpretation of unsupported `RunState` schema
  versions;
- copying verification output into a second TUI-specific persistence
  format.

This objective is the single authoritative live workstream. Do not select
unrelated open items from the lifecycle backlog or historical plans.
Existing ADRs remain constraints, but ADRs 0008, 0019, and 0021 describe
the retired TUI and must not be treated as instructions to restore it. ADR
0035 governs the replacement direction.
</content>
