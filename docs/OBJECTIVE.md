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

Items 1 through 15 (the initial vertical slice, ADR 0036/0037, the
Textual-independent read model, and the writer/reader lock-identity
hardening) are delivered. A post-delivery audit of that work found the
defects and gap addressed by items 16 through 25 below; item 26 closes
this objective's run with an observable version bump. Each is
independently mergeable and subject to the same task-sizing,
one-mergeable-slice-at-a-time discipline, and the same Ruff/formatting/
mypy/pytest gates, as all prior priorities.

16. Fix the explicit-refresh crash on a project with zero discovered runs.
    `action_refresh` unconditionally calls `_remember_browser_highlight`,
    which queries the `#run-list` widget; that widget does not exist when
    no runs are discovered (the browser renders an empty-state message
    instead), and the query sits outside the failure-handling `try` block.
    Refreshing such a project currently raises an uncaught widget-lookup
    exception instead of the safe, bounded outcome item 11 already
    requires. Add a regression test that refreshes an empty-run project
    and a project whose only run was deleted since the last snapshot.
17. Make explicit refresh reconcile the open **record** selection by
    stable ID, not only the selected run. Currently only the run-level
    selection and browser highlight survive a refresh; the open record
    detail, if any, continues to render its pre-refresh content even when
    the snapshot changed or the record disappeared, and if the selected
    run itself disappears, a stale record-selection index can be
    misapplied to render the wrong run's record once a different run is
    selected. On refresh: reload the open record's content from the new
    snapshot by its stable ID if it is still present; close the record
    detail (returning to run detail) if it is not; and never let a
    record-selection value outlive the run selection it was scoped to.
18. Sanitize lock-observation diagnostics. `observe_lock` currently passes
    raw `str(exc)` (including absolute lock paths and parser text) from a
    malformed-lock read failure and from an integration-path
    canonicalization failure into `LockObservation.diagnostic`, which the
    TUI renders verbatim in the run-detail summary. Replace both with
    fixed, safe classifications, following the pattern already used by
    the read model's other diagnostic sources (e.g. run-discovery and
    current-run diagnostics). This closes an unmet clause of item 12
    below.
19. Contain and sanitize project-resolution and startup-scan failures the
    same way refresh failures are already contained. Two independently
    mergeable slices:
    - `cmd_tui`'s `ProjectResolutionError` path currently prints the raw
      underlying error, which can embed full Git command stdout/stderr
      and is unbounded; replace it with a fixed, safe, bounded message,
      matching the existing `StateError` handling immediately below it in
      the same function.
    - `cmd_tui` (and snapshot/discovery construction generally) currently
      catches only `StateError` for a project-level scan failure; an
      `OSError` raised during directory enumeration (e.g. a mid-scan
      permission or mount change) is not caught and can still surface as
      an uncaught traceback at startup. Catch it the same way the
      existing explicit-refresh path already does.
20. Preserve keyboard usability across refresh, beyond the row highlight
    already preserved. A failed refresh currently preserves the
    highlighted row but not keyboard focus, so the preserved highlight is
    not actually usable without an extra manual click/keypress to
    refocus; a successful refresh resets the record-list cursor and
    focus to the top instead of preserving them. Restore focus to the
    list the user came from in both cases, per the existing keyboard-
    navigation requirement above.
21. Implement the last undelivered history-contradiction category:
    disagreement between the newest history entry and the authoritative
    current `RunState`. History validation today only compares adjacent
    history records to each other; it has no comparison against current
    `RunState` at all. Two independently mergeable slices:
    - Record an ADR defining what constitutes a reportable disagreement
      between the newest valid history entry and current `RunState`
      (candidates include: the newest entry's `phase_after` disagreeing
      with `RunState.phase` when no later transition is pending;
      persisted counters in the newest entry exceeding or contradicting
      `RunState`'s counters; the newest entry's `recorded_at` occurring
      after `RunState.updated_at`), calibrated to avoid reintroducing the
      phase-blind, non-lifecycle-aware false positives that motivated ADR
      0038's transition-aware counter design. The ADR must not change
      current `RunState`'s authority: a detected disagreement is a
      diagnostic, never a correction.
    - Implement and test the decision, preserving every valid history
      entry and every existing contradiction diagnostic unchanged.
