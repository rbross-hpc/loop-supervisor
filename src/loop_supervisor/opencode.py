"""OpenCode process/HTTP adapter.

Owns all details of starting `opencode serve`, creating sessions, and
sending prompts with structured output. The supervisor state machine talks
to the `AgentRunner` protocol instead, so it can be tested with a fake.

Never logs environment variables, full config, or authorization headers.
"""

from __future__ import annotations

import collections
import os
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar

import httpx

_READY_RE = re.compile(r"opencode server listening on (https?://\S+)")
_STDOUT_DRAIN_MAXLEN = 500

# Bound on the stdout pump's non-newline-terminated `partial` fragment.
# A subprocess writing an arbitrarily long line with no newline -- or
# one that never arrives -- would otherwise grow `partial` (and the
# `_stdout_partial` snapshot derived from it, which flows into startup
# diagnostics via _diagnostic_output()) without limit. Once `partial`
# would exceed this bound, the accumulated bytes are dropped entirely
# (not retained for later use) and the pump switches to "oversized"
# mode: it discards further bytes for the same line, scanning only for
# the eventual newline, so memory use for a single pathological line is
# bounded at roughly this constant plus one read()'s worth of bytes,
# never growing further regardless of how long the line actually is.
# supervisor.py's own _truncate_message() separately bounds the final
# persisted record; this bound exists further upstream, in memory,
# before that point is ever reached.
_MAX_STDOUT_FRAGMENT_BYTES = 64 * 1024

# Bound for waiting on the process group after SIGTERM before escalating
# to SIGKILL, and again after SIGKILL before giving up and reporting
# unresolved ownership. The group-wide SIGKILL is unconditional after
# the TERM grace period so a descendant that ignores SIGTERM is always
# killed before stop() reports success or unresolved ownership.
_GROUP_TERM_WAIT_SECONDS = 5.0
_GROUP_KILL_WAIT_SECONDS = 5.0
_GROUP_POLL_INTERVAL_SECONDS = 0.05

# Timeout for reading the launcher's identity line ("ready:pid:pgid\n")
# from the control pipe. If the launcher hasn't written it within this
# window, startup is treated as failed.
_LAUNCHER_READY_TIMEOUT_SECONDS = 10.0

# Bound for best-effort session-abort requests issued during cleanup
# (after a phase timeout, or when tearing down active sessions on
# shutdown). Deliberately short and finite: this must never share the
# long-lived control client's timeout=None default, since a hung abort
# request would then block cleanup indefinitely — exactly the failure
# mode abort is meant to help recover from. A fresh, short-lived client
# with an explicit connect/read/write/pool budget is used for every call
# rather than the shared client, so a hung abort can never delay whatever
# is waiting on cleanup (server teardown, lock release) beyond this
# bound.
_ABORT_TIMEOUT_SECONDS = 5.0

# Bound for a single httpx.Client.close() call. httpx's own request
# timeout (above) does not cover close() at all: a hung or slow close
# after an already-selected timeout/response would otherwise re-introduce
# exactly the unbounded-wait failure mode the request timeout is meant to
# prevent. close() is run on a dedicated daemon thread (see
# _BoundedCloseAttempt/_close_bounded below) so this bound can be enforced
# without a
# built-in way to forcibly interrupt a blocked synchronous call: Python
# cannot safely kill a running thread, so a timed-out close leaves its
# worker thread (and the client/socket it holds) to finish in the
# background rather than being abandoned mid-syscall.
_CLIENT_CLOSE_TIMEOUT_SECONDS = 1.0

_LAUNCHER_SCRIPT = str(Path(__file__).parent / "_launcher.py")


class OpenCodeError(RuntimeError):
    """Base class for all OpenCode adapter errors."""


class ServerStartupError(OpenCodeError):
    """Raised when the server process fails to become ready in time."""


class PhaseTimeoutError(OpenCodeError):
    """Raised when a role invocation exceeds its allotted time."""


class AgentInvocationError(OpenCodeError):
    """Raised when the OpenCode API returns a non-2xx or error response."""


class OpenCodeCleanupError(OpenCodeError):
    """Raised by stop() when one or more cleanup stages failed.

    Every cleanup stage is still attempted regardless of earlier
    failures (see OpenCodeServer.stop()); this exception is raised only
    after every stage has run, to report that cleanup was incomplete.
    """


class AgentRunner(Protocol):
    """Minimal interface the supervisor needs from an OpenCode backend."""

    def run_agent(
        self,
        *,
        agent: str,
        directory: Path,
        prompt: str,
        json_schema: dict[str, Any] | None = None,
        timeout: float = 1800.0,
    ) -> str:
        """Run one role invocation in a fresh session and return the raw
        text output (expected to be a single JSON object as text, or the
        structured_output field serialized to text)."""
        ...


@dataclass(frozen=True)
class InvocationRef:
    """Identifies an active OpenCode agent invocation."""

    session_id: str
    agent: str
    directory: Path
    started_monotonic: float


