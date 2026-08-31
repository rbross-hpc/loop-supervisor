"""Unit tests for the supervisor's `_build_*_prompt` functions.

These are pure string-formatting functions with no repo/supervisor
fixtures, so they get their own file rather than living in
test_supervisor.py's integration-style suite.
"""

from loop_supervisor.contracts import PlannerResult
from loop_supervisor.supervisor import _build_auditor_prompt, _build_builder_prompt


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


def _base_auditor_kwargs(**overrides):
    kwargs = dict(
        integration_branch="main",
        integration_commit="abc123",
        task_branch="loop/task-1",
        task_commit="def456",
        base_commit="abc123",
    )
    kwargs.update(overrides)
    return kwargs


def test_auditor_prompt_without_verification_omits_verification_section():
    prompt = _build_auditor_prompt(_planner(), **_base_auditor_kwargs())
    assert "Verification:" not in prompt
    assert "Builder-reported" not in prompt


def test_auditor_prompt_verification_ok_states_no_rerun_needed():
    verification_result = {
        "ok": True,
        "commands": [
            {
                "command": "pytest -q",
                "ok": True,
                "returncode": 0,
                "timed_out": False,
                "duration": 1.23,
                "output_path": ".loop-supervisor/verification/01.log",
                "summary": "5 passed",
            }
        ],
    }
    prompt = _build_auditor_prompt(
        _planner(), **_base_auditor_kwargs(verification_result=verification_result)
    )
    assert "every command succeeded" in prompt
    assert "You do not need to re-run them" in prompt
    assert "`pytest -q` [ok]" in prompt
    assert ".loop-supervisor/verification/01.log" in prompt
    assert "5 passed" in prompt


def test_auditor_prompt_verification_failure_states_not_disqualifying():
    verification_result = {
        "ok": False,
        "commands": [
            {
                "command": "pytest -q",
                "ok": False,
                "returncode": 1,
                "timed_out": False,
                "duration": 1.0,
                "output_path": ".loop-supervisor/verification/01.log",
                "summary": "FAILED test_x.py::test_y",
            }
        ],
    }
    prompt = _build_auditor_prompt(
        _planner(), **_base_auditor_kwargs(verification_result=verification_result)
    )
    assert "at least one command failed" in prompt
    assert "not automatically disqualifying" in prompt
    assert "exit 1" in prompt
    assert "FAILED test_x.py::test_y" in prompt


def test_auditor_prompt_verification_timeout_is_labeled():
    verification_result = {
        "ok": False,
        "commands": [
            {
                "command": "pytest -q",
                "ok": False,
                "returncode": None,
                "timed_out": True,
                "duration": 900.0,
                "output_path": ".loop-supervisor/verification/01.log",
                "summary": "",
            }
        ],
    }
    prompt = _build_auditor_prompt(
        _planner(), **_base_auditor_kwargs(verification_result=verification_result)
    )
    assert "TIMED OUT" in prompt


def test_auditor_prompt_includes_builder_self_reported_tests():
    prompt = _build_auditor_prompt(
        _planner(),
        **_base_auditor_kwargs(
            builder_tests_run=["pytest -q"],
            builder_test_results=["945 passed"],
        ),
    )
    assert "The builder self-reported" in prompt
    assert "not independently verified" in prompt
    assert "- pytest -q" in prompt
    assert "- 945 passed" in prompt


def test_auditor_prompt_omits_builder_self_report_header_when_absent():
    prompt = _build_auditor_prompt(_planner(), **_base_auditor_kwargs())
    assert "The builder self-reported" not in prompt


def test_auditor_prompt_suggested_git_commands_still_present_with_verification():
    verification_result = {"ok": True, "commands": []}
    prompt = _build_auditor_prompt(
        _planner(), **_base_auditor_kwargs(verification_result=verification_result)
    )
    assert "git diff abc123...def456" in prompt
    assert "git log --oneline abc123..def456" in prompt
