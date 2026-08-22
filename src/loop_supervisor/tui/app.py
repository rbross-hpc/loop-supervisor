"""Textual TUI application for loop-supervisor.

Architecture:
- ``LoopSupervisorApp`` is the root App.
- ``RunBrowserScreen`` is shown on startup; no lock acquired here.
- ``RunScreen`` is shown when a run is started or resumed; holds the
  repository lock for the lifetime of the screen.

Reducer ownership: the ``LiveActivityReducer`` is owned by the Textual
event-loop thread. Worker threads (SSE, invocation observers) must NEVER
touch it directly — they post typed messages via ``call_from_thread``.

Shutdown ordering:
  1. Set _shutdown_requested.
  2. Stop SSE (closes active response; ownership retained if it does not
     confirm termination).
  3. Abort active OpenCode sessions.
  4. Give the in-flight advance() worker a bounded cooperative grace
     period (_SHUTDOWN_GRACE_SECONDS) to unwind.
  5. If it is still running, escalate by stopping the OpenCode server to
     break any blocked prompt transport — this does NOT release the lock.
  6. Continue waiting (unbounded) for the advance() worker to fully unwind:
     the lock must never be released while a transition can still mutate
     Git/state, and no Python thread is force-killed.
  7. Ownership-preserving final cleanup: stop SSE/server, then release the
     lock only once the server is definitively gone. Any owned reference is
     cleared only when its resource is confirmed released.
  8. Pop the screen only on a clean shutdown; if cleanup left the lock or
     server unresolved, retain them (and diagnostics) and leave the lock on
     disk for explicit --recover-stale-lock.

The overall shutdown is therefore two-stage: a bounded cooperative grace
(plus server-stop escalation) followed by a final, ownership-preserving
release that never abandons the lock while a mutation worker may be live.

App-level exit ownership: pressing "q" on a run screen, the "Return to
runs" button, an unexpected unmount, and app-level exit (ctrl+q, an
explicit ``App.exit()`` call, or the driver posting ``ExitApp`` on
SIGINT/SIGTERM) all funnel into the same idempotent, retryable cleanup
via ``RunScreen.action_request_shutdown()``/``_shutdown_worker()``. At
most one cleanup attempt runs at a time per screen
(``_shutdown_attempt_lock``/``_shutdown_in_progress``); a completed
attempt that did not achieve a clean teardown (``shutdown_clean`` is
False — e.g. ``server.stop()`` could not confirm the process exited)
leaves every owned resource in place for a subsequent attempt, triggered
either by the user retrying ("q"/"Return to runs" again) or automatically
by app-level exit.

``LoopSupervisorApp`` overrides ``_on_exit_app`` — Textual's single
dispatch point for every exit path, since ``App.exit()`` only ever posts
an ``ExitApp`` message — to request shutdown on any active ``RunScreen``,
asynchronously await its completion (without blocking the Textual event
loop, so initialization/advance() can still finish and post messages),
and then repeat that request/await cycle, indefinitely, until every
active ``RunScreen`` reports ``shutdown_clean``, before allowing the
underlying ``_on_exit_app`` to proceed. This guarantees the process never
exits while the repository lock is held or an OpenCode server process
may still be running, regardless of which of the above paths triggered
the exit, and regardless of how many attempts cleanup actually takes.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from pathlib import Path

from rich.markup import escape
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Label,
    ListItem,
    ListView,
    Static,
    TextArea,
)

from ..git import GitError, GitRepo
from ..locking import LockError, SupervisorLock
from ..opencode import InvocationRef, OpenCodeServer, OpenCodeServerConfig
from ..opencode_events import OpenCodeEventError, normalize_global_event
from ..sse import SSEClient, SSEConnectionState
from ..state import RunOptions, RunState, list_runs, load_state
from ..supervisor import (
    PHASE_AWAITING_INPUT,
    PHASE_DONE,
    PHASE_FAILED,
    PHASE_OPERATIONAL_FAILURE,
    AdvanceStatus,
    FailurePersistenceError,
    LoopError,
    Supervisor,
)
from .live import LiveActivityReducer
from .messages import (
    AdvanceCompleted,
    AdvanceFailed,
    InvocationFinished,
    InvocationStarted,
    LiveConnectionChanged,
    LiveDisconnected,
    LiveUpdated,
    OpenCodeEventReceived,
)
from .renderers import (
    render_durable_summary,
    render_live_summary,
    render_operational_failure,
    render_pending_input,
)

_DEFAULT_OPTIONS = RunOptions(
    max_accepted_tasks=20,
    max_revisions_per_task=5,
    max_replans_per_task=3,
    max_architect_retries=3,
    malformed_output_retries=1,
    role_timeout=1800.0,
    worktree_root=None,
    require_decision_approval=False,
    opencode_executable="opencode",
    opencode_startup_timeout=30.0,
)

# How long to wait, per notification interval, for the in-flight advance()
# worker to finish cooperatively before re-drawing the "still waiting"
# banner. This is a UI refresh cadence, not an overall bound.
_SHUTDOWN_WAIT_SECONDS = 10.0

# Bounded cooperative grace period given to an in-flight advance() worker
# after aborting active sessions, before escalating to stopping the
# OpenCode server to break any blocked prompt transport. This does NOT
# bound total shutdown: the repository lock is never released while a
# transition worker can still mutate Git/state, so after escalation we
# continue waiting for the worker to fully unwind.
_SHUTDOWN_GRACE_SECONDS = 10.0

# How long to wait between automatic retries of a shutdown attempt that
# finished without achieving a clean teardown (e.g. server.stop() could
# not confirm the process exited). Deliberately short and not an overall
# bound: app-level exit retries indefinitely until cleanup is confirmed
# clean, since unresolved child/lock ownership must never be abandoned.
_SHUTDOWN_RETRY_INTERVAL_SECONDS = 2.0


class _QueueInputProvider:
    """InputProvider backed by a one-slot queue for TUI submission.

    Only one answer can be pending at a time. ``offer()`` returns False
    if a slot is already occupied, preventing duplicate submissions.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[str] = queue.Queue(maxsize=1)

    def offer(self, answer: str) -> bool:
        """Offer an answer. Returns True if accepted, False if slot full."""
        try:
            self._queue.put_nowait(answer)
            return True
        except queue.Full:
            return False

    def clear(self) -> None:
        """Drain the queue (called on shutdown or stale answer detection)."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def request(self, *, kind: str, message: str, context: dict) -> str | None:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None


class RunBrowserScreen(Screen):
    """Read-only run browser. No lock acquired here."""

    CSS = """
    RunBrowserScreen {
        align: center middle;
    }
    #browser-box {
        width: 80; height: auto; max-height: 40;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #browser-title {
        color: $accent;
        text-style: bold;
        margin-bottom: 1;
    }
    #run-list {
        height: auto;
        max-height: 20;
        margin-bottom: 1;
    }
    #new-run-button {
        margin-top: 1;
    }
    """

    BINDINGS = [("q", "app.quit", "Quit"), ("escape", "app.quit", "Quit")]

    def __init__(self, project_root: Path, *, recover_stale_lock: bool = False) -> None:
        super().__init__()
        self._project_root = project_root
        self._recover_stale_lock = recover_stale_lock

    def compose(self) -> ComposeResult:
        with Vertical(id="browser-box"):
            yield Label("loop-supervisor  —  Run Browser", id="browser-title")
            runs = self._load_runs()
            if runs:
                items = [ListItem(Label(r)) for r in runs]
                yield ListView(*items, id="run-list")
            else:
                yield Label("(no saved runs)", id="run-list")
            yield Button("Start new run", id="new-run-button", variant="primary")
        yield Footer()

    def _load_runs(self) -> list[str]:
        try:
            repo = GitRepo(self._project_root)
            return list_runs(repo.common_dir())
        except Exception:
            return []

    @on(Button.Pressed, "#new-run-button")
    def on_new_run(self) -> None:
        self.app.push_screen(
            RunScreen(
                self._project_root,
                run_id=None,
                recover_stale_lock=self._recover_stale_lock,
            )
        )

    @on(ListView.Selected)
    def on_run_selected(self, event: ListView.Selected) -> None:
        label = event.item.query_one(Label)
        run_id = str(label.render())
        self.app.push_screen(
            RunScreen(
                self._project_root,
                run_id=run_id,
                recover_stale_lock=self._recover_stale_lock,
            )
        )


class RunScreen(Screen):
    """Active run screen. Acquires the repository lock on start."""

    CSS = """
    RunScreen { }
    #run-banner {
        background: $panel;
        padding: 0 1;
        height: 3;
        border-bottom: solid $accent;
    }
    #body-columns {
        height: 1fr;
    }
    #durable-pane {
        width: 1fr;
        border-right: solid $panel-darken-1;
        padding: 0 1;
    }
    #durable-label {
        color: $accent;
        text-style: bold;
        margin-bottom: 1;
    }
    #live-pane {
        width: 1fr;
        padding: 0 1;
    }
    #live-label {
        color: $text-muted;
        text-style: dim;
        margin-bottom: 1;
    }
    #input-panel {
        height: auto;
        max-height: 14;
        border-top: solid $accent;
        padding: 0 1;
        display: none;
    }
    #input-panel.visible {
        display: block;
    }
    #input-area {
        height: 5;
        margin-bottom: 1;
    }
    #input-buttons {
        height: auto;
    }
    """

    BINDINGS = [("q", "request_shutdown", "Quit")]

    def __init__(
        self,
        project_root: Path,
        *,
        run_id: str | None = None,
        options: RunOptions | None = None,
        recover_stale_lock: bool = False,
    ) -> None:
        super().__init__()
        self._project_root = project_root
        self._run_id = run_id
        self._options = options or _DEFAULT_OPTIONS
        self._recover_stale_lock = recover_stale_lock

        self._repo: GitRepo | None = None
        # Captured once in on_mount(). self.app walks the live parent chain
        # and raises NoActiveAppError once this screen has been detached
        # (e.g. during its own pop_screen at the end of shutdown), so
        # background threads that need to reach the app after that point
        # (notably _shutdown_worker's final pop_screen call) use this
        # cached reference instead of the `self.app` property.
        self._app_ref: App | None = None
        self._lock: SupervisorLock | None = None
        self._server: OpenCodeServer | None = None
        self._supervisor: Supervisor | None = None
        self._state: RunState | None = None
        self._input_provider = _QueueInputProvider()

        self._reducer = LiveActivityReducer(owner_thread=threading.current_thread())

        self._sse_client: SSEClient | None = None
        self._transitioning = False
        self._submission_in_flight = False
        self._shutdown_requested = False
        self._advance_done_event = threading.Event()
        self._advance_done_event.set()
        # Cleared for the duration of _initialize(); shutdown must never
        # release the lock or stop the server while initialization is
        # still in flight, since it may still be mutating durable state.
        self._init_done_event = threading.Event()
        # Set exactly once, in _shutdown_worker's finally block, regardless
        # of how cleanup went (including exceptions from server.stop() or
        # lock.release()). This is the single completion signal every exit
        # path — "q", the "Return to runs" button, an unexpected unmount,
        # and app-level exit via LoopSupervisorApp._on_exit_app — can wait
        # on without caring which of them actually triggered shutdown.
        self._shutdown_complete_event = threading.Event()
        # Distinct from "attempt finished" above: True only if every owned
        # resource (SSE worker, OpenCode server, repository lock) was
        # definitively released. False means the attempt finished but left
        # something unresolved (e.g. the lock could not be released and is
        # left on disk for explicit --recover-stale-lock).
        self._shutdown_clean = False
        # Guards starting a shutdown attempt: at most one _shutdown_worker
        # may run at a time, and a new attempt is only started when
        # cleanup is not already clean and no attempt is currently active.
        # Without this, "q" pressed twice in quick succession, or "q"
        # racing app-level exit, could register two overlapping
        # _shutdown_worker calls that both try to stop the same server or
        # release the same lock concurrently.
        self._shutdown_attempt_lock = threading.Lock()
        self._shutdown_in_progress = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Static(id="run-banner"):
            yield Static("Initializing…", id="banner-text")
        with Horizontal(id="body-columns"):
            with VerticalScroll(id="durable-pane"):
                yield Label("Durable supervisor state", id="durable-label")
                yield Static("", id="durable-content")
            with VerticalScroll(id="live-pane"):
                yield Label("Live OpenCode activity — ephemeral", id="live-label")
                yield Static("", id="live-content")
        with Vertical(id="input-panel"):
            yield Static("", id="input-prompt")
            yield TextArea("", id="input-area")
            with Horizontal(id="input-buttons"):
                yield Button("Submit", id="submit-btn", variant="primary")
                yield Button("Replan", id="replan-btn", variant="default")
                yield Button("Approve", id="approve-btn", variant="success")
                yield Button("Reject", id="reject-btn", variant="error")
                yield Button("Retry", id="retry-btn", variant="warning")
                yield Button("Return to runs", id="return-btn", variant="default")
        yield Footer()

    def on_mount(self) -> None:
        self._app_ref = self.app
        self._reducer = LiveActivityReducer(owner_thread=threading.current_thread())
        self.run_worker(self._initialize, exclusive=False, thread=True)

    def _initialize(self) -> None:
        """Acquire and wire up all lifecycle resources.

        This entire method runs before ``_init_done_event`` is set (in a
        ``finally``), and shutdown always waits for that event before
        touching the lock or server. Any exception anywhere in this method
        — not just the previously-enumerated narrow types — is caught by
        the outer handler and results in full cleanup of whatever partial
        resources were acquired (lock, server) before returning, so a
        crash here can never leak the lock or an orphaned OpenCode
        process.
        """
        try:
            self._do_initialize()
        finally:
            self._init_done_event.set()

    def _do_initialize(self) -> None:
        if self._shutdown_requested:
            return

        try:
            repo = GitRepo(self._project_root)
        except GitError as exc:
            self.app.call_from_thread(self._set_banner, f"[red]Error: {escape(str(exc))}[/red]")
            return
        self._repo = repo
        common_dir = repo.common_dir()

        if self._shutdown_requested:
            return

        lock = SupervisorLock(
            common_dir,
            operation="tui",
            run_id=self._run_id,
            integration_path=str(repo.root),
            recover_stale=self._recover_stale_lock,
        )
        try:
            lock.acquire()
        except LockError as exc:
            self.app.call_from_thread(
                self._set_banner, f"[red]Lock error: {escape(str(exc))}[/red]"
            )
            return
        self._lock = lock

        try:
            self._do_initialize_locked(repo, common_dir)
        except FailurePersistenceError as exc:
            self.app.call_from_thread(
                self._set_banner,
                "[red]OpenCode startup failed, and the failure record could not be "
                f"persisted: {escape(str(exc))}[/red]",
            )
            self._release_after_failed_init()
        except Exception as exc:
            self.app.call_from_thread(
                self._set_banner, f"[red]Initialization error: {escape(str(exc))}[/red]"
            )
            self._release_after_failed_init()

    def _release_after_failed_init(self) -> None:
        """Cleanup of whatever partial resources _do_initialize_locked
        acquired before failing. Always runs on the initialization worker
        thread, never concurrently with shutdown (shutdown waits on
        _init_done_event before touching these same resources).

        Uses the same ownership-preserving routine as normal shutdown, in
        the same order (SSE, then server, then lock), so a failed
        initialization can never discard a still-live worker/process or
        release the lock on a transient failure."""
        server = self._server
        if server is not None:
            try:
                server.abort_active_sessions()
            except Exception:
                pass
        self._cleanup_resources()

    def _do_initialize_locked(self, repo: GitRepo, common_dir: Path) -> None:
        """Everything from state creation/validation through SSE startup.

        Called only while holding the just-acquired lock. Any exception
        propagates to `_do_initialize`'s handler, which cleans up whatever
        this method had already assigned to self._server/self._sse_client
        before the lock is released.
        """
        if self._shutdown_requested:
            return

        if self._run_id is None:
            supervisor = Supervisor(
                repo=repo,
                runner=_UnstartedRunner(),
                git_common_dir=common_dir,
                input_provider=self._input_provider,
                options=self._options,
            )
            state = supervisor.start_new_run()
        else:
            state = load_state(common_dir, self._run_id)
            supervisor = Supervisor(
                repo=repo,
                runner=_UnstartedRunner(),
                git_common_dir=common_dir,
                input_provider=self._input_provider,
            )
            try:
                state = supervisor.resume(state)
            except LoopError:
                # Resume validation failed: the saved state is untouched and
                # OpenCode was never started. Nothing further to clean up
                # beyond the lock, which the caller's except handler does.
                raise

        if self._shutdown_requested:
            return

        server_config = OpenCodeServerConfig(
            executable=state.options.opencode_executable,
            startup_timeout=state.options.opencode_startup_timeout,
        )
        server = OpenCodeServer(self._project_root, server_config)
        server.add_observer(_InvocationObserver(self))
        # Take ownership as soon as the object exists — ownership begins the
        # moment the server may acquire a process, not only after a
        # successful start(). Otherwise a start() that partially spawned a
        # process and then failed cleanup would drop the only handle to it.
        self._server = server
        try:
            server.start()
        except Exception as exc:
            # server.start() already terminates the process and releases its
            # own resources on any failure past subprocess creation; this
            # call is defense in depth in case that guarantee is ever
            # violated by a future change, not the primary cleanup path.
            try:
                server.stop()
            except Exception:
                pass
            # A run ID already exists at this point (either just-created or
            # loaded from disk), so persist a durable operational failure
            # rather than only showing a transient banner. The failure is
            # durable: the user can resume this run later (from the CLI or
            # the run browser) to retry, exactly like any other
            # operational_failure discovered inside advance(). Unlike a
            # transient startup exception, a failure to persist that record
            # must never be silently discarded: the UI would otherwise
            # imply a durable failure exists when it does not, and the user
            # would have no way to know resume will simply repeat the same
            # unrecorded failure.
            try:
                supervisor.record_external_failure(state, exc=exc, phase=state.phase)
            except FailurePersistenceError as persist_exc:
                raise persist_exc from exc
            raise

        supervisor.runner = server
        self._server = server
        self._supervisor = supervisor
        self._state = state

        if self._shutdown_requested:
            # Shutdown was requested while we were still starting the
            # server. Do NOT abort sessions / stop the server / clear
            # self._server here: doing so would duplicate (and race)
            # _shutdown_worker's own teardown, which is already waiting on
            # _init_done_event before touching these same resources (see
            # _do_shutdown). Simply returning leaves self._server owned;
            # _initialize()'s finally block sets _init_done_event right
            # after this method returns, at which point _do_shutdown
            # proceeds to perform the single canonical teardown. Clearing
            # self._server here (regardless of whether stop() "succeeded")
            # would let a later stop() failure go unnoticed by the
            # shutdown worker, since it would see self._server as None and
            # assume the server was already gone.
            return

        if server.base_url:
            sse = SSEClient(
                server.base_url,
                on_event=self._on_sse_event,
                on_state_change=self._on_sse_state_change,
                on_notice=self._on_sse_notice,
            )
            sse.start()
            self._sse_client = sse

        self.app.call_from_thread(self._refresh_durable)
        self.app.call_from_thread(self._start_advance)

    def _set_banner(self, text: str) -> None:
        self.query_one("#banner-text", Static).update(text)

    def _refresh_durable(self) -> None:
        state = self._state
        if state is None:
            return
        banner = (
            f"repo: {escape(str(self._project_root))}  |  "
            f"run: {escape(state.run_id)}  |  "
            f"phase: {escape(state.phase)}"
        )
        self.query_one("#banner-text", Static).update(banner)
        self.query_one("#durable-content", Static).update(render_durable_summary(state))
        self._update_input_panel(state)

    def _update_input_panel(self, state: RunState) -> None:
        panel = self.query_one("#input-panel")

        if state.phase == PHASE_OPERATIONAL_FAILURE:
            self.query_one("#input-prompt", Static).update(
                render_operational_failure(state.last_error or {})
            )
            self._configure_input_buttons("operational_failure", state.last_error or {})
            panel.add_class("visible")
            return

        if state.pending_question and state.phase == PHASE_AWAITING_INPUT:
            prompt = render_pending_input(state.pending_question)
            self.query_one("#input-prompt", Static).update(prompt)
            kind = state.pending_question.get("kind", "")
            self._configure_input_buttons(kind, {})
            panel.add_class("visible")
            return

        panel.remove_class("visible")

    def _configure_input_buttons(self, kind: str, extra: dict) -> None:
        submit = self.query_one("#submit-btn", Button)
        replan = self.query_one("#replan-btn", Button)
        approve = self.query_one("#approve-btn", Button)
        reject = self.query_one("#reject-btn", Button)
        retry = self.query_one("#retry-btn", Button)
        ret = self.query_one("#return-btn", Button)

        for btn in (submit, replan, approve, reject, retry, ret):
            btn.display = False

        if kind == "builder_guidance":
            submit.display = True
            replan.display = True
        elif kind == "architect_input":
            submit.display = True
        elif kind == "decision_approval":
            approve.display = True
            reject.display = True
        elif kind == "operational_failure":
            retryable = extra.get("retryable", False)
            if retryable:
                retry.display = True
            ret.display = True
        else:
            submit.display = True

    def _start_advance(self) -> None:
        if self._transitioning or self._shutdown_requested:
            return
        state = self._state
        if state is None:
            return
        if state.phase in (PHASE_DONE, PHASE_FAILED):
            phase = state.phase.upper()
            self._set_banner(
                f"[bold {'green' if state.phase == PHASE_DONE else 'red'}]{phase}[/bold]"
                f"  run {escape(state.run_id)}"
            )
            return
        if state.phase in (PHASE_AWAITING_INPUT, PHASE_OPERATIONAL_FAILURE):
            return
        self._transitioning = True
        self._advance_done_event.clear()
        self.run_worker(self._advance_worker, exclusive=False, thread=True)

    def _advance_worker(self) -> None:
        """Runs on a background thread. Never mutates ``_transitioning``,
        the reducer, or any widget directly — only posts a typed message
        for the Textual event loop to apply. ``_advance_done_event`` is a
        plain ``threading.Event`` and is the sole exception: it exists only
        to let the shutdown worker (also a background thread) know when it
        is safe to proceed, and carries no application state itself.
        """
        supervisor = self._supervisor
        state = self._state
        if supervisor is None or state is None:
            self._advance_done_event.set()
            return
        try:
            outcome = supervisor.advance(state)
            self.app.call_from_thread(self.post_message, AdvanceCompleted(outcome))
        except Exception as exc:
            self.app.call_from_thread(self.post_message, AdvanceFailed(exc))
        finally:
            self._advance_done_event.set()

    @on(AdvanceCompleted)
    def on_advance_completed(self, message: AdvanceCompleted) -> None:
        self._transitioning = False
        self._state = message.outcome.state
        self._submission_in_flight = False
        self._refresh_durable()
        outcome = message.outcome
        if outcome.status in (
            AdvanceStatus.INPUT_REQUIRED,
            AdvanceStatus.TERMINAL,
            AdvanceStatus.OPERATIONAL_FAILURE,
        ):
            pass
        else:
            self._start_advance()

    @on(AdvanceFailed)
    def on_advance_failed(self, message: AdvanceFailed) -> None:
        self._transitioning = False
        self._submission_in_flight = False
        self._set_banner(f"[red]Unexpected error: {escape(str(message.error))}[/red]")

    @on(OpenCodeEventReceived)
    def on_opencode_event_received(self, message: OpenCodeEventReceived) -> None:
        self._reducer.on_event(message.event)
        snapshot = self._reducer.snapshot()
        self.query_one("#live-content", Static).update(render_live_summary(snapshot))

    @on(LiveConnectionChanged)
    def on_live_connection_changed(self, message: LiveConnectionChanged) -> None:
        self._reducer.set_connection(str(message.state), message.reason)
        if message.state != SSEConnectionState.LIVE:
            snapshot = self._reducer.snapshot()
            self.query_one("#live-content", Static).update(render_live_summary(snapshot))
            self.post_message(LiveDisconnected(message.reason))

    @on(InvocationStarted)
    def on_invocation_started(self, message: InvocationStarted) -> None:
        self._reducer.register_invocation(message.invocation)

    @on(InvocationFinished)
    def on_invocation_finished(self, message: InvocationFinished) -> None:
        self._reducer.unregister_invocation(message.invocation)

    @on(LiveUpdated)
    def on_live_updated(self, message: LiveUpdated) -> None:
        self.query_one("#live-content", Static).update(render_live_summary(message.snapshot))

    @on(LiveDisconnected)
    def on_live_disconnected(self, message: LiveDisconnected) -> None:
        pass

    @on(Button.Pressed, "#submit-btn")
    def on_submit(self) -> None:
        text_area = self.query_one("#input-area", TextArea)
        answer = text_area.text.strip()
        if not answer:
            return
        text_area.clear()
        self._submit_answer(answer)

    @on(Button.Pressed, "#replan-btn")
    def on_replan(self) -> None:
        self.query_one("#input-area", TextArea).clear()
        self._submit_answer("replan")

    @on(Button.Pressed, "#approve-btn")
    def on_approve(self) -> None:
        self._submit_answer("approve")

    @on(Button.Pressed, "#reject-btn")
    def on_reject(self) -> None:
        self._submit_answer("no")

    @on(Button.Pressed, "#retry-btn")
    def on_retry(self) -> None:
        state = self._state
        if state is None:
            return
        if state.phase == PHASE_OPERATIONAL_FAILURE:
            self._start_operational_retry()
        else:
            self._submit_answer("retry")

    @on(Button.Pressed, "#return-btn")
    def on_return(self) -> None:
        self.action_request_shutdown()

    def _start_operational_retry(self) -> None:
        """Retry an operational failure without queueing any input answer."""
        if self._transitioning or self._shutdown_requested:
            return
        state = self._state
        if state is None or state.phase != PHASE_OPERATIONAL_FAILURE:
            return
        panel = self.query_one("#input-panel")
        panel.remove_class("visible")
        self._transitioning = True
        self._advance_done_event.clear()
        self.run_worker(self._advance_worker, exclusive=False, thread=True)

    def _submit_answer(self, answer: str) -> None:
        if self._submission_in_flight or self._transitioning or self._shutdown_requested:
            return
        if not self._input_provider.offer(answer):
            return
        self._submission_in_flight = True
        panel = self.query_one("#input-panel")
        panel.remove_class("visible")
        self._start_advance()

    def _on_sse_event(self, raw_event: dict) -> None:
        try:
            event = normalize_global_event(raw_event)
        except OpenCodeEventError:
            return
        self.app.call_from_thread(self.post_message, OpenCodeEventReceived(event))

    def _on_sse_state_change(self, state: SSEConnectionState, reason: str) -> None:
        self.app.call_from_thread(self.post_message, LiveConnectionChanged(state, reason))

    def _on_sse_notice(self, notice: str) -> None:
        pass

    @property
    def shutdown_clean(self) -> bool:
        """True only once every owned resource (SSE worker, OpenCode
        server, repository lock) has been definitively released. Read-only:
        set solely by _cleanup_resources()."""
        return self._shutdown_clean

    def action_request_shutdown(self) -> None:
        """Record shutdown intent and start a cleanup attempt if one is
        not already active.

        Idempotent and safe to call repeatedly (e.g. "q" pressed more than
        once, or racing app-level exit): _shutdown_requested is a
        persistent intent flag set once, but a new _shutdown_worker is
        only actually started when no attempt is currently running and
        cleanup has not already succeeded. This lets a failed attempt be
        retried by calling this method again (e.g. from _on_exit_app's
        retry loop, or the user pressing "q"/"Return to runs" again after
        a warning) without ever running two cleanup workers concurrently.
        """
        self._shutdown_requested = True
        self._maybe_start_shutdown_attempt()

    def _maybe_start_shutdown_attempt(self) -> bool:
        """Start a _shutdown_worker attempt if none is active and cleanup
        is not already clean. Returns True if an attempt was (or already
        is) in flight, i.e. the caller may wait on
        _shutdown_complete_event; returns False only if shutdown was
        already clean and there is nothing to wait for."""
        with self._shutdown_attempt_lock:
            if self._shutdown_clean:
                return False
            if self._shutdown_in_progress:
                return True
            self._shutdown_in_progress = True
            self._shutdown_complete_event.clear()

        app = self._app_ref or self.app
        # Run against the App node, not this screen: Textual cancels every
        # worker registered against a widget/screen as soon as it unmounts
        # (Widget._on_unmount -> workers.cancel_node(self)). Since shutdown
        # itself is what causes this screen to unmount (via pop_screen at
        # the end of _shutdown_worker), a worker registered against the
        # screen would race its own cancellation. Registering against the
        # app, which outlives the screen, avoids that self-cancellation.
        app.run_worker(self._shutdown_worker, exclusive=False, thread=True)
        return True

    async def await_shutdown_complete(self) -> None:
        """Await the current shutdown attempt's completion without
        blocking the Textual event loop. Used by
        LoopSupervisorApp._on_exit_app() so app-level exit (ctrl+q,
        App.exit(), or a driver-posted ExitApp on SIGINT/SIGTERM) waits
        for the same cleanup as "q"/on_unmount before the process is
        allowed to actually exit. Does not itself request shutdown or
        start an attempt; the caller must have already called
        action_request_shutdown() or _maybe_start_shutdown_attempt()."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._shutdown_complete_event.wait)

    def _shutdown_worker(self) -> None:
        try:
            self._do_shutdown()
        finally:
            # Reset "an attempt is running" before signalling completion,
            # so that any waiter which wakes on _shutdown_complete_event
            # and immediately calls action_request_shutdown() again (e.g.
            # _on_exit_app's retry loop) can actually start a new attempt
            # rather than seeing a stale _shutdown_in_progress=True.
            self._shutdown_in_progress = False
            # Signal that the shutdown *attempt* finished exactly once,
            # regardless of whether cleanup fully succeeded, raised, or was
            # interrupted partway: any waiter (await_shutdown_complete(), a
            # future retry) must never block forever because of an exception
            # inside cleanup itself. Whether cleanup was *clean* is a
            # separate signal (self._shutdown_clean / shutdown_clean).
            self._shutdown_complete_event.set()

    def _cleanup_resources(self) -> None:
        """Ownership-preserving teardown of SSE worker, OpenCode server, and
        the repository lock, in that order.

        Each owned reference is cleared only once the resource is
        definitively released; a timed-out/failed stop retains the
        reference so a later attempt can retry and so the lock is never
        released while a subordinate resource may still be live. Sets
        self._shutdown_clean only if everything was released.
        """
        clean = True

        if self._sse_client is not None:
            try:
                self._sse_client.stop()
            except Exception:
                # SSE worker did not confirm termination; retain it.
                clean = False
            else:
                self._sse_client = None

        if self._server is not None:
            try:
                self._server.stop()
            except Exception:
                # A stop() exception means at least one owned resource
                # (possibly a live process) was not confirmed released.
                # Retain the server object for retry and do NOT release the
                # lock below.
                clean = False
            else:
                self._server = None

        # Only release the repository lock once the server is definitively
        # gone: the accepted contract holds the lock through OpenCode
        # shutdown (ADR 0009). If the server is still owned, keep the lock.
        if self._server is None and self._lock is not None:
            try:
                self._lock.release()
                self._lock = None
            except Exception:
                # release() only forgets its token on a definitive outcome,
                # so a transient failure here means the lock file is most
                # likely still present and still ours. Leave self._lock set.
                clean = False
                self._set_banner_safe(
                    "[red]Warning: could not release the repository lock; it may "
                    "still be held. Manual inspection (or --recover-stale-lock "
                    "after this process exits) may be required.[/red]"
                )
        elif self._server is not None:
            clean = False

        self._shutdown_clean = clean

    def _do_shutdown(self) -> None:
        # Initialization may still be acquiring the lock, creating/loading
        # state, or starting the server. Never touch the lock or server
        # until it has fully unwound (successfully or not): releasing them
        # concurrently with _do_initialize_locked mutating durable state
        # would allow another process to acquire the lock while this one is
        # still writing to the same repository.
        while not self._init_done_event.is_set():
            self._set_banner_safe("[yellow]Waiting for startup to finish before exiting…[/yellow]")
            self._init_done_event.wait(timeout=1.0)

        # 1. Stop SSE first (ownership-preserving; retained if it does not
        #    confirm termination). Done as part of _cleanup_resources at the
        #    end, but stop it up front too so its worker/callbacks quiesce
        #    while we wait for the advance worker.
        if self._sse_client is not None:
            try:
                self._sse_client.stop()
            except Exception:
                pass
            else:
                self._sse_client = None

        # 2. Ask any in-flight advance()'s OpenCode sessions to abort, then
        #    give the worker a bounded cooperative grace period to unwind.
        server = self._server
        if server is not None:
            try:
                server.abort_active_sessions()
            except Exception:
                pass

        if not self._advance_done_event.wait(timeout=_SHUTDOWN_GRACE_SECONDS):
            # 3. Escalate: the advance worker is still running past the
            #    grace period. Stopping the server breaks any blocked prompt
            #    transport so the worker's OpenCode call fails and it can
            #    unwind, instead of blocking until the (up to 1800s) role
            #    timeout. This does not release the lock.
            self._set_banner_safe(
                "[yellow]Transition still running; stopping OpenCode to "
                "release it, then finishing shutdown…[/yellow]"
            )
            if self._server is not None:
                try:
                    self._server.stop()
                except Exception:
                    pass
                else:
                    self._server = None

        # 4. Wait for the advance worker to FULLY unwind before releasing
        #    the lock. There is no safe bound here: the lock must not be
        #    released while a transition can still mutate Git/state, and no
        #    Python thread may be force-killed.
        while not self._advance_done_event.wait(timeout=_SHUTDOWN_WAIT_SECONDS):
            self._set_banner_safe(
                "[yellow]Waiting for the in-progress transition to finish "
                "before exiting (it will not be interrupted)…[/yellow]"
            )

        # 5. Final ownership-preserving cleanup: (remaining) SSE, server,
        #    then the lock — releasing the lock only once the server is
        #    definitively gone.
        self._cleanup_resources()

        # 6. Only pop the screen on a clean return-to-browser shutdown. If
        #    cleanup left something unresolved, keep the screen (and its
        #    diagnostics) rather than implying a clean teardown.
        if not self._shutdown_clean:
            return

        app = self._app_ref
        if app is not None:
            try:
                app.call_from_thread(app.pop_screen)
            except Exception:
                # The app may already be exiting (app-level exit awaits this
                # same completion event from _on_exit_app before the screen
                # stack is torn down), or this screen may already have been
                # popped by a concurrent shutdown trigger. Either way, the
                # lock/server cleanup above has already completed, which is
                # the only externally-observable guarantee this method makes.
                pass

    def _set_banner_safe(self, text: str) -> None:
        app = self._app_ref
        if app is None:
            return
        try:
            app.call_from_thread(self._set_banner, text)
        except Exception:
            pass

    def on_unmount(self) -> None:
        """Safety net only.

        Normal shutdown always goes through action_request_shutdown() ->
        _shutdown_worker(), which is the only path permitted to release the
        lock or stop the server, and it does so only after both
        initialization and any in-flight advance() have fully stopped. If
        this screen is unmounted without that having happened (e.g. the
        app is torn down some other way), do not release resources here:
        doing so could release the lock or stop the server while a
        background thread is still mutating the repository. Leaving a lock
        held past process exit is recoverable via --recover-stale-lock;
        releasing it early while a Git mutation is in flight is not.
        """
        if not self._shutdown_requested:
            self.action_request_shutdown()


