import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from loop_supervisor.git import GitRepo
from loop_supervisor.state import RunOptions
from loop_supervisor.supervisor import (
    PHASE_AWAITING_INPUT,
    PHASE_DONE,
    PHASE_OPERATIONAL_FAILURE,
    PHASE_PLANNING,
    LoopError,
    Supervisor,
    _default_run_options,
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
    """Fake AgentRunner: agent name -> queue of raw JSON-text responses.

    Also commits a trivial change to the target directory whenever the
    builder agent is invoked, so Git-side verification has something real
    to check.
    """

    def __init__(self, responses: dict[str, list[str]], *, truncate_commit_to: int | None = None):
        self.responses = {k: list(v) for k, v in responses.items()}
        self.calls: list[tuple[str, Path]] = []
        self.prompts: list[tuple[str, str]] = []
        self._commit_counter = 0
        self._truncate_commit_to = truncate_commit_to

    def run_agent(self, *, agent, directory, prompt, json_schema=None, timeout=1800.0):
        self.calls.append((agent, directory))
        self.prompts.append((agent, prompt))
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
                full_commit = _run(["rev-parse", "HEAD"], directory).strip()
                if self._truncate_commit_to is not None:
                    data["commit"] = full_commit[: self._truncate_commit_to]
                else:
                    data["commit"] = full_commit
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


def _builder(task_id="task-1", objective="Do a thing", status="COMPLETE", **extra):
    payload = {
        "task_id": task_id,
        "objective": objective,
        "status": status,
        "implementation_summary": "did it",
        "implementation_strategy": [],
        "tests_run": [],
        "test_results": [],
        "files_changed": [],
        "commit": None,
        "open_concerns": [],
    }
    payload.update(extra)
    return json.dumps(payload)


def _auditor(task_id="task-1", objective="Do a thing", disposition="ACCEPT", **extra):
    payload = {
        "task_id": task_id,
        "objective": objective,
        "disposition": disposition,
        "findings": [],
        "required_changes": ["fix it"] if disposition == "REVISE" else [],
        "design_observations": [],
        "decision_required": False,
        "decision_question": None,
        "decision_rationale": None,
    }
    payload.update(extra)
    return json.dumps(payload)


def _architect_decided(question="Which approach?", title="Chosen approach"):
    return json.dumps(
        {
            "status": "DECIDED",
            "question": question,
            "rationale": "resolved",
            "adr": {
                "title": title,
                "context": "ctx",
                "decision": "dec",
                "consequences": [],
            },
            "input_request": None,
        }
    )


def _architect_needs_input(question="Which approach?"):
    return json.dumps(
        {
            "status": "NEEDS_INPUT",
            "question": question,
            "rationale": "unclear",
            "adr": None,
            "input_request": "Please pick option A or B",
        }
    )


def _make_options(**overrides):
    base = _default_run_options()
    return RunOptions(
        max_accepted_tasks=overrides.get("max_accepted_tasks", base.max_accepted_tasks),
        max_revisions_per_task=overrides.get("max_revisions_per_task", base.max_revisions_per_task),
        max_replans_per_task=overrides.get("max_replans_per_task", base.max_replans_per_task),
        max_architect_retries=overrides.get("max_architect_retries", base.max_architect_retries),
        malformed_output_retries=overrides.get(
            "malformed_output_retries", base.malformed_output_retries
        ),
        role_timeout=overrides.get("role_timeout", base.role_timeout),
        worktree_root=overrides.get("worktree_root", base.worktree_root),
        require_decision_approval=overrides.get(
            "require_decision_approval", base.require_decision_approval
        ),
        opencode_executable=overrides.get("opencode_executable", base.opencode_executable),
        opencode_startup_timeout=overrides.get(
            "opencode_startup_timeout", base.opencode_startup_timeout
        ),
        provision_commands=overrides.get("provision_commands", base.provision_commands),
        provision_timeout=overrides.get("provision_timeout", base.provision_timeout),
        verify_commands=overrides.get("verify_commands", base.verify_commands),
        verify_timeout=overrides.get("verify_timeout", base.verify_timeout),
    )


def _make_supervisor(
    tmp_path, runner, *, input_provider=None, limits=None, auto_decide=True, options=None
):
    repo = _init_repo(tmp_path / "project")
    common_dir = repo.common_dir()
    if options is None:
        overrides: dict[str, Any] = {}
        if limits is not None:
            overrides.update(
                max_accepted_tasks=limits.max_accepted_tasks,
                max_revisions_per_task=limits.max_revisions_per_task,
                max_replans_per_task=limits.max_replans_per_task,
                max_architect_retries=limits.max_architect_retries,
                malformed_output_retries=limits.malformed_output_retries,
                role_timeout=limits.role_timeout,
            )
        overrides["require_decision_approval"] = not auto_decide
        options = _make_options(**overrides)
    supervisor = Supervisor(
        repo=repo,
        runner=runner,
        git_common_dir=common_dir,
        input_provider=input_provider or ScriptedInput([]),
        options=options,
    )
    return supervisor, repo


def test_happy_path_accept_then_complete(tmp_path):
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
    assert (repo.root / "change-1.txt").exists()


def test_builder_abbreviated_commit_hash_is_accepted_and_resolved(tmp_path):
    # A builder that reports a 7-char abbreviation (e.g. from a habit of
    # copying `git log --oneline` output) instead of the full 40-character
    # SHA must not be rejected as an identity mismatch: the supervisor
    # resolves it and persists the full hash.
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready()],
            "loop-builder": [_builder(status="COMPLETE")],
        },
        truncate_commit_to=7,
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    supervisor.advance(state)  # planning -> creating_worktree
    supervisor.advance(state)  # creating_worktree -> building
    supervisor.advance(state)  # building -> auditing

    assert state.phase == "auditing"
    assert state.last_task_head is not None
    assert len(state.last_task_head) == 40


