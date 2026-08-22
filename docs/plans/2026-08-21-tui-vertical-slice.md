# Plan: first Textual TUI vertical slice

Branch: `feature/tui-vertical-slice`
Worktree: `/workspaces/loop-tui-experiment/loop-tui-experiment-tui-vertical-slice`
Status: not started (persisted before context compaction)

## Goal

Add a responsive Textual 6.x TUI that can:

- Start a new supervisor run or select a saved run.
- Display durable phase/task/counter/result state.
- Display best-effort live OpenCode activity over SSE.
- Collect all existing operator inputs.
- Exit cleanly on completion, pause, or operational failure.
- Retry resumable failures after restart.
- Prevent concurrent mutating supervisors against one Git repository.

Keep streaming cancellation, diff browsing, full logs, parallel tasks, run
deletion, and automatic merge-conflict resolution out of scope.

## Agreed decisions

1. **Framework:** Textual 6.x with Rich, following Falda analysis UI
   patterns (`/workspaces/falda/analysis`).
2. **Execution:** `Supervisor` remains synchronous. Each `advance()` runs
   in a Textual thread worker.
3. **State:** persisted `RunState` is authoritative; OpenCode SSE is
   ephemeral live telemetry only.
4. **Live data:** subscribe to `GET /global/event`, because invocations
   move between the integration checkout and task worktree.
5. **Locking:** supervisor-owned lock under
   `<git-common-dir>/loop-supervisor/supervisor.lock`; Git itself is
   unaware.
6. **Failure behavior:** operational failures persist a resumable error
   and stop the current execution. Resume retries the interrupted phase.
   Merge conflicts require operator repair before retry; corrupt/tampered
   resume state is reported without overwriting the last good state.
7. **Initial UI scope:** run browser, new/resume, durable state, live
   activity, pending-input forms, retry/error display.

## Git workflow

- Work only in this worktree, on `feature/tui-vertical-slice`.
- Do not commit, merge, or push unless explicitly requested.
- Do not touch the primary checkout at
  `/workspaces/loop-tui-experiment/loop-tui-experiment`.

---

## Phase 1: state-machine and durability prerequisites

### 1. Add `Supervisor.advance()`

Refactor `Supervisor.run()` in `src/loop_supervisor/supervisor.py` so
phase dispatch and persistence live in:

```python
def advance(self, state: RunState) -> AdvanceOutcome
```

Add:

```python
class AdvanceStatus(StrEnum):
    ADVANCED = "advanced"
    INPUT_REQUIRED = "input_required"
    INPUT_UNAVAILABLE = "input_unavailable"
    OPERATIONAL_FAILURE = "operational_failure"
    TERMINAL = "terminal"
```

```python
@dataclass(frozen=True, slots=True)
class AdvanceOutcome:
    status: AdvanceStatus
    state: RunState
    phase_before: str
    phase_after: str
    error: Exception | None = None
```

Semantics:

- Dispatch exactly the phase present at method entry.
- Never dispatch the resulting phase in the same call.
- Persist/checkpoint before every normal return.
- `planning -> building` invokes only planner.
- `building -> awaiting_input` invokes only builder.
- `awaiting_input -> building` routes one queued answer but does not
  invoke builder.
- `auditing -> merging` does not immediately perform cleanup unless that
  is the explicitly dispatched phase.
- Calling `advance()` on `done`/`failed` is an idempotent terminal no-op.
- `run()` becomes a compatibility loop over `advance()` and preserves
  existing headless behavior.

### 2. Make pending input fully transition-driven

Keep `InputProvider`, but make TUI usage nonblocking:

1. A phase creates and persists `pending_question`.
2. TUI displays that persisted question.
3. User submits an answer into a queue-backed provider.
4. TUI launches one `advance()` worker.
5. `advance()` routes the already-queued answer and persists the next
   phase.
6. A later `advance()` invokes the next role.

Do not block a worker waiting for a widget response.

