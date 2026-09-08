# use-loop-supervisor — Reference Index

Detailed material for each topic `SKILL.md` points at, split out so
`SKILL.md` stays focused on sequencing. Read the file for what you're
doing, not all of them up front.

| File | Covers |
|---|---|
| `observing-a-run.md` | What to poll (and how) once a run is detached and running |
| `verbose-output.md` | What `-v`/`-vv` print, and the stall-vs-busy heuristic they enable |
| `on-disk-layout.md` | The persisted run-state, phase-history, and verification-log files |
| `bounding-a-run.md` | Choosing between `--max-tasks`, `--step`, and `--max-steps` |
| `recovering-an-interrupted-run.md` | What to do (and not do) when a run is killed mid-phase |
| `auditing-a-merge.md` | What to check before trusting and pushing what the loop merged |
