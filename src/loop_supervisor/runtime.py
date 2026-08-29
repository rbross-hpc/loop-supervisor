"""Shared application-level controller for CLI and TUI.

``RunSession`` is the primary interface: a context manager owning the whole
lifecycle of one run or resume. The factories ``new_run_session()`` and
``resume_run_session()`` return an **inert** object (no lock, no state, no
server); every resource is acquired inside ``__enter__``::

    with new_run_session(project_root, options) as session:
        session.start_server()
        return session.run_to_completion()

``run_new()`` / ``run_resume()`` are thin wrappers over exactly that shape,
kept for the headless CLI.

Acquisition ordering:

1. Resolve repository / common directory.
2. Validate the run ID (resume only) *before* acquiring the lock, so a
   crafted ID can never be written into the lock record.
3. Acquire lock.
4. Create / load / validate state inside the lock.
5. Construct ``OpenCodeServer`` and install any observer (no process yet).
6. Mark the lease unreleasable, then ``server.start()``.
7. Hand the started server to the supervisor (runner handoff).
8. Run advance() / run().
9. Confirm ``server.stop()``, then release the lock.

For a new run, state is saved *before* OpenCode starts so that a server
startup failure can be recorded against a real run ID. For resume, Git
validation happens *before* OpenCode starts, so a mismatched or tampered
run fails closed with no side effects.

Lock-vs-OpenCode-cleanup ordering: the repository lock is only ever
released once OpenCode server cleanup has been *confirmed* successful.
An unconfirmed ``OpenCodeServer.stop()`` (a process that may still be
alive, still writing to the working tree) must never be followed by
releasing the lock, since a successor process could then mutate the same
repository concurrently with a surviving child. When cleanup cannot be
confirmed the session enters ``CLEANUP_UNRESOLVED`` and the lock is
retained on disk; ``close()`` may be called again to retry, and only a
confirmed stop() releases the lock. A primary run/resume/startup failure
always takes precedence in what is raised; a secondary cleanup failure is
attached as a note rather than replacing it.

**``close()`` owns the lock and the cleanup outcome.** It is the only
place that releases the lock and the only place that decides what an
unconfirmed stop() means for the lease. The startup helpers
(``_startup_failure``, ``_finalize_interrupted_startup``) only persist
and annotate; they never touch the lease. Splitting *that* ownership
previously caused a confirmed-clean interrupt to leak its lock.

One qualification, because the earlier wording overstated this:
``start_server()`` does invoke ``_confirm_cleanup()`` directly on the
ordinary-``Exception`` startup path, since ``_startup_failure`` must know
whether the lock was retained in order to word its diagnostic. Likewise,
``stop_server()`` lets a caller force a bounded stop() attempt ahead of
``close()`` — e.g. to break a blocked in-flight ``advance()`` call by
tearing down its HTTP transport, without releasing the lock. In both
cases the bounded ``_CLEANUP_ATTEMPTS`` budget is spent exactly once per
failure sequence: the outcome is stashed in ``_pending_cleanup`` and
consumed by the next ``close()``. So stop() confirmation is *initiated*
in three places (``start_server()``, ``stop_server()``, and ``close()``
itself when nothing pending exists); the retry budget and all lease/lock
decisions remain single-owner in ``close()``.

``__exit__`` always calls ``close()``. When a body exception is already
propagating, any failure from ``close()`` is converted into a note on that
exception — never allowed to escape and replace it (an exception raised
from ``__exit__`` *does* replace the primary, and would also add an
``__exit__`` frame). With no body exception, ``close()`` raises normally.

Startup-exception traceback guarantee: a direct ``BaseException``
(``KeyboardInterrupt``/``SystemExit``) from ``server.start()`` propagates
with its exact identity and traceback. ``__exit__`` and ``close()``
contribute no frames — cleanup work performed inside ``__exit__`` is
invisible to the traceback as long as it does not itself raise.

Thread-safety: ``RunSession`` may be driven from more than one thread,
as the Textual TUI does (an ``advance()`` worker thread plus a shutdown
worker thread; see ADR 0008). Two separate primitives are used, and the
distinction is load-bearing:

``_state_lock`` (an ``RLock``)
    Guards the mutable bookkeeping — ``_state``, ``_pending_cleanup``,
    ``_annotated``, and the lease decisions — and serializes the cleanup
    paths (``close()`` and ``stop_server()``) against each other.
    Re-entrant because ``__exit__`` → ``close()`` re-enters legitimately.

    It is deliberately **never** held across ``supervisor.advance()``:
    doing so would deadlock the very scenario ``stop_server()`` exists
    for, since a shutdown thread calling ``stop_server()`` to unblock a
    stuck ``advance()`` would instead block behind that same
    ``advance()``.

    It *is* held across ``server.stop()``, and that is required rather
    than incidental. ``OpenCodeServer.stop()`` takes the server's own
    internal cleanup lock, so both cleanup paths must acquire these two
    locks in the same order (this one first, the server's second).
    Releasing ours around ``stop()`` would let ``close()`` and
    ``stop_server()`` acquire them in opposite orders — a textbook ABBA
    deadlock.

``_advance_done`` (an ``Event``)
    A quiescence barrier, not a mutex. ``advance()`` clears it on entry
    and sets it in a ``finally``; ``close()`` waits on it before
    touching the lease, so the repository lock is never released while
    a transition may still be mutating Git/state. The wait is
    deliberately **unbounded**: there is no safe bound (that hazard is
    precisely what ADR 0009 exists to prevent) and no Python thread may
    be force-killed. ``stop_server()`` is the escape hatch for a
    transition that would otherwise block for its full role timeout.

Concurrency contract: ``close()``, ``stop_server()``, and
``abort_active_invocations()`` are safe to call from another thread
while ``advance()``/``run_to_completion()`` is in flight. ``close()``
will block until that call finishes. Concurrent ``advance()`` calls are
*not* supported — the state guard rejects the second one, as it does
single-threaded.

Known limitation: ``_confirm_server_stopped``'s backoff uses
``time.sleep()``, which is not interrupt-safe. This module therefore does
not claim complete primary-error precedence in every interrupt scenario.
"""

from __future__ import annotations

import inspect
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, NoReturn

from .git import GitError, GitRepo
from .input_providers import StdinInputProvider
from .locking import LockError, SupervisorLock
from .opencode import (
    InvocationObserver,
    OpenCodeServer,
    OpenCodeServerConfig,
    build_agent_env,
)
from .permissions import PermissionDenier
from .sse import SSECleanupError
from .state import RunOptions, RunState, StateError, list_runs, load_state, validate_run_id
from .supervisor import AdvanceOutcome, InputProvider, LoopError, Supervisor

# Bounded retry for confirming OpenCode server cleanup (server.stop()).
# Applied uniformly to startup failures, successful-run completion, and
# any BaseException raised from supervisor.run()/the runner handoff — the
# lease may only be marked releasable once one of these attempts actually
# confirms stop() succeeded. Backoff increases per attempt (0.1s, 0.2s)
# and is never applied after the final attempt.
_CLEANUP_ATTEMPTS = 3
_CLEANUP_BACKOFF_SECONDS = 0.1


class RuntimeError_(RuntimeError):
    """Raised by the runtime controller for startup/configuration errors."""


