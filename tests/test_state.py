import json
import os
from typing import Any

import pytest

import loop_supervisor.state as state_mod
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
    validate_run_id,
)


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


def _make_state(run_id: str, **overrides) -> RunState:
    defaults: dict[str, Any] = dict(
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


def test_save_sets_permissions_under_restrictive_umask(tmp_path):
    state = _make_state(new_run_id())
    (tmp_path / "loop-supervisor" / "runs").mkdir(parents=True, mode=0o700)
    previous_umask = os.umask(0o777)
    try:
        save_state(tmp_path, state)
    finally:
        os.umask(previous_umask)

    assert state_path(tmp_path, state.run_id).stat().st_mode & 0o777 == 0o600


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
        provision_commands=("python3 -m venv .venv",),
        provision_timeout=123.0,
        verify_commands=("pytest -q", "ruff check ."),
        verify_timeout=456.0,
    )
    state = _make_state(new_run_id(), options=options)
    save_state(tmp_path, state)

    loaded = load_state(tmp_path, state.run_id)
    assert loaded.options == options
    assert loaded.options.provision_commands == ("python3 -m venv .venv",)
    assert loaded.options.verify_commands == ("pytest -q", "ruff check .")


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
    assert loaded.decision_request is not None
    assert DecisionRequest.from_dict(loaded.decision_request) == request


def test_decision_request_rejects_invalid_origin():
    with pytest.raises(StateError):
        DecisionRequest.from_dict({"origin": "builder", "question": "q", "rationale": "r"})


def _make_cleanup_state(run_id: str, phase: str, **overrides) -> RunState:
    """Make a state with full task identity in a cleanup phase."""
    base: dict[str, Any] = dict(
        schema_version=STATE_SCHEMA_VERSION,
        run_id=run_id,
        git_common_dir="/repo/.git",
        integration_path="/repo",
        integration_branch="main",
        integration_commit_at_start="abc123",
        options=_make_options(),
        integration_expected_head="abc123",
        integration_status_snapshot="",
        original_task_id="task-1",
        task_worktree_path="/tmp/worktrees/task-1",
        task_branch="feature/task-1",
        task_base_commit="abc123",
        task_expected_head="def456",
        task_status_snapshot="",
        merge_pre_head="abc123",
        merge_task_head="def456",
        merge_commit="ghi789",
        phase=phase,
    )
    base.update(overrides)
    return RunState(**base)


def test_cleanup_worktree_state_loads_with_full_task_identity(tmp_path):
    state = _make_cleanup_state(new_run_id(), "cleanup_worktree")
    save_state(tmp_path, state)
    loaded = load_state(tmp_path, state.run_id)
    assert loaded.phase == "cleanup_worktree"
    assert loaded.task_worktree_path == "/tmp/worktrees/task-1"
    assert loaded.task_branch == "feature/task-1"


def test_cleanup_branch_state_loads_with_full_task_identity(tmp_path):
    state = _make_cleanup_state(new_run_id(), "cleanup_branch")
    save_state(tmp_path, state)
    loaded = load_state(tmp_path, state.run_id)
    assert loaded.phase == "cleanup_branch"
    assert loaded.original_task_id == "task-1"
    assert loaded.task_branch == "feature/task-1"


def test_cleanup_worktree_requires_merge_commit(tmp_path):
    state = _make_cleanup_state(new_run_id(), "cleanup_worktree", merge_commit=None)
    data = state.to_dict()
    import json

    path = tmp_path / "loop-supervisor" / "runs"
    path.mkdir(parents=True)
    (path / f"{state.run_id}.json").write_text(json.dumps(data))
    with pytest.raises(StateError, match="merge_commit"):
        load_state(tmp_path, state.run_id)


def test_cleanup_branch_requires_merge_pre_head(tmp_path):
    state = _make_cleanup_state(new_run_id(), "cleanup_branch", merge_pre_head=None)
    data = state.to_dict()
    import json

    path = tmp_path / "loop-supervisor" / "runs"
    path.mkdir(parents=True)
    (path / f"{state.run_id}.json").write_text(json.dumps(data))
    with pytest.raises(StateError, match="merge_pre_head"):
        load_state(tmp_path, state.run_id)