class InvocationObserver(Protocol):
    """Observer notified when an agent invocation starts or finishes."""

    def invocation_started(self, invocation: InvocationRef) -> None: ...

    def invocation_finished(
        self,
        invocation: InvocationRef,
        error: BaseException | None,
    ) -> None: ...


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class _ProcessOwner:
    launcher: subprocess.Popen[bytes]
    pgid: int
    command_fd: int
    event_fd: int
    state: str = field(default="live")
    term_sent: bool = False
    kill_sent: bool = False
    event_buffer: bytes = b""

    def send(self, command: str) -> None:
        if self.command_fd < 0:
            raise OpenCodeCleanupError("launcher command pipe is closed")
        os.write(self.command_fd, f"{command}\n".encode())

    def close_pipes(self) -> None:
        errors: list[Exception] = []
        for name in ("command_fd", "event_fd"):
            fd = getattr(self, name)
            if fd < 0:
                continue
            try:
                os.close(fd)
            except Exception as exc:
                errors.append(exc)
            else:
                setattr(self, name, -1)
        if errors:
            if len(errors) == 1:
                raise errors[0]
            raise ExceptionGroup("errors closing launcher pipes", errors)

    def read_event(self, timeout: float) -> str | None:
        if self.event_fd < 0:
            return None
        deadline = time.monotonic() + timeout
        while True:
            if b"\n" in self.event_buffer:
                raw, self.event_buffer = self.event_buffer.split(b"\n", 1)
                return raw.decode(errors="replace").strip()
            remaining = deadline - time.monotonic()
            if remaining < 0:
                return None
            ready, _, _ = select.select([self.event_fd], [], [], remaining)
            if not ready:
                return None
            chunk = os.read(self.event_fd, 256)
            if not chunk:
                return None
            self.event_buffer += chunk

    def wait_for_acknowledgement(self, prefix: str, timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            event = self.read_event(remaining)
            if event is None:
                return None
            if event.startswith(prefix):
                return event

    def wait_for_child_exit(self, timeout: float) -> bool:
        return self.wait_for_acknowledgement("child-exit:", timeout) is not None

    def wait_launcher(self, timeout: float) -> bool:
        try:
            self.launcher.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def shutdown_confirmed(self) -> bool:
        return self.kill_sent and self.launcher.returncode == -signal.SIGKILL


class _BoundedCloseAttempt:
    """Tracks one in-progress httpx.Client.close() call run on a dedicated
    daemon thread.

    Construction alone never starts the worker: call start() (or use the
    _start_bounded_close() factory below) to do that. This keeps
    construction and thread startup separable so a caller can catch
    failures from each step without any of them escaping as an
    unhandled exception from __init__.

    Python has no safe way to forcibly interrupt a blocked synchronous
    call, so a close() that does not finish within the caller's bound
    cannot be cancelled — it is left running in the background (the
    thread is a daemon, so it can never block interpreter exit) and this
    object remains the single handle for observing when/how it
    eventually finishes. This also means close() must never be invoked a
    second time concurrently against the same client: callers that want
    to retry waiting on a possibly-still-running close should call
    wait() again on the *same* attempt rather than creating a new one.
    """

    def __init__(self, client: httpx.Client) -> None:
        self.client = client
        self._done = threading.Event()
        self._started = threading.Event()
        self._error: BaseException | None = None
        self._startup_error: BaseException | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Construct and start the daemon worker thread. May raise if
        threading.Thread(...) construction or thread.start() itself
        fails; callers should treat that as an orchestration failure
        distinct from close() failing (see _start_bounded_close, which
        wraps this non-throwingly)."""
        thread = threading.Thread(target=self._run, name="opencode-client-close", daemon=True)
        self._thread = thread
        thread.start()

    def _run(self) -> None:
        self._started.set()
        try:
            self.client.close()
        except BaseException as exc:  # noqa: BLE001 - reported to the waiter, not raised here
            self._error = exc
        finally:
            self._done.set()

    def record_startup_error(self, error: BaseException) -> None:
        self._startup_error = error

    def worker_may_have_started(self) -> bool:
        thread = self._thread
        return self._started.is_set() or (thread is not None and thread.ident is not None)

    def wait(self, timeout: float) -> bool:
        """Wait up to timeout seconds for the close to finish (whether it
        succeeded or raised). Returns False if it is still running."""
        return self._done.wait(timeout=timeout)

    @property
    def error(self) -> BaseException | None:
        """The exception close() raised, if it has finished and did so."""
        return self._error

    @property
    def startup_error(self) -> BaseException | None:
        return self._startup_error


def _start_bounded_close(client: httpx.Client) -> _BoundedCloseAttempt | BaseException:
    """Non-throwing factory: construct a _BoundedCloseAttempt and start
    its daemon worker thread, catching any failure from attempt/Event
    construction, threading.Thread(...) construction, or thread.start()
    and returning it instead of raising. Never invokes client.close()
    itself on the calling thread. Callers must not let the returned
    BaseException replace an already-decided primary outcome (see the
    precedence convention documented on _close_request_local_client and
    the shared-client handling in OpenCodeServer.stop())."""
    try:
        attempt = _BoundedCloseAttempt(client)
    except BaseException as exc:  # noqa: BLE001 - reported to the caller, not raised here
        return exc
    try:
        attempt.start()
    except BaseException as exc:  # noqa: BLE001 - reported to the caller, not raised here
        try:
            worker_may_have_started = attempt.worker_may_have_started()
        except BaseException:  # noqa: BLE001 - ambiguous startup retains ownership
            worker_may_have_started = True
        if worker_may_have_started:
            try:
                attempt.record_startup_error(exc)
            except BaseException:  # noqa: BLE001 - retain ambiguous worker ownership
                pass
            return attempt
        return exc
    return attempt


T = TypeVar("T")


def _run_with_deadline(
    operation: Callable[[], T],
    *,
    deadline: float,
    timeout_error: PhaseTimeoutError,
) -> T:
    """Run a blocking HTTP operation without letting inactivity-based
    transport timeouts extend its absolute monotonic deadline.

    httpx's synchronous API has no wall-clock timeout: each arriving byte
    resets its read timeout. The daemon worker keeps that blocking I/O off
    the caller thread, while the caller waits only until ``deadline``. The
    request-local client's bounded close, performed by the caller's existing
    error path, then interrupts the transport when possible; the daemon
    ownership ensures an uncooperative transport cannot hold up the role.
    """
    done = threading.Event()
    result: list[T] = []
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            result.append(operation())
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller thread
            errors.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=_run, name="opencode-http-request", daemon=True)
    worker.start()
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not done.wait(timeout=remaining):
        raise timeout_error
    if errors:
        raise errors[0]
    return result[0]


def _safe_exception_text(error: BaseException) -> str:
    try:
        return str(error)
    except BaseException:  # noqa: BLE001 - diagnostic rendering must not escape
        try:
            return f"unprintable {type(error).__name__}"
        except BaseException:  # noqa: BLE001 - use a constant if introspection fails
            return "unprintable cleanup failure"


def _close_request_local_client(client: httpx.Client, primary: BaseException | None) -> None:
    """Close a request-local (not the shared control) client with a
    bounded wait, applying the primary-error-precedence convention used
    throughout this module (see ADR 0009):

    - If `primary` is not None, the caller already has a decided outcome
      (a translated timeout/network error, an HTTP-status error, or a
      malformed-response error) that it is about to raise. A close
      failure or timeout must never replace it — it is attached as a
      note on the same exception object instead, so the caller's bare
      `raise` still re-raises exactly the original exception.
    - If `primary` is None, the request itself succeeded and there is no
      decided outcome yet: an unconfirmed close is itself the failure,
      so it is raised as OpenCodeCleanupError rather than silently
      returning a result whose underlying connection may still be open.

    The worker thread started for this close is daemon-owned and never
    registered anywhere else: a request-local close that times out is
    solely owned by its own background thread, never added to
    OpenCodeServer's shared-client retry state (that state exists only
    for the long-lived control client tracked by stop()).
    """
    error = _close_bounded(client)
    if error is None:
        return
    if primary is not None:
        _add_cleanup_note(
            primary,
            "additionally, closing the HTTP client did not complete cleanly: ",
            error,
        )
        return
    raise OpenCodeCleanupError(
        "closing the HTTP client did not complete cleanly: " + _safe_exception_text(error)
    ) from error


def _add_cleanup_note(
    primary: BaseException,
    prefix: str,
    cleanup_error: BaseException,
) -> None:
    """Attach one deterministic cleanup note to `primary`, preserving any
    existing notes. Never touches `primary.__cause__`, and never lets a
    failure while formatting or calling add_note() itself propagate: the
    primary exception's identity must be preserved regardless of whether
    annotating it succeeds."""
    try:
        primary.add_note(prefix + _safe_exception_text(cleanup_error))
    except BaseException:  # noqa: BLE001 - annotating must never replace the primary
        pass


def _as_reportable_error(error: BaseException) -> Exception:
    """Normalize an arbitrary BaseException into an Exception suitable
    for inclusion in stop()'s errors without ever raising itself."""
    if isinstance(error, Exception):
        return error
    return OpenCodeCleanupError(_safe_exception_text(error))


