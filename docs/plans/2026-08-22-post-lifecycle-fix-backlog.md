# Backlog: deferred audit claims (post lifecycle-blocker fix)

Branch: `feature/tui-vertical-slice`
Context: recorded after fixing the four release-blocking lifecycle issues
(headless lock retention, TUI init/shutdown race, retryable TUI shutdown
with a clean-exit gate, bounded timeout abort). See
`docs/decisions/0009-supervisor-lock-and-operational-failure-semantics.md`
for the accepted lock/cleanup contract those fixes implement.

This is a prioritized list of substantiated findings from the lifecycle
audit that were judged **not** to block release, but that should be
tracked and addressed in follow-up work rather than dropped.

## Tier 1 — fix next: correctness/security

1. **Exhausted `ContractError` from malformed-output retries escapes
   without durable failure state.**
   `src/loop_supervisor/supervisor.py:524-532`,
   `src/loop_supervisor/supervisor.py:1383-1395`.
   When malformed-output retries are exhausted, the resulting
   `ContractError` is raised but never persisted as an
   `operational_failure`/`failed` state, so the operator has no durable
   record to resume from or diagnose.

2. **Ordinary post-transition `_save()` failures are outside
   classification/failure-persistence boundaries.**
   `src/loop_supervisor/supervisor.py:534-549`,
   `src/loop_supervisor/supervisor.py:716-718`.
   A `_save()` failure after an otherwise successful phase transition is
   not classified or recorded consistently with other operational
   failures.

3. **Persisted "sanitized" messages may include HTTP response bodies or
   server output.**
   `src/loop_supervisor/opencode.py:614-618`,
   `src/loop_supervisor/supervisor.py:1376-1380`.
   `OperationalErrorRecord` is documented (ADR 0009) as never containing
   secrets/headers/env vars, but response-body truncation is not the same
   as redaction; a response body could still contain sensitive content
   from the target repository or environment.

4. **Symlinked lock/state ancestor directories, and state-file symlinks,
   are not rejected.**
   `src/loop_supervisor/locking.py:76-103`,
   `src/loop_supervisor/state.py:665-695`.
   `_open_no_follow` guards the leaf lock/guard files, but a symlinked
   `loop-supervisor` *ancestor* directory could redirect lock/state writes
   outside Git metadata entirely.

5. **Dangling lock symlink can spin acquisition.**
   `src/loop_supervisor/locking.py:343-365`.
   `Path.exists()` follows symlinks and returns False for a dangling
   symlink at the lock path, so `_inspect_existing_lock()` returns `None`
   (meaning "retry") in a loop instead of surfacing a clear error.

6. **Bound non-newline OpenCode stdout fragments.**
   `src/loop_supervisor/opencode.py:223-251`.
   The stdout pump's `partial` buffer (data received but not yet
   newline-terminated) is unbounded; a subprocess that writes an
   arbitrarily long line without a newline could grow this without limit.

7. **Correct SSE event-size accounting and raw-line memory bounds.**
   `src/loop_supervisor/sse.py:70-129`.
   Confirm the SSE client's line/event buffering has an explicit size
   cap symmetrical to the OpenCode stdout pump fix above.

## Tier 2 — fix next: validation/startup

8. **CLI-created `RunOptions` bypass `RunOptions.from_dict()`
   validation.**
   `src/loop_supervisor/state.py:72-145`, `src/loop_supervisor/cli.py:55-66`.
   Directly constructed `RunOptions` (as opposed to those deserialized
   from persisted state) skip the validation `from_dict()` performs.

9. **Persisted nested role results, pending questions, and
   phase/result relationships are not fully validated.**
   `src/loop_supervisor/state.py:579-655`.
   Deep/cross-field validation (nested role results, pending-question
   shape, timestamp ordering, phase-vs-result consistency) is incomplete
   compared to the flat field/type checks already in place.

10. **Non-`FileNotFoundError` spawn failures need normalization.**
    `src/loop_supervisor/opencode.py:155-183`.
    Only `FileNotFoundError` is caught and normalized to
    `ServerStartupError`; other `OSError` subclasses from
    `subprocess.Popen` (e.g. `PermissionError`, `OSError` for exec format
    errors) propagate unclassified and are not persisted as a durable
    startup failure.

## Tier 3 — reliability

11. **`httpx` inactivity timeouts are not absolute wall-clock
    deadlines.**
    A trickling response (bytes arriving just often enough to reset the
    read-inactivity timer) could exceed `role_timeout` in wall-clock time
    without httpx ever raising `TimeoutException`. Needs an explicit
    monotonic deadline check independent of httpx's own timeout
    semantics.

12. **Define cleanup/error precedence for startup failures where server
    ownership remains unresolved**, beyond what blocker-1/blocker-4 fixes
    already cover — specifically for TUI initialization (parallel to the
    headless runtime's `_startup_failure()` handling added in this
    round).

13. **Acceptance tests for**: orphan-child prevention under process
    kill/crash, stale-lock recovery end-to-end, repeated cleanup attempts
    against a real (not faked) OpenCode process, and process-exit
    behavior under SIGINT/SIGTERM.

## Tier 4 — telemetry/UI

14. **SSE reconnect/session-registration reconciliation is absent.**
    `src/loop_supervisor/sse.py:247-277`, `src/loop_supervisor/tui/app.py:796-797`.
    `_on_sse_notice()` discards gap notices; there is no reconciliation of
    active-session state after a reconnect, and events that arrive before
    the corresponding session is registered can be misattributed or
    dropped.

15. **Trailing-event attribution loss.**
    `src/loop_supervisor/tui/live.py:224-236`.

16. **Browser, durable-state, and live rendering remain behind
    README/plan claims.**
    `src/loop_supervisor/tui/app.py:204-243`,
    `src/loop_supervisor/tui/renderers.py:58-78`.
    Reasoning, tools, files, feed, and connection-reason rendering are
    incomplete relative to documented scope. Either implement the
    missing rendering or narrow the README/plan language for this slice.

17. **`tui` should validate new-run options the same way `run` does.**
    `src/loop_supervisor/cli.py:257-302`.

18. **Define and propagate meaningful TUI process exit status.**
    `src/loop_supervisor/cli.py:234-250`.

19. **Add parser event-size limits and reconnect/backoff acceptance
    coverage** for the SSE client.

## Tier 5 — documentation/testing debt

20. **Correct ADR 0008's claim that worker and event-loop threads share
    no state** — `LiveActivityReducer` ownership and `call_from_thread`
    boundaries should be described accurately.

21. **Correct merge-conflict repair instructions.**
    `README.md:267-272`.

22. **Add missing end-to-end tests** for signals (SIGINT/SIGTERM against
    a real headless process), app-level exit refusal/retry against a real
    OpenCode process, TUI initialization races beyond what this round's
    fakes exercise, and cleanup failures under real process-kill
    scenarios rather than monkeypatched `stop()`.

## Out of scope for this backlog

Explicitly excluded from this list because they were already fixed in
this round: headless lock retention through confirmed OpenCode cleanup
(`runtime.py`), the TUI initialization/shutdown race
(`_do_initialize_locked`), retryable TUI shutdown with a clean-exit gate
(`action_request_shutdown`/`_on_exit_app`), and unbounded timeout-abort
HTTP calls (`opencode.py` abort helpers). See git history on
`feature/tui-vertical-slice` for the corresponding commits and
`tests/test_runtime.py`, `tests/test_tui_app.py`, `tests/test_opencode.py`
for their regression coverage.