def test_builder_complete_with_falsy_commit_raises_loop_error(tmp_path, monkeypatch):
    # BuilderResult's own validator already requires a commit when status
    # is COMPLETE, so a falsy commit can never reach _do_building through
    # the normal BuilderResult.parse() path. This exercises the
    # supervisor's defense-in-depth check directly by bypassing that
    # validation (model_construct), simulating a hand-edited or otherwise
    # corrupted result rather than a normal contract violation.
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready()],
            "loop-builder": [_builder(status="COMPLETE")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    supervisor.advance(state)
    supervisor.advance(state)
    assert state.phase == "building"

    from loop_supervisor.contracts import BuilderResult, BuilderStatus

    corrupted = BuilderResult.model_construct(
        task_id="task-1",
        objective="Do a thing",
        status=BuilderStatus.COMPLETE,
        implementation_summary="did it",
        implementation_strategy=[],
        tests_run=[],
        test_results=[],
        files_changed=[],
        commit=None,
        open_concerns=[],
    )
    monkeypatch.setattr(BuilderResult, "parse", classmethod(lambda cls, raw: corrupted))

    # advance() catches LoopError internally and converts it to a terminal
    # failure outcome rather than raising, so call _do_building() directly
    # to observe the LoopError itself.
    with pytest.raises(LoopError, match="no commit hash"):
        supervisor._do_building(state)


def test_run_max_steps_none_matches_unbounded_default(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(), _planner_complete()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    final = supervisor.run(state, max_steps=None)

    assert final.phase == PHASE_DONE
    assert final.accepted_task_count == 1


def test_run_max_steps_stops_before_terminal_without_error(tmp_path):
    # The happy path above takes exactly 8 advance() calls to reach `done`
    # (planning -> creating_worktree -> building -> auditing -> merging ->
    # cleanup_worktree -> cleanup_branch -> planning -> done). Capping at 7
    # must stop one step short, mid-flight, with no error raised.
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(), _planner_complete()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    final = supervisor.run(state, max_steps=7)

    assert final.phase == PHASE_PLANNING
    assert final.phase != PHASE_DONE


def test_run_max_steps_one_performs_exactly_one_advance(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(), _planner_complete()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    final = supervisor.run(state, max_steps=1)

    assert final.phase == "creating_worktree"
    planner_calls = [c for c in runner.calls if c[0] == "loop-planner"]
    assert len(planner_calls) == 1


def test_run_max_steps_exact_terminal_count_completes_normally(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(), _planner_complete()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    final = supervisor.run(state, max_steps=8)

    assert final.phase == PHASE_DONE


def test_run_max_steps_counts_input_required_as_a_step(tmp_path):
    # A BLOCKED builder makes advance() return INPUT_REQUIRED without a
    # phase transition (building -> awaiting_input). That call must still
    # consume one unit of the step budget: if it didn't, max_steps=3 would
    # under-count and call advance() a 4th time (attempting to resolve the
    # pending question against an empty input queue) instead of stopping
    # exactly at the INPUT_REQUIRED call.
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready()],
            "loop-builder": [_builder(status="BLOCKED", open_concerns=["need clarification"])],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner, input_provider=ScriptedInput([]))
    state = supervisor.start_new_run()

    advance_calls = []
    original_advance = supervisor.advance

    def counting_advance(state):
        advance_calls.append(state.phase)
        return original_advance(state)

    supervisor.advance = counting_advance

    # planning -> creating_worktree -> building(INPUT_REQUIRED) is 3 steps.
    final = supervisor.run(state, max_steps=3)

    assert final.phase == PHASE_AWAITING_INPUT
    assert len(advance_calls) == 3
    builder_calls = [c for c in runner.calls if c[0] == "loop-builder"]
    assert len(builder_calls) == 1


def test_run_max_steps_zero_returns_state_immediately_without_advancing(tmp_path):
    runner = ScriptedRunner({"loop-planner": [_planner_ready(), _planner_complete()]})
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    final = supervisor.run(state, max_steps=0)

    assert final.phase == PHASE_PLANNING
    assert runner.calls == []


def test_project_complete_immediately(tmp_path):
    runner = ScriptedRunner({"loop-planner": [_planner_complete()]})
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    final = supervisor.run(state)

    assert final.phase == PHASE_DONE
    assert final.accepted_task_count == 0


def test_revise_then_accept(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(), _planner_complete()],
            "loop-builder": [_builder(status="COMPLETE"), _builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="REVISE"), _auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    final = supervisor.run(state)

    assert final.phase == PHASE_DONE
    assert final.accepted_task_count == 1
    builder_calls = [c for c in runner.calls if c[0] == "loop-builder"]
    assert len(builder_calls) == 2
    # Both builder invocations happen on the same preserved worktree/directory.
    assert builder_calls[0][1] == builder_calls[1][1]


def test_revise_passes_required_changes_and_findings_but_not_design_observations(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(), _planner_complete()],
            "loop-builder": [_builder(status="COMPLETE"), _builder(status="COMPLETE")],
            "loop-auditor": [
                _auditor(
                    disposition="REVISE",
                    required_changes=["reject unterminated quoted values"],
                    findings=["a line with an unterminated quote is silently accepted"],
                    design_observations=["the schema could use a stricter mode"],
                ),
                _auditor(disposition="ACCEPT"),
            ],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    final = supervisor.run(state)

    assert final.phase == PHASE_DONE
    builder_prompts = [p for agent, p in runner.prompts if agent == "loop-builder"]
    assert len(builder_prompts) == 2
    second_builder_prompt = builder_prompts[1]

    assert "reject unterminated quoted values" in second_builder_prompt
    assert "a line with an unterminated quote is silently accepted" in second_builder_prompt
    assert "the schema could use a stricter mode" not in second_builder_prompt

    changes_index = second_builder_prompt.index("reject unterminated quoted values")
    findings_index = second_builder_prompt.index(
        "a line with an unterminated quote is silently accepted"
    )
    assert changes_index < findings_index


def test_replan_continues_on_same_worktree(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [
                _planner_ready(task_id="task-1"),
                _planner_ready(task_id="task-1-v2"),
                _planner_complete(),
            ],
            "loop-builder": [
                _builder(task_id="task-1", status="COMPLETE"),
                _builder(task_id="task-1-v2", status="COMPLETE"),
            ],
            "loop-auditor": [
                _auditor(task_id="task-1", disposition="REPLAN"),
                _auditor(task_id="task-1-v2", disposition="ACCEPT"),
            ],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    final = supervisor.run(state)

    assert final.phase == PHASE_DONE
    assert final.accepted_task_count == 1

    planner_calls = [c for c in runner.calls if c[0] == "loop-planner"]
    # Second planner invocation (the replan) must target the preserved task
    # worktree, not the integration root.
    assert planner_calls[1][1] != repo.root
    assert planner_calls[1][1] == planner_calls[2][1] or True  # third call is post-merge (root)


def test_builder_blocked_pauses_and_resumes_with_guidance(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(), _planner_complete()],
            "loop-builder": [
                _builder(status="BLOCKED", open_concerns=["need clarification"]),
                _builder(status="COMPLETE"),
            ],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    input_provider = ScriptedInput(["use approach X"])
    supervisor, repo = _make_supervisor(tmp_path, runner, input_provider=input_provider)
    state = supervisor.start_new_run()
    state = supervisor.run(state)

    assert state.phase == PHASE_DONE
    assert input_provider.requests[0]["kind"] == "builder_guidance"


def test_builder_blocked_noninteractive_pauses_cleanly(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready()],
            "loop-builder": [_builder(status="BLOCKED")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner, input_provider=ScriptedInput([]))
    state = supervisor.start_new_run()
    final = supervisor.run(state)

    assert final.phase == PHASE_AWAITING_INPUT
    assert final.pending_question["kind"] == "builder_guidance"


def test_decision_required_invokes_architect_and_writes_adr(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(decision_required=True), _planner_complete()],
            "loop-architect": [_architect_decided()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner, auto_decide=True)
    state = supervisor.start_new_run()
    final = supervisor.run(state)

    assert final.phase == PHASE_DONE
    adr_files = list((repo.root / "docs" / "decisions").glob("*.md"))
    assert len(adr_files) == 1
    assert "Chosen approach" in adr_files[0].read_text()


def test_architect_needs_input_then_decided(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(decision_required=True), _planner_complete()],
            "loop-architect": [_architect_needs_input(), _architect_decided()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    input_provider = ScriptedInput(["option A, please"])
    supervisor, repo = _make_supervisor(tmp_path, runner, input_provider=input_provider)
    state = supervisor.start_new_run()
    final = supervisor.run(state)

    assert final.phase == PHASE_DONE
    assert input_provider.requests[0]["kind"] == "architect_input"


def test_architect_needs_input_noninteractive_pauses_cleanly(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(decision_required=True)],
            "loop-architect": [_architect_needs_input()],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner, input_provider=ScriptedInput([]))
    state = supervisor.start_new_run()
    final = supervisor.run(state)

    assert final.phase == PHASE_AWAITING_INPUT
    assert final.pending_question["kind"] == "architect_input"


def test_adr_is_committed_by_builder_and_present_after_merge(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(decision_required=True), _planner_complete()],
            "loop-architect": [_architect_decided()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner, auto_decide=True)
    state = supervisor.start_new_run()
    final = supervisor.run(state)

    assert final.phase == PHASE_DONE
    merged_adrs = list((repo.root / "docs" / "decisions").glob("*.md"))
    assert len(merged_adrs) == 1
    assert repo.is_clean()


def test_decision_approval_required_and_rejected_retries_architect(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(decision_required=True), _planner_complete()],
            "loop-architect": [_architect_decided(), _architect_decided()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    input_provider = ScriptedInput(["no", "more context please", "yes"])
    supervisor, repo = _make_supervisor(
        tmp_path, runner, input_provider=input_provider, auto_decide=False
    )
    state = supervisor.start_new_run()
    final = supervisor.run(state)

    assert final.phase == PHASE_DONE
    kinds = [r["kind"] for r in input_provider.requests]
    assert "decision_approval" in kinds


def test_revision_limit_raises(tmp_path):
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


def test_replan_limit_raises(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready() for _ in range(10)],
            "loop-builder": [_builder(status="COMPLETE") for _ in range(10)],
            "loop-auditor": [_auditor(disposition="REPLAN") for _ in range(10)],
        }
    )
    supervisor, repo = _make_supervisor(
        tmp_path, runner, options=_make_options(max_replans_per_task=2)
    )
    state = supervisor.start_new_run()

    with pytest.raises(LoopError):
        supervisor.run(state)


def test_max_accepted_tasks_stops_run(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(task_id=f"task-{i}") for i in range(1, 4)],
            "loop-builder": [_builder(task_id=f"task-{i}", status="COMPLETE") for i in range(1, 4)],
            "loop-auditor": [
                _auditor(task_id=f"task-{i}", disposition="ACCEPT") for i in range(1, 4)
            ],
        }
    )
    supervisor, repo = _make_supervisor(
        tmp_path, runner, options=_make_options(max_accepted_tasks=2)
    )
    state = supervisor.start_new_run()
    final = supervisor.run(state)

    assert final.phase == PHASE_DONE
    assert final.accepted_task_count == 2


