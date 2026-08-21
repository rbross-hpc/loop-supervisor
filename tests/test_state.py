import json

import pytest

from loop_supervisor.state import (
    STATE_SCHEMA_VERSION,
    DecisionRequest,
    RunOptions,
    RunState,
    StateError,
    list_runs,
    load_state,
    new_run_id,
    save_state,
    state_path,
)


def _make_options(**overrides) -> RunOptions:
    defaults = dict(
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
    )
    defaults.update(overrides)
    return RunOptions(**defaults)


def _make_state(run_id: str, **overrides) -> RunState:
    defaults = dict(
        schema_version=STATE_SCHEMA_VERSION,
        run_id=run_id,
        git_common_dir="/repo/.git",
        integration_path="/repo",
        integration_branch="main",
        integration_commit_at_start="abc123",
        options=_make_options(),
        integration_expected_head="abc123",
        integration_status_snapshot="",
    )
    defaults.update(overrides)
    return RunState(**defaults)


def test_save_and_load_roundtrip(tmp_path):
    state = _make_state(new_run_id())
    save_state(tmp_path, state)

    loaded = load_state(tmp_path, state.run_id)
    assert loaded.run_id == state.run_id
    assert loaded.integration_branch == "main"
    assert loaded.phase == "planning"


def test_save_sets_permissions(tmp_path):
    state = _make_state(new_run_id())
    save_state(tmp_path, state)
    path = state_path(tmp_path, state.run_id)
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_load_missing_run_raises(tmp_path):
    with pytest.raises(StateError):
        load_state(tmp_path, "does-not-exist")


def test_load_rejects_wrong_schema_version(tmp_path):
    state = _make_state(new_run_id())
    save_state(tmp_path, state)
    path = state_path(tmp_path, state.run_id)
    data = json.loads(path.read_text())
    data["schema_version"] = 999
    path.write_text(json.dumps(data))

    with pytest.raises(StateError):
        load_state(tmp_path, state.run_id)


def test_load_rejects_unknown_fields(tmp_path):
    state = _make_state(new_run_id())
    save_state(tmp_path, state)
    path = state_path(tmp_path, state.run_id)
    data = json.loads(path.read_text())
    data["mystery_field"] = "surprise"
    path.write_text(json.dumps(data))

    with pytest.raises(StateError):
        load_state(tmp_path, state.run_id)


def test_list_runs_empty(tmp_path):
    assert list_runs(tmp_path) == []


def test_list_runs_returns_saved_ids(tmp_path):
    a = _make_state("run-a")
    b = _make_state("run-b")
    save_state(tmp_path, a)
    save_state(tmp_path, b)

    assert list_runs(tmp_path) == ["run-a", "run-b"]


def test_save_updates_timestamp(tmp_path):
    state = _make_state(new_run_id())
    original_updated = state.updated_at
    save_state(tmp_path, state)
    assert state.updated_at >= original_updated


def test_run_options_roundtrip(tmp_path):
    options = _make_options(
        max_accepted_tasks=7,
        max_revisions_per_task=2,
        max_replans_per_task=1,
        max_architect_retries=5,
        malformed_output_retries=3,
        role_timeout=42.0,
        worktree_root="/tmp/worktrees",
        require_decision_approval=True,
        opencode_executable="/usr/local/bin/opencode",
        opencode_startup_timeout=99.0,
    )
    state = _make_state(new_run_id(), options=options)
    save_state(tmp_path, state)

    loaded = load_state(tmp_path, state.run_id)
    assert loaded.options == options


def test_load_rejects_missing_options(tmp_path):
    state = _make_state(new_run_id())
    save_state(tmp_path, state)
    path = state_path(tmp_path, state.run_id)
    data = json.loads(path.read_text())
    del data["options"]
    path.write_text(json.dumps(data))

    with pytest.raises(StateError):
        load_state(tmp_path, state.run_id)


def test_load_rejects_unknown_option_field(tmp_path):
    state = _make_state(new_run_id())
    save_state(tmp_path, state)
    path = state_path(tmp_path, state.run_id)
    data = json.loads(path.read_text())
    data["options"]["mystery"] = "surprise"
    path.write_text(json.dumps(data))

    with pytest.raises(StateError):
        load_state(tmp_path, state.run_id)


def test_load_rejects_schema_v1_without_migration(tmp_path):
    state = _make_state(new_run_id())
    save_state(tmp_path, state)
    path = state_path(tmp_path, state.run_id)
    data = json.loads(path.read_text())
    data["schema_version"] = 1
    del data["options"]
    del data["integration_expected_head"]
    del data["integration_status_snapshot"]
    path.write_text(json.dumps(data))

    with pytest.raises(StateError, match="schema_version 1"):
        load_state(tmp_path, state.run_id)


def test_load_rejects_partial_task_identity(tmp_path):
    state = _make_state(new_run_id(), task_worktree_path="/repo-task-1")
    save_state(tmp_path, state)

    with pytest.raises(StateError, match="partial task identity"):
        load_state(tmp_path, state.run_id)


def test_save_load_preserves_decision_request(tmp_path):
    request = DecisionRequest(origin="auditor", question="Which approach?", rationale="Ambiguous")
    state = _make_state(new_run_id(), decision_request=request.to_dict())
    save_state(tmp_path, state)

    loaded = load_state(tmp_path, state.run_id)
    assert loaded.decision_request == request.to_dict()
    assert DecisionRequest.from_dict(loaded.decision_request) == request


def test_decision_request_rejects_invalid_origin():
    with pytest.raises(StateError):
        DecisionRequest.from_dict({"origin": "builder", "question": "q", "rationale": "r"})
