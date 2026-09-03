# Two config gotchas that cause a silent or confusing failure

Both of these produce a run that hangs, gets permission-denied with no
obvious cause, or resumes into an error that looks unrelated to its
actual cause. Neither is obvious from reading `opencode.json` in
isolation, and both have been hit in practice.

## 1. `external_directory` must allow the parent and its subtree

`loop-supervisor` creates task worktrees as **siblings** of the
integration project root by default — one directory level up, not
nested inside it. An agent working inside a task worktree that needs
to read anything outside its own worktree (a sibling task's diff, a
file under the integration root, a log file elsewhere on disk) needs
`external_directory` to allow both the **parent** directory itself and
paths below it. OpenCode path patterns match strings: allowing only the
exact parent does not match files inside sibling task worktrees.

Given a project at `/home/user/my-project`, the permission block
should allow both `/home/user` and `/home/user/**`, not only the project
root or only the exact parent:

```json
"permission": {
  "external_directory": {
    "*": "deny",
    "/home/user": "allow",
    "/home/user/**": "allow"
  }
}
```

Order matters: OpenCode uses the **last matching rule**, so the broad
`"*": "deny"` must precede both specific allows. `loop-supervisor init`
computes these entries automatically. Run `loop-supervisor config
validate` after any manual edit — it checks both the parent itself and
a representative descendant path.

## 2. `opencode.json` need not be tracked, but must be gitignored if you don't track it

The supervisor sets `OPENCODE_CONFIG` to the integration root's
`opencode.json` for every agent invocation, so a builder or auditor
running inside a task worktree resolves that one file by absolute path
rather than the worktree's own checked-out copy (ADR 0032). Two
consequences follow:

- **A config fix at the integration root reaches in-flight worktrees
  automatically.** You do not need to re-apply and re-commit the same
  edit inside each task worktree — the old "commit config before a
  worktree exists" trap is gone. (This was previously a real failure
  mode: OpenCode used to load each worktree's own copy, so a mid-run
  fix at the root silently didn't apply.)
- **Whether `opencode.json` is version-controlled is your choice.** It
  often holds environment-specific provider, model, or MCP settings a
  project may not want committed. Resolution no longer depends on git
  carrying the file into worktrees.

If you choose **not** to track `opencode.json`, add it to `.gitignore`
rather than leaving it merely untracked. The supervisor's cleanliness
gates and `loop-supervisor config validate` both use `git status
--porcelain`, which lists untracked-but-not-ignored files — so an
untracked, unignored `opencode.json` reads as a dirty working tree and
will block a run. A gitignored file does not appear there.

Run `loop-supervisor config validate` before the first run; it checks
that `opencode.json` exists on disk and that its `external_directory`
scoping is correct, regardless of the file's tracking status.
