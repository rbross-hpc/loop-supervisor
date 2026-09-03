# Interpreting the first `--max-steps 1` run

`loop-supervisor run --project . --max-steps 1` performs exactly one
phase transition and stops — almost always the planner choosing (or
declining to choose) a first task. Read the printed `final phase` and
any `paused at phase ...` line.

## A healthy first step

- `final phase: planning` (or similar), with a paused message, and the
  planner's chosen task visible via `loop-supervisor resume` (list
  mode, no run_id) or the TUI. The task should be a coherent,
  reasonably-scoped unit of work that plausibly matches
  `docs/OBJECTIVE.md`.
- Exit code `1` is normal here — it means the run paused rather than
  reaching `done`, which is expected after exactly one step.

If this is what you see, show it to the human (the checkpoint at the
end of `SKILL.md`), then let them decide whether to continue with
`loop-supervisor run --project .` (no step limit) or `tui`.

## Common early failures and what they mean

**Planner immediately reports `COMPLETE`.** Usually means
`docs/OBJECTIVE.md` is too vague, already-satisfied, or describes a
finished state the planner reads as "nothing to do." Revisit the
objective — see `objective-and-adrs.md` — rather than assuming the
project is actually done.

**A permission denial in the output (e.g. `denied permission request
... ('external_directory')`).** Check
`config-and-permissions.md`'s first gotcha — the parent-directory
allow rule. Re-run `loop-supervisor config validate` to confirm.

**Run fails with no text output from an agent role
(`agent 'loop-X' returned no text output`).** This is usually a
downstream symptom of repeated permission denials exhausting the
agent's steps without ever producing valid output, not a distinct bug.
Check the same permission configuration first.

**`error: resume task worktree has changed since it was paused`.**
Something modified the task worktree outside of loop-supervisor's own
control after it was created — a manual edit or commit inside it, or a
process interrupted mid-edit. (Config fixes no longer require touching
a worktree: the supervisor points every invocation at the integration
root's `opencode.json` via `OPENCODE_CONFIG`, so fix it there and it
applies everywhere — see `config-and-permissions.md`.) If the
worktree's `HEAD` moved (e.g. a manual commit), there is no supported
way to reconcile this. If `HEAD` did not move and the worktree is
merely dirty (e.g. a killed builder invocation), this is recoverable —
see the `use-loop-supervisor` skill's `recovering-an-interrupted-run.md`.

**The planner or auditor requests a design decision
(`decision_required: true`) and the architect responds `NEEDS_INPUT`.**
This is not a failure — it means the architect genuinely needs
information only the human has (a preference, a constraint not present
anywhere in the repository). Answer the specific question asked; do
not guess on the human's behalf.
