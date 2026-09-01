"""Tests for Supervisor.advance() and the new durable side-effect phases."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from loop_supervisor.git import GitRepo
from loop_supervisor.state import RunOptions, load_state
from loop_supervisor.supervisor import (
    PHASE_AWAITING_INPUT,
    PHASE_BUILDING,
    PHASE_CLEANUP_BRANCH,
    PHASE_CLEANUP_WORKTREE,
    PHASE_CREATING_WORKTREE,
    PHASE_DONE,
    PHASE_MERGING,
    PHASE_OPERATIONAL_FAILURE,
    PHASE_PLANNING,
    PHASE_RECORDING_DECISION,
    AdvanceStatus,
    LoopError,
    Supervisor,
)


def _run(args, cwd):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _init_repo(path: Path) -> GitRepo:
    path.mkdir(parents=True)
    _run(["init", "-b", "main"], path)
    _run(["config", "user.email", "test@example.com"], path)
    _run(["config", "user.name", "Test"], path)
    (path / "README.md").write_text("hello\n")
    _run(["add", "-A"], path)
    _run(["commit", "-m", "initial"], path)
    return GitRepo(path)


class ScriptedRunner:
    def __init__(self, responses: dict[str, list[str]]):
        self.responses = {k: list(v) for k, v in responses.items()}
        self.calls: list[tuple[str, Path]] = []
        self._commit_counter = 0

    def run_agent(self, *, agent, directory, prompt, json_schema=None, timeout=1800.0):
        self.calls.append((agent, directory))
        queue = self.responses.get(agent)
        if not queue:
            raise AssertionError(f"no more scripted responses for agent {agent!r}")
        raw = queue.pop(0)
        if agent == "loop-builder":
            data = json.loads(raw)
            if data.get("status") == "COMPLETE":
                self._commit_counter += 1
                fname = f"change-{self._commit_counter}.txt"
                (directory / fname).write_text("change\n")
                _run(["add", "-A"], directory)
                _run(["commit", "-m", f"builder change {self._commit_counter}"], directory)
                data["commit"] = _run(["rev-parse", "HEAD"], directory).strip()
                raw = json.dumps(data)
        return raw


class ScriptedInput:
    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.requests: list[dict] = []

    def request(self, *, kind, message, context):
        self.requests.append({"kind": kind, "message": message, "context": context})
        if not self.answers:
            return None
        return self.answers.pop(0)


def _planner_ready(task_id="task-1", objective="Do a thing", decision_required=False, **extra):
    payload = {
        "status": "READY",
        "task_id": task_id,
        "objective": objective,
        "rationale": "because",
        "acceptance_criteria": ["works"],
        "relevant_files": [],
        "design_questions": [],
        "decision_required": decision_required,
        "decision_question": "Which approach?" if decision_required else None,
        "decision_rationale": "Ambiguous" if decision_required else None,
    }
    payload.update(extra)
    return json.dumps(payload)


def _planner_complete():
    return json.dumps({"status": "COMPLETE"})


def _builder(status="COMPLETE", task_id="task-1", objective="Do a thing", **extra):
    payload = {
        "status": status,
        "task_id": task_id,
        "objective": objective,
        "implementation_summary": "did the thing",
        "open_concerns": [],
        "commit": None,
    }
    payload.update(extra)
    return json.dumps(payload)


def _auditor(disposition="ACCEPT", task_id="task-1", objective="Do a thing", **extra):
    payload = {
        "disposition": disposition,
        "task_id": task_id,
        "objective": objective,
        "findings": ["needs fixing"] if disposition == "REVISE" else [],
        "required_changes": ["fix it"] if disposition == "REVISE" else [],
        "design_observations": [],
        "decision_required": False,
        "decision_question": None,
        "decision_rationale": None,
    }
    payload.update(extra)
    return json.dumps(payload)


def _architect_decided(question="Which approach?", **extra):
    payload = {
        "status": "DECIDED",
        "question": question,
        "rationale": "because",
        "input_request": None,
        "adr": {
            "title": "ADR: Use approach X",
            "context": "Context here",
            "decision": "Use X",
            "consequences": ["Fast"],
        },
    }
    payload.update(extra)
    return json.dumps(payload)


def _architect_needs_input(question="Which approach?", **extra):
    payload = {
        "status": "NEEDS_INPUT",
        "question": question,
        "rationale": "unclear",
        "adr": None,
        "input_request": "Please pick option A or B",
    }
    payload.update(extra)
    return json.dumps(payload)


def _make_options(**overrides) -> RunOptions:
    defaults: dict[str, Any] = dict(
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
        provision_commands=(),
        provision_timeout=600.0,
        verify_commands=(),
        verify_timeout=900.0,
    )
    defaults.update(overrides)
    return RunOptions(**defaults)


def _make_supervisor(tmp_path, runner, *, input_provider=None, options=None):
    repo_path = tmp_path / "project"
    repo = _init_repo(repo_path)
    common_dir = repo.common_dir()
    supervisor = Supervisor(
        repo=repo,
        runner=runner,
        git_common_dir=common_dir,
        input_provider=input_provider or ScriptedInput([]),
        options=options,
    )
    return supervisor, repo


def test_advance_planning_transitions_to_creating_worktree(tmp_path):
    runner = ScriptedRunner({"loop-planner": [_planner_ready()]})
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()

    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.ADVANCED
    assert outcome.phase_before == PHASE_PLANNING
    assert outcome.phase_after == PHASE_CREATING_WORKTREE
    assert state.pending_worktree_path is not None


def test_advance_creating_worktree_creates_worktree_and_advances(tmp_path):
    runner = ScriptedRunner({"loop-planner": [_planner_ready()]})
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    supervisor.advance(state)

    assert state.phase == PHASE_CREATING_WORKTREE
    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.ADVANCED
    assert state.task_worktree_path is not None
    assert state.phase == PHASE_BUILDING


def test_creating_worktree_uses_persisted_base_even_after_integration_advances(tmp_path):
    """If the integration branch moves after intent is persisted but before
    the worktree is actually created, the worktree must still be created
    from the persisted base commit, not the new integration HEAD."""
    runner = ScriptedRunner({"loop-planner": [_planner_ready()]})
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    supervisor.advance(state)
    assert state.phase == PHASE_CREATING_WORKTREE
    persisted_base = state.pending_worktree_base
    assert persisted_base is not None

    (repo.root / "unrelated.txt").write_text("advance\n")
    _run(["add", "-A"], repo.root)
    _run(["commit", "-m", "integration moved"], repo.root)
    new_head = repo.head_commit()
    assert new_head != persisted_base

    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.ADVANCED
    assert state.task_base_commit == persisted_base
    worktree_head = _run(["rev-parse", "HEAD"], Path(state.task_worktree_path)).strip()
    assert worktree_head == persisted_base


def test_creating_worktree_reconciles_worktree_created_before_crash(tmp_path):
    """Simulate a crash after Git worktree creation but before the resulting
    task identity is saved: reload and advance must recognize the exact
    existing worktree rather than attempting to create another."""
    runner = ScriptedRunner({"loop-planner": [_planner_ready()]})
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    supervisor.advance(state)
    assert state.phase == PHASE_CREATING_WORKTREE

    expected_path = repo.default_worktree_path("task-1")
    expected_branch = repo.branch_name("task-1")
    _run(
        ["worktree", "add", "-b", expected_branch, str(expected_path), state.pending_worktree_base],
        repo.root,
    )

    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.ADVANCED
    assert state.task_worktree_path == str(expected_path)
    assert state.task_branch == expected_branch
    assert state.phase == PHASE_BUILDING

    result = _run(["worktree", "list", "--porcelain"], repo.root)
    assert result.count(str(expected_path)) == 1


def test_creating_worktree_rejects_existing_worktree_past_base(tmp_path):
    """An existing worktree/branch whose HEAD has moved past the persisted
    base (e.g. from a builder commit) must not be silently reused, since it
    would mean skipping the builder phase entirely."""
    runner = ScriptedRunner({"loop-planner": [_planner_ready()]})
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    supervisor.advance(state)
    assert state.phase == PHASE_CREATING_WORKTREE

    expected_path = repo.default_worktree_path("task-1")
    expected_branch = repo.branch_name("task-1")
    _run(
        ["worktree", "add", "-b", expected_branch, str(expected_path), state.pending_worktree_base],
        repo.root,
    )
    (expected_path / "extra.txt").write_text("builder-ish\n")
    _run(["add", "-A"], expected_path)
    _run(["commit", "-m", "moved past base"], expected_path)

    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert state.phase == PHASE_OPERATIONAL_FAILURE
    assert state.task_worktree_path is None


def test_creating_worktree_rejects_dirty_untracked_content(tmp_path):
    """An existing crash-left worktree/branch at exactly the persisted base
    but with untracked content must be rejected: no builder phase has run
    yet, so any such content is unexplained and must not be silently
    adopted as legitimate task state."""
    runner = ScriptedRunner({"loop-planner": [_planner_ready()]})
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    supervisor.advance(state)
    assert state.phase == PHASE_CREATING_WORKTREE

    expected_path = repo.default_worktree_path("task-1")
    expected_branch = repo.branch_name("task-1")
    _run(
        ["worktree", "add", "-b", expected_branch, str(expected_path), state.pending_worktree_base],
        repo.root,
    )
    (expected_path / "mystery.txt").write_text("where did this come from\n")

    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert state.phase == PHASE_OPERATIONAL_FAILURE
    assert state.last_error["kind"] == "git"
    assert state.last_error["requires_repair"] is True
    assert expected_path.exists()
    assert (expected_path / "mystery.txt").exists()


def test_creating_worktree_rejects_dirty_tracked_modification(tmp_path):
    """Same as the untracked case, but for a tracked modification."""
    runner = ScriptedRunner({"loop-planner": [_planner_ready()]})
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    supervisor.advance(state)
    assert state.phase == PHASE_CREATING_WORKTREE

    expected_path = repo.default_worktree_path("task-1")
    expected_branch = repo.branch_name("task-1")
    _run(
        ["worktree", "add", "-b", expected_branch, str(expected_path), state.pending_worktree_base],
        repo.root,
    )
    (expected_path / "README.md").write_text("modified without committing\n")

    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert state.phase == PHASE_OPERATIONAL_FAILURE


def test_creating_worktree_repair_then_resume_succeeds(tmp_path):
    """After a dirty crash-left worktree causes an operational failure, an
    operator who removes the unexpected content must be able to resume
    and have the exact same worktree successfully reconciled."""
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(), _planner_complete()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    supervisor.advance(state)
    assert state.phase == PHASE_CREATING_WORKTREE

    expected_path = repo.default_worktree_path("task-1")
    expected_branch = repo.branch_name("task-1")
    _run(
        ["worktree", "add", "-b", expected_branch, str(expected_path), state.pending_worktree_base],
        repo.root,
    )
    (expected_path / "mystery.txt").write_text("unexpected\n")

    outcome = supervisor.advance(state)
    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE

    reloaded = load_state(repo.common_dir(), state.run_id)

    (expected_path / "mystery.txt").unlink()

    supervisor2 = Supervisor(
        repo=repo,
        runner=runner,
        git_common_dir=repo.common_dir(),
        input_provider=ScriptedInput([]),
    )
    resumed = supervisor2.resume(reloaded)
    retry_outcome = supervisor2.advance(resumed)
    assert retry_outcome.status == AdvanceStatus.ADVANCED
    assert resumed.phase == PHASE_CREATING_WORKTREE

    reconcile_outcome = supervisor2.advance(resumed)
    assert reconcile_outcome.status == AdvanceStatus.ADVANCED
    assert resumed.task_worktree_path == str(expected_path)
    assert resumed.task_branch == expected_branch
    assert resumed.phase == PHASE_BUILDING

    final = supervisor2.run(resumed)
    assert final.phase == PHASE_DONE


def test_creating_worktree_rejects_partial_state(tmp_path):
    """Path exists but branch missing (or vice versa) must fail closed."""
    runner = ScriptedRunner({"loop-planner": [_planner_ready()]})
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    supervisor.advance(state)
    assert state.phase == PHASE_CREATING_WORKTREE

    expected_path = repo.default_worktree_path("task-1")
    expected_path.mkdir(parents=True)
    (expected_path / "placeholder.txt").write_text("not a real worktree\n")

    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert state.phase == PHASE_OPERATIONAL_FAILURE
    assert state.task_worktree_path is None


def test_advance_invokes_exactly_one_role(tmp_path):
    runner = ScriptedRunner({"loop-planner": [_planner_ready()]})
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()

    supervisor.advance(state)

    planner_calls = [c for c in runner.calls if c[0] == "loop-planner"]
    assert len(planner_calls) == 1


def test_advance_terminal_is_idempotent(tmp_path):
    runner = ScriptedRunner({"loop-planner": [_planner_complete()]})
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    supervisor.advance(state)

    outcome1 = supervisor.advance(state)
    outcome2 = supervisor.advance(state)

    assert outcome1.status == AdvanceStatus.TERMINAL
    assert outcome2.status == AdvanceStatus.TERMINAL
    assert state.phase == PHASE_DONE


def test_advance_input_required_when_no_provider_answer(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready()],
            "loop-builder": [_builder(status="BLOCKED", open_concerns=["unclear"])],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner, input_provider=ScriptedInput([]))
    state = supervisor.start_new_run()
    supervisor.advance(state)
    supervisor.advance(state)

    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.INPUT_REQUIRED
    assert state.phase == PHASE_AWAITING_INPUT

    outcome2 = supervisor.advance(state)
    assert outcome2.status == AdvanceStatus.INPUT_UNAVAILABLE


def test_advance_routes_answer_in_separate_transition(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(), _planner_complete()],
            "loop-builder": [
                _builder(status="BLOCKED", open_concerns=["unclear"]),
                _builder(status="COMPLETE"),
            ],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner, input_provider=ScriptedInput(["proceed"]))
    state = supervisor.start_new_run()
    supervisor.advance(state)
    supervisor.advance(state)
    supervisor.advance(state)

    assert state.phase == PHASE_AWAITING_INPUT
    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.ADVANCED
    assert state.phase == PHASE_BUILDING


def test_advance_operational_failure_persists_last_error(tmp_path):
    from loop_supervisor.opencode import AgentInvocationError

    class FailingRunner:
        def run_agent(self, **_):
            raise AgentInvocationError("network error")

    supervisor, repo = _make_supervisor(tmp_path, FailingRunner())
    state = supervisor.start_new_run()

    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert state.phase == PHASE_OPERATIONAL_FAILURE
    assert state.last_error is not None
    assert state.last_error["retryable"] is True


def test_operational_failure_message_redacts_secret_from_environment(tmp_path, monkeypatch):
    """A durable OperationalErrorRecord must not carry a secret that
    happens to appear in an error message, closing the loop end-to-end
    through _handle_operational_failure() (see _sanitize_message() /
    tests/test_sanitize.py for the unit-level coverage)."""
    from loop_supervisor.opencode import AgentInvocationError

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-" + "x" * 95)
    secret = "sk-ant-api03-" + "x" * 95

    class FailingRunner:
        def run_agent(self, **_):
            raise AgentInvocationError(f"network error: auth rejected for key {secret}")

    supervisor, repo = _make_supervisor(tmp_path, FailingRunner())
    state = supervisor.start_new_run()

    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert state.last_error is not None
    assert secret not in state.last_error["message"]
    assert "[redacted:ANTHROPIC_API_KEY]" in state.last_error["message"]

    reloaded = load_state(repo.common_dir(), state.run_id)
    assert reloaded.last_error is not None
    assert secret not in reloaded.last_error["message"]


def test_advance_retry_operational_failure(tmp_path):
    from loop_supervisor.opencode import AgentInvocationError

    call_count = [0]
    inner = ScriptedRunner({"loop-planner": [_planner_ready()]})

    class FlakyRunner:
        def run_agent(self, *, agent, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise AgentInvocationError("flaky first call")
            return inner.run_agent(agent=agent, **kwargs)

    supervisor, repo = _make_supervisor(tmp_path, FlakyRunner())
    state = supervisor.start_new_run()

    outcome = supervisor.advance(state)
    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert state.phase == PHASE_OPERATIONAL_FAILURE
    assert state.last_error is not None
    assert state.last_error["retry_phase"] == PHASE_PLANNING

    outcome2 = supervisor.advance(state)
    assert outcome2.status == AdvanceStatus.ADVANCED
    assert state.phase == PHASE_PLANNING

    outcome3 = supervisor.advance(state)
    assert outcome3.status == AdvanceStatus.ADVANCED
    assert state.phase == PHASE_CREATING_WORKTREE


def test_exhausted_malformed_output_persists_operational_failure(tmp_path):
    from loop_supervisor.contracts import ContractError

    call_count = [0]

    class AlwaysMalformedRunner:
        def run_agent(self, **_):
            call_count[0] += 1
            return "this is not json at all"

    supervisor, repo = _make_supervisor(tmp_path, AlwaysMalformedRunner())
    state = supervisor.start_new_run()

    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert isinstance(outcome.error, ContractError)
    assert state.phase == PHASE_OPERATIONAL_FAILURE
    assert state.last_error is not None
    assert state.last_error["kind"] == "contract"
    assert state.last_error["retryable"] is True
    assert state.last_error["retry_phase"] == PHASE_PLANNING
    assert state.last_error["failed_phase"] == PHASE_PLANNING
    # One initial call plus one retry (the default malformed_output_retries=1).
    assert call_count[0] == 2


def test_malformed_output_failure_record_survives_reload(tmp_path):
    class AlwaysMalformedRunner:
        def run_agent(self, **_):
            return "not json"

    supervisor, repo = _make_supervisor(tmp_path, AlwaysMalformedRunner())
    state = supervisor.start_new_run()
    supervisor.advance(state)

    assert state.phase == PHASE_OPERATIONAL_FAILURE
    reloaded = load_state(repo.common_dir(), state.run_id)

    assert reloaded.phase == PHASE_OPERATIONAL_FAILURE
    assert reloaded.last_error is not None
    assert reloaded.last_error["kind"] == "contract"
    assert reloaded.last_error["retry_phase"] == PHASE_PLANNING


def test_retry_after_malformed_output_resumes_at_failed_phase(tmp_path):
    call_count = [0]
    inner = ScriptedRunner({"loop-planner": [_planner_ready()]})

    class FlakyMalformedRunner:
        def run_agent(self, *, agent, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 2:
                return "not json"
            return inner.run_agent(agent=agent, **kwargs)

    supervisor, repo = _make_supervisor(tmp_path, FlakyMalformedRunner())
    state = supervisor.start_new_run()

    outcome = supervisor.advance(state)
    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert state.phase == PHASE_OPERATIONAL_FAILURE
    assert state.last_error is not None
    assert state.last_error["retry_phase"] == PHASE_PLANNING

    outcome2 = supervisor.advance(state)
    assert outcome2.status == AdvanceStatus.ADVANCED
    assert state.phase == PHASE_PLANNING

    outcome3 = supervisor.advance(state)
    assert outcome3.status == AdvanceStatus.ADVANCED
    assert state.phase == PHASE_CREATING_WORKTREE


def test_builder_identity_contract_failure_persists_with_building_retry_phase(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(task_id="task-1")],
            "loop-builder": [_builder(task_id="task-WRONG", status="COMPLETE")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    supervisor.advance(state)
    supervisor.advance(state)
    assert state.phase == PHASE_BUILDING

    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert state.phase == PHASE_OPERATIONAL_FAILURE
    assert state.last_error is not None
    assert state.last_error["kind"] == "contract"
    assert state.last_error["failed_phase"] == PHASE_BUILDING
    assert state.last_error["retry_phase"] == PHASE_BUILDING
    assert state.last_error["retryable"] is True


def test_advance_revision_limit_produces_terminal_failure(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready()],
            "loop-builder": [_builder(status="COMPLETE") for _ in range(10)],
            "loop-auditor": [_auditor(disposition="REVISE") for _ in range(10)],
        }
    )
    supervisor, repo = _make_supervisor(
        tmp_path, runner, options=_make_options(max_revisions_per_task=2)
    )
    state = supervisor.start_new_run()

    with pytest.raises(LoopError):
        supervisor.run(state)


def test_advance_decision_approval_not_reinvoked(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(decision_required=True), _planner_complete()],
            "loop-architect": [_architect_decided()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    input_provider = ScriptedInput([])
    supervisor, repo = _make_supervisor(
        tmp_path,
        runner,
        input_provider=input_provider,
        options=_make_options(require_decision_approval=True),
    )
    state = supervisor.start_new_run()
    supervisor.run(state)

    assert state.phase == PHASE_AWAITING_INPUT
    assert state.pending_question["kind"] == "decision_approval"

    arch_calls_before = sum(1 for c in runner.calls if c[0] == "loop-architect")
    input_provider.answers = ["approve"]
    supervisor.run(state)

    arch_calls_after = sum(1 for c in runner.calls if c[0] == "loop-architect")
    assert arch_calls_after == arch_calls_before


def test_rejected_decision_state_reloads_and_resumes_with_feedback(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(decision_required=True)],
            "loop-architect": [_architect_decided(), _architect_decided()],
        }
    )
    input_provider = ScriptedInput([])
    supervisor, repo = _make_supervisor(
        tmp_path,
        runner,
        input_provider=input_provider,
        options=_make_options(require_decision_approval=True),
    )
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_AWAITING_INPUT)
    assert state.pending_question is not None
    assert state.pending_question["kind"] == "decision_approval"

    input_provider.answers = ["no"]
    outcome = supervisor.advance(state)
    assert outcome.status == AdvanceStatus.ADVANCED
    assert state.phase == PHASE_AWAITING_INPUT
    assert state.architect_result is not None
    assert state.architect_result["status"] == "DECIDED"
    assert state.pending_question is not None
    assert state.pending_question["kind"] == "architect_input"

    reloaded = load_state(repo.common_dir(), state.run_id)
    fresh_input = ScriptedInput(["please reconsider"])
    fresh_supervisor = Supervisor(
        repo=repo,
        runner=runner,
        git_common_dir=repo.common_dir(),
        input_provider=fresh_input,
    )
    resumed = fresh_supervisor.resume(reloaded)
    fresh_supervisor.advance(resumed)
    assert resumed.phase == "architecting"
    fresh_supervisor.advance(resumed)
    assert resumed.phase == PHASE_AWAITING_INPUT
    assert resumed.pending_question is not None
    assert resumed.pending_question["kind"] == "decision_approval"


def test_architect_retry_limit_terminal_state_with_guidance_is_reloadable(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(decision_required=True)],
            "loop-architect": [_architect_needs_input(), _architect_needs_input()],
        }
    )
    input_provider = ScriptedInput([])
    supervisor, repo = _make_supervisor(
        tmp_path,
        runner,
        input_provider=input_provider,
        options=_make_options(max_architect_retries=1),
    )
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_AWAITING_INPUT)

    input_provider.answers = ["pick option A"]
    supervisor.advance(state)
    outcome = supervisor.advance(state)
    assert outcome.status == AdvanceStatus.TERMINAL
    assert state.phase == "failed"
    assert state.pending_question is not None
    assert state.pending_question["answer"] == "pick option A"
    assert state.last_error is not None
    assert state.last_error["failed_phase"] == "architecting"

    reloaded = load_state(repo.common_dir(), state.run_id)
    assert reloaded.phase == "failed"
    assert reloaded.pending_question == state.pending_question


def test_advance_recording_decision_phase(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(decision_required=True), _planner_complete()],
            "loop-architect": [_architect_decided()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    final = supervisor.run(state)
    assert final.phase == PHASE_DONE


def test_recording_decision_oserror_becomes_durable_operational_failure(tmp_path, monkeypatch):
    """An ordinary filesystem failure while writing the ADR (permission
    error, disk full, etc.) is wrapped as DecisionError by decisions.py
    (see test_decisions.py for direct OSError-wrapping coverage) and must
    then be classified by advance() as a durable, retryable operational
    failure targeting recording_decision, not escape unclassified."""
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(decision_required=True)],
            "loop-architect": [_architect_decided()],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_RECORDING_DECISION)

    import loop_supervisor.supervisor as supervisor_mod
    from loop_supervisor.decisions import DecisionError

    def _boom(decisions_dir, adr, **kw):
        raise DecisionError("filesystem error writing ADR: simulated I/O error")

    monkeypatch.setattr(supervisor_mod, "write_adr_idempotent", _boom)

    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert state.phase == PHASE_OPERATIONAL_FAILURE
    assert state.last_error["kind"] == "decision"
    assert state.last_error["retry_phase"] == PHASE_RECORDING_DECISION
    assert state.last_error["requires_repair"] is True


def test_resume_recording_decision_rejects_tampered_adr_path(tmp_path):
    """A persisted pending_adr_path pointing outside the active worktree
    (e.g. tampered state file) must be rejected at resume, before
    OpenCode is ever started for this run."""
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(decision_required=True)],
            "loop-architect": [_architect_decided()],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_RECORDING_DECISION)
    assert state.pending_adr_path is not None

    state.pending_adr_path = str(tmp_path / "outside" / "0001-evil.md")

    supervisor2 = Supervisor(
        repo=repo,
        runner=ScriptedRunner({}),
        git_common_dir=repo.common_dir(),
        input_provider=ScriptedInput([]),
    )
    with pytest.raises(LoopError):
        supervisor2.resume(state)


def test_resume_recording_decision_rejects_traversal_in_adr_path(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(decision_required=True)],
            "loop-architect": [_architect_decided()],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_RECORDING_DECISION)
    assert state.pending_adr_path is not None

    worktree_path = Path(state.task_worktree_path)
    state.pending_adr_path = str(worktree_path / "docs" / "decisions" / ".." / ".." / "evil.md")

    supervisor2 = Supervisor(
        repo=repo,
        runner=ScriptedRunner({}),
        git_common_dir=repo.common_dir(),
        input_provider=ScriptedInput([]),
    )
    with pytest.raises(LoopError):
        supervisor2.resume(state)


def test_resume_recording_decision_reconciles_crash_after_write_before_save(tmp_path):
    """Simulate a crash where the ADR file was written to disk by
    _do_recording_decision() but the process died before advance()'s final
    save. The persisted task_status_snapshot is still the pre-write
    snapshot; resume must recognize the exact expected ADR as the only
    permitted drift and let the idempotent writer reconcile it, rather than
    failing closed on the generic status-snapshot mismatch."""
    from loop_supervisor.decisions import write_adr_idempotent
    from loop_supervisor.state import save_state

    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(decision_required=True), _planner_complete()],
            "loop-architect": [_architect_decided()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_RECORDING_DECISION)
    assert state.pending_adr_path is not None
    assert state.pending_adr_hash is not None

    from loop_supervisor.contracts import ArchitectResult

    result = ArchitectResult.model_validate(state.architect_result)
    assert result.adr is not None
    worktree_path = Path(state.task_worktree_path)
    write_adr_idempotent(
        worktree_path / "docs" / "decisions",
        result.adr,
        worktree_root=worktree_path,
        target_path=state.pending_adr_path,
        expected_hash=state.pending_adr_hash,
    )
    target = Path(state.pending_adr_path)
    original_content = target.read_text()

    save_state(repo.common_dir(), state)
    reloaded = load_state(repo.common_dir(), state.run_id)
    assert reloaded.phase == PHASE_RECORDING_DECISION

    supervisor2 = Supervisor(
        repo=repo,
        runner=runner,
        git_common_dir=repo.common_dir(),
        input_provider=ScriptedInput([]),
    )
    resumed = supervisor2.resume(reloaded)
    outcome = supervisor2.advance(resumed)

    assert outcome.status == AdvanceStatus.ADVANCED
    assert target.read_text() == original_content
    assert outcome.phase_after == PHASE_BUILDING

    final = supervisor2.run(resumed)
    assert final.phase == PHASE_DONE


def test_resume_recording_decision_rejects_adr_content_mismatch(tmp_path):
    """If the on-disk ADR at the expected path does not hash to the
    persisted expected_hash, resume must fail closed rather than treat it
    as the reconcilable crash-after-write case."""
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(decision_required=True)],
            "loop-architect": [_architect_decided()],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_RECORDING_DECISION)
    assert state.pending_adr_path is not None

    target = Path(state.pending_adr_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("tampered content\n")

    supervisor2 = Supervisor(
        repo=repo,
        runner=ScriptedRunner({}),
        git_common_dir=repo.common_dir(),
        input_provider=ScriptedInput([]),
    )
    with pytest.raises(LoopError):
        supervisor2.resume(state)


def test_resume_recording_decision_rejects_unrelated_dirty_file(tmp_path):
    """An additional, unrelated worktree change beyond the expected ADR
    file must still fail closed, even though the expected ADR file itself
    was correctly written."""
    from loop_supervisor.decisions import write_adr_idempotent

    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(decision_required=True)],
            "loop-architect": [_architect_decided()],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_RECORDING_DECISION)
    assert state.pending_adr_path is not None
    assert state.pending_adr_hash is not None

    from loop_supervisor.contracts import ArchitectResult

    result = ArchitectResult.model_validate(state.architect_result)
    assert result.adr is not None
    worktree_path = Path(state.task_worktree_path)
    write_adr_idempotent(
        worktree_path / "docs" / "decisions",
        result.adr,
        worktree_root=worktree_path,
        target_path=state.pending_adr_path,
        expected_hash=state.pending_adr_hash,
    )
    (worktree_path / "unrelated.txt").write_text("sneaky\n")

    supervisor2 = Supervisor(
        repo=repo,
        runner=ScriptedRunner({}),
        git_common_dir=repo.common_dir(),
        input_provider=ScriptedInput([]),
    )
    with pytest.raises(LoopError):
        supervisor2.resume(state)


def test_run_compatibility_loop_still_works(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(), _planner_complete()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    final = supervisor.run(state)
    assert final.phase == PHASE_DONE
    assert final.accepted_task_count == 1


def _advance_to_phase(supervisor, state, target_phase):
    """Advance until state.phase == target_phase or terminal/failure."""
    from loop_supervisor.supervisor import _TERMINAL_PHASES, PHASE_OPERATIONAL_FAILURE

    max_steps = 30
    for _ in range(max_steps):
        if state.phase == target_phase:
            return
        if state.phase in _TERMINAL_PHASES or state.phase == PHASE_OPERATIONAL_FAILURE:
            raise AssertionError(
                f"Reached terminal/failure {state.phase!r} before {target_phase!r}"
            )
        supervisor.advance(state)
    raise AssertionError(f"Did not reach {target_phase!r} after {max_steps} advances")


def test_merging_reconciles_crash_after_git_merge_before_state_save(tmp_path):
    """Simulate a crash where Git committed the merge but merge_commit was
    never saved: resuming in the merging phase must recognize the exact
    existing merge rather than merging a second time."""
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_MERGING)
    assert state.merge_commit is None

    worktree = supervisor._active_worktree
    real_merge_commit = repo.merge_task_branch(worktree)

    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.ADVANCED
    assert state.merge_commit == real_merge_commit
    assert state.phase == PHASE_CLEANUP_WORKTREE

    log = _run(["log", "--oneline", "--all"], repo.root)
    assert log.count(real_merge_commit[:7]) == 1


def test_merging_rejects_task_branch_moved_after_audit(tmp_path):
    """If the task branch advances after the merge_task_head snapshot was
    taken (e.g. a stray commit), merging must refuse to integrate the
    moved, unreviewed tip."""
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_MERGING)

    worktree = supervisor._active_worktree
    (worktree.path / "unreviewed.txt").write_text("sneaky\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "unreviewed change after audit"], worktree.path)

    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert state.phase == PHASE_OPERATIONAL_FAILURE
    assert repo.branch_exists(worktree.branch)


def test_cleanup_rejects_merge_commit_with_wrong_second_parent(tmp_path):
    """If merge_commit is tampered or corrupted so its second parent is not
    merge_task_head, cleanup must refuse to remove the worktree/branch."""
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_CLEANUP_WORKTREE)
    assert state.merge_commit is not None

    state.merge_task_head = "0" * 40

    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert state.phase == PHASE_OPERATIONAL_FAILURE


def test_cleanup_worktree_state_reloadable(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_CLEANUP_WORKTREE)

    assert state.task_worktree_path is not None
    assert state.task_branch is not None
    assert state.original_task_id is not None
    assert state.merge_commit is not None

    reloaded = load_state(repo.common_dir(), state.run_id)
    assert reloaded.phase == PHASE_CLEANUP_WORKTREE
    assert reloaded.task_worktree_path == state.task_worktree_path
    assert reloaded.task_branch == state.task_branch
    assert reloaded.merge_commit == state.merge_commit


def test_cleanup_branch_state_reloadable(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_CLEANUP_BRANCH)

    assert state.task_branch is not None
    assert state.original_task_id is not None
    assert state.task_worktree_path is not None

    reloaded = load_state(repo.common_dir(), state.run_id)
    assert reloaded.phase == PHASE_CLEANUP_BRANCH
    assert reloaded.task_branch == state.task_branch
    assert reloaded.merge_commit == state.merge_commit


def test_cleanup_branch_failure_persists_with_absent_worktree(tmp_path):
    """A GitError during cleanup_branch (after the worktree is already
    removed) must persist as a retryable operational_failure, not blow up
    failure persistence by trying to inspect the missing worktree."""
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_CLEANUP_BRANCH)

    worktree_path = Path(state.task_worktree_path)
    task_branch = state.task_branch
    expected_head_before = state.task_expected_head
    assert not worktree_path.exists()
    assert repo.branch_exists(task_branch)

    from loop_supervisor.git import GitError

    def _boom(_worktree):
        raise GitError("simulated branch deletion failure")

    repo.delete_task_branch_only = _boom  # type: ignore[method-assign]

    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert state.phase == PHASE_OPERATIONAL_FAILURE
    assert state.last_error is not None
    assert state.last_error["kind"] == "git"
    assert state.last_error["failed_phase"] == PHASE_CLEANUP_BRANCH
    assert state.last_error["retry_phase"] == PHASE_CLEANUP_BRANCH
    assert state.task_expected_head == expected_head_before
    assert state.task_status_snapshot is None
    assert not worktree_path.exists()
    assert repo.branch_exists(task_branch)

    reloaded = load_state(repo.common_dir(), state.run_id)
    assert reloaded.phase == PHASE_OPERATIONAL_FAILURE
    assert reloaded.last_error is not None
    assert reloaded.last_error["failed_phase"] == PHASE_CLEANUP_BRANCH
    assert reloaded.last_error["retry_phase"] == PHASE_CLEANUP_BRANCH
    assert reloaded.task_status_snapshot is None


def test_cleanup_branch_failure_resumes_and_completes(tmp_path):
    """After a persisted cleanup_branch failure, a fresh Supervisor must
    resume despite the missing worktree and complete branch cleanup."""
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(), _planner_complete()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_CLEANUP_BRANCH)

    task_branch = state.task_branch

    from loop_supervisor.git import GitError

    calls = {"n": 0}
    original_delete = repo.delete_task_branch_only

    def _flaky(worktree):
        calls["n"] += 1
        if calls["n"] == 1:
            raise GitError("simulated one-shot branch deletion failure")
        return original_delete(worktree)

    repo.delete_task_branch_only = _flaky  # type: ignore[method-assign]

    outcome = supervisor.advance(state)
    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE

    reloaded = load_state(repo.common_dir(), state.run_id)

    supervisor2 = Supervisor(
        repo=repo,
        runner=ScriptedRunner({"loop-planner": [_planner_complete()]}),
        git_common_dir=repo.common_dir(),
        input_provider=ScriptedInput([]),
    )
    resumed = supervisor2.resume(reloaded)

    unwrap = supervisor2.advance(resumed)
    assert unwrap.status == AdvanceStatus.ADVANCED
    assert resumed.phase == PHASE_CLEANUP_BRANCH

    final = supervisor2.run(resumed)
    assert final.accepted_task_count == 1
    assert final.original_task_id is None
    assert not repo.branch_exists(task_branch)


def test_cleanup_branch_retry_rejects_moved_branch(tmp_path):
    """Making cleanup_branch failure persistence work must not weaken
    cleanup safety: a surviving task branch that has moved off the reviewed
    merge head must be rejected on resume."""
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_CLEANUP_BRANCH)

    task_branch = state.task_branch

    from loop_supervisor.git import GitError

    def _boom(_worktree):
        raise GitError("simulated branch deletion failure")

    repo.delete_task_branch_only = _boom  # type: ignore[method-assign]
    supervisor.advance(state)

    reloaded = load_state(repo.common_dir(), state.run_id)

    # Move the surviving branch tip to a new commit off the reviewed head.
    _run(["checkout", task_branch], repo.root)
    (repo.root / "sneaky.txt").write_text("moved\n")
    _run(["add", "-A"], repo.root)
    _run(["commit", "-m", "move branch tip"], repo.root)
    _run(["checkout", "main"], repo.root)

    supervisor2 = Supervisor(
        repo=repo,
        runner=ScriptedRunner({}),
        git_common_dir=repo.common_dir(),
        input_provider=ScriptedInput([]),
    )
    with pytest.raises(LoopError):
        supervisor2.resume(reloaded)


def test_cleanup_does_not_double_increment_accepted_count(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(), _planner_complete()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    final = supervisor.run(state)
    assert final.accepted_task_count == 1


def test_cleanup_worktree_dirty_after_merge_is_preserved(tmp_path):
    """If the task worktree is dirtied (accidentally or otherwise) after the
    reviewed commit is merged, cleanup_worktree must not delete it. The
    transition must surface as a retryable operational failure with the
    worktree, its dirty content, and the task branch all still present."""
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_CLEANUP_WORKTREE)

    worktree_path = Path(state.task_worktree_path)
    task_branch = state.task_branch
    (worktree_path / "unreviewed.txt").write_text("dirty\n")

    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert state.phase == PHASE_OPERATIONAL_FAILURE
    assert state.last_error["kind"] == "git"
    assert state.last_error["retry_phase"] == PHASE_CLEANUP_WORKTREE
    assert worktree_path.exists()
    assert (worktree_path / "unreviewed.txt").exists()
    assert repo.branch_exists(task_branch)

    reloaded = load_state(repo.common_dir(), state.run_id)
    assert reloaded.phase == PHASE_OPERATIONAL_FAILURE

    supervisor2 = Supervisor(
        repo=repo,
        runner=ScriptedRunner({}),
        git_common_dir=repo.common_dir(),
        input_provider=ScriptedInput([]),
    )
    resumed = supervisor2.resume(reloaded)
    assert (worktree_path / "unreviewed.txt").exists()

    retry_outcome = supervisor2.advance(resumed)
    assert retry_outcome.status == AdvanceStatus.ADVANCED
    assert resumed.phase == PHASE_CLEANUP_WORKTREE

    final_outcome = supervisor2.advance(resumed)
    assert final_outcome.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert worktree_path.exists()
    assert (worktree_path / "unreviewed.txt").exists()


def test_cleanup_worktree_dirty_repair_then_resume_succeeds(tmp_path):
    """After a dirty-worktree operational failure, an operator who deletes
    the unreviewed file (repairing the worktree back to clean, matching
    the reviewed merge) must be able to successfully resume and complete
    cleanup. Resume validation must not require the status snapshot to
    still equal the dirty snapshot recorded at the moment of failure."""
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(), _planner_complete()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_CLEANUP_WORKTREE)

    worktree_path = Path(state.task_worktree_path)
    task_branch = state.task_branch
    (worktree_path / "unreviewed.txt").write_text("dirty\n")

    outcome = supervisor.advance(state)
    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert state.last_error["requires_repair"] is True

    reloaded = load_state(repo.common_dir(), state.run_id)

    # Operator repairs the worktree: delete the unreviewed file, restoring
    # it to exactly the reviewed merge's clean state.
    (worktree_path / "unreviewed.txt").unlink()

    supervisor2 = Supervisor(
        repo=repo,
        runner=runner,
        git_common_dir=repo.common_dir(),
        input_provider=ScriptedInput([]),
    )
    resumed = supervisor2.resume(reloaded)
    final = supervisor2.run(resumed)

    assert final.phase == PHASE_DONE
    assert final.accepted_task_count == 1
    assert not worktree_path.exists()
    assert not repo.branch_exists(task_branch)


def test_cleanup_worktree_repair_rejects_moved_task_head(tmp_path):
    """A repaired worktree whose HEAD has moved (new commit added) rather
    than just having its dirty file removed must still be rejected: repair
    means reaching the exact reviewed clean state, not any clean state."""
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_CLEANUP_WORKTREE)

    worktree_path = Path(state.task_worktree_path)
    (worktree_path / "unreviewed.txt").write_text("dirty\n")
    outcome = supervisor.advance(state)
    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE

    reloaded = load_state(repo.common_dir(), state.run_id)

    # "Repair" by committing the unreviewed file instead of discarding it:
    # this moves the task branch tip past the reviewed merge_task_head.
    _run(["add", "-A"], worktree_path)
    _run(["commit", "-m", "sneaky extra commit"], worktree_path)

    supervisor2 = Supervisor(
        repo=repo,
        runner=ScriptedRunner({}),
        git_common_dir=repo.common_dir(),
        input_provider=ScriptedInput([]),
    )
    resumed = supervisor2.resume(reloaded)
    retry_outcome = supervisor2.advance(resumed)
    assert retry_outcome.status == AdvanceStatus.ADVANCED
    assert resumed.phase == PHASE_CLEANUP_WORKTREE

    final_outcome = supervisor2.advance(resumed)
    assert final_outcome.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert worktree_path.exists()


def test_cleanup_worktree_resume_then_complete(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_CLEANUP_WORKTREE)

    reloaded = load_state(repo.common_dir(), state.run_id)
    supervisor2 = Supervisor(
        repo=repo,
        runner=ScriptedRunner({"loop-planner": [_planner_complete()]}),
        git_common_dir=repo.common_dir(),
        input_provider=ScriptedInput([]),
    )
    state2 = supervisor2.resume(reloaded)
    final = supervisor2.run(state2)
    assert final.phase == PHASE_DONE
    assert final.accepted_task_count == 1


def test_merge_conflict_goes_to_operational_failure(tmp_path):
    from loop_supervisor.git import MergeConflictError

    conflict_raised = [False]

    class ConflictRepo:
        def __init__(self, real_repo):
            self._real = real_repo
            for attr in dir(real_repo):
                if not attr.startswith("_") and not hasattr(self, attr):
                    setattr(self, attr, getattr(real_repo, attr))

        def reconcile_or_merge_task(self, *, pre_head, task_head):
            conflict_raised[0] = True
            raise MergeConflictError(task_head, "simulated conflict")

        def __getattr__(self, name):
            return getattr(self._real, name)

    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_MERGING)

    supervisor._active_worktree.path  # noqa

    conflict_repo = ConflictRepo(repo)
    supervisor.repo = conflict_repo

    outcome = supervisor.advance(state)
    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert state.phase == PHASE_OPERATIONAL_FAILURE
    assert state.last_error is not None
    assert state.last_error["kind"] == "merge_conflict"
    assert state.last_error["requires_repair"] is True
    assert state.last_error["retry_phase"] == PHASE_MERGING


def test_operator_guidance_survives_a_retried_building_phase(tmp_path):
    """Regression test for backlog item 24: _do_building previously
    cleared state.pending_question (consuming the operator's guidance
    answer) before invoking the builder agent. If that call then failed
    and the phase retried, the guidance was gone -- the operator had to
    resupply it with no indication why. Guidance must now survive an
    operational failure discovered after it was read but before the
    builder produced a usable result."""
    from loop_supervisor.opencode import AgentInvocationError

    inner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(), _planner_complete()],
            "loop-builder": [
                _builder(status="BLOCKED", open_concerns=["unclear"]),
                _builder(status="COMPLETE"),
            ],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    prompts: list[str] = []
    call_count = [0]

    class FlakyRunner:
        def run_agent(self, *, agent, prompt, **kwargs):
            if agent == "loop-builder":
                prompts.append(prompt)
                call_count[0] += 1
                if call_count[0] == 2:
                    # Fails on the first *retried* building attempt, i.e.
                    # the call made after guidance has already been read
                    # (and, before the fix, already cleared).
                    raise AgentInvocationError("flaky builder call")
            return inner.run_agent(agent=agent, prompt=prompt, **kwargs)

    supervisor, repo = _make_supervisor(
        tmp_path, FlakyRunner(), input_provider=ScriptedInput(["use approach B"])
    )
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_AWAITING_INPUT)

    outcome = supervisor.advance(state)  # awaiting_input -> building (guidance read)
    assert outcome.status == AdvanceStatus.ADVANCED
    assert state.phase == PHASE_BUILDING
    assert state.pending_question is not None
    assert state.pending_question["answer"] == "use approach B"

    outcome2 = supervisor.advance(state)  # building fails (flaky)
    assert outcome2.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert state.phase == PHASE_OPERATIONAL_FAILURE
    assert state.pending_question is not None
    assert state.pending_question["answer"] == "use approach B"

    outcome3 = supervisor.advance(state)  # operational_failure -> building (retry)
    assert outcome3.status == AdvanceStatus.ADVANCED
    assert state.phase == PHASE_BUILDING
    assert state.pending_question is not None
    assert state.pending_question["answer"] == "use approach B"

    outcome4 = supervisor.advance(state)  # building succeeds this time
    assert outcome4.status == AdvanceStatus.ADVANCED

    assert any("use approach B" in p for p in prompts)
    assert state.pending_question is None


def test_operator_guidance_survives_a_retried_architecting_phase(tmp_path):
    """Regression test for backlog item 24: _do_architecting previously
    cleared state.pending_question (consuming prior_answer) before
    invoking the architect agent. If that call then failed and the phase
    retried, the operator's prior answer was gone."""
    from loop_supervisor.opencode import AgentInvocationError

    inner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(decision_required=True), _planner_complete()],
            "loop-architect": [_architect_needs_input(), _architect_decided()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    prompts: list[str] = []
    call_count = [0]

    class FlakyRunner:
        def run_agent(self, *, agent, prompt, **kwargs):
            if agent == "loop-architect":
                prompts.append(prompt)
                call_count[0] += 1
                if call_count[0] == 2:
                    raise AgentInvocationError("flaky architect call")
            return inner.run_agent(agent=agent, prompt=prompt, **kwargs)

    supervisor, repo = _make_supervisor(
        tmp_path, FlakyRunner(), input_provider=ScriptedInput(["pick option A"])
    )
    state = supervisor.start_new_run()
    _advance_to_phase(supervisor, state, PHASE_AWAITING_INPUT)

    outcome = supervisor.advance(state)  # awaiting_input -> architecting (answer read)
    assert outcome.status == AdvanceStatus.ADVANCED
    assert state.phase == "architecting"
    assert state.pending_question is not None
    assert state.pending_question["answer"] == "pick option A"

    outcome2 = supervisor.advance(state)  # architecting fails (flaky)
    assert outcome2.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert state.pending_question is not None
    assert state.pending_question["answer"] == "pick option A"

    outcome3 = supervisor.advance(state)  # operational_failure -> architecting (retry)
    assert outcome3.status == AdvanceStatus.ADVANCED

    assert any("pick option A" in p for p in prompts)


def test_post_transition_save_failure_is_classified_not_escaped(tmp_path):
    """Regression test for backlog item 2: advance()'s success-path
    _save() call previously sat outside its try/except, so a GitError
    raised by _save() -> _checkpoint() -> repo.head_commit()/
    status_snapshot() after an otherwise-successful phase transition
    escaped unclassified -- no OperationalErrorRecord, no retry
    classification -- rather than being handled like any other
    operational failure discovered during the same advance() call."""
    from loop_supervisor.git import GitError

    runner = ScriptedRunner({"loop-planner": [_planner_ready()]})
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()

    call_count = [0]
    real_status_snapshot = repo.status_snapshot

    class FlakyRepo:
        def status_snapshot(self, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise GitError("simulated checkpoint failure")
            return real_status_snapshot(**kwargs)

        def __getattr__(self, name):
            return getattr(repo, name)

    supervisor.repo = FlakyRepo()

    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert state.phase == PHASE_OPERATIONAL_FAILURE
    assert state.last_error is not None
    assert state.last_error["retryable"] is True
    # The dispatch itself succeeded and had already advanced state.phase
    # to creating_worktree in memory before the save failed; the retry
    # target must be that already-completed transition, not the
    # planning phase the dispatch ran from.
    assert state.last_error["retry_phase"] == PHASE_CREATING_WORKTREE

    reloaded = load_state(repo.common_dir(), state.run_id)
    assert reloaded.phase == PHASE_OPERATIONAL_FAILURE
    assert reloaded.last_error is not None
    assert reloaded.last_error["retry_phase"] == PHASE_CREATING_WORKTREE

    outcome2 = supervisor.advance(state)
    assert outcome2.status == AdvanceStatus.ADVANCED
    assert state.phase == PHASE_CREATING_WORKTREE


def test_post_transition_save_failure_after_terminal_transition_retries_phase_before(
    tmp_path,
):
    """Regression test for a defect that the naive fix for item 2 would
    have introduced: if a dispatch transitions state.phase straight to a
    terminal phase (_do_planning -> PHASE_DONE) and the subsequent save
    then fails, classifying the failure against the terminal phase would
    build an invalid OperationalErrorRecord (RETRY_TARGET_PHASES
    excludes terminal phases). The retry target must fall back to
    phase_before (planning) instead."""
    from loop_supervisor.git import GitError

    # Planning has no side effects (it is not in _DURABLE_SIDE_EFFECT_PHASES),
    # so retrying it from scratch after this save failure is safe -- but it
    # does mean the planner is invoked a second time; two identical
    # COMPLETE responses are queued for that reason.
    runner = ScriptedRunner({"loop-planner": [_planner_complete(), _planner_complete()]})
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()

    call_count = [0]
    real_status_snapshot = repo.status_snapshot

    class FlakyRepo:
        def status_snapshot(self, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise GitError("simulated checkpoint failure")
            return real_status_snapshot(**kwargs)

        def __getattr__(self, name):
            return getattr(repo, name)

    supervisor.repo = FlakyRepo()

    outcome = supervisor.advance(state)

    assert outcome.status == AdvanceStatus.OPERATIONAL_FAILURE
    assert state.phase == PHASE_OPERATIONAL_FAILURE
    assert state.last_error is not None
    assert state.last_error["retryable"] is True
    assert state.last_error["retry_phase"] == PHASE_PLANNING

    reloaded = load_state(repo.common_dir(), state.run_id)
    assert reloaded.last_error is not None
    assert reloaded.last_error["retry_phase"] == PHASE_PLANNING

    outcome2 = supervisor.advance(state)  # operational_failure -> planning
    assert outcome2.status == AdvanceStatus.ADVANCED
    assert state.phase == PHASE_PLANNING

    outcome3 = supervisor.advance(state)  # planning -> done, this time saved
    assert outcome3.status == AdvanceStatus.ADVANCED
    assert state.phase == PHASE_DONE