def _safe_exception_text(error: BaseException | None) -> str:
    """Render `error` for inclusion in a diagnostic message/note without
    ever raising itself, even if `error.__str__` raises (e.g. an
    exception subclass with a broken or adversarial `__str__`). Every
    diagnostic constructed in this module that interpolates an arbitrary
    exception — a startup primary, a cleanup/stop() failure, a
    persistence failure, or a lock-release failure — must render it
    through this helper rather than an f-string's implicit `str()`/
    `format()` call, so that a throwing `__str__` can never itself
    become an escaping exception that replaces (or masks) the actual
    primary outcome being reported (see ADR 0009's primary-error
    precedence). Mirrors `opencode._safe_exception_text`, with `None`
    additionally accepted (rendered as "None") since some callers here
    pass an optional `_CleanupOutcome.last_error`."""
    if error is None:
        return "None"
    try:
        return str(error)
    except BaseException:  # noqa: BLE001 - diagnostic rendering must not escape
        try:
            return f"unprintable {type(error).__name__}"
        except BaseException:  # noqa: BLE001 - use a constant if introspection fails
            return "unprintable error"


@dataclass
class _CleanupOutcome:
    """Structured result of a bounded server-stop retry sequence. Never
    raised itself; callers decide how to combine this with any primary
    exception they are already handling (see _confirm_server_stopped)."""

    confirmed: bool
    last_error: BaseException | None
    attempts: int


def _confirm_server_stopped(server: OpenCodeServer) -> _CleanupOutcome:
    """Retry server.stop() on the same OpenCodeServer instance up to
    _CLEANUP_ATTEMPTS times, with bounded backoff between attempts,
    returning structured success/failure information instead of raising.

    Cleanup is confirmed only once a stop() call returns without raising;
    a later successful attempt means cleanup is confirmed and earlier
    transient failures need not remain attached to whatever primary
    exception the caller is handling. The server handle itself is never
    discarded between retries — every attempt calls stop() again on the
    exact same instance, since OpenCodeServer.stop() is documented as
    safe to retry after a partial failure.

    A KeyboardInterrupt/SystemExit raised by stop() itself stops the
    retry loop immediately (retrying cleanup after an operator interrupt
    would ignore their request); it is reported via last_error like any
    other failure rather than being re-raised here, so it can never
    replace whatever primary exception the caller is already handling.
    """
    last_error: BaseException | None = None
    for attempt in range(_CLEANUP_ATTEMPTS):
        try:
            server.stop()
        except (KeyboardInterrupt, SystemExit) as exc:
            return _CleanupOutcome(confirmed=False, last_error=exc, attempts=attempt + 1)
        except BaseException as exc:  # noqa: BLE001 - reported, not raised; caller may retry
            last_error = exc
            if attempt < _CLEANUP_ATTEMPTS - 1:
                time.sleep(_CLEANUP_BACKOFF_SECONDS * (attempt + 1))
            continue
        else:
            return _CleanupOutcome(confirmed=True, last_error=None, attempts=attempt + 1)
    return _CleanupOutcome(confirmed=False, last_error=last_error, attempts=_CLEANUP_ATTEMPTS)


def _current_exception() -> BaseException | None:
    """Return the exception currently being handled, if any.

    Used by ``RunSession.close()`` to tell an ``__exit__``-driven call
    (where ``error`` is actively unwinding and will carry a note on its
    own) from an explicit ``close(error=...)`` made outside that unwind
    (where the caller learns the outcome only from what ``close()``
    raises). Getting that distinction wrong is how a failed retry ends up
    looking like a successful one.

    Must be read exactly once, at the top of ``close()``, before any
    nested ``try``/``except`` runs: cleanup helpers (``_confirm_cleanup()``
    and friends) raise and catch their own exceptions internally, and
    ``sys.exc_info()`` tracks whichever exception handler is innermost at
    the moment it is called -- so a second call made from inside one of
    those nested handlers would report *that* exception instead of the
    one ``close()`` was actually asked to report against.
    """
    return sys.exc_info()[1]


def _add_note(exc: BaseException, message: str) -> None:
    """Attach one deterministic note to `exc`, never letting a failure to
    annotate (or the underlying add_note() call itself) propagate: the
    caller's exception identity must be preserved regardless of whether
    annotating it succeeds."""
    try:
        exc.add_note(message)
    except BaseException:  # noqa: BLE001 - annotating must never replace the primary
        pass


def _cleanup_prefix(outcome: _RunOutcome, *, startup_interrupted: bool) -> str:
    """Choose the retained-lock diagnostic's opening clause.

    A pure function of the two facts that determine it: whether the
    run/resume succeeded, and (a special case of failure) whether the
    interruption happened during startup specifically. Kept separate
    from ``RunSession.close()`` so the mapping can be exercised directly,
    without constructing a session or triggering an unresolved cleanup.

    ``startup_interrupted`` takes precedence over ``outcome`` because a
    startup interrupt is itself a kind of failure -- the caller need not
    (and in practice does not) also pass ``_RunOutcome.FAILED`` for it.
    """
    if startup_interrupted:
        return "startup was interrupted; the"
    if outcome is _RunOutcome.FAILED:
        return "the run failed and the"
    return "run completed but"


def _unresolved_cleanup_message(prefix: str, outcome: _CleanupOutcome) -> str:
    """Render a retained-lock diagnostic. Never raises, even if
    `outcome.last_error` has a broken/adversarial `__str__`: it is
    rendered via `_safe_exception_text` rather than an f-string's
    implicit `str()` call, since this message is composed while a
    startup/run primary is already being handled (see _startup_failure,
    _run_and_stop) and must never itself escape and replace that
    primary."""
    return (
        f"{prefix} OpenCode server cleanup could not be confirmed "
        f"({_safe_exception_text(outcome.last_error)}) — the repository lock has been "
        "retained; verify no OpenCode process survives before using --recover-stale-lock"
    )


def _raise_unresolved_cleanup(prefix: str, outcome: _CleanupOutcome) -> NoReturn:
    """Raise for an otherwise-successful operation whose OpenCode cleanup
    could not be confirmed (no primary exception to preserve).

    If the last cleanup attempt failed with KeyboardInterrupt/SystemExit,
    that exact object is annotated with a retained-lock note and
    re-raised unchanged, rather than wrapped: an operator's interrupt
    must never be converted into an ordinary RuntimeError_. Any ordinary
    Exception cleanup failure is wrapped in RuntimeError_ as before.
    """
    last_error = outcome.last_error
    message = _unresolved_cleanup_message(prefix, outcome)
    if last_error is not None and not isinstance(last_error, Exception):
        _add_note(last_error, message)
        raise last_error
    raise RuntimeError_(message) from last_error


class _LockLease:
    """Wraps a SupervisorLock so release only happens once the caller has
    explicitly confirmed it is safe.

    Defaults to releasable: most of a run/resume's lifecycle (state
    creation/loading/validation) never touches OpenCode, so a failure
    there should release the lock normally. The lease is marked
    unreleasable immediately before the first point that may cause an
    OpenCode process to exist (``server.start()``), and marked releasable
    again only once a subsequent ``server.stop()`` has been confirmed to
    succeed. ``RunSession.close()`` consults ``releasable`` instead of
    always releasing, so an unconfirmed OpenCode cleanup can never be
    followed by releasing the lock.

    Only ``RunSession.close()`` transitions this lease back to releasable.
    The startup helpers deliberately do not, so that the bounded stop()
    retry budget is spent in exactly one place.
    """

    def __init__(self, lock: SupervisorLock) -> None:
        self._lock = lock
        self._releasable = True

    def mark_unreleasable(self) -> None:
        self._releasable = False

    def mark_releasable(self) -> None:
        self._releasable = True

    @property
    def releasable(self) -> bool:
        return self._releasable

    def release(self) -> None:
        self._lock.release()