class _InvocationObserver:
    """Posts typed messages to the Textual event loop. Never touches the reducer."""

    def __init__(self, screen: RunScreen) -> None:
        self._screen = screen

    def invocation_started(self, invocation: InvocationRef) -> None:
        self._screen.app.call_from_thread(self._screen.post_message, InvocationStarted(invocation))

    def invocation_finished(self, invocation: InvocationRef, error: object) -> None:
        self._screen.app.call_from_thread(
            self._screen.post_message,
            InvocationFinished(invocation, error if isinstance(error, BaseException) else None),
        )


class _UnstartedRunner:
    def run_agent(self, **_: object) -> str:
        raise LoopError("agent invoked before server was started")


class LoopSupervisorApp(App):
    """Root application. Opens the run browser, then a run screen."""

    CSS = ""

    def __init__(self, project_root: Path, *, recover_stale_lock: bool = False) -> None:
        super().__init__()
        self._project_root = project_root
        self._recover_stale_lock = recover_stale_lock

    def on_mount(self) -> None:
        self.push_screen(
            RunBrowserScreen(
                self._project_root,
                recover_stale_lock=self._recover_stale_lock,
            )
        )

    async def _on_exit_app(self) -> None:
        """Own every app-level exit path.

        ``App.exit()`` (bound to ctrl+q via the built-in ``quit`` action,
        and also what a driver posts on SIGINT/SIGTERM) only ever results
        in an ``ExitApp`` message being posted; ``_on_exit_app`` is
        Textual's single dispatch point for it regardless of the trigger.
        Any active ``RunScreen`` on any screen stack still owns the
        repository lock and possibly a running OpenCode server, so their
        orderly shutdown (stop SSE, abort sessions, wait for the
        in-flight advance() worker, stop the server, release the lock)
        must complete before the underlying Textual shutdown sequence is
        allowed to start closing screens and the driver.

        Awaiting via ``run_in_executor`` keeps the event loop alive and
        processing messages (``call_from_thread`` calls from the
        initialization/advance/shutdown worker threads, typed message
        handlers, etc.) for as long as cleanup needs, rather than
        blocking it.

        Cleanup is retried automatically, indefinitely, until every
        active ``RunScreen`` reports ``shutdown_clean``: a single attempt
        can finish "complete but not clean" (e.g. ``server.stop()``
        raised because the process could not be confirmed exited), and
        the underlying Textual shutdown must never be allowed to proceed
        on top of that — doing so would let the process exit while the
        repository lock is still held or an OpenCode process may still be
        alive. There is deliberately no overall timeout here: unresolved
        child/lock ownership must keep blocking app-level exit rather than
        being abandoned.
        """
        run_screens = [
            screen
            for stack in self._screen_stacks.values()
            for screen in stack
            if isinstance(screen, RunScreen)
        ]
        for screen in run_screens:
            screen.action_request_shutdown()
        for screen in run_screens:
            await screen.await_shutdown_complete()
        while not all(screen.shutdown_clean for screen in run_screens):
            await asyncio.sleep(_SHUTDOWN_RETRY_INTERVAL_SECONDS)
            for screen in run_screens:
                if not screen.shutdown_clean:
                    screen.action_request_shutdown()
            for screen in run_screens:
                await screen.await_shutdown_complete()
        await super()._on_exit_app()
