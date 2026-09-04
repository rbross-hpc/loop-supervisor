import json
import os
import socket
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from loop_supervisor.read_model import (
    LockActivity,
    ProjectResolution,
    ProjectSnapshot,
    build_snapshot,
)
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


def _save_state(git_common_dir: Path, run_id: str, *, updated_at: str) -> None:
    save_state(
        git_common_dir,
        RunState(
            schema_version=STATE_SCHEMA_VERSION,
            run_id=run_id,
            git_common_dir=str(git_common_dir),
            integration_path=str(git_common_dir),
            integration_branch="main",
            integration_commit_at_start="abc123",
            options=_options(),
            integration_expected_head="abc123",
            integration_status_snapshot="",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at=updated_at,
        ),
    )
    path = git_common_dir / "loop-supervisor" / "runs" / f"{run_id}.json"
    state = json.loads(path.read_text())
    state["updated_at"] = updated_at
    path.write_text(json.dumps(state))


def _project(path: Path) -> ProjectResolution:
    return ProjectResolution(integration_root=path, git_common_dir=path)


def test_build_snapshot_returns_an_immutable_empty_snapshot_for_absent_runs(tmp_path: Path) -> None:
    snapshot = build_snapshot(_project(tmp_path))

    assert isinstance(snapshot, ProjectSnapshot)
    assert snapshot.runs == ()
    assert snapshot.lock.activity is LockActivity.ABSENT
    assert snapshot.diagnostics == ()
    with pytest.raises(FrozenInstanceError):
        snapshot.runs = ()  # type: ignore[misc]


def test_build_snapshot_rereads_disk_without_reusing_prior_artifact_values(tmp_path: Path) -> None:
    project = _project(tmp_path)
    first = build_snapshot(project)
    _save_state(tmp_path, "new", updated_at="2026-01-02T00:00:00+00:00")

    second = build_snapshot(project)

    assert first.runs == ()
    assert [run.run_id for run in second.runs] == ["new"]


def test_build_snapshot_preserves_degraded_rows_and_orders_loadable_runs_newest_first(
    tmp_path: Path,
) -> None:
    _save_state(tmp_path, "older", updated_at="2026-01-01T00:00:00+00:00")
    _save_state(tmp_path, "newer", updated_at="2026-01-02T00:00:00+00:00")
    (tmp_path / "loop-supervisor" / "runs" / "broken.json").write_text("not JSON")

    snapshot = build_snapshot(_project(tmp_path))

    assert [run.run_id for run in snapshot.runs] == ["newer", "older", "broken"]
    assert snapshot.runs[2].loadable is False
    assert snapshot.runs[2].phase is None
    assert snapshot.runs[2].updated_at is None
    assert snapshot.runs[2].diagnostic is not None


def test_build_snapshot_reflects_lock_observation_for_its_discovered_runs(tmp_path: Path) -> None:
    _save_state(tmp_path, "active", updated_at="2026-01-02T00:00:00+00:00")
    lock_dir = tmp_path / "loop-supervisor"
    (lock_dir / "supervisor.lock").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "token": "secret",
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "started_at": "2026-01-01T00:00:00Z",
                "operation": "run",
                "run_id": "active",
                "integration_path": str(tmp_path),
            }
        )
    )

    snapshot = build_snapshot(_project(tmp_path))

    assert snapshot.lock.activity is LockActivity.LOCAL_LIVE_ASSOCIATED
    assert snapshot.lock.activities[0].run_id == snapshot.runs[0].run_id == "active"
    assert snapshot.lock.activities[0].label == "running"


def test_build_snapshot_reports_incomplete_diagnostic_after_10000_candidates(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "loop-supervisor" / "runs"
    runs.mkdir(parents=True)
    for index in range(10_001):
        (runs / f"run-{index}.json").write_text("{}")

    snapshot = build_snapshot(_project(tmp_path))

    assert len(snapshot.runs) == 10_000
    assert snapshot.diagnostics[0].code == "run_candidates_incomplete"
    assert "10000 candidates" in snapshot.diagnostics[0].message
