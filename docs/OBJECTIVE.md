# Objective

This is the `loop-supervisor` project itself: a headless supervisor
that drives an OpenCode planner/architect/builder/auditor loop over
Git worktrees, so an LLM coding agent can incrementally implement a
project one bounded task at a time, with an independent review step
before anything merges.

The current objective is to hold the supervisor to production quality
as its own most demanding user: run it against itself, find defects
the way any real user would, and fix them with the same rigor the
auditor demands of any other project (failing-first tests, an ADR for
any non-obvious design decision, no regressions in the existing
suite).

Active priorities, in order:

1. Work through the open items in the current lifecycle-fix backlog —
   see `docs/plans/2026-08-22-post-lifecycle-fix-backlog.md` for the
   full, itemized list and current status of each. Tier 1 items are
   the most severe and should be preferred over lower tiers absent a
   specific reason to do otherwise.
2. Once the backlog above is clear, look for defects, gaps, or
   unnecessary complexity in the supervisor by direct inspection and
   by exercising it against a real project, and fix them following the
   same conventions as the rest of this repository.

Consult `docs/decisions/` for accepted design constraints before
proposing anything that would contradict one; escalate to the
architect role instead of silently overriding a prior ADR.

This file is also the worked example for what a project bootstrapped
from this template should write for itself — see the "Handing off
from a standalone session" section of `README.md`.
