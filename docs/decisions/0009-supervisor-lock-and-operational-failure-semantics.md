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
on disk. Both front-ends share exactly one mechanism for this: `RunSession`
(`runtime.py`), via the `_LockLease` it owns internally, released only
once cleanup is confirmed. The headless CLI (`run_new()` / `run_resume()`)
and the TUI (`RunScreen`) each construct and drive their own `RunSession`
instance, but neither implements its own release decision — that logic
lives once, in `RunSession.close()`, tracked via `SessionState` rather
than by either caller nulling out a server reference. This is intentional:
releasing the lock while an OpenCode process may still be alive and
mutating the working tree would let a successor process start
concurrently mutating the same repository. An operator who encounters a
retained lock after a failed cleanup must verify no OpenCode process
survives before passing `--recover-stale-lock`.

The TUI's `RunScreen._cleanup_resources()` still exists, but it is no
longer a cleanup owner in its own right — it is a thin adapter with two
responsibilities `RunSession` cannot have, because it has no knowledge of
either: tearing down the TUI-owned SSE client before touching the session
(SSE failure must never block server/lock cleanup, and vice versa), and
translating `RunSession.close()`'s raising contract into the non-raising
`shutdown_clean` boolean that `LoopSupervisorApp`'s app-level exit-retry
loop polls. Ordering, retry, and the release decision itself belong
entirely to `close()`.

In the headless runtime, "confirmed" means a bounded retry sequence
(`_confirm_server_stopped()` in `runtime.py`, up to `_CLEANUP_ATTEMPTS`
calls to `server.stop()` on the exact same `OpenCodeServer` instance, with
bounded backoff between attempts) has produced at least one attempt that
returned without raising. The server handle itself is never discarded
between retries, and a later successful attempt fully confirms cleanup
regardless of how many earlier attempts failed transiently. This same
bounded retry is applied uniformly to a failed `server.start()`, to the
runner handoff and `supervisor.run()` (success or failure), and to the
ordinary post-run cleanup path — there is exactly one cleanup-confirmation
mechanism in the headless runtime, not one path for startup and a
different one for run completion.

`_CLEANUP_ATTEMPTS` bounds a single confirmation *attempt sequence*, not
the total number of `server.stop()` calls a session may make in its
lifetime — those are not the same thing. `RunSession.stop_server()` lets
a caller (typically a shutdown thread) force a bounded stop() attempt
ahead of `close()`, primarily to unblock an `advance()`/
`run_to_completion()` call that is stuck for up to its full role timeout,
by tearing down the HTTP transport underneath it. `stop_server()` never
releases the lock or touches the lease — only `close()` does either,
matching the single-owner rule above — so calling it is always safe in
the direction that matters: retaining the lock while the server has been
stopped is ADR-compliant, but releasing the lock before a *confirmed*
stop would not be.

The handoff into the `close()` that follows treats a `start_server()`
failure and a `stop_server()` call differently, and the difference is
deliberate rather than an oversight. A `start_server()` failure's
outcome is *consumed as-is* by the immediately following `close()`
(via `__exit__`) even if unconfirmed: retrying there would merely spend
the documented `_CLEANUP_ATTEMPTS` budget a second time for the same
failure sequence, since no time has passed for conditions to change. An
unconfirmed `stop_server()` outcome, by contrast, is *retried* by the
next `close()` rather than trusted as final: a blocked transition
usually unwinds in the interval between the two calls, so the later
attempt made by `close()` is the one likely to actually confirm cleanup.
This is precisely why a single session can call
`server.stop()` more than `_CLEANUP_ATTEMPTS` times in its lifetime: an
operator forcing repeated `stop_server()` calls, each retried up to
`_CLEANUP_ATTEMPTS` times, is the ordinary case, not an edge case. A
*confirmed* outcome, once recorded by either path, is always consumed
as-is and never retried or overwritten by a later failing attempt.