22. Stop reporting a spurious phase-discontinuity diagnostic when a
    sequence gap already explains the discontinuity. Adjacent-record
    comparison currently runs over the list of records that survived
    validation, so when an intervening record is missing or excluded
    (already reported as a sequence gap or a malformed-record
    diagnostic), the two records on either side of the gap are also
    compared to each other and reported as a phase discontinuity that
    does not reflect any actual contradiction in the underlying history.
    Skip the adjacent-record comparison whenever the two records are not
    truly sequence-adjacent (`following.seq != preceding.seq + 1`).
23. Record an ADR describing the accepted scope of the verification log
    post-read mutation check, and correct this objective and the shipped
    code to match it precisely. The current check reliably detects a log
    replaced by a different inode after the first read, but a same-size,
    same-inode in-place rewrite is detected only when the file's mtime
    changes at the granularity the filesystem actually provides; on at
    least one supported filesystem this granularity is coarse enough that
    an ordinary (non-adversarial) same-size concurrent rewrite is missed
    in the common case, and an adversarial rewrite that restores the
    original mtime is never detected by this check at all. The ADR must
    state plainly which mutation shapes are and are not detected and why
    closing the remaining gap is or is not warranted now. If the decision
    is to accept the current scope, correct item 13's historical wording
    above (already satisfied) and the corresponding test's naming/intent
    so neither overstates same-size in-place detection as covered. If the
    decision is to close the gap, implement it as a follow-on slice using
    a stronger, still-explicit, still-bounded, still-descriptor-relative
    check, with a test that exercises a genuine same-size, same-mtime
    in-place rewrite (not only a size-changing one).
24. Diagnostic-hygiene cleanup, each independently mergeable:
    - Apply the verification read model's existing bounded-name helper to
      every diagnostic site that includes an untrusted filename, not only
      some of them, so a diagnostic artifact string cannot grow
      unboundedly (observed with adversarially many colliding ordinal
      spellings).
    - Consolidate the several duplicated copies of the 256 KiB /
      10,000-line rendered-output ceiling constants and near-identical
      bounding functions in the TUI into one shared helper, applied
      consistently.
    - Remove the one remaining raw-exception-text passthrough in a
      history-validation diagnostic reason, replacing it with a fixed
      classification consistent with the rest of that module.
    - Make the `builder_guidance_count` reset permitted on a
      `building`-to-`verifying`/`auditing` transition conditional on that
      record's recorded status, matching ADR 0038's stated rationale
      ("a successful building transition") rather than only its phase
      pair.
25. Sync the current planner agent prompt into the project-skeleton copy
    used by newly initialized projects
    (`src/loop_supervisor/_skeleton/.opencode/agents/loop-planner.md`),
    which has drifted out of sync with the live prompt, and restore
    parity test coverage for it in `tests/test_cli_init.py` (currently
    disabled pending this sync; see the comment marking why).
26. Bump the distribution version and make it observable at runtime.
    **This item must be selected last, after every other item in
    "Ordered priorities" above is complete**, so the version names the
    finished state of this objective.

    `pyproject.toml`'s `version` is still `0.1.0`, which predates the
    entire read-only TUI browser, ADRs 0035-0038, and lock schema v2. The
    version is not exposed anywhere in the package or the CLI today --
    there is no `__version__`, no `--version` flag, and `doctor` reports
    only the Python version -- so an installed copy cannot be
    distinguished from any earlier build at runtime.

    - Bump `version` to `0.2.0` in `pyproject.toml`.
    - Expose `__version__` sourced from installed package metadata
      (`importlib.metadata`), not hardcoded a second time, so the number
      lives in exactly one place.
    - Add a `--version` flag to the CLI.
    - Report the version in `doctor` output alongside the existing
      Python-version check.

    Do not create a Git tag. A release tag must name the post-merge
    commit on `main`, which does not exist while this task is being
    built in a worktree; tagging is a human step after this objective's
    run completes.

## Deferred work (not yet scheduled)

Priorities 16 through 26 above are the current active work. The following
is captured so it is not lost, but it is deliberately **not** part of
"Ordered priorities" yet: the planner must not select it, and it is not
part of this objective's completion criteria, until a human promotes it
into "Ordered priorities" by editing this document.

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
  regression, current-state disagreement) surface as diagnostics without
  overriding current `RunState`, and a sequence gap does not also produce a
  spurious adjacent-record contradiction;
- the verification log mutation-detection scope is recorded in an ADR and
  this objective's wording matches what is actually implemented;
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
