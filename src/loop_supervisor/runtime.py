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

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .git import GitError, GitRepo
from .locking import LockError, SupervisorLock
from .opencode import OpenCodeError, OpenCodeServer, OpenCodeServerConfig
from .state import RunOptions, RunState, StateError, list_runs, load_state, validate_run_id
from .supervisor import LoopError, Supervisor


class RuntimeError_(RuntimeError):
    """Raised by the runtime controller for startup/configuration errors."""


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
    except BaseException:
        # lock.release() can raise LockError on a transient failure (see
        # SupervisorLock.release()) rather than always succeeding. That
        # must never mask whatever exception the body itself raised (e.g.
        # a real run/resume failure): the run's own outcome is more
        # actionable than a secondary lock-release detail, and losing it
        # would make debugging real failures much harder. And if the
        # lease was marked unreleasable (OpenCode cleanup was never
        # confirmed), the lock must not be released at all: it is
        # deliberately left on disk for explicit stale-lock recovery.
        if lease.releasable:
            try:
                lease.release()
            except LockError:
                pass
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
        except OpenCodeError as exc:
            raise _startup_failure(supervisor, state, server, lease, exc) from exc

        supervisor.runner = server
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
        except OpenCodeError as exc:
            raise _startup_failure(supervisor, state, server, lease, exc) from exc

        supervisor.runner = server
        final = _run_and_stop(supervisor, state, server, lease)

    return final


def _startup_failure(
    supervisor: Supervisor,
    state: RunState,
    server: OpenCodeServer,
    lease: _LockLease,
    exc: OpenCodeError,
) -> RuntimeError_:
    """Build the exception to raise for a failed server.start(), persisting
    the operational failure and deciding whether the lock lease may be
    marked releasable again.

    server.start() already terminates the process and releases its own
    resources on any failure past subprocess creation, but that guarantee
    is not something this caller can observe directly — and startup's own
    internal best-effort cleanup could itself have failed silently. A
    retry of stop() here is defense in depth: it is the only way this
    function can confirm (rather than assume) that no OpenCode process
    survives before it is safe to mark the lease releasable again. If that
    retry also fails, the lease is left unreleasable and the lock is
    retained on disk for explicit stale-lock recovery once the operator
    has verified no OpenCode process remains.
    """
    cleanup_error: Exception | None = None
    try:
        server.stop()
    except Exception as stop_exc:
        cleanup_error = stop_exc
    else:
        lease.mark_releasable()

    try:
        supervisor.record_external_failure(state, exc=exc, phase=state.phase)
    except Exception as persist_exc:
        # A failed persistence attempt is itself a startup-time failure;
        # prefer surfacing it (it means the operator has no durable record
        # to resume from), but never let it hide an unresolved cleanup
        # failure.
        if cleanup_error is not None:
            result = RuntimeError_(
                f"failed to start OpenCode server: {exc}; additionally, the failure "
                f"could not be persisted ({persist_exc}); additionally, OpenCode "
                f"server cleanup could not be confirmed ({cleanup_error}) — the "
                "repository lock has been retained; verify no OpenCode process "
                "survives before using --recover-stale-lock"
            )
        else:
            result = RuntimeError_(
                f"failed to start OpenCode server: {exc}; additionally, the failure "
                f"could not be persisted ({persist_exc})"
            )
        result.__cause__ = exc
        return result

    if cleanup_error is not None:
        result = RuntimeError_(
            f"failed to start OpenCode server: {exc}; additionally, OpenCode server "
            f"cleanup could not be confirmed ({cleanup_error}) — the repository lock "
            "has been retained; verify no OpenCode process survives before using "
            "--recover-stale-lock"
        )
        result.__cause__ = exc
        return result

    return RuntimeError_(f"failed to start OpenCode server: {exc}")


def _run_and_stop(
    supervisor: Supervisor, state: RunState, server: OpenCodeServer, lease: _LockLease
) -> RunState:
    """Run the supervisor loop and always attempt to stop the server
    afterward, without letting a server.stop() cleanup failure mask a
    primary exception (or result) from supervisor.run().

    server.stop() can raise OpenCodeCleanupError/ExceptionGroup if one or
    more of its own cleanup stages failed (see OpenCodeServer.stop());
    that must never silently replace a real run failure, since the run's
    own outcome is far more actionable than a secondary process-cleanup
    detail — but a failed stop() also means the lock lease must remain
    unreleasable (see mark_unreleasable() in run_new()/run_resume()), so
    the lock is retained on disk rather than released while an OpenCode
    process may still be alive. The lease is only marked releasable again
    once stop() is confirmed to succeed, regardless of whether
    supervisor.run() itself succeeded or raised.
    """
    try:
        final = supervisor.run(state)
    except BaseException:
        try:
            server.stop()
        except Exception:
            pass
        else:
            lease.mark_releasable()
        raise
    try:
        server.stop()
    except Exception as exc:
        raise RuntimeError_(
            f"run completed but OpenCode server cleanup could not be confirmed "
            f"({exc}) — the repository lock has been retained; verify no OpenCode "
            "process survives before using --recover-stale-lock"
        ) from exc
    else:
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
