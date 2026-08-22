# Plan: second-round lifecycle fixes (fresh independent audit, NO-GO)

Branch: `feature/tui-vertical-slice`

Context: a fresh, independent read-only lifecycle audit (distinct from the
audit that produced `2026-08-22-post-lifecycle-fix-backlog.md`) found five
release-blocking defects. This plan captures the fix design for all five
and tracks execution status. See `docs/decisions/0009-supervisor-lock-and-
operational-failure-semantics.md` for the accepted lock/cleanup contract
these fixes must uphold and extend.

## Blockers

1. **TUI startup-failure deadlock** — successful cleanup after a failed
   `_do_initialize_locked()` sets `shutdown_clean=True` without signaling
   `_shutdown_complete_event`; a later app exit or "q" press waits forever
   on an event nobody will set.
   `src/loop_supervisor/tui/app.py:471`, `:852`, `:1149`.

2. **Detached-screen ownership loss** — an unexpectedly unmounted, unclean
   `RunScreen` disappears from `_screen_stacks`, so `_on_exit_app()` can no
   longer discover it and may proceed to base Textual exit while it still
   owns a server/lock.
   `src/loop_supervisor/tui/app.py:1055`, `:1143`.

3. **Timeout precedence still breaks** — every `client.close()` in
   `opencode.py` is synchronous and unbounded; it can hang past a request
   timeout, or raise from a `finally` and replace an already-selected
   `PhaseTimeoutError`.
   `src/loop_supervisor/opencode.py:460`, `:506`, `:554` (and shared-client
   close in `stop()` at `:349`).

4. **OpenCode descendants are not owned** — `OpenCodeServer` spawns with an
   ordinary `Popen` (no new session/process group) and `stop()` only
   signals/waits on the direct child. A descendant can survive `stop()`
   returning successfully, after which the repository lock is released.
   `src/loop_supervisor/opencode.py:174`, `:355`, `src/loop_supervisor/
   runtime.py:357`.

5. **Headless cleanup gaps** — `run_new()`/`run_resume()` catch only
   `OpenCodeError` from `server.start()`, so other exceptions (plain
   `OSError` subclasses, unexpected errors) bypass `_startup_failure()`'s
   cleanup/persistence. Cleanup is attempted once; failures are discarded
   rather than attached to the primary exception; the in-memory `server`
   handle is lost on unwind, eliminating further in-process retry.
   `src/loop_supervisor/runtime.py:177`, `:182`, `:252`.

## Fix design (in execution order)

### Step 1 — Process-tree ownership (blocker 4)

- Spawn OpenCode with `start_new_session=True` in
  `OpenCodeServer.start()`; the project is already POSIX-only (`fcntl`
  advisory locking in `locking.py`), so a POSIX-only session/process-group
  primitive is acceptable for this round.
- Capture and verify the process group id (`os.getpgid(pid) == pid`)
  right after a successful spawn; never derive a group id later from a
  possibly-reused pid.
- Rework `stop()` to signal the whole group: `os.killpg(pgid, SIGTERM)`,
  bounded wait, escalate to `os.killpg(pgid, SIGKILL)`, bounded wait
  again.
- Treat `ProcessLookupError`/`ESRCH` as "already gone" (idempotent
  success); treat `PermissionError`/`EPERM` and a still-alive group after
  the kill-wait as unresolved ownership — retain the handle for retry, do
  not clear it.
