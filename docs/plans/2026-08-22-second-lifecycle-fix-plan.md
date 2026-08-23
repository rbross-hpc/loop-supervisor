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
      A fresh independent audit found the prior implementation
      incomplete: bounded-close construction/start could still escape
      uncaught, `_close_bounded()`'s timeout bound was frozen at
      definition time via a default argument (immune to later
      `_CLIENT_CLOSE_TIMEOUT_SECONDS` changes), each of
      `create_session()`/`send_prompt()`/`_abort_session_bounded()`
      closed before the full request outcome (status/decode/validation/
      assistant-shape) was decided, `OpenCodeServer.__exit__()` only
      caught `Exception` (a cleanup `KeyboardInterrupt`/`SystemExit`
      could replace a body exception), and the shared-client close
      ownership state machine in `stop()` needed explicit
      construction/start/mismatch/retry handling.

      Remediated on `fix/http-cleanup-precedence`: `_BoundedCloseAttempt`
      now separates construction from thread start, and
      `_start_bounded_close()` is a non-throwing factory catching
      construction/`Thread()`/`start()` failures and returning them
      rather than raising. `_close_bounded()` reads
      `_CLIENT_CLOSE_TIMEOUT_SECONDS` dynamically on every call (no
      frozen default argument). `create_session()`, `send_prompt()`, and
      `_abort_session_bounded()` now decide the complete primary outcome
      — transport, HTTP status, JSON decode, ID/shape validation,
      assistant-error, and text/structured-output extraction — inside a
      single guarded block, closing only in an `except BaseException as
      primary: ...; raise` / `else: ...; return` structure so a close
      failure/timeout can never replace a primary that was only
      partially decided. `_add_cleanup_note()` attaches at most one
      deterministic note without ever touching `__cause__` and without
      letting annotation failure itself replace the primary.
      `OpenCodeServer.__exit__()` now catches `BaseException` (not just
      `Exception`) from `stop()`, so a cleanup-time
      `KeyboardInterrupt`/`SystemExit` is noted rather than replacing the
      body exception. The shared-client close block in `stop()` is now
      an explicit state machine: construction/start failure retains the
      client with no attempt installed; an existing attempt for the same
      client is re-waited, never re-invoked; an attempt bound to a
      different client is reported and left untouched; a timeout retains
      both client and attempt; a completed exception retains the client
      and clears the attempt for one retry; success clears both.
      `start()` remains rejected while either is unresolved.

      Added a comprehensive `httpx.MockTransport` test matrix for
      `create_session()`/`send_prompt()`/`_abort_session_bounded()` and
      every required close outcome, plus bounded-close orchestration,
      context-manager precedence, and shared-client ownership/retry
      semantics. A subsequent adversarial audit reopened Step 2 after
      finding unsafe cleanup-exception rendering, unguarded shared wait/
      result inspection, ambiguous post-start failure ownership, a
      leaking context-manager test, and missing exact-semantics/failure-
      mode assertions. Those gaps were remediated and independently
      re-audited **GO**. Final verification: `test_opencode.py` 139 passed;
      `test_runtime.py`/`test_tui_app.py` 42 passed; full `pytest -q` 611
      passed across three consecutive runs; `ruff check .`, `ruff format
      --check .`, `mypy src`, and `git diff --check` clean. The audit also
      reran 139 OpenCode tests and all 611 tests, confirmed no fixture
      process leaks, and verified Step 1 process ownership was unchanged.
