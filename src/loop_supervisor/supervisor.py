"""The loop state machine: planner -> optional architect -> builder ->
auditor -> supervisor merge.

This module is deliberately decoupled from the OpenCode HTTP/process
details (see opencode.py) and from real Git side effects being
irreversible without inspection: it depends only on `AgentRunner` and
`GitRepo`, both of which can be faked in tests.
"""

from __future__ import annotations

import os
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]
        pass


from .contracts import (
    ArchitectResult,
    ArchitectStatus,
    AuditorDisposition,
    AuditorResult,
    BuilderResult,
    BuilderStatus,
    ContractError,
    PlannerResult,
    PlannerStatus,
    check_decision_answered,
    check_task_identity,
)
from .decisions import (
    DecisionError,
    adr_content_hash,
    render_adr,
    validate_adr_target,
    validate_decisions_subpath,
    write_adr_idempotent,
)
from .git import GitError, GitRepo, MergeConflictError, TaskWorktree
from .opencode import AgentInvocationError, AgentRunner, OpenCodeError, PhaseTimeoutError
from .phases import (
    PHASE_ARCHITECTING,
    PHASE_AUDITING,
    PHASE_AWAITING_INPUT,
    PHASE_BUILDING,
    PHASE_CLEANUP_BRANCH,
    PHASE_CLEANUP_WORKTREE,
    PHASE_CREATING_WORKTREE,
    PHASE_DONE,
    PHASE_FAILED,
    PHASE_MERGING,
    PHASE_OPERATIONAL_FAILURE,
    PHASE_PLANNING,
    PHASE_RECORDING_DECISION,
    TERMINAL_PHASES,
)
from .state import DecisionRequest, OperationalErrorRecord, RunOptions, RunState, save_state

_TERMINAL_PHASES = TERMINAL_PHASES
_DURABLE_SIDE_EFFECT_PHASES = {
    PHASE_CREATING_WORKTREE,
    PHASE_RECORDING_DECISION,
    PHASE_MERGING,
    PHASE_CLEANUP_WORKTREE,
    PHASE_CLEANUP_BRANCH,
}


class LoopError(RuntimeError):
    """Raised for unrecoverable loop-level failures (limits, conflicts)."""


class FailurePersistenceError(RuntimeError):
    """Raised when the supervisor cannot persist a failure record."""


class InputProvider(Protocol):
    def request(self, *, kind: str, message: str, context: dict) -> str | None:
        """Return the user's answer, or None if input is unavailable right
        now (e.g. non-interactive/no TTY), in which case the caller must
        persist a pending question and stop."""
        ...


@dataclass
class Limits:
    max_accepted_tasks: int = 20
    max_revisions_per_task: int = 5
    max_replans_per_task: int = 3
    max_architect_retries: int = 3
    malformed_output_retries: int = 1
    role_timeout: float = 1800.0


def _default_run_options() -> RunOptions:
    defaults = Limits()
    return RunOptions(
        max_accepted_tasks=defaults.max_accepted_tasks,
        max_revisions_per_task=defaults.max_revisions_per_task,
        max_replans_per_task=defaults.max_replans_per_task,
        max_architect_retries=defaults.max_architect_retries,
        malformed_output_retries=defaults.malformed_output_retries,
        role_timeout=defaults.role_timeout,
        worktree_root=None,
        require_decision_approval=False,
        opencode_executable="opencode",
        opencode_startup_timeout=30.0,
    )


class AdvanceStatus(StrEnum):
    ADVANCED = "advanced"
    INPUT_REQUIRED = "input_required"
    INPUT_UNAVAILABLE = "input_unavailable"
    OPERATIONAL_FAILURE = "operational_failure"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class AdvanceOutcome:
    status: AdvanceStatus
    state: RunState
    phase_before: str
    phase_after: str
    error: Exception | None = None


