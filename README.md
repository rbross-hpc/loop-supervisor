# loop-supervisor

A headless supervisor that drives an [OpenCode](https://opencode.ai) agent
loop — planner, optional architect, builder, auditor — over Git worktrees,
so an LLM coding agent can incrementally implement a project one bounded
task at a time, with an independent review step before anything merges.

`loop-supervisor init` bootstraps a new project that **depends on**
`loop-supervisor` as an installed package — it does not vendor a copy
of this repository's own source, tests, or history (see
[ADR 0018](docs/decisions/0018-bootstrap-generates-a-dependent-skeleton-not-a-vendored-copy.md)).
See [`docs/INSTALLING.md`](docs/INSTALLING.md) for the full install and
first-run walkthrough, or [`docs/ADOPTING.md`](docs/ADOPTING.md) if
you're bringing the loop into a project that already has code.

## How the loop works

```
planner --> [architect] --> builder --> [verify] --> auditor --> supervisor merge
   ^                                                     |
   +------------------- REPLAN -------------------------+
                           |
                        REVISE --> builder (same task)
```

`[verify]` runs only if `[verify].commands` is configured
(`loop-supervisor.toml`, see ADR 0025); otherwise the builder's commit
goes straight to the auditor, unchanged from before this feature
existed.

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
  claimed to satisfy. If `[verify].commands` is configured, its prompt
  also includes the supervisor's own independently-run verification
  result (not the auditor's or builder's claim) — see ADR 0025.

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

## Commit messages

A subject line alone (e.g. `fix: reject symlinked lock and state
storage`) is not enough — `git log`/`git blame` should be able to
answer "why was it done this way" without also opening the backlog
entry written about it afterward. Every commit's body should cover:

- **What changed and why**, in enough detail that the reasoning
  survives independently of any planning document that motivated it.
- **The failing-first verification evidence** for any new test: which
  prior commit it was verified against, what it failed with there, and
  why that failure is the right one (see "Testing discipline" below).
- **Any known limitations or deviations** from the task's original
  scope — do not let these surface for the first time in the diff.

Never amend a commit to fix a mistake found later; make a new,
corrective commit instead (see this project's own git-workflow
convention for the reasoning).

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
  Answering `replan` sends the task back to the planner instead. After
  `--max-builder-guidance-attempts` consecutive non-`COMPLETE` results
  (default 3), the supervisor stops re-invoking the builder and instead
  escalates: answer `replan` to send the task back to the planner, or
  `abandon` to fail the task (the worktree and branch are preserved for
  inspection, same as any other terminal failure). An operator `replan`
  answer, whether from ordinary guidance or from this escalation, counts
  against `--max-replans` the same as an auditor `REPLAN` does — see
  [ADR 0030](docs/decisions/0030-builder-guidance-circuit-breaker.md).
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