Refactor decision approval in `_do_architecting()` so it never calls
`InputProvider` directly. It should always persist:

```text
architecting -> awaiting_input
```

when approval is required.

### 3. Add durable side-effect phases

Add phase constants:

```text
creating_worktree
recording_decision
merging
cleanup_worktree
cleanup_branch
operational_failure
```

Use them to establish durable intent before non-idempotent mutations.

#### Worktree creation

```text
planning
  -> save planner result and intended task identity
  -> creating_worktree
  -> create/reconcile worktree
  -> building or architecting
```

Persist intended path, branch, and base before `git worktree add`.

#### ADR creation

```text
architecting
  -> awaiting_input when approval required
  -> recording_decision after approval
  -> write/reconcile exact ADR
  -> building or planning
```

Persist deterministic target path and content hash before writing.

Update `decisions.py` (`write_adr`) to support idempotent reconciliation:

- Missing target: create it.
- Existing target with exact expected bytes/hash: treat as already
  completed.
- Existing target with different content: fail closed.

#### Merge and cleanup

```text
auditing ACCEPT
  -> merging
  -> cleanup_worktree
  -> cleanup_branch
  -> planning
```

Before merge, persist:

```python
merge_pre_head
merge_task_head
```

After merge, persist:

```python
merge_commit
```

Split `GitRepo.remove_task_worktree()` into idempotent operations:

```python
remove_task_worktree_only(...)
delete_task_branch_only(...)
```

Never force-delete an unrelated directory found at the expected path.

### 4. Persist failures before returning or raising

Add schema-v3 error records:

```python
@dataclass(frozen=True)
class OperationalErrorRecord:
    error_id: str
    kind: str
    operation: str
    failed_phase: str
    retry_phase: str | None
    exception_type: str
    message: str
    retryable: bool
    requires_repair: bool
    recovery_hint: str | None
    occurred_at: str
```

Add to `RunState`:

```python
last_error: dict[str, Any] | None
```

Do not persist traceback text, request payloads, environment variables,
headers, or secrets.

Classification:

| Failure | State |
|---|---|
| OpenCode startup/network/timeout | `operational_failure`, retryable |
| Exhausted malformed model output | `operational_failure`, retryable |
| Ordinary Git/filesystem failure with known retry point | `operational_failure`, retryable |
| Merge conflict successfully aborted | `operational_failure`, retryable after repair |
| Revision/replan/architect retry limit | `failed`, nonretryable |
| Internal invariant/unknown phase | `failed`, nonretryable |
| Wrong repo, branch mismatch, tampered checkpoint | reject resume without modifying saved state |
| Failure while persisting failure state | raise dedicated `FailurePersistenceError` |

On builder failure, refresh and persist the task worktree's actual
HEAD/status because the builder may have made partial progress before
the transport failed.

### 5. State schema v3

Update `STATE_SCHEMA_VERSION` in `state.py`.

Schema v3 adds:

- `last_error`
- side-effect phase fields
- merge intent/result fields
- deterministic ADR target/hash fields

Migration:

- Continue rejecting schema v1.
- Deterministically migrate schema v2 to v3 by filling only additive
  fields with `None`.
- Do not infer or alter existing run options or Git checkpoints.
- Validate phase-specific invariants during load.

---

## Phase 2: repository execution lock

### 6. Add `src/loop_supervisor/locking.py`

Lock location:

```text
<git-common-dir>/loop-supervisor/supervisor.lock
```

Strict JSON record:

```json
{
  "schema_version": 1,
  "token": "random-owner-token",
  "pid": 12345,
  "hostname": "host",
  "started_at": "ISO-8601 UTC",
  "operation": "run|resume|tui",
  "run_id": "optional",
  "integration_path": "/absolute/path"
}
```

Requirements:

- Mode `0600`.
- Atomic create-if-absent (temp file + `os.link`, or documented
  equivalent).
