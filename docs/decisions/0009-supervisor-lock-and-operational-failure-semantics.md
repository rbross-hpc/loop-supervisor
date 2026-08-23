# 0009 — Supervisor lock and operational failure semantics

## Status

Accepted

## Context

Multiple operator sessions (CLI run, CLI resume, TUI) could attempt to
mutate the same Git repository simultaneously. Concurrent mutation could
corrupt the integration branch or produce inconsistent run state. The
supervisor also needed a well-defined policy for transient failures that
occur mid-phase (network timeouts, Git errors, merge conflicts) so that
operators could safely restart without re-doing completed work or losing
diagnosable context.

## Decision

### Repository execution lock

A per-repository lock is stored at:

    <git-common-dir>/loop-supervisor/supervisor.lock

The lock is a mode-0600 JSON file with `schema_version`, `token`, `pid`,
`hostname`, `started_at`, `operation`, `run_id`, and `integration_path`.

Acquisition uses an atomic link(2)-based create-if-absent strategy. The
owner token prevents one process from releasing a successor's lock. The
lock is held for the entire mutating lifecycle including OpenCode shutdown.
Read-only operations (listing runs, loading a run for display) are
lock-free.

Stale-lock recovery is always explicit: the operator passes
`--recover-stale-lock`. A demonstrably dead local PID may be recovered
with this flag. Remote-hostname and malformed locks are never auto-recovered.

**Lock release requires confirmed OpenCode shutdown.** The lock is
released only after `OpenCodeServer.stop()` (or the TUI's equivalent
ownership-preserving cleanup) has been *confirmed* to succeed — not
merely attempted. OpenCode runs beneath a private Python launcher that is
the verified session/process-group leader and remains alive after the
direct server child exits. Startup verifies the anchor before permitting
it to spawn OpenCode. Shutdown sends TERM and KILL commands through an
owned pipe while that anchor is live; successful completion requires the
launcher to be reaped with a SIGKILL return status. The parent never
signals or probes a numeric PGID after anchor loss. A `stop()` that raises
(the process group, control client, launcher pipes, or stdout pump thread
could not be confirmed released) means the lock is deliberately retained
on disk, in both the headless runtime (`run_new()` / `run_resume()` in
`runtime.py`, via the `_LockLease` released only when cleanup is confirmed)
and the TUI (`RunScreen._cleanup_resources()`, which only releases the
lock once `self._server is None`). This is intentional: releasing the lock
while an OpenCode process may still be alive and mutating the working tree
would let a successor process start concurrently mutating the same
repository. An operator who encounters a retained lock after a failed
cleanup must verify no OpenCode process survives before passing
`--recover-stale-lock`.

In the headless runtime, "confirmed" means a bounded retry sequence
(`_confirm_server_stopped()` in `runtime.py`, `_CLEANUP_ATTEMPTS` calls to
`server.stop()` on the exact same `OpenCodeServer` instance, with bounded
backoff between attempts) has produced at least one attempt that returned
without raising. The server handle itself is never discarded between
retries, and a later successful attempt fully confirms cleanup regardless
of how many earlier attempts failed transiently. This same bounded retry
is applied uniformly to a failed `server.start()`, to the runner handoff
and `supervisor.run()` (success or failure), and to the ordinary
post-run cleanup path — there is exactly one cleanup-confirmation
mechanism in the headless runtime, not one path for startup and a
different one for run completion.

**Primary errors take precedence over cleanup errors.** Whenever a
primary operation (a run/resume failure, a startup failure) and a
secondary cleanup step (`server.stop()`) both fail, the primary error is
what propagates; the cleanup failure is attached as additional context
(e.g. appended to the exception message, or as a `PEP 678` note via
`add_note()` when the primary is a live exception object rather than a
freshly constructed message) rather than replacing it. This holds in both
the headless runtime and the TUI, and it holds uniformly across
`BaseException`, not only `Exception`: a `KeyboardInterrupt`/`SystemExit`
raised from `server.start()`, from `supervisor.run()`, or from the runner
handoff is never wrapped in `RuntimeError_` and never persisted as an
`OperationalErrorRecord` — its exact identity and traceback propagate via
a bare `raise`, with retained-lock/unresolved-cleanup guidance attached
only as a note if cleanup itself could not be confirmed. Symmetrically, a
`KeyboardInterrupt`/`SystemExit` raised by cleanup itself (i.e. by
`server.stop()` during one of the bounded retry attempts) is reported
structurally to the caller rather than being allowed to replace whatever
primary exception is already being handled; if there is no primary to
preserve (an otherwise fully successful run followed by unconfirmable
cleanup), that exact interrupt is what propagates, annotated with a
retained-lock note, rather than being converted into a `RuntimeError_`.

