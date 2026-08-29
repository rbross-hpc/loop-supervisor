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

17. **`tui` should validate new-run options the same way `run` does.**
    `src/loop_supervisor/cli.py:257-302`.

18. **Define and propagate meaningful TUI process exit status.**
    `src/loop_supervisor/cli.py:234-250`.

19. **Add parser event-size limits and reconnect/backoff acceptance
    coverage** for the SSE client.

## Tier 5 — documentation/testing debt

20. **Document `LiveActivityReducer`'s single-owner-thread contract in
    ADR 0008.** `src/loop_supervisor/tui/live.py:104-127`. The reducer
    asserts (`_assert_owner()`) that only the Textual event-loop thread
    ever touches it, constructed with `owner_thread=threading.current_thread()`
    at `RunScreen.__init__`; ADR 0008 does not document this contract or
    the assertion that enforces it. (The other half of this item — the
    ADR's blanket "worker threads do not share state with the event
    loop" claim — was corrected during the `RunSession` TUI migration:
    see ADR 0008's Consequences section, which now distinguishes UI/
    widget state, which is not shared, from lifecycle state, which is
    deliberately shared and guarded by `threading.Event`/`RunSession`'s
    own concurrency primitives.)

21. **Correct merge-conflict repair instructions.**
    `README.md:267-272`.

22. **Add missing end-to-end tests** for signals (SIGINT/SIGTERM against
    a real headless process), app-level exit refusal/retry against a real
    OpenCode process, TUI initialization races beyond what this round's
    fakes exercise, and cleanup failures under real process-kill
    scenarios rather than monkeypatched `stop()`.

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

25. **`init --destination` bootstraps a fork of the supervisor, not a
    new project.**
    `src/loop_supervisor/cli.py:211-253`,
    `docs/decisions/0004-template-bootstrap.md`,
    `docs/decisions/0007-tracked-files-only-bootstrap-copy.md`.
    ADR 0004 states copy-mode exists to seed "new, unrelated projects."
    In practice `cmd_init_copy()` copies every Git-tracked file — all 60
    — of which only about 8 belong in a new project. The destination
    receives the 21-file `src/loop_supervisor/` package, the 17-file
    test suite, ADRs 0001-0009 (all describing supervisor internals:
    lock leases, `RunSession`, TUI threading), this project's own
    `docs/plans/`, a 380-line `README.md` about the supervisor, and a
    `pyproject.toml` declaring `name = "loop-supervisor"` with its
    dependencies and console script.

    This is not merely untidy. The planner, builder, and auditor all
    read `README.md` and `docs/decisions/` as canonical truth, so a
    freshly bootstrapped project points them at the supervisor's design
    rather than their own. The auditor holds `pytest *` and is asked to
    judge "test adequacy," so every audit would run the supervisor's
    ~790 tests. The builder holds `edit: allow` over supervisor source
    unrelated to its task.

    Wanted in a new project: `.opencode/agents/*` (4),
    `docs/decisions/README.md` (the ADR format contract),
    `opencode.json`, `.gitignore`, `.env.example`. Not wanted:
    everything else.

    ADR 0007 tightened copy-mode's *safety* (tracked-files allowlist
    instead of a name denylist) but did not revisit its *scope*; the
    two ADRs together still promise a project seed and deliver a fork.

    Fixing this is a design change, not a filter tweak, and should
    resolve an open question first: does a bootstrapped project
    **depend on** `loop-supervisor` as an installed tool, or **vendor**
    it? Today it implicitly vendors. The tool model would additionally
    require the packaged-resource bootstrap ADR 0007 defers, since
    copy-mode currently cannot run from an installed wheel. A real fix
    also needs generated (not copied) `README.md` and `pyproject.toml`
    scaffolds — the codebase has no templating today — and would
    require amending ADRs 0004 and 0007 and reworking
    `tests/test_cli_init.py`, which pins current behavior.

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

31. **Route `permission.asked` auto-denial through the TUI's own live
    SSE connection too.** `src/loop_supervisor/tui/app.py`,
    `src/loop_supervisor/permissions.py`.

    Item 27's `PermissionDenier` was deliberately scoped to the
    headless path only (`RunSession.start_server()`/`close()`), where
    it closes a real gap: SSE was previously TUI-only, so a headless
    run had no permission-response channel at all. The TUI already
    subscribes to `GET /global/event` via its own `SSEClient`
    (`RunScreen._on_sse_event` in `tui/app.py`) and already renders
    permission-adjacent state, so an operator watching the TUI can, in
    principle, notice and react to an `ask` — the case for an
    automatic responder there is weaker and the UX tradeoff (should
    the TUI ever auto-deny on the operator's behalf, or only surface
    the pending request?) is a real design question, not just a
    wiring change. Tracked separately rather than folded into item 27
    because it needs its own answer to that question plus a second SSE
    consumer sharing one connection's event dispatch, not just
    reusing `PermissionDenier` as-is.

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