- Owner token prevents one process from deleting a successor's lock.
- Context-manager release in `finally`.
- Hold lock for the entire mutating lifecycle, including OpenCode
  shutdown.
- Saved-run listing and read-only details remain lock-free.

### 7. Stale-lock policy

Add `--recover-stale-lock` to mutating CLI/TUI actions.

- Live local PID: always reject.
- Local PID demonstrably gone:
  - without flag: report stale and reject;
  - with flag: compare token again, remove, and retry acquisition.
- Different hostname: never auto-recover.
- Malformed lock: never auto-recover.
- Permission-denied PID probe: treat as live.
- Crash leaves stale lock requiring explicit recovery.

### 8. Shared application runtime

Avoid separate lock/error/startup implementations in CLI and TUI.

Add an application-level controller, e.g.:

```text
src/loop_supervisor/runtime.py
```

Responsibilities:

1. Resolve repository/common directory.
2. Acquire lock.
3. Create/load/validate state.
4. Start OpenCode only after state exists and validation succeeds.
5. Run `advance()`/`run()`.
6. Persist classified failures.
7. Stop OpenCode.
8. Release lock.

Preserve resume validation-before-OpenCode ordering currently
established in `cli.py`.

For a new run, reverse current startup order: create and save the run
before starting OpenCode so server startup failure can be recorded
against a real run ID.

---

## Phase 3: OpenCode live-event support

### 9. Correct structured-output compatibility

In `opencode.py` (`_extract_text`):

1. Prefer `info["structured"]` for OpenCode 1.18.21.
2. Fall back to legacy `info["structured_output"]`.
3. Fall back to text parts only if neither exists.
4. Convert malformed response JSON and non-timeout `httpx.RequestError`
   into `AgentInvocationError`.

Update the fake server's canonical response to `structured`, while
retaining a legacy mode.

### 10. Expose active invocation identity

Add:

```python
@dataclass(frozen=True)
class InvocationRef:
    session_id: str
    agent: str
    directory: Path
    started_monotonic: float
```

```python
class InvocationObserver(Protocol):
    def invocation_started(self, invocation: InvocationRef) -> None: ...
    def invocation_finished(
        self,
        invocation: InvocationRef,
        error: BaseException | None,
    ) -> None: ...
```

In `OpenCodeServer.run_agent()`:

1. Create session.
2. Register it in a thread-safe active-session map.
3. Notify `invocation_started`.
4. Perform blocking prompt POST.
5. Remove registration in `finally`.
6. Notify `invocation_finished` exactly once.

Observer errors must not fail the agent invocation.

Add best-effort `abort_active_sessions()` using short-lived clients; do
not share the long-lived control client across worker threads.

### 11. Add SSE transport

Create:

```text
src/loop_supervisor/sse.py
```

Subscribe to:

```http
GET /global/event
Accept: text/event-stream
```

Use `/global/event`, not `/event`, because planner/architect/builder/
auditor sessions may run in different worktree directories.

Implement a pure parser:

```python
def iter_sse_json(lines, *, max_event_bytes=1 << 20, on_notice=None)
```

Support:

- multiline `data:`;
- blank-line dispatch;
- comments/heartbeats;
- CRLF;
- malformed JSON as nonfatal notice;
- object-only JSON;
- maximum event size;
- incomplete EOF record discarded.

Transport behavior:

- Dedicated `httpx.Client`.
- Explicit connect/read/write/pool timeouts.
- Connection states: disconnected, connecting, live, reconnecting,
  stopped.
- Capped exponential reconnect backoff.
- Stop-aware waits.
- Reconnect on EOF/read timeout/transport failure.
- Every reconnect records a visible telemetry gap.
- SSE failure never changes `RunState` or fails the supervisor run.

### 12. Reconcile after SSE reconnect

Because OpenCode's global SSE stream has no replay cursor:

- Treat every reconnect as potentially lossy.
- Reconcile active sessions with:
  - `GET /session/status?directory=...`
  - `GET /session/{id}/message?directory=...`
  - optionally todo and diff endpoints.