The TUI's own app-level exit retry (below) composes with this budget
rather than replacing it: each of its automatic retries calls
`RunScreen._cleanup_resources()`, which calls `session.close()`, which
may itself internally retry `server.stop()` up to `_CLEANUP_ATTEMPTS`
times. Where the TUI's pre-`RunSession` cleanup made exactly one
`server.stop()` call per app-level retry, it may now make up to
`_CLEANUP_ATTEMPTS`; a stuck cleanup that previously needed several
app-level retries to eventually confirm may now need fewer, since each
one already exhausts its own internal budget first.

### Concurrency and the quiescence barrier

`RunSession` (`runtime.py`) may be driven from more than one thread, as
the Textual TUI does (an `advance()`/`run_to_completion()` worker thread
plus a shutdown worker thread; see ADR 0008). Two primitives enforce the
lock's safety under that concurrency, and the distinction between them is
load-bearing:

- **`_state_lock`** (an `RLock`) guards the session's mutable bookkeeping
  and serializes `close()` against `stop_server()`. It is held across
  `server.stop()` deliberately: `OpenCodeServer.stop()` takes the
  server's own internal `_cleanup_lock`, so both cleanup paths must
  acquire the two locks in the same order, or `close()` and
  `stop_server()` could acquire them in opposite orders — an ABBA
  deadlock. It is just as deliberately **never** held across
  `supervisor.advance()`/`Supervisor.run()`: doing so would deadlock the
  very scenario `stop_server()` exists to handle, since a shutdown
  thread calling `stop_server()` to unblock a stuck transition would
  instead block behind that same transition.
- **`_advance_done`** (an `Event`) is a quiescence barrier, not a mutex.
  A transition clears it on entry and sets it in a `finally`; `close()`
  waits on it, unboundedly and before acquiring `_state_lock`, before
  touching the lease. This is what makes lock release safe: the
  repository lock may never be released while a transition may still be
  mutating Git or run state. The wait has no safe bound — that hazard is
  exactly what this ADR exists to prevent, and no Python thread can be
  force-killed — so `stop_server()` is the only sanctioned way to
  shorten it, by tearing down the transition's HTTP transport rather
  than by timing out the wait itself.

This barrier is a precondition on **both** of `RunSession`'s run-driving
entry points, not just the per-phase one. `Supervisor.run()`
(`supervisor.py:697-727`) loops calling `self.advance(state)` on the
*supervisor* directly to drive a run to a terminal phase; it never calls
back through `RunSession.advance()`, so `RunSession.run_to_completion()`
(the wrapper CLI/TUI code actually calls) clears and sets
`_advance_done` around that entire call itself, exactly mirroring
`advance()`'s own contract, or a concurrent `close()` would have nothing
to wait on for the whole duration of a multi-phase run.

Because both entry points release `_state_lock` for the duration of the
actual supervisor work, either can return to find that a concurrent
`close()` already claimed the session in the meantime (lock released,
session `CLOSED` or beyond). Any write-back performed after such a call
returns — restoring `SessionState.STARTED`, or writing the resulting
`RunState` back onto the session — must therefore be guarded
by a check that the session is still the same activity that started it
(`_state_is()`, consumed via `_restore_started_unless_closed()` and
`_store_run_state_unless_closed()` — the former restores `STARTED`, the
latter writes the run state back onto the session), never applied
unconditionally. The real result of the finished work is still returned
to the immediate caller either way; only writing it back into a session
that no longer owns it is forbidden. Skipping this guard previously let
a finishing transition resurrect a session that had already released its
lock into something that looked live and advanceable again.

Concurrent `advance()`/`run_to_completion()` calls on the same session
are not supported — a second call is rejected by the same state guard
that rejects it single-threaded — but `close()`, `stop_server()`, and
`abort_active_invocations()` are all safe to call from another thread
while one is in flight; `close()` is simply the one that waits.

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

