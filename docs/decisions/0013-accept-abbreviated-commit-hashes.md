# Accept unambiguous abbreviated commit hashes from the builder

## Status

Accepted

## Context

`verify_builder_commit()` checked the builder's reported `commit` field
against the task worktree's actual `HEAD` with exact string equality.

Running the loop against a real project (`test-run`, run
`2dba05654b5e`), the builder (`argo/Claude Sonnet 5`) twice reported a
7-character abbreviated hash (e.g. `7b7a0a3`) instead of the full
40-character SHA, even though it had genuinely committed the work.
Exact-string comparison against the full `HEAD` hash rejected this as
a durable `GitError` operational failure both times, even though the
commit itself was correct and safely present on the branch. Retrying
the phase caused the builder to re-report on the same commit, this
time with the full hash, so the failure cost a full role invocation
and a resume cycle without ever reflecting a real problem.

The `loop-builder` prompt's own schema example was also complicit:
`"commit": "abcdef123456"` is 12 characters, not 40, and nothing in
the surrounding instructions said "report the full hash." A model
following the example literally would reasonably produce something
short.

Two other commit-adjacent failure modes already existed and are
explicitly preserved by this decision, not relaxed:

- A revspec such as `HEAD`, a branch name, or `HEAD~1` would, if
  naively passed to `git rev-parse`, resolve to *something* — but
  accepting it would mean the check is no longer verifying that the
  builder actually knows and reports a specific commit, just that the
  worktree is in some valid state. This defeats the purpose of the
  check.
- An abbreviation that resolves to a real, existing commit that is
  *not* actually `HEAD` (e.g. the base commit, or an earlier commit on
  the same branch) must still be rejected as a mismatch.

## Decision

`verify_builder_commit()` now:

1. Rejects `reported_commit` outright, without ever calling Git, if it
   is not a bare hex string of 7-40 characters (matching Git's own
   default `core.abbrev` floor of 7). This is what keeps revspecs like
   `HEAD` or a branch name out: they never reach step 2, regardless of
   whether Git could otherwise resolve them.
2. Resolves the value in the task worktree via
   `git rev-parse --verify --end-of-options <value>^{commit}`,
   which fails closed on anything ambiguous or unresolvable.
3. Compares the *resolved* hash against the actual `HEAD`, not the
   original reported string, and rejects a mismatch exactly as before.
4. Always returns the full, resolved `HEAD` hash — `state.last_task_head`
   is therefore always a canonical 40-character hash regardless of what
   the builder reported, so nothing downstream needs to know an
   abbreviation was ever involved.

The supervisor's call site (`_do_building`) also now raises a `LoopError`
naming the actual defect if a `COMPLETE` builder result somehow carries
a falsy `commit` — `BuilderResult`'s own contract validator already
requires a non-empty commit for `COMPLETE`, so this is defense-in-depth
against a hand-edited or corrupted state replay, not a path reachable
through normal contract validation.

The `loop-builder` prompt's schema example was corrected to a real
40-character hash, and an explicit instruction was added to report the
full hash (e.g. from `git rev-parse HEAD`), not an abbreviation —
abbreviations are now *accepted*, not *requested*.

## Consequences

- A builder reporting a 7+ character unambiguous abbreviation of the
  real `HEAD` no longer triggers a false-positive operational failure.
- `state.last_task_head`, `state.merge_task_head`, and everything
  derived from them remain full 40-character hashes; no downstream
  code needed to change.
- Revspecs (`HEAD`, branch names, `HEAD~N`) are still rejected
  categorically, before any Git call, preserving the check's actual
  purpose: proving the builder knows a specific commit, not merely
  that the worktree is in *some* valid state.
- An abbreviation of a real but non-`HEAD` commit is still rejected as
  a mismatch, exactly as a full wrong hash always was.
- The prompt still asks for the full hash; the resolver's tolerance is
  a safety net for when a model doesn't follow that instruction, the
  same relationship ADR 0010 established between prompt instructions
  and parser tolerance for JSON preambles.