- Show a visible "activity during disconnect may be missing" notice.
- The blocking prompt response remains authoritative for role completion
  and structured output.
- Never infer durable supervisor phase from `session.status`.

### 13. Add bounded live-event reducer

Create:

```text
src/loop_supervisor/tui/live.py
```

Immutable models:

```python
LiveConnection
LiveInvocation
LiveMessage
LiveTool
LiveFeedItem
LiveActivitySnapshot
```

Handle initially:

- `server.connected`
- `session.status`
- `session.idle`
- `session.error`
- `message.updated`
- `message.part.updated`
- `message.part.delta`
- `message.part.removed`
- `todo.updated`
- `file.edited`
- `session.diff`

Filtering:

- Session-bearing events must match a registered `InvocationRef`.
- Directory must exactly match integration/task/invocation directory.
- Ignore unknown sessions.
- Do not use path-prefix matching.
- Unknown event/part types increment counters but do not fail.

Bounds:

- Last 4 invocations.
- Last 200 feed records.
- 16 KiB text and reasoning tails.
- 100 tools.
- 200 touched files.
- 1 KiB tool result summary.
- 2,048 recent event IDs for deduplication.
- No unbounded raw-event retention.

### 14. Drain OpenCode stdout

After startup readiness is detected in `OpenCodeServer.start()`,
continue draining server stdout in a daemon thread into a bounded
deque.

This prevents the pipe from filling and blocking a long-running server.

Stop/join the drainer during `OpenCodeServer.stop()`.

---

## Phase 4: Textual UI

### 15. Dependencies

Update `pyproject.toml`:

```toml
"textual>=6.0,<7"
"rich>=13.9,<15"
```

Development dependency:

```toml
"pytest-asyncio>=1,<2"
```

Do not assume availability until dependency installation succeeds.

### 16. Module layout

```text
src/loop_supervisor/tui/
├── __init__.py
├── app.py
├── runtime.py
├── live.py
├── messages.py
├── screens.py
├── widgets.py
└── renderers.py
```

Keep Textual imports out of core modules except the TUI package.

### 17. CLI command

Add:

```bash
loop-supervisor tui --project PATH
```

The app opens a read-only run browser:

- "Start new run"
- Saved schema-v2/v3 runs
- Phase, updated time, task, and last error

No repository lock is acquired while merely browsing.

When starting a new run:

- Use the same `RunOptions` defaults/flags as `run`.
- Acquire lock only when execution begins.

When resuming:

- Use persisted options only.
- Acquire lock before validation.
- Validate before starting OpenCode.

### 18. `LoopSupervisorApp`

Use Textual thread workers:

- **Transition worker:** one `Supervisor.advance()` call; never
  exclusive-cancel a still-running transition thread.
- **SSE worker:** long-running event stream; independent and
  restartable.
- All widget mutation occurs on Textual's event-loop thread through
  typed messages.
- Worker threads publish via
  `app.call_from_thread(app.post_message, ...)`.

Do not rely on `Worker.cancel()` to kill a Python thread. Use
cooperative stop events and explicit HTTP/session shutdown.

### 19. Screens

#### Run browser

Display:

- New-run action.
- Saved run IDs.
- Phase.
- Updated time.
- Current task.
- Error/repair status.
- Resume button.
- Stale-lock recovery dialog when applicable.

#### Run screen

Suggested structure:

```text
Header
Run banner: repo | run ID | durable phase | worker state | SSE state
Phase strip
Two-column body:
  Durable pane:
    task/objective
    counters
    latest role result
    decision/error details
  Live pane:
    active role/session
    assistant/reasoning tail
    active tools table
    bounded activity feed
Pending-input panel
Footer
```

At narrow widths, stack durable pane above live pane.

Clearly label:

```text
Durable supervisor state
Live OpenCode activity — ephemeral
```

Never visually merge the two concepts.

### 20. Pending-input controls