class Supervisor:
    def __init__(
        self,
        *,
        repo: GitRepo,
        runner: AgentRunner,
        git_common_dir: Path,
        decisions_subpath: Path = Path("docs/decisions"),
        input_provider: InputProvider,
        options: RunOptions | None = None,
    ) -> None:
        self.repo = repo
        self.runner = runner
        self.git_common_dir = git_common_dir
        self.decisions_subpath = decisions_subpath
        self.input_provider = input_provider
        self.options = options or _default_run_options()
        self._active_worktree: TaskWorktree | None = None

    # -- convenience views onto immutable run options --------------------

    @property
    def limits(self) -> Limits:
        return Limits(
            max_accepted_tasks=self.options.max_accepted_tasks,
            max_revisions_per_task=self.options.max_revisions_per_task,
            max_replans_per_task=self.options.max_replans_per_task,
            max_architect_retries=self.options.max_architect_retries,
            malformed_output_retries=self.options.malformed_output_retries,
            role_timeout=self.options.role_timeout,
        )

    @property
    def worktree_root(self) -> Path | None:
        return Path(self.options.worktree_root) if self.options.worktree_root else None

    @property
    def auto_decide(self) -> bool:
        return not self.options.require_decision_approval

    # -- run lifecycle --------------------------------------------------

    def start_new_run(self) -> RunState:
        self.repo.require_clean_integration()
        from .state import STATE_SCHEMA_VERSION, new_run_id

        integration_head = self.repo.head_commit()
        state = RunState(
            schema_version=STATE_SCHEMA_VERSION,
            run_id=new_run_id(),
            git_common_dir=str(self.git_common_dir),
            integration_path=str(self.repo.root),
            integration_branch=self.repo.current_branch(),
            integration_commit_at_start=integration_head,
            options=self.options,
            integration_expected_head=integration_head,
            integration_status_snapshot=self.repo.status_snapshot(),
            phase=PHASE_PLANNING,
        )
        self._save(state)
        return state

    def resume(self, state: RunState) -> RunState:
        self.options = state.options
        self._validate_resume(state)
        if state.task_worktree_path and state.task_branch:
            self._active_worktree = TaskWorktree(
                path=Path(state.task_worktree_path),
                branch=state.task_branch,
                original_task_id=state.original_task_id or "",
                base_commit=state.task_base_commit or "",
            )
        return state

    def _effective_resume_phase(self, state: RunState) -> str:
        """Return the phase resume validation should dispatch against.

        For every phase except operational_failure, this is simply
        state.phase. For operational_failure, the phase that will actually
        run next (once _do_retry_operational_failure() executes on the
        first advance() after resume) is the *retry_phase* recorded in the
        error, not "operational_failure" itself. Validating against the
        literal "operational_failure" phase would fall through to the
        generic task-worktree branch (which requires an exact status
        snapshot match) for every operational failure regardless of which
        phase it actually interrupted — including cleanup_worktree, whose
        own dedicated validation is deliberately more lenient about
        worktree content because cleanup is expected to remove it. Using
        the effective phase here ensures resume validates the same rules
        that will actually apply once retried.

        Does not mutate state.phase; that mutation remains the sole
        responsibility of _do_retry_operational_failure() inside advance().
        """
        if state.phase != PHASE_OPERATIONAL_FAILURE:
            return state.phase
        if state.last_error is None:
            raise LoopError("operational_failure state is missing its error record")
        record = OperationalErrorRecord.from_dict(state.last_error)
        if not record.retryable or record.retry_phase is None:
            # Not retryable: there is no meaningful "next phase" to validate
            # against. Fall back to the literal phase so the generic branch
            # below still performs its baseline checks; _do_retry_operational_failure()
            # will reject this state as non-retryable on the next advance().
            return state.phase
        return record.retry_phase

    def _validate_resume(self, state: RunState) -> None:
        common_dir = str(self.git_common_dir)
        if state.git_common_dir != common_dir:
            raise LoopError(
                f"resume repository mismatch: state has {state.git_common_dir!r}, "
                f"current repo is {common_dir!r}"
            )
        if state.integration_path != str(self.repo.root):
            raise LoopError("resume integration worktree path mismatch")
        if state.integration_branch != self.repo.current_branch():
            raise LoopError("resume integration branch mismatch")
        if not self.repo.is_clean():
            raise LoopError("resume requires a clean integration worktree")

        current_head = self.repo.head_commit()
        if current_head != state.integration_expected_head:
            if not self.repo.is_ancestor(state.integration_expected_head, current_head):
                raise LoopError(
                    "resume integration HEAD diverged from the expected checkpoint: "
                    f"expected {state.integration_expected_head!r} (or a descendant), "
                    f"found {current_head!r}"
                )

        effective_phase = self._effective_resume_phase(state)

        task_fields = (
            state.original_task_id,
            state.task_worktree_path,
            state.task_branch,
            state.task_base_commit,
        )
        has_task = any(f is not None for f in task_fields)
        if has_task:
            worktree = TaskWorktree(
                path=Path(state.task_worktree_path or ""),
                branch=state.task_branch or "",
                original_task_id=state.original_task_id or "",
                base_commit=state.task_base_commit or "",
            )
            if effective_phase == PHASE_CLEANUP_BRANCH:
                self._validate_resume_cleanup_branch(state, worktree)
            elif effective_phase == PHASE_CLEANUP_WORKTREE:
                self._validate_resume_cleanup_worktree(state, worktree)
            else:
                if state.task_expected_head is None:
                    raise LoopError("resume task state is missing an expected HEAD checkpoint")
                try:
                    self.repo.validate_task_worktree(
                        worktree, expected_head=state.task_expected_head
                    )
                except GitError as exc:
                    raise LoopError(f"resume task worktree validation failed: {exc}") from exc
                if state.task_status_snapshot is not None:
                    actual_snapshot = self.repo.status_snapshot(cwd=worktree.path)
                    if actual_snapshot != state.task_status_snapshot:
                        if effective_phase == PHASE_RECORDING_DECISION:
                            self._validate_recording_decision_status_drift(
                                state, worktree, actual_snapshot
                            )
                        else:
                            raise LoopError(
                                "resume task worktree has changed since it was paused "
                                "(working-tree status snapshot mismatch)"
                            )
        elif effective_phase in (PHASE_ARCHITECTING, PHASE_BUILDING, PHASE_AUDITING):
            raise LoopError(f"resume phase {effective_phase!r} requires an active task worktree")

        if effective_phase == PHASE_RECORDING_DECISION:
            self._validate_resume_recording_decision(state)

    def _validate_resume_recording_decision(self, state: RunState) -> Path:
        """Reject a tampered or inconsistent persisted ADR target before
        OpenCode is ever started, so a corrupted pending_adr_path cannot
        cause a write outside the active worktree. Returns the validated,
        resolved target path."""
        if state.pending_adr_path is None or state.pending_adr_hash is None:
            raise LoopError("resume recording_decision requires pending ADR path and hash")
        try:
            validate_decisions_subpath(self.decisions_subpath)
            directory = self._active_directory(state)
            return validate_adr_target(
                worktree_root=directory,
                decisions_dir=directory / self.decisions_subpath,
                target_path=Path(state.pending_adr_path),
            )
        except DecisionError as exc:
            raise LoopError(f"resume recording_decision: {exc}") from exc

    def _validate_recording_decision_status_drift(
        self, state: RunState, worktree: TaskWorktree, actual_snapshot: str
    ) -> None:
        """Allow exactly one worktree change since the pre-write checkpoint
        during recording_decision resume: the already-written, byte-exact
        pending ADR file appearing as a new untracked entry.

        This narrowly reopens crash-after-write recovery (the ADR write in
        _do_recording_decision happens before the post-transition state
        save, so a crash in between leaves the pre-write status snapshot on
        disk) without weakening the checkpoint guarantee for any other
        change. Any other difference — a missing expected line, an extra
        line, mismatched ADR content, or an ADR target outside the active
        worktree — fails closed exactly like the generic mismatch case.
        """
        target = self._validate_resume_recording_decision(state)
        assert state.pending_adr_hash is not None

        def _fail() -> None:
            raise LoopError(
                "resume task worktree has changed since it was paused "
                "(working-tree status snapshot mismatch)"
            )

        if not target.exists() or target.is_symlink():
            _fail()
        actual_hash = adr_content_hash(target.read_text())
        if actual_hash != state.pending_adr_hash:
            _fail()

        try:
            relative = target.relative_to(worktree.path.resolve())
        except ValueError:
            _fail()
            return
        expected_line = f"?? {relative.as_posix()}"

        persisted_lines = (
            state.task_status_snapshot.splitlines() if state.task_status_snapshot else []
        )
        actual_lines = actual_snapshot.splitlines()

        persisted_counts = Counter(persisted_lines)
        actual_counts = Counter(actual_lines)
        added = actual_counts - persisted_counts
        removed = persisted_counts - actual_counts
        if removed or dict(added) != {expected_line: 1}:
            _fail()

    def _validate_resume_cleanup_worktree(self, state: RunState, worktree: TaskWorktree) -> None:
        """Resume cleanup_worktree: worktree may be present (remove again) or already gone."""
        registered = self.repo.registered_worktree_paths()
        path = worktree.path.resolve()
        if path in registered:
            actual_branch = self.repo.branch_at_path(worktree.path)
            if actual_branch != worktree.branch:
                raise LoopError(
                    f"resume cleanup_worktree: worktree at {path} is on branch "
                    f"{actual_branch!r}, expected {worktree.branch!r}"
                )
        elif worktree.path.exists():
            raise LoopError(
                f"resume cleanup_worktree: path {path} exists but is not a registered worktree"
            )

    def _validate_resume_cleanup_branch(self, state: RunState, worktree: TaskWorktree) -> None:
        """Resume cleanup_branch: worktree must be gone; branch may be present or deleted."""
        registered = self.repo.registered_worktree_paths()
        path = worktree.path.resolve()
        if path in registered:
            raise LoopError(
                f"resume cleanup_branch: worktree path {path} is still registered; "
                "cleanup_worktree must complete before cleanup_branch"
            )
        if self.repo.branch_exists(worktree.branch):
            integration_head = self.repo.head_commit()
            if not self.repo.is_ancestor(worktree.branch, integration_head):
                raise LoopError(
                    f"resume cleanup_branch: branch {worktree.branch!r} still exists "
                    "but its tip is not integrated into the current HEAD; "
                    "check for unexpected rewrite or incomplete merge"
                )

    def _checkpoint_phase(self, state: RunState) -> str:
        """The phase whose checkpoint semantics apply to this save.

        Ordinarily this is simply ``state.phase``. But when a retryable
        operational failure has already been recorded (phase is
        ``operational_failure``), the meaningful phase for checkpointing is
        the phase that actually failed and will be retried
        (``last_error.retry_phase``). This matters for cleanup_branch: the
        task worktree is intentionally gone once cleanup_branch is entered,
        so a failure *during* cleanup_branch must still checkpoint with the
        worktree-absent rules — otherwise _checkpoint() would try to
        inspect the removed worktree and failure persistence would itself
        fail (FailurePersistenceError), leaving the original branch-cleanup
        failure unrecorded.
        """
        if state.phase == PHASE_OPERATIONAL_FAILURE and state.last_error is not None:
            record = OperationalErrorRecord.from_dict(state.last_error)
            if record.retry_phase is not None:
                return record.retry_phase
        return state.phase

    def _checkpoint(self, state: RunState) -> None:
        """Refresh Git checkpoints before saving.

        During cleanup_branch the task worktree has been intentionally removed;
        we retain the last known task HEAD and clear the status snapshot rather
        than attempting to inspect a missing path. This also applies to an
        operational_failure whose retry target is cleanup_branch (a failure
        that occurred *during* branch cleanup, after the worktree was removed).
        """
        state.integration_expected_head = self.repo.head_commit()
        state.integration_status_snapshot = self.repo.status_snapshot()
        if self._checkpoint_phase(state) == PHASE_CLEANUP_BRANCH:
            state.task_status_snapshot = None
        elif state.task_worktree_path is not None:
            worktree_path = Path(state.task_worktree_path)
            state.task_expected_head = self.repo.head_commit(cwd=worktree_path)
            state.task_status_snapshot = self.repo.status_snapshot(cwd=worktree_path)
        else:
            state.task_expected_head = None
            state.task_status_snapshot = None

    # -- single-transition advance() ------------------------------------

    def advance(self, state: RunState) -> AdvanceOutcome:
        """Dispatch exactly the phase present at entry and return an outcome.

        Never dispatches the resulting phase in the same call. Persists state
        before every normal return. Calling advance() on done/failed is an
        idempotent terminal no-op."""
        phase_before = state.phase

        if phase_before in _TERMINAL_PHASES:
            return AdvanceOutcome(
                status=AdvanceStatus.TERMINAL,
                state=state,
                phase_before=phase_before,
                phase_after=phase_before,
            )

        try:
            if phase_before == PHASE_AWAITING_INPUT:
                resolved = self._try_resolve_pending_input(state)
                self._save(state)
                if resolved:
                    return AdvanceOutcome(
                        status=AdvanceStatus.ADVANCED,
                        state=state,
                        phase_before=phase_before,
                        phase_after=state.phase,
                    )
                else:
                    return AdvanceOutcome(
                        status=AdvanceStatus.INPUT_UNAVAILABLE,
                        state=state,
                        phase_before=phase_before,
                        phase_after=state.phase,
                    )
            elif phase_before == PHASE_PLANNING:
                self._do_planning(state)
            elif phase_before == PHASE_ARCHITECTING:
                self._do_architecting(state)
            elif phase_before == PHASE_BUILDING:
                self._do_building(state)
            elif phase_before == PHASE_AUDITING:
                self._do_auditing(state)
            elif phase_before == PHASE_CREATING_WORKTREE:
                self._do_creating_worktree(state)
            elif phase_before == PHASE_RECORDING_DECISION:
                self._do_recording_decision(state)
            elif phase_before == PHASE_MERGING:
                self._do_merging(state)
            elif phase_before == PHASE_CLEANUP_WORKTREE:
                self._do_cleanup_worktree(state)
            elif phase_before == PHASE_CLEANUP_BRANCH:
                self._do_cleanup_branch(state)
            elif phase_before == PHASE_OPERATIONAL_FAILURE:
                self._do_retry_operational_failure(state)
            else:
                raise LoopError(f"unknown phase {phase_before!r}")
        except _InputRequiredSignal:
            self._save(state)
            return AdvanceOutcome(
                status=AdvanceStatus.INPUT_REQUIRED,
                state=state,
                phase_before=phase_before,
                phase_after=state.phase,
            )
        except (
            AgentInvocationError,
            PhaseTimeoutError,
            GitError,
            DecisionError,
            ContractError,
        ) as exc:
            return self._handle_operational_failure(
                state,
                exc=exc,
                failed_phase=phase_before,
                phase_before=phase_before,
            )
        except LoopError as exc:
            return self._handle_terminal_failure(state, exc=exc, phase_before=phase_before)

        if state.phase == PHASE_AWAITING_INPUT:
            self._save(state)
            return AdvanceOutcome(
                status=AdvanceStatus.INPUT_REQUIRED,
                state=state,
                phase_before=phase_before,
                phase_after=state.phase,
            )

        self._save(state)
        return AdvanceOutcome(
            status=AdvanceStatus.ADVANCED,
            state=state,
            phase_before=phase_before,
            phase_after=state.phase,
        )

    def _handle_operational_failure(
        self,
        state: RunState,
        *,
        exc: Exception,
        failed_phase: str,
        phase_before: str,
    ) -> AdvanceOutcome:
        retry_phase, retryable, requires_repair, hint = _classify_operational_failure(
            exc, failed_phase
        )
        record = OperationalErrorRecord(
            error_id=uuid.uuid4().hex[:12],
            kind=_error_kind(exc),
            operation=failed_phase,
            failed_phase=failed_phase,
            retry_phase=retry_phase,
            exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
            message=_sanitize_message(str(exc)),
            retryable=retryable,
            requires_repair=requires_repair,
            recovery_hint=hint,
            occurred_at=datetime.now(UTC).isoformat(),
        )
        state.last_error = record.to_dict()
        if failed_phase == PHASE_BUILDING and state.task_worktree_path is not None:
            try:
                worktree_path = Path(state.task_worktree_path)
                state.task_expected_head = self.repo.head_commit(cwd=worktree_path)
                state.task_status_snapshot = self.repo.status_snapshot(cwd=worktree_path)
            except GitError:
                pass
        state.phase = PHASE_OPERATIONAL_FAILURE
        try:
            self._save(state)
        except Exception as save_exc:
            raise FailurePersistenceError(
                f"could not persist failure record: {save_exc}"
            ) from save_exc
        return AdvanceOutcome(
            status=AdvanceStatus.OPERATIONAL_FAILURE,
            state=state,
            phase_before=phase_before,
            phase_after=PHASE_OPERATIONAL_FAILURE,
            error=exc,
        )

    def _handle_terminal_failure(
        self,
        state: RunState,
        *,
        exc: Exception,
        phase_before: str,
    ) -> AdvanceOutcome:
        record = OperationalErrorRecord(
            error_id=uuid.uuid4().hex[:12],
            kind="terminal",
            operation=phase_before,
            failed_phase=phase_before,
            retry_phase=None,
            exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
            message=_sanitize_message(str(exc)),
            retryable=False,
            requires_repair=False,
            recovery_hint=None,
            occurred_at=datetime.now(UTC).isoformat(),
        )
        state.last_error = record.to_dict()
        state.phase = PHASE_FAILED
        try:
            self._save(state)
        except Exception as save_exc:
            raise FailurePersistenceError(
                f"could not persist terminal failure record: {save_exc}"
            ) from save_exc
        return AdvanceOutcome(
            status=AdvanceStatus.TERMINAL,
            state=state,
            phase_before=phase_before,
            phase_after=PHASE_FAILED,
            error=exc,
        )

    def record_external_failure(self, state: RunState, *, exc: Exception, phase: str) -> None:
        """Persist an operational failure that occurred outside advance()'s
        own dispatch loop (e.g. OpenCode server startup failing after a new
        run's initial state was already saved, or failing again on resume).
        Mutates and saves `state` in place; raises FailurePersistenceError
        if the record itself cannot be saved, exactly like a failure
        discovered inside advance().

        `phase` is normally the state's current phase at the point OpenCode
        startup was attempted. If that phase is already
        PHASE_OPERATIONAL_FAILURE — i.e. this is a *repeated* startup
        failure on a resumed run that had not yet recovered — the real
        target to fail into is the *previous* record's retry_phase, not
        "operational_failure" itself. Failing into "operational_failure"
        would both violate the invariant that a retryable record can never
        name "operational_failure" as its own retry target, and would
        silently discard the original interrupted phase, replacing it with
        a value that can never be resumed past.
        """
        effective_phase = self._resolve_retry_target(state, phase)
        self._handle_operational_failure(
            state,
            exc=exc,
            failed_phase=effective_phase,
            phase_before=phase,
        )

    def _resolve_retry_target(self, state: RunState, phase: str) -> str:
        """Return the phase that a startup failure discovered while
        attempting to act on `state` (currently at `phase`) should be
        classified against. If `phase` is already operational_failure,
        unwrap to the retry_phase of the existing record so repeated
        startup failures never overwrite the real interrupted phase."""
        if phase != PHASE_OPERATIONAL_FAILURE:
            return phase
        if state.last_error is None:
            raise LoopError(
                "cannot record a startup failure against operational_failure "
                "with no existing error record to recover the retry target from"
            )
        existing = OperationalErrorRecord.from_dict(state.last_error)
        if not existing.retryable or existing.retry_phase is None:
            raise LoopError(
                "cannot retry: the existing operational_failure record is not "
                "retryable or has no retry phase"
            )
        return existing.retry_phase

    def _do_retry_operational_failure(self, state: RunState) -> None:
        """Resume an operational failure by retrying at the recorded retry phase."""
        if state.last_error is None:
            raise LoopError("operational_failure phase has no error record")
        record = OperationalErrorRecord.from_dict(state.last_error)
        if not record.retryable:
            raise LoopError(f"operational failure is not retryable: {record.message}")
        retry_phase = record.retry_phase
        if retry_phase is None:
            raise LoopError("operational failure has no retry phase")
        state.last_error = None
        state.phase = retry_phase

    # -- compatibility loop over advance() ------------------------------

    def run(self, state: RunState, *, max_steps: int | None = None) -> RunState:
        """Compatibility loop: runs advance() repeatedly until terminal, input
        unavailable, the step budget is exhausted, or an error. Re-raises
        LoopError-originated failures to preserve existing headless CLI
        behavior.

        ``max_steps`` counts completed ``advance()`` calls, including ones
        that return ``INPUT_REQUIRED`` (which loops back without a phase
        transition). ``None`` (the default) means unbounded, matching prior
        behavior exactly. When the budget is exhausted before a terminal
        phase is reached, the current (non-terminal) state is returned
        without error, the same as ``INPUT_UNAVAILABLE``.
        """
        steps_taken = 0
        while state.phase not in _TERMINAL_PHASES:
            if max_steps is not None and steps_taken >= max_steps:
                return state
            outcome = self.advance(state)
            state = outcome.state
            steps_taken += 1
            if outcome.status == AdvanceStatus.INPUT_UNAVAILABLE:
                return state
            if outcome.status == AdvanceStatus.TERMINAL:
                if outcome.error is not None and state.phase == PHASE_FAILED:
                    raise LoopError(str(outcome.error)) from outcome.error
                return state
            if outcome.status == AdvanceStatus.OPERATIONAL_FAILURE:
                if outcome.error is not None:
                    raise LoopError(str(outcome.error)) from outcome.error
                return state
        return state

    def _save(self, state: RunState) -> None:
        self._checkpoint(state)
        save_state(self.git_common_dir, state)

    # -- planning ---------------------------------------------------------

    def _do_planning(self, state: RunState) -> None:
        if state.accepted_task_count >= self.limits.max_accepted_tasks:
            state.phase = PHASE_DONE
            return

        directory = self._planning_directory(state)
        prompt = _build_planner_prompt(state)
        raw = self.runner.run_agent(
            agent="loop-planner",
            directory=directory,
            prompt=prompt,
            timeout=self.limits.role_timeout,
        )
        result = _parse_with_retry(
            lambda text: PlannerResult.parse(text),
            raw,
            retries=self.limits.malformed_output_retries,
            rerun=lambda: self.runner.run_agent(
                agent="loop-planner",
                directory=directory,
                prompt=prompt + "\n\nYour previous response was invalid. "
                "Return exactly one JSON object matching the required schema.",
                timeout=self.limits.role_timeout,
            ),
        )
        state.planner_result = result.model_dump(mode="json")

        if result.status is PlannerStatus.COMPLETE:
            if state.task_worktree_path is not None:
                raise LoopError(
                    "planner returned COMPLETE while a task worktree "
                    f"({state.task_worktree_path}) is still active; this would "
                    "leak an unresolved task"
                )
            state.phase = PHASE_DONE
            return

        assert result.task_id is not None

        if state.task_worktree_path is None:
            state.pending_worktree_path = str(
                self.repo.default_worktree_path(result.task_id, worktree_root=self.worktree_root)
            )
            state.pending_worktree_branch = self.repo.branch_name(result.task_id)
            state.pending_worktree_base = self.repo.head_commit()
            state.phase = PHASE_CREATING_WORKTREE
            return

        state.revision_count = 0

        if result.decision_required:
            state.decision_request = DecisionRequest(
                origin="planner",
                question=result.decision_question or "",
                rationale=result.decision_rationale or "",
            ).to_dict()
            state.phase = PHASE_ARCHITECTING
        else:
            state.phase = PHASE_BUILDING

    def _planning_directory(self, state: RunState) -> Path:
        if state.task_worktree_path is not None:
            return Path(state.task_worktree_path)
        return self.repo.root

    def _active_directory(self, state: RunState) -> Path:
        if state.task_worktree_path is not None:
            return Path(state.task_worktree_path)
        return self.repo.root

    # -- creating worktree ------------------------------------------------

    def _do_creating_worktree(self, state: RunState) -> None:
        """Create (or reconcile) the task worktree based on persisted intent.

        The persisted path, branch, and base commit are immutable intent:
        this never substitutes current mutable Git state (e.g. the current
        integration HEAD) for what was recorded before the Git operation was
        attempted. See GitRepo.create_or_reconcile_task_worktree for the
        exact reconciliation rules applied when a crash leaves both the
        worktree and branch already created but the resulting task identity
        unsaved.
        """
        if (
            state.pending_worktree_path is None
            or state.pending_worktree_branch is None
            or state.pending_worktree_base is None
        ):
            raise LoopError("creating_worktree phase has no pending worktree intent")

        planner = self._require_planner_result(state)
        original_task_id = planner.task_id
        if not original_task_id:
            raise LoopError("creating_worktree phase has no planner task_id to reconcile against")

        worktree = self.repo.create_or_reconcile_task_worktree(
            original_task_id=original_task_id,
            path=Path(state.pending_worktree_path),
            branch=state.pending_worktree_branch,
            base_commit=state.pending_worktree_base,
            worktree_root=self.worktree_root,
        )

        state.original_task_id = worktree.original_task_id
        state.task_worktree_path = str(worktree.path)
        state.task_branch = worktree.branch
        state.task_base_commit = worktree.base_commit
        state.pending_worktree_path = None
        state.pending_worktree_branch = None
        state.pending_worktree_base = None
        self._active_worktree = worktree
        state.revision_count = 0

        if planner.decision_required:
            state.decision_request = DecisionRequest(
                origin="planner",
                question=planner.decision_question or "",
                rationale=planner.decision_rationale or "",
            ).to_dict()
            state.phase = PHASE_ARCHITECTING
        else:
            state.phase = PHASE_BUILDING

    # -- architecting -----------------------------------------------------

    def _do_architecting(self, state: RunState) -> None:
        if state.decision_request is None:
            raise LoopError("no active decision request recorded")
        decision_request = DecisionRequest.from_dict(state.decision_request)
        directory = self._active_directory(state)

        prior_answer = None
        if state.pending_question is not None:
            prior_answer = state.pending_question.get("answer")
            state.pending_question = None

        extra_context = None
        if decision_request.origin == "auditor" and state.auditor_result is not None:
            auditor = AuditorResult.model_validate(state.auditor_result)
            lines: list[str] = []
            if auditor.findings:
                lines.append("Auditor findings:")
                lines.extend(f"- {f}" for f in auditor.findings)
            if auditor.design_observations:
                lines.append("Auditor design observations:")
                lines.extend(f"- {o}" for o in auditor.design_observations)
            if lines:
                extra_context = "\n".join(lines)

        prompt = _build_architect_prompt(
            origin=decision_request.origin,
            question=decision_request.question,
            rationale=decision_request.rationale,
            prior_answer=prior_answer,
            extra_context=extra_context,
        )
        raw = self.runner.run_agent(
            agent="loop-architect",
            directory=directory,
            prompt=prompt,
            timeout=self.limits.role_timeout,
        )
        result = _parse_with_retry(
            lambda text: ArchitectResult.parse(text),
            raw,
            retries=self.limits.malformed_output_retries,
            rerun=lambda: self.runner.run_agent(
                agent="loop-architect",
                directory=directory,
                prompt=prompt + "\n\nYour previous response was invalid. "
                "Return exactly one JSON object matching the required schema.",
                timeout=self.limits.role_timeout,
            ),
        )
        check_decision_answered(
            requested_question=decision_request.question, answered_question=result.question
        )
        state.architect_result = result.model_dump(mode="json")

        if result.status is ArchitectStatus.NEEDS_INPUT:
            state.architect_retry_count += 1
            if state.architect_retry_count > self.limits.max_architect_retries:
                raise LoopError(
                    f"architect exceeded {self.limits.max_architect_retries} NEEDS_INPUT retries"
                )
            state.pending_question = {
                "kind": "architect_input",
                "message": result.input_request or "The architect needs more input.",
                "context": {"question": decision_request.question},
            }
            state.phase = PHASE_AWAITING_INPUT
            return

        assert result.adr is not None
        if not self.auto_decide:
            state.pending_question = {
                "kind": "decision_approval",
                "message": "Approve the proposed architecture decision?",
                "context": {
                    "title": result.adr.title,
                    "decision": result.adr.decision,
                },
            }
            state.phase = PHASE_AWAITING_INPUT
            raise _InputRequiredSignal()

        self._prepare_record_decision(state)

    def _prepare_record_decision(self, state: RunState) -> None:
        """Persist decision-recording intent and transition to recording_decision."""
        if state.architect_result is None:
            raise LoopError("no architect result recorded to approve")
        if state.decision_request is None:
            raise LoopError("no active decision request recorded to resolve")
        result = ArchitectResult.model_validate(state.architect_result)
        if result.adr is None:
            raise LoopError("architect result has no adr to record")

        directory = self._active_directory(state)
        validate_decisions_subpath(self.decisions_subpath)
        decisions_dir = directory / self.decisions_subpath
        from .decisions import next_adr_number, slugify

        content = render_adr(result.adr)
        number = next_adr_number(decisions_dir)
        filename = f"{number:04d}-{slugify(result.adr.title)}.md"
        target_path = decisions_dir / filename
        validate_adr_target(
            worktree_root=directory,
            decisions_dir=decisions_dir,
            target_path=target_path,
        )
        content_hash = adr_content_hash(content)

        state.pending_adr_path = str(target_path)
        state.pending_adr_hash = content_hash
        state.phase = PHASE_RECORDING_DECISION

    def _do_recording_decision(self, state: RunState) -> None:
        """Write the already-approved ADR idempotently and route to continuation."""
        if state.architect_result is None:
            raise LoopError("no architect result recorded to approve")
        if state.decision_request is None:
            raise LoopError("no active decision request recorded to resolve")
        if state.pending_adr_path is None or state.pending_adr_hash is None:
            raise LoopError("recording_decision phase missing path/hash intent")

        result = ArchitectResult.model_validate(state.architect_result)
        if result.adr is None:
            raise LoopError("architect result has no adr to record")

        directory = self._active_directory(state)
        validate_decisions_subpath(self.decisions_subpath)
        write_adr_idempotent(
            directory / self.decisions_subpath,
            result.adr,
            worktree_root=directory,
            target_path=state.pending_adr_path,
            expected_hash=state.pending_adr_hash,
        )

        origin = DecisionRequest.from_dict(state.decision_request).origin
        state.pending_adr_path = None
        state.pending_adr_hash = None
        state.decision_request = None
        state.architect_retry_count = 0
        state.phase = PHASE_BUILDING if origin == "planner" else PHASE_PLANNING

    def _record_decision(self, state: RunState) -> None:
        """Write the already-persisted, approved architect proposal exactly
        as recorded, and route to the correct continuation based on who
        escalated the decision. Never re-invokes the architect: approval
        consumes the existing proposal, it does not request a new one."""
        self._prepare_record_decision(state)

    def _require_planner_result(self, state: RunState) -> PlannerResult:
        if state.planner_result is None:
            raise LoopError("no planner result recorded")
        return PlannerResult.model_validate(state.planner_result)

    # -- building -----------------------------------------------------------

    def _do_building(self, state: RunState) -> None:
        planner = self._require_planner_result(state)
        worktree = self._require_worktree(state)

        guidance = None
        if state.pending_question is not None:
            guidance = state.pending_question.get("answer")
            state.pending_question = None

        required_changes = None
        audit_findings = None
        if state.auditor_result is not None:
            auditor = AuditorResult.model_validate(state.auditor_result)
            if auditor.disposition is AuditorDisposition.REVISE:
                required_changes = auditor.required_changes
                # design_observations is deliberately withheld here: it is the
                # auditor's scope/criteria-critique channel, routed only to
                # the planner on REPLAN (see _build_planner_prompt). Handing
                # it to the builder would invite the scope creep the auditor
                # prompt explicitly tells it to avoid on REVISE.
                audit_findings = auditor.findings

        prompt = _build_builder_prompt(
            planner,
            required_changes=required_changes,
            audit_findings=audit_findings,
            guidance=guidance,
        )
        raw = self.runner.run_agent(
            agent="loop-builder",
            directory=worktree.path,
            prompt=prompt,
            timeout=self.limits.role_timeout,
        )
        result = _parse_with_retry(
            lambda text: BuilderResult.parse(text),
            raw,
            retries=self.limits.malformed_output_retries,
            rerun=lambda: self.runner.run_agent(
                agent="loop-builder",
                directory=worktree.path,
                prompt=prompt + "\n\nYour previous response was invalid. "
                "Return exactly one JSON object matching the required schema.",
                timeout=self.limits.role_timeout,
            ),
        )
        check_task_identity(
            task_id=planner.task_id or "",
            objective=planner.objective or "",
            other_task_id=result.task_id,
            other_objective=result.objective,
            other_role="builder",
        )
        state.builder_result = result.model_dump(mode="json")

        if result.status is BuilderStatus.COMPLETE:
            if not result.commit:
                # BuilderResult's validator already requires a commit when
                # status is COMPLETE, so reaching here means a hand-edited
                # or otherwise corrupted state was replayed rather than a
                # normal contract violation. Fail closed with a message
                # naming the actual problem instead of letting an empty
                # string reach verify_builder_commit as a bare "commit ''
                # does not match" Git error.
                raise LoopError(
                    "builder reported status COMPLETE with no commit hash; "
                    "this should be unreachable via BuilderResult's own validation"
                )
            verified_head = self.repo.verify_builder_commit(worktree, result.commit)
            state.last_task_head = verified_head
            state.phase = PHASE_AUDITING
            return

        state.pending_question = {
            "kind": "builder_guidance",
            "message": (
                f"Builder reported {result.status.value}. "
                f"Concerns: {'; '.join(result.open_concerns) or '(none stated)'}\n"
                "Provide guidance to continue, or type 'replan' to send back to the planner."
            ),
            "context": {"status": result.status.value},
        }
        state.phase = PHASE_AWAITING_INPUT

    def _require_worktree(self, state: RunState) -> TaskWorktree:
        if self._active_worktree is not None:
            return self._active_worktree
        if state.task_worktree_path is None or state.task_branch is None:
            raise LoopError("no active task worktree recorded")
        worktree = TaskWorktree(
            path=Path(state.task_worktree_path),
            branch=state.task_branch,
            original_task_id=state.original_task_id or "",
            base_commit=state.task_base_commit or "",
        )
        self._active_worktree = worktree
        return worktree

    # -- auditing ------------------------------------------------------------

    def _do_auditing(self, state: RunState) -> None:
        planner = self._require_planner_result(state)
        worktree = self._require_worktree(state)
        integration_commit = self.repo.head_commit()

        prompt = _build_auditor_prompt(
            planner,
            integration_branch=state.integration_branch,
            integration_commit=integration_commit,
            task_branch=worktree.branch,
            task_commit=state.last_task_head or "",
            base_commit=worktree.base_commit,
        )
        raw = self.runner.run_agent(
            agent="loop-auditor",
            directory=worktree.path,
            prompt=prompt,
            timeout=self.limits.role_timeout,
        )
        result = _parse_with_retry(
            lambda text: AuditorResult.parse(text),
            raw,
            retries=self.limits.malformed_output_retries,
            rerun=lambda: self.runner.run_agent(
                agent="loop-auditor",
                directory=worktree.path,
                prompt=prompt + "\n\nYour previous response was invalid. "
                "Return exactly one JSON object matching the required schema.",
                timeout=self.limits.role_timeout,
            ),
        )
        check_task_identity(
            task_id=planner.task_id or "",
            objective=planner.objective or "",
            other_task_id=result.task_id,
            other_objective=result.objective,
            other_role="auditor",
        )
        state.auditor_result = result.model_dump(mode="json")

        if result.disposition is AuditorDisposition.ACCEPT:
            worktree = self._require_worktree(state)
            state.merge_pre_head = self.repo.head_commit()
            state.merge_task_head = self.repo.head_commit(cwd=worktree.path)
            state.phase = PHASE_MERGING
            return

        if result.disposition is AuditorDisposition.REVISE:
            state.revision_count += 1
            if state.revision_count > self.limits.max_revisions_per_task:
                raise LoopError(
                    f"task {self._require_worktree(state).original_task_id!r} exceeded "
                    f"{self.limits.max_revisions_per_task} revisions"
                )
            state.phase = PHASE_BUILDING
            return

        # REPLAN
        state.replan_count += 1
        if state.replan_count > self.limits.max_replans_per_task:
            raise LoopError(
                f"task {self._require_worktree(state).original_task_id!r} exceeded "
                f"{self.limits.max_replans_per_task} replans"
            )
        state.builder_result = None
        if result.decision_required:
            state.decision_request = DecisionRequest(
                origin="auditor",
                question=result.decision_question or "",
                rationale=result.decision_rationale or "",
            ).to_dict()
            state.phase = PHASE_ARCHITECTING
        else:
            state.phase = PHASE_PLANNING

    # -- merge and cleanup ------------------------------------------------

    def _do_merging(self, state: RunState) -> None:
        """Merge (or reconcile an already-completed merge of) the persisted,
        immutable task head into the integration branch.

        On conflict, let MergeConflictError reach the operational-failure
        classifier directly (do not wrap as GitError). Never merges the
        mutable task branch name: merge_pre_head/merge_task_head are
        immutable intent captured at ACCEPT time, so a crash after Git
        commits the merge but before state is saved is safely reconciled
        rather than re-merged.
        """
        worktree = self._require_worktree(state)
        if state.merge_pre_head is None or state.merge_task_head is None:
            raise LoopError("merging phase has no persisted merge intent")

        actual_task_head = self.repo.head_commit(cwd=worktree.path)
        if actual_task_head != state.merge_task_head:
            raise GitError(
                f"task branch {worktree.branch!r} HEAD {actual_task_head!r} no longer "
                f"matches the audited merge_task_head {state.merge_task_head!r}; "
                "refusing to merge unreviewed commits"
            )

        merge_commit = self.repo.reconcile_or_merge_task(
            pre_head=state.merge_pre_head,
            task_head=state.merge_task_head,
        )
        state.merge_commit = merge_commit
        state.phase = PHASE_CLEANUP_WORKTREE

    def _validate_merge_cleanup_safety(self, state: RunState, worktree: TaskWorktree) -> None:
        """Prove the persisted merge_commit is exactly the reviewed merge of
        merge_task_head before any cleanup removes the worktree or branch.

        This prevents cleanup from deleting a branch/worktree based merely
        on "the task branch tip is an ancestor of HEAD", which could be
        true for a fast-forward, cherry-pick, or a later unreviewed commit
        that happens to also be integrated.
        """
        if state.merge_commit is None or state.merge_task_head is None:
            raise LoopError("cleanup requires a persisted merge_commit and merge_task_head")

        if not self.repo.commit_exists(state.merge_commit):
            raise GitError(f"recorded merge_commit {state.merge_commit!r} no longer exists")

        integration_head = self.repo.head_commit()
        if state.merge_commit != integration_head and not self.repo.is_ancestor(
            state.merge_commit, integration_head
        ):
            raise GitError(
                f"recorded merge_commit {state.merge_commit!r} is not reachable from "
                f"the current integration HEAD {integration_head!r}"
            )

        parents = self.repo.commit_parents(state.merge_commit)
        if len(parents) != 2 or parents[1] != state.merge_task_head:
            raise GitError(
                f"recorded merge_commit {state.merge_commit!r} does not have "
                f"merge_task_head {state.merge_task_head!r} as its second parent; "
                f"found parents {parents!r}"
            )

        if self.repo.branch_exists(worktree.branch):
            branch_tip = self.repo.branch_commit(worktree.branch)
            if branch_tip != state.merge_task_head:
                raise GitError(
                    f"task branch {worktree.branch!r} has moved to {branch_tip!r} since "
                    f"the reviewed merge of {state.merge_task_head!r}; refusing to clean up"
                )

    def _do_cleanup_worktree(self, state: RunState) -> None:
        worktree = self._require_worktree(state)
        self._validate_merge_cleanup_safety(state, worktree)
        self.repo.remove_task_worktree_only(worktree)
        state.phase = PHASE_CLEANUP_BRANCH

    def _do_cleanup_branch(self, state: RunState) -> None:
        worktree = self._require_worktree(state)
        self._validate_merge_cleanup_safety(state, worktree)
        self.repo.delete_task_branch_only(worktree)
        self._finish_task_cleanup(state)

    def _finish_task_cleanup(self, state: RunState) -> None:
        state.accepted_task_count += 1
        state.original_task_id = None
        state.task_worktree_path = None
        state.task_branch = None
        state.task_base_commit = None
        state.last_task_head = None
        state.revision_count = 0
        state.replan_count = 0
        state.architect_retry_count = 0
        state.planner_result = None
        state.architect_result = None
        state.builder_result = None
        state.auditor_result = None
        state.decision_request = None
        state.merge_pre_head = None
        state.merge_task_head = None
        state.merge_commit = None
        self._active_worktree = None
        state.phase = PHASE_PLANNING

    # -- pending input resolution --------------------------------------------

    def _try_resolve_pending_input(self, state: RunState) -> bool:
        assert state.pending_question is not None
        pending = state.pending_question
        answer = self.input_provider.request(
            kind=pending["kind"],
            message=pending["message"],
            context=pending.get("context", {}),
        )
        if answer is None:
            return False

        pending["answer"] = answer

        if pending["kind"] == "builder_guidance":
            if answer.strip().lower() == "replan":
                state.pending_question = None
                state.builder_result = None
                state.phase = PHASE_PLANNING
            else:
                state.pending_question = pending
                state.phase = PHASE_BUILDING
        elif pending["kind"] == "architect_input":
            state.pending_question = pending
            state.phase = PHASE_ARCHITECTING
        elif pending["kind"] == "decision_approval":
            state.pending_question = None
            if answer.strip().lower() in ("y", "yes", "approve"):
                self._prepare_record_decision(state)
            else:
                question = (
                    DecisionRequest.from_dict(state.decision_request).question
                    if state.decision_request is not None
                    else ""
                )
                state.pending_question = {
                    "kind": "architect_input",
                    "message": "Provide feedback on the rejected decision proposal.",
                    "context": {"question": question},
                }
                state.phase = PHASE_AWAITING_INPUT
        else:
            raise LoopError(f"unknown pending question kind {pending['kind']!r}")
        return True


