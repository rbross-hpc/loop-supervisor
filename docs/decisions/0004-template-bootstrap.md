# This repository doubles as a project template with two bootstrap modes

## Status

Accepted

## Context

The supervisor, agent definitions, contracts, and tooling built here are
intended to be reused across future projects, not rebuilt from scratch
each time. That means this repository needs to work both as itself (an
active project with its own history) and as a seed for new, unrelated
projects that shouldn't inherit its Git history or its `.env` secrets.

There are two realistic ways someone starts a new project from this
template: they may already have a fresh clone they intend to repurpose in
place, or they may want to copy the template out to a new location while
leaving the original checkout untouched.

## Decision

Support both bootstrap modes as first-class `loop-supervisor init`
behavior, not just documentation:

- `loop-supervisor init --destination <path>`: copies all tracked
  template files to a new, empty directory, explicitly excluding `.git`,
  `.env`, and local caches/build artifacts. Never initializes a Git
  repository in the destination; that is left to the user.
- `loop-supervisor init --in-place --yes`: verifies the current directory
  looks like the template (checks for `pyproject.toml`,
  `src/loop_supervisor`, `.opencode/agents`) and has a `.git` directory,
  requires a clean tree (or `--force`), requires explicit confirmation
  (typed phrase, or `--yes`), and then permanently deletes `.git`. Files,
  including the local `.env`, are left in place. No replacement
  repository is initialized automatically.

Both paths keep the secret-bearing `.env` out of anything that gets
copied or reused, while leaving `opencode.json` tracked and secret-free
(it references credentials via `{env:VAR}` interpolation).

## Consequences

- New projects never accidentally inherit this repository's commit
  history, remotes, or credentials.
- Because `init` is a tested Python subcommand rather than a shell
  script, its safety checks (non-empty destination, non-template
  directory, dirty tree, missing `.git`) are exercised the same way as
  the rest of the supervisor.
- Users are still responsible for running `git init` themselves after
  either bootstrap mode; the supervisor deliberately does not create a
  new repository or first commit on their behalf.