| Kind | UI |
|---|---|
| `builder_guidance` | multiline input, Submit, Replan |
| `architect_input` | multiline input, Submit |
| `decision_approval` | Approve, Reject |
| rejected decision feedback | multiline input, Submit |
| merge conflict | read-only repair instructions |
| operational failure | Retry/Resume, Return to runs |

Submit canonical values:

- Approval: `approve`
- Replan: `replan`
- Rejection: preserve current two-step behavior—submit rejection, then
  display persisted feedback question.

The UI must submit through `InputProvider`; it must not mutate
`RunState` directly.

### 21. Shutdown

Order (two-stage: bounded cooperative grace + escalation, then a final
ownership-preserving release that never abandons the lock while a mutation
worker may still be live):

1. Disable input/actions.
2. Request cooperative transition stop.
3. Stop SSE and close active response (ownership retained if it does not
   confirm termination).
4. Abort active OpenCode sessions.
5. Give the in-flight advance() worker a bounded cooperative grace period.
6. If it is still running, escalate by stopping the OpenCode server to
   break any blocked prompt transport. This does NOT release the lock.
7. Continue waiting (unbounded) for the advance() worker to fully unwind:
   the lock must never be released while a transition can still mutate
   Git/state.
8. Ownership-preserving final cleanup: stop SSE/server, then release the
   lock only once the server is definitively gone. Clear each owned
   reference only when its resource is confirmed released.
9. Pop the screen / exit Textual only on a clean shutdown; otherwise
   retain the lock (and diagnostics) on disk for --recover-stale-lock.

No force-killing Python worker threads.

"Stop" means stop after safe unwinding/abort, not guaranteed interruption
of an arbitrary Git syscall. A bounded grace plus server-stop escalation
breaks blocked OpenCode transport, but the final lock release cannot be
bounded if a Git syscall is stuck: retaining the lock is always safer than
releasing it while a mutation is in flight.

---

## Phase 5: tests

### 22. Core transition tests

Extend `tests/test_supervisor.py`:

- `advance()` invokes exactly one role.
- Successive advances follow expected phases.
- `run()` preserves existing behavior.
- Terminal advance is idempotent.
- Role-created question returns `INPUT_REQUIRED`.
- Empty provider returns `INPUT_UNAVAILABLE`.
- Answer routing is a separate persisted transition.
- Approval does not reinvoke architect.
- Rejection does not reinvoke until feedback is submitted.
- Merge conflict is persisted before propagation.
- Operational failures persist `last_error` and retry phase.
- Policy-limit failures persist terminal `failed`.
- Builder timeout checkpoints partial worktree changes.
- Resume retries the recorded operational phase.
- Resume-validation failure does not overwrite saved state.
- Save failure followed by successful failure-save returns a durable
  failure.
- Failure-save failure raises `FailurePersistenceError`.

### 23. State tests

Extend `tests/test_state.py`:

- Schema-v2 to v3 migration.
- Schema-v1 rejection remains.
- Operational error round-trip.
- Unknown error fields rejected.
- Phase-specific invariants.
- Merge phase requires intent fields.
- Recording-decision phase requires path/hash.
- Operational failure requires retry phase and error.
- Additive migration never changes existing options/checkpoints.

### 24. Lock tests

Add `tests/test_locking.py`:

- Common lock path across linked worktrees.
- Required metadata and mode `0600`.
- Exactly one concurrent acquisition winner.
- Live local owner rejected.
- Live local owner cannot be force-recovered.
- Dead local owner requires explicit recovery.
- Explicit recovery works.
- Remote/malformed lock never auto-recovers.
- Release verifies owner token.
- Exception/KeyboardInterrupt releases lock.
- Hard-crashed child leaves a stale lock.

### 25. Git crash-boundary tests

Extend `tests/test_git.py`:

