# Supervisor points OPENCODE_CONFIG at the integration-root opencode.json

## Status

Accepted

## Context

OpenCode resolves `opencode.json` starting from each invocation's own working
directory, walking up only to the nearest enclosing git directory. A task
worktree is its own git checkout, so builder and auditor invocations — which run
with `directory` set to their task worktree — load the worktree's own copy of
`opencode.json`, never the integration root's. ADR 0014 documented the
consequence: a permission-config edit made at the integration root after a task
worktree already exists does not reach that worktree, because the worktree still
has whatever `opencode.json` was checked out at its `HEAD`. Fixing config
mid-run therefore required committing the identical edit a second time inside the
in-flight worktree on its own branch — a sharp, easily-missed operational edge
(ADR 0014's final consequence bullet; the `adopt-loop-supervisor` skill's
`config-and-permissions.md` gotcha "config changes must be committed before a
worktree exists").

That whole class of problem exists only because config resolution was tied to
what git had materialized into each worktree. It also forced an implicit
requirement that `opencode.json` be tracked and committed, so that
`git worktree add` would carry it into each new worktree. Whether a project's
`opencode.json` is tracked is properly the project owner's choice — the file may
legitimately hold environment-specific provider, model, and MCP settings (this
project's own file carries an Argo provider block and a Falda MCP server with a
per-project tenant) that a user may or may not want in version control. The
runtime should not force that decision.

OpenCode already exposes a resolution channel that does not depend on the
invocation's working directory or on git: the `OPENCODE_CONFIG` environment
variable names a config file by absolute path, loaded between the user's global
config and any project `opencode.json`. The supervisor already sets the OpenCode
server's environment once at startup (`build_agent_env`), and that environment is
inherited by every agent invocation regardless of the `directory` each one runs
in.

## Decision

When the integration root contains an `opencode.json`, the supervisor sets
`OPENCODE_CONFIG` to that file's absolute path in the OpenCode server
environment. Every agent invocation — planner and architect at the integration
root, builder and auditor in task worktrees — then resolves the same file by
absolute path, independent of what (if anything) each worktree has checked out
and independent of whether the file is git-tracked. When no `opencode.json` is
present at the integration root, `OPENCODE_CONFIG` is left unset and OpenCode
falls back to its normal resolution, so a project relying solely on global config
is unaffected.

`init` is unchanged: it scaffolds a fresh project into an empty destination and
writes an `opencode.json` there as before (`external_directory` scoped to the
destination's parent, `doom_loop: deny`). Adopting an *existing* repository is
handled by the `adopt-loop-supervisor` skill, whose setup agent reads any
`opencode.json` already present, proposes the required `external_directory` /
`doom_loop` edits for the human to approve, and creates one only if absent —
never clobbering a project's existing provider, model, or MCP settings. Tracking
is left to the project owner: the runtime works identically whether the file is
committed, gitignored, or untracked.

This supersedes ADR 0014's requirement that each task worktree carry its own copy
of `opencode.json` for a permission change to take effect, and retires the
"commit config before a worktree exists" gotcha in the `adopt-loop-supervisor`
skill. ADR 0014's other decisions (deny-by-default `external_directory` and
`doom_loop` under server mode, project-venv PATH handling) are unchanged. This
does not touch how providers and models resolve (ADR 0023): those still come from
the user's global config and any provider block in the project `opencode.json`.

## Consequences

- A config edit at the integration root now reaches in-flight task worktrees
  automatically, because every invocation loads the same absolute-path file. The
  "apply the identical edit inside the worktree too" step is no longer required.
- Whether `opencode.json` is tracked no longer affects correctness of config
  resolution. A project may keep it out of version control — useful when it holds
  credentials-adjacent provider or MCP configuration — without breaking worktree
  invocations. To keep it out of version control it must be *gitignored*, not
  merely left untracked: the supervisor's cleanliness gates and `config
  validate`'s clean-worktree check use `git status --porcelain`, which lists
  untracked (but not ignored) files, so an untracked-and-unignored `opencode.json`
  would read as a dirty tree. This is a documentation point for the
  `adopt-loop-supervisor` skill, not a change to cleanliness semantics.
- For integration-root invocations the same file is loaded twice: once via
  `OPENCODE_CONFIG` and once as the project `opencode.json` for that directory.
  This is a harmless no-op merge (identical content, project tier wins on any
  conflict, and there is none).
- If a project has no `opencode.json` at all, worktree invocations get no
  `external_directory` scoping and will deny out-of-worktree access under server
  mode, exactly as before. `config validate` continues to check the on-disk
  `opencode.json` and flags a missing or wrongly-scoped `external_directory`
  regardless of the file's tracking status.
- `.opencode/agents/*.md` are resolved from directory-tier config and are
  unaffected by `OPENCODE_CONFIG`; they remain per-worktree and are carried by
  git as before. Their frontmatter still only overrides `edit`/`bash`/`skill`,
  so `external_directory` and `doom_loop` must continue to live in
  `opencode.json`, which is what `OPENCODE_CONFIG` now delivers to every worktree.
