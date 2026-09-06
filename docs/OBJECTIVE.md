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

Items 1 through 29 are delivered: the initial vertical slice, ADR
0036/0037, the Textual-independent read model, writer/reader
lock-identity hardening, a post-delivery audit's fixes for the TUI
refresh crash, record-selection reconciliation, lock-diagnostic and
startup sanitization, keyboard-focus preservation, the newest-history/
current-state disagreement diagnostic (ADR 0039), the sequence-gap
false positive, diagnostic hygiene, skeleton-prompt parity, and the
observable `0.2.0` version bump, and a second-pass audit's hardening of
a recurring TUI list-widget crash family, importability without
installed distribution metadata, and rendering of the ADR 0039
disagreement diagnostics that were previously computed but never shown.
ADR 0041 additionally superseded ADR 0040: verification logs are
write-once (`_summarize_verification` in `supervisor.py`), so the
same-size in-place-rewrite gap ADR 0040 flagged is not a mutation shape
this project's writer can produce, and the byte-comparison slice it
proposed was not implemented.

The following items from that second-pass audit were intentionally left
as still open, lower-priority follow-ups rather than scheduled here:

- `read_model/project.py`'s `ProjectResolutionError` still embeds the
  full underlying Git command's stdout/stderr in the exception object,
  even though `cmd_tui` (item 19's fix) no longer prints it. This is
  containment at the sole consumer, not sanitization at the source; a
  future second caller, or an unhandled propagation, could still expose
  it.
- Three TUI render sites still bypass the shared rendered-output-ceiling
  helper introduced for item 24's consolidation: the project path line,
  the record-list row label, and the verification log-list row label.
  Each interpolates a value that is separately bounded upstream (an
  OS-bounded path, an ordinal capped at 128 digits, a phase name
  constrained to a fixed set), so this is a consistency gap, not a
  reachable overflow.
- The reset-family test coverage for ADR 0039's newest-history/
  current-state disagreement diagnostic is partly decorative: a test
  parametrized over ADR 0038's four reset-family transitions still
  passes if every transition is replaced with an unrelated phase pair,
  because suppression in that window is driven entirely by the
  timestamp gate, not by the transition. There is also no test for the
  ordinary post-merge shape where history's `accepted_task_count`
  exceeds current `RunState`'s (a lagging or failed history write).

There is no remaining scheduled work in "Ordered priorities" as of this
revision; see "Deferred work" below for the one item intentionally held
back from scheduling.

## Deferred work (not yet scheduled)

Priorities 16 through 29 above are delivered. The following is captured
so it is not lost, but it is deliberately **not** part of "Ordered
priorities": the planner must not select it, and it is not part of this
objective's completion criteria, until a human promotes it into "Ordered
priorities" by editing this document.

This item exists because the audit that produced items 16-25 was itself
lost between planner invocations: a builder commit message deferred the
newest-history/current-`RunState` comparison (item 21) to "a follow-on
slice," the auditor accepted that framing, and the planner then reported
the objective COMPLETE without the deferred slice ever being scheduled,
because the deferral existed only in commit prose the planner never reads
and has no instruction to look for.

27. Give the planner a narrow, mechanical way to carry a just-completed
    task's stated rationale into its next invocation, and require it to
    check that rationale for a named, still-unresolved deferral before
    considering the objective complete. Two independently mergeable
    slices:
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
