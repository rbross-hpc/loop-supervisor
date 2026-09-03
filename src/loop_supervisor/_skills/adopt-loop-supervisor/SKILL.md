---
name: adopt-loop-supervisor
description: Use ONLY when bringing the loop-supervisor planner/architect/builder/auditor agent loop into an existing repository that does not already use it — the human will typically ask you to "adopt loop-supervisor into this project" or similar. Covers prerequisites, installing loop-supervisor, generating and merging the project skeleton, writing docs/OBJECTIVE.md and seed ADRs from the existing codebase, retargeting the auditor/builder toolchain, and the config-and-permissions gotchas that cause a run to silently fail. Do not use for starting a brand-new empty project (use `loop-supervisor init` directly) or for day-to-day loop-supervisor operation once it is already adopted.
---

# Agent Skill: adopt-loop-supervisor

## Purpose

`loop-supervisor` (https://github.com/rbross-hpc/loop-supervisor) is
a headless supervisor that drives an OpenCode planner/architect/builder/
auditor loop over Git worktrees. `loop-supervisor init` is the supported
way to start a brand-new project, but it requires an **empty**
destination directory — there is no built-in command for bringing the
loop into a repository that already has code, history, and its own
conventions. That is what this skill is for.

You (the agent) are doing this work, not the human — the whole point of
the loop is that a human can hand a project to it and step back. Pause
at the numbered checkpoints below where the human's judgment is
required (naming the project's actual objective, approving toolchain
changes, approving the first real run); do not guess through those.

Every reference file below is short and single-purpose. Read the one
relevant to the step you're on; don't front-load all of them.

## The Workflow

### 0. Prerequisites check

Run:

```bash
loop-supervisor config validate --project . --json
```

If `loop-supervisor` isn't installed yet, install it first — ask the
human "please install loop-supervisor" if you don't have shell access
to do so yourself, or run:

```bash
pip install "loop-supervisor @ git+https://github.com/rbross-hpc/loop-supervisor.git"
```

Read the JSON report. If `"ok": false`, **stop** and read
`references/prerequisites.md` for what each failing check means and
how to fix it before continuing — most commonly a missing OpenCode
install, a dirty working tree, or unset provider credentials. Do not
proceed past this step with any check failing.

### 1. Generate a reference skeleton

Generate the skeleton into a **temporary** directory, not the target
repository — `init` requires an empty destination, and the target repo
is not empty:

```bash
loop-supervisor init --destination /tmp/loop-skeleton-<project-name>
```

You will selectively copy pieces of this into the target repository in
the steps below; you are never running `init` against the target repo
itself.

### 2. Write `docs/OBJECTIVE.md`

This is the single most important step and the one most likely to need
human input. `docs/OBJECTIVE.md` is the first thing every loop agent
role reads; a vague or wrong objective misdirects the entire loop.

Read `references/objective-and-adrs.md` for the full guidance on
reverse-engineering an objective and seed ADRs from an existing
codebase's history, README, and code structure. **Checkpoint:** show
the human your drafted objective before committing it — this is a
judgment call about scope and priority that only the human can
actually confirm.

### 3. Copy and adapt the agent definitions

Copy `.opencode/agents/` from the generated skeleton into the target
repo unchanged as a starting point, then retarget the builder's and
auditor's tool-specific permissions (the skeleton defaults assume
`pytest`/`ruff`/`mypy`, which is almost certainly wrong for a non-Python
project). See `references/toolchain.md` for exactly which lines to
change and how to identify the target project's real toolchain — it also
covers the optional `model:` frontmatter line if you want a role to run
on a specific model rather than the project's default.

### 4. Configure `opencode.json` and permissions

The supervisor points every agent invocation at the integration root's
`opencode.json` via `OPENCODE_CONFIG` (ADR 0032), so there is one file
to get right and it applies to task worktrees too. Read
**`references/config-and-permissions.md` in full before editing
anything** — it documents two specific mistakes that produce a run that
hangs or silently fails with no clear error.

Look at the target repo's existing `opencode.json` if it has one:

- **If it exists**, read it and propose the edits it needs — most
  importantly an `external_directory` block allowing the *parent* of
  the project root and that parent's `/**` subtree (not just either
  exact directory), plus `doom_loop: deny`. Preserve whatever provider,
  model, and MCP settings the project already has. **Checkpoint:** show
  the human the proposed edits and apply them only once they approve.
- **If it does not exist**, create one — the quickest source is the
  `opencode.json` in the skeleton you generated in step 1, whose
  `external_directory` is already scoped to the skeleton's parent;
  adjust the paths to the target repo's actual parent directory.

Whether the resulting `opencode.json` is committed is the project
owner's call. If they prefer not to track it (e.g. it carries
credentials-adjacent provider config), add it to `.gitignore` rather
than leaving it untracked — see `references/config-and-permissions.md`
for why an untracked-but-unignored file breaks the run.

### 5. Commit and re-validate

Commit everything from steps 2-4 on a clean branch in the target repo,
then re-run:

```bash
loop-supervisor config validate --project . --json
```

Confirm `"ok": true` before proceeding — this catches most of the
mistakes from step 4 if they slipped through.

### 6. Smoke test

```bash
loop-supervisor run --project . --max-steps 1
```

This runs exactly one phase transition (almost always the planner
choosing a first task) and stops, so you see real output without
committing to a full run. Read `references/first-run.md` for what a
healthy first step looks like and how to interpret common early
failures (permission denials, an empty/wrong objective, a planner that
immediately reports `COMPLETE`).

**Checkpoint:** show the human the planner's first task before running
further steps. Once they're satisfied, hand off to normal operation:

```bash
loop-supervisor run --project .
```

for a full unattended run.
