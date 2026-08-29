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
runs" button, an unexpected unmount, and app-level exit (ctrl+q or an
explicit ``App.exit()`` call) all funnel into the same idempotent,
retryable cleanup via
``RunScreen.action_request_shutdown()``/``_shutdown_worker()``. This
does NOT include OS-level SIGINT/SIGTERM: Textual's Linux driver strips
the terminal's ``ISIG`` flag in raw mode (so Ctrl-C does not even
generate SIGINT — it is read as an ordinary keypress) and installs no
SIGTERM handler at all; only Textual's web driver bridges those signals
into ``ExitApp``. Unlike the headless `run`/`resume` commands (which
bridge SIGTERM into this same cleanup path — see
``cli._bridge_sigterm_to_keyboard_interrupt`` and ADR 0015), `cmd_tui`
deliberately installs no such bridge: injecting an externally raised
``KeyboardInterrupt`` into Textual's running event loop is untested and
could leave the terminal stuck in raw mode. A `kill <pid>` against a
running `loop-supervisor tui` process still terminates at default
disposition today, with the same orphan/stale-lock exposure the CLI
bridge fixes for the headless path; giving the TUI equivalent protection
needs its own UX decision and is tracked separately (backlog item 22b),
not solved here. At
most one cleanup attempt runs at a time per screen
(``_shutdown_attempt_lock``/``_shutdown_in_progress``); a completed
attempt that did not achieve a clean teardown (``shutdown_clean`` is
False — e.g. ``server.stop()`` could not confirm the process exited)
leaves every owned resource in place for a subsequent attempt, triggered
either by the user retrying ("q"/"Return to runs" again) or automatically
by app-level exit.

Lifecycle ownership registry: ``LoopSupervisorApp`` maintains a strong,
app-level registry of every lifecycle-owned ``RunScreen``
(``_owned_run_screens``), populated at the very start of
``RunScreen.on_mount()`` — before any resource acquisition — and
consulted instead of Textual's own ``_screen_stacks`` wherever lifecycle
ownership must be determined. Membership is the sole authority for
"does this app still own something that must be cleaned up before
exit": being unmounted or detached from the visible screen stack never
removes a screen from this registry. A screen is deregistered only once
it is fully quiescent (initialization and any in-flight ``advance()``
have both stopped) and its cleanup is confirmed clean
(``RunScreen.ready_to_finalize``); see ``finalize_run_screen()``, which
is also the only code path permitted to pop a screen, and does so
identity-safely (only if that exact screen is still the active one).

An unexpectedly unmounted, unclean screen therefore remains strongly
reachable and gets its own app-owned automatic retry coordinator
(``ensure_cleanup_coordinator()`` /
``_run_screen_cleanup_coordinator()``), started from
``RunScreen.on_unmount()``: it repeatedly requests shutdown and awaits
completion, retrying indefinitely on a fixed interval until cleanup is
confirmed clean, with no interactive UI required. At most one such
coordinator task runs per screen at a time — ``on_unmount()`` and
app-level exit draining share the same coordinator rather than each
starting their own.

``LoopSupervisorApp`` overrides ``_on_exit_app`` — Textual's single
dispatch point for every exit path, since ``App.exit()`` only ever posts
an ``ExitApp`` message — to repeatedly drain ``_owned_run_screens``:
ensure every currently-registered screen (mounted or detached) has a
running cleanup coordinator, wait (with periodic re-checks so screens
registered while waiting are picked up promptly), and loop until the
registry is empty, before allowing the underlying ``_on_exit_app`` to
proceed. This guarantees the process never exits while the repository
lock is held or an OpenCode server process may still be running,
regardless of which of the above paths triggered the exit, regardless of
how many attempts cleanup actually takes, and regardless of whether a
screen was ever actually visible on the active screen stack.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from pathlib import Path
from typing import cast

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

