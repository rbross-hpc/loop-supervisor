# SIGTERM is bridged into the existing exception-driven cleanup path

## Status

Accepted

## Context

The supervisor's entire cleanup architecture -- stopping the OpenCode
process group (`OpenCodeServer.stop()`), stopping the permission denier,
and releasing the supervisor lock -- is reached exclusively through
Python exception unwinding: `RunSession.__exit__` (`runtime.py`) and
`OpenCodeServer.__exit__` (`opencode.py`) run this cleanup for *any*
`BaseException` propagating out of the `with` block that owns them, and
`RunSession.close()` is the single, carefully specified owner of the
lock-release decision (see ADR 0009). This is well-engineered and well
tested -- see `test_opencode.py`'s real-subprocess process-group tests
and `test_runtime.py`'s synthetic-`KeyboardInterrupt` traceback-identity
tests -- but every one of those tests, and every one of those cleanup
paths, is reachable only by an exception actually being raised in the
main thread.

SIGINT's default disposition in CPython already raises
`KeyboardInterrupt` in the main thread, so a plain Ctrl-C gets this
entire cleanup path for free with zero supervisor-specific signal code.
SIGTERM's default disposition is immediate process termination with
*no* Python-level unwinding: no `finally` blocks run, no `__exit__` is
called, nothing is raised. Confirmed empirically (not just by reading
the CPython signal-handling documentation) with a minimal repro:

```
SIGINT:  rc=-2   cleanup_ran=True
SIGTERM: rc=-15  cleanup_ran=False
```

and confirmed against the real supervisor CLI, spawned as a genuine
subprocess against a real OpenCode process group (the same
`fake_opencode.py` fixture `test_opencode.py` uses): a bare
`kill <pid>` sent to a supervisor mid-run left the supervisor lock file
on disk and orphaned the OpenCode process group (reparented to init,
still running). A search of `src/` confirms there is exactly one
`signal.signal()` call in the entire codebase, in `_launcher.py`, which
runs in the anchored launcher subprocess, not the supervisor itself;
there is no `atexit`, no `__del__`, and nothing else bridges OS-level
termination into the cleanup machinery.

This gap is the root cause of orphaned `opencode serve` processes and
stale lock files observed after killing a stuck supervisor process
during earlier development sessions; those had been provisionally
attributed to backlog item 22, which as originally filed asked only for
*tests* covering signal behavior. Writing such tests against the
as-shipped code would have encoded this gap as intended behavior.

## Decision

A one-shot SIGTERM handler is installed in `cli.py`, scoped narrowly to
the two headless entry points (`cmd_run`, `cmd_resume`) around their
`run_new`/`run_resume` calls, via a small context manager
(`_bridge_sigterm_to_keyboard_interrupt`). On first SIGTERM delivery,
the handler:

1. Restores the platform's prior SIGTERM disposition immediately
   (before doing anything else).
2. Prints a one-line notice to stderr naming the cause.
3. Raises `KeyboardInterrupt`.

The raised `KeyboardInterrupt` is not a new code path: it rides the
exact same `RunSession.__exit__`/`close()` machinery that a real Ctrl-C
already exercises, including the existing "a second interrupt during
cleanup aborts the retry loop instead of being retried indefinitely"
semantics documented in `runtime.py`. No new cleanup logic was added
anywhere; the fix is entirely "make SIGTERM produce the same stimulus
SIGINT already produces," at the single place (the process entry point)
that legitimately owns process-wide signal disposition.

Three scoping choices, each deliberate:

- **Only `cmd_run`/`cmd_resume`, not `main()` globally, and explicitly
  not `cmd_tui`.** Textual's Linux driver already disables the
  terminal's `ISIG` flag in raw mode (so even Ctrl-C is read as an
  ordinary keypress inside a running TUI, not delivered as SIGINT) and
  installs no SIGTERM handler of its own; only Textual's *web* driver
  bridges SIGINT/SIGTERM into `ExitApp`. Injecting an externally raised
  `KeyboardInterrupt` into Textual's running `asyncio` event loop is an
  untested code path that could leave the terminal stuck in raw mode.
  TUI-side signal handling is a separate, UX-shaped problem (does "q" 's
  existing shutdown worker get triggered? does the terminal need
  explicit restoration first?) and is tracked separately, not solved
  here.
- **One-shot, not persistent.** The handler restores default
  disposition on first delivery rather than staying installed for
  every future SIGTERM. This matches the conventional process-supervisor
  escalation model (SIGTERM, wait, SIGTERM again or SIGKILL) and
  intentionally mirrors the existing double-Ctrl-C behavior: a second
  signal arriving while cleanup is still unwinding kills immediately
  rather than injecting a second `KeyboardInterrupt` into whatever
  cleanup retry loop the first one triggered. The accepted cost is that
  a second SIGTERM during a slow-but-otherwise-healthy cleanup can strand
  the lock; `--recover-stale-lock` exists precisely for this.
- **Installed at the CLI boundary, never inside library code.** Neither
  `RunSession` nor `OpenCodeServer` touch `signal.signal()`. Something
  importing `loop_supervisor` as a library must not have its process's
  signal disposition silently changed as a side effect of that import.

## Consequences

- A bare `kill <pid>` (SIGTERM) against a headless `run`/`resume`
  invocation now reliably releases the supervisor lock and terminates
  the OpenCode process group, verified end-to-end in
  `tests/test_signal_handling.py` by spawning the real CLI as a
  subprocess against the real OpenCode fixture and sending a real
  SIGTERM -- not a synthetic in-process raise. That test file is also
  the demonstrated failing-first baseline: it fails against the
  pre-fix code with the lock retained and the fake OpenCode server
  process orphaned, and passes once the handler is installed.
- The reported exit code for a SIGTERM-terminated run is **130** (as if
  by SIGINT), not the conventional **143** (128 + SIGTERM), because
  CPython's top-level `KeyboardInterrupt` handling re-raises the signal
  against itself at `SIG_DFL` regardless of which signal produced the
  `KeyboardInterrupt`. Getting a signal-accurate exit code would require
  a distinct exception type threaded through every
  `except (KeyboardInterrupt, SystemExit)` site in `runtime.py` and
  `opencode.py` -- a materially larger change for a benefit (a more
  precise shell-visible exit code) judged not to justify it. The stderr
  notice printed by the handler exists specifically to compensate: an
  operator watching the process's own output is not misled about the
  cause even though the wait-status is.
- `loop-supervisor tui` is unaffected: a real SIGTERM against a running
  TUI process still terminates at default disposition with the same
  orphan/stale-lock exposure this ADR fixes for the headless path. This
  is a known, explicitly deferred gap (see the Decision section above),
  not an oversight; `docs/plans/2026-08-22-post-lifecycle-fix-backlog.md`
  tracks it as a distinct item pending a TUI-specific design.
- Backlog item 22, originally scoped as "add missing signal tests," is
  split: this ADR and its accompanying real-subprocess test file resolve
  the actual behavior gap (22a); a real-OpenCode-binary end-to-end suite
  and TUI-specific signal/init-race coverage remain open under a
  re-filed 22b, now written against a codebase where SIGTERM already
  behaves correctly rather than one that would have required the test
  to encode the bug.