def _finalize_interrupted_startup(server: OpenCodeServer, exc: BaseException) -> None:
    """Annotate `exc` for a direct `BaseException` (not an `Exception`
    subclass) raised from `server.start()`.

    Never raises or re-raises `exc` itself — the caller
    (``RunSession.start_server()``) performs the actual bare ``raise``
    immediately after calling this, from within its own ``except`` clause,
    so `exc`'s exact identity and traceback are preserved unchanged (no
    frame from this helper, or from a shared ``_startup_failure()``, is
    ever added).

    This function deliberately performs **no** cleanup and **no** lease
    bookkeeping. Confirming ``server.stop()`` and releasing the lock are
    owned solely by ``RunSession.close()``, which ``__exit__`` always
    calls. Attempting cleanup here as well would spend the bounded
    ``_CLEANUP_ATTEMPTS`` budget twice over, and previously caused a
    confirmed-clean interrupt to leak its lock because the lease was
    marked releasable in a code path that never called ``release()``.

    A note is attached only when cleanup is *not* subsequently confirmed;
    since that is not known yet at this point, the note is attached by
    ``close()`` instead. This helper therefore currently attaches nothing
    and exists to document the interrupt path's contract and to give
    ``start_server()`` a single, named place to record the startup
    exception's identity.
    """
    return None


def _startup_failure(
    supervisor: Supervisor,
    state: RunState,
    server: OpenCodeServer,
    exc: Exception,
    cleanup: _CleanupOutcome,
) -> NoReturn:
    """Handle an ordinary Exception raised from server.start(): persist the
    operational failure and raise a wrapped ``RuntimeError_``. Always
    raises; never returns.

    Callers (``RunSession.start_server()``) must only reach this function
    for `exc` values that are `Exception` subclasses. A direct
    `BaseException` (KeyboardInterrupt, SystemExit, or any other
    non-Exception) must never be routed here: it is handled entirely by
    the caller itself, via ``_finalize_interrupted_startup()`` followed
    immediately by a bare ``raise`` in the caller's own ``except`` clause,
    so its exact identity and traceback are preserved with no frame from
    this function (or any other called helper) ever added.

    `cleanup` is the already-computed outcome of confirming
    ``server.stop()``. This function does not run the retry itself and
    does not touch the lock lease: cleanup is owned by
    ``RunSession.close()``. The outcome is passed in only so the raised
    diagnostic can state whether the lock was retained.
    """
    outcome = cleanup
    exc_text = _safe_exception_text(exc)
    try:
        supervisor.record_external_failure(state, exc=exc, phase=state.phase)
    except Exception as persist_exc:
        # A failed persistence attempt is itself a startup-time failure;
        # prefer surfacing it (it means the operator has no durable record
        # to resume from), but never let it hide an unresolved cleanup
        # failure.
        persist_exc_text = _safe_exception_text(persist_exc)
        last_error_text = _safe_exception_text(outcome.last_error)
        if not outcome.confirmed:
            message = (
                f"failed to start OpenCode server: {exc_text}; additionally, the failure "
                f"could not be persisted ({persist_exc_text}); additionally, OpenCode "
                f"server cleanup could not be confirmed ({last_error_text}) — the "
                "repository lock has been retained; verify no OpenCode process "
                "survives before using --recover-stale-lock"
            )
        else:
            message = (
                f"failed to start OpenCode server: {exc_text}; additionally, the failure "
                f"could not be persisted ({persist_exc_text})"
            )
        result = RuntimeError_(message)
        _add_note(result, f"persistence failure: {persist_exc_text}")
        if not outcome.confirmed:
            _add_note(result, _unresolved_cleanup_message("startup failed and the", outcome))
        raise result from exc

    if not outcome.confirmed:
        message = (
            f"failed to start OpenCode server: {exc_text}; additionally, OpenCode server "
            f"cleanup could not be confirmed ({_safe_exception_text(outcome.last_error)}) — "
            "the repository lock has been retained; verify no OpenCode process survives "
            "before using --recover-stale-lock"
        )
        result = RuntimeError_(message)
        _add_note(result, _unresolved_cleanup_message("startup failed and the", outcome))
        raise result from exc

    raise RuntimeError_(f"failed to start OpenCode server: {exc_text}") from exc


class SessionState(Enum):
    """Lifecycle state of a :class:`RunSession` (single-threaded).

    Normal progression::

        NEW → ENTERING → READY → STARTING → STARTED → CLOSING → CLOSED
                                            ↕
                                        ADVANCING

    ``CLOSED`` and ``FAILED`` are terminal; every other state permits a
    further ``close()``.

    Two distinct non-terminal failure states exist because they describe
    materially different operator situations:

    ``CLEANUP_UNRESOLVED``
        ``server.stop()`` could not be confirmed. An OpenCode process may
        still be alive, so the lock is deliberately retained and must not
        be released. ``close()`` retries the bounded stop().

    ``RELEASE_PENDING``
        ``server.stop()`` *was* confirmed — no process survives — but
        releasing the lock failed. Only the release needs retrying;
        ``close()`` must not spend the stop() budget again.
    """

    NEW = "new"
    ENTERING = "entering"
    READY = "ready"
    STARTING = "starting"
    STARTED = "started"
    ADVANCING = "advancing"
    CLOSING = "closing"
    CLEANUP_UNRESOLVED = "cleanup_unresolved"
    RELEASE_PENDING = "release_pending"
    CLOSED = "closed"
    FAILED = "failed"


class _RunKind(Enum):
    """Which flavour of session this is: a new run or a resume.

    Orthogonal to the lock ``operation`` label (see ``RunSession``'s
    ``operation`` parameter): ``_RunKind`` drives run-id validation and
    which ``Supervisor`` construction path is used, while ``operation``
    only labels the lock record for display. The value here is merely
    the *default* ``operation`` when the caller does not override it.
    ``SupervisorLock`` validates the resolved operation against a fixed
    vocabulary (``run``/``resume``/``tui``)."""

    NEW = "run"
    RESUME = "resume"


