# Recovering a merge-conflict operational failure

The supervisor is the sole writer of the integration branch during a
normal run, so a merge conflict cannot arise from the loop's own
operation alone — it means something external changed the integration
branch while the run was in progress (a manual commit, or a second
supervisor run pointed at the same repository). This reference is
specifically about that failure. For a killed/crashed process instead,
see `recovering-an-interrupted-run.md`.

## Confirm this is the right recovery path

Read (never edit) the run's persisted state:

```
<git-common-dir>/loop-supervisor/runs/<run-id>.json
```

You're in this case when:

- `phase` is `operational_failure`
- `last_error.kind` is `merge_conflict`
- `last_error.retry_phase` is `merging`
- `last_error.requires_repair` is `true`

Also note `merge_pre_head` and `merge_task_head` — both are on the
top-level state object. These are immutable intent captured at the
moment the auditor accepted the task: `merge_pre_head` is the
integration branch's `HEAD` just before the merge was attempted;
`merge_task_head` is the exact, already-reviewed task commit. Resume
merges `merge_task_head` again — never the mutable task branch name,
which may have moved since.

## Why "resolve the conflict" isn't enough by itself

When the merge failed, the supervisor ran `git merge --abort` and
restored the integration worktree to its pre-merge state. There are no
conflict markers waiting in the integration worktree to resolve — you
have to recreate the merge yourself.

## The repair recipe

1. **Do not switch what's checked out** in the integration worktree —
   `resume` requires the same branch that was checked out when the run
   started.

2. In the integration worktree, start the merge again, using the exact
   commit SHA, not the task branch name:

   ```bash
   cd <integration-checkout>
   git merge --no-ff --no-commit <merge_task_head>
   ```

3. Resolve the conflicts in the integration worktree, same as any
   normal merge conflict:

   ```bash
   git status --porcelain            # see conflicted paths
   # edit the conflicted files
   git add <resolved-paths>
   ```

4. Commit the merge. Do not abort and create an ordinary one-parent
   commit instead — it must be a real merge commit:

   ```bash
   git commit
   ```

5. Verify the result before resuming:

   ```bash
   git rev-parse HEAD^1        # must equal merge_pre_head (or a clean
                                # descendant of it, if the integration
                                # branch has since advanced further)
   git rev-parse HEAD^2        # must equal merge_task_head exactly
   git status --porcelain      # must be empty
   ```

6. Resume from the integration checkout:

   ```bash
   loop-supervisor resume <run-id> --project .
   ```

   Resume looks for a merge commit on the integration branch's
   first-parent chain (after `merge_pre_head`) whose **second parent is
   exactly `merge_task_head`**. If it finds the commit you just made,
   it records it and proceeds directly to cleanup — it does not merge
   again.

## What will NOT satisfy resume

Do not substitute any of these for step 2-4 above; each fails the
exact-second-parent check and leaves the run unresumable that way:

- fast-forwarding the integration branch to the task branch/commit;
- squash-merging (a squash commit has one parent, not two);
- cherry-picking the task's commits instead of merging them;
- rebasing either branch;
- merging the task branch **name** after it has moved past
  `merge_task_head` (always merge the SHA);
- manually editing the persisted run-state JSON to invent a
  `merge_commit` — its consistency is strictly validated on load and a
  fabricated value that doesn't match real Git state will be rejected
  or, worse, silently trusted for a cleanup step that then deletes the
  wrong thing.

## If you can't safely resolve it

The task worktree and branch are untouched by the failed merge attempt
— nothing is lost. You can abandon the run (the worktree/branch remain
for manual inspection) and salvage the reviewed commit by hand, or
continue investigating before attempting the repair above.
