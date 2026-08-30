# Deny-by-default permission asks and project-venv PATH in server mode

## Status

Accepted

## Context

Running the loop against a real project (`test-run`, run `2dba05654b5e`),
the auditor role hung twice, each time for 15-20 minutes before being
killed manually. The global OpenCode log (not the supervisor's own log,
which recorded nothing during the hang) showed the actual cause:

```
message=evaluated permission=glob pattern=pytest* action.action=allow
message=evaluated permission=glob pattern=ruff*   action.action=allow
message=evaluated permission=glob pattern=mypy*   action.action=allow
message=evaluated permission=external_directory pattern=/usr/local/bin/*      action.action=ask
message=asking    permission=external_directory patterns=["/usr/local/bin/*"]
message=evaluated permission=external_directory pattern=/home/node/.local/bin/* action.action=ask
message=asking    permission=external_directory patterns=["/home/node/.local/bin/*"]
```

then silence until killed. The auditor globbed for `pytest`, `ruff`,
and `mypy`, did not find them on `PATH` (they are installed only into
each project's own `.venv`, never onto the ambient `PATH`), and went
looking for them in system directories outside the project. Every
project's `opencode.json` leaves `external_directory` at its default
of `"ask"` outside the one explicitly allowed path. In an interactive
session, `ask` renders a prompt a human answers. The supervisor drives
OpenCode as `opencode serve` over HTTP with no human and no code
subscribed to the permission-ask channel, so the underlying
`POST /session/{id}/message` call simply never returns: `ask` is a
silent, permanent hang in this mode, not a bounded failure.
`role_timeout` did not help because 15-20 minutes never approached
its 1800-second bound; had the process been left running, the timeout
would eventually have fired, but that is 30 minutes wasted per
occurrence rather than a real fix.

The global OpenCode log also shows one earlier occurrence of the
structurally identical problem for a different permission: a single
`doom_loop` `ask` (run `10fcb9bd`, 2026-08-22) that only resolved once
a human answered it in a later interactive session. Any permission
whose action can resolve to `ask` is a latent instance of the same
deadlock class under a server-driven run; `external_directory` and
`doom_loop` are the two permissions that default to `ask` per
OpenCode's own defaults (every other permission defaults to
`"allow"`).

Separately, and the actual root cause the auditor was reacting to:
`pytest`, `ruff`, and `mypy` are genuinely not reachable via plain
`PATH` lookup for either the integration project or a task worktree,
even though `test-run/.venv/bin/` (inside the already-allowed
`external_directory` tree) contains all three. The auditor's bash
allowlist is pattern `"pytest *"`, which matches `pytest -q` but not
`.venv/bin/pytest -q`, so even an auditor that *did* find the venv by
inspection could not invoke it under its own permissions without an
explicit path-qualified allowlist entry. Both completed audits in this
run (`task-002`, `task-004`) ACCEPTed based on static inspection alone
and said so explicitly in their `findings`, because neither could run
the verification commands its own prompt asks for.

A symlinked or shared `.venv` between the integration project and a
task worktree was considered and rejected as a fix for the PATH
problem: an editable install's `.pth` file, and every console-script
shebang OpenCode's own `pytest`/`ruff`/`mypy` would produce, embeds an
*absolute* path back to wherever `pip install -e .` was run. A task
worktree sharing the integration project's venv would therefore import
the integration checkout's `src/`, not its own — silently verifying
the wrong tree with no visible error, which is worse than the auditor
being unable to run tests at all today.

## Decision

**Permission defaults** (`opencode.json`, both this project's own
config and every project the supervisor drives): explicitly set

```json
"permission": {
  "external_directory": { "*": "deny", "<allowed paths>": "allow" },
  "doom_loop": "deny"
}
```

`"*": "deny"` is placed before the specific `allow` entries so that,
under OpenCode's last-match-wins rule evaluation, the already-allowed
paths remain allowed and only the previously-`ask` fallback becomes a
`deny`. No other permission key needs an entry: every one of them
already defaults to `"allow"`, and a literal blanket `"*"` at the top
level of `permission` (as opposed to scoped to `external_directory`)
was considered and rejected, since the four loop agents' own
Markdown-frontmatter permission blocks only override `edit` and
`bash` — a top-level `"*": "deny"` would silently deny `read`, `glob`,
`grep`, etc. for every agent, since nothing overrides those keys back.

