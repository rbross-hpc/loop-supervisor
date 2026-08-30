# Generated projects ship no provider configuration

## Status

Accepted

## Context

`init`'s generated `opencode.json.tmpl` has, since the skeleton
existed, hardcoded a specific model provider: an Argo-branded
`@ai-sdk/openai-compatible` endpoint at an ANL-internal base URL, five
named Argo model IDs, and a Falda MCP server at an internal hostname.
`loop-architect.md` additionally pinned `model: argo/Claude Opus 4.8`.
None of this is generic to "a project that uses loop-supervisor" — it
is this project's own development environment, copied into every new
project regardless of what provider, models, or MCP servers that
project's own user actually has.

Outside that specific environment, a freshly-`init`ed project's
`opencode.json` cannot work at all: the base URL is unreachable, the
API key environment variable name is meaningless to any other
provider, and the architect's pinned model doesn't exist under any
provider the new user is likely to have configured. Nothing in `init`,
the generated files, or the README told a new user this was
environment-specific rather than a working default, so the failure
mode was silent misconfiguration rather than a clear error.

OpenCode itself already has a natural place for provider
configuration that doesn't require the skeleton to guess: a user's own
global `~/.config/opencode/opencode.json`, which every other OpenCode
project — not just ones bootstrapped by this tool — already relies on
for provider/model setup. Project-level config in OpenCode's merge
model overrides global, but is not required to duplicate it.

## Decision

The generated `opencode.json` (`opencode.json.tmpl`) no longer
contains a `provider` block or the Falda `mcp` block. It retains only
what is genuinely project-scoped: `lsp` and `permission`
(`external_directory` and `doom_loop`). Models resolve from whatever
the new project's own user has configured globally — the same
resolution path any other OpenCode project uses.

`loop-architect.md` no longer hardcodes `model: argo/Claude Opus 4.8`.
`init` gains an optional `--architect-model` flag substituted into a
new `__LOOP_SUPERVISOR_ARCHITECT_MODEL__` placeholder; when the flag is
omitted (the default), the placeholder line is dropped entirely and
the architect agent inherits whatever `default_agent`/global model the
project's own OpenCode config resolves to, same as every other role.
Because this promotes `loop-architect.md` from a byte-for-byte copy of
its live counterpart to a templated file, the existing drift test
(`test_cli_init.py`'s `_EXPECTED_IDENTICAL`/`test_skeleton_agents_
planner_and_architect_match_live_exactly`) is loosened from exact
byte-identity to identity modulo the `model:` frontmatter line, so
unintended drift elsewhere in the file is still caught.

`.env.example` is trimmed to a comment explaining that the specific
variable names it lists (`ARGO_API_KEY`, `FALDA_TOKEN`,
`FALDA_TENANT`) are this project's own development defaults, not a
requirement — a project using a different provider names its own
variables and references them from its own `opencode.json`.

## Consequences

- A freshly-`init`ed project has no working provider out of the box.
  This is intentional: the previous "working" default only worked
  inside one specific network and credential set, and failed silently
  and confusingly everywhere else. The new default fails by simply not
  configuring a model, which surfaces immediately (and is exactly what
  `config validate`'s env-var check and any downstream "no model
  configured" error from OpenCode itself are for).
- A user who wants the Argo/Falda setup this project itself uses can
  still get it — that setup now lives as a documented worked example
  in `docs/INSTALLING.md` (see the accompanying docs branch) rather
  than being silently baked into every generated project.
- This project's own `opencode.json` (unaffected by this ADR — `init`
  only changes what is *generated*, not this repository's own
  development config) still uses Argo/Falda directly, so this
  project's own loop continues to work unchanged.
- `--architect-model` is optional and generic — it takes any
  `provider/model-id` string, not an Argo-specific one — so a project
  using any provider can still give the architect role a stronger
  model than its default if it wants one, without loop-supervisor
  needing to know anything about that provider.
- Backlog items 34/35 (self-hosting, version/agent-prompt drift on
  upgrade) are unaffected by this change.
