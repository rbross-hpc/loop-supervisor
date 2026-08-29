# OpenCode server and session lifetimes

## Status

Accepted

## Context

`RunSession` (`src/loop_supervisor/runtime.py`) owns exactly one
`OpenCodeServer` per run, and `OpenCodeServer.run_agent()` creates a
fresh OpenCode HTTP session for every agent invocation. Neither of
these lifetimes was previously documented anywhere but the module's
own docstring and its constant/method bodies — despite both being
concrete, load-bearing invariants that a change to `runtime.py` or
`opencode.py` could silently violate.

This gap surfaced while investigating two related questions: whether
the supervisor restarts the OpenCode server between steps (it does
not, and cannot), and whether never deleting an OpenCode session
constitutes a real operational problem (it is real, but not the one
initially suspected — see Consequences).

## Decision

**One `opencode serve` process per `RunSession`, started once, never
restarted.**

- The sole spawn point is `OpenCodeServer.start()`
  (`opencode.py:645`), called from exactly one place:
  `RunSession.start_server()` (`runtime.py:942`).
- `start_server()` only runs from `SessionState.READY`
  (`runtime.py:926`) and transitions to `STARTED` on success
  (`runtime.py:997`); a second call raises.
- `OpenCodeServer.start()` independently refuses to double-start if
  any prior lifecycle resource (`_owner`, `_pending_launcher`,
  `_client`, `_client_close_attempt`, `_stdout_thread`) is still set
  (`opencode.py:615-628`).
- The server is torn down exactly once, in `RunSession.close()`, via
  `_confirm_server_stopped()` (`runtime.py:1281`, `:200-232`), with a
  bounded retry (`_CLEANUP_ATTEMPTS = 3`) that always retries `stop()`
  on the same server instance — never `start()`.
- `RunSession.stop_server()` (`runtime.py:703-751`) exists as a
  one-way escape hatch to unblock a hung `advance()` call (its only
  caller is the TUI's shutdown escalation, `tui/app.py:1094`). It
  tears down HTTP transport so a blocked prompt unwinds; it does not
  release the lock and it never restarts the server.

**Why a restart is structurally prohibited, not just absent:** the
run's `_LockLease` is marked unreleasable immediately before
`server.start()` is attempted (`runtime.py:938`) and only becomes
releasable again once a stop is *confirmed*, not merely attempted
(`runtime.py:1304`). A mid-run restart would necessarily pass through
an unconfirmed-stop window in which a surviving child process could
still mutate the worktree while the supervisor believed the server
was down — precisely the hazard ADR 0009's lock semantics exist to
prevent. `_server_may_exist` (`runtime.py:939`) is a latch that is set
once, on the first `start()` attempt, and is never cleared for the
life of the `RunSession`.

**One OpenCode HTTP session per agent invocation, never reused, never
persisted.**

- `OpenCodeServer.run_agent()` creates a new session as its first
  action (`opencode.py:1350`) and never reuses an existing one.
- The `session_id` is a local variable in `run_agent`; it is
  registered in `_active_sessions` only for the duration of the call
  and unconditionally popped in the `finally` (`opencode.py:1358-1359`,
  `:1385-1387`). It never appears in `RunState` or anywhere else that
  would let a later step, or a resumed run, address the same session
  again.
- Consequently a malformed-output retry (`_parse_with_retry`,
  `supervisor.py:1513-1525`) does not resubmit to the failed session —
  it calls `run_agent` again, creating a second, independent session
  against the same still-running server.
- On a phase timeout, the response is `POST /session/{id}/abort`
  (`opencode.py:1289`), a best-effort cancellation — never a session
  delete and never a server restart.

## Consequences

- **Sessions are never deleted, and this is deliberate, not an
  oversight.** OpenCode 1.18.22 exposes `DELETE /session/{sessionID}`
  ("Delete a session and permanently remove all associated data,
  including messages and history"), but the supervisor never calls
  it. The `event` and `part` rows OpenCode stores per session are the
  forensic record of what an agent actually did during a phase —
  auto-deleting them on run completion would trade a small amount of
  disk for a real loss of debuggability on exactly the runs (failed
  ones) where that record matters most.
- **Session state lives on disk, in a machine-global SQLite database
  shared with interactive OpenCode use** (observed at
  `~/.local/share/opencode/opencode.db`), not in the server process's
  memory. This means the one-server-per-run lifetime above bounds any
  *in-memory* accumulation to a single run's duration, but does
  nothing to bound *disk* accumulation, which is cumulative across
  every run the supervisor has ever performed, forever, on that
  machine.
- Measured on one long-lived development machine: supervisor-created
  sessions (`title LIKE 'loop:%'`) accounted for 51 of 265 total
  sessions and roughly 22 MB of a 1.4 GB database — about 1.6% of
  the total, or roughly 116 KB per session. The remainder is
  interactive OpenCode usage unrelated to the supervisor. This is a
  slow, low-severity housekeeping concern, not a stability or memory
  concern, and is tracked separately (backlog item 43) rather than
  fixed here — this ADR documents the lifetime invariant that makes
  the accumulation pattern legible, not a remediation.
- Any future change that makes the server's lifetime shorter than "one
  per run" (e.g. restarting between phases to recover from a wedged
  process) must first change the lock-release ordering this ADR
  documents, and should record that change as an amendment here or in
  a superseding ADR, not as a silent behavior change in `runtime.py`.
- Any future change that makes an OpenCode session longer-lived than
  "one per invocation" (e.g. reusing a session across retries or
  across steps) needs its own decision: today's per-invocation
  session boundary is also what keeps a malformed-output retry's
  prompt history from leaking into the next attempt.