**`RunSession.close()` takes the caller's declared outcome and the
exception to annotate as two independent parameters
(`close(*, outcome: _RunOutcome, error: BaseException | None)`), not one
combined value.** An earlier design folded both into a single nullable
`primary` parameter; that conflated three genuinely independent facts —
whether the run succeeded, whether the caller is holding an exception,
and which retained-lock explanation an operator should see — into one
value, which made "the run failed, but no exception is available to
report" inexpressible. That shape silently reported such a failure using
the success wording ("run completed but ..."), a regression that shipped
once already. `outcome` is the caller's own declaration, independent of
whether `error` is given: a caller may know a run failed after already
having caught and discarded the exception that proved it, and a cleanup
interrupt during an otherwise-successful run is still worth annotating
regardless of `outcome`. `_cleanup_prefix(outcome, *, startup_interrupted)`
is a pure function of exactly these two facts, with `startup_interrupted`
taking precedence — a startup interrupt is itself a kind of failure, so
its caller need not also pass `outcome=FAILED`.

This split is also what makes `close()` usable by a **detached caller**
— one whose `error` argument, whatever it is, is *not* the exception
currently propagating through this exact call — as distinct from
`__exit__`, where a body exception is still actively unwinding and
`error` is exactly that exception. `close()` reads `sys.exc_info()`
exactly once, at its own entry, to tell these two shapes apart, and the
distinction is by **identity against that read, not by lexical
position**: calling `close()` from inside an `except` block does not by
itself make a caller detached, because a caller can pass the exception it
just caught as `error` and still be the unwinding shape for as long as
`sys.exc_info()` still reports it (which it does for the duration of that
`except` block, even for calls made from deeper in the call stack). What
actually determines the shape is whether `error is` that ambient
exception: if so, a retained lock is reported by annotating it, since it
will already propagate on its own; if `error` is `None`, or is some other
exception the caller is holding after already having handled the real
one, a retained lock must instead be *raised*, or a detached caller
retrying `close()` would get a silent return instead of the raise it
needs to detect a failed retry.