def test_operational_failure_validates_last_error_schema(tmp_path):
    import uuid
    from datetime import UTC, datetime

    from loop_supervisor.state import OperationalErrorRecord

    record = OperationalErrorRecord(
        error_id=uuid.uuid4().hex[:12],
        kind="git",
        operation="merging",
        failed_phase="merging",
        retry_phase="merging",
        exception_type="GitError",
        message="conflict",
        retryable=True,
        requires_repair=True,
        recovery_hint="Resolve and resume.",
        occurred_at=datetime.now(UTC).isoformat(),
    )
    state = _make_state(new_run_id(), phase="operational_failure", last_error=record.to_dict())
    save_state(tmp_path, state)
    loaded = load_state(tmp_path, state.run_id)
    assert loaded.phase == "operational_failure"
    assert loaded.last_error is not None
    assert loaded.last_error["kind"] == "git"


def test_operational_failure_rejects_missing_last_error(tmp_path):
    import json

    state = _make_state(new_run_id(), phase="operational_failure", last_error=None)
    data = state.to_dict()
    path = tmp_path / "loop-supervisor" / "runs"
    path.mkdir(parents=True)
    (path / f"{state.run_id}.json").write_text(json.dumps(data))
    with pytest.raises(StateError, match="requires last_error"):
        load_state(tmp_path, state.run_id)


def test_operational_failure_rejects_unknown_error_fields(tmp_path):
    import json
    import uuid
    from datetime import UTC, datetime

    raw_error = {
        "error_id": uuid.uuid4().hex[:12],
        "kind": "git",
        "operation": "merging",
        "failed_phase": "merging",
        "retry_phase": "merging",
        "exception_type": "GitError",
        "message": "conflict",
        "retryable": True,
        "requires_repair": True,
        "recovery_hint": None,
        "occurred_at": datetime.now(UTC).isoformat(),
        "unexpected_field": "surprise",
    }
    state = _make_state(new_run_id(), phase="operational_failure", last_error=raw_error)
    data = state.to_dict()
    path = tmp_path / "loop-supervisor" / "runs"
    path.mkdir(parents=True)
    (path / f"{state.run_id}.json").write_text(json.dumps(data))
    with pytest.raises(StateError, match="unknown fields"):
        load_state(tmp_path, state.run_id)


# -- run_id validation / traversal safety -----------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        ".",
        "..",
        "../evil",
        "../../etc/passwd",
        "a/b",
        "a\\b",
        "/etc/passwd",
        "a" * 129,
        ".hidden",
        "-leading-dash",
    ],
)
def test_validate_run_id_rejects_unsafe_values(bad_id):
    with pytest.raises(StateError):
        validate_run_id(bad_id)


@pytest.mark.parametrize("good_id", ["run-1", "abc123", "a", "run.1_2-3", new_run_id()])
def test_validate_run_id_accepts_safe_values(good_id):
    assert validate_run_id(good_id) == good_id


def test_validate_run_id_rejects_non_string():
    with pytest.raises(StateError):
        validate_run_id(123)
    with pytest.raises(StateError):
        validate_run_id(None)


def test_state_path_rejects_traversal(tmp_path):
    with pytest.raises(StateError):
        state_path(tmp_path, "../../etc/passwd")


def test_load_state_rejects_traversal_before_filesystem_access(tmp_path):
    # A file that traversal would otherwise reach, one level above the
    # common dir, containing arbitrary non-JSON content: if traversal
    # validation is skipped, this would fail with a JSON/other error
    # instead of StateError, proving the path was actually opened.
    outside = tmp_path.parent / "outside-secret.json"
    outside.write_text("not json at all {{{")
    try:
        with pytest.raises(StateError):
            load_state(tmp_path, f"../{outside.name[:-5]}")
    finally:
        outside.unlink()


def test_save_state_rejects_unsafe_embedded_run_id(tmp_path):
    state = _make_state("../../evil")
    with pytest.raises(StateError):
        save_state(tmp_path, state)


def test_load_state_rejects_embedded_run_id_mismatch(tmp_path):
    state = _make_state(new_run_id())
    save_state(tmp_path, state)
    path = state_path(tmp_path, state.run_id)
    data = json.loads(path.read_text())

    # Tamper with the file in place: the filename (and thus the requested
    # run_id) stays the same, but the embedded run_id now names a
    # different run.
    data["run_id"] = new_run_id()
    path.write_text(json.dumps(data))

    with pytest.raises(StateError, match="embedded"):
        load_state(tmp_path, state.run_id)


def test_load_state_rejects_non_object_top_level_json(tmp_path):
    run_id = new_run_id()
    path = tmp_path / "loop-supervisor" / "runs"
    path.mkdir(parents=True)
    (path / f"{run_id}.json").write_text(json.dumps([1, 2, 3]))
    with pytest.raises(StateError):
        load_state(tmp_path, run_id)