class _InputRequiredSignal(BaseException):
    pass


def _classify_operational_failure(
    exc: Exception, failed_phase: str
) -> tuple[str | None, bool, bool, str | None]:
    """Return (retry_phase, retryable, requires_repair, recovery_hint)."""
    if isinstance(exc, MergeConflictError):
        return (
            PHASE_MERGING,
            True,
            True,
            "The merge was aborted. Manually resolve and create a no-FF merge of the "
            "exact persisted task commit into the integration branch, then resume.",
        )
    if isinstance(exc, PhaseTimeoutError):
        return (failed_phase, True, False, "Resume to retry the timed-out phase.")
    if isinstance(exc, AgentInvocationError):
        return (failed_phase, True, False, "Resume to retry after a transient OpenCode error.")
    if isinstance(exc, GitError):
        if failed_phase == PHASE_CLEANUP_WORKTREE:
            return (
                failed_phase,
                True,
                True,
                "The reviewed commit is already merged; this worktree has unreviewed "
                "content. Inspect it, then commit/discard the unreviewed changes (do "
                "not add new commits) or restore the branch to the reviewed head, and "
                "resume.",
            )
        if failed_phase == PHASE_CREATING_WORKTREE:
            return (
                failed_phase,
                True,
                True,
                "A worktree already exists at the intended path but contains "
                "unexpected content (no builder phase has run against it yet). "
                "Inspect and remove the unexpected content, then resume.",
            )
        return (failed_phase, True, False, "Resume to retry after the Git error is resolved.")
    if isinstance(exc, DecisionError):
        return (failed_phase, True, True, "Inspect the ADR directory and resume.")
    if isinstance(exc, OpenCodeError):
        return (
            failed_phase,
            True,
            False,
            "Resume to retry after the OpenCode server/startup problem is resolved.",
        )
    if isinstance(exc, ContractError):
        return (
            failed_phase,
            True,
            False,
            "The role returned output that does not satisfy its contract. Resume "
            "to retry the phase; if it recurs, the agent prompt or the contract "
            "schema may have drifted.",
        )
    return (failed_phase, True, False, None)


