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
