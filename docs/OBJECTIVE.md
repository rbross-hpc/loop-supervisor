# Objective

This is the `loop-supervisor` project itself: a headless supervisor that
drives an OpenCode planner/architect/builder/auditor loop over Git worktrees.

The current objective is to replace the temporary `loop-supervisor tui` stub
with a production-quality, read-only Textual interface for exploring run data
captured by the supervisor.

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

The first release may expose only the distinctions supported safely by
current lock data. A later priority is to investigate durable active-run
identity or a heartbeat so fresh runs and PID reuse can be classified more
reliably (see "Ordered priorities" item 7). That work must not be
approximated by recency heuristics.

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
current snapshot, and quitting cleanly.

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
- keep one malformed run or record from crashing the application.

Missing history must be represented as unavailable or incomplete, never as
proof that a phase did not run. `updated_at` and history `recorded_at` are
persisted update/completion times, not phase-start times; the TUI must not
fabricate elapsed-time or stall information.

## Ordered priorities

1. Define the disk-read boundary, evidence-based status taxonomy, refresh
   semantics, history validation, log-containment policy, and module
   boundaries. Record non-obvious decisions in a new ADR that builds on
   ADRs 0034 and 0035.
2. Implement and thoroughly test a Textual-independent read model for run
   discovery, current snapshots, history, verification logs, and
   conservative lock observations.
3. Replace the `cmd_tui` stub with a minimum vertical slice: optional
   `--project`, newest-first run browser, run selection, summary, workflow
   timeline, manual refresh, back, and quit.
4. Add opinionated result/error detail, escaped raw JSON, and the opt-in
   bounded verification-log viewer.
5. Harden empty/loading/degraded states, narrow layouts, concurrent
   filesystem changes, resource cleanup, and keyboard affordances.
6. Update README, installation, skeleton, skill, and CLI documentation so
   none describes `tui` as unavailable. Exercise it against realistic
   persisted fixtures and, where practical, artifacts from a bounded real
   supervisor run.
7. After the read-only TUI is complete, investigate reliable active-run
   attribution or heartbeat persistence (e.g. a PID/liveness field in the
   lock record) as a separate design task. Do not block the initial
   explorer on pretending current lock data can answer more than it can.

## Completion criteria

The objective is complete when:

- `loop-supervisor tui [--project PATH]` launches the real TUI;
- all runs are listed newest first, including degraded unloadable entries;
- durable state and evidence-based activity are clearly distinguished;
- selecting a run exposes its summary and ordered workflow;
- phase results, errors, raw records, verification summaries, and
  explicitly selected bounded logs are inspectable;
- manual refresh reflects changed, added, removed, or newly malformed
  files without restarting or crashing;
- the TUI acquires no mutating supervisor lock and performs no writes;
- Textual tests cover navigation and rendering;
- read-model tests cover malformed, partial, missing, pruned, symlinked,
  traversal, oversized, and changing data;
- CLI tests prove the stub has been replaced and current-directory project
  resolution works;
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