def _error_kind(exc: Exception) -> str:
    if isinstance(exc, MergeConflictError):
        return "merge_conflict"
    if isinstance(exc, PhaseTimeoutError):
        return "timeout"
    if isinstance(exc, AgentInvocationError):
        return "agent_invocation"
    if isinstance(exc, GitError):
        return "git"
    if isinstance(exc, DecisionError):
        return "decision"
    if isinstance(exc, OpenCodeError):
        return "opencode_startup"
    if isinstance(exc, ContractError):
        return "contract"
    return "unknown"


# Env var *names* treated as likely to hold a secret. Matched against
# os.environ keys, not values -- deliberately broad (better to redact an
# unrelated "MY_TOKEN" than to miss a real one) since the actual
# candidate strings being scrubbed always come from _this_ process's own
# environment, never from user-controlled text.
_SECRET_ENV_NAME_RE = re.compile(
    r"(API_KEY|_KEY$|^KEY$|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTHORIZATION|ACCESS_KEY)",
    re.IGNORECASE,
)

# Below this length, a secret-named env var's value is not redacted. Real
# credentials (API keys, tokens) are comfortably longer than this in every
# common format (shortest observed: 40-character AWS/GitHub secrets); a
# short value under a secret-sounding name is far more likely to be an
# unset placeholder, a username typed into the wrong field, or a test
# fixture, and blind literal-value replacement at that length risks
# silently mangling unrelated diagnostic text (e.g. a value that happens
# to match part of a file path).
_MIN_REDACTABLE_SECRET_LENGTH = 16