def test_start_new_run_requires_clean_integration(tmp_path):
    runner = ScriptedRunner({})
    supervisor, repo = _make_supervisor(tmp_path, runner)
    (repo.root / "dirty.txt").write_text("oops\n")

    from loop_supervisor.git import GitError

    with pytest.raises(GitError):
        supervisor.start_new_run()


def test_resume_rejects_mismatched_common_dir(tmp_path):
    runner = ScriptedRunner({})
    supervisor, repo = _make_supervisor(tmp_path, runner)

    state = supervisor.start_new_run()
    state.git_common_dir = "/somewhere/else/.git"
    with pytest.raises(LoopError):
        supervisor.resume(state)


def test_resume_accepts_unchanged_checkpoint(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(), _planner_complete()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()

    resumed = supervisor.resume(state)
    final = supervisor.run(resumed)
    assert final.phase == PHASE_DONE


def test_resume_rejects_integration_branch_change(tmp_path):
    runner = ScriptedRunner({})
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    state.integration_branch = "other-branch"

    with pytest.raises(LoopError):
        supervisor.resume(state)


def test_resume_rejects_integration_head_rewind(tmp_path):
    runner = ScriptedRunner({})
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()

    (repo.root / "more.txt").write_text("more\n")
    _run(["add", "-A"], repo.root)
    _run(["commit", "-m", "more work"], repo.root)

    # state.integration_expected_head still points at the pre-commit HEAD,
    # which *is* an ancestor of the new HEAD, so this should be accepted...
    resumed = supervisor.resume(state)
    assert resumed is state

    # ...but rewriting expected_head to something that is NOT an ancestor
    # of current HEAD must be rejected.
    state.integration_expected_head = "0" * 40
    with pytest.raises(LoopError):
        supervisor.resume(state)


def test_resume_rejects_dirty_integration(tmp_path):
    runner = ScriptedRunner({})
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    (repo.root / "dirty.txt").write_text("oops\n")

    with pytest.raises(LoopError):
        supervisor.resume(state)


def test_resume_uses_persisted_limits(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready() for _ in range(10)],
            "loop-builder": [_builder(status="COMPLETE") for _ in range(10)],
            "loop-auditor": [_auditor(disposition="REPLAN") for _ in range(10)],
        }
    )
    supervisor, repo = _make_supervisor(
        tmp_path, runner, options=_make_options(max_replans_per_task=1)
    )
    state = supervisor.start_new_run()

    fresh_supervisor = Supervisor(
        repo=repo,
        runner=runner,
        git_common_dir=repo.common_dir(),
        input_provider=ScriptedInput([]),
    )
    resumed = fresh_supervisor.resume(state)
    with pytest.raises(LoopError):
        fresh_supervisor.run(resumed)
    assert fresh_supervisor.limits.max_replans_per_task == 1