This precedence applies to the *complete* outcome of an operation, not
just its first failure point. In `OpenCodeServer.create_session()`,
`send_prompt()`, and `_abort_session_bounded()`, the primary outcome is
not decided until every stage of the request has been evaluated in
full — transport failure or success, HTTP status, JSON decoding,
response-shape/session-ID validation, and (for `send_prompt()`)
assistant-error and text/structured-output extraction. Only once that
complete outcome is known is the request-local `httpx.Client` closed,
via a dedicated daemon thread bounded by `_CLIENT_CLOSE_TIMEOUT_SECONDS`
so a hung or slow `close()` can never reintroduce an unbounded wait. A
close failure or timeout is attached to the already-decided primary
exception as a note (`add_note()`) and never replaces it, including when
the primary is itself a translated `httpx` exception carried as
`__cause__`, an `HTTPStatusError`-equivalent `AgentInvocationError`, or a
non-transport failure such as an assistant error discovered only after
full JSON decoding. If the request succeeds and only the close fails or
times out, that unconfirmed close is itself raised as
`OpenCodeCleanupError` (there is no primary to preserve). The same
precedence governs `OpenCodeServer.__exit__()`: a cleanup failure from
`stop()` — including `BaseException` subclasses such as
`KeyboardInterrupt`/`SystemExit` raised during cleanup itself — is
attached as a note to a pending body exception and never replaces it;
with no body exception, `stop()`'s own failure propagates unchanged.

The shared, long-lived control client tracked directly on
`OpenCodeServer` (as opposed to the short-lived, request-local clients
above) has its own bounded-close ownership state in `stop()`: at most
one `_BoundedCloseAttempt` is ever in flight for that client at a time.
A `stop()` call that finds an existing in-progress attempt for the same
client re-waits on it rather than starting a second concurrent
`close()`; a `stop()` that fails to even start a close (construction or
thread-start failure) retains the client for a later retry; a completed
close exception clears the attempt (permitting one retry) while
retaining the client; a completed close success clears both. `start()`
remains rejected while either the client or its close attempt is
unresolved, so a new lifecycle can never begin while a prior one's
cleanup is still outstanding.

**TUI app-level exit retries indefinitely until cleanup is confirmed
clean.** `LoopSupervisorApp._on_exit_app()` requests shutdown on every
active `RunScreen`, awaits completion, and — if any screen's
`shutdown_clean` is still False — retries automatically on a fixed
interval, forever. There is deliberately no overall exit timeout: the
underlying Textual shutdown sequence (and therefore process exit) must
never be allowed to proceed while the repository lock is held or an
OpenCode process may still be alive. The same retry is available
interactively via "q" / the "Return to runs" button after a failed
attempt.

### Operational failure vs terminal failure

Failures are classified into two categories:

**Operational failures** are transient and resumable. The supervisor
persists an `OperationalErrorRecord` (without tracebacks or secrets) into
`last_error` on `RunState`, sets `phase = "operational_failure"`, and
exits. The next `resume` or TUI retry replays from the `retry_phase`
recorded in the error record. Examples: OpenCode network/timeout errors,
ordinary Git/filesystem errors, merge conflicts requiring operator repair.

**Terminal failures** are non-recoverable policy violations. The supervisor
sets `phase = "failed"` and exits. Examples: revision/replan/architect
retry limits, internal invariant violations, unknown phases.

Tampered or schema-mismatched state is rejected on load without overwriting
the last good state.

### Durable side-effect phases

Before every non-idempotent mutation, the supervisor persists a durable
intent phase:

- `creating_worktree` — intent path/branch/base persisted before `git worktree add`
- `recording_decision` — target path and content hash persisted before ADR write
- `merging` — integration and task HEADs persisted before merge
- `cleanup_worktree` — entered after merge commit is recorded
- `cleanup_branch` — entered after worktree is removed

Each side-effect operation is written to be idempotent: resuming from a
partially completed phase reconciles against the persisted intent rather
than re-executing blindly.

## Consequences

- No two mutating supervisor processes can act on the same repository
  simultaneously.
- Stale locks from crashes require explicit operator acknowledgement to
  remove.
- Transient failures are recoverable by resume; operators see a clear
  error record with a recovery hint.
- Non-recoverable failures produce a clear `failed` state; no further
  resume is possible without starting a new run.
- The `OperationalErrorRecord` never contains tracebacks, HTTP headers,
  environment variables, or secrets.
- State schema v3 (additive migration from v2) carries the new fields
  without breaking existing runs.
