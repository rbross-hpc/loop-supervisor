# Verification log directory is keyed by commit, not run_id alone

## Status

Accepted. Supersedes the "Verification logs move to
`<git_common_dir>/loop-supervisor/verification/<run_id>/`" paragraph
of [ADR
0027](0027-verification-logs-live-under-git-common-dir-not-the-worktree.md)
and the `0o700`-only claim in its following paragraph. Everything else
in ADR 0027 (the move out of the task worktree, the reasoning for why
that is safe, the read-access analysis) is unaffected and remains as
decided there.

## Context

An independent audit of the merged `fix/verification-output-and-
sequencing` branch found that ADR 0027's chosen key -- `run_id` alone
-- was still wrong, in a way none of that branch's new tests exercised
because they each drove exactly one task through exactly one
verification. A single run accepts up to `max_accepted_tasks` (default
20) tasks in sequence, all sharing one `run_id`; a `REVISE` disposition
also re-verifies a new commit within the same task. Every one of these
re-entries into `_do_verifying` wrote into the *same* directory,
naming each command's log by position (`01.log`, `02.log`, ...).

A later verification with fewer commands than an earlier one only
partially overwrote that directory. Reproduced directly: task 1 runs
two commands where the second fails, producing `01.log` (pass) and
`02.log` (the real failure); task 2 -- a different, later task in the
same run -- runs one passing command, overwriting only `01.log`.
`02.log`, task 1's genuine failure, survives untouched, sitting next
to task 2's passing `01.log` in the same directory the auditor is
told to trust and has permission to browse. The auditor's prompt for
task 2 correctly cites only `01.log`, but nothing stops it (or a
human) from finding the orphaned `02.log` and misattributing it.

The `0o700`-directory-only claim in ADR 0027 was also inaccurate: only
the leaf `<run_id>/` directory got that mode, while the log *files*
inherited the default `0o644` -- looser than `save_state`'s `0o600`
for run-state JSON, despite the ADR's text claiming parity.

## Decision

`_verification_log_dir` now takes the verified commit as a third
argument and returns `<git_common_dir>/loop-supervisor/verification/
<run_id>/<commit>/`. `_do_verifying` resolves `worktree`'s current
HEAD (`self.repo.head_commit(cwd=worktree.path)`) before running any
verify command and passes it through. Every verification run targets
a single, immutable commit -- the exact one the auditor's prompt
already cites as "against this exact commit" -- so keying on it gives
each verification attempt (each task, and each `REVISE` re-attempt
within a task) its own directory with no collision, and requires no
new counter field in `RunState`: the commit is already computed for
this purpose and is itself a stable, unique identifier per attempt.

The commit is validated with a new `_validate_commit_sha` (a bare hex
regex, `^[0-9a-fA-F]{7,64}$`) before being used as a path component,
matching `validate_run_id`'s existing traversal-guard pattern for
`run_id`. Since the commit is always the supervisor's own
`head_commit()` result rather than external input, this is a defense-
in-depth measure rather than a response to a concrete threat, but it
keeps `_verification_log_dir`'s two path components validated by the
same discipline.

Log files are now `chmod`'d to `0o600` individually (the directory
remains `0o700`), so the actual on-disk posture now matches the
parity ADR 0027 claimed for it.

## Consequences

- Verification logs are now uniquely addressed per (run, commit),
  never overwritten or shadowed by a different task's or attempt's
  logs within the same run. A new regression test drives two accepted
  tasks through one run with different verify-command counts and
  asserts the first task's logs are untouched after the second task's
  verification completes.
- A `REVISE` cycle that re-verifies the same task's new commit after a
  builder fix now also gets its own directory (a new commit implies a
  new key), rather than reusing -- and potentially colliding with --
  the discarded attempt's directory. This is a strict improvement:
  the discarded attempt's log is preserved rather than partially
  overwritten, at the cost of one more small directory per revision,
  which is within the scope of backlog item 47's already-tracked
  unbounded-retention concern rather than a new one.
- `_do_verifying` now calls `head_commit()` once before running verify
  commands; this is a fast, local `git rev-parse` against a worktree
  that is not being mutated by anything else at that point in the
  phase, so it adds no meaningful latency or race window.
</content>