- Real conflict distinguished from ordinary merge failure.
- Conflict abort restores pre-merge HEAD and clean integration.
- Task branch/worktree preserved.
- Completed merge recognized by ancestry.
- Worktree-only cleanup idempotent.
- Branch-only cleanup idempotent.
- Cleanup refuses unrelated path.
- Branch deletion refuses unintegrated task head.

### 26. OpenCode/SSE tests

Extend `tests/fixtures/fake_opencode.py` with:

- Unique session IDs.
- `/global/event`.
- `/session/status`.
- Message history.
- Configurable busy/text/tool/idle events.
- Forced disconnect/reconnect scenarios.
- Prompt blocking/release.
- Abort observability.
- Canonical `info.structured`, plus legacy mode.

Add:

```text
tests/test_sse.py
tests/test_opencode_events.py
```

Cover framing, reconnection, filtering, normalization, bounded memory,
deduplication, reconciliation, observer ordering, active-session abort,
structured-output compatibility, and stdout draining.

### 27. Runtime tests

Add `tests/test_runtime.py`:

- New-run state saved before OpenCode startup.
- Resume lock/validation occurs before OpenCode startup.
- Server stops before lock release.
- Awaiting input releases lock in headless mode.
- TUI active-run mode holds lock.
- Operational failures are sanitized and persisted.
- SSE failure does not fail the run.
- Run listing does not acquire lock.

### 28. Textual tests

Add `tests/test_tui_app.py` using Textual `run_test()` and pilot
patterns from Falda (`/workspaces/falda/analysis/tests/test_app.py`).

Cover:

- Run browser rendering.
- New-run selection.
- Saved-run selection.
- Durable phase update.
- Live status updates only the live pane.
- SSE disconnect leaves durable UI usable.
- Each input form.
- Double-submit prevention.
- Done/failed/error rendering.
- Lock contention and stale-lock dialogs.
- Narrow layout.
- Rich markup escaping.
- Bounded live output.
- Clean shutdown.

### 29. End-to-end TUI scenario

Use a real temporary Git repository plus fake OpenCode server:

1. Start a run.
2. Planner returns `READY`.
3. SSE emits busy/text/tool events.
4. Builder reports `BLOCKED`.
5. Durable `awaiting_input` appears.
6. Submit guidance.
7. Builder completes and commits.
8. Auditor accepts.
9. Planner returns `COMPLETE`.
10. Durable state reloads as `done`.
11. SSE and OpenCode stop.
12. Lock is released.
13. No orphan process/worktree remains.

Add a second scenario for resumable operational failure and restart.

---

## Documentation and ADRs

### 30. Add ADRs

Add:

- **0008 — Textual TUI and execution model**
  - Textual 6.x + Rich.
  - One `advance()` per thread worker.
  - Durable `RunState` vs ephemeral live state.
  - `/global/event` SSE.
  - Blocking final POST remains authoritative.

- **0009 — Supervisor lock and operational failure semantics**
  - Per-common-dir supervisor lock.
  - Explicit stale recovery.
  - Operational failure vs terminal failure.
  - Durable side-effect phases and crash reconciliation.

Update `docs/decisions/README.md`.

### 31. Update README

Document:

- `loop-supervisor tui --project PATH`.
- Run browser and saved runs.
- Durable vs live status.
- Pending input.
- Lock behavior/recovery.
- Operational failure retry.
- Merge-conflict repair.
- Remaining limitations.

Remove "Headless only; no TUI yet."

---

## Verification

Run from this worktree:

```bash
rtk pytest
rtk ruff check .
rtk ruff format --check .
rtk mypy src
rtk git diff --check
rtk git status
rtk git log --oneline -10
```

Also verify:

- No credentials in tracked files or diff.
- `.env` remains ignored.
- Lock files and run-state files remain under Git common metadata only.
- No leftover OpenCode process.
- No leftover test worktrees/branches.
- Existing `run`, `resume`, and `init` CLI syntax still works.
- `resume` still validates before OpenCode starts.
- TUI remains usable with SSE unavailable.

Stop with changes uncommitted unless explicitly asked to commit.
