import json
import os
import socket
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

import loop_supervisor.read_model.snapshot as snapshot_module
from loop_supervisor.read_model import (
    LockActivity,
    ProjectResolution,
    ProjectSnapshot,
    build_snapshot,
)
from loop_supervisor.read_model.current_run import CurrentRun
from loop_supervisor.read_model.history import HistoryLoad, HistoryStatus
from loop_supervisor.read_model.verification import VerificationDiscovery
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
        verify_commands=("pytest",),
        verify_timeout=900.0,
    )


def _save_state(
    git_common_dir: Path,
    run_id: str,
    *,
    updated_at: str,
    phase: str = "planning",
    accepted_task_count: int = 0,
    revision_count: int = 0,
    replan_count: int = 0,
    architect_retry_count: int = 0,
    builder_guidance_count: int = 0,
    operational_retry_count: int = 0,
) -> None:
    task_state: dict[str, Any] = {}
    if phase in {"building", "verifying"}:
        task_state = {
            "original_task_id": "task-1",
            "task_worktree_path": "/worktrees/task-1",
            "task_branch": "loop/task-1",
            "task_base_commit": "a" * 40,
            "task_expected_head": "a" * 40,
            "task_status_snapshot": "",
            "planner_result": {
                "status": "READY",
                "task_id": "task-1",
                "objective": "Implement the task",
                "rationale": "The task is ready.",
                "acceptance_criteria": ["Implement the task"],
                "relevant_files": [],
                "design_questions": [],
                "decision_required": False,
                "decision_question": None,
                "decision_rationale": None,
            },
        }
    if phase == "verifying":
        task_state.update(
            builder_result={
                "task_id": "task-1",
                "objective": "Implement the task",
                "status": "COMPLETE",
                "implementation_summary": "Implemented the task.",
                "implementation_strategy": [],
                "tests_run": [],
                "test_results": [],
                "files_changed": [],
                "commit": "a" * 40,
                "open_concerns": [],
            },
            last_task_head="a" * 40,
        )
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
            phase=phase,
            accepted_task_count=accepted_task_count,
            revision_count=revision_count,
            replan_count=replan_count,
            architect_retry_count=architect_retry_count,
            builder_guidance_count=builder_guidance_count,
            operational_retry_count=operational_retry_count,
            **task_state,
        ),
    )
    path = git_common_dir / "loop-supervisor" / "runs" / f"{run_id}.json"
    state = json.loads(path.read_text())
    state["updated_at"] = updated_at
    path.write_text(json.dumps(state))


def _project(path: Path) -> ProjectResolution:
    return ProjectResolution(integration_root=path, git_common_dir=path)


def _write_history(
    tmp_path: Path,
    run_id: str,
    *,
    recorded_at: str,
    phase: str = "planning",
    phase_after: str = "planning",
    counters: dict[str, int] | None = None,
    seq: int = 1,
) -> None:
    record = {
        "seq": seq,
        "run_id": run_id,
        "phase": phase,
        "phase_after": phase_after,
        "status": "advanced",
        "recorded_at": recorded_at,
        "original_task_id": None,
        "counters": counters
        or {
            "accepted_task_count": 0,
            "revision_count": 0,
            "replan_count": 0,
            "architect_retry_count": 0,
            "builder_guidance_count": 0,
        },
        "result": None,
        "error": None,
    }
    directory = tmp_path / "loop-supervisor" / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{seq:04d}-{phase}.json").write_text(json.dumps(record))


def test_build_snapshot_returns_an_immutable_empty_snapshot_for_absent_runs(tmp_path: Path) -> None:
    snapshot = build_snapshot(_project(tmp_path))

    assert isinstance(snapshot, ProjectSnapshot)
    assert snapshot.runs == ()
    assert snapshot.lock.activity is LockActivity.ABSENT
    assert snapshot.diagnostics == ()
    with pytest.raises(FrozenInstanceError):
        snapshot.runs = ()  # type: ignore[misc]