def test_list_runs_skips_unsafe_filenames(tmp_path):
    state = _make_state(new_run_id())
    save_state(tmp_path, state)
    runs_dir = tmp_path / "loop-supervisor" / "runs"
    # Not producible through validate_run_id, but simulate a stray file
    # that could exist on disk (e.g. leftover manual edit) with a leading
    # dot, which validate_run_id also rejects.
    (runs_dir / ".hidden.json").write_text("{}")
    result = list_runs(tmp_path)
    assert result == [state.run_id]


# -- state storage symlink safety ---------------------------------------------


@pytest.mark.parametrize("capability", ["O_NOFOLLOW", "O_DIRECTORY"])
def test_state_storage_fails_closed_without_required_open_capability(
    tmp_path, monkeypatch, capability
):
    monkeypatch.delattr(state_mod.os, capability)
    state = _make_state("unsupported-platform")

    with pytest.raises(StateError, match=rf"requires os\.{capability}"):
        save_state(tmp_path, state)
    with pytest.raises(StateError, match=rf"requires os\.{capability}"):
        load_state(tmp_path, state.run_id)

    assert not (tmp_path / "loop-supervisor").exists()


def test_missing_no_follow_capability_cannot_read_or_modify_symlink_target(tmp_path, monkeypatch):
    outside = tmp_path.parent / "outside-unsupported-state"
    outside.mkdir()
    outside_runs = outside / "runs"
    outside_runs.mkdir()
    run_id = "unsupported-symlink"
    target = outside_runs / f"{run_id}.json"
    target.write_text("outside secret\n")
    storage = tmp_path / "loop-supervisor"
    storage.symlink_to(outside, target_is_directory=True)
    monkeypatch.delattr(state_mod.os, "O_NOFOLLOW")

    with pytest.raises(StateError, match=r"requires os\.O_NOFOLLOW"):
        save_state(tmp_path, _make_state(run_id))
    with pytest.raises(StateError, match=r"requires os\.O_NOFOLLOW"):
        load_state(tmp_path, run_id)

    assert storage.is_symlink()
    assert target.read_text() == "outside secret\n"
    assert sorted(path.name for path in outside_runs.iterdir()) == [target.name]


@pytest.mark.parametrize("failure_stage", ["fchmod", "fdopen"])
def test_save_closes_temporary_fd_on_setup_failure(tmp_path, monkeypatch, failure_stage):
    original_open = os.open
    original_close = os.close
    original_fchmod = os.fchmod
    original_fdopen = os.fdopen
    temporary_fd: list[int] = []
    close_count = 0

    def _recording_open(path, *args, **kwargs):
        fd = original_open(path, *args, **kwargs)
        if str(path).startswith(".tmp-"):
            temporary_fd.append(fd)
        return fd

    def _fchmod(fd, mode):
        if failure_stage == "fchmod" and temporary_fd == [fd]:
            raise OSError("simulated state temporary fchmod failure")
        return original_fchmod(fd, mode)

    def _fdopen(fd, *args, **kwargs):
        if failure_stage == "fdopen" and temporary_fd == [fd]:
            raise OSError("simulated state temporary fdopen failure")
        return original_fdopen(fd, *args, **kwargs)

    def _recording_close(fd):
        nonlocal close_count
        if temporary_fd == [fd]:
            close_count += 1
        return original_close(fd)

    monkeypatch.setattr(state_mod.os, "open", _recording_open)
    monkeypatch.setattr(state_mod.os, "fchmod", _fchmod)
    monkeypatch.setattr(state_mod.os, "fdopen", _fdopen)
    monkeypatch.setattr(state_mod.os, "close", _recording_close)
    state = _make_state("temporary-setup-failure")

    with pytest.raises(StateError, match="could not be saved") as caught:
        save_state(tmp_path, state)

    assert isinstance(caught.value.__cause__, OSError)
    assert len(temporary_fd) == 1
    assert close_count == 1
    assert not state_path(tmp_path, state.run_id).exists()
    assert not list((tmp_path / "loop-supervisor" / "runs").glob(".tmp-*"))


