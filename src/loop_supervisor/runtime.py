"""Shared application-level controller for CLI and TUI.

Coordinates the acquisition ordering that both the headless CLI and the
Textual TUI need to follow:

1. Resolve repository / common directory.
2. Acquire lock.
3. Create / load / validate state.
4. Start OpenCode only after state exists and validation succeeds.
5. Run advance() / run().
6. Persist classified failures.
7. Stop OpenCode.
8. Release lock.

For a new run, state is saved *before* OpenCode starts so that a server
startup failure can be recorded against a real run ID.

For resume, Git validation happens *before* OpenCode starts, so a
mismatched or tampered run fails closed with no side effects.

Lock-vs-OpenCode-cleanup ordering: the repository lock is only ever
released once OpenCode server cleanup has been *confirmed* successful.
This is enforced by ``_LockLease`` below rather than by unconditionally
releasing at the end of the ``with`` block: an unconfirmed
``OpenCodeServer.stop()`` (a process that may still be alive, still
writing to the working tree or emitting output) must never be followed by
releasing the lock, since a successor process could then start mutating
the same repository concurrently with a surviving child. When cleanup
cannot be confirmed, the lock is deliberately retained on disk for
explicit ``--recover-stale-lock`` after the operator has verified no
OpenCode process remains running. A primary run/resume/startup failure
always takes precedence in what is raised; a secondary cleanup failure is
attached to it as a note rather than replacing it.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from .git import GitError, GitRepo
from .locking import LockError, SupervisorLock
from .opencode import OpenCodeServer, OpenCodeServerConfig
from .state import RunOptions, RunState, StateError, list_runs, load_state, validate_run_id
from .supervisor import LoopError, Supervisor

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


def _add_note(exc: BaseException, message: str) -> None:
    """Attach one deterministic note to `exc`, never letting a failure to
    annotate (or the underlying add_note() call itself) propagate: the
    caller's exception identity must be preserved regardless of whether
    annotating it succeeds."""
    try:
        exc.add_note(message)
    except BaseException:  # noqa: BLE001 - annotating must never replace the primary
        pass


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
    succeed. ``_lock_context`` consults ``releasable`` instead of always
    releasing, so an unconfirmed OpenCode cleanup can never be followed by
    releasing the lock.
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


@contextmanager
def _lock_context(
    git_common_dir: Path,
    *,
    operation: str,
    run_id: str | None = None,
    integration_path: str | None = None,
    recover_stale: bool = False,
) -> Iterator[_LockLease]:
    lock = SupervisorLock(
        git_common_dir,
        operation=operation,
        run_id=run_id,
        integration_path=integration_path,
        recover_stale=recover_stale,
    )
    try:
        lock.acquire()
    except LockError:
        raise
    lease = _LockLease(lock)
    try:
        yield lease
    except BaseException as body_exc:
        # lock.release() can raise LockError on a transient failure (see
        # SupervisorLock.release()) rather than always succeeding. That
        # must never mask whatever exception the body itself raised (e.g.
        # a real run/resume failure): the run's own outcome is more
        # actionable than a secondary lock-release detail, and losing it
        # would make debugging real failures much harder. If release()
        # fails, its failure is attached as a note instead of being
        # silently discarded, so the operator still learns about it. And
        # if the lease was marked unreleasable (OpenCode cleanup was
        # never confirmed), release() must not be called at all: the
        # caller (_startup_failure / _run_and_stop) is responsible for
        # having already attached retained-lock guidance to body_exc
        # before this point, and calling release() before OpenCode
        # cleanup is confirmed would be exactly the ordering violation
        # this lease exists to prevent.
        if lease.releasable:
            try:
                lease.release()
            except LockError as release_exc:
                _add_note(
                    body_exc,
                    "additionally, the repository lock could not be released: "
                    + _safe_exception_text(release_exc),
                )
        raise
    else:
        if lease.releasable:
            lease.release()
        # else: cleanup was never confirmed on this otherwise-successful
        # path; the lock is intentionally retained. Callers that reach
        # this branch with an unreleasable lease are expected to have
        # already raised (see _run_and_stop), so this is defense in depth.


def run_new(
    project_root: Path,
    options: RunOptions,
    *,
    recover_stale_lock: bool = False,
) -> RunState:
    """Start a new run from project_root.

    Acquires the repository lock, creates and saves the initial state (before
    starting OpenCode), then runs the full headless loop.
    """
    try:
        repo = GitRepo(project_root)
    except GitError as exc:
        raise RuntimeError_(f"cannot open repository: {exc}") from exc

    common_dir = repo.common_dir()

    from .cli import StdinInputProvider

    with _lock_context(
        common_dir,
        operation="run",
        integration_path=str(repo.root),
        recover_stale=recover_stale_lock,
    ) as lease:
        server_config = OpenCodeServerConfig(
            executable=options.opencode_executable,
            startup_timeout=options.opencode_startup_timeout,
        )
        supervisor = Supervisor(
            repo=repo,
            runner=_UnstartedRunner(),
            git_common_dir=common_dir,
            input_provider=StdinInputProvider(),
            options=options,
        )
        state = supervisor.start_new_run()

        server = OpenCodeServer(project_root, server_config)
        # From this point an OpenCode process may exist. The lock must not
        # be released again until its cleanup is confirmed successful.
        lease.mark_unreleasable()
        try:
            server.start()
        except BaseException as exc:
            if not isinstance(exc, Exception):
                # A direct BaseException (KeyboardInterrupt, SystemExit, or
                # any other non-Exception) must propagate with its exact
                # original identity and traceback (see ADR 0009). The bare
                # `raise` below executes directly in this `except` clause
                # — the one still actively handling `exc` — rather than in
                # a called helper, so no additional frame is added to the
                # traceback the caller observes. _finalize_interrupted_startup
                # only performs cleanup confirmation/lease/note bookkeeping
                # and never itself raises or re-raises `exc`.
                _finalize_interrupted_startup(server, lease, exc)
                raise
            _startup_failure(supervisor, state, server, lease, exc)

        final = _run_and_stop(supervisor, state, server, lease)

    return final


def run_resume(
    project_root: Path,
    run_id: str,
    *,
    recover_stale_lock: bool = False,
) -> RunState:
    """Resume a saved run from project_root.

    Acquires the lock first, then loads and validates state inside it, so
    no other process can modify the checkpoint between our load and mutation.
    OpenCode is not started until after validation succeeds.
    """
    try:
        repo = GitRepo(project_root)
    except GitError as exc:
        raise RuntimeError_(f"cannot open repository: {exc}") from exc

    common_dir = repo.common_dir()

    # Validate the caller-supplied run ID *before* acquiring the lock. This
    # is cheap input validation (no state load, no Git mutation, no race),
    # and it prevents a crafted ID (e.g. "../../evil") from ever being
    # written into the lock record — which release() would then refuse to
    # parse, leaving a malformed, unrecoverable lock behind.
    try:
        validate_run_id(run_id)
    except StateError as exc:
        raise RuntimeError_(f"cannot load run {run_id!r}: {exc}") from exc

    from .cli import StdinInputProvider

    with _lock_context(
        common_dir,
        operation="resume",
        run_id=run_id,
        integration_path=str(repo.root),
        recover_stale=recover_stale_lock,
    ) as lease:
        try:
            state = load_state(common_dir, run_id)
        except StateError as exc:
            raise RuntimeError_(f"cannot load run {run_id!r}: {exc}") from exc

        supervisor = Supervisor(
            repo=repo,
            runner=_UnstartedRunner(),
            git_common_dir=common_dir,
            input_provider=StdinInputProvider(),
        )

        try:
            state = supervisor.resume(state)
        except LoopError as exc:
            raise RuntimeError_(f"resume validation failed: {exc}") from exc

        server_config = OpenCodeServerConfig(
            executable=state.options.opencode_executable,
            startup_timeout=state.options.opencode_startup_timeout,
        )

        server = OpenCodeServer(project_root, server_config)
        # From this point an OpenCode process may exist. The lock must not
        # be released again until its cleanup is confirmed successful.
        lease.mark_unreleasable()
        try:
            server.start()
        except BaseException as exc:
            if not isinstance(exc, Exception):
                # See the matching comment in run_new(): this bare `raise`
                # must execute here, in the `except` clause still actively
                # handling `exc`, to preserve its exact traceback.
                _finalize_interrupted_startup(server, lease, exc)
                raise
            _startup_failure(supervisor, state, server, lease, exc)

        final = _run_and_stop(supervisor, state, server, lease)

    return final


def _finalize_interrupted_startup(
    server: OpenCodeServer, lease: _LockLease, exc: BaseException
) -> None:
    """Perform cleanup confirmation, lease bookkeeping, and note
    attachment for a direct `BaseException` (not an `Exception` subclass)
    raised from `server.start()`. Never raises or re-raises `exc` itself
    — the caller (run_new()/run_resume()) is responsible for the actual
    bare `raise` immediately after calling this, from within its own
    `except` clause, so `exc`'s exact identity and traceback are
    preserved unchanged (no frame from this helper, or from a shared
    `_startup_failure()`, is ever added).

    Bounded stop() retries here are defense in depth: they are the only
    way this function can confirm (rather than assume) that no OpenCode
    process survives before it is safe to mark the lease releasable
    again. If cleanup remains unresolved after every retry, the lease is
    left unreleasable and a retained-lock note is attached to `exc`
    directly (never replacing it) so the operator learns cleanup could
    not be confirmed.
    """
    outcome = _confirm_server_stopped(server)
    if outcome.confirmed:
        lease.mark_releasable()
    else:
        _add_note(exc, _unresolved_cleanup_message("startup was interrupted; the", outcome))


def _startup_failure(
    supervisor: Supervisor,
    state: RunState,
    server: OpenCodeServer,
    lease: _LockLease,
    exc: Exception,
) -> NoReturn:
    """Handle an ordinary Exception raised from server.start(), persisting
    the operational failure and deciding whether the lock lease may be
    marked releasable again. Always raises; never returns.

    Callers (run_new()/run_resume()) must only reach this function for
    `exc` values that are `Exception` subclasses. A direct `BaseException`
    (KeyboardInterrupt, SystemExit, or any other non-Exception) must never
    be routed here: it is handled entirely by the caller itself, via
    _finalize_interrupted_startup() followed immediately by a bare
    `raise` in the caller's own `except` clause, so its exact identity
    and traceback are preserved with no frame from this function (or any
    other called helper) ever added. See _finalize_interrupted_startup()
    for that path.

    server.start() already terminates the process and releases its own
    resources on any failure past subprocess creation, but that guarantee
    is not something this caller can observe directly — and startup's own
    internal best-effort cleanup could itself have failed silently.
    Bounded stop() retries here are defense in depth: they are the only
    way this function can confirm (rather than assume) that no OpenCode
    process survives before it is safe to mark the lease releasable
    again. If cleanup remains unresolved after every retry, the lease is
    left unreleasable and the lock is retained on disk for explicit
    stale-lock recovery once the operator has verified no OpenCode
    process remains.
    """
    outcome = _confirm_server_stopped(server)
    if outcome.confirmed:
        lease.mark_releasable()

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


def _run_and_stop(
    supervisor: Supervisor, state: RunState, server: OpenCodeServer, lease: _LockLease
) -> RunState:
    """Hand the started server to the supervisor, run the supervisor loop,
    and always attempt bounded, confirmed server-stop cleanup afterward,
    without letting a cleanup failure mask a primary exception (or
    result) from the runner handoff or supervisor.run().

    The runner handoff (`supervisor.runner = server`) happens inside this
    function's protected boundary rather than in run_new()/run_resume(),
    so a failure raised by the assignment itself (e.g. a property setter
    that validates its argument) is treated exactly like a run failure:
    the started server is still cleaned up and the lock lease is only
    marked releasable once that cleanup is confirmed.

    server.stop() can raise OpenCodeCleanupError/ExceptionGroup if one or
    more of its own cleanup stages failed (see OpenCodeServer.stop());
    that must never silently replace a real primary failure, since the
    primary's own outcome is far more actionable than a secondary
    process-cleanup detail — but a failed stop() also means the lock
    lease must remain unreleasable (see mark_unreleasable() in
    run_new()/run_resume()), so the lock is retained on disk rather than
    released while an OpenCode process may still be alive. The lease is
    only marked releasable again once stop() is confirmed to succeed,
    regardless of whether the runner handoff/supervisor.run() itself
    succeeded or raised. A KeyboardInterrupt/SystemExit raised by cleanup
    itself never replaces a pending primary exception; it is described
    only in a note attached to that primary.
    """
    try:
        supervisor.runner = server
        final = supervisor.run(state)
    except BaseException as exc:
        outcome = _confirm_server_stopped(server)
        if outcome.confirmed:
            lease.mark_releasable()
        else:
            _add_note(exc, _unresolved_cleanup_message("the run failed and the", outcome))
        raise

    outcome = _confirm_server_stopped(server)
    if not outcome.confirmed:
        _raise_unresolved_cleanup("run completed but", outcome)
    lease.mark_releasable()
    return final


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