# Common credential formats, matched even when the value never appeared
# in *this* process's environment (e.g. it belongs to the OpenCode child
# process's own configuration, or was echoed back by a remote provider).
# This is a backstop, not a guarantee: it recognizes known shapes, not
# arbitrary secrets.
_SECRET_PATTERN_RE = re.compile(
    r"sk-[A-Za-z0-9_-]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{36}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|(?i:authorization|bearer)\s*:?\s*\S+"
)

# Total persisted message length, and how much of that budget goes to the
# leading portion. Kept well under _diagnostic_output()'s raw ceiling
# (500 stdout lines, each unbounded) so a single record can never grow
# unreasonably large, while still preserving both ends of the original
# text: the earliest lines (often a banner or config echo) and the latest
# lines (often the actual terminal error), rather than only the former.
_MAX_MESSAGE_LENGTH = 2000
_MESSAGE_HEAD_LENGTH = 500


def _redact_secrets(msg: str) -> str:
    """Best-effort removal of known-secret environment values and common
    credential formats from `msg`. Not a guarantee that no secret can
    ever appear (arbitrary repository content or novel credential
    formats are out of scope), but removes the specific, high-confidence
    cases this process can identify."""
    for name, value in os.environ.items():
        if len(value) < _MIN_REDACTABLE_SECRET_LENGTH:
            continue
        if not _SECRET_ENV_NAME_RE.search(name):
            continue
        if value in msg:
            msg = msg.replace(value, f"[redacted:{name}]")
    return _SECRET_PATTERN_RE.sub("[redacted]", msg)


