# Sibling Git worktrees per task, with supervisor-owned merge and REPLAN continuity

## Status

Accepted

## Context

The builder needs an isolated place to make commits without touching the
integration branch directly, so a rejected or in-progress task can never
corrupt the branch other work depends on. At the same time, `REPLAN`
happens when the auditor decides the *plan* was wrong, not necessarily
that all the builder's work was wasted — discarding a preserved worktree
on every `REPLAN` would throw away legitimate intermediate progress for
no reason.

## Decision

- Each task gets its own Git worktree, created as a sibling directory one
  level above the integration checkout: `<parent>/<repo>-<task-id>`, on
  branch `loop/<task-id>`. `<task-id>` here is always the *original*
  planner `task_id` for that unit of work.
- The supervisor creates the worktree (from current integration `HEAD`)
  before invoking the builder, and is the only actor that removes it or
  merges its branch.
- On `ACCEPT`: supervisor performs `git merge --no-ff` into the
  integration branch, allowing the integration branch to have advanced
  in the meantime (no forced fast-forward). Then the worktree and branch
  are removed.
- On merge conflict: the supervisor aborts the merge (leaving the
  integration worktree clean), preserves the task worktree/branch for
  diagnosis, and stops the run rather than attempting any automatic
  resolution.
- On `REPLAN`: the task worktree and branch are preserved. The planner
  runs again with the same worktree as its context and continues from
  the intermediate commits already there — it does not start over from
  integration `HEAD`. This is why the worktree/branch name is pinned to
  the *original* task ID even if the planner's replanned task gets a
  different logical `task_id`.
- On builder `BLOCKED`/`INCOMPLETE`: same preservation — the supervisor
  collects operator guidance and retries the builder on the same
  worktree.
- Before every builder `COMPLETE` is trusted, the supervisor independently
  verifies actual Git state (branch, clean tree, `HEAD`, and that new
  commits exist since the worktree's base) rather than trusting the
  builder's self-report.

## Consequences

- A `REPLAN` cycle does not lose builder work; it gives the planner a
  chance to redirect an already-started task rather than discarding
  effort.
- The branch/path naming rule (stable original task ID) means a
  discrepancy between "current logical task_id" and "branch name" is
  expected and must not be treated as an error by tooling that inspects
  branches.
- Conflict handling is intentionally conservative: a human must resolve
  conflicts by hand. This avoids ever having the supervisor guess how to
  reconcile diverged history.
- No parallel task worktrees are supported yet; only one task is active
  per run.
