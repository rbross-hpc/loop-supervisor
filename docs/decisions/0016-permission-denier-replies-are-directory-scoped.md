# Permission-denier replies must carry the ask's own directory

## Status

Accepted

## Context

The headless `PermissionDenier` (ADR 0014's follow-on, closing backlog
item 27) replies to every `permission.asked` SSE event with
`POST /permission/{requestID}/reply`, taking only `requestID` from the
event and no other identifying information. This route is not
implicitly scoped to the session that raised the ask: OpenCode 1.18.22
resolves it against whichever directory the request identifies (via a
`directory` query parameter), defaulting to the server's current
instance when that parameter is absent. This was not apparent from
reading the route's shape alone; the client SDK bundled in the OpenCode
binary defines this exact route as accepting `directory`/`workspace`
query parameters, and every call site in the built-in TUI's own
permission-reply code passes them (`directory: U.directory, workspace:
w.workspace.current()`).

The supervisor runs multiple isolated OpenCode instances concurrently
during a single run: the project root (used by `loop-planner`) and, for
every other role (`loop-architect`, `loop-builder`, `loop-auditor`), the
current task's own worktree. Each of these gets its own instance,
bootstrapped on demand — confirmed directly in OpenCode's own log by a
`creating instance` / `bootstrapping` pair appearing milliseconds after
an ask that originates in a worktree, for a *different* directory than
the one that raised the ask.

Because the denier's reply omitted `directory`, every ask that
originated inside a task worktree resolved against the wrong instance
and 404'd, while asks from the project root (i.e. only `loop-planner`)
succeeded. This was not caught before shipping ADR 0014's fix because:

- Every unit test against the fake OpenCode fixture used a bare,
  unconditional reply handler that accepted the reply regardless of any
  query parameter, so the missing scoping was invisible to the test
  suite.
- The one live proof run available before this fix happened to hit the
  bug on its very next ask after the fix first shipped, but the
  original `_reply_reject` discarded both the HTTP status and any
  exception on failure, collapsing every failure mode to a bare `False`
  and a generic "failed to deny" message. Diagnosing the actual cause
  required manually cross-referencing OpenCode's own
  `~/.local/share/opencode/log/opencode.log` for the request ID by
  hand, rather than the supervisor's own output explaining itself.

Root cause was confirmed by direct comparison of a succeeded and a
failed denial from that log:

| | Succeeded | Failed |
|---|---|---|
| Agent | `loop-planner` | `loop-auditor` |
| `cwd` at ask time | project root | task worktree |
| Immediately after ask | normal session flow | `creating instance`/`bootstrapping` for a *different* directory |

and reproduced deliberately and repeatedly against the real server (not
just inferred from the log) by resuming a paused real run at the
`auditing` phase — a worktree-scoped ask — first observing `HTTP 404`
with improved diagnostics in place, then observing a successful denial
once the fix below was applied, both against request IDs independently
confirmed in OpenCode's own log to have originated from a
`test-run-2-worktrees/*` `cwd`.

## Decision

1. `_reply_reject` now takes the ask's own `directory` — already present
   on every `permission.asked` event's envelope via
   `normalize_global_event` (`OpenCodeEvent.directory`) — and passes it
   as a `directory` query parameter on the reply POST. No new event
   subscription or lookup is required; this field was already being
   parsed and simply discarded.
2. `_reply_reject` now returns a small `_ReplyOutcome(accepted, detail)`
   instead of a bare `bool`. `detail` always describes the actual
   outcome (the HTTP status on a non-2xx response, or the exception
   type/message on a client-construction or transport failure) and is
   included in the "failed to deny" warning. This is deliberately
   independent of fix (1): even a correctly-scoped reply can still fail
   for other reasons (server restart, network blip, a future protocol
   change), and the failure must explain itself without requiring
   another manual cross-reference against OpenCode's own log.
3. The fake OpenCode fixture (`tests/fixtures/fake_opencode.py`) gained
   `FAKE_OPENCODE_SSE_PERMISSION_ASK_DIRECTORY` (lets a test simulate an
   ask from a specific instance) and
   `FAKE_OPENCODE_PERMISSION_REPLY_REQUIRE_DIRECTORY` (scopes the fake
   reply route exactly like the real server does, 404ing on a
   missing/mismatched `directory`). Without this, the fixture's
   previous unconditional acceptance of any reply meant no test could
   have caught this bug, or could catch a regression of it.

## Consequences

- Every `permission.asked` originating in a task worktree — i.e. every
  ask raised by `loop-architect`, `loop-builder`, or `loop-auditor`,
  which is most of a real run — is now reliably denied instead of
  silently failing to deny while still incrementing nothing and
  reporting nothing actionable. Confirmed by resuming the same paused
  real run that originally exposed the bug and observing the identical
  ask type (`external_directory`, from an auditor session in a task
  worktree) succeed where it previously 404'd.
- The "failed to deny" warning is now self-diagnosing (names the HTTP
  status or exception), so a *future* denier failure — of whatever
  cause — will not require repeating the manual log archaeology this
  one did.
- This is scoped narrowly to the denier's own reply path. It does not
  change `send_prompt`'s or any other OpenCode client call's directory
  handling (both already pass `directory` correctly, per
  `opencode.py:1167`, `:1229`); those were never in question. It also
  does not address backlog item 31 (routing `permission.asked`
  auto-denial through the TUI's own SSE connection), which is a
  separate consumer with its own directory-resolution context.