def _truncate_message(msg: str) -> str:
    """Bound `msg` to `_MAX_MESSAGE_LENGTH`, keeping both a leading and a
    trailing portion rather than only the head. A pure head-truncation
    would keep whatever came first (often a startup banner) and discard
    whatever came last (often the actual terminating error), which is
    exactly backwards for diagnosing a failure."""
    if len(msg) <= _MAX_MESSAGE_LENGTH:
        return msg
    marker = "\n…[truncated]…\n"
    tail_length = _MAX_MESSAGE_LENGTH - _MESSAGE_HEAD_LENGTH - len(marker)
    return msg[:_MESSAGE_HEAD_LENGTH] + marker + msg[-tail_length:]


def _sanitize_message(msg: str) -> str:
    """Redact known secrets, then truncate, for inclusion in a durable
    `OperationalErrorRecord.message`. Redaction runs first so a secret
    cannot survive by falling on the truncation boundary. Best-effort:
    see `_redact_secrets()`'s docstring for what is and is not covered.
    """
    return _truncate_message(_redact_secrets(msg))


def _parse_with_retry(parse, raw, *, retries, rerun):
    try:
        return parse(raw)
    except ContractError:
        if retries <= 0:
            raise
        for _ in range(retries):
            raw = rerun()
            try:
                return parse(raw)
            except ContractError:
                continue
        return parse(raw)


