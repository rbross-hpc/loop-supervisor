# Run options are immutable and persisted; resume validates exact Git checkpoints

## Status

Accepted

## Context

The initial resumable-state implementation (schema v1) persisted phase
and role results, but not the run's own configuration: limits,
worktree root, decision-approval policy, and the OpenCode
executable/timeout were all supplied fresh on every `loop-supervisor
resume` invocation. That meant a run started with, say,
`--max-revisions 1` could resume with the default of `5` simply because
the operator forgot to repeat the flag — silently changing the run's
safety limits mid-flight.

Resume validation was also shallow: it checked that the recorded common
Git directory, integration path, and integration branch matched, and
that a recorded task worktree path merely *existed* on disk. It did not
check that the path was still a real, registered Git worktree, that it
was on the expected branch, that its `HEAD` matched what the supervisor
last observed, or that the integration branch hadn't been rewound or
rewritten since the run paused. A resumed run could therefore proceed
against a task worktree or integration branch that had silently changed
underneath it.

## Decision

- `RunState` (schema v2) persists an immutable `RunOptions` — every
  behavior-affecting setting captured once at `start_new_run()`: task,
  revision, replan, and architect-retry limits; malformed-output
  retries; role timeout; worktree root; decision-approval policy; and
  the OpenCode executable/startup timeout. `resume` reconstructs the
  supervisor entirely from these persisted options. The `resume` CLI
  subcommand does not accept any run-behavior flags at all — only
  `--project` and the run ID — so there is no way to accidentally
  override a running task's limits from the command line.
- `RunState` also persists Git checkpoints refreshed on every phase
  transition: the integration branch's expected `HEAD` and a full
  working-tree status snapshot, and, whenever a task worktree is active,
  its expected `HEAD` and status snapshot too.
- Resuming validates, before starting any OpenCode process:
  - the integration worktree's common dir, path, and branch match;
  - the integration worktree is clean, and its current `HEAD` is either
    exactly the recorded checkpoint or a clean descendant of it (an
    unrelated commit landing on the integration branch between pause and
    resume is tolerated; a rewind or history rewrite is not);
  - if a task is active, its worktree is a real worktree registered with
    Git, checked out on the expected branch, with the branch ref and
    worktree `HEAD` agreeing with each other and with the last recorded
    checkpoint, its base commit still existing and still an ancestor,
    and its working-tree status snapshot matching exactly (this
    intentionally allows a builder `BLOCKED`/`INCOMPLETE` pause to leave
    the worktree dirty — the point is detecting *unexpected* change,
    not requiring cleanliness).
- Schema v1 state cannot satisfy any of this (no persisted options, no
  checkpoints) and is rejected outright with an explicit error rather
  than silently migrated or resumed with reconstructed defaults.

## Consequences

- A run's safety limits, worktree location, and approval policy cannot
  drift between `run` and `resume`, or between successive `resume`
  invocations.
- Resume fails closed — with an actionable error — on integration or
  task state that changed unexpectedly while paused, instead of
  proceeding against a worktree or branch that no longer matches what
  the supervisor last verified.
- Existing schema-v1 runs cannot be resumed after this change; operators
  must complete or abandon them before upgrading, then start new runs.
