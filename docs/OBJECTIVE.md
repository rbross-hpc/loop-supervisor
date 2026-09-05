# Objective

This is the `loop-supervisor` project itself: a headless supervisor that
drives an OpenCode planner/architect/builder/auditor loop over Git worktrees.

The read-only `loop-supervisor tui` run browser has shipped. It is a
production-quality Textual interface for exploring supervisor-captured run data;
this document records its delivered product contract, active-run attribution's
lock-writer implementation, and the remaining writer-hardening and
reader/TUI-classification work still in progress (see "Ordered priorities").

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
current lock data. Durable active-run identity has since been decided (ADR
0037) and its writer-side schema and stale-lock recovery are shipped so PID
reuse cannot be recovered as a live owner; a follow-on audit's writer-side
robustness fixes must land first (see "Ordered priorities" item 8), then
classifying reads against that identity (item 9). That work must not be
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

## Lock owner-identity robustness (writer-side)

ADR 0037's writer-side schema and stale-lock recovery are shipped
(`src/loop_supervisor/locking.py`), but a follow-on audit found gaps between
the shipped code and the ADR's identity-chain contract. These are writer-side
correctness and coverage fixes within ADR 0037's existing design, not a design
change:

- A recorded owner that no longer exists is stale evidence regardless of
  which `OSError` subtype the kernel raises for a mid-read
  `/proc/<pid>/stat` disappearance. `FileNotFoundError` is already handled;
  `ProcessLookupError`/`ESRCH` (the kernel can raise either for a process
  that exits between `open()` and `read()`) must be treated the same way,
  not surfaced as unverifiable.
- Reading local kernel identity must never let an unexpected exception
  escape the classifier. `/proc/<pid>/stat`'s `comm` field is arbitrary
  bytes and need not be valid UTF-8; a decode failure there (or any other
  unexpected read failure) must resolve to a normalized `LockError` and
  therefore `IdentityStatus.UNVERIFIABLE`, never propagate a raw
  `UnicodeDecodeError` out of `acquire()` or stale-lock recovery.
- `IdentityStatus.UNVERIFIABLE` remains fail-closed: it must never be
  treated as proof of either liveness or staleness, in either acquisition
  or recovery.
- Operator-facing recovery messages and `SupervisorLock`'s docstrings
  (`recover_stale`, `_inspect_existing_lock`, `classify_local_owner_identity`)
  must describe schema-2 staleness accurately: the recorded owner can be
  stale while the numeric PID names a live successor process (PID reuse or
  a reboot that reused the PID), not only when "the PID is dead." Wording
  that says "dead process" for a schema-2 stale classification is
  misleading and must be corrected.