- Never signal a group id that does not match the verified invariant
  (defense against ever calling `killpg` on the supervisor's own group).
- Extend `tests/fixtures/fake_opencode.py` to optionally spawn a real
  descendant (and a SIGTERM-resistant variant) so tests can assert actual
  process-tree absence, not just direct-child `poll()`.
- Add tests: normal descendant terminated with the group; descendant
  ignores SIGTERM and group escalates to SIGKILL; leader exits early but
  descendant survives and is still reaped; group-signal failure retains
  ownership and a retry succeeds; idempotent stop against an
  already-dead group.

### Step 2 — Bounded, precedence-safe HTTP client cleanup (blocker 3)

- Add a private bounded-close helper (`_CLIENT_CLOSE_TIMEOUT_SECONDS`,
  short — e.g. 1s) that runs `client.close()` on a dedicated daemon
  thread and waits at most that bound; never use `ThreadPoolExecutor`
  (its non-daemon workers can block interpreter exit).
- Apply it at every `client.close()` site: `create_session()`,
  `send_prompt()`, `_abort_session_bounded()`, and the shared client in
  `stop()`.
- Restructure each call site so a primary request/timeout/HTTP-status
  exception is preserved via `except ... as primary: ...; raise` with the
  close failure attached via `add_note()`, never substituted.
- If the request succeeds but close does not confirm within the bound,
  raise `OpenCodeCleanupError` (no prior primary exception to preserve).
- Track shared-client in-progress close state on `OpenCodeServer` so a
  retried `stop()` does not start a second concurrent `close()` against
  the same client; it should re-check/re-wait instead.
- Add tests: throwing/hanging close after a session-creation timeout;
  throwing/hanging close after a prompt timeout (assert `run_agent()`
  still aborts and the observer receives the exact original timeout);
  throwing/hanging close after abort's own request timeout; hanging
  shared-client close during `stop()` still allows process-group
  termination to proceed and reports `OpenCodeCleanupError`; retried
  `stop()` does not invoke concurrent `close()`.

### Step 3 — Headless cleanup completeness (blocker 5)

- Catch `BaseException` (not just `OpenCodeError`) from `server.start()`
  once the lease is unreleasable, in both `run_new()` and `run_resume()`.
- Normalize all `subprocess.Popen` `OSError` subclasses (not just
  `FileNotFoundError`) to `ServerStartupError` in `OpenCodeServer.start()`.
- Re-raise `KeyboardInterrupt`/`SystemExit` unchanged after attempting
  cleanup; do not wrap them in `RuntimeError_`.
- Add bounded cleanup retries (small fixed attempt count with backoff)
  shared by startup failure and normal run-completion cleanup; mark the
  lease releasable only once the process tree is confirmed gone.
- Preserve the primary exception and attach unresolved
  cleanup/lock-release details as notes instead of silently discarding
  them (`_run_and_stop()`'s `except Exception: pass` on cleanup failure).
- Add tests: non-`FileNotFoundError` spawn failures are normalized and
  persisted; unexpected post-spawn `BaseException` during `start()` is
  cleaned up and reported; `KeyboardInterrupt` during `supervisor.run()`
  propagates unchanged with lock-retention notes on unresolved cleanup;
  transient cleanup failure followed by a successful retry releases the
  lock without operator action.

### Step 4 — TUI ownership registry (blocker 2)

- Add an app-level strong registry of lifecycle-owned `RunScreen`
  instances, registered at the start of `on_mount()` before any resource
  acquisition.
- Never deregister on unmount; deregister only once init/advance are
  quiescent and `shutdown_clean` is true.
- Make `_on_exit_app()` repeatedly drain the registry instead of taking a
  single `_screen_stacks` snapshot.
- Add automatic retry for detached-but-unclean screens (no interactive UI
  remains for them).
- Finalize by screen identity: pop only if that exact screen is still the
  active one; otherwise just deregister.
- Add tests: detached unclean screen remains registered and retried by
  app exit; exit race between stack removal and `on_unmount()`; detached
  clean shutdown does not pop an unrelated newly-pushed screen; detached
  lock-release-only failure remains retried.

### Step 5 — Per-attempt shutdown signaling (blocker 1)

- Replace the single reusable `_shutdown_complete_event` with a
  per-attempt handle (generation + its own `threading.Event`).
- `request_shutdown()`/`_maybe_start_shutdown_attempt()` returns: the
  existing in-flight attempt, a newly started attempt, or "already
  clean" (nothing to wait for) — synchronized under
  `_shutdown_attempt_lock`.
- Callers (`_on_exit_app()`, `"q"`, "Return to runs") never await an
  event unless they hold a real attempt handle for it.
- Failed-init cleanup that reaches "already clean" finalizes/dismisses
  directly rather than relying on a shutdown-worker-only completion path.
- Add tests: app exit after clean startup-failure cleanup completes
  (no deadlock); "q"/"Return to runs" work after clean startup-failure
  cleanup; distinct attempt generations don't cross-signal; app exit
  requested mid-failed-init-cleanup waits correctly and then proceeds.

### Step 6 — Documentation and full verification

- Update ADR 0009: OpenCode shutdown means full process-group
  termination, not just the direct child; "active `RunScreen`" means
  every lifecycle-owned screen (mounted or detached), not just those in
  `_screen_stacks`.
- Run focused suites (`test_opencode.py`, `test_runtime.py`,
  `test_tui_app.py`), then full `pytest -q` three times, `ruff check .`,
  `ruff format --check .`, `mypy src`, `git diff --check`.
- Obtain a fresh, independent, read-only lifecycle audit; commit only on
  a **GO** verdict.

## Execution status

- [x] Step 1 — process-tree ownership (blocker 4). Remediation implemented
      and independently audited GO. A Python launcher is started as a
      verified session/process-group leader before it is permitted to spawn
      `opencode serve`; it remains alive after child exit and accepts TERM/KILL
      commands over an owned pipe. The parent never signals or probes a bare
      PGID after anchor loss. Startup and shutdown share one lifecycle lock,
      partial startup retains a pending launcher handle, unresolved cleanup is
      retryable, and ownership clears only after a sent KILL command and a
      reaped launcher whose return code confirms SIGKILL. Tests cover real
      descendants, TERM-resistant descendants, direct-child exit with anchor
      persistence, concurrent starts/stops, restart, command-write retry,
      production launcher invocation, and no parent-side PGID probing.
- [x] Step 2 — bounded/precedence-safe HTTP client cleanup (blocker 3).
      Added `_BoundedCloseAttempt`/`_close_bounded`/
      `_close_request_local_client` running `client.close()` on a
      dedicated daemon thread bounded by `_CLIENT_CLOSE_TIMEOUT_SECONDS`
      (1.0s). Applied at all four close sites: `create_session()`,
      `send_prompt()`, `_abort_session_bounded()`, and the shared client
      in `stop()`. Every request-local call site now decides its primary
      exception first and attaches a close failure via `add_note()`
      rather than letting it replace the primary; a close failure with no
      pending primary raises `OpenCodeCleanupError`. `stop()` tracks an
      in-progress shared-client close (`_client_close_attempt`) so a
      retried `stop()` observes rather than restarts a still-running
      close. Added 9 new tests in `tests/test_opencode.py` covering
      throwing/hanging close after session-creation and prompt timeouts,
      `run_agent()` still aborting and delivering the exact timeout to
      the observer, abort surviving a throwing close, a hanging abort
      close bounded, a successful prompt with a failing close raising
      `OpenCodeCleanupError`, `stop()` continuing past a hanging shared
      close, and a retried `stop()` not invoking a second concurrent
      close.
      Verification for steps 1+2: `pytest -q` 519 passed, stable across 3
      runs (no flakiness observed); `ruff check .` clean; `ruff format .`
      applied and `--check` clean; `mypy src` clean; `git diff --check`
      clean.
- [ ] Step 3 — headless cleanup completeness (blocker 5)
- [ ] Step 4 — TUI ownership registry (blocker 2)
- [ ] Step 5 — per-attempt shutdown signaling (blocker 1)
- [ ] Step 6 — documentation and full verification
