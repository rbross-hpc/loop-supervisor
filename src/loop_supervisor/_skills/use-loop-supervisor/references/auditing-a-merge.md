# Auditing what the loop merged

The supervisor is the only thing that merges into the integration
branch — the builder's own permissions deny `git merge*`/`git push*`.
That merge having happened does not mean it deserves to be pushed
unreviewed; treat it the way you would a human contributor's PR before
`git push`.

## Merge shape

```bash
git log --format="%P" -1 <merge-commit>   # must print two hashes
git diff <merged-branch> main --stat      # must be empty
```

Two parents confirms a real merge (not a fast-forward masquerading as
one); an empty diff against the branch confirms nothing was lost or
added outside the merge itself.

## Commit quality

- Every commit should have a body, not just a subject line — what
  changed, why, and the failing-first verification evidence (which
  prior commit was restored, what failed, why that failure was the
  right one).
- `git log <base>..<merge-commit>` to see every commit the loop
  produced for this task, not just the last one — a REVISE cycle
  produces more than one.
- The merge commit's own subject reads `Merge commit '<sha>'`, not
  `Merge branch '<name>'` — this is deliberate (the supervisor merges
  an immutable, checkpointed commit SHA rather than a mutable branch
  name, for crash-safety), not something to "fix."

## Test and assertion integrity

```bash
git diff <base>..<merge-commit> -- tests/ | grep "^-" | grep -i assert
```

Should normally be empty. A new test replacing an old one is fine; an
assertion quietly weakened or removed without explanation in the
commit message is worth investigating before trusting the result.

## Re-run the gates on the merged result yourself

Passing gates inside the task worktree before merge does not
guarantee the same is true of the merged integration branch — verify
independently:

```bash
git checkout main   # or wherever the merge landed
<your project's lint/format/typecheck commands>
<your project's full test suite>
```

If the project has a `loop-supervisor.toml` `[verify]` section, those
are the same commands the supervisor itself already ran against the
task's commit — rerunning them on the merged integration branch is
still worth doing, since the two are not always guaranteed to be
identical (see the project's own backlog for whether `[provision]`/
`[verify]` config applies uniformly across `run`/`resume`).

## Spot-check the failing-first claim

Pick at least one behavioral claim from the commit message and
actually verify it, rather than trusting the description at face
value — restore the prior version of the touched file(s) (`git show
<commit-before-the-fix>:path > path`), confirm the new test fails for
the reason claimed, then restore the merged version. This is the same
discipline the project's own auditor role is expected to apply, and
catches the class of defect a "looks reasonable" read misses (e.g. a
test that trivially fails on any code, or a historical-defect probe
that never actually reaches its own behavioral assertion).