class _RunOutcome(Enum):
    """Did the run/resume this ``close()`` call is concluding succeed or
    fail? Passed explicitly by the caller rather than inferred from
    whether an exception object happens to be attached.

    This is deliberately a separate question from "is there an exception
    to annotate" (see ``close()``'s ``error`` parameter). A caller that
    caught and already reported an exception before calling ``close()``
    still knows the run failed, even though it holds nothing further to
    annotate -- collapsing the two questions into one nullable exception
    parameter made that case inexpressible and silently produced the
    wrong retained-lock wording (a "run completed but ..." diagnostic on
    a run that had, in fact, failed)."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RunSession:
    """Guarded, retryable lifecycle object for one run or resume.

    Obtained inert from :func:`new_run_session` / :func:`resume_run_session`
    (state ``NEW``); every resource is acquired in ``__enter__``.

    Cleanup ownership: ``close()`` is the *only* place that confirms
    ``server.stop()`` and the *only* place that releases the lock. It is
    idempotent once the session is terminal, and retryable while cleanup
    remains unresolved. ``__exit__`` always delegates to it.

    Not thread-safe; see the module docstring.
    """

    def __init__(
        self,
        *,
        project_root: Path,
        run_kind: _RunKind,
        options: RunOptions | None = None,
        run_id: str | None = None,
        input_provider: InputProvider | None = None,
        recover_stale_lock: bool = False,
        server_observer: InvocationObserver | None = None,
        operation: str | None = None,
    ) -> None:
        self._project_root = project_root
        self._run_kind = run_kind
        self._options = options
        self._run_id = run_id
        # Label written into the lock record. Independent of run_kind: a
        # TUI session resuming a run is still a resume for _run_kind's
        # purposes (run-id validation, Supervisor construction), but the
        # lock should say "tui", not "resume". Defaults to run_kind's
        # value, preserving prior behavior exactly when not overridden.
        # Validation of the resolved value is SupervisorLock's job (see
        # its acquire()), not this class's: an invalid operation fails
        # closed via LockError before anything is written to disk, so
        # there is no malformed-lock hazard to guard against here.
        self._operation = operation if operation is not None else run_kind.value
        self._input_provider = input_provider
        self._recover_stale_lock = recover_stale_lock
        self._server_observer = server_observer

        self._state = SessionState.NEW
        self._lease: _LockLease | None = None
        self._supervisor: Supervisor | None = None
        self._run_state: RunState | None = None
        self._server: OpenCodeServer | None = None
        self._permission_denier: PermissionDenier | None = None
        # Snapshot taken in close() before the denier is torn down, so
        # denied_permission_count/_summary remain readable by a caller
        # (e.g. the CLI) that only inspects the session after the `with`
        # block has exited.
        self._denied_permission_count = 0
        self._denied_permission_summary: list[str] = []

        # True from the moment server.start() is attempted: an OpenCode
        # process may exist from then on, even if start() raised.
        self._server_may_exist = False
        # Identity of a direct BaseException raised by server.start(), so
        # close() can pick the correct retained-lock wording.
        self._startup_exception: BaseException | None = None
        # Cleanup outcome already computed during this failure sequence, if
        # any. The bounded _CLEANUP_ATTEMPTS budget is per cleanup attempt,
        # not per call site: start_server() must confirm stop() to decide
        # whether the raised startup diagnostic says the lock was retained,
        # and the close() that immediately follows must consume that same
        # result rather than spending the budget a second time.
        self._pending_cleanup: _CleanupOutcome | None = None
        # Whether an *unconfirmed* _pending_cleanup should be retried by
        # the next _confirm_cleanup() rather than consumed as-is.
        #
        # start_server()'s handoff is not retryable: the close() that
        # consumes it runs immediately afterwards via __exit__ with
        # nothing in between, so re-attempting stop() there would simply
        # spend the documented _CLEANUP_ATTEMPTS budget twice for one
        # failure sequence.
        #
        # stop_server()'s handoff is retryable: an in-flight advance()
        # typically unwinds between the two calls, and that later attempt
        # is the one most likely to succeed (it is no longer racing a
        # blocked HTTP transport). Consuming the stale failure instead
        # would make close() attempt stop() zero times and declare the
        # session unresolved on the strength of an obsolete attempt.
        self._pending_cleanup_retryable = False
        # Exceptions that have already received a retained-lock note, so a
        # second close() does not annotate the same object twice.
        #
        # Deliberately scoped to specific exception *identities* rather
        # than being a session-wide "already reported" flag. A bare flag
        # also suppressed the *outcome* of later independent close()
        # retries, so a retry whose stop() still failed returned None and
        # was indistinguishable from success while the lock was still
        # held. Suppression must cover duplicate annotation only, never
        # whether close() reports failure.
        self._annotated: list[BaseException] = []

        # See the module docstring's thread-safety section for why these
        # are two distinct primitives rather than one mutex.
        self._state_lock = threading.RLock()
        # Set == no advance() in flight. Starts set: a session that has
        # never advanced must not make close() wait.
        self._advance_done = threading.Event()
        self._advance_done.set()

    # -- read-only views -------------------------------------------------

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def base_url(self) -> str | None:
        """The started server's base URL, or None before startup."""
        if self._server is None:
            return None
        return self._server.base_url

    @property
    def run_state(self) -> RunState | None:
        """The current run state, or None before ``__enter__``."""
        return self._run_state

    @property
    def denied_permission_count(self) -> int:
        """How many ``permission.asked`` requests the headless denier
        (see ``permissions.PermissionDenier``) has auto-rejected so far.
        Zero before the server is started or if no request ever arrived.
        This is an in-memory diagnostic only, not persisted to
        ``RunState``.

        Readable after ``close()`` too: ``close()`` snapshots the count
        before tearing the denier down, since the common shape is
        ``with session: ...`` followed by inspecting the session once
        the block has already exited.
        """
        if self._permission_denier is not None:
            return self._permission_denier.denied_count
        return self._denied_permission_count

    @property
    def denied_permission_summary(self) -> list[str]:
        """Distinct permission keys denied so far, in first-seen order."""
        if self._permission_denier is not None:
            return self._permission_denier.denied_summary
        return list(self._denied_permission_summary)

    # -- observer / invocation control -----------------------------------

    def add_observer(self, observer: InvocationObserver) -> None:
        """Register an invocation observer on the underlying server."""
        if self._server is None:
            raise RuntimeError_(
                "add_observer() requires an entered session; the server does not exist yet"
            )
        self._server.add_observer(observer)

    def abort_active_invocations(self) -> None:
        """Best-effort abort of any in-flight agent invocations.

        Safe to call before startup (no-op) and never raises. The guard is
        real rather than decorative: ``abort_active_sessions()`` suppresses
        only per-session ``Exception``s, so a ``KeyboardInterrupt`` or a
        failure outside that inner loop would otherwise escape. Callers
        use this on shutdown paths where an escaping exception would
        replace whatever outcome is already being reported.
        """
        if self._server is None:
            return
        try:
            self._server.abort_active_sessions()
        except BaseException:  # noqa: BLE001 - best-effort by contract
            pass

    def stop_server(self) -> None:
        """Force a bounded ``server.stop()`` attempt ahead of ``close()``.

        For breaking a blocked in-flight ``advance()`` call: an OpenCode
        role invocation can block for up to its configured timeout, and
        stopping the server tears down the HTTP transport underneath it,
        letting the caller's ``advance()`` thread unwind with an error
        instead of waiting out the full timeout. This does **not** release
        the lock and does not touch the lease — only ``close()`` ever does
        either, per the module's single-owner contract. Deliberately more
        conservative than what it replaces: retaining the lock while the
        server is stopped is always safe under ADR 0009; only releasing it
        *before* a confirmed stop would not be.

        Safe to call before startup (no-op) and never raises, matching
        ``abort_active_invocations()``'s contract: this is meant for
        shutdown paths where an escaping exception would replace whatever
        outcome is already being reported. The bounded
        ``_CLEANUP_ATTEMPTS`` retry is spent here and the result is
        stashed in ``_pending_cleanup``, so the ``close()`` that follows
        consumes it instead of spending the budget a second time — the
        same handoff ``start_server()`` already relies on.

        A previously confirmed pending outcome is never overwritten by a
        later failed attempt: once cleanup is known to have succeeded,
        a subsequent (redundant) call must not make ``close()`` believe
        it is unresolved again.

        Safe to call from another thread while ``advance()`` is in flight
        — that is its purpose. It serializes against ``close()`` on
        ``_state_lock`` but never waits for ``advance()`` to finish, since
        waiting would defeat the point (see the module docstring's
        thread-safety section).
        """
        # self._server is constructed in __enter__, before server.start()
        # is ever attempted, so `self._server is None` does not mean "not
        # started yet" -- it only means "not entered at all". The correct
        # "has server.start() been attempted" guard is the same one
        # close() uses: self._server_may_exist (set immediately before
        # start_server() calls server.start()).
        with self._state_lock:
            if self._server is None or not self._server_may_exist:
                return
            outcome = _confirm_server_stopped(self._server)
            pending = self._pending_cleanup
            if pending is not None and pending.confirmed and not outcome.confirmed:
                return
            self._pending_cleanup = outcome
            self._pending_cleanup_retryable = True

    # -- lifecycle -------------------------------------------------------

    def __enter__(self) -> RunSession:
        """Acquire repository, lock, state, and server handle.

        Python does not call ``__exit__`` when ``__enter__`` raises, so
        this method is its own cleanup boundary: any failure after the
        lock is taken releases it (respecting the lease) before
        re-raising, attaching a note if release itself fails.
        """
        if self._state is not SessionState.NEW:
            raise RuntimeError_(
                f"cannot enter a session in state {self._state.value!r}; "
                "a RunSession may only be entered once"
            )
        self._state = SessionState.ENTERING

        try:
            repo = GitRepo(self._project_root)
        except GitError as exc:
            self._state = SessionState.FAILED
            raise RuntimeError_(f"cannot open repository: {exc}") from exc

        common_dir = repo.common_dir()

        # Validate the caller-supplied run ID *before* acquiring the lock.
        # This is cheap input validation (no state load, no Git mutation,
        # no race), and it prevents a crafted ID (e.g. "../../evil") from
        # ever being written into the lock record — which release() would
        # then refuse to parse, leaving a malformed, unrecoverable lock.
        if self._run_kind is _RunKind.RESUME:
            try:
                validate_run_id(self._run_id)
            except StateError as exc:
                self._state = SessionState.FAILED
                raise RuntimeError_(f"cannot load run {self._run_id!r}: {exc}") from exc

        lock = SupervisorLock(
            common_dir,
            operation=self._operation,
            run_id=self._run_id if self._run_kind is _RunKind.RESUME else None,
            integration_path=str(repo.root),
            recover_stale=self._recover_stale_lock,
        )
        try:
            lock.acquire()
        except LockError:
            self._state = SessionState.FAILED
            raise

        lease = _LockLease(lock)
        self._lease = lease

        try:
            provider = (
                self._input_provider if self._input_provider is not None else StdinInputProvider()
            )

            if self._run_kind is _RunKind.NEW:
                assert self._options is not None
                options = self._options
                supervisor = Supervisor(
                    repo=repo,
                    runner=_UnstartedRunner(),
                    git_common_dir=common_dir,
                    input_provider=provider,
                    options=options,
                )
                run_state = supervisor.start_new_run()
            else:
                assert self._run_id is not None
                try:
                    run_state = load_state(common_dir, self._run_id)
                except StateError as exc:
                    raise RuntimeError_(f"cannot load run {self._run_id!r}: {exc}") from exc

                supervisor = Supervisor(
                    repo=repo,
                    runner=_UnstartedRunner(),
                    git_common_dir=common_dir,
                    input_provider=provider,
                )
                try:
                    run_state = supervisor.resume(run_state)
                except LoopError as exc:
                    raise RuntimeError_(f"resume validation failed: {exc}") from exc
                options = run_state.options

            server = OpenCodeServer(
                self._project_root,
                OpenCodeServerConfig(
                    executable=options.opencode_executable,
                    startup_timeout=options.opencode_startup_timeout,
                    env=build_agent_env(self._project_root),
                ),
            )
            if self._server_observer is not None:
                server.add_observer(self._server_observer)

            self._supervisor = supervisor
            self._run_state = run_state
            self._server = server
        except BaseException as enter_exc:
            # Nothing here can have started an OpenCode process, so the
            # lease is still releasable and the lock must not be stranded.
            released = True
            if lease.releasable:
                try:
                    lease.release()
                except LockError as release_exc:
                    released = False
                    self._annotate_once(
                        enter_exc,
                        "additionally, the repository lock could not be released: "
                        + _safe_exception_text(release_exc),
                    )
            # A failed release leaves the lock on disk and still owned by
            # this session, so entry must not be terminal: RELEASE_PENDING
            # keeps close() able to retry the release. FAILED is correct
            # only when nothing is still held.
            self._state = SessionState.FAILED if released else SessionState.RELEASE_PENDING
            raise

        self._state = SessionState.READY
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> Literal[False]:
        """Always attempt ``close()``; never suppress the body exception.

        With no body exception, ``close()`` raises normally (an unresolved
        cleanup on an otherwise-successful run must surface).

        With a body exception, ``close()`` is still attempted but must not
        be allowed to escape: raising from ``__exit__`` would *replace*
        the primary exception and add an ``__exit__`` frame to it. Any
        cleanup failure is therefore attached as a note instead.

        Doing cleanup work here costs the propagating exception no
        traceback frames — only *raising* from ``__exit__`` would.
        """
        if exc_val is None:
            self.close()
            return False
        try:
            self.close(outcome=_RunOutcome.FAILED, error=exc_val)
        except BaseException as close_exc:  # noqa: BLE001 - must never replace exc_val
            if close_exc is not exc_val:
                _add_note(exc_val, _safe_exception_text(close_exc))
        return False

    def start_server(self) -> None:
        """Start OpenCode and hand it to the supervisor.

        Three distinct stages, each with its own failure contract:

        1. ``server.start()`` raising an ordinary ``Exception`` — persist
           an operational failure and raise a wrapped ``RuntimeError_``.
        2. ``server.start()`` raising a direct ``BaseException`` — bare
           ``raise`` from the active ``except`` clause, preserving exact
           identity and traceback; never persisted.
        3. The runner handoff (``supervisor.runner = server``) — a
           *separate* stage that propagates raw: not wrapped, not
           persisted. It is deliberately outside the ``try`` above, since
           routing it through ``_startup_failure`` would silently convert
           a handoff bug into a persisted ``opencode_startup`` failure.

        Cleanup for every one of these is handled by ``close()``.
        """
        if self._state is not SessionState.READY:
            raise RuntimeError_(
                f"cannot start the server in state {self._state.value!r}; expected 'ready'"
            )
        assert self._server is not None
        assert self._lease is not None
        assert self._supervisor is not None
        assert self._run_state is not None

        self._state = SessionState.STARTING
        # From this point an OpenCode process may exist. The lock must not
        # be released again until its cleanup is confirmed successful.
        self._lease.mark_unreleasable()
        self._server_may_exist = True

        try:
            self._server.start()
        except BaseException as exc:
            if not isinstance(exc, Exception):
                # A direct BaseException (KeyboardInterrupt, SystemExit, or
                # any other non-Exception) must propagate with its exact
                # original identity and traceback (see ADR 0009). The bare
                # `raise` below executes directly in this `except` clause —
                # the one still actively handling `exc` — rather than in a
                # called helper, so no additional frame is added.
                self._startup_exception = exc
                _finalize_interrupted_startup(self._server, exc)
                raise
            cleanup = self._confirm_cleanup()
            # Hand this outcome to the close() that __exit__ is about to
            # run, so the bounded retry budget is spent exactly once.
            # Not retryable: that close() follows immediately, with no
            # opportunity for conditions to have changed in between.
            self._pending_cleanup = cleanup
            self._pending_cleanup_retryable = False
            try:
                _startup_failure(self._supervisor, self._run_state, self._server, exc, cleanup)
            except BaseException as startup_exc:
                # _startup_failure has already described the retained lock
                # in the exception it raised; record that identity so the
                # close() in __exit__ does not annotate it a second time.
                if not cleanup.confirmed:
                    self._annotated.append(startup_exc)
                raise

        # Best-effort: a failure to start the permission denier must never
        # fail the run (mirrors sse.py's "SSE failure is strictly
        # non-fatal" contract, which PermissionDenier itself builds on).
        # Without it a stray permission.asked would silently stall a
        # phase for up to role_timeout with no diagnostic (see backlog
        # item 27); failing to start it is worse than not having it, but
        # still not worth failing an otherwise-healthy run over.
        try:
            base_url = self._server.base_url
        except Exception:
            base_url = None
        if base_url is not None:
            denier = PermissionDenier(base_url)
            try:
                denier.start()
            except Exception as exc:
                print(
                    f"loop-supervisor: permission denier failed to start: {exc}",
                    file=sys.stderr,
                )
            else:
                self._permission_denier = denier

        # Separate stage: a failure here is a runner-handoff failure, not a
        # startup failure, and propagates raw.
        self._supervisor.runner = self._server
        self._state = SessionState.STARTED

    def advance(self) -> AdvanceOutcome:
        """Dispatch exactly one phase and return its outcome.

        Requires a started server (the supervisor's runner is installed by
        ``start_server()``); calling it earlier would dispatch against the
        ``_UnstartedRunner`` placeholder and fail the run.

        Thread-safety: the supervisor call itself runs *outside*
        ``_state_lock`` (holding it there would deadlock ``stop_server()``,
        which exists to unblock exactly this call). ``_advance_done`` is
        cleared for the duration so a concurrent ``close()`` waits for
        this transition to finish before releasing the lock, and is set
        again in a ``finally`` so a raising ``advance()`` can never strand
        that waiter. On the way back in, both the ``SessionState``
        restore and the ``run_state`` write-back are guarded against a
        concurrent ``close()`` having already claimed the session while
        this call was in flight: the outcome is still returned to the
        caller (real work was done), but neither is written into a
        session that no longer owns it.
        """
        with self._state_lock:
            if self._state is not SessionState.STARTED:
                raise RuntimeError_(
                    f"cannot advance in state {self._state.value!r}; expected 'started'"
                )
            assert self._supervisor is not None
            assert self._run_state is not None
            supervisor = self._supervisor
            run_state = self._run_state
            self._state = SessionState.ADVANCING
            self._advance_done.clear()

        try:
            try:
                outcome = supervisor.advance(run_state)
            finally:
                # Release the barrier BEFORE reacquiring _state_lock, and
                # in a finally so a raising advance() cannot strand a
                # waiter. Lock-ordering matters here: a waiter must never
                # be able to hold _state_lock while blocked on this event
                # (close() deliberately waits before taking the lock), and
                # this side must never take the lock before setting it.
                self._advance_done.set()
        except BaseException:
            with self._state_lock:
                self._restore_started_unless_closed()
            raise

        with self._state_lock:
            self._store_run_state_unless_closed(outcome.state)
            self._restore_started_unless_closed()
        return outcome

    def _state_is(self, expected: SessionState) -> bool:
        """True if the state is still exactly ``expected``.

        Caller must hold ``_state_lock``. Used after a long-running call
        (``advance()``/``run_to_completion()``) has released the lock for
        the duration of the actual supervisor work, to detect whether a
        concurrent ``close()`` has since claimed the session -- so the
        finishing call does not write into, or resurrect, a session it no
        longer owns.
        """
        return self._state is expected

    def _restore_started_unless_closed(self) -> None:
        """Return to ``STARTED`` only if this advance() is still the
        session's current activity.

        Caller must hold ``_state_lock``. Unconditionally assigning
        ``STARTED`` (as this did before) let a finishing ``advance()``
        clobber the terminal state written by a concurrent ``close()``,
        silently resurrecting a session that had already released its
        lock into something that looked live and advanceable.
        """
        if self._state_is(SessionState.ADVANCING):
            self._state = SessionState.STARTED

    def run_to_completion(self, *, max_steps: int | None = None) -> RunState:
        """Run the supervisor loop to a terminal phase (or until input is
        unavailable, or ``max_steps`` completed advances have been taken),
        delegating to ``Supervisor.run()``.

        ``max_steps`` is a per-invocation session control, not a persisted
        run option: it is never written into ``RunState.options`` and does
        not survive into a later resume.

        Cleanup is not performed here: the caller's ``with`` block triggers
        ``__exit__`` → ``close()``.

        Thread-safety: mirrors ``advance()``'s contract exactly, and for
        the same reason -- ``Supervisor.run()`` itself loops calling
        ``advance()`` many times without ever going through
        ``RunSession.advance()``, so it does not otherwise get any of
        that method's protection. The supervisor call runs *outside*
        ``_state_lock`` (holding it here would deadlock ``stop_server()``,
        more acutely than for a single ``advance()`` since this call can
        run for the entire remaining session). ``_advance_done`` is
        cleared for the duration so a concurrent ``close()`` waits for
        this call to finish before releasing the lock, and is set again
        in a ``finally`` so a raising call can never strand that waiter.

        Unlike ``advance()``, this method makes no ``SessionState``
        transition of its own (state remains ``STARTED`` throughout), so
        there is nothing to restore on the way out. Instead, writing the
        result back to ``_run_state`` is guarded by the same "still the
        session's current activity" check: if a concurrent ``close()``
        has already claimed the session by the time this call returns,
        the result is still returned to the caller (real work was done)
        but is not written into a session that no longer owns it.
        """
        with self._state_lock:
            if self._state is not SessionState.STARTED:
                raise RuntimeError_(
                    f"cannot run in state {self._state.value!r}; expected 'started'"
                )
            assert self._supervisor is not None
            assert self._run_state is not None
            supervisor = self._supervisor
            run_state = self._run_state
            self._advance_done.clear()

        try:
            final = supervisor.run(run_state, max_steps=max_steps)
        finally:
            # Release the barrier BEFORE reacquiring _state_lock -- same
            # lock-ordering requirement as advance(): a waiter must never
            # be able to hold _state_lock while blocked on this event.
            self._advance_done.set()

        with self._state_lock:
            self._store_run_state_unless_closed(final)
        return final

    def _store_run_state_unless_closed(self, run_state: RunState) -> None:
        """Write ``run_state`` back to the session only if it is still the
        session's current activity.

        Caller must hold ``_state_lock``. Shared by both ``advance()`` and
        ``run_to_completion()``, whose supervisor calls each run outside
        ``_state_lock`` and can therefore return after a concurrent
        ``close()`` has already claimed the session. Extracted as its own
        method (as ``_restore_started_unless_closed`` is for the
        analogous ``SessionState`` guard) precisely so it is directly
        callable and therefore directly testable -- a test that instead
        re-implements the ``if self._state_is(...)`` check inline can
        pass even when the guard inside the caller is missing or wrong,
        since it never calls the method it means to be verifying. (An
        earlier version of ``advance()`` wrote ``self._run_state``
        directly instead of through this method, which meant it lacked
        this guard even after ``run_to_completion()`` gained it.)
        """
        if self._state_is(SessionState.STARTED):
            self._run_state = run_state

    def close(
        self,
        *,
        outcome: _RunOutcome = _RunOutcome.SUCCEEDED,
        error: BaseException | None = None,
    ) -> None:
        """Confirm OpenCode cleanup, then release the lock.

        The single owner of both operations. Behaviour:

        * Terminal (``CLOSED``/``FAILED``) — no-op, so ``__exit__`` after
          an explicit ``close()`` is harmless.
        * No server was ever started — release the lock directly.
        * ``server.stop()`` confirmed — mark the lease releasable and
          release the lock; state becomes ``CLOSED``.
        * ``server.stop()`` not confirmed — leave the lease unreleasable
          so the lock stays on disk, set ``CLEANUP_UNRESOLVED``, and
          report. Calling ``close()`` again retries the bounded stop().
        * ``server.stop()`` confirmed but ``release()`` failed — set
          ``RELEASE_PENDING`` and report. Calling ``close()`` again
          retries the release **only**, without re-spending the stop()
          budget.

        Every call that ends with the lock still held reports it, either
        by raising or (when ``error`` is actively unwinding) by
        annotating it. A retry is never silently successful.

        Two independent pieces of information, deliberately not folded
        into one nullable parameter (an earlier design did, and that is
        what let a failed run with no exception in hand silently report
        as "run completed"):

        ``outcome``
            Did the run/resume this call is concluding succeed or fail?
            Selects the retained-lock wording ("the run failed and the
            ..." versus "run completed but ..."). The caller declares
            this; it is not inferred from whether ``error`` is given,
            because a caller may know the run failed after already
            having caught and disposed of the exception that says so.
        ``error``
            The exception to annotate, if the caller is holding one
            (regardless of ``outcome`` — an interrupt during an
            otherwise-successful cleanup retry is still worth attaching
            a note to). A cleanup failure must never replace a real run
            failure, so this is always annotated rather than raised
            when it is the exception actively unwinding through this
            call.

        Thread-safety: if an ``advance()`` is in flight on another thread,
        this blocks (unboundedly) until it finishes before touching the
        lease, so the repository lock is never released while a
        transition may still be mutating Git/state. The wait happens
        *before* ``_state_lock`` is acquired, so a blocked ``close()``
        never holds the lock that ``advance()`` needs in order to finish.
        """
        # Deliberately outside _state_lock -- see the lock-ordering note
        # in advance(). Unbounded by design: there is no safe bound, and
        # stop_server() is the escape hatch for a transition that would
        # otherwise block for its full role timeout.
        self._advance_done.wait()

        # Read exactly once, before any nested try/except runs (see
        # _current_exception()'s docstring for why a second read would be
        # unsafe).
        currently_unwinding = _current_exception()

        def _annotate_and_check_unwinding(target: BaseException | None, message: str) -> bool:
            """Annotate `target` if given, then report whether the
            caller may return silently (True) or must raise (False) --
            the one piece of control flow shared by every retained-lock/
            retained-lease branch in `close()`. True only when `target`
            is the exception actively unwinding through this exact call;
            never merely because `target` was given.

            Kept as a boolean rather than performing the raise itself so
            that each call site's own `raise`/`_raise_...()` still
            executes directly in `close()`'s frame, matching the no-
            extra-frames guarantee callers rely on.

            Centralising the *decision* is what fixes the recurring
            defect: two of the three call sites previously returned
            silently whenever an exception was merely *given*, rather
            than checking it was actively *unwinding* -- so a caller
            retrying `close()` on an exception it had already caught
            elsewhere (and was no longer unwinding) got a silent return
            instead of the raise it needed to detect a failed retry.
            """
            if target is not None:
                self._annotate_once(target, message)
            return target is not None and target is currently_unwinding

        with self._state_lock:
            if self._state in (SessionState.CLOSED, SessionState.FAILED):
                return

            lease = self._lease
            if lease is None:
                self._state = SessionState.CLOSED
                return

            state_before_close = self._state
            self._state = SessionState.CLOSING

            # Best-effort, and deliberately before server cleanup: an
            # SSECleanupError (worker thread not confirmed stopped within
            # the bounded join) must never replace the primary close()
            # outcome, matching every other best-effort teardown in this
            # method. Cleared even on failure so a retried close() does
            # not attempt to stop it again.
            if self._permission_denier is not None:
                denier = self._permission_denier
                self._denied_permission_count = denier.denied_count
                self._denied_permission_summary = denier.denied_summary
                self._permission_denier = None
                try:
                    denier.stop()
                except SSECleanupError:
                    pass

            # Skip stop() entirely when a previous close() already confirmed
            # it and only the lock release remains outstanding: the bounded
            # retry budget belongs to cleanup, not to lock release.
            if (
                state_before_close is not SessionState.RELEASE_PENDING
                and self._server_may_exist
                and self._server is not None
            ):
                outcome_obj = self._confirm_cleanup()
                if not outcome_obj.confirmed:
                    # Cleanup unresolved: the lease stays unreleasable, so the
                    # lock deliberately remains on disk for --recover-stale-lock.
                    self._state = SessionState.CLEANUP_UNRESOLVED

                    # An operator's interrupt takes the startup-specific
                    # wording and is annotated over `error`: the interrupt
                    # itself is the more relevant object to attach a note
                    # to, and it is what __exit__ will actually be
                    # unwinding in the production shape.
                    startup_exc = self._startup_exception
                    is_startup_interrupt = startup_exc is not None
                    annotate_target = startup_exc if is_startup_interrupt else error
                    prefix = _cleanup_prefix(outcome, startup_interrupted=is_startup_interrupt)
                    message = _unresolved_cleanup_message(prefix, outcome_obj)
                    if _annotate_and_check_unwinding(annotate_target, message):
                        # The exception carrying the note is actively
                        # unwinding through this call; it will surface on
                        # its own, and raising instead would only be
                        # swallowed by __exit__ anyway.
                        return
                    _raise_unresolved_cleanup(prefix, outcome_obj)
                lease.mark_releasable()

            if lease.releasable:
                try:
                    lease.release()
                except LockError as release_exc:
                    # Cleanup is confirmed but the lock is still on disk.
                    # SupervisorLock.release() keeps its ownership token on a
                    # transient failure precisely so this can be retried, so
                    # this state must stay non-terminal.
                    self._state = SessionState.RELEASE_PENDING
                    if _annotate_and_check_unwinding(
                        error,
                        "additionally, the repository lock could not be released: "
                        + _safe_exception_text(release_exc),
                    ):
                        return
                    # Preserve the parent's exception type on this path: an
                    # otherwise-successful operation whose release fails raised
                    # LockError before RunSession existed.
                    raise

            self._state = SessionState.CLOSED

    def _annotate_once(self, exc: BaseException, message: str) -> None:
        """Attach `message` to `exc` unless this session already annotated
        that exact object.

        Identity-scoped rather than a session-wide flag: repeated
        ``close()`` calls must not double-annotate the same exception, but
        must still each report their own outcome.
        """
        for seen in self._annotated:
            if seen is exc:
                return
        self._annotated.append(exc)
        _add_note(exc, message)

    def _confirm_cleanup(self) -> _CleanupOutcome:
        """Confirm ``server.stop()``, consuming a *confirmed* outcome already
        computed during this failure sequence.

        ``start_server()`` must know whether cleanup succeeded in order to
        word its startup diagnostic correctly, and ``close()`` then runs
        immediately afterwards via ``__exit__``. Without this handoff each
        would spend the full ``_CLEANUP_ATTEMPTS`` budget, doubling the
        documented bound.

        A confirmed pending outcome is always consumed as-is. An
        *unconfirmed* one is only retried when it was marked retryable
        (see ``_pending_cleanup_retryable``): ``stop_server()``'s handoff
        is, because a blocked transition usually unwinds between the two
        calls and the later attempt is the one likely to succeed;
        ``start_server()``'s is not, because the consuming ``close()``
        runs immediately afterwards and retrying there would merely spend
        the documented ``_CLEANUP_ATTEMPTS`` budget twice for a single
        failure sequence.
        """
        assert self._server is not None
        pending = self._pending_cleanup
        retryable = self._pending_cleanup_retryable
        self._pending_cleanup = None
        self._pending_cleanup_retryable = False
        if pending is not None and (pending.confirmed or not retryable):
            return pending
        return _confirm_server_stopped(self._server)