@pytest.mark.parametrize(
    ("counter", "phase", "phase_after"),
    [
        ("revision_count", "planning", "building"),
        ("replan_count", "cleanup_branch", "planning"),
        ("architect_retry_count", "recording_decision", "building"),
        ("builder_guidance_count", "building", "verifying"),
    ],
)
def test_build_snapshot_suppresses_resettable_disagreements_when_current_is_later(
    tmp_path: Path, counter: str, phase: str, phase_after: str
) -> None:
    _save_state(
        tmp_path,
        "run-1",
        updated_at="2026-01-01T00:00:01+00:00",
        phase=phase_after,
    )
    counters = {
        "accepted_task_count": 0,
        "revision_count": 0,
        "replan_count": 0,
        "architect_retry_count": 0,
        "builder_guidance_count": 0,
    }
    counters[counter] = 1
    _write_history(
        tmp_path,
        "run-1",
        recorded_at="2026-01-01T00:00:00+00:00",
        phase=phase,
        phase_after=phase_after,
        counters=counters,
    )

    detail = build_snapshot(_project(tmp_path)).detail_for("run-1")

    assert detail.current.phase == phase_after
    assert detail.history.entries[0].phase == phase
    assert detail.history.entries[0].phase_after == phase_after
    assert detail.history.entries[0].counters[counter] == 1
    assert detail.current_state_disagreements == ()


def test_build_snapshot_suppresses_operational_retry_disagreement_when_current_may_be_later(
    tmp_path: Path,
) -> None:
    _save_state(
        tmp_path,
        "run-1",
        updated_at="2026-01-01T00:00:01+00:00",
        operational_retry_count=0,
    )
    _write_history(
        tmp_path,
        "run-1",
        recorded_at="2026-01-01T00:00:00+00:00",
        counters={
            "accepted_task_count": 0,
            "revision_count": 0,
            "replan_count": 0,
            "architect_retry_count": 0,
            "builder_guidance_count": 0,
            "operational_retry_count": 1,
        },
    )

    detail = build_snapshot(_project(tmp_path)).detail_for("run-1")

    assert detail.current.operational_retry_count == 0
    assert detail.history.entries[0].counters["operational_retry_count"] == 1
    assert detail.current_state_disagreements == ()


def test_build_snapshot_loads_history_before_current_state(tmp_path: Path, monkeypatch) -> None:
    _save_state(tmp_path, "run-1", updated_at="2026-01-01T00:00:00+00:00")
    calls: list[str] = []
    original_history = snapshot_module.load_history
    original_current = snapshot_module.load_current_run

    def record_history(git_common_dir: Path, run_id: str) -> HistoryLoad:
        calls.append("history")
        return original_history(git_common_dir, run_id)

    def record_current(git_common_dir: Path, run_id: str) -> CurrentRun:
        calls.append("current")
        return original_current(git_common_dir, run_id)

    monkeypatch.setattr(snapshot_module, "load_history", record_history)
    monkeypatch.setattr(snapshot_module, "load_current_run", record_current)

    build_snapshot(_project(tmp_path))

    assert calls == ["history", "current"]


def test_build_snapshot_reports_same_transition_phase_and_counter_disagreements(
    tmp_path: Path,
) -> None:
    _save_state(
        tmp_path,
        "run-1",
        updated_at="2026-01-01T00:00:00+00:00",
        operational_retry_count=0,
    )
    _write_history(
        tmp_path,
        "run-1",
        recorded_at="2026-01-01T00:00:01+00:00",
        phase_after="building",
        counters={
            "accepted_task_count": 1,
            "revision_count": 1,
            "replan_count": 1,
            "architect_retry_count": 1,
            "builder_guidance_count": 1,
            "operational_retry_count": 1,
        },
    )

    detail = build_snapshot(_project(tmp_path)).detail_for("run-1")

    assert [(item.field, item.history_seq) for item in detail.current_state_disagreements] == [
        ("phase", 1),
        ("accepted_task_count", 1),
        ("revision_count", 1),
        ("replan_count", 1),
        ("architect_retry_count", 1),
        ("builder_guidance_count", 1),
        ("operational_retry_count", 1),
    ]
    assert detail.current.phase == "planning"
    assert detail.history.entries[0].phase_after == "building"


@pytest.mark.parametrize("updated_at", ["2026-01-01T00:00:01+00:00", "2026-01-01T00:00:00+00:00"])
def test_build_snapshot_reports_only_accepted_count_when_current_is_not_older(
    tmp_path: Path, updated_at: str
) -> None:
    _save_state(tmp_path, "run-1", updated_at=updated_at)
    _write_history(
        tmp_path,
        "run-1",
        recorded_at="2026-01-01T00:00:00+00:00",
        phase_after="building",
        counters={
            "accepted_task_count": 1,
            "revision_count": 1,
            "replan_count": 1,
            "architect_retry_count": 1,
            "builder_guidance_count": 1,
        },
    )

    detail = build_snapshot(_project(tmp_path)).detail_for("run-1")

    assert [(item.field, item.history_seq) for item in detail.current_state_disagreements] == [
        ("accepted_task_count", 1)
    ]