A denied action returns control to the agent immediately (as an
explicit refusal it can reason about and route around, e.g. reporting
`BLOCKED` or trying a different path), which is qualitatively
different from `ask`: it can never hang.

**`PATH` injection**: `OpenCodeServer` gains a `build_agent_env()`
helper (`opencode.py`) that `RunSession.__enter__` (`runtime.py`) now
calls to construct `OpenCodeServerConfig.env`, prepending two entries:

1. A *relative* `.venv/bin`, included unconditionally. Every agent
   invocation runs with `directory` set to its own task worktree, and
   a relative `PATH` entry is resolved fresh at each command's
   exec-time against *that* invocation's cwd — not against the
   supervisor's own directory when `build_agent_env()` runs, typically
   once at server startup. This transparently picks up whichever
   worktree's own `.venv` exists, with the supervisor never needing to
   track per-worktree paths itself.
2. `<project_root>/.venv/bin`, an absolute fallback (only added if it
   exists) for invocations with no venv of their own, e.g. the planner
   working in the integration root before any task worktree exists.

Both are prepended ahead of the inherited `PATH`, so a project-local
tool always shadows a same-named system tool.

## Consequences

- A permission that would previously prompt (`external_directory`
  outside the allowed path, `doom_loop`) now denies immediately under
  a server-driven run. This is a behavior change for interactive
  OpenCode use of the same config too, not just the supervisor: a
  human running these projects directly will see `deny` where they
  might previously have been asked and could have said yes. This is
  accepted as the right tradeoff for these specific two permissions —
  a repository lock, worktree isolation, and this project's design
  documents already define what the agents are meant to touch, so
  reaching outside them (or doom-looping) is not expected to be a
  legitimate need.
- `pytest`, `ruff`, and `mypy` are now findable via plain `PATH`
  lookup from any agent invocation, without any change to the
  auditor's `"pytest *"` bash allowlist pattern — the pattern still
  matches, it now simply resolves to the right binary.
- No `.venv` symlinking or sharing between the integration project and
  task worktrees: each worktree needing its own venv must create one,
  exactly as `test-run-task-002`'s builder already did unprompted.
  Sharing would silently test the wrong source tree (see Context).
- `role_timeout` remains the correct backstop for a genuinely slow or
  stuck agent; it was never the right tool for an *unanswerable*
  question, which no timeout duration fixes, only masks the cost of.
- Not addressed here, filed as backlog items instead: supervisor-side
  detection of a pending permission `ask` over the OpenCode API (so
  config is defense-in-depth rather than the only guard against this
  deadlock class), and the fact that both audits completed so far in
  the `test-run` run never actually executed their verification
  commands, so their ACCEPT dispositions rest on static inspection
  alone.
- **A permission-config edit to the integration project does not
  retroactively reach a task worktree that already existed when the
  edit was made.** Confirmed while diagnosing a real stuck run
  (`test-run-2`, `5c7e2d584cfd`, backlog item 44): OpenCode loads
  `opencode.json` starting from the invocation's own `directory`
  (`RunSession`/`opencode.py`'s `run_agent` sets this to the task
  worktree, not the server's `project_dir`), so each task worktree
  needs its own copy of `opencode.json` for a permission change to
  take effect there. A normal `cmd_init_copy`-generated project never
  notices this: `git worktree add` checks out whatever the integration
  branch's tip already contains, so a config fix committed *before* a
  worktree is created is picked up automatically. The gap only appears
  when a fix is applied *after* task worktrees already exist — as
  happened here, where a hand-authored `opencode.json` (missing this
  ADR's `permission` block entirely, since it predated `cmd_init_copy`
  scaffolding) was corrected mid-run at the integration root, but the
  in-flight task worktree still had the pre-fix file checked out at
  its own `HEAD` and needed the identical edit applied and committed
  there separately before the auditor could read the file it needed.
  Not a code defect — this follows directly from how `git worktree`
  and OpenCode's own config resolution interact — but worth knowing
  before assuming a permission-config fix applies uniformly to a run
  already in progress.
