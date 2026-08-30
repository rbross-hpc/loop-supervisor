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

## Corrections to prior commit messages

Commit messages in this project are treated as part of the durable
record and are never amended; a factual error in one is corrected here
instead, since this file is already the place inaccuracies discovered
after the fact get written down.

- **258357d** ("chore: extend mypy to cover tests, add
  check_untyped_defs, fix pyright venv") states that `git diff <base>
  -- tests/ | grep "^-" | grep assert` is empty and that "every
  assertion change is an addition." That is not accurate: one
  assertion line matches the removal filter, in
  `tests/test_opencode.py`'s `test_bounded_close_attempt_reports_hang`:

  ```
  -        assert isinstance(attempt, _BoundedCloseAttempt)
  ```

  The line was moved, not deleted. It and its preceding `attempt =
  _start_bounded_close(client)` were hoisted out of the `try:` block
  to just above it, so that `attempt` is narrowed from
  `_BoundedCloseAttempt | BaseException` to `_BoundedCloseAttempt`
  before the `finally:` clause references it — without the hoist,
  mypy reports `union-attr` on `attempt.wait(...)` in `finally`. The
  assertion still runs, in the same order, with the same predicate.

  No code change accompanies this correction; the hoist is correct as
  it stands and is mildly preferable to the original (it checks the
  precondition before entering the block whose `finally` depends on
  it). The one behavioural difference is that if the `isinstance`
  assertion itself were ever to fail, `release.set()` in the
  `finally` would no longer run. That cannot hang the suite:
  `_BoundedCloseAttempt.start()` creates its worker with
  `daemon=True` (`opencode.py:257`), so a stranded worker cannot block
  interpreter exit.

  The verification claim should have read: one assertion line was
  relocated within its test; no assertion was removed, weakened, or
  had its predicate changed.

## Tier 1 — fix next: correctness/security

1. ~~Exhausted `ContractError` from malformed-output retries escapes
   without durable failure state.~~ **Resolved.**
   `ContractError` is a `ValueError`, but `advance()`'s operational-failure
   tuple (`src/loop_supervisor/supervisor.py:524-530`) previously only
   caught `RuntimeError` subclasses, so any `ContractError` — from
   exhausted malformed-output retries (`_parse_with_retry()`,
   `supervisor.py:1396-1408`) or from `check_decision_answered()`/
   `check_task_identity()`, which have no retry wrapper at all — escaped
   `advance()` before `_save()` ever ran. Fixed by adding `ContractError`
   to that tuple and giving it a `kind="contract"` classification and
   recovery hint in `_classify_operational_failure()`/`_error_kind()`
   (`supervisor.py:1324-1386`). All raise sites are now covered, not just
   the retry-exhaustion case. See
   `docs/decisions/0009-supervisor-lock-and-operational-failure-semantics.md`'s
   operational-failure examples and `tests/test_advance.py`'s
   `test_exhausted_malformed_output_persists_operational_failure` and
   related tests.

   Known follow-up, not fixed here: `_do_architecting()`/`_do_building()`
   clear `state.pending_question`/consume the operator's guidance before
   the downstream contract check can raise (`supervisor.py:867-869`,
   `1022-1024` vs. `909`, `1051`), so a retry after one of these specific
   `ContractError` sites can lose that input. This is pre-existing and
   identical for exceptions already in the tuple (e.g. `AgentInvocationError`
   from the same `run_agent` call hits the same window) — the fix above
   makes `ContractError` consistent with that existing behavior rather
   than introducing a new hazard. Tracked as item 24, below.

2. **Ordinary post-transition `_save()` failures are outside
   classification/failure-persistence boundaries.**
   `src/loop_supervisor/supervisor.py:534-549`,
   `src/loop_supervisor/supervisor.py:716-718`.
   A `_save()` failure after an otherwise successful phase transition is
   not classified or recorded consistently with other operational
   failures.

3. ~~Persisted "sanitized" messages may include HTTP response bodies or
   server output.~~ **Resolved (partially).**
   `_sanitize_message()` (`src/loop_supervisor/supervisor.py:1406-1479`)
   previously only truncated to 2000 characters; its docstring claimed it
   also stripped secret patterns, but it did not, and ADR 0009 and
   `state.py`'s `OperationalErrorRecord` docstring both asserted an
   absolute "never contains ... environment variables, or secrets"
   guarantee with nothing in the code enforcing it. Confirmed exploitable
   via `_diagnostic_output()` (`opencode.py:820-828`): OpenCode's child
   process inherits the full environment (`opencode.py:578`), so a
   startup failure that echoes an env-derived auth error survives
   head-only truncation intact (the secret sits in the early lines,
   which `msg[:2000]` keeps).

   Fixed by giving `_sanitize_message()` two real passes before
   truncating: (1) literal replacement of secret-named environment
   variable values (`*_KEY`, `*_TOKEN`, `*_SECRET`, etc.) above a minimum
   length, to avoid mangling unrelated text on a short/placeholder value
   under a secret-sounding name (a real hazard found while designing
   this — e.g. `OPENAI_API_KEY=rross` blindly redacting every occurrence
   of `rross`, including in unrelated file paths); (2) a pattern backstop
   for common credential formats (`sk-...`, `ghp_...`, `Bearer ...`) that
   catches keys not in *this* process's own environment (e.g. the
   OpenCode child's own provider key). Truncation now keeps both a
   leading and trailing portion instead of only the head, so it no
   longer discards the actual terminating error in favor of a startup
   banner. Also truncated the one previously-unbounded server-supplied
   channel, `opencode.py`'s `_extract_text()` error field. ADR 0009 and
   the `OperationalErrorRecord` docstring now describe this as a
   best-effort guarantee, not an absolute one.

   Explicitly **not** resolved: arbitrary repository content in Git/
   contract-error messages (a merge conflict's file list, an agent's
   malformed output quoting a config value) is not distinguishable from
   legitimate diagnostic content and can still appear verbatim. The
   record remains a `0o600` file under `.git/loop-supervisor/runs/`
   (never committed, never transmitted) and should be treated as
   sensitive regardless. See `tests/test_sanitize.py` and
   `docs/decisions/0009-supervisor-lock-and-operational-failure-semantics.md`'s
   Consequences section.

4. **Symlinked lock/state ancestor directories, and state-file symlinks,
   are not rejected.**
   `src/loop_supervisor/locking.py:76-103`,
   `src/loop_supervisor/state.py:665-695`.
   `_open_no_follow` guards the leaf lock/guard files, but a symlinked
   `loop-supervisor` *ancestor* directory could redirect lock/state writes
   outside Git metadata entirely.

5. ~~Dangling lock symlink can spin acquisition.~~ **Resolved.**
   `src/loop_supervisor/locking.py:364` (`_inspect_existing_lock`) and
   `:446` (`release`). `Path.exists()` follows symlinks and returns
   `False` for a dangling symlink, so `_inspect_existing_lock()`
   previously returned `None` ("disappeared, retry") in `acquire()`'s
   `while True` loop, which then re-attempted `os.link()` against the
   same dangling symlink — always `FileExistsError`, since `link(2)`
   does not follow symlinks — spinning at 100% CPU forever, *while
   holding the guard flock* (`with _guarded(...)`, `locking.py:343`),
   wedging every supervisor process on the repository.
   `--recover-stale-lock` could not help: recovery lives past this
   early return. Confirmed reproducible before the fix.

   Fixed by switching both sites from `Path.exists()` to
   `os.path.lexists()`, so a dangling symlink is seen rather than read
   as absent. At the `acquire()` site this falls through to
   `_read_lock`, whose `_open_no_follow` raises `OSError` ("too many
   levels of symbolic links"), already wrapped as `MalformedLockError`.
   `release()` had the identical hazard on its own `.exists()` check
   (`:446`): a dangling symlink there would make it conclude "already
   gone" and silently discard the ownership token while leaving the
   symlink in place. Fixed the same way; now raises `LockError` instead.
   See `tests/test_locking.py`
   (`test_dangling_lock_symlink_is_rejected_not_spun_on`,
   `test_dangling_lock_symlink_at_release_time_is_not_treated_as_absent`).
   The first test runs `acquire()` in a subprocess with a bounded
   `join()`, since calling it in-process against the pre-fix code would
   have hung the test process itself with no way to time out.

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

12. ~~Define cleanup/error precedence for startup failures where server
    ownership remains unresolved, beyond what blocker-1/blocker-4 fixes
    already cover — specifically for TUI initialization (parallel to the
    headless runtime's `_startup_failure()` handling added in this
    round).~~ **Resolved** by the `RunSession` TUI migration
    (`docs/decisions/0009-supervisor-lock-and-operational-failure-semantics.md`):
    `RunScreen._do_initialize` now constructs and enters a `RunSession`,
    and TUI startup failures route through `RunSession.__enter__()` /
    `start_server()` into the same `_startup_failure()` the headless
    runtime uses (`runtime.py:869-925`) — exactly the parallel this item
    asked for, not a separate TUI-specific implementation.

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

17. **`tui` should validate new-run options the same way `run` does,
    and accept the same run-behavior flags instead of a hardcoded
    default.** `src/loop_supervisor/cli.py:416-422` (the `tui`
    subparser accepts only `--project`/`--recover-stale-lock`, none of
    `run`'s nine flags), `src/loop_supervisor/tui/app.py:159-170`
    (`_DEFAULT_OPTIONS`). (Citation to `cli.py:257-302` in an earlier
    version of this item had drifted from the `tui` parser it was
    meant to describe; corrected here.) Also covers `max_steps`/
    `--step`, which have no TUI equivalent at all — the TUI's step
    loop (`tui/app.py:689-750`) runs to completion or to the first
    pause with no budget concept, unlike `Supervisor.run()`'s
    `max_steps` (`supervisor.py:718-735`). See
    `docs/decisions/0021-tui-drives-runsession-in-process.md` for the
    full accounting of where the TUI's step loop diverges from the
    headless one.

18. **Define and propagate meaningful TUI process exit status.**
    `src/loop_supervisor/cli.py:234-250`.

19. **Add parser event-size limits and reconnect/backoff acceptance
    coverage** for the SSE client.

## Tier 5 — documentation/testing debt

20. ~~**Document `LiveActivityReducer`'s single-owner-thread contract in
    ADR 0008.**~~ **Resolved.**
    `src/loop_supervisor/tui/live.py:104-127`. The reducer asserts
    (`_assert_owner()`) that only the Textual event-loop thread ever
    touches it, constructed with `owner_thread=threading.current_thread()`
    at `RunScreen.__init__`. Fixed by adding this contract to ADR
    0008's Consequences section directly (the "other half" of this
    item — the ADR's blanket "worker threads do not share state with
    the event loop" claim — was already corrected during the
    `RunSession` TUI migration). Additionally, while auditing this
    code, promoted the reducer's own bounds (`_MAX_INVOCATIONS`,
    `_MAX_FEED_RECORDS`, `_MAX_TEXT_TAIL`, etc.) and its exact-
    directory-match (never prefix-match) event-attribution rule to a
    new companion ADR, since both were real, tested, load-bearing
    invariants with no ADR home. See
    `docs/decisions/0019-live-activity-reducer-bounds-and-event-
    filtering.md`.

21. **Correct merge-conflict repair instructions.**
    `README.md:267-272`.

22a. ~~Add missing end-to-end tests for signals (SIGINT/SIGTERM against
    a real headless process)~~ **Resolved.**
    `src/loop_supervisor/cli.py` (`_bridge_sigterm_to_keyboard_interrupt`,
    `cmd_run`, `cmd_resume`), `tests/test_signal_handling.py`,
    `docs/decisions/0015-sigterm-bridged-into-exception-driven-cleanup.md`.

    Originally filed as a testing gap, but writing the described tests
    against the as-shipped code would have encoded a real, unfixed
    behavior gap: the supervisor's entire cleanup path (stopping the
    OpenCode process group, stopping the permission denier, releasing
    the lock) is reached exclusively through Python exception
    unwinding. SIGINT's default disposition already raises
    `KeyboardInterrupt`, so Ctrl-C gets that cleanup for free; SIGTERM's
    default disposition is immediate termination with no Python-level
    unwinding at all. Confirmed empirically and against the real CLI
    spawned as a subprocess: a bare `kill <pid>` left the supervisor
    lock on disk and orphaned the OpenCode process group. This matches
    orphaned `opencode serve` processes and stale locks observed after
    killing stuck supervisor runs in earlier sessions.

    Fixed by bridging SIGTERM into the same `KeyboardInterrupt` path
    SIGINT already takes, scoped narrowly to the headless `run`/`resume`
    entry points (never `tui`, and never inside library code — see ADR
    0015 for the full scoping rationale, including the accepted
    exit-code tradeoff: a SIGTERM-terminated run reports 130, not 143).
    Verified end-to-end in `tests/test_signal_handling.py` by spawning
    the real CLI as a subprocess against the real OpenCode fixture and
    delivering a real SIGTERM; that test is confirmed to fail against
    the pre-fix code (lock retained, server orphaned) and pass once the
    handler is installed.

22b. **Add the remaining end-to-end coverage item 22 originally asked
    for**, now against a codebase where SIGTERM already behaves
    correctly (22a): app-level exit refusal/retry against a real
    OpenCode process, TUI initialization races beyond what this round's
    fakes exercise, and cleanup failures under real process-kill
    scenarios rather than monkeypatched `stop()`. Also covers TUI-side
    signal handling itself, which 22a deliberately left untouched: a
    real SIGTERM against a running `loop-supervisor tui` process still
    terminates at default disposition today (same orphan/stale-lock
    exposure 22a fixed for the headless path), and fixing it needs its
    own UX decision — does an externally raised interrupt need to
    restore the terminal before anything else, does it reuse
    `RunScreen`'s existing shutdown worker, etc. — not just a copy of
    22a's bridge.

23. ~~Investigate hidden `ResourceWarning`s in the full test suite~~
    **Closed: investigated, no cleanup gap found.** Originally raised
    while adding `filterwarnings` for
    `PytestUnhandledThreadExceptionWarning`/`PytestUnraisableExceptionWarning`
    (see `pyproject.toml`'s `[tool.pytest.ini_options]`), surfaced via
    `pytest tests/ -W always`, and reported as "9 hidden ResourceWarnings"
    with one — a subprocess leak attributed to
    `test_start_bounded_close_reports_thread_start_failure` — flagged as
    possibly a real cleanup gap since it appeared in a fault-injection
    test specifically about cleanup behavior.

    That specific claim was investigated directly and does not hold. A
    standalone reproduction of the malformed-anchor-identity startup
    path (the scenario most resembling the flagged test) confirmed the
    launcher is correctly reaped on that failure: `launcher.poll()`
    returns `-9` (terminated, not running), `launcher.stdout.closed` is
    `True`, and `OpenCodeServer._pending_launcher` is cleared to `None`
    — exactly what `stop()`'s cleanup contract requires.

    The warning itself turned out to be a GC-attribution artifact rather
    than a fixed location: `ResourceWarning` fires at object
    finalization, which pytest attributes to whichever test happens to
    be executing when the garbage collector runs the finalizer — not the
    test that created the leaked-looking object. Confirmed by four
    independent probes: (a) the subprocess warning's *blamed test name*
    changed across repeated full-suite runs (seen attributed to both
    `test_malformed_anchor_identity_retains_pending_until_reaped` and the
    originally-named `test_start_bounded_close_reports_thread_start_failure`);
    (b) the *count* of hidden warnings varied across runs (3, 8, and 9
    observed, so the original "9" was one sample of a non-deterministic
    count, not a stable figure); (c) running `test_opencode.py` alone
    never reproduces the subprocess warning at all; (d) instrumenting
    `subprocess.Popen.__del__` to report any object still running at
    finalization time found none across a full-suite run in which the
    warning still fired under plain `-W always` — and running under
    `-X tracemalloc=30` (which perturbs GC timing) made the warning
    disappear entirely, which is itself consistent with a
    finalization-timing artifact and inconsistent with a real fixed
    leak.

    Net effect: no code change is warranted, and the entry naming a
    specific test as the suspected leak site should not be trusted as
    stable — GC-attributed warnings will keep landing on whichever test
    is unlucky enough to be running at the next collection cycle. Escalating
    `filterwarnings` to `error` for `ResourceWarning` remains inadvisable,
    not because of a latent bug but because it would fail the suite
    non-deterministically based on GC timing rather than on any
    particular test's own behavior. The other two (pytest/`logging`
    internals) are unaffected by this finding and remain believed not
    ours to fix.

24. **Retry after an operational failure discovered mid-phase can lose
    operator-supplied guidance.**
    `src/loop_supervisor/supervisor.py:867-869`,
    `src/loop_supervisor/supervisor.py:1022-1024`.
    `_do_architecting()` and `_do_building()` clear
    `state.pending_question` (consuming any operator guidance/answer)
    before invoking the role and running its contract checks. If the
    role invocation or a downstream contract check then fails and the
    phase is retried, the consumed guidance is gone and the operator
    must resupply it. Pre-existing for every exception already routed
    through `_handle_operational_failure()`, not introduced by item 1's
    `ContractError` fix; not previously called out in this backlog.

25. ~~**`init --destination` bootstraps a fork of the supervisor, not a
    new project.**~~ **Resolved.** ADR 0004 stated copy-mode exists to
    seed "new, unrelated projects," but `cmd_init_copy()` copied every
    Git-tracked file — the full `src/loop_supervisor/` package, the
    test suite, ADRs 0001-0017 documenting supervisor internals, this
    project's own `docs/plans/`, and a `pyproject.toml` declaring
    `name = "loop-supervisor"`. Every agent role reads `README.md` and
    `docs/decisions/` as canonical truth, so a bootstrapped project
    pointed them at the supervisor's own design; the auditor's
    `pytest *` would run the supervisor's ~860 tests; the builder held
    `edit: allow` over supervisor source unrelated to its task.

    Fixed by resolving the open question this item posed: a
    bootstrapped project now **depends on** `loop-supervisor` as an
    installed package rather than vendoring it. `init --destination`
    writes a packaged skeleton (`src/loop_supervisor/_skeleton/`,
    shipped as package data and read via `importlib.resources`, so it
    needs no `.git` at all) containing only `.opencode/agents/*.md`,
    `opencode.json`, `.gitignore`, `.env.example`,
    `pyrightconfig.json`, `docs/decisions/README.md`, a stub
    `docs/OBJECTIVE.md`, and generated `README.md`/`pyproject.toml`
    scaffolds. `--in-place` (whose only purpose was de-`git`-ing a
    checkout that already looked like the supervisor's own source
    tree — a shape nothing produces under the dependency model) is
    removed outright. See
    `docs/decisions/0018-bootstrap-generates-a-dependent-skeleton-not-a-vendored-copy.md`,
    which supersedes ADR 0007's copy mechanism and narrows ADR 0004's
    two-bootstrap-mode design to one.

    Verified live with the same method as item 33/ADR 0017:
    `init --destination`, a distinctive `docs/OBJECTIVE.md`, then
    `run --max-steps 1` produced a `task-001` matching it exactly.
    `tests/test_cli_init.py` was rewritten (every prior test pinned
    the removed Git-checkout mechanism).

    Flagged, not fixed, as follow-up items: 34 (self-hosting
    regression: no supported way for a new project to also hack on
    `loop-supervisor`'s own source) and 35 (versioning: generated
    projects pin `loop-supervisor` to a Git URL with no released
    version yet, and agent-definition compatibility across upgrades is
    unenforced).

26. ~~`mypy src tests` (checked together) surfaces ~331 errors that
    `mypy src` and `mypy tests` (checked separately) do not.~~
    **Resolved.** `tests/test_runtime.py` (268), `tests/test_tui_app.py`
    (28), `tests/test_state.py` (16), `tests/test_advance.py` (8),
    `tests/test_opencode_events.py` (8), `tests/test_opencode.py` (3).
    All 331 are now fixed; the gate runs the combined `mypy src tests`
    invocation.

    The cause was a style choice, not something inherent to testing
    this way. A minimal two-file probe proved it: `m.GitRepo = Fake`
    (direct module-attribute assignment) errors under mypy with
    `misc`/`assignment`, because mypy can see the real type on the
    right-hand side and compare it against the left; the identical
    intent via `monkeypatch.setattr(m, "GitRepo", Fake)` is clean,
    because `setattr` accepts a bare attribute-name string that breaks
    the type comparison. Checked in isolation (the old `mypy tests`
    invocation), mypy also could not see `src`'s real types at all, so
    even the direct-assignment form passed — which is what let ~250
    such assignments accumulate silently in `tests/test_runtime.py`
    over time, undetected by either the old split gate or by local
    convention (the file already had 36 `monkeypatch.setattr` calls
    alongside 135 direct assignments, so there was no single dominant
    style to infer from). Fixed by converting the class/method
    assignments to `pytest.MonkeyPatch()` (used directly, not as a
    fixture, since two of the largest offending blocks were shared
    `@contextlib.contextmanager` helpers with 70+ call sites that
    cannot take the `monkeypatch` fixture) and `monkeypatch.setattr`
    where a fixture was already in scope; function assignments
    (`rt.load_state = ...`) were left untouched since they were
    already type-clean. See README.md's "Testing discipline" for the
    resulting convention.

    The remaining ~81 errors were dict-unpacking inference gaps
    (`defaults = dict(...); Cls(**defaults)` needing a `dict[str,
    Any]` annotation — including `tests/test_runtime.py:53` and
    `tests/test_tui_app.py:70`, previously miscategorised in this
    project's session notes as pre-existing LSP/pyright noise to
    ignore; they are real mypy findings, only invisible under the old
    split-invocation gate) and `union-attr` reaches through `X | None`
    attributes (`RunSession | None`, `_LockLease | None`, etc.) without
    an `assert x is not None` guard first. Verified none of these
    indicated a real production bug: in every case the corresponding
    `src` code already guards the same attribute correctly (e.g.
    `session = self._session` followed by a check in
    `tui/app.py:370`+), and only the tests were reaching through
    unguarded. Fixed with a targeted `assert ... is not None` at each
    site, which also documents the test's actual precondition. A
    handful of remaining cases were genuinely-intentional duck-typed
    fakes or bare `object()` identity sentinels passed where a real
    type was expected; those got a targeted `# type: ignore[arg-type]`
    with a one-line reason rather than a signature change, since
    widening the real signatures to accept `object` was out of scope
    and would have weakened `src`'s actual type guarantees.

27. ~~A permission `ask` in a headless agent run stalls the phase for
    the full `role_timeout` with no diagnostic.~~ **Resolved.** (Tier 3
    — reliability)
    `src/loop_supervisor/opencode.py:1196-1245`,
    `src/loop_supervisor/opencode.py:257`,
    `docs/decisions/0014-server-mode-permission-defaults.md`.
    ADR 0014 set `external_directory` and `doom_loop` to `deny` so
    that an agent action needing permission fails fast instead of
    blocking on a prompt nobody can answer. That closes the two
    observed cases but leaves the general shape open: any permission
    that evaluates to `ask` in server mode has no responder, because
    the supervisor speaks to OpenCode over plain HTTP
    (`POST /session/{id}/message`) and never opens the event stream in
    the headless path — `sse.py` and `opencode_events.py` are
    imported only by `tui/app.py`. The prompt is raised server-side
    and waits.

    The consequence is bounded but poor: `send_prompt` builds its
    client with `timeout=timeout` (default 1800.0), so the request
    does eventually raise `PhaseTimeoutError` and the phase is retried
    — after a silent 30-minute stall, reported as a generic timeout
    with nothing pointing at the real cause. Diagnosis currently
    requires reading `~/.local/share/opencode/log/opencode.log` for
    `action.action=ask`, which is how the original instance was
    eventually found (runs `40e0c0bd`, `be71f648`, `e3e6e838`, all
    `loop-auditor` globbing for `pytest`/`ruff`/`mypy` outside the
    project root before the PATH fix).

    Note this is a distinct failure from the PATH gap ADR 0014 also
    fixed: the PATH fix removed the *reason* those particular asks
    fired, and the deny defaults convert them to fast failures, but
    neither prevents a future `ask` on some permission not currently
    listed. An ask still fired on 2026-08-28 (an interactive run
    reaching `/home/node/.local/share/rtk/tee/*`, 14:47 UTC), confirming
    that allowlisting only covers paths that were predicted in advance.
    Zero asks have been observed in a `loop-auditor`/`loop-builder`/
    `loop-architect`/`loop-planner` run since the PATH fix landed
    (2026-08-28T02:09), only in interactive sessions.

    Recommended fix, cheapest first: (a) log a loud warning naming the
    permission when a phase times out, so the 30-minute stall is at
    least self-diagnosing; (b) a config-level catch-all deny, tracked
    separately as item 28, which is the stronger and now-verified fix
    and should be tried before this item's (a); (c) have the headless
    path consume the event stream (reusing `sse.py` and
    `normalize_global_event`) and auto-deny pending permission
    requests, which is the only option that gives a precise error at
    the moment of the ask, but is materially more work — it puts an
    event consumer in the previously synchronous headless path, with
    its own lifecycle and teardown obligations under the ADR 0009
    lock/cleanup contract. Try (b) first; only pursue (a) as a
    diagnostic backstop and (c) if asks recur after (b) ships.

    **Resolution:** (c) was implemented directly rather than as a
    last resort, because `Permission.evaluate`'s `ask` fallback is
    hard-coded (`?? {action: "ask", ...}`) — no config, including
    item 28's catch-all, can eliminate it for every possible
    permission key, only narrow the surface. `permissions.
    PermissionDenier` starts alongside the OpenCode server in
    `RunSession.start_server()`, subscribes to `GET /global/event`
    (reusing `sse.py`/`normalize_global_event` exactly as anticipated
    above — SSE is no longer TUI-only), and replies `reject` to every
    `permission.asked` event via `POST /permission/{requestID}/reply`.
    Both the event name and the reply route/body were verified against
    the OpenCode 1.18.22 binary's own compiled route table, and the
    approach mirrors that binary's own client-side `mode: "auto"`
    auto-reply path (inverted to `reject` rather than `once`/approve).
    `RunSession.close()` stops the denier before the server itself is
    stopped. A denier fault (start failure, reply transport error,
    non-2xx reply status) is swallowed and never fails the run,
    matching `sse.py`'s own "SSE failure is strictly non-fatal"
    contract — the same posture used throughout this module already.
    Denial counts/summaries are in-memory only, not persisted to
    `RunState` (see item 30 for why); `run_new`/`run_resume` print a
    one-line stderr diagnostic (`denied N permission request(s)
    (...)`) when any occurred, closing the "no diagnostic" half of
    this item without the weaker warn-on-timeout option (a). Item 28
    (config-level catch-all) remains worthwhile as defence-in-depth
    but is no longer required to close this item.

    **Follow-up correction (still item 27, not a new item):** the
    initial `PermissionDenier` implementation's reply omitted the
    `directory` query parameter, which is not optional —
    `POST /permission/{requestID}/reply` is not implicitly scoped to
    the session that raised the ask, and an unscoped reply resolves
    against the server's default/current instance. Because the project
    root instance is only used by `loop-planner`, every ask from
    `loop-architect`/`loop-builder`/`loop-auditor` (raised from a task
    worktree's own instance) 404'd silently — confirmed live against a
    real paused run, root-caused by comparing a succeeded (planner,
    project root) and a failed (auditor, worktree) denial in OpenCode's
    own log, and fixed by passing the ask's own `directory` (already
    present on the event envelope) through to the reply. See ADR 0016.
    Also fixed in the same pass: `_reply_reject` previously discarded
    the HTTP status/exception on any failure, which is why root-causing
    the live failure required manually reading OpenCode's own log
    instead of the supervisor's own output explaining itself.

28. **Add a config-level catch-all `deny` so no permission can ever
    evaluate to `ask`, closing the general case behind item 27.**
    `opencode.json`. Verified against the installed OpenCode 1.18.22
    binary (`/home/node/.npm-global/lib/node_modules/opencode-ai/bin/opencode.exe`)
    by reading its (minified) permission-resolution code directly,
    since 1.18.22 ships compiled and the published JSON Schema at
    `https://opencode.ai/config.json` documents the shape of
    `permission` but not its runtime semantics:

    - `Permission.fromConfig` (config → ruleset) flattens each
      permission entry; a bare string value (e.g. `"doom_loop":
      "deny"`) becomes one rule with `pattern: "*"`, and an object
      value becomes one rule per key, with `~`/`$HOME` expanded in the
      *pattern* only, never in the permission key.
    - `Permission.evaluate` picks the **last** ruleset entry (in
      object-insertion order) where both the permission name and the
      pattern match, via `Array.prototype.findLast`, and falls back to
      `{action: "ask"}` if nothing matches. Critically, the permission
      *key* itself is matched with the same wildcard matcher as the
      pattern (`g.match(requestedPermission, rule.permission)`), so a
      `"*"` key is a valid catch-all across every permission type, not
      just `external_directory`-style path patterns.
    - Consequently, `{"permission": "deny"}` (a bare top-level string)
      is schema-valid per `PermissionConfig`'s `anyOf` but does
      **nothing** here — `Object.entries` over a string yields no
      rules, so it silently falls through to the same `ask` default.
      The catch-all must be a `"*"` **key**, and because
      `findLast` favours later entries, it must be the **first** key
      in the `permission` object so any more specific rule after it
      (`external_directory`, `doom_loop`, etc.) still wins:

      ```json
      "permission": {
        "*": "deny",
        "external_directory": {"*": "deny", "/workspaces/loop-tui-experiment": "allow"},
        "doom_loop": "deny",
        "bash": "allow", "read": "allow", "edit": "allow", "write": "allow",
        "glob": "allow", "grep": "allow", "list": "allow", "task": "allow"
      }
      ```

    Not yet applied: `Permission.evaluate` merges the session-level
    ruleset from `opencode.json` with each of the four `.opencode/
    agents/*.md` agent-level `permission:` blocks
    (`de.merge(agent.permission, session.permission ?? [])`), and a
    top-level `"*": "deny"` will win over anything the agent files
    don't already re-allow. Applying this requires auditing all four
    agent permission blocks against the re-allow list above and a live
    smoke run of at least one full task cycle to confirm no agent
    action that currently succeeds starts asking or gets denied.

29. **`resume` on a terminal run silently no-ops after needlessly
    starting OpenCode.**
    `src/loop_supervisor/cli.py:177-181`,
    `src/loop_supervisor/runtime.py:1434-1442`,
    `src/loop_supervisor/supervisor.py:469-475`, `:719`.
    Discovered live: a completed run's state (`phase: "done"`) is not
    rejected by `resume`, so nothing prevents pointing `resume` at a
    finished run_id. `_paused_phase_message` returns `None` for
    `TERMINAL_PHASES`, so `cmd_resume` prints only `final phase: done`
    and exits 0 — indistinguishable from a resume that performed real
    work and completed. An operator has to open the state JSON to
    learn why nothing happened.

    Worse, the no-op is discovered late rather than up front.
    `run_resume` acquires the supervisor lock and calls
    `session.start_server()` (spawning a real `opencode serve`
    process) *before* `run_to_completion` ever reaches
    `Supervisor.run()`'s `while state.phase not in _TERMINAL_PHASES`
    loop guard, which is what actually exits immediately for a
    terminal phase. A resume that is a guaranteed no-op therefore
    still pays for a full server spawn and teardown — the same
    lifecycle already responsible for this session's orphaned-server
    and stale-lock incidents (see the live-run environment notes on
    `nohup`-managed steps).

    This is not a reason to allow reopening a finished run — ADR 0006
    and README's "Operational failure and retry" section are
    deliberate on that point ("no further resume is possible. Start a
    new run."), and nothing here argues for relaxing it. This item is
    about the *reporting and cost* of the already-correct no-op, not
    the invariant itself.

    Fix: reject terminal phases early — in `cmd_resume` or
    `_validate_resume` — before the lock is acquired and before
    `start_server()` runs, with an actionable message naming the run,
    its phase, and `accepted_task_count` (e.g. "run 2dba05654b5e is
    already done (6 tasks accepted); start a new run with
    'loop-supervisor run'") and a non-zero exit. Note `cmd_resume`'s
    `return 0 if final.phase == "done" else 1` currently conflates
    "finished just now" with "was already finished before this
    invocation"; an early rejection sidesteps that ambiguity rather
    than trying to distinguish the two after the fact.

30. **Squash `RunState` schema migrations (currently v2→v3) into a
    single current version.** `src/loop_supervisor/state.py`
    (`STATE_SCHEMA_VERSION`, `_migrate_v2_to_v3`, `_V2_FIELDS`,
    `_V3_ONLY_FIELDS`, `V2_PHASES`).

    This project has no users and no production installs — every
    `RunState` document that has ever existed was created by this
    codebase, in this repo, during development. There is no real
    document anywhere carrying schema v1 or v2 that a migration needs
    to keep loading. The v2→v3 migration path (`_migrate_v2_to_v3`,
    plus the strict v2-field-set/v2-phase-vocabulary enforcement
    around it) is pure carrying cost: real code, real tests, and a
    real audit surface, purchased for compatibility nobody needs.

    This showed up concretely while implementing item 27's resolution
    (`permissions.PermissionDenier`): the natural place to persist
    denied-permission counts/summaries would have been a new
    `RunState` field, but `RunState.from_dict`'s exact-field-set
    validation (`known - keys` / `keys - known`, both fatal) means any
    new field requires bumping `STATE_SCHEMA_VERSION` and writing a
    `_migrate_v3_to_v4` mirroring the existing v2→v3 machinery. That
    cost was avoided for now by keeping denial counts in-memory only
    (see item 27's resolution note), but the next legitimate field
    addition will face the identical tax, and it only compounds:
    v2→v3, then v3→v4, then v4→v5, forever, for a schema whose only
    real consumers are this repo's own tests and a handful of
    throwaway runs in `test-run/.git/loop-supervisor/runs/`.

    Fix: pick a point (ideally right before or right after this
    backlog closes out) to collapse the schema to a single current
    version — delete `_migrate_v2_to_v3`, `_V2_FIELDS`,
    `_V3_ONLY_FIELDS`, `V2_PHASES`, and the v1/v2 rejection branches in
    `RunState.from_dict`, and drop `STATE_SCHEMA_VERSION` back to a
    single implicit "current" shape (or reset it to 1 with a comment
    explaining the reset, if a fixed starting number is preferred).
    Any existing run-state files on disk at that point are dev
    artifacts and can simply be deleted rather than migrated. Revisit
    this policy (i.e. start taking migrations seriously again) only if
    the project ever acquires a real installed user base whose
    in-flight run state would need to survive an upgrade.

31. **Decide whether the TUI should get its own say over
    `permission.asked` auto-denial, rather than silently inheriting
    the headless denier.** `src/loop_supervisor/tui/app.py`,
    `src/loop_supervisor/permissions.py`.

    Corrected premise (see `docs/decisions/0021-tui-drives-
    runsession-in-process.md`): `PermissionDenier` is **not**
    headless-only today. It is constructed inside
    `RunSession.start_server()` (`runtime.py:983`), and the TUI calls
    that exact method (`tui/app.py:597`) — so a permission `ask`
    raised during a TUI-driven run is already being auto-denied by
    the same denier the headless CLI uses, with no TUI involvement in
    the decision. What is genuinely headless-only is the
    *reporting*: `_report_denied_permissions` (`runtime.py:1449`) is
    called only from `run_new`/`run_resume`, so the TUI silently
    inherits the denial behavior without surfacing it (see the fix
    that reads `denied_permission_count`/`denied_permission_summary`
    into the durable pane, addressing the reporting half only). The
    open question this item still tracks is a real UX/design one: is
    "the headless denier decides, TUI just gets told" the right
    answer, or should an operator watching the TUI be able to see the
    request and reply themselves before it is auto-rejected? That
    needs its own answer plus a second SSE consumer sharing one
    connection's event dispatch, not just reusing `PermissionDenier`
    as-is.

32. **Consider having the supervisor provision each task worktree's
    `.venv` itself, instead of relying on the builder agent to do it.**
    `src/loop_supervisor/supervisor.py` (`_do_creating_worktree`),
    `src/loop_supervisor/opencode.py` (`build_agent_env`),
    `docs/decisions/0014-server-mode-permission-defaults-and-venv-path.md`.

    ADR 0014 already establishes that each task worktree must have its
    own `.venv` — never symlinked or shared with the integration
    project's, because an editable install's `.pth` file (and every
    console-script shebang under `.venv/bin`) embeds an absolute path,
    so a shared venv would silently run verification against the
    wrong checkout's source. Today, creating that venv is left
    entirely to the builder agent's own initiative: ADR 0014's
    consequences note that `test-run-task-002`'s builder "already did
    unprompted," which is another way of saying it works because an
    agent happened to choose to, not because the supervisor's design
    guarantees it. `build_agent_env` (`opencode.py`) already prepends
    a *relative* `.venv/bin` to `PATH` unconditionally, specifically
    so it resolves per-worktree at each command's exec-time — the
    supervisor already anticipates a per-worktree venv existing; it
    just doesn't create one.

    Provisioning is unlikely to belong as unconditional behavior of
    `_do_creating_worktree` itself: `GitRepo` and the worktree
    lifecycle are language-agnostic, while `python3 -m venv &&
    pip install -e ".[dev]"` is a Python-specific convention that
    happens to be this project's own. The likelier shape is an
    opt-in hook on `RunOptions` (a configurable provisioning command,
    or none), defaulting to today's behavior — nothing changes for an
    existing project unless it opts in.

    One appealing refinement is having the supervisor learn what to
    provision from the agents' own prior work rather than requiring
    static configuration — e.g. `BuilderResult.tests_run` or
    `implementation_strategy` (`contracts.py`) already carry signal
    about what tooling a build actually needed. This does not work
    today without a further change: `state.builder_result` is reset
    to `None` at every task boundary (`supervisor.py`, in both the
    replan and task-acceptance paths), specifically so a new task
    starts from a clean slate, so nothing currently survives from one
    task's builder run to inform the next worktree's setup. Making
    this work would mean persisting some distilled form of that
    signal across task boundaries, which — per item 30's rationale —
    means a `RunState` schema change and should be scoped and decided
    on its own merits, not assumed as part of provisioning itself.

    Also unresolved: who owns a provisioning failure (hard-fail the
    `creating_worktree` phase, or proceed and let the builder cope as
    today), the per-task cost of a full editable install versus the
    agent turns currently spent redoing it, and whether
    `cleanup_worktree` needs any awareness that a venv was
    supervisor-created (it does not today, since `git worktree
    remove` already takes the whole directory, `.venv` included).

33. ~~**No objective channel exists between a standalone OpenCode
    session and the loop supervisor; `docs/plans/` is invisible to
    every agent prompt.**~~ **Resolved.** A fresh run's planner prompt
    was the single line `"Determine the next unit of work."`
    (`supervisor.py:1528`), with no supervisor-read project files and
    no `objective`/`goal`/`spec`/`brief` field anywhere in `RunState`,
    `RunOptions`, or the CLI. All scope derived from
    `.opencode/agents/loop-planner.md` naming exactly `README.md` and
    `docs/decisions/`; `docs/plans/` was tracked and actively used but
    named by zero agent prompts. `AGENTS.md` could not fill this role:
    gitignored, never copied by `init`, referenced by zero code and
    zero prompts, and its actual content (RTK/Falda host tooling) was
    the wrong artifact regardless. Fixed by adding `docs/OBJECTIVE.md`
    as a named canonical source in all four agent prompts (ahead of
    `README.md`/`docs/decisions/`), adding `docs/plans/` to the
    planner and architect prompts, documenting the handoff procedure
    in `README.md`, and writing this repository's own
    `docs/OBJECTIVE.md` as the worked example. See
    `docs/decisions/0017-objective-channel-is-a-tracked-file.md` for
    why this is a tracked file rather than a `--objective`/`RunState`
    prompt-injection parameter, and what would supersede it once item
    30's schema squash lands.

34. **No supported way for a new project to also hack on
    `loop-supervisor`'s own source (self-hosting regression from item
    25's fix).**
    `docs/decisions/0018-bootstrap-generates-a-dependent-skeleton-not-a-vendored-copy.md`.
    This repository improves itself via its own loop today — the
    builder can freely edit `src/loop_supervisor/` because that source
    is right there in the checkout. Once a new project depends on
    `loop-supervisor` as an installed package (item 25's fix), it has
    no supervisor source to edit at all, and no supported bootstrap
    mode reintroduces one. A project that specifically wants to
    co-develop the supervisor alongside its own work (as this
    repository does) has no path there other than manually cloning
    `loop-supervisor` itself and switching its dependency to a local
    editable path. If this is judged to matter, the likely shape is an
    `init --fork` mode that vendors (today's old behavior, minus the
    scope problem item 25 fixed) as an explicit opt-in alternative to
    the new default, not a repurposing of the removed `--in-place`.

35. **Versioning: generated projects pin `loop-supervisor` to a Git
    URL with no released version, and agent-definition compatibility
    across upgrades is unenforced.**
    `src/loop_supervisor/_skeleton/pyproject.toml.tmpl`,
    `docs/decisions/0018-bootstrap-generates-a-dependent-skeleton-not-a-vendored-copy.md`.
    The generated `pyproject.toml`'s dependency is `loop-supervisor @
    git+<url>` with an explicit "TODO: pin this to a released version
    or tag once one exists" comment, because no released version
    exists yet. Once one does, a generated project and the
    `loop-supervisor` version it depends on can drift: the four
    `.opencode/agents/*.md` files are a point-in-time copy made at
    `init` time, not something re-synced on a `pip install --upgrade`,
    so an upgraded supervisor's expectations about, e.g., the
    structured JSON contract (`contracts.py`) or prompt content could
    silently diverge from what the copied agent definitions actually
    say. No compatibility check exists between an installed
    `loop-supervisor` version and a project's own copied agent
    definitions.

36. **TUI startup-failure deadlock: the single reusable
    `_shutdown_complete_event` can be awaited by a caller with no
    attempt in flight to ever set it.** (Tier 1 — correctness)
    `src/loop_supervisor/tui/app.py:407`, `:906` (`_maybe_start_-
    shutdown_attempt`), `:931` (`await_shutdown_complete`).
    Migrated from `docs/plans/2026-08-22-second-lifecycle-fix-
    plan.md`'s Step 5 (blocker 1), which is the only place this defect
    was tracked; it does not appear anywhere in this backlog before
    now. Confirmed still present: `app.py:407` still constructs one
    `threading.Event()` per `RunScreen`, not one per shutdown attempt.
    Successful cleanup after a failed `_do_initialize_locked()` can set
    `shutdown_clean = True` without ever setting this event (no
    `_shutdown_worker` attempt ran to signal it); a later app exit or
    "q" press that awaits the event then waits forever. Step 4's TUI
    ownership registry fix (resolved, this backlog's item 12) narrowly
    worked around one instance of this ("already clean with nothing to
    signal" is checked before awaiting) without redesigning the
    underlying single-event design, and its own commit says so
    explicitly.

    Fix design carried over from the source plan: replace the single
    reusable event with a per-attempt handle (generation counter + its
    own `threading.Event`); `request_shutdown()`/
    `_maybe_start_shutdown_attempt()` returns the existing in-flight
    attempt, a newly started one, or "already clean" (nothing to wait
    for), synchronized under a dedicated attempt lock; every caller
    (`_on_exit_app()`, `"q"`, "Return to runs") awaits only a real
    attempt handle it holds, never a bare shared event. Needed tests:
    app exit after clean startup-failure cleanup completes without
    deadlock; `"q"`/"Return to runs" work after clean startup-failure
    cleanup; distinct attempt generations don't cross-signal; app exit
    requested mid-failed-init-cleanup waits correctly and then
    proceeds.

37. ~~`_confirm_server_stopped()`'s inter-attempt backoff uses
    `time.sleep()`, which is not interrupt-safe.~~ **Resolved.**
    `src/loop_supervisor/runtime.py:196-224` (`_confirm_server_stopped`).
    Migrated from the same Step 3 audit trail as the now-resolved
    denial-routing and traceback-preservation findings (this backlog's
    items 26/27's neighbors in that plan), but explicitly called out
    there as remaining open across three separate remediation rounds
    ("Findings 3–4 ... remain unresolved") and never carried into this
    backlog until now.

    A `KeyboardInterrupt`/`SystemExit` delivered while
    `_confirm_server_stopped()` was inside its inter-attempt
    `time.sleep()` previously escaped the function entirely, bypassing
    the "returns structured success/failure information instead of
    raising" contract its own docstring claims — an interrupt raised
    by `stop()` itself already got that treatment (reported via
    `last_error`, not re-raised), but the identical interrupt arriving
    one line later, during backoff, did not.

    Fixed by wrapping the backoff `time.sleep()` call in its own
    `except (KeyboardInterrupt, SystemExit)`, returning the same
    `_CleanupOutcome(confirmed=False, last_error=<interrupt>, ...)`
    shape as the `stop()`-raises case, so both interrupt windows are
    now indistinguishable to every caller. Removed the module
    docstring's "Known limitation" paragraph, which is no longer true.
    See `tests/test_runtime.py`
    (`test_confirm_server_stopped_interrupt_during_backoff_reported_not_raised`,
    `test_confirm_server_stopped_system_exit_during_backoff_reported_not_raised`).

38. ~~Cleanup-time `KeyboardInterrupt`/`SystemExit` retry/exhaustion
    edge cases across `_confirm_server_stopped()`'s bounded retry loop
    lack test coverage.~~ **Resolved.**
    `src/loop_supervisor/runtime.py:196-224`.
    Migrated from the same Step 3 finding as item 37 (the two were
    always paired as "findings 3–4" in the source plan and never
    resolved). Distinct from item 37: this was a coverage gap for
    behavior that may already have been adequate, not a known defect.

    Added `test_confirm_server_stopped_interrupt_on_final_attempt_reported_not_raised`,
    covering an interrupt raised by `stop()` on the last attempt (the
    budget is exhausted regardless, so no backoff would occur even
    without the interrupt) — confirms `attempts` is reported correctly
    and the interrupt is not swallowed by the loop's normal exhaustion
    path. Combined with item 37's two new tests, both the "between
    attempts" and "on the final attempt" cases from this item's
    description are now covered.

39. **Unsafe `str()` interpolation of arbitrary exceptions in three
    `opencode.py` HTTP-error-message sites, outside the one path Step
    3's remediation already fixed.** (Tier 1 — correctness/security)
    `src/loop_supervisor/opencode.py:784` (`_parse_anchor_identity`'s
    `getpgid` failure message), `:1177` (`create_session`'s
    `httpx.RequestError` message), `:1238` (`send_prompt`'s), `:1297`
    (`_abort_session_bounded`'s).
    Migrated from `second-lifecycle-fix-plan.md`'s Step 3 remediation
    trail, which fixed exactly one instance of this pattern (the
    `OSError`→`ServerStartupError` normalization in `start()`, this
    backlog's resolved item 27's neighbor) via `_safe_exception_text()`
    and explicitly named these four as "out of scope for this narrowly
    targeted fix." All four remain plain f-string `{exc}` interpolation
    of an exception whose `__str__` this code does not control (a
    third-party `httpx` exception, or a `getpgid` `OSError`); a
    throwing `__str__` on any of them would raise from inside message
    construction itself, escaping in place of the intended
    `AgentInvocationError`/`ServerStartupError` and losing whatever
    that intended error was reporting — the same class of bug ADR-
    adjacent Step 2 (`opencode._safe_exception_text`) and Step 3's
    fixed instance both exist specifically to prevent. Fix is
    mechanical: route all four through the existing
    `_safe_exception_text()` helper, matching the fixed instance's
    pattern exactly.

40. **TUI module layout never matched the planned split; `tui/app.py`
    has grown to ~1,400 lines holding both `RunBrowserScreen` and
    `RunScreen`.** (Tier 5 — documentation/testing debt)
    `docs/plans/2026-08-21-tui-vertical-slice.md` (archived; see below)
    §16 named a `tui/` package with separate `screens.py` and
    `widgets.py` modules; the actual package is `__init__.py`,
    `app.py`, `live.py`, `messages.py`, `renderers.py`, with both
    screen classes living in `app.py`. This is a maintainability
    observation, not a defect: nothing is currently broken by the
    combined file. Worth a real decision (revisit the split, or
    formally drop it) rather than carrying it as silent drift from an
    abandoned plan.

41. **`tests/fixtures/fake_opencode.py` emits only the legacy
    `structured_output` response shape; the canonical `info.structured`
    shape it was supposed to gain has no test coverage on this
    fixture.** (Tier 5 — documentation/testing debt)
    `tests/fixtures/fake_opencode.py:19,209`.
    `docs/plans/2026-08-21-tui-vertical-slice.md` §26 asked for both
    the canonical and legacy shapes; only the legacy one
    (`{"structured_output": ...}`) was ever implemented. `opencode.py`
    itself already supports the canonical shape with a legacy fallback
    (`info["structured"]`, per that plan's §9) — this is purely a gap
    in what the test fixture can simulate, not in production code.

42. **No rendering-level TUI test coverage: run-browser rendering,
    narrow-layout behavior, Rich-markup escaping, and bounded live
    output are all untested.** (Tier 5 — documentation/testing debt)
    `tests/test_tui_app.py` (30 tests, confirmed all lifecycle/
    registry/shutdown-focused; none touch rendering).
    Migrated from `docs/plans/2026-08-21-tui-vertical-slice.md` §28,
    which named these specifically. Distinct from this backlog's item
    22b (real-process exit/init/cleanup/signal behavior): this item is
    about what the TUI *displays*, not its process lifecycle. In
    particular, "Rich markup escaping" is a real correctness concern
    if any rendered text ever includes agent- or repository-controlled
    content (a commit message, a file path, an error string) that
    could contain literal Rich markup syntax.

43. **Opt-in pruning for OpenCode sessions the supervisor created.**
    (Tier 5 — documentation/testing debt)
    `src/loop_supervisor/opencode.py` (`run_agent`, `create_session`),
    `docs/decisions/0020-opencode-server-and-session-lifetimes.md`.
    The supervisor never deletes an OpenCode session; every agent
    invocation leaves one behind in OpenCode's on-disk, machine-global
    SQLite store (observed at
    `~/.local/share/opencode/opencode.db`), shared with interactive
    OpenCode use. Measured on one long-lived development machine:
    supervisor sessions (`title LIKE 'loop:%'`) were 51 of 265 total
    and roughly 22 MB of a 1.4 GB database (~1.6%, ~116 KB/session).
    This is real but slow-moving housekeeping, not a stability or
    memory problem — the server process itself holds no session state
    in memory (see ADR 0020) — and is deliberately not fixed here: the
    `event`/`part` rows for a session are the forensic record of what
    an agent did during a phase, and unconditionally deleting them on
    run completion would remove exactly the evidence needed to debug a
    failed run. If this is worth fixing, the likely shape is an
    opt-in, explicit pruning command (e.g. `loop-supervisor prune
    --older-than N`) rather than automatic deletion, and it must
    filter strictly on `title LIKE 'loop:%'` since the database is
    shared with non-supervisor OpenCode usage. Two implementation
    notes for whoever picks this up: OpenCode 1.18.22 does expose
    `DELETE /session/{sessionID}`, but the supervisor does not call it
    today and `tests/fixtures/fake_opencode.py` has no route for it
    yet; and a bare `DELETE` will not shrink `opencode.db` on disk
    without a subsequent `VACUUM`, since SQLite only frees the pages
    for reuse.

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
