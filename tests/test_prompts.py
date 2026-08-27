"""Unit tests for the supervisor's `_build_*_prompt` functions.

These are pure string-formatting functions with no repo/supervisor
fixtures, so they get their own file rather than living in
test_supervisor.py's integration-style suite.
"""

from loop_supervisor.contracts import PlannerResult
from loop_supervisor.supervisor import _build_builder_prompt


def _planner(**overrides):
    payload = {
        "status": "READY",
        "task_id": "task-1",
        "objective": "Do a thing",
        "rationale": "because",
        "acceptance_criteria": ["works"],
        "relevant_files": [],
        "design_questions": [],
        "decision_required": False,
        "decision_question": None,
        "decision_rationale": None,
    }
    payload.update(overrides)
    return PlannerResult.model_validate(payload)


def test_required_changes_rendered_under_its_header():
    prompt = _build_builder_prompt(
        _planner(),
        required_changes=["fix the thing"],
        audit_findings=None,
        guidance=None,
    )
    assert "The auditor requested these changes on your previous attempt:" in prompt
    assert "- fix the thing" in prompt


def test_audit_findings_rendered_under_context_header():
    prompt = _build_builder_prompt(
        _planner(),
        required_changes=["fix the thing"],
        audit_findings=["the bug reproduces with input X"],
        guidance=None,
    )
    assert (
        "Supporting detail from the audit (context for the changes above, "
        "not additional requirements):" in prompt
    )
    assert "- the bug reproduces with input X" in prompt


def test_required_changes_precede_audit_findings():
    prompt = _build_builder_prompt(
        _planner(),
        required_changes=["fix the thing"],
        audit_findings=["the bug reproduces with input X"],
        guidance=None,
    )
    changes_index = prompt.index("fix the thing")
    findings_index = prompt.index("the bug reproduces with input X")
    assert changes_index < findings_index


def test_audit_findings_none_omits_context_header():
    prompt = _build_builder_prompt(
        _planner(),
        required_changes=["fix the thing"],
        audit_findings=None,
        guidance=None,
    )
    assert "Supporting detail from the audit" not in prompt


def test_audit_findings_empty_list_omits_context_header():
    prompt = _build_builder_prompt(
        _planner(),
        required_changes=["fix the thing"],
        audit_findings=[],
        guidance=None,
    )
    assert "Supporting detail from the audit" not in prompt


def test_design_observations_never_appear_in_builder_prompt():
    # Regression guard: design_observations is the auditor's
    # scope/criteria-critique channel, routed only to the planner on
    # REPLAN. _build_builder_prompt has no parameter for it at all, so
    # this is really asserting the function's signature doesn't grow one
    # by mistake, plus that neither existing field's rendering accidentally
    # leaks the string "design_observations".
    prompt = _build_builder_prompt(
        _planner(),
        required_changes=["fix the thing"],
        audit_findings=["some finding"],
        guidance="some guidance",
    )
    assert "design_observations" not in prompt
    assert "design observation" not in prompt.lower()


def test_guidance_rendered_after_findings():
    prompt = _build_builder_prompt(
        _planner(),
        required_changes=["fix the thing"],
        audit_findings=["some finding"],
        guidance="use approach X",
    )
    assert "Additional guidance from the operator: use approach X" in prompt
    findings_index = prompt.index("some finding")
    guidance_index = prompt.index("use approach X")
    assert findings_index < guidance_index


def test_no_findings_or_changes_omits_both_headers():
    prompt = _build_builder_prompt(
        _planner(),
        required_changes=None,
        audit_findings=None,
        guidance=None,
    )
    assert "The auditor requested these changes" not in prompt
    assert "Supporting detail from the audit" not in prompt
