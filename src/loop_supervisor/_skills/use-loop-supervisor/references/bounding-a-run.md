# Bounding a run: `--max-tasks`, `--step`, `--max-steps`

These bound different things. Picking the wrong one either stops
before you see anything meaningful, or commits to far more than you
meant to supervise.

## `--max-tasks N`

Bounds **accepted tasks** (the planner will not select a new task once
`accepted_task_count` reaches `N`; a task already in progress finishes
normally). This is the right choice for "run exactly one full task,
start to merge, and stop" — the natural unit for a first supervised
run after a change to the supervisor or its agent prompts.

```bash
loop-supervisor run --project . --max-tasks 1
```

Note this still runs to completion for that one task — planning
through merge and cleanup — which can be a long time (see `SKILL.md`'s
detach guidance). It does not limit how long the task itself takes.

## `--step` / `--max-steps N`

Bounds **completed phase transitions**, regardless of which task
they belong to or whether any task ever completes. `--step` is
shorthand for `--max-steps 1`.

```bash
loop-supervisor run --project . --max-steps 1
```

Use this to inspect one phase transition at a time — e.g. confirming
the planner picks a sane first task before letting the loop continue,
or stepping through a run you don't yet trust. Each step can still
block for up to `--role-timeout` if it invokes an agent (most phases
do), so this bounds progress, not wall-clock time.

`--step`/`--max-steps` and `--max-tasks` are not mutually exclusive:
combine them if you want a hard ceiling on both.

## Neither flag

An unbounded `loop-supervisor run --project .` runs until the planner
reports `COMPLETE` (nothing left to do) or a terminal failure. This is
appropriate for genuine unattended operation, not for a first
supervised run of anything you don't already trust.
