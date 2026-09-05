"""Textual coverage for the read-only run-browser landing screen."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any, cast

import pytest

from loop_supervisor.read_model import ProjectResolution, build_snapshot
from loop_supervisor.read_model.history import (
    HistoryDiagnostic,
    HistoryEntry,
    HistoryLoad,
    HistoryStatus,
)
from loop_supervisor.state import STATE_SCHEMA_VERSION, RunOptions, RunState, save_state
from loop_supervisor.supervisor import AdvanceStatus
from loop_supervisor.tui import RunBrowserApp


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


def _persist_run(git_common_dir: Path, run_id: str, *, updated_at: str) -> None:
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
            phase="done",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at=updated_at,
        ),
    )
    state_path = git_common_dir / "loop-supervisor" / "runs" / f"{run_id}.json"
    state = json.loads(state_path.read_text())
    state["updated_at"] = updated_at
    state_path.write_text(json.dumps(state))


def _persist_lock(git_common_dir: Path, *, run_id: str | None, hostname: str | None = None) -> None:
    directory = git_common_dir / "loop-supervisor"
    directory.mkdir(exist_ok=True)
    (directory / "supervisor.lock").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "token": "browser-test-ownership-token",
                "pid": os.getpid(),
                "hostname": hostname or socket.gethostname(),
                "started_at": "2026-01-03T00:00:00Z",
                "operation": "run",
                "run_id": run_id,
                "integration_path": str(git_common_dir),
            }
        )
    )


def _persist_history(
    git_common_dir: Path,
    run_id: str,
    name: str,
    *,
    seq: int,
    phase: str = "planning",
    phase_after: str = "creating_worktree",
    status: str = "advanced",
    recorded_at: str = "2026-01-03T00:00:00+00:00",
    has_result: bool = True,
    has_error: bool = False,
) -> None:
    directory = git_common_dir / "loop-supervisor" / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    record: dict[str, object] = {
        "seq": seq,
        "run_id": run_id,
        "phase": phase,
        "phase_after": phase_after,
        "status": status,
        "recorded_at": recorded_at,
        "original_task_id": None,
        "counters": {
            "accepted_task_count": 2,
            "revision_count": 1,
            "replan_count": 0,
            "architect_retry_count": 0,
            "builder_guidance_count": 0,
        },
        "result": None,
        "error": None,
    }
    if has_result:
        record["result"] = {
            "status": "COMPLETE",
            "task_id": None,
            "objective": None,
            "rationale": None,
            "acceptance_criteria": [],
            "relevant_files": [],
            "design_questions": [],
            "decision_required": False,
            "decision_question": None,
            "decision_rationale": None,
        }
    if has_error:
        record["error"] = {
            "error_id": "history-error",
            "kind": "operational",
            "operation": phase,
            "failed_phase": phase,
            "retry_phase": None,
            "exception_type": "RuntimeError",
            "message": "recorded failure",
            "retryable": False,
            "requires_repair": False,
            "recovery_hint": None,
            "occurred_at": "2026-01-03T00:00:00+00:00",
        }
    (directory / name).write_text(json.dumps(record))


@pytest.mark.asyncio
async def test_run_browser_lists_newest_loadable_runs_and_degraded_rows_then_quits(
    tmp_path: Path,
) -> None:
    _persist_run(tmp_path, "older", updated_at="2026-01-01T00:00:00+00:00")
    _persist_run(tmp_path, "newer", updated_at="2026-01-02T00:00:00+00:00")
    (tmp_path / "loop-supervisor" / "runs" / "broken.json").write_text("not JSON")

    snapshot = build_snapshot(ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path))
    app = RunBrowserApp(snapshot)
    async with app.run_test() as pilot:
        rendered_rows = [cast(Any, row.render()).plain for row in app.screen.query(".run-row")]

        assert ["newer" in row for row in rendered_rows] == [True, False, False]
        assert ["older" in row for row in rendered_rows] == [False, True, False]
        assert "broken" in rendered_rows[2]
        assert "unloadable" in rendered_rows[2]

        await pilot.press("q")

    assert app.is_running is False


@pytest.mark.asyncio
async def test_run_browser_displays_evidence_based_activity_and_safe_lock_details(
    tmp_path: Path,
) -> None:
    _persist_run(tmp_path, "associated", updated_at="2026-01-02T00:00:00+00:00")
    _persist_run(tmp_path, "other", updated_at="2026-01-01T00:00:00+00:00")
    _persist_lock(tmp_path, run_id="associated")

    snapshot = build_snapshot(ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path))
    app = RunBrowserApp(snapshot)
    async with app.run_test() as pilot:
        rendered_rows = [cast(Any, row.render()).plain for row in app.screen.query(".run-row")]
        associated_row = next(row for row in rendered_rows if row.startswith("associated"))
        other_row = next(row for row in rendered_rows if row.startswith("other"))
        assert "Activity: running" in associated_row
        assert "Activity: not evidenced running" in other_row

        await pilot.press("enter")

        detail = cast(Any, app.screen.query_one(".run-detail-summary").render()).plain
        assert "Activity: running" in detail
        assert "Lock observation: local live associated" in detail
        assert "Lock started: 2026-01-03T00:00:00Z" in detail
        assert f"Lock hostname: {socket.gethostname()}" in detail
        assert f"Lock PID: {os.getpid()}" in detail
        assert "Lock operation: run" in detail
        assert "Lock association: associated" in detail
        assert f"Lock integration path: {tmp_path}" in detail
        assert "browser-test-ownership-token" not in detail

        await pilot.press("b")
        (tmp_path / "loop-supervisor" / "supervisor.lock").unlink()
        await pilot.press("r")

        absent_rows = [cast(Any, row.render()).plain for row in app.screen.query(".run-row")]
        assert all(
            "Activity: not evidenced running (inactive at inspection time)" in row
            for row in absent_rows
        )


@pytest.mark.asyncio
async def test_run_detail_bounds_oversized_multiline_lock_metadata(tmp_path: Path) -> None:
    _persist_run(tmp_path, "selected", updated_at="2026-01-02T00:00:00+00:00")
    _persist_lock(tmp_path, run_id="selected", hostname=("host\n" * 100_001))

    snapshot = build_snapshot(ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path))
    app = RunBrowserApp(snapshot)
    async with app.run_test() as pilot:
        await pilot.press("enter")

        detail = cast(Any, app.screen.query_one(".run-detail-summary").render()).plain
        assert len(detail.encode("utf-8")) <= 256 * 1024
        assert len(detail.splitlines()) <= 10_000
        assert "Summary output truncated: rendered-output limit reached." in detail
        assert "Activity: not evidenced running" in detail
        assert "Lock observation: remote" in detail


@pytest.mark.asyncio
async def test_run_browser_manual_refresh_updates_rows_and_reconciles_removed_selection(
    tmp_path: Path,
) -> None:
    _persist_run(tmp_path, "older", updated_at="2026-01-01T00:00:00+00:00")

    snapshot = build_snapshot(ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path))
    app = RunBrowserApp(snapshot)
    async with app.run_test() as pilot:
        _persist_run(tmp_path, "newer", updated_at="2026-01-02T00:00:00+00:00")

        await pilot.press("r")

        refreshed_rows = [cast(Any, row.render()).plain for row in app.screen.query(".run-row")]
        assert ["newer" in row for row in refreshed_rows] == [True, False]
        assert ["older" in row for row in refreshed_rows] == [False, True]

        await pilot.press("enter")
        detail = cast(Any, app.screen.query_one(".run-detail-summary").render()).plain
        assert "Run ID: newer" in detail

        _persist_run(tmp_path, "newer", updated_at="2026-01-03T00:00:00+00:00")
        await pilot.press("r")

        refreshed_detail = cast(Any, app.screen.query_one(".run-detail-summary").render()).plain
        assert "Run ID: newer" in refreshed_detail
        assert "Updated: 2026-01-03T00:00:00+00:00" in refreshed_detail

        (tmp_path / "loop-supervisor" / "runs" / "newer.json").unlink()

        await pilot.press("r")

        assert app.screen.query_one("#run-browser")
        remaining_rows = [cast(Any, row.render()).plain for row in app.screen.query(".run-row")]
        assert ["older" in row for row in remaining_rows] == [True]


@pytest.mark.asyncio
async def test_run_browser_opens_authoritative_detail_and_returns_to_browser(
    tmp_path: Path,
) -> None:
    _persist_run(tmp_path, "selected", updated_at="2026-01-02T00:00:00+00:00")

    snapshot = build_snapshot(ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path))
    app = RunBrowserApp(snapshot)
    async with app.run_test() as pilot:
        await pilot.press("enter")

        detail = cast(Any, app.screen.query_one(".run-detail-summary").render()).plain
        assert "Run ID: selected" in detail
        assert "Durable phase: done" in detail
        assert "Created: 2026-01-01T00:00:00+00:00" in detail
        assert "Updated: 2026-01-02T00:00:00+00:00" in detail
        assert "Integration branch: main" in detail
        assert "Current task: unavailable" in detail
        assert "Accepted tasks: 0" in detail
        assert "Pending question: unavailable" in detail
        assert "Latest operational error: unavailable" in detail

        await pilot.press("b")

        assert app.screen.query_one("#run-browser")

        await pilot.press("enter")

        reopened_detail = cast(Any, app.screen.query_one(".run-detail-summary").render()).plain
        assert "Run ID: selected" in reopened_detail


@pytest.mark.asyncio
async def test_run_browser_opens_run_id_with_period(
    tmp_path: Path,
) -> None:
    _persist_run(tmp_path, "old.run", updated_at="2026-01-02T00:00:00+00:00")

    snapshot = build_snapshot(ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path))
    app = RunBrowserApp(snapshot)
    async with app.run_test() as pilot:
        await pilot.press("enter")

        detail = cast(Any, app.screen.query_one(".run-detail-summary").render()).plain
        assert "Run ID: old.run" in detail


@pytest.mark.asyncio
async def test_run_detail_opens_escaped_record_detail_and_expandable_raw_json(
    tmp_path: Path,
) -> None:
    _persist_run(tmp_path, "selected", updated_at="2026-01-04T00:00:00+00:00")
    _persist_history(tmp_path, "selected", "0001-planning.json", seq=1, has_error=True)
    _persist_history(tmp_path, "selected", "0002-planning.json", seq=2, has_result=False)
    record_path = tmp_path / "loop-supervisor" / "runs" / "selected" / "0001-planning.json"
    record = json.loads(record_path.read_text())
    record["result"]["objective"] = "[bold]literal result[/bold]"
    record["error"]["message"] = "[red]literal error[/red]"
    record_path.write_text(json.dumps(record))

    snapshot = build_snapshot(ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path))
    app = RunBrowserApp(snapshot)
    async with app.run_test() as pilot:
        await pilot.press("enter", "down", "enter")

        detail = cast(Any, app.screen.query_one(".record-detail").render()).plain
        assert "[bold]literal result[/bold]" in detail
        assert "[red]literal error[/red]" in detail
        assert "Raw JSON: collapsed (press r to expand)" in detail

        await pilot.press("r")

        raw_json = cast(Any, app.screen.query_one(".record-detail-raw-json").render()).plain
        assert '"objective": "[bold]literal result[/bold]"' in raw_json
        assert '"message": "[red]literal error[/red]"' in raw_json

        await pilot.press("b")
        await pilot.press("down", "down", "enter")

        unavailable = cast(Any, app.screen.query_one(".record-detail").render()).plain
        assert "Result: unavailable (none recorded)." in unavailable
        assert "Error: unavailable (none recorded)." in unavailable


@pytest.mark.asyncio
async def test_run_detail_renders_ordered_incomplete_history_timeline(tmp_path: Path) -> None:
    _persist_run(tmp_path, "selected", updated_at="2026-01-04T00:00:00+00:00")
    _persist_history(
        tmp_path,
        "selected",
        "0003-planning.json",
        seq=3,
        phase_after="creating_worktree",
        recorded_at="2026-01-03T03:00:00+00:00",
        has_result=False,
        has_error=True,
    )
    _persist_history(
        tmp_path,
        "selected",
        "0001-planning.json",
        seq=1,
        recorded_at="2026-01-03T01:00:00+00:00",
    )
    (tmp_path / "loop-supervisor" / "runs" / "selected" / "0002-planning.json").write_text("{")

    snapshot = build_snapshot(ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path))
    app = RunBrowserApp(snapshot)
    async with app.run_test() as pilot:
        await pilot.press("enter")

        timeline = cast(Any, app.screen.query_one(".run-detail-timeline").render()).plain
        assert timeline.index("Sequence 1") < timeline.index("Sequence 3")
        assert "Phase: planning → creating_worktree" in timeline
        assert "Outcome: advanced" in timeline
        assert "Recorded: 2026-01-03T01:00:00+00:00" in timeline
        expected_counters = (
            "Counters: accepted tasks=2, revisions=1, replans=0, "
            "architect retries=0, builder guidance=0"
        )
        assert expected_counters in timeline
        assert "Result: available; Error: unavailable" in timeline
        assert "Recorded: 2026-01-03T03:00:00+00:00" in timeline
        assert "Result: unavailable; Error: available" in timeline
        assert "Workflow timeline: incomplete" in timeline
        assert "0002-planning.json: malformed history record" in timeline


def test_timeline_rendering_reserves_incomplete_diagnostic_within_output_limits() -> None:
    entry = HistoryEntry(
        seq=1,
        phase="planning",
        phase_after="creating_worktree",
        status=AdvanceStatus.ADVANCED,
        recorded_at="2026-01-03T00:00:00+00:00",
        counters={
            "accepted_task_count": 2,
            "revision_count": 1,
            "replan_count": 0,
            "architect_retry_count": 0,
            "builder_guidance_count": 0,
        },
        original_task_id=None,
        has_result=True,
        has_error=False,
        result_detail=None,
        error_detail=None,
        raw_json="{}",
        raw_json_truncated=False,
    )
    history = HistoryLoad(
        entries=(entry,) * 10_000,
        completeness=HistoryStatus.INCOMPLETE,
        diagnostics=(HistoryDiagnostic("0002-planning.json", "malformed history record", 2),),
    )

    timeline = RunBrowserApp._render_history(history)

    assert len(timeline.encode("utf-8")) <= 256 * 1024
    assert len(timeline.splitlines()) <= 10_000
    assert "Timeline output truncated: rendered-output limit reached." in timeline
    assert "Workflow timeline: incomplete" in timeline
    assert "Timeline diagnostics:" in timeline
    assert "0002-planning.json: malformed history record" in timeline


@pytest.mark.asyncio
async def test_run_browser_opens_unloadable_run_as_safe_unavailable_detail_and_quits(
    tmp_path: Path,
) -> None:
    _persist_run(tmp_path, "loadable", updated_at="2026-01-02T00:00:00+00:00")
    (tmp_path / "loop-supervisor" / "runs" / "unloadable.json").write_text("not JSON")

    snapshot = build_snapshot(ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path))
    app = RunBrowserApp(snapshot)
    async with app.run_test() as pilot:
        await pilot.press("down", "enter")

        detail = cast(Any, app.screen.query_one(".run-detail-summary").render()).plain
        assert "Run ID: unloadable" in detail
        assert "Details: unavailable" in detail
        assert "Current run details are unavailable" in detail
        assert "Durable phase:" not in detail
        assert "Created:" not in detail
        assert "Accepted tasks:" not in detail

        timeline = cast(Any, app.screen.query_one(".run-detail-timeline").render()).plain
        assert timeline == "Workflow timeline: unavailable (no recorded history)."

        await pilot.press("q")

    assert app.is_running is False