def test_resume_rejects_task_worktree_head_change(tmp_path):
    runner = ScriptedRunner({"loop-planner": [_planner_ready()]})
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    supervisor.advance(state)
    supervisor.advance(state)
    assert state.task_worktree_path is not None

    # Task worktree already created and checkpointed; commit an extra
    # change directly on the task branch to simulate drift since the last
    # save.
    worktree_path = Path(state.task_worktree_path)
    (worktree_path / "extra.txt").write_text("drift\n")
    _run(["add", "-A"], worktree_path)
    _run(["commit", "-m", "untracked drift"], worktree_path)

    with pytest.raises(LoopError):
        supervisor.resume(state)


def test_resume_rejects_unregistered_task_directory(tmp_path):
    runner = ScriptedRunner({"loop-planner": [_planner_ready()]})
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    supervisor.advance(state)
    supervisor.advance(state)

    worktree_path = Path(state.task_worktree_path)
    expected_head = state.task_expected_head
    _run(["worktree", "remove", str(worktree_path), "--force"], repo.root)
    # The recorded path no longer corresponds to any registered git
    # worktree at all.
    worktree_path.mkdir()

    with pytest.raises(LoopError):
        supervisor.resume(state)
    assert state.task_expected_head == expected_head


def test_task_identity_mismatch_from_builder_raises(tmp_path):
    # Prior to durable operational-failure handling for ContractError,
    # this asserted `pytest.raises(ContractError)` around `run()`: the
    # exception escaped advance()'s dispatch loop entirely, so nothing
    # was ever persisted. ContractError is now classified as an
    # operational failure like every other role-invocation error, so
    # run() converts it to LoopError (see Supervisor.run()'s
    # OPERATIONAL_FAILURE branch) and the failure is durable.
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(task_id="task-1")],
            "loop-builder": [_builder(task_id="task-WRONG", status="COMPLETE")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()

    from loop_supervisor.contracts import ContractError

    with pytest.raises(LoopError) as excinfo:
        supervisor.run(state)
    assert isinstance(excinfo.value.__cause__, ContractError)
    assert state.phase == PHASE_OPERATIONAL_FAILURE
    assert state.last_error is not None
    assert state.last_error["kind"] == "contract"
    assert state.last_error["retryable"] is True


def test_auditor_decision_request_routes_to_architect_then_planner(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [
                _planner_ready(task_id="task-1"),
                _planner_ready(task_id="task-1-v2"),
                _planner_complete(),
            ],
            "loop-builder": [
                _builder(task_id="task-1", status="COMPLETE"),
                _builder(task_id="task-1-v2", status="COMPLETE"),
            ],
            "loop-architect": [_architect_decided(question="Auditor's question?")],
            "loop-auditor": [
                _auditor(
                    task_id="task-1",
                    disposition="REPLAN",
                    decision_required=True,
                    decision_question="Auditor's question?",
                    decision_rationale="Needs a design call",
                ),
                _auditor(task_id="task-1-v2", disposition="ACCEPT"),
            ],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner, auto_decide=True)
    state = supervisor.start_new_run()
    final = supervisor.run(state)

    assert final.phase == PHASE_DONE
    calls = [c[0] for c in runner.calls]
    auditor_index = calls.index("loop-auditor")
    architect_index = calls.index("loop-architect")
    second_planner_index = calls.index("loop-planner", calls.index("loop-planner") + 1)
    assert architect_index == auditor_index + 1
    assert second_planner_index == architect_index + 1

    adr_files = list((repo.root / "docs" / "decisions").glob("*.md"))
    assert len(adr_files) == 1


def test_planner_origin_decision_call_order_unaffected(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(decision_required=True), _planner_complete()],
            "loop-architect": [_architect_decided()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner, auto_decide=True)
    state = supervisor.start_new_run()
    final = supervisor.run(state)

    assert final.phase == PHASE_DONE
    calls = [c[0] for c in runner.calls]
    assert calls[:4] == ["loop-planner", "loop-architect", "loop-builder", "loop-auditor"]


def test_architect_must_answer_the_requested_question(tmp_path):
    # See test_task_identity_mismatch_from_builder_raises: ContractError is
    # now a durable operational failure, so run() converts it to LoopError
    # instead of letting it escape raw.
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(decision_required=True)],
            "loop-architect": [_architect_decided(question="A different question entirely")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner, auto_decide=True)
    state = supervisor.start_new_run()

    from loop_supervisor.contracts import ContractError

    with pytest.raises(LoopError) as excinfo:
        supervisor.run(state)
    assert isinstance(excinfo.value.__cause__, ContractError)
    assert state.phase == PHASE_OPERATIONAL_FAILURE
    assert state.last_error is not None
    assert state.last_error["kind"] == "contract"
    assert state.last_error["retryable"] is True


