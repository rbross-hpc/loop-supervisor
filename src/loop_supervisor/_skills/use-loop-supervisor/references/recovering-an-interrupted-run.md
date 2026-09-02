# Recovering an interrupted run

A `loop-supervisor` process that dies mid-phase (killed by a harness
timeout, `^C`, an OOM, a host restart) is often — but not always —
cleanly resumable. The distinction that matters is **whether the task
worktree's `HEAD` moved** since the last checkpoint, not merely
whether it has uncommitted changes.

## First: try resuming normally

```bash
loop-supervisor resume <run_id> --project .
```

If this succeeds, you're done — nothing below applies.

## If it fails with "resume task worktree has changed since it was paused"

This is `validate_task_worktree`'s checkpoint comparison failing
closed, exactly as designed — it means the task worktree's `HEAD` or
working-tree status no longer matches what was recorded the last time
the supervisor saved state. **Do not** treat this as automatically
unrecoverable, and do not immediately reach for `git checkout --` or
`git clean` without first checking which of the two cases below you're
in — one is safe, the other discards a paused BLOCKED/INCOMPLETE
state.

### Case A: uncommitted edits, `HEAD` unchanged (recoverable)

This is what happens when a builder invocation was killed mid-edit,
before it committed: the checkpoint recorded a clean tree at some
commit, and the tree is now dirty at that same commit.

```bash
cd <task-worktree-path>
git log --oneline -1        # confirm HEAD matches task_expected_head
                             # in the run-state JSON (see observing-a-run.md)
git status --porcelain      # see what's uncommitted
```

If you want to inspect the interrupted work before discarding it (it
is sometimes already correct and fully tested — worth a quick
`pytest`/lint pass before throwing it away, since redoing it costs a
full agent invocation), do that now. Then restore the worktree to
its checkpoint and resume:

```bash
git checkout -- .
git clean -fd
git status --porcelain      # must be empty
cd <integration-checkout>
loop-supervisor resume <run_id> --project .
```

Resuming re-enters the same phase the process was killed in (usually
`building`), which re-invokes the same role from scratch. This is
expected and safe — it is exactly the same path a builder `REVISE`
takes, and it may produce a different (but equally valid) fix than the
one you discarded.

### Case B: `HEAD` moved (not recoverable this way)

If the task branch's `HEAD` no longer matches `task_expected_head` in
the run-state JSON — e.g. because something committed to that branch
after the checkpoint was taken — there is no supported way to
reconcile this. The recorded checkpoint and the actual worktree
disagree about a fact (which commit is current) that the supervisor
has no principled way to resolve on your behalf. Do not force it by
hand-editing the run-state JSON's `task_expected_head`/
`task_status_snapshot` fields to match current reality — those strict
validators exist specifically to prevent a corrupted or tampered
checkpoint from being silently accepted, and "I know what I'm doing"
is exactly the situation they're designed to catch.

In this case, decide whether to abandon the run (the task
worktree/branch survive for manual inspection even from a terminal
`failed` state — nothing is deleted) or salvage the work by hand and
start a fresh run.

## What NOT to do

- Do not run `git checkout --`/`git clean` in the task worktree before
  confirming which case you're in (Case A vs. B above) — in Case B it
  won't help, and in either case it is irreversible for whatever it
  discards.
- Do not hand-edit the persisted run-state JSON to force a phase
  transition or "fix" a checkpoint mismatch. `RunState`'s validators
  are deliberately strict and exact-field-matching; a hand-edit that
  gets any field even slightly wrong can produce a state that is
  *rejected on the next load* (a worse outcome than the original
  interruption) or, worse, one that is silently accepted but no longer
  accurately describes reality.
