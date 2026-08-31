# Supervisor-run verification is a finding for the auditor, not an operational fault

## Status

Accepted. Its "where full command output lives" and
"no stop-on-first-failure" paragraphs below are superseded by [ADR
0027](0027-verification-logs-live-under-git-common-dir-not-the-worktree.md):
verification logs write under git-common-dir rather than the task
worktree, and `_do_verifying` now actually passes
`stop_on_failure=False` as this ADR always said it did. Everything
else here (the `verifying` phase, the fault/finding policy, the
prompt changes) is unaffected. Kept for historical context.

## Context

The auditor's permission block has always granted `pytest`, `ruff`,
and `mypy` (`"pytest *": allow`, etc.), but its prompt body never
actually instructed it to run anything -- the only suggested commands
`_build_auditor_prompt` ever emitted were two `git` invocations. ADR
0014 documented the consequence directly: in a real run, "both
audits completed so far in the `test-run` run never actually executed
their verification commands, so their ACCEPT dispositions rest on
static inspection alone," and promised a backlog item on exactly this
that was never filed (see backlog item 46, now resolved by this
change). Separately, `BuilderResult.tests_run`/`test_results` were
already collected and persisted but never shown to the auditor at
all -- the auditor saw neither its own verification nor the builder's
claim about testing.

`loop-supervisor.toml`'s `[verify]` table (ADR 0025) gives a project a
place to configure test/lint/typecheck commands; this ADR covers how
the supervisor uses them once configured.

Two placements were considered for running these commands:

1. **Inline at the top of `_do_auditing`.** Smallest change, no new
   phase vocabulary. Rejected: `auditing` would gain an expensive,
   non-idempotent-feeling side effect, and an operational failure
   discovered later in the same phase (e.g. a malformed auditor
   response after retries) would re-run the entire verification suite
   on resume, for no benefit.
2. **A new `verifying` phase between `building` and `auditing`,**
   entered only when `verify_commands` is configured; otherwise
   `building` routes straight to `auditing` exactly as before this
   change. Chosen: this matches the codebase's revealed preference
   for splitting expensive or logically-distinct steps into their own
   durable phase (merging/cleanup_worktree/cleanup_branch were each
   split for independent retryability), gives the TUI/CLI a real,
   visible progress state, and lets the result persist in `RunState`
   so an auditor retry never re-runs the suite.

Two policies also needed deciding:

- **What a failing verification command means.** It is not treated as
  an operational fault (the phase never raises on a command's own
  nonzero exit or timeout) -- a failing test suite is exactly the kind
  of finding the auditor exists to weigh, not evidence the supervisor
  itself malfunctioned. A task's acceptance criteria might not even
  touch the failing area (a pre-existing, unrelated failure), so an
  automatic REVISE/block was rejected in favor of handing the auditor
  the result and trusting its judgment, consistent with the auditor
  being "the single decision point" for task outcomes elsewhere in
  this design.
- **Where full command output lives.** Inline in the prompt was
  rejected for anything beyond a short summary: a full `pytest -q`
  failure dump can be tens of KB, which would crowd the prompt for a
  case where the auditor may only need a specific failing test's
  name. Full stdout/stderr is written to a file under the task
  worktree's `.loop-supervisor/verification/` (already reserved,
  gitignored, per `_skeleton/.gitignore`) with the prompt citing each
  file's path -- giving the auditor both a scannable summary and a
  way to read the exact text if the summary isn't enough.

## Decision

`PHASE_VERIFYING` (`phases.py`) sits between `building` and
`auditing`. It is not a member of `DURABLE_SIDE_EFFECT_PHASES`: it has
no Git side effect and is safe to re-run to an equivalent result, so
it carries none of `creating_worktree`'s crash-reconciliation
concerns.

`_do_verifying` runs `RunOptions.verify_commands` via
`commands.run_commands` (the same non-`shell=True`, mandatory-timeout
runner ADR 0025 introduced for provisioning), using
`build_agent_env(worktree.path)` so `PATH` resolves the same way it
already does for every other in-worktree command. It always continues
to every configured command (no stop-on-first-failure, unlike
provisioning: a lint failure should not hide a downstream test
failure the auditor also needs to see) and never raises.

The result is summarized by `_summarize_verification` into
`RunState.verification_result`: overall `ok`, and per command
`{command, ok, returncode, timed_out, duration, output_path,
summary}`, where `summary` is a redacted, truncated
(`_truncate_message`/`_redact_secrets`, the same helpers used for
`OperationalErrorRecord.message`) combination of stdout+stderr, and
`output_path` points at the full, untruncated log file relative to
the worktree root.

`_build_auditor_prompt` (converted from a single static expression to
the `lines`-accumulator style its sibling prompt builders already
use) renders, when `verification_result` is present: an explicit
statement of whether every command succeeded, per-command
status/output-path/summary lines, and -- separately -- the builder's
own self-reported `tests_run`/`test_results`, explicitly labeled as
an unverified claim to weigh against the independently-run result
when both are present. When no verification is configured, the
prompt is unchanged from before this feature existed.

`AuditorResult`'s schema is unchanged: the auditor's disposition
vocabulary (ACCEPT/REVISE/REPLAN) already covers every outcome a
verification finding could justify, so no new field was needed --
avoiding `StrictModel`'s `extra="forbid"` lockstep problem, where a
schema change requires updating `contracts.py` and both copies of
`loop-auditor.md` in the same change or every audit hard-fails.

`RunState.verification_result` is cleared alongside `builder_result`
at every point the existing code already clears it: on REPLAN, on the
BLOCKED-then-"replan"-answer path, and at task-boundary cleanup
(`_finish_task_cleanup`) -- and additionally on REVISE, where it was
not previously cleared for `builder_result` at all, since a REVISE
keeps the same task but discards the verified commit: the next
builder attempt produces a new commit that the stale verification
result would otherwise misrepresent.

Both `loop-auditor.md` files (live and skeleton) now say that a
"Verification" section in the prompt means results are already
supplied and do not need to be re-run, while a failing command "is
not automatically disqualifying" and should be weighed against
acceptance criteria. The `pytest`/`ruff`/`mypy` permission grants are
kept unchanged, so the auditor can still investigate a specific
finding if the summary and log file aren't enough.

## Consequences

- A project with no `loop-supervisor.toml` (or one with an empty
  `[verify]` table) sees no behavior change at all: `building` routes
  straight to `auditing`, `verification_result` stays `None`, and the
  auditor prompt is byte-for-byte what it was before this ADR.
- `RunState` gained one field (`verification_result`), which needed no
  schema migration because ADR 0024 (squashing the schema to a single
  current version) had just landed for this exact reason.
- The auditor no longer needs to discover `pytest`/`ruff`/`mypy` by
  globbing when verification is configured -- the class of failure
  ADR 0014 and backlog items 25/27/44 all trace back to (an auditor
  reaching outside its worktree looking for tools it can't find) has
  one fewer trigger, though the permission grants remain for the case
  where it still wants to investigate directly.
- `_build_auditor_prompt` has real test coverage for the first time
  (`tests/test_prompts.py`); it previously had none.
</content>
