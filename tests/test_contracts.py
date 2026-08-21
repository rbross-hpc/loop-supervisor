import json

import pytest

from loop_supervisor.contracts import (
    ArchitectResult,
    AuditorResult,
    BuilderResult,
    ContractError,
    PlannerResult,
    check_task_identity,
)


def test_planner_ready_valid():
    raw = json.dumps(
        {
            "status": "READY",
            "task_id": "task-1",
            "objective": "Do a thing",
            "rationale": "Because reasons",
            "acceptance_criteria": ["it works"],
            "relevant_files": [],
            "design_questions": [],
            "decision_required": False,
            "decision_question": None,
            "decision_rationale": None,
        }
    )
    result = PlannerResult.parse(raw)
    assert result.task_id == "task-1"


def test_planner_complete_omits_task_fields():
    raw = json.dumps({"status": "COMPLETE"})
    result = PlannerResult.parse(raw)
    assert result.task_id is None


def test_planner_ready_requires_task_fields():
    raw = json.dumps({"status": "READY"})
    with pytest.raises(ContractError):
        PlannerResult.parse(raw)


def test_planner_ready_requires_acceptance_criteria():
    raw = json.dumps(
        {
            "status": "READY",
            "task_id": "t",
            "objective": "o",
            "rationale": "r",
            "acceptance_criteria": [],
        }
    )
    with pytest.raises(ContractError):
        PlannerResult.parse(raw)


def test_planner_decision_required_needs_question_and_rationale():
    raw = json.dumps(
        {
            "status": "READY",
            "task_id": "t",
            "objective": "o",
            "rationale": "r",
            "acceptance_criteria": ["c"],
            "decision_required": True,
        }
    )
    with pytest.raises(ContractError):
        PlannerResult.parse(raw)


def test_planner_rejects_unknown_fields():
    raw = json.dumps({"status": "COMPLETE", "extra_field": "nope"})
    with pytest.raises(ContractError):
        PlannerResult.parse(raw)


def test_planner_rejects_prose():
    with pytest.raises(ContractError):
        PlannerResult.parse("Sure, here's my plan: ...")


def test_planner_rejects_malformed_json():
    with pytest.raises(ContractError):
        PlannerResult.parse("{not json")


def test_planner_accepts_markdown_fence():
    raw = "```json\n" + json.dumps({"status": "COMPLETE"}) + "\n```"
    result = PlannerResult.parse(raw)
    assert result.status.value == "COMPLETE"


def test_planner_rejects_unknown_status():
    raw = json.dumps({"status": "DONE"})
    with pytest.raises(ContractError):
        PlannerResult.parse(raw)


def test_builder_complete_requires_commit():
    raw = json.dumps(
        {
            "task_id": "t",
            "objective": "o",
            "status": "COMPLETE",
            "implementation_summary": "did it",
        }
    )
    with pytest.raises(ContractError):
        BuilderResult.parse(raw)


def test_builder_complete_valid():
    raw = json.dumps(
        {
            "task_id": "t",
            "objective": "o",
            "status": "COMPLETE",
            "implementation_summary": "did it",
            "commit": "abc123",
        }
    )
    result = BuilderResult.parse(raw)
    assert result.commit == "abc123"


def test_builder_blocked_does_not_require_commit():
    raw = json.dumps(
        {
            "task_id": "t",
            "objective": "o",
            "status": "BLOCKED",
            "implementation_summary": "stuck",
        }
    )
    result = BuilderResult.parse(raw)
    assert result.commit is None


def test_builder_rejects_unknown_status():
    raw = json.dumps(
        {
            "task_id": "t",
            "objective": "o",
            "status": "DONE",
            "implementation_summary": "x",
        }
    )
    with pytest.raises(ContractError):
        BuilderResult.parse(raw)


def test_auditor_revise_requires_required_changes():
    raw = json.dumps(
        {
            "task_id": "t",
            "objective": "o",
            "disposition": "REVISE",
            "findings": ["issue"],
        }
    )
    with pytest.raises(ContractError):
        AuditorResult.parse(raw)


def test_auditor_accept_valid():
    raw = json.dumps(
        {
            "task_id": "t",
            "objective": "o",
            "disposition": "ACCEPT",
            "findings": [],
        }
    )
    result = AuditorResult.parse(raw)
    assert result.disposition.value == "ACCEPT"


def test_auditor_decision_required_needs_fields():
    raw = json.dumps(
        {
            "task_id": "t",
            "objective": "o",
            "disposition": "REPLAN",
            "decision_required": True,
        }
    )
    with pytest.raises(ContractError):
        AuditorResult.parse(raw)


def test_architect_decided_requires_adr():
    raw = json.dumps(
        {
            "status": "DECIDED",
            "question": "q",
            "rationale": "r",
        }
    )
    with pytest.raises(ContractError):
        ArchitectResult.parse(raw)


def test_architect_decided_valid():
    raw = json.dumps(
        {
            "status": "DECIDED",
            "question": "q",
            "rationale": "r",
            "adr": {
                "title": "Title",
                "context": "ctx",
                "decision": "dec",
                "consequences": ["c1"],
            },
        }
    )
    result = ArchitectResult.parse(raw)
    assert result.adr is not None
    assert result.adr.title == "Title"


def test_architect_needs_input_requires_request():
    raw = json.dumps({"status": "NEEDS_INPUT", "question": "q", "rationale": "r"})
    with pytest.raises(ContractError):
        ArchitectResult.parse(raw)


def test_architect_needs_input_valid():
    raw = json.dumps(
        {
            "status": "NEEDS_INPUT",
            "question": "q",
            "rationale": "r",
            "input_request": "please clarify X",
        }
    )
    result = ArchitectResult.parse(raw)
    assert result.input_request == "please clarify X"


def test_check_task_identity_mismatched_id():
    with pytest.raises(ContractError):
        check_task_identity(
            task_id="a",
            objective="obj",
            other_task_id="b",
            other_objective="obj",
            other_role="builder",
        )


def test_check_task_identity_mismatched_objective():
    with pytest.raises(ContractError):
        check_task_identity(
            task_id="a",
            objective="obj1",
            other_task_id="a",
            other_objective="obj2",
            other_role="auditor",
        )


def test_check_task_identity_match_ok():
    check_task_identity(
        task_id="a",
        objective="obj",
        other_task_id="a",
        other_objective="obj",
        other_role="builder",
    )
