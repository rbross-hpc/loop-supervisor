# `loop-supervisor.toml` is the project configuration channel

## Status

Accepted

## Context

Two upcoming features -- the supervisor provisioning a task worktree's
`.venv` before building (backlog item 32), and the supervisor running
project-defined verification commands and handing the results to the
auditor (backlog item 46) -- both need a project to say, once, what
commands to run and how long to allow them. Before this change,
loop-supervisor had no configuration file of its own and read no
environment variable of its own: every behavior-affecting setting was
either a CLI flag or a field of the persisted, immutable `RunOptions`
(see ADR 0006). `opencode.json` exists, but it belongs to OpenCode,
not loop-supervisor -- `doctor.py` only ever reads it, never writes
settings into it, and it is loaded per-invocation-directory by
OpenCode itself, a load model this project does not control (see ADR
0014's Consequences). `.env` is a credential channel (`python-dotenv`,
loaded once per CLI invocation into `os.environ` so OpenCode's
`{env:VAR}` interpolation can resolve it) -- nothing in this project
reads a behavior *setting* out of it, and repurposing it for one would
blur two channels that are currently cleanly separated.

Three formats/locations were considered for a new project-owned
config file:

1. `[tool.loop-supervisor]` in the project's own `pyproject.toml`.
   Rejected: the loop itself is language-agnostic (a generated project
   need not even be Python -- see ADR 0018), so tying configuration to
   a Python-specific build file is wrong for the general case, even
   though this repository's own dogfooding usage happens to be Python.
2. A key inside `opencode.json`. Rejected: that file belongs to
   OpenCode. Squatting a `loop-supervisor` key inside it would mean a
   loop-supervisor setting is silently at the mercy of OpenCode's own
   merge/precedence rules and per-invocation-directory loading
   semantics (ADR 0014), which have already caused one documented
   surprise (a worktree not inheriting a post-creation fix to the
   integration root's `opencode.json`).
3. A new, project-owned file: `loop-supervisor.toml` at the project
   root, committed like any other project file. TOML was chosen over
   JSON because `tomllib` is stdlib on Python >= 3.11 (this project's
   own minimum, per `pyproject.toml`), so parsing it costs zero new
   dependencies, and TOML's native support for comments and multi-line
   arrays reads better for a list of shell command lines than JSON
   would.

Option 3 was chosen. `_skeleton/.gitignore` had already reserved an
unclaimed `.loop-supervisor/` namespace (for possible future local
runtime artifacts); `loop-supervisor.toml` at the project root is a
sibling concept -- committed configuration rather than local state --
and does not conflict with that reservation.

An environment-variable channel (e.g. `LOOP_SUPERVISOR_VERIFY_CMD`)
was also considered and rejected: this project reads no env var of
its own today (`doctor.py`'s `_KNOWN_PROVIDER_ENVS` only *reports*
provider-credential presence, never consumes a value), and a list of
shell command lines does not fit naturally into a single environment
variable's value without inventing an escaping/delimiter convention.
Introducing a third configuration channel (file, flags, and env) for
no compensating benefit over two (file, flags) was judged not worth
the added surface.

## Decision

`loop-supervisor.toml`, parsed with stdlib `tomllib`
(`config.py`), holds two optional tables:

```toml
[provision]
commands = ["python3 -m venv .venv", ".venv/bin/pip install -e '.[dev]'"]
timeout = 600

[verify]
commands = ["ruff check .", "mypy src tests", "pytest -q"]
timeout = 900
```

A missing file is not an error: both `provision.commands` and
`verify.commands` default to an empty list, meaning both features are
off, matching today's behavior for every existing project exactly.
Unknown top-level tables and unknown keys within `[provision]`/
`[verify]` are rejected outright (`ConfigError`), matching the
strictness `RunOptions.from_dict` already applies elsewhere in this
project -- a typo in a config key fails loudly rather than being
silently ignored.

Each command entry is a shell-style command line, parsed with
`shlex.split` at the point of execution (`commands.py`) and run
directly via `subprocess.run`, **never** `shell=True`: shell
metacharacters in a configured command line are inert rather than
interpreted. `run_command`'s timeout is mandatory, unlike `git.py`'s
`_run` helper -- a project-configured command is untrusted-until-run
in a way a hardcoded `git` invocation is not.

Precedence is CLI flag > config file > off, the same precedence every
other run-behavior setting in this project already follows. New `run`
flags: `--config PATH` (override the default
`<project>/loop-supervisor.toml` location), `--provision-command CMD`
/ `--verify-command CMD` (repeatable; each *replaces* the config
file's corresponding list wholesale, never appends to it), and
`--no-provision` / `--no-verify` (force the corresponding list to
empty regardless of what the config file says). `provision_commands`,
`provision_timeout`, `verify_commands`, and `verify_timeout` are new
`RunOptions` fields, captured once at `start_new_run()` like every
other run-behavior setting; per ADR 0006, `resume` does not accept any
of the new flags and reconstructs these values entirely from the
persisted run, exactly like every existing `RunOptions` field.

Trust model: a project's `loop-supervisor.toml` is a committed file
the project's own maintainers control, at the same trust level as the
`.opencode/agents/*.md` files the supervisor already runs unattended.
This is the first feature where the supervisor itself executes
project-defined commands (previously it only ever invoked `git` with
supervisor-constructed arguments, or delegated command execution to an
OpenCode agent operating under its own permission grants) -- but it
introduces no new trust boundary beyond the one a project already
crosses by adopting loop-supervisor at all.

`config validate` (`doctor.py`) gained a `project_config` check that
parses the file and confirms each configured command's executable
token resolves on `PATH` (checked against the same PATH construction
`build_agent_env` uses, so a command that only resolves via the
project's own `.venv/bin` is not falsely reported as missing) -- but
it never executes a command, preserving the offline/fast preflight
contract ADR 0022 established.

## Consequences

- Adding four `RunOptions` fields required no schema migration,
  because ADR 0024 (squashing the state schema to a single current
  version) landed immediately before this change specifically to
  avoid that tax.
- A project wanting the venv-provisioning speed advantages of a faster
  installer (e.g. `uv venv && uv pip install -e '.[dev]'`, which uses
  hardlinks from a global cache rather than a fresh download/copy per
  worktree) gets that entirely through `[provision].commands` -- this
  project takes no dependency on `uv` or any other installer, and
  knows nothing about which one a project chooses.
- `loop-supervisor.toml` is read once per `run` invocation (not on
  `resume`), consistent with every other run-behavior setting; editing
  it mid-run has no effect on that run, only on the next `run` (this
  mirrors the existing `opencode.json` mid-run-edit gotcha documented
  in ADR 0014, and is called out for the same reason).
- The two follow-on features (worktree provisioning, supervisor-run
  verification) can now be implemented as plain consumers of
  `RunOptions.provision_commands`/`verify_commands` via `commands.py`,
  with no further configuration-surface work.
</content>