- Source documentation must not claim capability that does not exist yet:
  `classify_local_owner_identity`'s docstring must describe its current
  writer-side-only use; claiming it is already shared with the read model
  is inaccurate until the read model actually consumes it (see "Ordered
  priorities" item 9).

## Ordered priorities

1. **Delivered.** Define the disk-read boundary, evidence-based status
   taxonomy, refresh semantics, history validation, log-containment policy, and
   module boundaries in ADR 0036.
2. **Delivered.** Implement and thoroughly test a Textual-independent read model
   for run discovery, current snapshots, history, verification logs, and
   conservative lock observations.
3. **Delivered.** Launch the minimum vertical slice from `cmd_tui`: optional
   `--project`, newest-first run browser, run selection, summary, workflow
   timeline, manual refresh, back, and quit.
4. **Delivered.** Add opinionated result/error detail, escaped raw JSON, and the
   opt-in bounded verification-log viewer.
5. **Delivered.** Harden empty/loading/degraded states, narrow layouts,
   concurrent filesystem changes, resource cleanup, and keyboard affordances.
6. **Delivered.** Update README, installation, skeleton, skill, and CLI
   documentation to describe the shipped read-only browser. Read-model and
   Textual fixtures provide repository-verifiable coverage; exercising a bounded
   real supervisor run remains optional validation, not an open delivery item.
7. **Delivered (design and writer).** ADR 0037 decides PID-reuse-resistant lock
   ownership. Lock schema version 2 (`owner_boot_id`, `owner_process_start`) and
   the writer-side identity chain are shipped: acquisition fails closed if it
   cannot read local kernel identity, and schema-2 stale-lock recovery correctly
   treats a live PID with mismatched boot/start identity (PID reuse) as stale,
   not as a live owner.
8. **Current priority (writer-side hardening; closes the Part 1 audit).**
   Fix the gaps recorded in "Lock owner-identity robustness (writer-side)"
   above. Independently mergeable slices:
   - 8a (behavioral fix; land first): in
     `src/loop_supervisor/locking.py`, make `_read_process_start` treat
     `ProcessLookupError`/`ESRCH` as an absent PID (stale), alongside the
     existing `FileNotFoundError` handling, and catch a `comm`-field decode
     failure (or any other unexpected read failure) as a normalized
     `LockError` so it resolves to `IdentityStatus.UNVERIFIABLE` rather than
     escaping uncaught. Preserve the existing `FileNotFoundError`-as-stale
     and `UNVERIFIABLE`-fail-closed contracts exactly. Add focused tests
     that exercise the real `_read_process_start`/`_read_boot_id` functions
     (not mocks): malformed `/proc/<pid>/stat` content, a process name
     containing parentheses, an empty boot ID, a non-`FileNotFoundError`/
     `ProcessLookupError` `OSError`, and a non-UTF-8 `comm` field.
   - 8b (wording and docstrings): correct the `StaleLockError` "dead
     process" message and the `recover_stale` / `_inspect_existing_lock` /
     `classify_local_owner_identity` docstrings so they describe PID-reuse
     staleness accurately and describe the reader-sharing status correctly
     (writer-side only, until item 9 lands).
   - 8c (test coverage): add an end-to-end recovery test with a matching
     boot ID and a differing process-start value (ordinary same-boot PID
     reuse, not a reboot), carried through `_inspect_existing_lock` both
     with and without `recover_stale`; add a genuine schema-2 lock fixture
     (with valid `owner_boot_id`/`owner_process_start`) whose only defect
     is `schema_version: 2.0`, in both `tests/test_locking.py` and
     `tests/test_read_model_lock_observation.py`.
   This priority does not change ADR 0037's design and requires no new
   ADR.
9. Update the read model's `observe_lock` and the TUI to consume the
   schema-2 identity chain via `classify_local_owner_identity` (after item
   8 has hardened it): `running` must require the complete match (local
   hostname, boot ID, live PID, matching process-start ticks, matching
   integration path, and an associated loadable `RunState`), per ADR 0037.
   Schema-1 locks must never satisfy `running`; distinguish stale from
   unverifiable evidence without exposing the lock ownership token.
   Replace or update any read-model test that currently encodes the
   legacy PID-only "live PID implies running" behavior. Do not alter the
   shipped explorer's classification by pretending current lock data can
   answer more than it can until this lands.
10. Make `ProjectSnapshot` genuinely complete: include immutable
    current-state, history, and verification metadata for every bounded
    run in the snapshot built at initial load and at each explicit
    refresh. Run-detail navigation (opening a run, opening a record,
    returning to the browser) must read only the already-built snapshot,
    not reread disk. Only an explicit refresh action rebuilds
    disk-derived data and reconciles selection by stable run/record ID,
    per this objective's existing "Data is refreshed only when the user
    explicitly requests it" requirement.
11. Contain startup and refresh failures: a project-level scan failure
    (e.g. an unreadable or replaced state directory) must become a safe,
    bounded diagnostic rather than an uncaught exception. Launching the
    TUI on such a project must show a clear error, not a traceback.
    Explicit refresh must preserve the previously displayed snapshot and
    selection and report the refresh failure, rather than losing the
    prior view; any open log content is closed rather than silently
    carried across a failed or successful refresh.
12. Sanitize and bound every rendered diagnostic: replace raw
    `StateError`/`OSError` text interpolation in run-row and
    project-level diagnostics with fixed, safe classifications and
    bounded logical artifact names, and enforce the existing 256 KiB /
    10,000-line rendered-output ceiling on every diagnostic and row, not
    only on opinionated detail views.
13. Harden verification discovery: bound the number of ordinal digits
    accepted from a log filename before converting it with `int()`
    (mirroring history's existing bound), and strengthen the
    post-read mutation check so a log replaced or modified after the
    first `fstat` is still reported as `changed_during_read`. Keep
    explicit, bounded, descriptor-relative, no-follow log access
    unchanged.
14. Report history contradictions instead of silently accepting them:
    detect and diagnose adjacent-record phase discontinuity, timestamp
    reversal, counter regression, and disagreement between the newest
    history entry and the authoritative current `RunState`, while
    preserving every other valid entry and keeping current `RunState`
    authoritative, per this objective's existing "treat the latest
    `RunState` as authoritative" requirement.
15. Repair TUI keyboard-navigation state: preserve the highlighted run,
    record, or log by stable identity across Back and across refresh
    (do not silently jump to a different row), restore keyboard focus to
    the list the user came from, and stop overloading the advertised
    Refresh binding to also mean "expand raw JSON" in record detail.

Each priority above (8 through 15) is subject to the same task-sizing,
one-mergeable-slice-at-a-time discipline as items 1 through 7, and to the
same Ruff/formatting/mypy/pytest gates.

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
- CLI tests prove the browser launches and current-directory project resolution
  works;
- a recorded lock owner that no longer exists is classified stale for every
  relevant `OSError` subtype the kernel can raise during a mid-read
  disappearance, and any identity-read/decode failure resolves to a
  controlled `STALE`/`UNVERIFIABLE` outcome rather than an uncaught
  exception, with focused tests at the real `_read_process_start`/
  `_read_boot_id` boundary and an end-to-end same-boot PID-reuse recovery
  test;
- `running` requires the complete schema-2 identity chain and a schema-1
  lock never satisfies it;
- run detail reflects only the manually built/refreshed snapshot; a failed
  refresh preserves the prior snapshot and selection and reports failure;
  a project-level scan failure at startup is a clean error, not a
  traceback;
- browser rows and diagnostics are sanitized (no raw exception text) and
  stay within the existing rendered-output bounds;
- history contradictions (phase discontinuity, timestamp reversal, counter
  regression, current-state disagreement) surface as diagnostics without
  overriding current `RunState`;
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
