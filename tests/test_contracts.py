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


def test_planner_accepts_leading_prose_before_json():
    # Observed from a real model: a confirmation sentence before the
    # required JSON object, despite being told to return exactly one
    # JSON object and no other text.
    raw = (
        "Confirmed: no parser exists yet. The next coherent unit of "
        "work is implementing it.\n\n" + json.dumps({"status": "COMPLETE"})
    )
    result = PlannerResult.parse(raw)
    assert result.status.value == "COMPLETE"


def test_planner_accepts_leading_prose_with_fence():
    raw = (
        "Confirmed: no parser exists yet.\n\n```json\n"
        + json.dumps({"status": "COMPLETE"})
        + "\n```"
    )
    result = PlannerResult.parse(raw)
    assert result.status.value == "COMPLETE"


def test_planner_rejects_trailing_prose_after_json():
    # Leading commentary is tolerated; trailing commentary is not. A
    # model that keeps talking after the object is a different failure
    # mode than one that briefly narrates before it.
    raw = json.dumps({"status": "COMPLETE"}) + "\n\nHope that helps!"
    with pytest.raises(ContractError, match="not valid JSON"):
        PlannerResult.parse(raw)


def test_planner_rejects_trailing_prose_after_fenced_json():
    # A leftover closing ``` after the object is tolerated (see
    # test_planner_accepts_leading_prose_with_fence, where the fence's
    # own opening line is skipped as part of the ignored preamble and
    # only the closer remains) -- but that tolerance is exactly for a
    # bare fence closer, not for arbitrary trailing content that merely
    # follows one.
    raw = "```json\n" + json.dumps({"status": "COMPLETE"}) + "\n```\n\nHope that helps!"
    with pytest.raises(ContractError, match="not valid JSON"):
        PlannerResult.parse(raw)


def test_planner_rejects_two_json_objects():
    raw = json.dumps({"status": "COMPLETE"}) + "\n" + json.dumps({"status": "READY"})
    with pytest.raises(ContractError, match="not valid JSON"):
        PlannerResult.parse(raw)


def test_planner_rejects_prose_containing_brace():
    # A deliberate limitation, not an oversight: tolerating a preamble
    # means decoding from the first '{' in the text. If the preamble
    # itself contains a brace that doesn't open a valid JSON object,
    # parsing fails loudly rather than skipping ahead to search for a
    # second, unrelated '{' later in the text -- which would risk
    # silently accepting whichever of several candidate objects happens
    # to parse, instead of the specific one a fuller scan can't
    # distinguish from noise.
    raw = "Use a mapping like {key: value} for this.\n\n" + json.dumps({"status": "COMPLETE"})
    with pytest.raises(ContractError, match="not valid JSON"):
        PlannerResult.parse(raw)


def test_planner_rejects_array_after_prose():
    # No '{' anywhere in the text (arrays don't have one), so this is
    # indistinguishable from ordinary malformed/non-JSON output -- it
    # can never reach the object-vs-array check, which only applies
    # once something has actually been decoded.
    raw = "Here is the result:\n[1, 2, 3]"
    with pytest.raises(ContractError, match="not valid JSON"):
        PlannerResult.parse(raw)


def test_planner_rejects_top_level_array_with_no_preamble():
    # This is the case that *does* reach the object-vs-array check:
    # json.loads succeeds on the whole text (no preamble to trip it up),
    # returning a list rather than a dict.
    raw = json.dumps([1, 2, 3])
    with pytest.raises(ContractError, match="must be a single JSON object"):
        PlannerResult.parse(raw)


def test_planner_rejects_empty_output():
    with pytest.raises(ContractError, match="not valid JSON"):
        PlannerResult.parse("")


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


def test_check_task_identity_allows_restated_objective():
    # A downstream role paraphrasing the objective (rather than echoing it
    # verbatim) is not identity drift: no agent prompt asks for a verbatim
    # echo, so exact string equality on `objective` was rejecting honest
    # restatements. `task_id` remains the load-bearing identity check.
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


def test_check_task_identity_rejects_empty_objective():
    with pytest.raises(ContractError, match="objective"):
        check_task_identity(
            task_id="a",
            objective="obj",
            other_task_id="a",
            other_objective="",
            other_role="builder",
        )


def test_check_task_identity_rejects_whitespace_only_objective():
    with pytest.raises(ContractError, match="objective"):
        check_task_identity(
            task_id="a",
            objective="obj",
            other_task_id="a",
            other_objective="   \n\t",
            other_role="auditor",
        )


def test_check_task_identity_rejects_mismatched_id_despite_matching_objective():
    with pytest.raises(ContractError, match="task_id"):
        check_task_identity(
            task_id="task-002",
            objective="same objective",
            other_task_id="task-003",
            other_objective="same objective",
            other_role="builder",
        )


def test_check_task_identity_allows_restated_objective_live_regression():
    # Verbatim strings from the live test-run auditor rejection that
    # motivated this fix (run 2dba05654b5e, task-002).
    check_task_identity(
        task_id="task-002",
        objective=(
            "Implement the generic logfmt LogRecord parsing layer (module + "
            "tests) that turns OpenCode log lines into typed records, per "
            "ADR 0001, without touching the query/subcommand layer."
        ),
        other_task_id="task-002",
        other_objective=(
            "Implement a generic logfmt LogRecord parser with malformed-line accounting and tests."
        ),
        other_role="auditor",
    )
