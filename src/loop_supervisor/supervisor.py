"""The loop state machine: planner -> optional architect -> builder ->
auditor -> supervisor merge.

This module is deliberately decoupled from the OpenCode HTTP/process
details (see opencode.py) and from real Git side effects being
irreversible without inspection: it depends only on `AgentRunner` and
`GitRepo`, both of which can be faked in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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
from .decisions import write_adr
from .git import GitError, GitRepo, MergeConflictError, TaskWorktree
from .opencode import AgentRunner
from .state import DecisionRequest, RunOptions, RunState, save_state

PHASE_PLANNING = "planning"
PHASE_ARCHITECTING = "architecting"
PHASE_BUILDING = "building"
PHASE_AUDITING = "auditing"
PHASE_AWAITING_INPUT = "awaiting_input"
PHASE_DONE = "done"
PHASE_FAILED = "failed"

_TERMINAL_PHASES = {PHASE_DONE, PHASE_FAILED}


class LoopError(RuntimeError):
    """Raised for unrecoverable loop-level failures (limits, conflicts)."""


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

        task_fields = (
            state.original_task_id,
            state.task_worktree_path,
            state.task_branch,
            state.task_base_commit,
        )
        has_task = any(f is not None for f in task_fields)
        if has_task:
            if state.task_expected_head is None:
                raise LoopError("resume task state is missing an expected HEAD checkpoint")
            worktree = TaskWorktree(
                path=Path(state.task_worktree_path or ""),
                branch=state.task_branch or "",
                original_task_id=state.original_task_id or "",
                base_commit=state.task_base_commit or "",
            )
            try:
                self.repo.validate_task_worktree(worktree, expected_head=state.task_expected_head)
            except GitError as exc:
                raise LoopError(f"resume task worktree validation failed: {exc}") from exc
            if state.task_status_snapshot is not None:
                actual_snapshot = self.repo.status_snapshot(cwd=worktree.path)
                if actual_snapshot != state.task_status_snapshot:
                    raise LoopError(
                        "resume task worktree has changed since it was paused "
                        "(working-tree status snapshot mismatch)"
                    )
        elif state.phase in (PHASE_ARCHITECTING, PHASE_BUILDING, PHASE_AUDITING):
            raise LoopError(f"resume phase {state.phase!r} requires an active task worktree")

    def _checkpoint(self, state: RunState) -> None:
        """Refresh Git checkpoints before saving. Called on every phase
        transition so resume can detect any external change since the
        last save."""
        state.integration_expected_head = self.repo.head_commit()
        state.integration_status_snapshot = self.repo.status_snapshot()
        if state.task_worktree_path is not None:
            worktree_path = Path(state.task_worktree_path)
            state.task_expected_head = self.repo.head_commit(cwd=worktree_path)
            state.task_status_snapshot = self.repo.status_snapshot(cwd=worktree_path)
        else:
            state.task_expected_head = None
            state.task_status_snapshot = None

    def run(self, state: RunState) -> RunState:
        while state.phase not in _TERMINAL_PHASES:
            if state.phase == PHASE_AWAITING_INPUT:
                if not self._try_resolve_pending_input(state):
                    self._save(state)
                    return state
            elif state.phase == PHASE_PLANNING:
                self._do_planning(state)
            elif state.phase == PHASE_ARCHITECTING:
                self._do_architecting(state)
            elif state.phase == PHASE_BUILDING:
                self._do_building(state)
            elif state.phase == PHASE_AUDITING:
                self._do_auditing(state)
            else:
                raise LoopError(f"unknown phase {state.phase!r}")
            self._save(state)
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
        original_task_id = state.original_task_id or result.task_id

        if state.task_worktree_path is None:
            worktree = self.repo.create_task_worktree(
                original_task_id, worktree_root=self.worktree_root
            )
            state.original_task_id = worktree.original_task_id
            state.task_worktree_path = str(worktree.path)
            state.task_branch = worktree.branch
            state.task_base_commit = worktree.base_commit
            self._active_worktree = worktree

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
            answer = self.input_provider.request(
                kind="decision_approval",
                message=f"Approve this decision?\n\n{result.adr.title}\n\n{result.adr.decision}",
                context={},
            )
            if answer is None:
                state.pending_question = {
                    "kind": "decision_approval",
                    "message": "Approve the proposed architecture decision?",
                    "context": {},
                }
                state.phase = PHASE_AWAITING_INPUT
                return
            if answer.strip().lower() not in ("y", "yes", "approve"):
                state.pending_question = {
                    "kind": "architect_input",
                    "message": "Provide feedback on the rejected decision proposal.",
                    "context": {"question": decision_request.question},
                }
                state.phase = PHASE_AWAITING_INPUT
                return

        self._record_decision(state)

    def _record_decision(self, state: RunState) -> None:
        """Write the already-persisted, approved architect proposal exactly
        as recorded, and route to the correct continuation based on who
        escalated the decision. Never re-invokes the architect: approval
        consumes the existing proposal, it does not request a new one."""
        if state.architect_result is None:
            raise LoopError("no architect result recorded to approve")
        if state.decision_request is None:
            raise LoopError("no active decision request recorded to resolve")
        result = ArchitectResult.model_validate(state.architect_result)
        if result.adr is None:
            raise LoopError("architect result has no adr to record")

        directory = self._active_directory(state)
        write_adr(directory / self.decisions_subpath, result.adr)

        origin = DecisionRequest.from_dict(state.decision_request).origin
        state.decision_request = None
        state.architect_retry_count = 0
        state.phase = PHASE_BUILDING if origin == "planner" else PHASE_PLANNING

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

        auditor_findings = None
        if state.auditor_result is not None:
            auditor = AuditorResult.model_validate(state.auditor_result)
            if auditor.disposition is AuditorDisposition.REVISE:
                auditor_findings = auditor.required_changes

        prompt = _build_builder_prompt(planner, findings=auditor_findings, guidance=guidance)
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
            verified_head = self.repo.verify_builder_commit(worktree, result.commit or "")
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
            self._merge_accepted(state, worktree)
            return

        if result.disposition is AuditorDisposition.REVISE:
            state.revision_count += 1
            if state.revision_count > self.limits.max_revisions_per_task:
                raise LoopError(
                    f"task {worktree.original_task_id!r} exceeded "
                    f"{self.limits.max_revisions_per_task} revisions"
                )
            state.phase = PHASE_BUILDING
            return

        # REPLAN
        state.replan_count += 1
        if state.replan_count > self.limits.max_replans_per_task:
            raise LoopError(
                f"task {worktree.original_task_id!r} exceeded "
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

    def _merge_accepted(self, state: RunState, worktree: TaskWorktree) -> None:
        try:
            self.repo.merge_task_branch(worktree)
        except MergeConflictError as exc:
            state.phase = PHASE_FAILED
            state.pending_question = {
                "kind": "merge_conflict",
                "message": str(exc),
                "context": {"task_branch": worktree.branch},
            }
            raise LoopError(str(exc)) from exc

        self.repo.remove_task_worktree(worktree)
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
                self._record_decision(state)
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
    findings: list[str] | None,
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
    if findings:
        lines.append("")
        lines.append("The auditor requested these changes on your previous attempt:")
        lines.extend(f"- {f}" for f in findings)
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