def test_build_snapshot_reports_no_disagreement_for_coherent_or_unavailable_sources(
    tmp_path: Path,
) -> None:
    _save_state(tmp_path, "coherent", updated_at="2026-01-01T00:00:00+00:00")
    _write_history(
        tmp_path,
        "coherent",
        recorded_at="2026-01-01T00:00:01+00:00",
    )
    _save_state(tmp_path, "absent", updated_at="2026-01-01T00:00:00+00:00")
    (tmp_path / "loop-supervisor" / "runs" / "unloadable.json").write_text("not JSON")
    _write_history(
        tmp_path,
        "unloadable",
        recorded_at="2026-01-01T00:00:01+00:00",
        phase_after="building",
    )

    snapshot = build_snapshot(_project(tmp_path))

    assert snapshot.detail_for("coherent").current_state_disagreements == ()
    assert snapshot.detail_for("absent").current_state_disagreements == ()
    assert snapshot.detail_for("unloadable").current.loadable is False
    assert snapshot.detail_for("unloadable").current_state_disagreements == ()


def test_build_snapshot_compares_newest_valid_history_after_malformed_newest_artifact(
    tmp_path: Path,
) -> None:
    _save_state(tmp_path, "run-1", updated_at="2026-01-01T00:00:00+00:00")
    _write_history(
        tmp_path,
        "run-1",
        recorded_at="2026-01-01T00:00:01+00:00",
        phase_after="building",
        counters={
            "accepted_task_count": 0,
            "revision_count": 1,
            "replan_count": 0,
            "architect_retry_count": 0,
            "builder_guidance_count": 0,
        },
    )
    malformed = tmp_path / "loop-supervisor" / "runs" / "run-1" / "0002-building.json"
    malformed.write_text("not JSON")

    detail = build_snapshot(_project(tmp_path)).detail_for("run-1")

    assert [(entry.seq, entry.phase_after) for entry in detail.history.entries] == [(1, "building")]
    assert [
        (diagnostic.artifact, diagnostic.reason) for diagnostic in detail.history.diagnostics
    ] == [("0002-building.json", "malformed history record")]
    assert [(item.field, item.history_seq) for item in detail.current_state_disagreements] == [
        ("phase", 1),
        ("revision_count", 1),
    ]
    assert detail.current.phase == "planning"
    assert detail.current.revision_count == 0


def test_build_snapshot_rereads_disk_without_reusing_prior_artifact_values(tmp_path: Path) -> None:
    project = _project(tmp_path)
    first = build_snapshot(project)
    _save_state(tmp_path, "new", updated_at="2026-01-02T00:00:00+00:00")

    second = build_snapshot(project)

    assert first.runs == ()
    assert [run.run_id for run in second.runs] == ["new"]


def test_build_snapshot_carries_per_run_detail_metadata_for_loadable_and_degraded_runs(
    tmp_path: Path,
) -> None:
    _save_state(tmp_path, "loadable", updated_at="2026-01-02T00:00:00+00:00")
    (tmp_path / "loop-supervisor" / "runs" / "degraded.json").write_text("not JSON")

    snapshot = build_snapshot(_project(tmp_path))

    metadata_by_run = {item.summary.run_id: item for item in snapshot.run_details}
    assert isinstance(metadata_by_run["loadable"].current, CurrentRun)
    assert metadata_by_run["loadable"].current.loadable is True
    assert isinstance(metadata_by_run["loadable"].history, HistoryLoad)
    assert metadata_by_run["loadable"].history.completeness is HistoryStatus.ABSENT
    assert isinstance(metadata_by_run["loadable"].verification, VerificationDiscovery)
    assert metadata_by_run["loadable"].verification.attempts == ()
    assert metadata_by_run["degraded"].current.loadable is False
    assert metadata_by_run["degraded"].history.completeness is HistoryStatus.ABSENT
    assert metadata_by_run["degraded"].verification.attempts == ()


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

    assert snapshot.lock.activity is LockActivity.LEGACY_UNVERIFIED
    assert snapshot.lock.activities[0].run_id == snapshot.runs[0].run_id == "active"
    assert snapshot.lock.activities[0].label == "not_evidenced_running"


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
