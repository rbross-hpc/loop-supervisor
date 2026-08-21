# Architect escalation is explicit and separate from ordinary replanning

## Status

Accepted

## Context

Some questions the planner or auditor encounters are genuinely
architectural — they deserve a stronger reasoning model, a durable
written record, and possibly human sign-off. But not every `REPLAN` is
like this: most replanning is just "the plan didn't work out," not "we
have discovered a design ambiguity." Automatically escalating every
`REPLAN` to a more expensive model would be wasteful and would blur two
different failure modes together. Likewise, the architect must never
write files directly, or approval review would have nothing concrete and
stable to approve.

## Decision

- Escalation is explicit: the planner (on a `READY` result) or the
  auditor (on a `REPLAN` disposition) sets `decision_required: true`
  along with a `decision_question` and `decision_rationale`. Ordinary
  `REPLAN` without this flag goes straight back to the planner, not the
  architect.
- `loop-architect` is read-only and uses a distinct (currently stronger)
  model than the other three agents. It answers exactly the escalated
  question — it does not re-review the whole task.
- The architect returns `DECIDED` (with a full ADR: title, context,
  decision, consequences) or `NEEDS_INPUT` (with a specific question for
  the operator). It never guesses when it lacks enough information.
- `NEEDS_INPUT` retries the *architect*, not the planner, once the
  operator's answer is collected.
- The supervisor, not the architect, writes the exact approved ADR text
  to disk, inside the active task worktree, so the builder's own commit
  captures it as part of the task's history.
- By default (`auto_decide=True`), a `DECIDED` proposal is accepted
  automatically and written immediately. Passing
  `--require-decision-approval` makes this interactive: an operator can
  approve, or reject with feedback, which sends the architect back for
  another attempt.

## Consequences

- Ordinary replanning stays cheap and fast; the more expensive model is
  reserved for genuinely escalated questions.
- Because the architect never writes files, "what got approved" and
  "what got committed" are always the same text — there is no
  transcription step where a second model call could subtly alter
  approved content.
- Requiring a fresh, focused prompt for `NEEDS_INPUT` retries keeps the
  architect from silently drifting away from the original question while
  waiting on operator input.