- [ ] Step 3 — headless cleanup completeness (blocker 5). **Reopened.**
      Implemented on `fix/headless-cleanup-completeness`. `runtime.py` now
      has one shared bounded server-stop retry helper
      (`_confirm_server_stopped()`, `_CLEANUP_ATTEMPTS = 3` with increasing
      backoff, never discarding the server handle between attempts) used
      uniformly for startup failure, the runner handoff, `supervisor.run()`
      (success and failure), and ordinary post-run cleanup — replacing the
      four previously separate single-attempt `try/except Exception`
      cleanup call sites. `run_new()`/`run_resume()` now catch
      `BaseException` (not just `OpenCodeError`) from `server.start()`;
      `_startup_failure()` re-raises `KeyboardInterrupt`/`SystemExit`
      unchanged (never wrapped, never persisted as an operational
      failure), and only ordinary `Exception`s are persisted via
      `record_external_failure()` and wrapped in `RuntimeError_`. The lock
      lease is marked releasable only once `_confirm_server_stopped()`
      actually confirms success; unresolved cleanup/persistence failures
      are attached as deterministic notes (`add_note()`) on the primary
      exception rather than replacing it or being silently discarded.
      `supervisor.runner = server` moved inside `_run_and_stop()`'s
      protected boundary, so a failure raised by the assignment itself is
      cleaned up exactly like a run failure. `_lock_context()` now
      attaches a lock-release failure as a note on the body exception
      instead of silently discarding it, and still never calls release()
      while the lease is unreleasable. All `subprocess.Popen` `OSError`
      subclasses remain normalized to `ServerStartupError` with exact
      `__cause__` identity (direct `PermissionError`/generic `OSError`
      tests added). Step 1 process ownership and Step 2 HTTP cleanup
      semantics are unchanged.

      Added 21 new deterministic tests across `test_runtime.py` (exact
      retry count/backoff, transient-failure-then-success and
      retry-exhaustion for startup/run-success/run-failure, runner-
      assignment-failure cleanup, exact `KeyboardInterrupt`/`SystemExit`
      identity preservation for startup and run-time, cleanup-time
      `KeyboardInterrupt` not replacing a pending primary, persistence-
      failure-plus-cleanup-failure, lock-release-failure-with-existing-
      primary, and lock-file-released-only-after-confirmed-cleanup) and
      `test_opencode.py` (direct `PermissionError`/generic `OSError`
      normalization with exact cause identity, and startup-cleanup
      `KeyboardInterrupt` not replacing the startup primary). Verification
      at that point: `test_opencode.py` 142 passed; `test_runtime.py`/
      `test_cli_runtime.py`/`test_locking.py` 104 passed (246 combined
      with `test_opencode.py`); `test_tui_app.py` 20 passed; full
      `pytest -q` 632 passed across three consecutive clean runs with no
      fixture process leaks; `ruff check .`, `ruff format --check .`,
      `mypy src`, and `git diff --check` clean.

      A subsequent focused read-only audit (scoped to this step, run after
      Step 4 was implemented) returned **NO-GO**, finding four defects in
      the implementation above:

      1. `OpenCodeServer.start()` (`raise primary` at the former
         `opencode.py:651`) and `_startup_failure()` (`raise exc` at the
         former `runtime.py:405`) redispatched the primary exception by
         naming it in a `raise <expr>` statement instead of a true bare
         `raise`, inserting an extra frame into the propagated traceback
         despite the documented "exact identity and traceback" guarantee.
         Existing tests asserted exception identity (`is`) but never
         traceback shape, so this was not caught.
      2. Several Step 3 diagnostic sites built exception text via plain
         f-string interpolation (implicit `str()`) before any
         `add_note()`/message construction — `_unresolved_cleanup_message()`,
         the `_lock_context()` release-failure note, and
         `_startup_failure()`'s message composition — so a
         cleanup/persistence/lock-release exception with a throwing
         `__str__` could itself escape and replace the primary being
         reported, violating the same precedence `opencode._safe_exception_text`
         was already introduced to protect in Step 2.
      3. `time.sleep()` in `_confirm_server_stopped()`'s inter-attempt
         backoff is not interruption-safe with respect to
         `KeyboardInterrupt`/`SystemExit` precedence during the backoff
         window itself (unresolved).
      4. Cleanup-time `KeyboardInterrupt`/`SystemExit` retry/exhaustion
         semantics across the bounded retry loop have edge cases not
         fully covered by tests (unresolved).

      Findings 1–2 were remediated on `fix/step3-primary-preservation`
      (scope explicitly limited to these two; findings 3–4 are
      out of scope for this remediation and remain open, so this step
      stays reopened/incomplete overall):

      - `OpenCodeServer.start()` was restructured so the entire spawn/
        readiness sequence runs inside one outer `try`, with a single
        `except BaseException as primary: ...; raise` at the end that
        performs startup cleanup and then bare re-raises — never `raise
        primary` — so the traceback the caller observes is exactly the
        one produced at the original raise site, with no
        `OpenCodeServer.start` redispatch frame appended. The `OSError`→
        `ServerStartupError` normalization now uses `raise wrapped from
        exc` at the point of the original failure (still inside the
        `except OSError` handling it), preserving exact `__cause__`
        identity.
      - `_startup_failure()`'s direct-`BaseException` branch now uses a
        bare `raise` (relying on `exc` still being the exception actively
        handled in the caller's `except BaseException as exc:` block in
        `run_new()`/`run_resume()`) instead of `raise exc`.
      - Added `_safe_exception_text()` to `runtime.py` (mirroring
        `opencode._safe_exception_text`, extended to accept `None` for
        `_CleanupOutcome.last_error`) and applied it at every Step 3
        diagnostic site that interpolates an arbitrary exception:
        `_unresolved_cleanup_message()`, the `_lock_context()`
        release-failure note, and every message/note built in
        `_startup_failure()` (startup primary, persistence failure,
        cleanup `last_error`).

      Added 4 new adversarial tests to `test_runtime.py` (unprintable
      `__str__` on a startup-cleanup-retry-exhaustion error, an
      `record_external_failure()` persistence error, a `stop()` error
      after an otherwise-successful run, and a `_LockLease.release()`
      failure) confirming the resulting diagnostic falls back to a
      deterministic `"unprintable <ClassName>"` rendering rather than
      crashing or leaking the broken `__str__`, plus 2 new traceback-frame
      tests (`test_opencode.py`, `test_runtime.py`) asserting no
      cleanup-redispatch frame is present after a startup failure.
      Verification after this remediation: `test_opencode.py` 143 passed;
      `test_runtime.py` 175 passed (188 combined with `test_opencode.py`);
      full `pytest -q` 648 passed across three consecutive clean runs;
      `ruff check .`, `ruff format --check .`, `mypy src`, and
      `git diff --check` clean. Step 1/2/4 semantics were not touched.
      **Findings 3–4 remain unresolved; this step is not complete.**