def _build_planner_prompt(state: RunState) -> str:
    lines = ["Determine the next unit of work."]
    if state.auditor_result is not None:
        auditor = AuditorResult.model_validate(state.auditor_result)
        if auditor.disposition is AuditorDisposition.REPLAN:
            lines.append("")
            lines.append(
                "The previous task on this worktree/branch was sent back for replanning. "
                "The existing branch and its intermediate commits are preserved; "
                "continue from that state rather than starting over."
            )
            lines.append(f"Previous task_id: {auditor.task_id}")
            lines.append(f"Previous objective: {auditor.objective}")
            if auditor.findings:
                lines.append("Auditor findings:")
                lines.extend(f"- {f}" for f in auditor.findings)
            if auditor.design_observations:
                lines.append("Auditor design observations:")
                lines.extend(f"- {o}" for o in auditor.design_observations)
            if auditor.decision_required:
                lines.append("")
                lines.append("The auditor escalated this design question:")
                lines.append(f"Question: {auditor.decision_question}")
                lines.append(f"Rationale: {auditor.decision_rationale}")
    if state.architect_result is not None:
        architect = ArchitectResult.model_validate(state.architect_result)
        if architect.adr is not None:
            lines.append("")
            lines.append(f"A decision was just recorded: {architect.adr.title}")
            lines.append(architect.adr.decision)
    return "\n".join(lines)