@pytest.mark.parametrize("component", ["loop-supervisor", "runs"])
def test_save_rejects_symlinked_state_directory_and_leaves_target_untouched(tmp_path, component):
    outside = tmp_path.parent / f"outside-save-{component}"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("outside\n")

    if component == "loop-supervisor":
        (tmp_path / "loop-supervisor").symlink_to(outside, target_is_directory=True)
    else:
        parent = tmp_path / "loop-supervisor"
        parent.mkdir()
        (parent / "runs").symlink_to(outside, target_is_directory=True)

    state = _make_state("symlink-save")
    with pytest.raises(StateError, match="symbolic link"):
        save_state(tmp_path, state)

    assert marker.read_text() == "outside\n"
    assert sorted(path.name for path in outside.iterdir()) == ["marker.txt"]


@pytest.mark.parametrize("component", ["loop-supervisor", "runs"])
def test_load_rejects_symlinked_state_directory_without_reading_target(tmp_path, component):
    outside = tmp_path.parent / f"outside-load-{component}"
    outside.mkdir()
    run_id = "symlink-load"
    target_parent = outside / "runs" if component == "loop-supervisor" else outside
    target_parent.mkdir(exist_ok=True)
    target = target_parent / f"{run_id}.json"
    target.write_text("outside secret that is not JSON")

    if component == "loop-supervisor":
        (tmp_path / "loop-supervisor").symlink_to(outside, target_is_directory=True)
    else:
        parent = tmp_path / "loop-supervisor"
        parent.mkdir()
        (parent / "runs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(StateError, match="symbolic link"):
        load_state(tmp_path, run_id)

    assert target.read_text() == "outside secret that is not JSON"


def test_save_rejects_state_file_symlink_and_leaves_target_untouched(tmp_path):
    run_id = "symlink-save-leaf"
    runs = tmp_path / "loop-supervisor" / "runs"
    runs.mkdir(parents=True)
    outside = tmp_path.parent / "outside-save-state.json"
    outside.write_text("outside state\n")
    target = runs / f"{run_id}.json"
    target.symlink_to(outside)

    with pytest.raises(StateError, match="symbolic link"):
        save_state(tmp_path, _make_state(run_id))

    assert target.is_symlink()
    assert outside.read_text() == "outside state\n"


def test_load_rejects_state_file_symlink_without_reading_target(tmp_path):
    run_id = "symlink-load-leaf"
    runs = tmp_path / "loop-supervisor" / "runs"
    runs.mkdir(parents=True)
    outside = tmp_path.parent / "outside-load-state.json"
    outside.write_text("outside secret that is not JSON")
    target = runs / f"{run_id}.json"
    target.symlink_to(outside)

    with pytest.raises(StateError, match="symbolic link"):
        load_state(tmp_path, run_id)

    assert target.is_symlink()
    assert outside.read_text() == "outside secret that is not JSON"


# -- strict phase / error-record validation ----------------------------------


def test_load_rejects_unknown_phase(tmp_path):
    state = _make_state(new_run_id())
    data = state.to_dict()
    data["phase"] = "not_a_real_phase"
    path = tmp_path / "loop-supervisor" / "runs"
    path.mkdir(parents=True)
    (path / f"{state.run_id}.json").write_text(json.dumps(data))
    with pytest.raises(StateError, match="unknown phase"):
        load_state(tmp_path, state.run_id)


def _base_error_record(**overrides):
    import uuid
    from datetime import UTC, datetime

    record = dict(
        error_id=uuid.uuid4().hex[:12],
        kind="git",
        operation="merging",
        failed_phase="merging",
        retry_phase="merging",
        exception_type="GitError",
        message="conflict",
        retryable=True,
        requires_repair=True,
        recovery_hint="Resolve and resume.",
        occurred_at=datetime.now(UTC).isoformat(),
    )
    record.update(overrides)
    return record


def test_error_record_rejects_retry_phase_operational_failure(tmp_path):
    raw_error = _base_error_record(retry_phase="operational_failure")
    state = _make_state(new_run_id(), phase="operational_failure", last_error=raw_error)
    data = state.to_dict()
    path = tmp_path / "loop-supervisor" / "runs"
    path.mkdir(parents=True)
    (path / f"{state.run_id}.json").write_text(json.dumps(data))
    with pytest.raises(StateError, match="retry_phase"):
        load_state(tmp_path, state.run_id)


def test_error_record_rejects_retryable_without_retry_phase(tmp_path):
    raw_error = _base_error_record(retry_phase=None)
    state = _make_state(new_run_id(), phase="operational_failure", last_error=raw_error)
    data = state.to_dict()
    path = tmp_path / "loop-supervisor" / "runs"
    path.mkdir(parents=True)
    (path / f"{state.run_id}.json").write_text(json.dumps(data))
    with pytest.raises(StateError):
        load_state(tmp_path, state.run_id)


def test_error_record_rejects_nonretryable_with_retry_phase(tmp_path):
    raw_error = _base_error_record(retryable=False, requires_repair=False, retry_phase="merging")
    state = _make_state(new_run_id(), phase="failed", last_error=raw_error)
    data = state.to_dict()
    path = tmp_path / "loop-supervisor" / "runs"
    path.mkdir(parents=True)
    (path / f"{state.run_id}.json").write_text(json.dumps(data))
    with pytest.raises(StateError):
        load_state(tmp_path, state.run_id)


def test_error_record_rejects_requires_repair_without_retryable(tmp_path):
    raw_error = _base_error_record(retryable=False, requires_repair=True, retry_phase=None)
    state = _make_state(new_run_id(), phase="failed", last_error=raw_error)
    data = state.to_dict()
    path = tmp_path / "loop-supervisor" / "runs"
    path.mkdir(parents=True)
    (path / f"{state.run_id}.json").write_text(json.dumps(data))
    with pytest.raises(StateError):
        load_state(tmp_path, state.run_id)


def test_error_record_rejects_unknown_failed_phase(tmp_path):
    raw_error = _base_error_record(failed_phase="not_a_real_phase")
    state = _make_state(new_run_id(), phase="operational_failure", last_error=raw_error)
    data = state.to_dict()
    path = tmp_path / "loop-supervisor" / "runs"
    path.mkdir(parents=True)
    (path / f"{state.run_id}.json").write_text(json.dumps(data))
    with pytest.raises(StateError):
        load_state(tmp_path, state.run_id)


def test_error_record_rejects_non_bool_retryable(tmp_path):
    raw_error = _base_error_record(retryable=1)
    state = _make_state(new_run_id(), phase="operational_failure", last_error=raw_error)
    data = state.to_dict()
    path = tmp_path / "loop-supervisor" / "runs"
    path.mkdir(parents=True)
    (path / f"{state.run_id}.json").write_text(json.dumps(data))
    with pytest.raises(StateError):
        load_state(tmp_path, state.run_id)


def test_error_record_rejects_naive_timestamp(tmp_path):
    raw_error = _base_error_record(occurred_at="2024-01-01T00:00:00")
    state = _make_state(new_run_id(), phase="operational_failure", last_error=raw_error)
    data = state.to_dict()
    path = tmp_path / "loop-supervisor" / "runs"
    path.mkdir(parents=True)
    (path / f"{state.run_id}.json").write_text(json.dumps(data))
    with pytest.raises(StateError):
        load_state(tmp_path, state.run_id)


def test_failed_phase_requires_nonretryable_error(tmp_path):
    raw_error = _base_error_record(retryable=True, requires_repair=True, retry_phase="merging")
    state = _make_state(new_run_id(), phase="failed", last_error=raw_error)
    data = state.to_dict()
    path = tmp_path / "loop-supervisor" / "runs"
    path.mkdir(parents=True)
    (path / f"{state.run_id}.json").write_text(json.dumps(data))
    with pytest.raises(StateError, match="failed"):
        load_state(tmp_path, state.run_id)


def test_operational_failure_requires_retryable_error(tmp_path):
    raw_error = _base_error_record(retryable=False, requires_repair=False, retry_phase=None)
    state = _make_state(new_run_id(), phase="operational_failure", last_error=raw_error)
    data = state.to_dict()
    path = tmp_path / "loop-supervisor" / "runs"
    path.mkdir(parents=True)
    (path / f"{state.run_id}.json").write_text(json.dumps(data))
    with pytest.raises(StateError, match="operational_failure"):
        load_state(tmp_path, state.run_id)


@pytest.mark.parametrize("version", [1.0, 2, 0, True, "1"])
def test_load_rejects_non_integer_schema_version(tmp_path, version):
    state = _make_state(new_run_id())
    save_state(tmp_path, state)
    path = state_path(tmp_path, state.run_id)
    data = json.loads(path.read_text())
    data["schema_version"] = version
    path.write_text(json.dumps(data))
    with pytest.raises(StateError):
        load_state(tmp_path, state.run_id)


# -- strict v3 required fields and scalar types -----------------------------


@pytest.mark.parametrize(
    "field",
    ["accepted_task_count", "created_at", "integration_branch", "phase"],
)
def test_load_rejects_missing_serialized_field(tmp_path, field):
    state = _make_state(new_run_id())
    save_state(tmp_path, state)
    path = state_path(tmp_path, state.run_id)
    data = json.loads(path.read_text())
    del data[field]
    path.write_text(json.dumps(data))
    with pytest.raises(StateError):
        load_state(tmp_path, state.run_id)


@pytest.mark.parametrize(
    "field,value",
    [
        ("accepted_task_count", -1),
        ("accepted_task_count", "5"),
        ("accepted_task_count", True),
        ("integration_branch", 123),
        ("integration_status_snapshot", None),
        ("created_at", ""),
    ],
)
def test_load_rejects_bad_scalar_types(tmp_path, field, value):
    state = _make_state(new_run_id())
    save_state(tmp_path, state)
    path = state_path(tmp_path, state.run_id)
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(StateError):
        load_state(tmp_path, state.run_id)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_accepted_tasks", -1),
        ("max_accepted_tasks", True),
        ("role_timeout", 0),
        ("role_timeout", float("inf")),
        ("role_timeout", "x"),
        ("require_decision_approval", "yes"),
        ("opencode_executable", ""),
        ("worktree_root", ""),
        ("provision_commands", "pytest -q"),
        ("provision_commands", [""]),
        ("provision_commands", [1]),
        ("provision_timeout", 0),
        ("provision_timeout", "x"),
        ("verify_commands", "pytest -q"),
        ("verify_commands", [""]),
        ("verify_timeout", 0),
        ("verify_timeout", float("inf")),
    ],
)
def test_load_rejects_bad_run_option_values(tmp_path, field, value):
    state = _make_state(new_run_id())
    save_state(tmp_path, state)
    path = state_path(tmp_path, state.run_id)
    data = json.loads(path.read_text())
    data["options"][field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(StateError):
        load_state(tmp_path, state.run_id)


# -- task identity / checkpoint invariants ----------------------------------


def _write_active_task(tmp_path, run_id, **overrides):
    data = _make_state(run_id, phase="building").to_dict()
    data.update(
        original_task_id="task-1",
        task_worktree_path="/tmp/wt/task-1",
        task_branch="feature/task-1",
        task_base_commit="abc123",
        task_expected_head="def456",
        task_status_snapshot="",
    )
    data.update(overrides)
    path = tmp_path / "loop-supervisor" / "runs"
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{run_id}.json").write_text(json.dumps(data))


def test_load_rejects_active_task_without_expected_head(tmp_path):
    run_id = new_run_id()
    _write_active_task(tmp_path, run_id, task_expected_head=None)
    with pytest.raises(StateError):
        load_state(tmp_path, run_id)


def test_load_rejects_active_task_without_status_snapshot(tmp_path):
    run_id = new_run_id()
    _write_active_task(tmp_path, run_id, task_status_snapshot=None)
    with pytest.raises(StateError):
        load_state(tmp_path, run_id)


def test_load_accepts_active_task_with_empty_status_snapshot(tmp_path):
    run_id = new_run_id()
    _write_active_task(tmp_path, run_id, task_status_snapshot="")
    loaded = load_state(tmp_path, run_id)
    assert loaded.task_status_snapshot == ""


def test_load_rejects_empty_string_task_identity(tmp_path):
    run_id = new_run_id()
    _write_active_task(tmp_path, run_id, task_branch="")
    with pytest.raises(StateError):
        load_state(tmp_path, run_id)


def test_load_rejects_task_checkpoints_without_identity(tmp_path):
    run_id = new_run_id()
    data = _make_state(run_id).to_dict()
    data["task_expected_head"] = "def456"
    path = tmp_path / "loop-supervisor" / "runs"
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{run_id}.json").write_text(json.dumps(data))
    with pytest.raises(StateError):
        load_state(tmp_path, run_id)


# -- error normalization -----------------------------------------------------


def test_load_wraps_malformed_json_as_state_error(tmp_path):
    run_id = new_run_id()
    path = tmp_path / "loop-supervisor" / "runs"
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{run_id}.json").write_text("{not valid json")
    with pytest.raises(StateError):
        load_state(tmp_path, run_id)


def test_failed_phase_requires_error_on_load(tmp_path):
    state = _make_state(new_run_id(), phase="failed")
    data = state.to_dict()
    assert data["last_error"] is None
    path = tmp_path / "loop-supervisor" / "runs"
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{state.run_id}.json").write_text(json.dumps(data))
    with pytest.raises(StateError):
        load_state(tmp_path, state.run_id)