This is precisely why `RunScreen._release_after_failed_init()` always
passes `error=None` rather than the caught exception, from either of its
two call sites: one runs from inside its caller's own `except Exception
as exc:` block, the other from a shutdown race that has no exception to
pass at all. For the first, passing `exc` there would make `close()`
treat it as the exception actively unwinding through this exact call
(since `sys.exc_info()` still reports it for the whole duration of a
synchronous call made from within that block), even though that `except`
block does not re-raise `exc`; it only renders a banner and returns
normally. `close()` would then report an unresolved cleanup by
annotating `exc` and returning silently — invisible, since nothing
further in that call chain re-raises `exc` — leaving `shutdown_clean`
`True` with the lock still on disk. `error=None` makes every call here a
genuine detached call, so `close()` raises on an unresolved cleanup
exactly as it should.
Deriving `outcome` from ambient exception state instead of taking it as
an explicit parameter would not work for this shape either: ambient state
answers "is something unwinding right now", not "did the operation this
call is concluding succeed", and a detached caller's `except` block means
those two questions can disagree.

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

**"Active `RunScreen`" means every lifecycle-owned screen, mounted or
detached — not just those currently in Textual's `_screen_stacks`.**
`LoopSupervisorApp` maintains its own authoritative registry
(`_owned_run_screens`), populated in `RunScreen.on_mount()` before any
resource acquisition and cleared only by `finalize_run_screen()` once a
screen is fully quiescent (`_init_done_event`/`_advance_done_event` both
set) and its last cleanup attempt confirmed clean
(`RunScreen.ready_to_finalize`). Unmounting a screen — whether via normal
shutdown, an unexpected pop, or a stack replacement — never removes it
from this registry by itself; `RunScreen.on_unmount()` instead starts (or
reuses) an app-owned automatic retry coordinator
(`ensure_cleanup_coordinator()`/`_run_screen_cleanup_coordinator()`) that
keeps requesting shutdown on a fixed interval, indefinitely, with no
interactive UI required, until that screen's cleanup is confirmed clean.
`_on_exit_app()` repeatedly drains this registry — ensuring every
currently-registered screen has a running coordinator, awaiting them, and
re-reading the registry — rather than taking a single snapshot of
`_screen_stacks`, so a screen registered while exit is already waiting
(or one that became detached-and-unclean) is never invisible to it. The
underlying Textual `_on_exit_app()` is invoked only once the registry is
completely empty. `on_unmount()` and the exit-drain loop share exactly
one coordinator per screen (`ensure_cleanup_coordinator()` is a no-op if
one is already running), so a detached-and-unclean screen is never
retried by two overlapping attempts at once.

Finalization is identity-safe: `finalize_run_screen()` only calls
`pop_screen()` when the screen being finalized is `self.screen` (Textual's
actual current top-of-stack screen) — never unconditionally. A screen
that finalizes late, after a different screen has since become active,
therefore only deregisters itself and never pops the unrelated active
screen.

### Operational failure vs terminal failure

Failures are classified into two categories:

**Operational failures** are transient and resumable. The supervisor
persists an `OperationalErrorRecord` (without tracebacks, with known
secrets redacted on a best-effort basis — see Consequences) into
`last_error` on `RunState`, sets `phase = "operational_failure"`, and
exits. The next `resume` or TUI retry replays from the `retry_phase`
recorded in the error record. Examples: OpenCode network/timeout errors,
ordinary Git/filesystem errors, merge conflicts requiring operator repair,
and a role's structured output exhausting its malformed-output retries or
failing a downstream identity check (`ContractError`, `kind="contract"`).

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
  or request payloads (no code path interpolates any of these into a
  persisted message). Environment-variable values and common credential
  formats (API keys, bearer tokens) are redacted on a best-effort basis
  by `_sanitize_message()`: it replaces literal values of secret-named
  environment variables (`*_KEY`, `*_TOKEN`, `*_SECRET`, etc., above a
  minimum length to avoid mangling unrelated text on a short/placeholder
  value) and known credential formats (`sk-...`, `ghp_...`, `Bearer ...`)
  wherever they appear in the message, then truncates, keeping both a
  leading and trailing portion so truncation cannot itself discard the
  terminating error while preserving an early banner. This is a
  best-effort backstop, not a guarantee: arbitrary repository content
  (e.g. a `git merge` conflict listing, or an agent's malformed output
  quoting a config value) can still appear verbatim, since it cannot be
  distinguished from legitimate diagnostic content. The record is
  persisted to a `0o600` file under `.git/loop-supervisor/runs/` (never
  committed, never transmitted) and should still be treated as
  sensitive.
- State schema v3 (additive migration from v2) carries the new fields
  without breaking existing runs.
- `RunSession` may be safely driven by multiple threads (a transition
  worker plus a shutdown worker), with the lock never released while a
  transition is in flight, at the cost of an unbounded wait in `close()`
  that only `stop_server()` can shorten.
- `close()`'s explicit `outcome`/`error` parameters make "failed with no
  exception in hand" and "detached caller re-invoking cleanup from an
  `except` block" both representable, closing the gap that previously
  let a failed run report success wording.
- The headless CLI and the TUI now share exactly one cleanup-ownership
  implementation (`RunSession.close()`/`_LockLease`); `RunScreen` no
  longer has its own release decision, only a thin adapter
  (`_cleanup_resources()`) that manages the TUI-owned SSE client and
  translates `close()`'s raise into the app's non-raising
  `shutdown_clean` boolean.
- The TUI's app-level exit retry now composes with the headless
  runtime's internal `_CLEANUP_ATTEMPTS` budget, so a single app-level
  retry may issue more `server.stop()` attempts than it did before this
  ADR's original TUI text was written; total attempts across a stuck
  shutdown sequence rose accordingly.