def _build_architect_prompt(
    *,
    origin: str,
    question: str,
    rationale: str,
    prior_answer: str | None,
    extra_context: str | None,
) -> str:
    lines = [
        "A design decision is required before implementation can proceed.",
        "",
        f"Escalated by: {origin}",
        f"Question: {question}",
        f"Rationale for escalation: {rationale}",
    ]
    if extra_context:
        lines.append("")
        lines.append(extra_context)
    if prior_answer:
        lines.append("")
        lines.append(f"Additional input previously provided: {prior_answer}")
    return "\n".join(lines)


def _build_builder_prompt(
    planner: PlannerResult,
    *,
    required_changes: list[str] | None,
    audit_findings: list[str] | None = None,
    guidance: str | None,
) -> str:
    lines = [
        f"task_id: {planner.task_id}",
        f"objective: {planner.objective}",
        f"rationale: {planner.rationale}",
        "acceptance_criteria:",
    ]
    lines.extend(f"- {c}" for c in planner.acceptance_criteria)
    if planner.relevant_files:
        lines.append("relevant_files:")
        lines.extend(f"- {f}" for f in planner.relevant_files)
    if required_changes:
        lines.append("")
        lines.append("The auditor requested these changes on your previous attempt:")
        lines.extend(f"- {c}" for c in required_changes)
    if audit_findings:
        lines.append("")
        lines.append(
            "Supporting detail from the audit (context for the changes above, "
            "not additional requirements):"
        )
        lines.extend(f"- {f}" for f in audit_findings)
    if guidance:
        lines.append("")
        lines.append(f"Additional guidance from the operator: {guidance}")
    return "\n".join(lines)


def _build_auditor_prompt(
    planner: PlannerResult,
    *,
    integration_branch: str,
    integration_commit: str,
    task_branch: str,
    task_commit: str,
    base_commit: str,
) -> str:
    return "\n".join(
        [
            f"task_id: {planner.task_id}",
            f"objective: {planner.objective}",
            "acceptance_criteria:",
            *[f"- {c}" for c in planner.acceptance_criteria],
            "",
            f"Integration branch: {integration_branch}",
            f"Integration commit: {integration_commit}",
            f"Task branch: {task_branch}",
            f"Task commit: {task_commit}",
            f"Task base commit: {base_commit}",
            "",
            "Suggested inspection commands:",
            f"git diff {base_commit}...{task_commit}",
            f"git log --oneline {base_commit}..{task_commit}",
        ]
    )
