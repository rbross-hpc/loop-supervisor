# Allow downstream roles to restate the objective in their own words

## Status

Accepted

## Context

`check_task_identity()` guards against a downstream role (builder,
auditor) silently drifting onto a different task than the one the
planner defined. Since its introduction (see ADR 0001, "Resolved
item 1: ContractError durability") it checked two fields for exact
equality against the planner's `PlannerResult`: `task_id` and
`objective`.

Running the loop against a real project (`test-run`, run
`2dba05654b5e`, task `task-002`), the auditor
(`argo/GPT-5.6 Sol`) was rejected with a durable `operational_failure`
even though its `task_id` matched exactly and its review was
substantively good (it correctly identified two real defects in the
builder's parser). The rejection was purely textual:

- Planner's `objective`:
  > "Implement the generic logfmt LogRecord parsing layer (module +
  > tests) that turns OpenCode log lines into typed records, per ADR
  > 0001, without touching the query/subcommand layer."
- Auditor's `objective`:
  > "Implement a generic logfmt LogRecord parser with malformed-line
  > accounting and tests."

The auditor's sentence is an accurate summary of the same task, not a
different task. No agent prompt (planner, builder, auditor, or
architect) instructs the model to echo the objective verbatim; the
field appears in each prompt's JSON schema example only, never with
an instruction to copy it. Exact-string equality therefore encoded a
contract no prompt actually establishes, and rejected honest,
correct paraphrasing as if it were drift.

Two narrower fixes were considered and rejected:

- **(A) Drop the objective check entirely.** This loses the ability
  to catch a role that returns a well-formed but empty or missing
  objective, which is a real (if weaker) signal of drift or a broken
  response.
- **(B) Normalize before comparing** (e.g. case-fold, strip
  punctuation, whitespace-collapse). This does not fix the actual
  problem: the live failure was a genuine paraphrase, not a
  formatting difference, and no normalization scheme closes that gap
  without effectively becoming a semantic-similarity check, which is
  out of scope for a structural contract layer.

`check_decision_answered()` (the analogous check for the architect's
`decision_question`) is deliberately left untouched by this decision.
ADR 0003 makes exact-question matching there load-bearing: the
architect is answering a single, specific question posed by the
supervisor, and there is no `task_id`-equivalent field carrying
identity independently of the question text. The objective and the
decision question are not the same kind of field.

## Decision

In `check_task_identity()`:

- Keep `task_id` as an exact-match requirement. It is the only field
  that reliably carries task identity across roles, and a mismatch
  there means the role is reporting on the wrong task outright.
- Replace exact-match on `objective` with a presence check: the
  downstream role's `objective` must be non-empty after stripping
  whitespace. It is no longer compared against the planner's
  `objective` at all.

No change to any agent prompt: none of them asked for verbatim
objective echo, so there is nothing to relax there.

## Consequences

- A downstream role that paraphrases, summarizes, or otherwise
  restates the objective in different words no longer triggers a
  durable `operational_failure`.
- `task_id` mismatches are still caught exactly as before — this
  decision does not weaken task-identity protection, only removes an
  unintended verbatim-text requirement layered on top of it.
- A role that returns a well-formed result with a blank or
  whitespace-only `objective` is still rejected, preserving the
  original check's ability to catch a dropped or empty field.
- `check_decision_answered()` is unaffected: exact-question matching
  there remains load-bearing per ADR 0003.
