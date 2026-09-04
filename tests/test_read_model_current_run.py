"""Coverage for authoritative current-run detail loading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loop_supervisor.read_model import CurrentRun, load_current_run
from loop_supervisor.state import STATE_SCHEMA_VERSION, RunOptions, RunState, save_state


def _options() -> RunOptions:
    return RunOptions(
        max_accepted_tasks=20,
        max_revisions_per_task=5,
        max_replans_per_task=3,
        max_architect_retries=3,
        max_builder_guidance_attempts=3,
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


def _save_state(
    git_common_dir: Path,
    run_id: str = "current",
    *,
    pending_question: dict[str, object] | None = None,
    last_error: dict[str, object] | None = None,
) -> Path:
    state = RunState(
        schema_version=STATE_SCHEMA_VERSION,
        run_id=run_id,
        git_common_dir=str(git_common_dir),
        integration_path=str(git_common_dir),
        integration_branch="integration",
        integration_commit_at_start="abc123",
        options=_options(),
        integration_expected_head="abc123",
        integration_status_snapshot="",
        original_task_id="task-42",
        task_worktree_path="/worktrees/task-42",
        task_branch="loop/task-42",
        task_base_commit="abc123",
        task_expected_head="abc123",
        task_status_snapshot="",
        phase="awaiting_input" if pending_question is not None else "planning",
        planner_result={
            "status": "READY",
            "task_id": "task-42",
            "objective": "Build the detail reader",
            "rationale": "The summary needs it",
            "acceptance_criteria": ["Map current state"],
        },
        builder_result=(
            {
                "task_id": "task-42",
                "objective": "Build the detail reader",
                "status": "BLOCKED",
                "implementation_summary": "Needs guidance.",
            }
            if pending_question is not None
            else None
        ),
        accepted_task_count=4,
        revision_count=3,
        replan_count=2,
        architect_retry_count=1,
        builder_guidance_count=5,
        pending_question=pending_question,
        last_error=last_error,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-02T00:00:00+00:00",
    )
    save_state(git_common_dir, state)
    path = git_common_dir / "loop-supervisor" / "runs" / f"{run_id}.json"
    payload = json.loads(path.read_text())
    payload["updated_at"] = "2026-01-02T00:00:00+00:00"
    path.write_text(json.dumps(payload))
    return path


def test_load_current_run_maps_validated_state_into_immutable_summary(tmp_path: Path) -> None:
    _save_state(
        tmp_path,
        pending_question={
            "kind": "builder_guidance",
            "message": "Which supported API should be used?",
            "context": {"status": "BLOCKED"},
        },
        last_error={
            "error_id": "error-1",
            "kind": "transport",
            "operation": "planning",
            "failed_phase": "planning",
            "retry_phase": "planning",
            "exception_type": "ConnectionError",
            "message": "The control plane was unavailable.",
            "retryable": True,
            "requires_repair": False,
            "recovery_hint": None,
            "occurred_at": "2026-01-02T00:00:00+00:00",
        },
    )

    current = load_current_run(tmp_path, "current")

    assert isinstance(current, CurrentRun)
    assert current.loadable is True, current.diagnostic
    assert current.run_id == "current"
    assert current.phase == "awaiting_input"
    assert current.created_at == "2026-01-01T00:00:00+00:00"
    assert current.updated_at == "2026-01-02T00:00:00+00:00"
    assert current.integration_branch == "integration"
    assert current.current_task_id == "task-42"
    assert current.accepted_task_count == 4
    assert current.revision_count == 3
    assert current.replan_count == 2
    assert current.architect_retry_count == 1
    assert current.builder_guidance_count == 5
    assert current.pending_question == "Which supported API should be used?"
    assert current.latest_operational_error == "The control plane was unavailable."
    assert current.diagnostic is None
    with pytest.raises(AttributeError, match="cannot assign to field"):
        current.phase = "done"  # type: ignore[misc]


@pytest.mark.parametrize(
    "kind",
    ["missing", "malformed", "oversized", "symlink", "non_regular", "identity_mismatch", "schema"],
)
def test_load_current_run_returns_safe_degraded_value_for_unloadable_state(
    tmp_path: Path, kind: str
) -> None:
    path = _save_state(tmp_path)
    if kind == "missing":
        path.unlink()
    elif kind == "malformed":
        path.write_text("not JSON")
    elif kind == "oversized":
        path.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    elif kind == "symlink":
        target = tmp_path / "outside.json"
        target.write_text("{}")
        path.unlink()
        path.symlink_to(target)
    elif kind == "non_regular":
        path.unlink()
        path.mkdir()
    else:
        payload = json.loads(path.read_text())
        if kind == "identity_mismatch":
            payload["run_id"] = "other"
        else:
            payload["schema_version"] = 999
        path.write_text(json.dumps(payload))

    current = load_current_run(tmp_path, "current")

    assert current == CurrentRun.degraded("current", current.diagnostic or "")
    assert current.loadable is False
    assert current.phase is None
    assert current.created_at is None
    assert current.updated_at is None
    assert current.integration_branch is None
    assert current.current_task_id is None
    assert current.accepted_task_count is None
    assert current.revision_count is None
    assert current.replan_count is None
    assert current.architect_retry_count is None
    assert current.builder_guidance_count is None
    assert current.pending_question is None
    assert current.latest_operational_error is None
    assert current.diagnostic is not None
    assert "secret-lock-token" not in current.diagnostic