from ..git import GitRepo
from ..opencode import InvocationRef
from ..opencode_events import OpenCodeEventError, normalize_global_event
from ..runtime import RunSession, _RunOutcome, new_run_session, resume_run_session
from ..sse import SSEClient, SSEConnectionState
from ..state import RunOptions, RunState, list_runs
from ..supervisor import (
    PHASE_AWAITING_INPUT,
    PHASE_DONE,
    PHASE_FAILED,
    PHASE_OPERATIONAL_FAILURE,
    AdvanceStatus,
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

        # Captured once in on_mount(). self.app walks the live parent chain
        # and raises NoActiveAppError once this screen has been detached
        # (e.g. during its own pop_screen at the end of shutdown), so
        # background threads that need to reach the app after that point
        # (notably _shutdown_worker's final pop_screen call) use this
        # cached reference instead of the `self.app` property.
        self._app_ref: LoopSupervisorApp | None = None
        # Owns the lock, OpenCode server, and Supervisor as a single unit
        # (see runtime.RunSession). None until _do_initialize_locked
        # successfully constructs it; from that point on it is entered
        # (RunSession.__enter__ already called) and never re-entered, and
        # this screen calls close() directly on the init/shutdown threads
        # rather than using a `with` block, since acquisition and release
        # happen on two different background threads.
        self._session: RunSession | None = None
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
        # Register with the app's lifecycle-ownership registry BEFORE any
        # resource acquisition (lock, server) begins, and before anything
        # else in this method runs. Registry membership — not whether this
        # screen is mounted/visible/in Textual's own screen stack — is
        # what keeps it strongly reachable and its resources owned for
        # cleanup purposes; see LoopSupervisorApp.register_run_screen().
        app = cast(LoopSupervisorApp, self.app)
        app.register_run_screen(self)
        self._app_ref = app
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

        if self._run_id is None:
            session = new_run_session(
                self._project_root,
                self._options,
                input_provider=self._input_provider,
                recover_stale_lock=self._recover_stale_lock,
                operation="tui",
            )
        else:
            session = resume_run_session(
                self._project_root,
                self._run_id,
                input_provider=self._input_provider,
                recover_stale_lock=self._recover_stale_lock,
                operation="tui",
            )

        try:
            session.__enter__()
        except Exception as exc:
            # RunSession.__enter__ is its own cleanup boundary: on failure
            # it has already released the lock (or left it RELEASE_PENDING
            # if release itself failed) before re-raising, so there is
            # nothing further for this screen to release here. No banner
            # distinction is drawn between a Git error, a lock error, and
            # a resume-validation error: RunSession reports all of them as
            # RuntimeError_, which was already true for the resume case
            # before this migration.
            self.app.call_from_thread(
                self._set_banner, f"[red]Initialization error: {escape(str(exc))}[/red]"
            )
            return
        # From here on this screen owns `session` and is responsible for
        # calling close() on it exactly once cleanup is safe to attempt
        # (see _cleanup_resources()); __enter__ succeeding means the lock
        # is held and _session must be set before any further failure, so
        # a subsequent exception still finds it and can release it.
        self._session = session

        if self._shutdown_requested:
            self._release_after_failed_init()
            return

        try:
            self._do_initialize_locked()
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

        Uses the same ownership-preserving routine as normal shutdown
        (_cleanup_resources(), which itself asks the session to abort any
        active invocations before closing it), so a failed initialization
        can never discard a still-live worker/process or release the lock
        on a transient failure. Deliberately does not finalize
        (deregister/pop) this screen itself: ready_to_finalize requires
        _init_done_event, which is not set until _initialize()'s finally
        block runs *after* this method returns — see the finalization
        check there, which handles both the clean and not-yet-clean cases
        uniformly with normal shutdown.

        Passes outcome=FAILED and error=None to _cleanup_resources,
        deliberately never the caught exception itself. Two reasons:

        1. Every caller of this method is on a failure path (a caught
           exception already reported via the banner, or shutdown racing
           a startup that never got to report success), so silently
           defaulting to close()'s own outcome=SUCCEEDED would reintroduce
           the exact wording defect ADR 0009 documents (a failure
           reported as "run completed but...").
        2. error=None matters for correctness, not just wording, and is
           easy to get backwards -- particularly for one of this
           method's two call sites. From _do_initialize's `except
           Exception as exc:` block, passing error=exc instead would
           make close() treat `exc` as the exception actively unwinding
           through this exact call (sys.exc_info() still reports it for
           the whole duration of a synchronous call made from within
           that block) and silently return without raising on an
           unresolved cleanup -- correct for __exit__, where the
           caller's exception genuinely keeps propagating on its own,
           but wrong here, since that except block does not re-raise
           `exc`; it only renders a banner and returns normally. The
           other call site (shutdown racing a startup that never got to
           report success) is not in an except block at all and has no
           exception to pass regardless. Either way, passing anything
           other than error=None would risk making an unresolved
           cleanup invisible: close() could return silently, this
           method's own exception handling would never fire, and
           shutdown_clean would stay True with the lock still on disk.
           error=None makes every call here a genuine detached call, so
           close() raises on an unresolved cleanup exactly as it
           should."""
        self._cleanup_resources(outcome=_RunOutcome.FAILED)

    def _do_initialize_locked(self) -> None:
        """Everything from server startup through SSE startup.

        Called only once ``self._session`` has successfully entered (lock
        held, state created/validated). Any exception propagates to
        ``_do_initialize``'s handler, which cleans up whatever this method
        had already assigned to ``self._sse_client`` before the session
        (and therefore the lock) is released.
        """
        if self._shutdown_requested:
            return

        session = self._session
        assert session is not None
        session.add_observer(_InvocationObserver(self))
        session.start_server()
        self._state = session.run_state

        if self._shutdown_requested:
            # Shutdown was requested while we were still starting the
            # server. Do NOT abort sessions / stop the server here: doing
            # so would duplicate (and race) _shutdown_worker's own
            # teardown, which is already waiting on _init_done_event
            # before touching this same session (see _do_shutdown).
            # Simply returning leaves self._session owned;
            # _initialize()'s finally block sets _init_done_event right
            # after this method returns, at which point _do_shutdown
            # proceeds to perform the single canonical teardown.
            return

        if session.base_url:
            sse = SSEClient(
                session.base_url,
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
        session = self._session
        denied_count = session.denied_permission_count if session is not None else 0
        denied_summary = session.denied_permission_summary if session is not None else []
        self.query_one("#durable-content", Static).update(
            render_durable_summary(
                state,
                denied_permission_count=denied_count,
                denied_permission_summary=denied_summary,
            )
        )
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

        Distinct from ``RunSession``'s own internal ``_advance_done``
        barrier (see runtime.py's thread-safety docstring): that one
        gates ``RunSession.close()`` against this exact call, while this
        one gates this screen's own ``_do_shutdown`` (which waits on it
        before touching the session at all). ``session.advance()``
        already provides the former; this screen still needs the latter
        to know when it may safely call ``close()``.
        """
        session = self._session
        if session is None or self._state is None:
            self._advance_done_event.set()
            return
        try:
            outcome = session.advance()
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
            AdvanceStatus.INPUT_UNAVAILABLE,
            AdvanceStatus.TERMINAL,
            AdvanceStatus.OPERATIONAL_FAILURE,
        ):
            # INPUT_UNAVAILABLE currently also gets stopped by
            # _start_advance()'s own PHASE_AWAITING_INPUT guard (the
            # phase this status always leaves the state in), so this
            # arm was previously reachable only via that guard rather
            # than this match. Listed explicitly so the stop is a
            # documented consequence of the status, not an incidental
            # side effect of which phase the status happens to leave
            # the state in.
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

    @property
    def ready_to_finalize(self) -> bool:
        """True only once this screen may safely be deregistered from the
        app's lifecycle-ownership registry and (if still the active
        screen) popped.

        Requires all three: initialization has fully finished
        (``_init_done_event``), no ``advance()`` worker is still in flight
        (``_advance_done_event``), and the most recent cleanup attempt
        confirmed every owned resource was released (``shutdown_clean``).
        A screen that is still initializing, still transitioning, or
        whose last cleanup attempt left something unresolved (e.g.
        ``server.stop()`` could not confirm the process exited) is never
        ready to finalize — it must remain registered so the app-level
        exit drain (and any automatic retry coordinator) keeps retrying
        it.
        """
        return (
            self._init_done_event.is_set()
            and self._advance_done_event.is_set()
            and self._shutdown_clean
        )

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
        action_request_shutdown() or _maybe_start_shutdown_attempt().

        Polls with a short bounded wait per executor call, rather than
        one single unbounded `Event.wait()`, so this coroutine remains
        promptly cancellable: a plain unbounded wait handed to
        `run_in_executor()` occupies a real OS thread that keeps running
        even after the awaiting asyncio Task is cancelled (a blocking
        `threading.Event.wait()` cannot be interrupted), which can hang
        the executor's own shutdown/join indefinitely if the event is
        never set (e.g. this is called against a screen whose shutdown
        was already "clean" with no attempt actually started/signalled).
        Bounding each individual wait means a cancellation is observed
        between polls, within one bound, instead of blocking forever.
        """
        loop = asyncio.get_running_loop()
        while not self._shutdown_complete_event.is_set():
            await loop.run_in_executor(None, self._shutdown_complete_event.wait, 0.1)

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

    def _cleanup_resources(
        self, *, outcome: _RunOutcome = _RunOutcome.SUCCEEDED, error: BaseException | None = None
    ) -> None:
        """Ownership-preserving teardown of the SSE worker, then the
        session (OpenCode server + repository lock together, via
        ``RunSession.close()``).

        SSE is torn down first since it is TUI-owned and RunSession has
        no knowledge of it; a timed-out/failed stop retains the SSE
        reference so a later attempt can retry. ``session.close()`` then
        owns the server-stop/lock-release ordering and retry semantics
        itself (see ADR 0009) — it is never null-checked and cleared here
        the way the SSE client is, because RunSession tracks "is this
        confirmed released" via its own SessionState rather than by
        discarding its internal references, and re-invoking close() on
        the same still-owned session is exactly how a retry is meant to
        work. Sets self._shutdown_clean only if everything was released.

        ``outcome``/``error`` are forwarded to ``session.close()``
        unchanged and default to exactly what a bare ``close()`` call
        defaults to (matching the CLI's ``__exit__`` with no body
        exception): ordinary shutdown — requested via "q", the "Return to
        runs" button, or app-level exit — is not itself evidence the run
        failed, regardless of which phase the run happened to be in.
        Only ``_release_after_failed_init()`` overrides these, since it
        is a detached caller invoked from inside an active ``except``
        block with a real failure already in hand.

        RunSession.close() raises on an unresolved failure (its
        single-owner cleanup contract, load-bearing for the headless
        CLI's traceback guarantees); this screen's contract is instead
        the non-raising shutdown_clean boolean the app-level exit-retry
        loop depends on, so the raise is caught here and never allowed to
        escape. Caught as BaseException, not Exception: an operator
        KeyboardInterrupt/SystemExit raised from close() itself must
        still leave the screen in the same "retry later" shape as any
        other unresolved cleanup, not crash the shutdown worker.
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

        session = self._session
        if session is not None:
            try:
                session.close(outcome=outcome, error=error)
            except BaseException:  # noqa: BLE001 - mapped to shutdown_clean, never re-raised
                # CLEANUP_UNRESOLVED (server may still be alive) or
                # RELEASE_PENDING (server confirmed gone, lock release
                # itself failed) both retain what close() still owns;
                # either way this screen has nothing further to release
                # and must simply report "not clean" and let a later
                # close() retry.
                clean = False
                self._set_banner_safe(
                    "[red]Warning: could not confirm OpenCode/lock cleanup; it may "
                    "still be held. Manual inspection (or --recover-stale-lock "
                    "after this process exits) may be required.[/red]"
                )

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
        # RunSession.abort_active_invocations() is a no-op before startup
        # and never raises (see its docstring), so no None-check or
        # try/except is needed here the way the raw server call required.
        session = self._session
        if session is not None:
            session.abort_active_invocations()

        if not self._advance_done_event.wait(timeout=_SHUTDOWN_GRACE_SECONDS):
            # 3. Escalate: the advance worker is still running past the
            #    grace period. Stopping the server breaks any blocked prompt
            #    transport so the worker's OpenCode call fails and it can
            #    unwind, instead of blocking until the (up to 1800s) role
            #    timeout. RunSession.stop_server() never releases the lock
            #    or touches the lease -- only close() (in the final cleanup
            #    step below) ever does either -- and, unlike the server
            #    reference this replaced, is never cleared here: RunSession
            #    tracks whether cleanup is confirmed via its own
            #    SessionState, not by discarding self._session, and
            #    stop_server() itself never raises (see its docstring), so
            #    there is nothing here to catch.
            self._set_banner_safe(
                "[yellow]Transition still running; stopping OpenCode to "
                "release it, then finishing shutdown…[/yellow]"
            )
            if session is not None:
                session.stop_server()

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

        # 6. Only finalize (deregister from the app's lifecycle-ownership
        #    registry, and pop if still the active screen) once this
        #    screen is fully quiescent and clean. If cleanup left
        #    something unresolved, keep the screen registered (and its
        #    diagnostics) rather than implying a clean teardown; the
        #    registry, not whether this screen is popped, is what
        #    app-level exit relies on to know cleanup is not yet done.
        if not self.ready_to_finalize:
            return

        app = self._app_ref
        if app is not None:
            try:
                app.call_from_thread(app.finalize_run_screen, self)
            except Exception:
                # The app may already be exiting (app-level exit's drain
                # loop finalizes registered screens itself before the
                # screen stack is torn down), or this screen may already
                # have been finalized by a concurrent shutdown trigger
                # (finalize_run_screen() is idempotent). Either way, the
                # lock/server cleanup above has already completed, which
                # is the only externally-observable guarantee this method
                # makes.
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
        """Notify the app that this screen has detached, and ensure it
        keeps getting cleaned up even though it is no longer visible.

        Normal shutdown always goes through action_request_shutdown() ->
        _shutdown_worker(), which is the only path permitted to release the
        lock or stop the server, and it does so only after both
        initialization and any in-flight advance() have fully stopped. This
        method never releases resources directly: doing so could release
        the lock or stop the server while a background thread is still
        mutating the repository. Leaving a lock held past process exit is
        recoverable via --recover-stale-lock; releasing it early while a
        Git mutation is in flight is not.

        Unmounting (e.g. via an unexpected pop, or a stack replacement)
        never removes this screen from the app's lifecycle-ownership
        registry — only finalize_run_screen() does that, and only once
        cleanup is confirmed clean. This method's job is only to make sure
        an app-owned retry coordinator exists for this now-detached screen
        so it keeps getting cleaned up automatically with no interactive
        UI available to press "q"/"Return to runs" on.
        """
        if not self._shutdown_requested:
            self.action_request_shutdown()
        app = self._app_ref
        if app is not None:
            app.ensure_cleanup_coordinator(self)


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


class LoopSupervisorApp(App):
    """Root application. Opens the run browser, then a run screen."""

    CSS = ""

    def __init__(self, project_root: Path, *, recover_stale_lock: bool = False) -> None:
        super().__init__()
        self._project_root = project_root
        self._recover_stale_lock = recover_stale_lock
        # Authoritative lifecycle-ownership registry: every RunScreen that
        # has begun (or is about to begin) acquiring resources, whether or
        # not it is currently mounted/visible/in Textual's own
        # _screen_stacks. This — not _screen_stacks — is what
        # _on_exit_app() consults to decide whether the process may
        # actually exit. Membership is added in RunScreen.on_mount()
        # before any resource acquisition, and removed only by
        # finalize_run_screen() once a screen is fully quiescent and its
        # cleanup is confirmed clean. All mutation of this registry (and
        # of _run_screen_cleanup_tasks below) happens on the event-loop
        # thread: on_mount/on_unmount are Textual callbacks that already
        # run there, finalize_run_screen() is only ever invoked directly
        # from the loop or via call_from_thread (which itself runs the
        # target on the loop), and the coordinator tasks below are
        # themselves asyncio tasks scheduled on this app's loop.
        self._owned_run_screens: set[RunScreen] = set()
        # At most one automatic cleanup/retry coordinator task per
        # RunScreen. RunScreen.on_unmount() and _on_exit_app()'s drain
        # loop both call ensure_cleanup_coordinator() rather than each
        # starting their own worker, so a detached-and-unclean screen is
        # never retried by two overlapping coordinators at once.
        self._run_screen_cleanup_tasks: dict[RunScreen, asyncio.Task[None]] = {}

    def on_mount(self) -> None:
        self.push_screen(
            RunBrowserScreen(
                self._project_root,
                recover_stale_lock=self._recover_stale_lock,
            )
        )

    def register_run_screen(self, screen: RunScreen) -> None:
        """Add `screen` to the lifecycle-ownership registry.

        Must be called before that screen acquires any resource (lock,
        server) — see RunScreen.on_mount(). Idempotent: registering an
        already-registered screen is a no-op.
        """
        self._owned_run_screens.add(screen)

    def ensure_cleanup_coordinator(self, screen: RunScreen) -> None:
        """Ensure exactly one automatic cleanup/retry coordinator task is
        running for `screen`.

        A no-op if a coordinator for this exact screen is already running
        (whether started by a prior on_unmount() or a prior _on_exit_app()
        drain iteration) — callers never need to check first. If the
        previous coordinator for this screen has already finished (either
        because it successfully finalized the screen, or because it
        failed unexpectedly), a fresh one is started. Never starts a
        coordinator for a screen that is not (or is no longer)
        registered — there would be nothing for it to do.
        """
        if screen not in self._owned_run_screens:
            return
        existing = self._run_screen_cleanup_tasks.get(screen)
        if existing is not None and not existing.done():
            return
        self._run_screen_cleanup_tasks[screen] = asyncio.create_task(
            self._run_screen_cleanup_coordinator(screen)
        )

    async def _run_screen_cleanup_coordinator(self, screen: RunScreen) -> None:
        """Repeatedly request shutdown on `screen` and await completion,
        retrying indefinitely on a fixed interval until either the screen
        is finalized (deregistered) or it is no longer registered at all
        (already finalized by someone else, e.g. a direct "q" press that
        completed cleanly while this coordinator was between retries).

        Never overlaps with itself: ensure_cleanup_coordinator() guards
        against a second task being created while this one is still
        running. A failure anywhere in this coroutine is caught and
        swallowed rather than propagated — an unexpected exception here
        must fail closed (the screen stays registered, so a later
        ensure_cleanup_coordinator() call, e.g. from the next
        _on_exit_app() drain iteration, starts a fresh attempt) rather
        than silently abandoning ownership of a possibly-still-live
        server/lock.
        """
        try:
            while screen in self._owned_run_screens:
                # Only await completion if shutdown is not already clean.
                # request_shutdown()/_maybe_start_shutdown_attempt() do
                # not set _shutdown_complete_event when cleanup already
                # succeeded (there is no worker to run and signal it) —
                # awaiting unconditionally here would deadlock on a
                # screen that reached "already clean" via some path other
                # than a _shutdown_worker attempt (e.g. cleanup performed
                # directly by a failed-initialization handler). This
                # check is the narrow, non-redesigning guard for that
                # case; the full per-attempt signaling redesign is Step
                # 5's responsibility, not this one's.
                if not screen.shutdown_clean:
                    screen.action_request_shutdown()
                    await screen.await_shutdown_complete()
                if screen not in self._owned_run_screens:
                    return
                if screen.ready_to_finalize:
                    self.finalize_run_screen(screen)
                    return
                await asyncio.sleep(_SHUTDOWN_RETRY_INTERVAL_SECONDS)
        except Exception:
            # Fail closed: leave the screen registered so a subsequent
            # ensure_cleanup_coordinator() call retries from scratch.
            pass

    def finalize_run_screen(self, screen: RunScreen) -> None:
        """Deregister `screen` from the lifecycle-ownership registry, and
        pop it if (and only if) it is still the exact active screen.

        Must only be called once `screen.ready_to_finalize` is True (both
        RunScreen._do_shutdown()'s success path and
        _run_screen_cleanup_coordinator() check this before calling).
        Idempotent: calling this for a screen that is no longer registered
        (already finalized by a concurrent path) is a safe no-op.

        Identity-safe by construction: `self.screen` is Textual's actual
        current top-of-stack screen, so popping only happens when this
        exact screen object is it. If a different screen has since become
        active (this one was pushed under something else, or already
        replaced), nothing is popped — only this screen's registry
        membership (and thus this app's ownership bookkeeping) is
        cleared. A detached screen that finalizes late can therefore never
        pop an unrelated, currently active screen.
        """
        if screen not in self._owned_run_screens:
            return
        if self.screen is screen:
            try:
                self.pop_screen()
            except Exception:
                # There may be nothing left to pop to (e.g. this was the
                # only screen on the stack), or a concurrent path may have
                # already popped it. Either way, this screen's resources
                # are already confirmed released (ready_to_finalize
                # requires shutdown_clean), so deregistering below is
                # still correct and safe.
                pass
        self._owned_run_screens.discard(screen)
        self._run_screen_cleanup_tasks.pop(screen, None)

    async def _on_exit_app(self) -> None:
        """Own every app-level exit path.

        ``App.exit()`` (bound to ctrl+q via the built-in ``quit`` action,
        and also what a driver posts on SIGINT/SIGTERM) only ever results
        in an ``ExitApp`` message being posted; ``_on_exit_app`` is
        Textual's single dispatch point for it regardless of the trigger.
        Every ``RunScreen`` in ``_owned_run_screens`` — mounted or
        detached, visible or not — still owns the repository lock and
        possibly a running OpenCode server, so their orderly shutdown
        (stop SSE, abort sessions, wait for the in-flight advance()
        worker, stop the server, release the lock) must complete before
        the underlying Textual shutdown sequence is allowed to start
        closing screens and the driver. ``_screen_stacks`` is
        deliberately never consulted here: it reflects Textual's
        visibility bookkeeping, not lifecycle ownership, and an
        unexpectedly detached-but-unclean screen would otherwise be
        invisible to this method entirely (see the ADR 0009 "Detached-
        screen ownership loss" blocker this registry replaces).

        Draining is repeated, not a single pass: a coordinator's
        completion may deregister some screens while leaving others
        (or newly-registered ones, if a new run was started while exit was
        already waiting) still owned, so the registry is re-read after
        every batch of coordinators finishes, and the underlying Textual
        shutdown is only invoked once it is completely empty. A
        coordinator that raises is treated as leaving its screen owned
        (fails closed) rather than as success. There is deliberately no
        overall timeout here: unresolved child/lock ownership must keep
        blocking app-level exit rather than being abandoned.
        """
        while self._owned_run_screens:
            pending = list(self._owned_run_screens)
            for screen in pending:
                self.ensure_cleanup_coordinator(screen)
            tasks = [
                self._run_screen_cleanup_tasks[screen]
                for screen in pending
                if screen in self._run_screen_cleanup_tasks
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            else:
                # Every pending screen already had no coordinator to wait
                # on (e.g. it was deregistered between the snapshot and
                # ensure_cleanup_coordinator() finding it unregistered) —
                # avoid a tight busy loop before re-checking the registry.
                await asyncio.sleep(_SHUTDOWN_RETRY_INTERVAL_SECONDS)
        await super()._on_exit_app()