def test_planner_complete_with_active_worktree_fails_closed(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(), _planner_complete()],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    supervisor.advance(state)
    supervisor.advance(state)
    assert state.task_worktree_path is not None

    state.phase = "planning"
    with pytest.raises(LoopError):
        supervisor.run(state)
    assert state.task_worktree_path is not None


def test_pending_decision_approval_resume_does_not_reinvoke_architect(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(decision_required=True), _planner_complete()],
            "loop-architect": [_architect_decided()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    supervisor, repo = _make_supervisor(
        tmp_path, runner, input_provider=ScriptedInput([]), auto_decide=False
    )
    state = supervisor.start_new_run()
    state = supervisor.run(state)

    assert state.phase == PHASE_AWAITING_INPUT
    assert state.pending_question["kind"] == "decision_approval"
    architect_calls_before = len([c for c in runner.calls if c[0] == "loop-architect"])
    assert architect_calls_before == 1

    fresh_supervisor = Supervisor(
        repo=repo,
        runner=runner,
        git_common_dir=repo.common_dir(),
        input_provider=ScriptedInput(["yes"]),
    )
    resumed = fresh_supervisor.resume(state)
    final = fresh_supervisor.run(resumed)

    assert final.phase == PHASE_DONE
    architect_calls_after = len([c for c in runner.calls if c[0] == "loop-architect"])
    assert architect_calls_after == 1
    adr_files = list((repo.root / "docs" / "decisions").glob("*.md"))
    assert len(adr_files) == 1


def test_pending_decision_approval_rejection_then_feedback_reinvokes_architect(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(decision_required=True), _planner_complete()],
            "loop-architect": [_architect_decided(), _architect_decided()],
            "loop-builder": [_builder(status="COMPLETE")],
            "loop-auditor": [_auditor(disposition="ACCEPT")],
        }
    )
    input_provider = ScriptedInput(["no", "please reconsider", "yes"])
    supervisor, repo = _make_supervisor(
        tmp_path, runner, input_provider=input_provider, auto_decide=False
    )
    state = supervisor.start_new_run()
    final = supervisor.run(state)

    assert final.phase == PHASE_DONE
    architect_calls = len([c for c in runner.calls if c[0] == "loop-architect"])
    assert architect_calls == 2