A saved run whose `schema_version` does not match the currently
installed version is rejected outright rather than silently migrated
or repaired; start a new run instead. There is deliberately no
migration path between schema versions (see backlog item 30's
resolution note in `docs/plans/2026-08-22-post-lifecycle-fix-backlog.md`
and `state.py`'s comment above `STATE_SCHEMA_VERSION`).

## Setup

This section covers working on `loop-supervisor` itself (this
checkout). If you want to install `loop-supervisor` and bootstrap a
new project with it, see [`docs/INSTALLING.md`](docs/INSTALLING.md)
instead — it also covers prerequisites (Python, OpenCode, a model
provider) this section assumes are already in place.

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

`loop-supervisor config validate --project .` checks whether a project
(this one, or any generated one) is plausibly set up for a run —
executables on `PATH`, a clean git worktree, `opencode.json` parses,
permissions cover the sibling task-worktree directory, agent
definitions present, `.env` exists — before you try to start one.

This checkout's own `loop-supervisor.toml` (see "Running" below)
provisions every task worktree with its own independent `.venv`
before a builder touches it — under `loop-supervisor run`, a
self-hosted run never shares this checkout's own venv with a task's,
so nothing a builder does inside its worktree (including an editable
`pip install`) can affect this checkout's own environment. See
backlog item 48 for the incident that motivated this.

**This protection is `run`-only.** `resume` reloads `provision_commands`
from the run's own persisted state rather than re-reading
`loop-supervisor.toml`, so a run started before provisioning was
configured stays unprotected across every future resume. `tui` never
reads `loop-supervisor.toml` at all — its `_DEFAULT_OPTIONS` hardcode
provisioning off — so a TUI-driven run remains fully exposed to the
item-48 hijack regardless of this file. See backlog item 52.

## Running

```bash
loop-supervisor run --project /path/to/integration/checkout
```

Useful flags: `--worktree-root`, `--max-tasks`, `--max-revisions`,
`--max-replans`, `--max-architect-retries`,
`--max-builder-guidance-attempts`, `--role-timeout`,
`--require-decision-approval`, `--opencode-executable`,
`--recover-stale-lock`, and the mutually exclusive `--step`/
`--max-steps` (bound how many phase transitions this invocation
performs before stopping; `--step` is shorthand for `--max-steps 1`).

Two optional, off-by-default features are configured via
`loop-supervisor.toml` at the project root (or `--config PATH`), with
CLI flags taking precedence over the file: `[provision].commands`
(run once, in the new task worktree, before building) and
`[verify].commands` (run after building, before auditing, with
results shown to the auditor). See ADR 0025 for the config format and
`--provision-command`/`--no-provision`/`--verify-command`/
`--no-verify` for the equivalent flags.

The integration checkout must be a clean Git working tree on a real
branch (not detached `HEAD`) before a run starts.

## Textual TUI

```bash
loop-supervisor tui --project /path/to/integration/checkout
```

Opens a terminal UI with:

- **Run browser** — lists saved runs (phase, task, last error) and a
  "Start new run" action. No lock is acquired while browsing.
- **Run screen** — shows durable supervisor state and best-effort live
  OpenCode activity in separate, clearly-labelled panes.
- **Pending-input panel** — presents the appropriate controls (multiline
  text, approve/reject buttons, retry) for each operator-input scenario.

The TUI acquires the repository lock before starting execution and
releases it on exit. Pass `--recover-stale-lock` if a previous run
crashed and left a stale lock from a demonstrably dead local process.

### Durable vs live status

The left pane ("Durable supervisor state") shows the authoritative
`RunState` from disk: phase, run ID, task, revision/replan counters,
and the last error record. This never changes unless `advance()` commits
a transition.

The right pane ("Live OpenCode activity — ephemeral") shows real-time
SSE telemetry: active sessions, assistant text tail, active tools, and
file edits. If SSE disconnects, a notice is shown and the durable pane
remains fully functional. Live state is never used to infer supervisor
phase.

### Repository lock

Only one mutating supervisor session (run, resume, or TUI) can act on a
given repository at a time. The lock is stored at:

    <git-common-dir>/loop-supervisor/supervisor.lock

If a crash leaves a stale lock from a dead local process, pass
`--recover-stale-lock` to remove it and retry. Remote-hostname and
malformed locks are never auto-recovered.

### Operational failure and retry

Transient failures (network errors, Git errors, merge conflicts) are
persisted as `operational_failure` with a structured error record and a
`retry_phase`. The TUI shows a Retry button; `loop-supervisor resume`
also retries from the recorded phase.

Non-recoverable failures (policy limits, invariant violations) set
`phase = "failed"`; no further resume is possible. Start a new run.

### Merge-conflict repair

The supervisor is the sole writer of the integration branch during a
normal run: every task branch is created from, and merged back into,
the integration HEAD it itself observed, so a conflict cannot arise
from the loop's own operation alone. It can only happen if something
external changes the integration branch while a run is in progress
(e.g. an operator manually committing to it, or a second supervisor
run pointed at the same repository). If a `--no-ff` merge conflicts,
the supervisor aborts the merge, records the conflict as an
operational failure requiring repair, and stops. Resolve the conflict
manually in the integration worktree, then resume (or click Retry in
the TUI).

## Bootstrapping a new project

`loop-supervisor` is a dependency your new project installs, not
something you fork or vendor. `init --destination` writes a small,
project-specific skeleton — agent definitions, `opencode.json`,
`.gitignore`, `.env.example`, a stub `docs/OBJECTIVE.md`, a starter
`README.md`, and a `pyproject.toml` that depends on `loop-supervisor` —
into a fresh directory:

```bash
loop-supervisor init --destination ../my-new-project
```

By default the generated `pyproject.toml`'s `loop-supervisor` dependency
points at this project's own Git URL; pass `--loop-supervisor-git-url`
to point at a fork, `--project-name` to override the name derived from
the destination directory, and `--architect-model provider/model-id`
to pin the architect role to a specific model (omitted by default — it
inherits the project's own default model). The destination must not
already exist non-empty, and has no Git repository yet — `cd` in,
review `.env.example`, and `git init` when ready. See
[`docs/INSTALLING.md`](docs/INSTALLING.md) for the full walkthrough,
including installing `loop-supervisor` itself first.

The generated `opencode.json` ships with no model provider configured
— see [ADR
0023](docs/decisions/0023-generated-projects-ship-no-provider-configuration.md)
and `docs/INSTALLING.md`'s [Configuring a model
provider](docs/INSTALLING.md#3-configure-a-model-provider) section.

If you're bringing the loop into a project that already has code
rather than starting fresh, `init` isn't the right tool — see
[`docs/ADOPTING.md`](docs/ADOPTING.md) instead.

This is a plain filesystem write with no dependency on `.git` or on
being run from a source checkout at all: it works the same way whether
`loop-supervisor` itself is an editable install or a real installed
package (see [ADR
0018](docs/decisions/0018-bootstrap-generates-a-dependent-skeleton-not-a-vendored-copy.md)).

The generated project never receives this repository's own source,
tests, ADRs, or working plans — only the small set of files a new
project actually needs. After bootstrapping, write `docs/OBJECTIVE.md`
and replace the generated `README.md`'s placeholder content with your
new project's actual goals and design; see "Handing off from a
standalone session" below.

## Handing off from a standalone session

A run's planner is given no objective by the supervisor itself — its
first prompt is literally "Determine the next unit of work." All scope
comes from what the agents are told to read. To hand a project to the
loop from an interactive, standalone OpenCode session:

1. In the standalone session, write or update `docs/OBJECTIVE.md` with
   the project's current objective — what you actually want built, in
   plain language. This is the one file every agent role reads first.
2. Record any accompanying design decisions as ADRs under
   `docs/decisions/` (see [Design decisions and the
   architect](#design-decisions-and-the-architect) above for the
   expected ADR shape), and any working notes under `docs/plans/`.
3. Commit these on a clean branch, then start the loop:

   ```bash
   loop-supervisor run --project /path/to/integration/checkout
   ```

The planner will read `docs/OBJECTIVE.md` on every invocation, so
updating it between runs (or between tasks, via a new commit on the
integration branch) is the supported way to redirect an in-progress
loop without restarting it. There is currently no `--objective` CLI
flag or `RunState` field — see [ADR
0017](docs/decisions/0017-objective-channel-is-a-tracked-file.md) for
why, and what would supersede this.

## Testing and quality checks

```bash
pytest
ruff check .
ruff format --check .
mypy src tests
```

`mypy` covers both `src` and `tests` in a single invocation. Checking
both roots together lets mypy compare test fakes against the real
types they stand in for, which is a stronger check than checking
either root alone — see "Patch module attributes with
`monkeypatch.setattr`" below for the one pattern this combined check
is sensitive to.

### Testing discipline

These recurred across multiple review cycles in this project's own
history, each costing a full audit pass to catch. Follow them up front
instead:

- **A test claiming to pin a historical defect must be verified failing
  against that defect's actual prior commit, not a hand-written
  injection.** Do this in your own task worktree — it persists
  unchanged through verification, auditing, and merge, so it is
  already an isolated trial environment; do not create a second
  worktree for this. Back up the file(s) the fix touched (e.g. `cp
  src/foo.py /tmp/foo.py.bak`), overwrite them with the pre-fix
  version (`git show <commit-before-the-fix>:path/to/file.py >
  path/to/file.py`), run the new test and confirm it fails, then
  restore from your backup (`cp /tmp/foo.py.bak src/foo.py`) — never
  `git checkout --` on uncommitted work, which discards it instead of
  restoring it. Confirm `git status` is clean again before reporting
  COMPLETE: the supervisor rejects a dirty worktree at that point. A
  hand-written injection only proves the test detects *your*
  injection, which is frequently a different (and often more broken)
  bug than the one that actually shipped.

  If a probe genuinely needs a different commit checked out entirely
  (not just one or two files swapped), a temporary worktree is the
  right tool — but remove it (`git worktree remove`) before reporting,
  rather than leaving it registered for the supervisor or a future
  session to find.
  - **Mandatory probe self-check:** before trusting the trial run's
    result, assert something that is only true of the *old* code being
    tested (e.g. `hasattr(module, "_SomeSymbolAddedByTheFix") is False`).
    An editable install (`pip install -e`) can silently resolve imports
    back to the live repo instead of the swapped-in old source, which
    once produced a false "all tests pass" result that looked like a
    successful pin.
  - **Never run `pip install -e` (editable or otherwise) from inside
    a task worktree while its own `.venv` is missing or incomplete.**
    `PATH` falls back to the integration checkout's `.venv/bin` when a
    worktree has none of its own, so `pip` there is the *integration
    checkout's* pip: an editable install's `.pth` file embeds an
    absolute path, so this silently repoints the integration
    checkout's own install at the worktree's `src` and breaks it the
    moment that worktree is later removed. Confirm `.venv/bin/pip`
    resolves inside your own worktree first (`which pip`); if a
    project's `loop-supervisor.toml` provisions one automatically, use
    it rather than reinstalling by hand.
- **Self-authored injections only prove the test detects the
  injection** — not that it detects the real defect. If a bug must be
  injected to exercise a test before the real fix exists, treat that as
  a provisional check only, superseded by the prior-commit verification
  above once the fix is real.
- **Never re-implement the guard inline inside a test.** A test that
  duplicates the production guard's logic (e.g. re-checking a state
  condition itself, or calling an API shape the old code already
  handled correctly) can pass against both fixed and unfixed code,
  proving nothing. Call the real method/path being verified.
- **Verify every new test failing-first.** Confirm it fails before the
  fix and passes after. A "wrong-reason pass" — e.g. a nominally
  concurrent test that runs synchronously and passes regardless of the
  race — is caught by watching for suspicious signals like uniform
  runtime rather than by the assertion alone.
- **Prefer `pytest.raises(X, match=...)` for new tests, especially any
  claiming to pin a specific defect.** A bare `pytest.raises(X)` can
  pass on a wrong-cause exception of the same type; this is not yet a
  consistent convention across the existing suite, but it should be
  followed going forward rather than copied from surrounding bare
  examples.
- **Assertions are additive only.** `git diff <base> -- tests/ | grep
  "^-" | grep assert` should be empty for any change claiming to be a
  pure addition. A test needing a change beyond a declared,
  pre-authorised surface should stop and be reported rather than edited
  around silently.
- **Patch module attributes with `monkeypatch.setattr` (or
  `pytest.MonkeyPatch()` where no fixture is in scope), not direct
  assignment.** `rt.GitRepo = FakeGitRepo` type-checks clean when
  `tests/` is checked in isolation, but fails once `src` and `tests`
  are checked together in the same `mypy` invocation, because mypy can
  then see `GitRepo`'s real type and compare it against the fake —
  `monkeypatch.setattr` accepts a bare attribute-name string, so this
  comparison never happens. Because the failure was invisible under
  the split-invocation gate this project ran for a while, this exact
  pattern accumulated to roughly 250 errors in `tests/test_runtime.py`
  alone before being converted; write new patches the
  `monkeypatch`/`MonkeyPatch()` way from the start rather than
  discovering the gap the next time the gate tightens.

## Current limitations

- One task in flight at a time (no parallel task worktrees).
- No post-merge test run after a successful `ACCEPT` merge (the last
  pre-merge verification, if configured, plus the auditor's own
  inspection, are relied upon instead).
- Cancellation during an active OpenCode invocation is cooperative
  (abort request sent); no guarantee of immediate termination.
- Diff browsing and full log inspection are not yet in the TUI.
- Automatic merge-conflict resolution is out of scope; operator repair is
  always required.
