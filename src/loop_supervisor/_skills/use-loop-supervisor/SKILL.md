---
name: use-loop-supervisor
description: Use when starting, monitoring, bounding, recovering, or auditing a `loop-supervisor` run in a project that has already adopted it — i.e. day-to-day operation, not first-time setup. Covers why a run must be started detached from any harness command timeout, how to observe a running loop without disturbing it, bounding a run with `--max-tasks`/`--step`/`--max-steps`, recovering an interrupted run, and what to check before trusting (and pushing) whatever the loop merged. Do not use for bringing loop-supervisor into a project that doesn't have it yet (use the `adopt-loop-supervisor` skill) or for starting a brand-new empty project (use `loop-supervisor init` directly).
---

# Agent Skill: use-loop-supervisor

## Purpose

Once `loop-supervisor` is adopted into a project (see the
`adopt-loop-supervisor` skill if it isn't yet), running it well as an
*agent* has a few sharp edges that are easy to hit once and costly to
hit twice. This skill exists because the most expensive mistake — a
foreground shell timeout killing a run mid-agent-call — is entirely
avoidable and not obvious from `--help` or the README alone.

Every reference file below is short and single-purpose. Read the one
relevant to what you're doing; don't front-load all of them.

## Before you start a run: detach it

**A `loop-supervisor run`/`resume` invocation is long-lived and must
never be started in a foreground shell subject to your own harness's
command timeout.** Each planner/architect/builder/auditor call can
block for up to `--role-timeout` (default 1800 seconds), and a single
task is many such calls in sequence — an entire run can easily run for
tens of minutes to hours even for one task.

If your own tool-calling shell enforces a timeout (many agent harnesses
do, often in the 5-30 minute range), a foreground `run` you block on
**will** get killed mid-agent-call once that timeout is shorter than
the current phase takes — this is not a hypothetical, it has happened
in this project's own history. Losing the process this way is
recoverable (see `references/recovering-an-interrupted-run.md`), but
avoiding it entirely is far cheaper:

```bash
nohup loop-supervisor run --project . --max-tasks 1 > /tmp/run.log 2>&1 &
disown
```

Then poll rather than block — see
`references/observing-a-run.md` for what to poll and how to tell a
slow-but-healthy run apart from a stuck one.

## Bounding a run

Read `references/bounding-a-run.md` before choosing between
`--max-tasks`, `--step`, and `--max-steps` — they bound different
things (accepted tasks vs. phase transitions) and picking the wrong
one either stops too early to see anything or commits to more than you
meant to supervise.

## If a run gets interrupted

A killed or crashed run is not automatically lost, but resuming it
incorrectly can make a perfectly recoverable situation look like a
dead end. Read `references/recovering-an-interrupted-run.md` **before**
running `git checkout --` or `git clean` in a task worktree, or before
concluding a resume failure means the run must be abandoned — both of
those are easy to get wrong in a way that discards real, good work.

## Before trusting (and pushing) what the loop merged

The loop merges into your integration branch on its own; treat that
merge the way you would treat a human contributor's PR, not as
automatically correct just because the gates passed. Read
`references/auditing-a-merge.md` for the specific checks (merge shape,
commit-message quality, failing-first evidence, gate re-run on the
merged result) before pushing.
