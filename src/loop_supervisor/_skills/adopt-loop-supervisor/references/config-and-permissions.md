# Two config gotchas that cause a silent or confusing failure

Both of these produce a run that hangs, gets permission-denied with no
obvious cause, or resumes into an error that looks unrelated to its
actual cause. Neither is obvious from reading `opencode.json` in
isolation, and both have been hit in practice.

## 1. `external_directory` must allow the parent, not just the project root

`loop-supervisor` creates task worktrees as **siblings** of the
integration project root by default — one directory level up, not
nested inside it. An agent working inside a task worktree that needs
to read anything outside its own worktree (a sibling task's diff, a
file under the integration root, a log file elsewhere on disk) needs
`external_directory` to allow the **parent** directory of the project
root, not the project root itself.

Given a project at `/home/user/my-project`, the permission block
should allow `/home/user`, not (only) `/home/user/my-project`:

```json
"permission": {
  "external_directory": {
    "*": "deny",
    "/home/user": "allow"
  }
}
```

`loop-supervisor init` computes this correctly automatically (it
allows the destination's parent); the mistake happens when someone
edits this block by hand afterward and narrows it back down to "just
the project itself" because that looks more locked-down. Run
`loop-supervisor config validate` after any manual edit to this block
— it specifically checks for this.

## 2. Config changes must be committed before a worktree exists

OpenCode resolves `opencode.json` from **the invocation's own working
directory**, not from wherever the supervisor's server process
started. For the planner/architect roles this is the integration root
and rarely matters. For the builder and auditor, each task runs inside
its own task worktree — a separate git checkout with its own copy of
every tracked file, including `opencode.json`.

This means: if you fix `opencode.json` in the integration root
*after* a task worktree has already been created (for example, while
debugging a stuck run), that worktree's own copy of `opencode.json` is
still whatever it was checked out with, and the fix does not apply to
it. The builder/auditor invocations running in that worktree will keep
using the stale config until the worktree itself gets the fix — which
means committing the same config change again, inside the task
worktree, on the task's own branch.

**Practical implication:** get `opencode.json` and permissions right
*before* starting the first run, not after. If you do need to fix
config mid-run because a task worktree already exists:

1. Fix `opencode.json` in the integration root and commit it there.
2. Also apply the identical fix inside the affected task worktree
   (`cd` into it, edit, commit on its own branch).
3. If the worktree's `HEAD` moves as a result, resume may reject it
   with "resume task worktree has changed since it was paused" —
   this is loop-supervisor correctly detecting the worktree changed
   out from under the recorded run state, not a bug. There is no
   supported "reconcile expected head" command; treat step 2 above as
   something you should avoid needing by getting config right at
   adoption time in the first place.

Checking `loop-supervisor config validate` at both step 0 and step 5
of the main workflow — before and after writing config — is what
catches most instances of both gotchas before a run ever starts.