def _close_bounded(client: httpx.Client, timeout: float | None = None) -> BaseException | None:
    """Close a request-local client on a dedicated daemon thread, bounded
    by `timeout` (or, if not given, the *current* value of
    _CLIENT_CLOSE_TIMEOUT_SECONDS — looked up dynamically here rather
    than captured at function-definition time, so a test or caller that
    changes the module-level constant is honored on every call). Returns
    None once close is confirmed to have completed successfully.
    Otherwise returns an exception describing why it did not: either the
    exception close() itself raised, an orchestration failure starting
    the worker, or an OpenCodeCleanupError if close had not completed at
    all within the bound. Never raises: the caller decides how to
    combine this with any primary exception it is already handling (see
    the precedence convention used throughout create_session/
    send_prompt/_abort_session_bounded: a close failure is attached as a
    note to a primary exception, never allowed to replace it).

    Intended for one-off request-local clients where no later stop()
    needs to track an in-progress close; OpenCodeServer.stop() manages
    its own _BoundedCloseAttempt directly instead, since a retried
    stop() must observe (not restart) an already-running close on the
    shared client.
    """
    bound = _CLIENT_CLOSE_TIMEOUT_SECONDS if timeout is None else timeout
    attempt = _start_bounded_close(client)
    if isinstance(attempt, BaseException):
        return attempt
    try:
        finished = attempt.wait(bound)
        if not finished:
            return OpenCodeCleanupError(
                f"closing the HTTP client did not complete within {bound}s; "
                "the close is still running in the background"
            )
        startup_error = attempt.startup_error
        error = attempt.error
    except BaseException as exc:  # noqa: BLE001 - reported to the caller, not raised here
        return exc
    return error if error is not None else startup_error


@dataclass
class OpenCodeServerConfig:
    executable: str = "opencode"
    hostname: str = "127.0.0.1"
    port: int | None = None
    startup_timeout: float = 30.0
    env: dict[str, str] | None = None


