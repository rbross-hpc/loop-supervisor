# Plan: objective channel + installable bootstrap

## Confirmed findings

- Fresh-run planner prompt is entirely `"Determine the next unit of
  work."` (`supervisor.py:1528`) — no objective is injected, and the
  supervisor reads no project files itself.
- All scope derives from `.opencode/agents/*.md` naming exactly two
  paths: `README.md` and `docs/decisions/`. `docs/plans/` is named by
  zero prompts, despite being tracked and actively used.
- `AGENTS.md` is a dead end for handoff: gitignored, never copied by
  `init`, and referenced by zero code and zero prompts.
- `init --destination` copies the supervisor wholesale (its own
  README, all ADRs, `src/loop_supervisor/**`, `tests/**`), plus two
  silently-wrong hardcoded values: `opencode.json:34`
  (`external_directory` allow-path) and `opencode.json:45`
  (`X-Falda-Tenant`).
- Packaging is already sound (`[project.scripts]`, setuptools/src
  layout); the wheel blocker is specifically `_template_source_root()`
  (`cli.py:247-248`) and `_tracked_files()`'s `git ls-files` dependency
  — see `docs/decisions/0007-tracked-files-only-bootstrap-copy.md`.

## Branch 1 — `feature/objective-doc` (first)

Small, independently useful: lets a standalone-session objective be
handed to the loop today, and gives Branch 2 something concrete to
generate.

1. Add `docs/OBJECTIVE.md` as a named canonical source in all four
   `.opencode/agents/*.md`, alongside `README.md` and
   `docs/decisions/`.
2. Add `docs/plans/` to the planner/architect prompts, closing the
   pre-existing gap where this repo's own working docs are invisible
   to the loop.
3. Write this repo's own `docs/OBJECTIVE.md` (dogfooding; also the
   template's worked example).
4. README section documenting the handoff procedure: standalone
   session writes `docs/OBJECTIVE.md` + ADRs, then
   `loop-supervisor run`.
5. ADR 0017: objective channel is file-based, not prompt-injected;
   records that the stronger `--objective`/`RunState` form is
   deliberately deferred behind backlog item 30 (schema squash).

**Verification that actually matters:** run the loop against a scratch
fixture with a deliberately distinctive `docs/OBJECTIVE.md` and
confirm with `--max-steps 1` that the planner's chosen task reflects
it. Prompt-file edits are not verifiable by unit test alone — this is
the real check.

## Branch 2 — `feature/bootstrap-skeleton` (second, larger)

1. Replace the git-based template mechanism with packaged template
   data (`importlib.resources`), retiring `_template_source_root()`
   and the `git ls-files` dependency. This is the actual enabler for
   wheel installs and supersedes part of ADR 0007 — written up rather
   than silently contradicted.
2. New project gets only: `.opencode/agents/*`, `opencode.json`,
   `.env.example`, `.gitignore`, a skeleton `pyproject.toml` depending
   on `loop-supervisor`, `docs/decisions/README.md`, a stub
   `docs/OBJECTIVE.md`, and a skeleton `README.md`. Not
   `src/loop_supervisor/**`, `tests/**`, this repo's README, its ADRs,
   or `docs/plans/**`.
3. Parameterize the landmines: `init` sets `external_directory`'s
   allow-path to the new project root and prompts for / blanks the
   Falda tenant.
4. ADR 0018 for the distribution model, superseding ADR 0007's
   tracked-files approach.

**Verification:** `init` into a temp dir → assert no supervisor source
or foreign ADRs are present → assert config is parameterized →
`pip install` the built wheel → run the loop with `--max-steps 1` and
confirm the planner plans the new project's objective, not the
supervisor's.

## Risks flagged, not yet actioned

- **Self-hosting regression.** Today this repo improves itself via the
  loop. Dependency-mode means a new project can't easily hack on the
  supervisor itself. If that matters, `init --fork` becomes a
  follow-up, not a silent loss.
- **Versioning.** Dependency mode implies agent definitions and the
  supervisor package can drift apart. Worth pinning agent-definition
  compatibility somewhere.

Both are worth capturing as backlog items regardless of whether they
are built now.
