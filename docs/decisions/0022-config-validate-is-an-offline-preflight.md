# `config validate` is an offline preflight, not a reachability check

## Status

Accepted

## Context

`loop-supervisor config validate` (`doctor.py`, `cli.py`'s
`cmd_config_validate`) exists to answer "is this project plausibly set
up for a run" before a human or an agent (notably the planned
`adopt-loop-supervisor` skill, which calls it as its very first step)
invests time in `run`/`resume`/`tui`. The natural next question once
executables-on-PATH and file-shape checks exist is whether it should
also confirm the configured model provider actually responds — the
single most common way a run fails five minutes in rather than at
startup.

## Decision

`config validate` deliberately makes no network call and never starts
`opencode serve`. Every check it performs is local: executable
presence/version (`git`, the configured `--opencode-executable`),
Python version, git repository/clean-worktree/attached-HEAD state,
`opencode.json` parses as JSON, `permission.external_directory` covers
the sibling task-worktree parent and descendants under it, all four
agent definition files are present, `.env` exists, and which of a known set of
provider-related environment variables are set (values are never
included in the report — only whether each is set).

A real reachability probe — resolving the configured provider, sending
one live completion request, confirming a 2xx response — would answer
a more useful question, but needs live credentials, a network path to
the provider, and tolerates the provider's own latency and rate
limits. Any of those can turn a sub-second, always-safe-to-run preflight
into a slow, occasionally-flaky one that costs real provider spend on
every invocation. That tradeoff is wrong for a command whose main job
is to run early, run often, and run for free.

## Consequences

- `config validate` can report `ok: true` and a run can still fail at
  the first actual agent invocation because the provider is
  misconfigured, unreachable, or rejects the request (bad API key,
  wrong base URL, a model name that doesn't exist under the configured
  provider). This is a known, accepted gap, not an oversight.
- If a reachability check is ever wanted, it should be a separate,
  explicitly-invoked check (e.g. `config validate --live` or a distinct
  subcommand) so the default, skill-invoked path stays fast and free —
  not a change to what `config validate` does by default.
- The env-var check only recognizes a fixed, small set of names drawn
  from this project's own skeleton (`ARGO_API_KEY`, `FALDA_TOKEN`,
  `FALDA_TENANT`); a project using a different provider's variable
  names gets no signal either way from that specific check, which is
  consistent with `config validate` not knowing or assuming which
  provider a project uses (see ADR 0023, "generated projects ship no
  provider configuration", which removes provider assumptions from
  the skeleton entirely).