def new_run_session(
    project_root: Path,
    options: RunOptions,
    *,
    input_provider: InputProvider | None = None,
    recover_stale_lock: bool = False,
    server_observer: InvocationObserver | None = None,
    operation: str | None = None,
) -> RunSession:
    """Return an inert :class:`RunSession` for a new run.

    Nothing is acquired until the session is entered.

    ``input_provider`` defaults to ``StdinInputProvider()`` (interactive,
    TTY-only) when not supplied, matching prior behavior for callers that
    do not need to inject a different provider (e.g. the TUI's
    non-blocking queue-backed provider).

    ``operation`` labels the lock record (e.g. ``"tui"`` for a Textual
    frontend); it defaults to ``"run"``. It does not affect the run
    itself, only how the lock is reported to other processes/operators.
    """
    return RunSession(
        project_root=project_root,
        run_kind=_RunKind.NEW,
        options=options,
        input_provider=input_provider,
        recover_stale_lock=recover_stale_lock,
        server_observer=server_observer,
        operation=operation,
    )


def resume_run_session(
    project_root: Path,
    run_id: str,
    *,
    input_provider: InputProvider | None = None,
    recover_stale_lock: bool = False,
    server_observer: InvocationObserver | None = None,
    operation: str | None = None,
) -> RunSession:
    """Return an inert :class:`RunSession` resuming ``run_id``.

    Nothing is acquired until the session is entered; the run ID is
    validated during ``__enter__``, before the lock is taken.

    ``operation`` labels the lock record (e.g. ``"tui"`` for a Textual
    frontend); it defaults to ``"resume"``. It does not affect which run
    is resumed, only how the lock is reported to other processes/operators.
    """
    return RunSession(
        project_root=project_root,
        run_kind=_RunKind.RESUME,
        run_id=run_id,
        input_provider=input_provider,
        recover_stale_lock=recover_stale_lock,
        server_observer=server_observer,
        operation=operation,
    )


