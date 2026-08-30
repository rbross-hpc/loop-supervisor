# Architecture decision records

This directory records consequential design decisions for this
project, using a lightweight ADR format.

## Format

Each ADR is a numbered Markdown file: `NNNN-short-title.md`, numbers
zero-padded to four digits and assigned sequentially. Each file has
exactly these sections:

```markdown
# Title

## Status

Accepted

## Context

What situation motivated this decision.

## Decision

The decision itself, stated plainly.

## Consequences

- Bullet list of resulting tradeoffs, constraints, or follow-up work.
```

## How new ADRs are created

Write the first ADR yourself, by hand, to record any design constraint
you already know you want the loop to respect before starting a run.
From then on, ADRs are proposed by the `loop-architect` agent
(read-only) when the planner or auditor escalates a design question
with `decision_required: true`. The supervisor — never the architect
directly — writes the exact approved text into the active task
worktree as the next available `NNNN-title.md`, so the builder's own
commit captures it.

`Status` is currently always `Accepted`: there is no
superseding/deprecation workflow yet. If a later decision reverses an
earlier one, note that in the newer ADR's `Context` section and
reference the older ADR by number.
