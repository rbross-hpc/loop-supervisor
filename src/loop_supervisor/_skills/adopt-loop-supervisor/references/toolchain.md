# Retargeting the builder/auditor toolchain

The generated `.opencode/agents/loop-auditor.md` and
`.opencode/agents/loop-builder.md` assume a Python project using
`pytest`, `ruff`, and `mypy`. This is a starting-point default from
`loop-supervisor`'s own development conventions, not a limitation of
the loop itself — retarget it to whatever the adopted project actually
uses.

## What to change in `loop-auditor.md`

The auditor's `permission.bash` block has an explicit allowlist ending
in tool-specific entries, e.g.:

```yaml
    "pytest *": allow
    "ruff *": allow
    "mypy *": allow
```

Identify the target project's real test/lint/typecheck commands (check
its own README, CI config, `package.json` scripts, `Makefile`, or
build config — whatever it actually uses) and replace these three
lines with the equivalent allow-entries. Keep the pattern narrow (e.g.
`"npm test*": allow`, not a broad `"npm *": allow`) — the auditor
should be able to verify the builder's work, not run arbitrary
commands.

## What to change in `loop-builder.md`

The builder's permission block is already broad (`bash: "*": allow`
except `git merge`/`git push`), so it typically needs no toolchain
changes. If the target project needs specific setup before tests can
run (e.g. a build step, a dependency install), that belongs in the
project's own README under a section the builder is told to read
(both prompts already point at README.md and `docs/decisions/`) rather
than as a permission-block change.

## Sanity check

After editing, the auditor's prompt body (not just its permission
block) may also reference `pytest`/`ruff`/`mypy` by name in prose — the
skeleton's own README template flags this exact spot. Search both
`.md` files for those three tool names and update any prose mentions
alongside the permission entries so the two stay consistent.

## Optional: pin a per-role model

Each `.opencode/agents/loop-*.md` file carries a YAML frontmatter block,
and any role may pin the model it runs on with an optional `model:` line
alongside the other frontmatter keys:

```yaml
---
description: Resolves an escalated design decision. Read-only.
mode: primary
model: anthropic/claude-opus-4
temperature: 0.1
steps: 30
permission:
  edit: deny
  skill: deny
  bash:
    "*": deny
    "git status*": allow
    "git log*": allow
    "git diff*": allow
    "git show*": allow
---
```

The value is a `provider/model-id` string, and it must name a model that
the **adopted project's own** OpenCode config resolves — the provider in
it has to be one that project actually has configured (globally in
`~/.config/opencode/opencode.json` or in its own `opencode.json`), not
necessarily the Argo provider this repository develops against.

When a role has **no** `model:` line it inherits whatever the project's
OpenCode config resolves as its default — this is the intended default
(see ADR 0023, "Generated projects ship no provider configuration"), so
leave all four unpinned unless you have a specific reason to pin one. In
the generated skeleton the planner, builder, and auditor ship unpinned
for exactly this reason; only the architect is pinnable out of the box
(via `loop-supervisor init --architect-model provider/model-id`, which
writes the same `model:` line the example above shows).

Pin a role's model when you want it to differ from the default — most
commonly giving the architect and auditor a stronger reasoning model
than the planner and builder, since those two roles make the judgment
calls (design decisions and accept/revise/replan verdicts) where model
quality matters most. There is no supervisor-side model configuration:
the agent frontmatter is the only place a role's model is set, so this
edit is the whole mechanism.

## Optional: let `loop-supervisor` provision the task worktree itself

Each task worktree needs its own environment (e.g. a Python `.venv`,
`node_modules`, or whatever the target project's toolchain requires),
never shared or symlinked with the integration checkout's own — a
tool that records absolute paths back to where it was installed (an
editable Python install, for example) would otherwise silently verify
the wrong source tree. By default this is left to the builder agent's
own initiative on first need. To have `loop-supervisor` set it up
deterministically before building starts instead, add a
`loop-supervisor.toml` at the project root:

```toml
[provision]
commands = ["python3 -m venv .venv", ".venv/bin/pip install -e '.[dev]'"]
timeout = 600
```

Replace the example commands with whatever the target project's own
setup requires (e.g. `npm ci` for a Node project). This is entirely
optional and off by default — see `loop-supervisor run --help` for the
equivalent `--provision-command`/`--no-provision` flags.