def _start_server_call_lineno(func: object) -> int:
    """Return the source line of the ``session.start_server()`` call inside
    ``func``.

    Used by the traceback tests instead of a hardcoded line number, which
    silently rots whenever the module shifts. Returns -1 when the call
    cannot be found, so a stale assertion fails loudly rather than
    becoming vacuously true.
    """
    src_lines, start = inspect.getsourcelines(func)  # type: ignore[arg-type]
    for offset, line in enumerate(src_lines):
        if "session.start_server()" in line:
            return start + offset
    return -1


def _report_denied_permissions(session: RunSession) -> None:
    """Print a one-line stderr diagnostic if the headless permission
    denier (see permissions.PermissionDenier) auto-rejected any
    ``permission.asked`` request during this run/resume invocation.

    This is the CLI-facing half of backlog item 27 ("no diagnostic"): a
    phase that stalled or failed because of a denial is otherwise
    silent about it. Deliberately printed here (inside the `with
    session:` block having just exited run_to_completion(), but before
    close() tears the denier down) rather than only in cmd_run/
    cmd_resume, so both headless entry points get it uniformly without
    duplicating the check.
    """
    count = session.denied_permission_count
    if count == 0:
        return
    keys = ", ".join(session.denied_permission_summary)
    print(
        f"loop-supervisor: denied {count} permission request(s) ({keys})",
        file=sys.stderr,
    )


