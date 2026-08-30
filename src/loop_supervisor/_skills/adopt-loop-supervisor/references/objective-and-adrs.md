# Writing `docs/OBJECTIVE.md` and seed ADRs from an existing codebase

## `docs/OBJECTIVE.md`

The planner reads this file fresh on every invocation — it is the
*only* channel loop-supervisor uses to tell the planner what the
project is for. Get this wrong and every subsequent task is
misdirected, no matter how good the builder/auditor are.

For an existing codebase, do not write a generic restatement of
"continue development." Instead:

1. Read the project's own README, package metadata, and any existing
   design docs to state concretely what the project *is* and what
   "done" looks like (or what the standing priority is, if it's a
   long-lived tool with no final state).
2. Look at recent commit history and any open issue tracker / TODO /
   backlog file for what the *next* coherent unit of work actually is
   — this is what "what should the loop work on first" should answer.
   Do not invent a roadmap the project doesn't already have signal for.
3. Note anything explicitly out of scope, if the existing codebase has
   clear boundaries the planner shouldn't wander past (e.g. "this repo
   only handles ingestion, not the downstream analysis pipeline").

**Checkpoint:** this is a judgment call about scope and priority, not
a mechanical transcription. Draft it, then show the human the draft
before committing — get explicit confirmation the priority ordering
and scope boundaries are right, not just that the prose reads well.

## Seed ADRs

An existing codebase almost always has unwritten design decisions
baked into its structure that a builder or auditor could plausibly
contradict without ever knowing they existed (e.g. "we don't use ORM X
because Y", "all public APIs are versioned under /v1", "config is
env-vars only, never a config file"). loop-supervisor's own auditor
will escalate to the architect role when it notices a plausible
contradiction, but only if it has something to check against — an
empty `docs/decisions/` gives it nothing to work with.

Look for these signals when drafting seed ADRs:
- Comments in code explaining "we chose X over Y because..."
- A CHANGELOG or commit messages describing a reversal or migration
  ("switched from A to B")
- An existing (non-ADR-formatted) design doc, RFC, or wiki page
- Consistent structural patterns with no obvious alternative
  explanation (e.g. every module has the same file layout)

Write these using the ADR format in the generated skeleton's
`docs/decisions/README.md` (Status/Context/Decision/Consequences).
You do not need to capture everything — a handful of the decisions
most likely to be silently violated is more useful than an exhaustive
history nobody asked for. It's fine to leave this thin at first; the
architect role adds to it over time as real questions get escalated.
