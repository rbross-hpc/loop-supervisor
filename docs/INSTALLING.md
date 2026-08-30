# Installing loop-supervisor

This covers getting `loop-supervisor` itself installed and a brand-new
project running. If you're bringing the loop into an existing
repository instead, see [`docs/ADOPTING.md`](ADOPTING.md) — most of
step 1-2 below still applies, but the rest of the process is different.

## Prerequisites

- **Python >= 3.11.**
- **[OpenCode](https://opencode.ai)** on `PATH` — `loop-supervisor`
  spawns `opencode serve` and drives it over HTTP; it does not bundle
  or install OpenCode itself.
- **Git**, with worktree support (any reasonably current Git has this).
- An OpenCode-compatible model provider you can configure — see
  [Configuring a model provider](#configuring-a-model-provider) below.

Once you have something to check these against (Python installed, an
OpenCode binary, a project directory), `loop-supervisor config
validate --project <path> --json` verifies all of them at once and
tells you exactly which is missing — see
[Verifying your setup](#verifying-your-setup) below.

## 1. Install loop-supervisor

There is no PyPI release yet — install directly from Git:

```bash
pip install "loop-supervisor @ git+https://github.com/rbross-hpc/loop-tui-experiment.git"
```

or with [pipx](https://pipx.pypa.io/) if you want it isolated from any
particular project's own virtual environment:

```bash
pipx install "git+https://github.com/rbross-hpc/loop-tui-experiment.git"
```

Confirm it's on `PATH`:

```bash
loop-supervisor --help
```

If you're instead working from a source checkout of this repository
(e.g. to modify `loop-supervisor` itself), use an editable install:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## 2. Generate a new project

```bash
loop-supervisor init --destination ../my-new-project
```

This writes a small skeleton — agent definitions, `opencode.json`,
`.gitignore`, `.env.example`, a stub `docs/OBJECTIVE.md`, a starter
`README.md`, and a `pyproject.toml` that depends on `loop-supervisor`
— into a fresh, empty directory. See the README's [Bootstrapping a new
project](../README.md#bootstrapping-a-new-project) section for the
full set of `init` flags and what it does and doesn't copy.

```bash
cd ../my-new-project
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

## 3. Configure a model provider

The generated `opencode.json` deliberately ships with **no model
provider configured** (see [ADR
0023](decisions/0023-generated-projects-ship-no-provider-configuration.md))
— it resolves models from your own global OpenCode config
(`~/.config/opencode/opencode.json`), the same place any other
OpenCode project looks. If you already use OpenCode for anything else,
you likely already have this set up and can skip straight to step 4.

If you don't yet have a provider configured, add one to your global
config. As a concrete worked example, here is the Argo-compatible
provider this project's own development environment uses (adjust the
base URL, models, and environment variable name for your own
provider):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "argo": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Argo",
      "options": {
        "baseURL": "https://apps.inside.anl.gov/argoapi/v1/",
        "apiKey": "{env:ARGO_API_KEY}"
      },
      "models": {
        "Claude Sonnet 5": { "name": "Claude Sonnet 5" }
      }
    }
  }
}
```

Then set `ARGO_API_KEY` (or whatever your provider's real variable name
is) in the new project's `.env` file, which `loop-supervisor` loads
before starting `opencode serve`.

If the architect role should use a stronger model than your default,
re-run `init` with `--architect-model provider/model-id`, or edit the
`model:` line in the generated `.opencode/agents/loop-architect.md`
directly.

## 4. Verify your setup

```bash
loop-supervisor config validate --project . --json
```

This runs nine independent, offline checks — executables on `PATH`,
Python version, git repository/clean-worktree state, `opencode.json`
parses, `external_directory` permission covers the sibling
task-worktree parent directory, all four agent definitions present,
`.env` exists — and reports each one by name so you can fix the
specific thing that's wrong. It does **not** confirm your model
provider actually responds (see [ADR
0022](decisions/0022-config-validate-is-an-offline-preflight.md) for
why); the first real signal of a provider misconfiguration is the
first agent invocation in step 5.

Before starting a run, write the generated project's own
`docs/OBJECTIVE.md` (the first thing every agent role reads) and
replace its `README.md` placeholder description, then commit both —
along with any ADRs recording known design constraints — on a clean
branch. The generated project's own README has a "Before your first
run" checklist covering this.

## 5. Run it

```bash
loop-supervisor run --project . --max-steps 1
```

`--max-steps 1` performs exactly one phase transition and stops, so
you can see the planner's first proposed task before committing to a
full run. Once satisfied:

```bash
loop-supervisor run --project .
```

or `loop-supervisor tui --project .` for the interactive view. See the
main [README](../README.md) for the full run/resume/TUI model.

## Troubleshooting

Most early failures are one of:

- **`config validate` reports a failing check.** Fix the specific
  check named — don't guess past it. See step 4 above.
- **A permission denial appears in agent output** (e.g. `denied
  permission request ... ('external_directory')`). The task worktree's
  own `opencode.json` is stale, or `external_directory` only allows the
  project root and not its parent. See [ADR
  0014](decisions/0014-server-mode-permission-defaults-and-venv-path.md)
  and, if adopting into an existing project,
  `docs/ADOPTING.md`'s linked skill reference on this exact gotcha.
- **The planner immediately reports the project `COMPLETE`.** Usually
  means `docs/OBJECTIVE.md` is too vague or already looks satisfied —
  revisit it rather than assuming the project is actually done.
- **An agent role returns with no text output.** Almost always a
  downstream symptom of repeated permission denials exhausting that
  role's steps, not a distinct bug — check permissions first.