- [x] Step 4 — TUI ownership registry (blocker 2). Implemented on
      `fix/tui-ownership-registry`. `LoopSupervisorApp` now maintains an
      authoritative `_owned_run_screens` registry, populated in
      `RunScreen.on_mount()` before any resource acquisition (lock/
      server) and consulted instead of Textual's own `_screen_stacks`
      wherever lifecycle ownership must be determined. Unmounting never
      deregisters: `RunScreen.on_unmount()` requests shutdown and starts
      (or reuses) exactly one app-owned automatic retry coordinator per
      screen (`ensure_cleanup_coordinator()`/
      `_run_screen_cleanup_coordinator()`), which retries indefinitely on
      a fixed interval with no interactive UI required until cleanup is
      confirmed clean. A screen deregisters only via `finalize_run_screen()`,
      which requires `RunScreen.ready_to_finalize` (both `_init_done_event`
      and `_advance_done_event` set, and `shutdown_clean` True) and is
      identity-safe: it pops only when the screen being finalized is
      exactly `self.screen`, so a detached screen finalizing late can
      never pop an unrelated newly-active screen. `_on_exit_app()` was
      rewritten to repeatedly drain the registry — ensuring a coordinator
      exists for every currently-registered screen, awaiting them, and
      re-reading the registry — rather than taking a single
      `_screen_stacks` snapshot, so screens registered while exit is
      already waiting (or unexpectedly detached-and-unclean screens) are
      never invisible to it; the underlying Textual `_on_exit_app()` runs
      only once the registry is completely empty. `on_unmount()` and the
      exit-drain loop share exactly one coordinator per screen, so a
      detached-and-unclean screen is never retried by two overlapping
      attempts.

      A hang was found and fixed during implementation:
      `RunScreen.await_shutdown_complete()` previously handed a single
      unbounded `threading.Event.wait()` to `run_in_executor()`; since
      that blocking call cannot be interrupted, a coordinator task
      awaiting it while the event would never be set (e.g. cleanup
      already completed by another path, such as a failed-initialization
      handler, with no `_shutdown_worker` attempt to signal it) could
      occupy a real OS thread indefinitely and hang the executor's own
      shutdown/join. Remediated by polling with a short bounded wait per
      executor call instead, so the coroutine remains promptly
      cancellable. The cleanup coordinator also checks
      `screen.shutdown_clean` before awaiting completion, rather than
      awaiting unconditionally, for the same "already clean with nothing
      to signal" case — deliberately narrow and not a redesign of the
      reusable `_shutdown_complete_event`, which remains Step 5's
      responsibility.

      Added 10 new deterministic tests to `test_tui_app.py`: registration
      before resource acquisition; a detached unclean screen remaining
      registered and auto-retried with no interactive input; a stack-
      removal/on_unmount() race (screen absent from every
      `_screen_stacks` entry while a blocked `advance()` keeps it
      unclean) not hiding it from app-level exit; a detached clean
      finalize not popping a newly-active unrelated screen; a detached
      lock-release-only failure remaining registered and retrying without
      re-invoking the already-cleared server's `stop()`; app exit
      rechecking the registry for a screen registered while already
      draining; `on_unmount()` and exit-drain sharing one coordinator
      (no overlapping `stop()` calls); base Textual exit never running
      while the registry is non-empty; registry membership clearing only
      after quiescence and a clean release; and no leftover coordinator
      task/lock file/server owner/registry entry after a clean exit.
      Final verification: `test_tui_app.py` 30 passed (43 consecutive
      clean runs during hang investigation, plus 3 further consecutive
      clean runs after the fix); full `pytest -q` 642 passed across three
      consecutive clean runs with no fixture process leaks; `ruff check .`,
      `ruff format --check .`, `mypy src`, and `git diff --check` clean.
      Step 3 headless runtime semantics were not touched. Step 5's
      per-attempt shutdown/generation redesign was not implemented.
- [ ] Step 5 — per-attempt shutdown signaling (blocker 1)
- [ ] Step 6 — documentation and full verification