def build_agent_env(
    project_root: Path,
    *,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the environment OpenCode (and every agent invocation under it)
    inherits, with each project's `.venv/bin` made findable on `PATH`.

    Agents commonly need `pytest`, `ruff`, and `mypy`, which this project's
    own convention installs into a project-local `.venv` rather than onto
    the ambient `PATH`. Without this, an agent that globs for those tools
    and doesn't find them on `PATH` may go looking in system directories
    (e.g. `/usr/local/bin`) outside the project, which under a
    server-driven run (no human to answer an `external_directory` prompt)
    can hang forever rather than fail (see ADR 0014).

    Two entries are prepended, in this order:

    1. A *relative* `.venv/bin`, included unconditionally regardless of
       whether it exists relative to this process's own cwd. Every agent
       invocation runs with its `directory` set to its own task worktree
       (see `_do_building` / `_do_auditing`), and a relative `PATH` entry
       is resolved fresh at each individual command's exec-time against
       *that* process's cwd, not against whatever directory the
       supervisor itself happens to be running in when this function
       runs (typically once, at server startup). Checking existence here
       would test the wrong directory entirely, so it is deliberately
       skipped; a worktree with no `.venv/bin` simply finds nothing there
       and falls through to the next entry, same as any other missing
       PATH directory. This is deliberately not a symlink to the
       integration project's venv: an editable install's `.pth` file (and
       every console-script shebang in `.venv/bin`) embeds an *absolute*
       path, so a symlinked/shared venv would silently run tests against
       the integration checkout's source tree instead of the task
       worktree's, defeating the point of verification without any
       visible error.
    2. `<project_root>/.venv/bin`, an absolute fallback for invocations
       whose cwd has no venv of its own (e.g. the top-level planner
       working in the integration root). This one *is* checked for
       existence, since `project_root` is a real, known path at the time
       this function runs.

    Returns a new dict; never mutates `base_env`.
    """
    env = dict(base_env if base_env is not None else os.environ)
    existing_path = env.get("PATH", "")
    existing_entries = existing_path.split(os.pathsep) if existing_path else []

    relative_entry = str(Path(".venv/bin"))
    absolute_entry = str(project_root / ".venv" / "bin")

    prefix = [relative_entry]
    if (project_root / ".venv" / "bin").is_dir():
        prefix.append(absolute_entry)

    prefix = [entry for entry in prefix if entry not in existing_entries]

    env["PATH"] = os.pathsep.join([*prefix, *existing_entries])
    return env


class OpenCodeServer:
    """Manages the lifecycle of one `opencode serve` process."""

    def __init__(self, project_dir: Path, config: OpenCodeServerConfig | None = None) -> None:
        self.project_dir = project_dir
        self.config = config or OpenCodeServerConfig()
        # Anchored process-group owner. Set only after the launcher's
        # identity has been read and verified from the control pipe; cleared
        # only after the launcher is reaped and the group confirmed gone.
        # None means no process is owned (server not started, or fully
        # cleaned up). stop() fails closed: it never falls back to
        # direct-child-only cleanup, and it never clears this field unless
        # every mandatory shutdown stage succeeded.
        self._owner: _ProcessOwner | None = None
        self._pending_launcher: subprocess.Popen[bytes] | None = None
        self.base_url: str | None = None
        self._client: httpx.Client | None = None
        # In-progress bounded close() of the shared client, if one is
        # currently running in the background past a prior stop()'s
        # timeout. A retried stop() must observe this existing attempt
        # rather than starting a second concurrent close() against the
        # same client, which would be undefined/racy on the underlying
        # httpx.Client.
        self._client_close_attempt: _BoundedCloseAttempt | None = None
        self._active_sessions: dict[str, InvocationRef] = {}
        self._active_sessions_lock = threading.Lock()
        self._observers: list[InvocationObserver] = []
        # A single thread owns the launcher's forwarded stdout for the
        # lifetime of the process. Readiness detection waits on
        # _stdout_ready_event (set by the pump thread), never reads the
        # pipe directly, so the deadline check is never blocked by a pipe
        # read.
        self._stdout_thread: threading.Thread | None = None
        self._stdout_lines: collections.deque[str] = collections.deque(maxlen=_STDOUT_DRAIN_MAXLEN)
        self._cleanup_lock = threading.RLock()
        self._stdout_stop = threading.Event()
        self._stdout_ready_event = threading.Event()
        self._stdout_eof_event = threading.Event()
        self._stdout_partial_lock = threading.Lock()
        self._stdout_partial = ""

    def __enter__(self) -> OpenCodeServer:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> Literal[False]:
        """Context-manager exit: always attempt stop(), never suppress body
        exceptions.

        - With no body exception: stop() errors propagate normally.
        - With a body exception: stop() is still attempted; if it raises
          *any* BaseException — including KeyboardInterrupt/SystemExit
          raised by cleanup itself, not just ordinary Exception subclasses
          — the failure is attached as a note to the body exception, and
          the body exception is re-raised unchanged (return False). The
          body exception (a RuntimeError, KeyboardInterrupt, SystemExit,
          or anything else) is never replaced by a cleanup failure.
        """
        if exc_val is None:
            self.stop()
            return False
        try:
            self.stop()
        except BaseException as stop_exc:  # noqa: BLE001 - must never replace exc_val
            _add_cleanup_note(
                exc_val,
                "additionally, OpenCodeServer.stop() failed during __exit__: ",
                stop_exc,
            )
        return False

    def _serve_command(self, port: int) -> list[str]:
        return [
            self.config.executable,
            "serve",
            "--hostname",
            self.config.hostname,
            "--port",
            str(port),
        ]

    def add_observer(self, observer: InvocationObserver) -> None:
        self._observers.append(observer)

    def remove_observer(self, observer: InvocationObserver) -> None:
        try:
            self._observers.remove(observer)
        except ValueError:
            pass

    def start(self) -> None:
        """Start the launcher/OpenCode process.

        On any startup failure — including a direct `BaseException` such
        as `KeyboardInterrupt`/`SystemExit` — best-effort cleanup (`stop()`)
        is attempted and, if it too fails, its failure is attached as a
        note; the *original* startup exception is then propagated via a
        bare `raise` from the single outermost `except` clause below, so
        its exact identity and traceback (not a redispatched copy) is
        what the caller observes. This mirrors the same primary-error
        precedence documented on `_close_request_local_client` (see ADR
        0009): a cleanup failure is never allowed to replace the primary
        outcome, and re-raising the primary is never done by naming it in
        a `raise <expr>` statement (which would add this method's own
        frame to its traceback) — only a bare `raise` inside the `except`
        block that is still actively handling it.
        """
        with self._cleanup_lock:
            if any(
                resource is not None
                for resource in (
                    self._owner,
                    self._pending_launcher,
                    self._client,
                    self._client_close_attempt,
                    self._stdout_thread,
                )
            ):
                raise OpenCodeError(
                    "server already started (or prior lifecycle resources are still unresolved)"
                )

            self._stdout_stop.clear()
            self._stdout_ready_event.clear()
            self._stdout_eof_event.clear()
            self._stdout_lines.clear()
            with self._stdout_partial_lock:
                self._stdout_partial = ""
            self.base_url = None

            port = self.config.port if self.config.port is not None else _free_port()
            env = dict(self.config.env if self.config.env is not None else os.environ)
            event_read_fd, event_write_fd = os.pipe()
            command_read_fd, command_write_fd = os.pipe()
            try:
                launcher: subprocess.Popen[bytes] | None = None
                try:
                    launcher = subprocess.Popen(
                        [
                            sys.executable,
                            _LAUNCHER_SCRIPT,
                            str(event_write_fd),
                            str(command_read_fd),
                            *self._serve_command(port),
                        ],
                        cwd=str(self.project_dir),
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL,
                        start_new_session=True,
                        pass_fds=(event_write_fd, command_read_fd),
                    )
                    self._pending_launcher = launcher
                except BaseException as exc:
                    if launcher is not None:
                        self._pending_launcher = launcher
                    if isinstance(exc, OSError):
                        # Render `exc` via _safe_exception_text() rather than
                        # an f-string's implicit str() call: an OSError
                        # subclass with a throwing __str__ must never let
                        # that failure escape in place of ServerStartupError,
                        # which would also lose the exact __cause__ identity
                        # established below (see ADR 0009's primary-error
                        # precedence, and the analogous fix already applied
                        # to runtime.py's diagnostic rendering).
                        wrapped = ServerStartupError(
                            f"failed to start launcher for {self.config.executable!r}: "
                            f"{_safe_exception_text(exc)}"
                        )
                        wrapped.__cause__ = exc
                        raise wrapped from exc
                    raise
                finally:
                    for fd in (event_write_fd, command_read_fd):
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                    if launcher is None:
                        for fd in (event_read_fd, command_write_fd):
                            try:
                                os.close(fd)
                            except OSError:
                                pass

                assert launcher is not None
                try:
                    line = self._read_launcher_event(
                        launcher, event_read_fd, _LAUNCHER_READY_TIMEOUT_SECONDS
                    )
                    owner = self._parse_anchor_identity(
                        launcher, command_write_fd, event_read_fd, line
                    )
                    self._owner = owner
                    self._pending_launcher = None
                    owner.send("start")
                    child_event = self._read_launcher_event(
                        launcher, event_read_fd, _LAUNCHER_READY_TIMEOUT_SECONDS
                    )
                    if child_event.startswith("start-error:"):
                        raise ServerStartupError(
                            f"launcher failed to start OpenCode: {child_event[12:]}"
                        )
                    if not child_event.startswith("child-ready:"):
                        raise ServerStartupError(
                            f"launcher sent unexpected child event: {child_event!r}"
                        )
                    self._start_stdout_pump(launcher)
                    self._await_ready(launcher)
                except BaseException:
                    if self._owner is None:
                        for fd in (event_read_fd, command_write_fd):
                            try:
                                os.close(fd)
                            except OSError:
                                pass
                    raise
            except BaseException as primary:  # noqa: BLE001 - primary must propagate unchanged
                try:
                    self.stop()
                except BaseException as cleanup_error:  # noqa: BLE001 - must never replace primary
                    _add_cleanup_note(
                        primary,
                        "additionally, startup cleanup failed: ",
                        cleanup_error,
                    )
                raise

    def _read_launcher_event(
        self,
        launcher: subprocess.Popen[bytes],
        event_fd: int,
        timeout: float,
    ) -> str:
        deadline = time.monotonic() + timeout
        partial = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ServerStartupError("launcher event timed out")
            ready, _, _ = select.select([event_fd], [], [], min(0.1, remaining))
            if ready:
                chunk = os.read(event_fd, 256)
                if not chunk:
                    raise ServerStartupError("launcher event pipe closed")
                partial += chunk
                if b"\n" in partial:
                    raw, _ = partial.split(b"\n", 1)
                    return raw.decode(errors="replace").strip()
            if launcher.poll() is not None:
                raise ServerStartupError(
                    f"launcher exited before expected event (code {launcher.returncode})"
                )

    def _parse_anchor_identity(
        self,
        launcher: subprocess.Popen[bytes],
        command_fd: int,
        event_fd: int,
        line: str,
    ) -> _ProcessOwner:
        if not line.startswith("anchor-ready:"):
            raise ServerStartupError(f"launcher sent unexpected anchor event: {line!r}")
        parts = line[13:].split(":")
        if len(parts) != 2:
            raise ServerStartupError(f"launcher anchor event malformed: {line!r}")
        try:
            pid, pgid = (int(value) for value in parts)
        except ValueError as exc:
            raise ServerStartupError(f"launcher anchor event malformed: {line!r}") from exc
        if pid != launcher.pid or pgid != pid:
            raise ServerStartupError(f"launcher identity mismatch: {line!r}")
        try:
            actual_pgid = os.getpgid(pid)
        except OSError as exc:
            raise ServerStartupError(
                f"could not verify launcher identity: {_safe_exception_text(exc)}"
            ) from exc
        if actual_pgid != pgid:
            raise ServerStartupError(f"launcher pgid mismatch: expected {pgid}, got {actual_pgid}")
        return _ProcessOwner(
            launcher=launcher,
            pgid=pgid,
            command_fd=command_fd,
            event_fd=event_fd,
        )

    def _start_stdout_pump(self, process: subprocess.Popen[bytes]) -> None:
        """Start the single thread that ever reads process.stdout.

        Both readiness detection and post-readiness draining are handled
        by this one pump, so there is never a second reader racing it for
        lines. Reading is done with raw, non-blocking-after-select
        `os.read()` on the underlying file descriptor and reassembled into
        lines here, rather than via the buffered `TextIOWrapper`'s
        `readline()`: a partial (non-newline-terminated) write would leave
        `readline()` blocked waiting for more bytes or EOF, which — if
        called from the thread that is also supposed to enforce the
        startup deadline — would silently bypass that deadline entirely.
        By recognizing the ready line here, in the same thread that reads
        every byte, `_stdout_ready_event` can be set the instant it
        appears, and `_await_ready()` never has to touch the pipe itself.
        """
        assert process.stdout is not None
        fileno = process.stdout.fileno()

        def _pump() -> None:
            partial = b""
            # True while the in-progress (not yet newline-terminated)
            # line has exceeded _MAX_STDOUT_FRAGMENT_BYTES and its bytes
            # have been dropped. The *next* newline seen still belongs to
            # that same oversized line (it is what finally terminates
            # it), so the first line produced by the next split must be
            # discarded rather than recorded -- it is only the tail of
            # an already-abandoned line, not a real one. Every line after
            # that first discarded one is unaffected and recorded
            # normally.
            oversized = False
            while not self._stdout_stop.is_set():
                try:
                    ready, _, _ = select.select([fileno], [], [], 0.1)
                except (OSError, ValueError):
                    break
                if not ready:
                    continue
                try:
                    chunk = os.read(fileno, 4096)
                except OSError:
                    break
                if not chunk:
                    if partial and not oversized:
                        self._record_line(partial.decode(errors="replace").rstrip("\r"))
                    self._stdout_eof_event.set()
                    break
                partial += chunk
                if b"\n" not in partial:
                    if len(partial) > _MAX_STDOUT_FRAGMENT_BYTES:
                        # Drop the accumulated bytes entirely rather than
                        # retaining a truncated slice: the point is to
                        # bound memory for a line that may never
                        # terminate, not to preserve a sample of it.
                        partial = b""
                        oversized = True
                    with self._stdout_partial_lock:
                        self._stdout_partial = "" if oversized else partial.decode(errors="replace")
                    continue
                lines = partial.split(b"\n")
                partial = lines.pop()
                with self._stdout_partial_lock:
                    self._stdout_partial = partial.decode(errors="replace")
                if oversized:
                    lines = lines[1:]
                    oversized = False
                for raw_line in lines:
                    self._record_line(raw_line.decode(errors="replace").rstrip("\r"))

        t = threading.Thread(target=_pump, name="opencode-stdout-pump", daemon=True)
        t.start()
        self._stdout_thread = t

    def _record_line(self, line: str) -> None:
        self._stdout_lines.append(line)
        if not self._stdout_ready_event.is_set() and _READY_RE.search(line):
            match = _READY_RE.search(line)
            assert match is not None
            self.base_url = match.group(1)
            self._stdout_ready_event.set()

    def _await_ready(self, launcher: subprocess.Popen[bytes]) -> None:
        deadline = time.monotonic() + self.config.startup_timeout
        while True:
            remaining = deadline - time.monotonic()
            if self._stdout_ready_event.wait(timeout=max(0.0, min(0.1, remaining))):
                assert self.base_url is not None
                self._client = httpx.Client(base_url=self.base_url, timeout=None)
                return
            owner = self._owner
            if owner is not None:
                event = owner.read_event(0.0)
                if event is not None and event.startswith("child-exit:"):
                    raise ServerStartupError(
                        f"opencode serve exited early ({event}): " + self._diagnostic_output()
                    )
            if launcher.poll() is not None:
                raise ServerStartupError(
                    f"launcher exited early (code {launcher.returncode}): "
                    + self._diagnostic_output()
                )
            if time.monotonic() >= deadline:
                raise ServerStartupError(
                    f"opencode serve did not become ready within "
                    f"{self.config.startup_timeout}s: " + self._diagnostic_output()
                )

    def _diagnostic_output(self) -> str:
        """Complete lines plus any trailing incomplete (non-newline-terminated)
        fragment, for inclusion in a ServerStartupError message."""
        with self._stdout_partial_lock:
            partial = self._stdout_partial
        lines = list(self._stdout_lines)
        if partial:
            lines.append(partial)
        return "\n".join(lines)

    def stop(self) -> None:
        with self._cleanup_lock:
            errors: list[Exception] = []
            self._stdout_stop.set()

            if self._client is None and self._client_close_attempt is not None:
                errors.append(
                    OpenCodeCleanupError(
                        "internal error: client-close attempt exists without a tracked client"
                    )
                )
            elif self._client is not None:
                attempt = self._client_close_attempt
                if attempt is not None and attempt.client is not self._client:
                    # An in-progress attempt exists but is bound to a
                    # different client object than the one currently
                    # tracked: this should not happen under the cleanup
                    # lock, and it must never be papered over by
                    # overwriting either ownership field (which could
                    # orphan the real in-flight close or start a second
                    # concurrent close() against _client). Report it and
                    # leave both fields exactly as they are; do not
                    # attempt any close this round.
                    errors.append(
                        OpenCodeCleanupError(
                            "internal error: in-progress client-close attempt does not "
                            "match the currently tracked client"
                        )
                    )
                else:
                    if attempt is None:
                        started = _start_bounded_close(self._client)
                        if isinstance(started, BaseException):
                            # Construction/start failed before any worker
                            # began running close() at all: _client is
                            # retained (nothing was closed), no attempt
                            # is installed, and a later stop() may retry.
                            errors.append(_as_reportable_error(started))
                            attempt = None
                        else:
                            attempt = started
                            self._client_close_attempt = attempt

                    if attempt is not None:
                        # Either a freshly started attempt or a
                        # pre-existing attempt for the same client:
                        # re-wait rather than starting a second
                        # concurrent close() call.
                        try:
                            finished = attempt.wait(_CLIENT_CLOSE_TIMEOUT_SECONDS)
                            if finished:
                                startup_error = attempt.startup_error
                                close_error = attempt.error
                            else:
                                startup_error = None
                                close_error = None
                        except BaseException as exc:  # noqa: BLE001 - continue all cleanup stages
                            errors.append(_as_reportable_error(exc))
                        else:
                            if not finished:
                                # Timeout: retain both client and attempt
                                # so a retried stop() re-waits on the
                                # same in-flight close rather than
                                # starting a new one.
                                errors.append(
                                    OpenCodeCleanupError(
                                        "closing the OpenCode control client timed out"
                                    )
                                )
                            elif close_error is not None or startup_error is not None:
                                # Completed with an exception: retain the
                                # client, but clear the completed attempt
                                # so a later stop() may start one retry
                                # close().
                                error = close_error if close_error is not None else startup_error
                                assert error is not None
                                errors.append(_as_reportable_error(error))
                                self._client_close_attempt = None
                            else:
                                # Successful completion: clear both.
                                self._client = None
                                self._client_close_attempt = None

            pending = self._pending_launcher
            if pending is not None:
                try:
                    running = pending.poll() is None
                except Exception as exc:
                    errors.append(exc)
                    running = True
                if running:
                    try:
                        pending.kill()
                    except Exception as exc:
                        errors.append(exc)
                try:
                    pending.wait(timeout=_GROUP_KILL_WAIT_SECONDS)
                except Exception as exc:
                    errors.append(exc)
                try:
                    if pending.stdout is not None:
                        pending.stdout.close()
                except Exception as exc:
                    errors.append(exc)
                try:
                    pending_reaped = pending.poll() is not None
                except Exception as exc:
                    errors.append(exc)
                    pending_reaped = False
                pending_stdout_closed = pending.stdout is None or pending.stdout.closed
                if pending_reaped and pending_stdout_closed:
                    self._pending_launcher = None
                else:
                    errors.append(
                        OpenCodeCleanupError("unverified launcher cleanup remains incomplete")
                    )

            owner = self._owner
            if owner is not None:
                try:
                    launcher_running = owner.launcher.poll() is None
                except Exception as exc:
                    errors.append(exc)
                    launcher_running = True
                if launcher_running:
                    if not owner.term_sent:
                        try:
                            owner.send("term")
                            term_result = owner.wait_for_acknowledgement(
                                "term-", _GROUP_POLL_INTERVAL_SECONDS * 4
                            )
                        except Exception as exc:
                            errors.append(exc)
                        else:
                            if term_result == "term-ok":
                                owner.term_sent = True
                            else:
                                errors.append(
                                    OpenCodeCleanupError(
                                        f"launcher did not acknowledge SIGTERM: {term_result!r}"
                                    )
                                )
                    if owner.term_sent:
                        try:
                            owner.wait_for_child_exit(_GROUP_TERM_WAIT_SECONDS)
                        except Exception as exc:
                            errors.append(exc)
                    try:
                        launcher_running = owner.launcher.poll() is None
                    except Exception as exc:
                        errors.append(exc)
                        launcher_running = True
                    if owner.term_sent and launcher_running and not owner.kill_sent:
                        try:
                            owner.send("kill")
                            kill_result = owner.wait_for_acknowledgement(
                                "kill-", _GROUP_POLL_INTERVAL_SECONDS * 4
                            )
                        except Exception as exc:
                            errors.append(exc)
                        else:
                            if kill_result is None:
                                owner.kill_sent = True
                            else:
                                errors.append(
                                    OpenCodeCleanupError(
                                        f"launcher failed to deliver SIGKILL: {kill_result}"
                                    )
                                )
                    try:
                        owner.wait_launcher(_GROUP_KILL_WAIT_SECONDS)
                    except Exception as exc:
                        errors.append(exc)

                confirmed = False
                try:
                    confirmed = owner.shutdown_confirmed()
                except Exception as exc:
                    errors.append(exc)

                pipes_closed = owner.command_fd < 0 and owner.event_fd < 0
                if confirmed and not pipes_closed:
                    try:
                        owner.close_pipes()
                    except Exception as exc:
                        errors.append(exc)
                    pipes_closed = owner.command_fd < 0 and owner.event_fd < 0

                stdout = owner.launcher.stdout
                stdout_closed = stdout is None or stdout.closed
                if confirmed and stdout is not None and not stdout_closed:
                    try:
                        stdout.close()
                    except Exception as exc:
                        errors.append(exc)
                    stdout_closed = stdout.closed

                if confirmed and pipes_closed and stdout_closed:
                    owner.state = "reaped"
                    self._owner = None
                else:
                    owner.state = "unresolved"
                    errors.append(
                        OpenCodeCleanupError(
                            "OpenCode process-group cleanup is incomplete; retaining ownership"
                        )
                    )

            thread = self._stdout_thread
            if thread is not None:
                try:
                    thread.join(timeout=2.0)
                except Exception as exc:
                    errors.append(exc)
                try:
                    alive = thread.is_alive()
                except Exception as exc:
                    errors.append(exc)
                    alive = True
                if alive:
                    errors.append(OpenCodeCleanupError("OpenCode stdout pump thread remains alive"))
                else:
                    self._stdout_thread = None

            if errors:
                if len(errors) == 1:
                    raise OpenCodeCleanupError("error during OpenCode server cleanup") from errors[
                        0
                    ]
                raise ExceptionGroup("errors during OpenCode server cleanup", errors)

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            raise OpenCodeError("server is not started")
        return self._client

    def health(self) -> dict[str, Any]:
        response = self.client.get("/global/health")
        _raise_for_status(response, "health check")
        return _decode_json_object(response, "health check")

    def create_session(
        self,
        directory: Path,
        *,
        title: str,
        timeout: float | None = None,
        deadline: float | None = None,
    ) -> str:
        """Create a session, bounded by `timeout` (falls back to the
        long-lived control client's own timeout if not given).

        Uses a request-local client when `timeout` is given rather than
        the shared long-lived control client, so a hung `/session` request
        can be independently bounded per invocation instead of relying on
        the control client's timeout=None default. When `deadline` is given,
        it additionally enforces that absolute monotonic deadline even if a
        response trickles bytes frequently enough to avoid httpx's inactivity
        timeout. Every ordinary
        transport failure is translated to PhaseTimeoutError or
        AgentInvocationError, matching send_prompt(), rather than
        propagating a raw httpx exception.
        """
        if timeout is not None:
            if self.base_url is None:
                raise OpenCodeError("server is not started")
            client = httpx.Client(base_url=self.base_url, timeout=timeout)
            close_client = True
        else:
            client = self.client
            close_client = False

        # The complete request outcome (transport, HTTP status, JSON
        # decoding, and session-ID validation) is decided in full before
        # any close() is attempted: a close() failure/timeout must never
        # replace a primary outcome that was only partially decided (see
        # _close_request_local_client's precedence convention). Every
        # branch here — the translated transport error, an HTTP-status
        # error, a decode error, or a missing/invalid ID — is `raise`d so
        # it becomes `primary` in the `except BaseException` clause below,
        # not held in an intermediate variable.
        try:
            try:

                def request() -> httpx.Response:
                    return client.post(
                        "/session",
                        params={"directory": str(directory)},
                        json={"title": title},
                    )

                if deadline is None:
                    response = request()
                else:
                    response = _run_with_deadline(
                        request,
                        deadline=deadline,
                        timeout_error=PhaseTimeoutError(
                            f"creating session for {title!r} exceeded its absolute deadline"
                        ),
                    )
            except httpx.TimeoutException as exc:
                raise PhaseTimeoutError(
                    f"creating session for {title!r} did not respond within "
                    f"{timeout if timeout is not None else 'the configured'}s"
                ) from exc
            except httpx.RequestError as exc:
                raise AgentInvocationError(
                    f"network error creating session for {title!r}: {_safe_exception_text(exc)}"
                ) from exc

            _raise_for_status(response, "create session")
            data = _decode_json_object(response, "create session")
            session_id = data.get("id")
            if not isinstance(session_id, str) or not session_id:
                raise AgentInvocationError(
                    f"create session response is missing a valid string 'id' field: {data!r}"[:500]
                )
        except BaseException as primary:
            if close_client:
                _close_request_local_client(client, primary)
            raise
        else:
            if close_client:
                _close_request_local_client(client, None)
            return session_id

    def send_prompt(
        self,
        *,
        session_id: str,
        directory: Path,
        agent: str,
        prompt: str,
        json_schema: dict[str, Any] | None = None,
        timeout: float = 1800.0,
        deadline: float | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "agent": agent,
            "parts": [{"type": "text", "text": prompt}],
        }
        if json_schema is not None:
            body["format"] = {"type": "json_schema", "schema": json_schema}

        if self.base_url is None:
            raise OpenCodeError("server is not started")

        client = httpx.Client(base_url=self.base_url, timeout=timeout)

        # See create_session(): the complete request outcome (transport,
        # HTTP status, JSON decoding, and text/structured-output
        # extraction, including assistant-error and response-shape
        # failures) is decided in full before any close() is attempted,
        # so a subsequent close() failure/timeout can never replace it
        # (notably PhaseTimeoutError, which run_agent() relies on to
        # trigger a best-effort abort — see _abort_session_best_effort).
        try:
            try:

                def request() -> httpx.Response:
                    return client.post(
                        f"/session/{session_id}/message",
                        params={"directory": str(directory)},
                        json=body,
                    )

                if deadline is None:
                    response = request()
                else:
                    response = _run_with_deadline(
                        request,
                        deadline=deadline,
                        timeout_error=PhaseTimeoutError(
                            f"agent {agent!r} exceeded its absolute deadline"
                        ),
                    )
            except httpx.TimeoutException as exc:
                raise PhaseTimeoutError(
                    f"agent {agent!r} did not respond within {timeout}s"
                ) from exc
            except httpx.RequestError as exc:
                raise AgentInvocationError(
                    f"network error communicating with agent {agent!r}: {_safe_exception_text(exc)}"
                ) from exc

            _raise_for_status(response, f"prompt for agent {agent!r}")
            data = _decode_json_object(response, f"prompt for agent {agent!r}")
            text = _extract_text(data, agent=agent)
        except BaseException as primary:
            _close_request_local_client(client, primary)
            raise
        else:
            _close_request_local_client(client, None)
            return text

    def abort_session(self, session_id: str) -> None:
        """Abort one session, bounded by _ABORT_TIMEOUT_SECONDS.

        Uses a fresh, short-lived client rather than the shared long-lived
        control client (which defaults to timeout=None): a hung abort
        request must never block whatever is waiting on this call (a
        caller recovering from PhaseTimeoutError, or shutdown tearing down
        active sessions) beyond a small, finite bound.
        """
        if self.base_url is None:
            raise OpenCodeError("server is not started")
        self._abort_session_bounded(session_id)

    def _abort_session_bounded(self, session_id: str) -> None:
        """Shared bounded-abort implementation used by both
        abort_session() and abort_active_sessions().

        Every ordinary transport failure (timeout, network error) and a
        non-2xx HTTP status are treated uniformly here: the caller is
        always a best-effort cleanup path (_abort_session_best_effort or
        abort_active_sessions' own per-session try/except), so raising a
        normalized OpenCodeError on any failure is sufficient — no caller
        in this module is meant to treat an abort failure as fatal.
        """
        assert self.base_url is not None
        client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(_ABORT_TIMEOUT_SECONDS),
        )

        # Same precedence convention as create_session()/send_prompt(): a
        # bounded close() failure/timeout must never replace the abort
        # request's complete own outcome (transport failure or non-2xx
        # HTTP status), and every caller here (both abort_session() and
        # abort_active_sessions()) already treats this method's
        # exceptions as best-effort/non-fatal.
        try:
            try:
                response = client.post(f"/session/{session_id}/abort")
            except httpx.TimeoutException as exc:
                raise OpenCodeError(
                    f"aborting session {session_id!r} did not respond within "
                    f"{_ABORT_TIMEOUT_SECONDS}s"
                ) from exc
            except httpx.RequestError as exc:
                raise OpenCodeError(
                    f"network error aborting session {session_id!r}: {_safe_exception_text(exc)}"
                ) from exc

            _raise_for_status(response, f"abort session {session_id!r}")
        except BaseException as primary:
            _close_request_local_client(client, primary)
            raise
        else:
            _close_request_local_client(client, None)

    def abort_active_sessions(self) -> None:
        """Best-effort abort of all currently registered active sessions,
        bounded per-session by _ABORT_TIMEOUT_SECONDS.

        Uses a fresh short-lived client per session (never the shared
        long-lived control client) so one hung or failing session's abort
        can never delay or prevent the others from being attempted, nor
        delay whatever is waiting on this call to return (e.g. shutdown
        proceeding to stop the server).
        """
        if self.base_url is None:
            return
        with self._active_sessions_lock:
            session_ids = list(self._active_sessions.keys())
        for session_id in session_ids:
            try:
                self._abort_session_bounded(session_id)
            except Exception:
                pass

    def active_invocations(self) -> list[InvocationRef]:
        with self._active_sessions_lock:
            return list(self._active_sessions.values())

    def reconcile_invocation(
        self, ref: InvocationRef
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Fetch best-effort live state for one exact active invocation.

        Both requests carry the invocation's registered directory and the
        message request carries its exact session ID.  The caller remains
        responsible for treating this telemetry as non-authoritative.
        """
        status_response = self.client.get(
            "/session/status", params={"directory": str(ref.directory)}
        )
        _raise_for_status(status_response, f"reconcile status for session {ref.session_id!r}")
        statuses = _decode_json_object(
            status_response, f"reconcile status for session {ref.session_id!r}"
        )
        status = statuses.get(ref.session_id)
        if not isinstance(status, dict):
            raise OpenCodeError(
                f"status reconciliation response omitted active session {ref.session_id!r}"
            )

        messages_response = self.client.get(
            f"/session/{ref.session_id}/message",
            params={"directory": str(ref.directory)},
        )
        _raise_for_status(messages_response, f"reconcile messages for session {ref.session_id!r}")
        try:
            messages = messages_response.json()
        except (ValueError, TypeError) as exc:
            raise OpenCodeError(
                f"invalid JSON reconciling messages for session {ref.session_id!r}"
            ) from exc
        if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
            raise OpenCodeError(
                f"message reconciliation response for session {ref.session_id!r} is not a list"
            )
        return status, messages

    def run_agent(
        self,
        *,
        agent: str,
        directory: Path,
        prompt: str,
        json_schema: dict[str, Any] | None = None,
        timeout: float = 1800.0,
    ) -> str:
        """Run one role invocation, bounded end-to-end by `timeout`.

        `timeout` covers session creation *and* the prompt response, not
        just the prompt: a hung `/session` request would otherwise never
        be bounded by role_timeout at all, since create_session() used to
        run on the long-lived control client with timeout=None. Session
        creation is charged against the same deadline as the prompt, and
        the prompt receives whatever budget remains. Both requests also
        run behind the same absolute monotonic deadline, independent of
        httpx's per-operation inactivity timeout semantics.
        """
        deadline = time.monotonic() + timeout
        session_id = self.create_session(
            directory,
            title=f"loop:{agent}",
            timeout=timeout,
            deadline=deadline,
        )

        ref = InvocationRef(
            session_id=session_id,
            agent=agent,
            directory=directory,
            started_monotonic=time.monotonic(),
        )
        with self._active_sessions_lock:
            self._active_sessions[session_id] = ref

        self._notify_started(ref)
        error: BaseException | None = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PhaseTimeoutError(
                    f"agent {agent!r} timed out during session creation before any "
                    "prompt could be sent"
                )
            return self.send_prompt(
                session_id=session_id,
                directory=directory,
                agent=agent,
                prompt=prompt,
                json_schema=json_schema,
                timeout=remaining,
                deadline=deadline,
            )
        except PhaseTimeoutError as exc:
            error = exc
            self._abort_session_best_effort(session_id)
            raise
        except BaseException as exc:
            error = exc
            raise
        finally:
            with self._active_sessions_lock:
                self._active_sessions.pop(session_id, None)
            self._notify_finished(ref, error)

    def _abort_session_best_effort(self, session_id: str) -> None:
        """Attempt to abort a session after a timeout without letting an
        abort failure replace the timeout the caller is already handling.
        The timeout is the primary, already-decided outcome; this cleanup
        is diagnostic best-effort only."""
        try:
            self.abort_session(session_id)
        except Exception:
            pass

    def _notify_started(self, ref: InvocationRef) -> None:
        for observer in list(self._observers):
            try:
                observer.invocation_started(ref)
            except Exception:
                pass

    def _notify_finished(self, ref: InvocationRef, error: BaseException | None) -> None:
        for observer in list(self._observers):
            try:
                observer.invocation_finished(ref, error)
            except Exception:
                pass


def _raise_for_status(response: httpx.Response, action: str) -> None:
    if response.status_code >= 400:
        raise AgentInvocationError(
            f"{action} failed with HTTP {response.status_code}: {response.text[:500]}"
        )


def _decode_json_object(response: httpx.Response, action: str) -> dict[str, Any]:
    """Decode a response body as a JSON object, converting every failure
    mode (invalid JSON, a top-level array/scalar/null) into
    AgentInvocationError rather than letting json.JSONDecodeError,
    AttributeError, or similar propagate unclassified. Response bodies are
    truncated in the error message to avoid leaking arbitrarily large or
    sensitive content."""
    try:
        data = response.json()
    except ValueError as exc:
        raise AgentInvocationError(
            f"{action}: response body is not valid JSON: {response.text[:500]!r}"
        ) from exc
    if not isinstance(data, dict):
        raise AgentInvocationError(
            f"{action}: response body is not a JSON object (got {type(data).__name__}): "
            f"{str(data)[:500]!r}"
        )
    return data


def _extract_text(data: dict[str, Any], *, agent: str) -> str:
    info = data.get("info", {})
    if not isinstance(info, dict):
        raise AgentInvocationError(
            f"agent {agent!r} response has a non-object 'info' field: {info!r}"[:500]
        )
    error = info.get("error")
    if error:
        raise AgentInvocationError(f"agent {agent!r} returned an error: {error}"[:500])

    structured = info.get("structured")
    if structured is None:
        structured = info.get("structured_output")
    if structured is not None:
        import json as _json

        try:
            return _json.dumps(structured)
        except (TypeError, ValueError) as exc:
            raise AgentInvocationError(
                f"agent {agent!r} returned malformed structured output: {_safe_exception_text(exc)}"
            ) from exc

    parts = data.get("parts", [])
    if not isinstance(parts, list):
        raise AgentInvocationError(
            f"agent {agent!r} response has a non-list 'parts' field: {type(parts).__name__}"
        )
    text_parts = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            text = part.get("text", "")
            text_parts.append(text if isinstance(text, str) else "")
    if not text_parts:
        raise AgentInvocationError(f"agent {agent!r} returned no text output")
    return "\n".join(text_parts).strip()
