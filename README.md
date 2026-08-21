# loop-supervisor

A headless supervisor that drives an [OpenCode](https://opencode.ai) agent
loop — planner, optional architect, builder, auditor — over Git worktrees,
so an LLM coding agent can incrementally implement a project one bounded
task at a time, with an independent review step before anything merges.

This repository is also a **project template**. Once you have a working
loop, you can bootstrap a new project from it (see [Bootstrapping a new
project](#bootstrapping-a-new-project) below) and keep only your project's
own goals and design docs, while reusing the supervisor, agent roles, and
tooling unchanged.

## How the loop works

```
planner --> [architect] --> builder --> auditor --> supervisor merge
   ^                                        |
   +---------------- REPLAN ----------------+
                        |
                     REVISE --> builder (same task)
```

Four OpenCode agents, each with a narrow permission boundary, plus a
Python supervisor that owns everything they are not allowed to do
themselves:

- **`loop-planner`** (`.opencode/agents/loop-planner.md`) — read-only.
  Chooses the next coherent unit of work, or reports that the project is
  `COMPLETE`. May flag that a design decision is required before work can
  proceed.
- **`loop-architect`** (`.opencode/agents/loop-architect.md`) — read-only,
  uses a stronger reasoning model. Invoked only when the planner or
  auditor explicitly escalates one focused design question. Proposes an
  ADR (title/context/decision/consequences), or reports that it needs
  more input rather than guessing.
- **`loop-builder`** (`.opencode/agents/loop-builder.md`) — read/write,
  full shell access except merge/push. Implements the task, runs tests,
  and commits to the task branch.
- **`loop-auditor`** (`.opencode/agents/loop-auditor.md`) — read-only plus
  `pytest`. Independently inspects the actual repository state (not just
  the builder's summary) and returns `ACCEPT`, `REVISE`, or `REPLAN`.
  The auditor evaluates strictly against the task's own stated acceptance
  criteria — it does not expand scope or apply preferences the task never
  claimed to satisfy.

The **Python supervisor** (`src/loop_supervisor/`) owns everything none of
the agents are trusted to do themselves: starting `opencode serve`,
creating fresh sessions, validating every agent's structured JSON output,
creating and merging Git worktrees, persisting resumable state, and
asking a human when an agent can't proceed on its own.

## Role boundaries (why merges are safe)

- The **builder** commits to its own task branch. It never merges or
  pushes.
- The **auditor** is strictly read-only. It reviews the diff between the
  integration branch and the task branch using exact commit hashes given
  to it by the supervisor — it never has to guess which branch or
  worktree is the candidate change.
- The **supervisor** is the only thing that merges, and only after
  independently re-verifying the builder's reported commit against actual
  Git state (branch, clean working tree, `HEAD`, and that new commits
  actually exist).

## Sibling task worktrees

Each task gets its own Git worktree, created one directory level above
the integration checkout:

```
/parent/
├── project/                    # integration worktree (this repo)
└── project-task-007/           # task worktree, branch loop/task-007
```

The task worktree and branch name are derived from the *original* planner
`task_id` for that unit of work and stay stable even if `REPLAN` produces
a revised `task_id` for the same worktree (see below). Override the
parent directory with `--worktree-root`.

## What happens on REVISE, REPLAN, and BLOCKED

- **`REVISE`**: the builder runs again on the *same* task worktree with
  the auditor's required changes appended to its prompt.
- **`REPLAN`**: the task worktree and its branch (including any
  intermediate commits) are **preserved**, not discarded. The planner is
  invoked again against that same worktree, with the auditor's findings
  in its prompt, and picks up from the intermediate state rather than
  starting over.
- **`BLOCKED` / `INCOMPLETE`** (builder): the supervisor pauses, asks the
  operator for guidance, and re-invokes the builder on the same worktree.
  Answering `replan` sends the task back to the planner instead.
- **Merge conflicts**: if a no-fast-forward merge into the integration
  branch fails, the supervisor aborts the merge (leaving the integration
  worktree untouched), preserves the task branch/worktree for diagnosis,
  and stops. It never auto-resolves or rewrites history.

## Design decisions and the architect

Some questions shouldn't be answered by the planner or auditor alone —
they need a more careful, possibly more expensive model, and they should
leave a durable record. Either the **planner** (`decision_required` on a
`READY` result) or the **auditor** (`decision_required` on a `REPLAN`
disposition) can escalate one focused question with a rationale. The
supervisor persists that request independently of either role's other
output, so it survives restarts and retries, and routes to
`loop-architect` instead of continuing directly.

- **Planner-originated** decisions continue straight to the **builder**
  once resolved — the planner has already scoped the task.
- **Auditor-originated** decisions return to the **planner** once
  resolved, on the same preserved task worktree/branch, along with the
  auditor's findings, design observations, and the recorded decision —
  the point of an auditor escalation is that the task needs to be
  reconsidered in light of the decision, not resumed as-is.
- The architect must answer the exact question it was asked; a
  mismatched or off-topic response is rejected as a contract violation
  rather than silently accepted.
- If the architect responds `DECIDED`, its proposal is persisted first.
  Only then, once approved (automatically, or by the operator under
  `--require-decision-approval`), does the supervisor write the *exact*
  approved text as a new file under `docs/decisions/NNNN-title.md` (see
  [`docs/decisions/README.md`](docs/decisions/README.md) for the ADR
  format) **inside the active task worktree**, so the builder's own
  commit captures it. Approval never re-invokes the architect — it
  records the already-persisted proposal, even across a pause/resume
  boundary. Rejecting a proposal and providing feedback is what
  triggers a new architect invocation.
- If the architect responds `NEEDS_INPUT`, the supervisor collects an
  answer from the operator and retries the architect — it does not fall
  back to the planner or auditor.
- By default, a `DECIDED` proposal is accepted automatically. Pass
  `--require-decision-approval` to pause for interactive
  approve/reject/feedback instead.

## Pausing and resuming

Runs are stateful and resumable. State is stored under Git's *shared*
metadata directory (`git rev-parse --git-common-dir`), not inside the
tracked worktree, so it's local to the clone, available from every linked
worktree, and never accidentally committed:

```
<git-common-dir>/loop-supervisor/runs/<run-id>.json
```

When the supervisor needs input it can't get non-interactively (no TTY,
or a future scripted run), it persists a pending question and exits. List
paused runs, or resume a specific one:

```bash
loop-supervisor resume            # lists saved run IDs
loop-supervisor resume <run-id>   # resumes from the saved phase
```

All run-defining options (limits, worktree root, approval policy,
OpenCode executable/timeout) are captured once when a run starts and
persisted as part of its state. `resume` does not accept any of these as
flags — it reconstructs the supervisor's behavior entirely from the
saved run, not from whatever happens to be passed at resume time, so a
run's limits and policies can't silently change mid-flight.

Resume validates the saved state strictly against current Git state
before starting anything (including before starting `opencode serve`),
and fails closed on any mismatch rather than guessing:

- the integration worktree's common dir, path, and branch must match;
- the integration worktree must be clean, and its `HEAD` must equal the
  last-recorded checkpoint or a clean descendant of it (concurrent,
  non-conflicting advancement of the integration branch is tolerated;
  a rewind or rewrite is not);
- if a task is active, its worktree must still be a real, registered Git
  worktree, checked out on the expected branch, with its `HEAD` and
  working-tree status matching the last-recorded checkpoint exactly (a
  builder `BLOCKED`/`INCOMPLETE` pause may legitimately leave the task
  worktree dirty — that recorded dirty state is what's compared against,
  not a requirement that it become clean).

State schema v1 (pre-dating these checkpoints and persisted run options)
cannot be resumed safely and is rejected outright rather than silently
migrated; start a new run instead.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# edit .env and fill in ARGO_API_KEY and FALDA_TOKEN (or your provider's
# equivalents) -- .env is gitignored and never committed
```

`opencode.json` is tracked and contains **no secrets** — it references
credentials with `{env:VAR}` interpolation, which OpenCode resolves from
the environment. `loop-supervisor` loads `.env` itself (via
`python-dotenv`) before starting `opencode serve`.

## Running

```bash
loop-supervisor run --project /path/to/integration/checkout
```

Useful flags: `--worktree-root`, `--max-tasks`, `--max-revisions`,
`--max-replans`, `--max-architect-retries`, `--role-timeout`,
`--require-decision-approval`, `--opencode-executable`.

The integration checkout must be a clean Git working tree on a real
branch (not detached `HEAD`) before a run starts.

## Bootstrapping a new project

Two ways to start a fresh project from this template:

**Copy to a new directory** (safe, non-destructive, works from anywhere):

```bash
loop-supervisor init --destination ../my-new-project
```

Copies only this checkout's Git-**tracked** files (via `git ls-files`) —
never `.git` itself, and never any untracked or ignored file (`.env`,
local caches, `.opencode/node_modules/`, stray secrets, etc.), regardless
of name. This requires the source to be a real Git checkout with a
readable index; it does not currently work from an installed wheel with
no `.git` present. The destination has no Git repository yet — `cd` in,
review `.env.example`, and `git init` when ready.

**Remove history in place** (destructive, for when you've already cloned
this template as the seed for a new repo and want to drop its history):

```bash
loop-supervisor init --in-place --yes
```

Requires a clean tree (or `--force`) and permanently deletes `.git` from
the current checkout. Files, including your local `.env`, are left in
place; no new repository is initialized automatically.

After bootstrapping either way, replace this README's project-specific
content and anything under `docs/` with your new project's actual goals
and design — the loop-planner, loop-builder, and loop-auditor agents all
read `README.md` and `docs/decisions/` as their canonical source of
truth.

## Testing and quality checks

```bash
pytest
ruff check .
ruff format --check .
mypy src
```

## Current limitations

- Headless only; no TUI yet.
- One task in flight at a time (no parallel task worktrees).
- No post-merge test run after a successful `ACCEPT` merge (builder and
  auditor test runs are relied upon instead).
- No lock preventing two supervisor runs against the same integration
  repository concurrently.