def run_new(
    project_root: Path,
    options: RunOptions,
    *,
    input_provider: InputProvider | None = None,
    recover_stale_lock: bool = False,
    max_steps: int | None = None,
) -> RunState:
    """Start a new run from project_root.

    Acquires the repository lock, creates and saves the initial state
    (before starting OpenCode), then runs the full headless loop.

    ``input_provider`` defaults to ``StdinInputProvider()`` (interactive,
    TTY-only) when not supplied, matching prior behavior for callers that
    do not need to inject a different provider (e.g. the TUI's
    non-blocking queue-backed provider).

    ``max_steps`` is a per-invocation session control (see
    ``RunSession.run_to_completion``), not part of the persisted
    ``RunOptions``.
    """
    session = new_run_session(
        project_root,
        options,
        input_provider=input_provider,
        recover_stale_lock=recover_stale_lock,
    )
    with session:
        session.start_server()
        result = session.run_to_completion(max_steps=max_steps)
        _report_denied_permissions(session)
        return result


def run_resume(
    project_root: Path,
    run_id: str,
    *,
    input_provider: InputProvider | None = None,
    recover_stale_lock: bool = False,
    max_steps: int | None = None,
) -> RunState:
    """Resume a saved run from project_root.

    Acquires the lock first, then loads and validates state inside it, so
    no other process can modify the checkpoint between our load and
    mutation. OpenCode is not started until after validation succeeds.

    ``input_provider`` defaults to ``StdinInputProvider()`` (interactive,
    TTY-only) when not supplied, matching prior behavior for callers that
    do not need to inject a different provider (e.g. the TUI's
    non-blocking queue-backed provider).

    ``max_steps`` is a per-invocation session control (see
    ``RunSession.run_to_completion``), not part of the persisted
    ``RunOptions``.
    """
    session = resume_run_session(
        project_root,
        run_id,
        input_provider=input_provider,
        recover_stale_lock=recover_stale_lock,
    )
    with session:
        session.start_server()
        result = session.run_to_completion(max_steps=max_steps)
        _report_denied_permissions(session)
        return result


def list_run_ids(project_root: Path) -> list[str]:
    """List saved run IDs. Does not acquire the lock."""
    try:
        repo = GitRepo(project_root)
    except GitError as exc:
        raise RuntimeError_(f"cannot open repository: {exc}") from exc
    return list_runs(repo.common_dir())


def load_run(project_root: Path, run_id: str) -> RunState:
    """Load a single saved run state. Does not acquire the lock."""
    try:
        repo = GitRepo(project_root)
    except GitError as exc:
        raise RuntimeError_(f"cannot open repository: {exc}") from exc
    try:
        return load_state(repo.common_dir(), run_id)
    except StateError as exc:
        raise RuntimeError_(f"cannot load run {run_id!r}: {exc}") from exc


class _UnstartedRunner:
    """Placeholder used between state creation/validation and server startup."""

    def run_agent(self, **_kwargs: object) -> str:
        raise LoopError("agent invoked before OpenCode server was started")
