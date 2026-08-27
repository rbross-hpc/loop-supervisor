# Route auditor findings to the builder alongside required_changes on REVISE

## Status

Accepted

## Context

`AuditorResult` carries three free-text list fields describing the
audit: `findings` (what was observed, including diagnosis and
reproducing examples), `required_changes` (the imperative,
must-fix action list), and `design_observations` (critique of the
task's own acceptance criteria — the auditor's designated channel for
"I think the plan itself was wrong," which the auditor is told to
raise there and prefer REPLAN over silently expanding scope through
REVISE for; see the `loop-auditor` prompt and the REVISE-vs-REPLAN
guidance it contains).

Before this decision, only `required_changes` crossed into the next
role's prompt, and only on the REVISE path: `_do_building` read
`auditor.required_changes` and `_build_builder_prompt` rendered it
under "The auditor requested these changes on your previous attempt."
`findings` and `design_observations` were computed by the auditor but
then discarded — never read by any downstream `_do_*`/`_build_*`
function on that path.

Running the loop against a real project (`test-run`, run
`2dba05654b5e`, task `task-002`), the auditor returned REVISE with a
`required_changes` entry ("reject unterminated quoted values and
non-key/value tokens") whose only concrete reproducing examples
(`timestamp=t level=INFO broken`, a literal unterminated `message="`
value) lived exclusively in `findings`. The builder revising the code
would have had to independently rediscover those exact failing inputs
that the auditor had already found, or risk fixing a narrower case
than the one actually observed.

Nothing in the `loop-auditor` prompt told the model that `findings`
would not reach the builder, so there was no reason for it to
duplicate diagnostic detail into `required_changes` — the two fields
serve genuinely different purposes (imperative vs. diagnostic) and
collapsing them loses information either way.

The REPLAN path already forwards more: `_build_planner_prompt` passes
the planner both `auditor.findings` and `auditor.design_observations`
(but not `required_changes`, which the `AuditorResult` validator
requires to be empty on REPLAN in practice). This decision brings the
REVISE path's context-richness closer to parity with REPLAN, without
making the two paths identical.

`design_observations` is deliberately excluded from what reaches the
builder on REVISE. It is the auditor's sanctioned channel for
questioning the task's own acceptance criteria rather than the
implementation against them; the auditor prompt tells it to prefer
REPLAN (which does route to the planner, the role actually able to
rescope a task) over letting criteria-level concerns leak into an
ordinary REVISE cycle. Handing `design_observations` to the builder on
REVISE would undermine that separation by giving the builder a second,
unreviewed path to scope changes the auditor was told not to force
through REVISE.

## Decision

- `_build_builder_prompt` gains an `audit_findings` parameter,
  rendered (when non-empty) under a header distinct from
  `required_changes`: "Supporting detail from the audit (context for
  the changes above, not additional requirements)." `required_changes`
  keeps its existing header and renders first.
- `_do_building` passes `auditor.findings` as `audit_findings` on the
  REVISE path. `auditor.design_observations` is still never read on
  this path.
- The `loop-auditor` prompt now states the routing explicitly: on
  REVISE, the builder receives `required_changes` (authoritative,
  must-fix, must stand alone) plus `findings` (supporting context);
  on REPLAN, the planner receives `findings` and `design_observations`;
  `design_observations` never reaches the builder. This lets the
  auditor write `findings` knowing it will actually be read on REVISE,
  rather than treating it as REPLAN-only.

## Consequences

- A REVISE cycle's builder invocation now has the auditor's
  diagnosis and reproducing examples available, not just the
  imperative summary, reducing the chance of a narrower-than-intended
  fix.
- `required_changes` remains the sole field whose presence is
  schema-enforced on REVISE (`AuditorResult`'s validator) and the sole
  field rendered under an imperative header; `findings` is explicitly
  framed as non-normative context, so a builder cannot satisfy the
  task by addressing `findings` while ignoring `required_changes`.
- `design_observations` remains structurally confined to the REPLAN
  path, preserving the auditor's REVISE-vs-REPLAN scope discipline
  from ADR-adjacent prompt guidance.
- No schema change: `AuditorResult` already had all three fields:
  this decision only changes which of them the supervisor reads on
  the REVISE path and how it renders them, plus what the auditor
  prompt tells the model about that routing.
