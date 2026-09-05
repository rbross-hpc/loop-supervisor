"""Textual coverage for the read-only run-browser landing screen."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any, cast

import pytest
from textual.widgets import Static

import loop_supervisor.read_model.discovery as discovery
import loop_supervisor.read_model.snapshot as snapshot_reader
import loop_supervisor.read_model.verification as verification
import loop_supervisor.tui.browser as browser
from loop_supervisor.read_model import (
    ProjectResolution,
    ProjectSnapshot,
    SnapshotDiagnostic,
    build_snapshot,
    load_current_run,
)
from loop_supervisor.read_model.history import (
    HistoryDiagnostic,
    HistoryEntry,
    HistoryLoad,
    HistoryStatus,
)
from loop_supervisor.read_model.lock_observation import (
    ActivityLabel,
    LockActivity,
    LockObservation,
    RunActivity,
)
from loop_supervisor.read_model.verification import (
    VerificationAttempt,
    VerificationDiagnostic,
    VerificationDiscovery,
)
from loop_supervisor.state import (
    STATE_SCHEMA_VERSION,
    RunOptions,
    RunState,
    StateError,
    load_state,
    save_state,
)
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


def _persist_run(
    git_common_dir: Path, run_id: str, *, updated_at: str, phase: str = "done"
) -> None:
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
            phase=phase,
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


def _verification_result(
    git_common_dir: Path, run_id: str, *, command: str = "pytest"
) -> dict[str, object]:
    commit = "a" * 40
    log = git_common_dir / "loop-supervisor" / "verification" / run_id / commit / "01.log"
    return {
        "ok": True,
        "commands": [
            {
                "command": command,
                "ok": True,
                "returncode": 0,
                "timed_out": False,
                "duration": 0.1,
                "output_path": str(log),
                "summary": "[green]literal summary[/green]",
            }
        ],
    }


def _persist_verification_result(
    git_common_dir: Path, run_id: str, result: dict[str, object]
) -> Path:
    state_path = git_common_dir / "loop-supervisor" / "runs" / f"{run_id}.json"
    state = json.loads(state_path.read_text())
    state.update(
        phase="auditing",
        original_task_id="task-1",
        task_worktree_path="/tmp/wt/task-1",
        task_branch="loop/task-1",
        task_base_commit="abc123",
        task_expected_head="d" * 40,
        task_status_snapshot="",
        last_task_head="d" * 40,
        planner_result={
            "status": "READY",
            "task_id": "task-1",
            "objective": "Implement the task",
            "rationale": "It is next",
            "acceptance_criteria": ["It works"],
            "relevant_files": [],
            "design_questions": [],
            "decision_required": False,
            "decision_question": None,
            "decision_rationale": None,
        },
        builder_result={
            "task_id": "task-1",
            "objective": "Implement the task",
            "status": "COMPLETE",
            "implementation_summary": "Implemented it",
            "implementation_strategy": [],
            "tests_run": [],
            "test_results": [],
            "files_changed": [],
            "commit": "d" * 40,
            "open_concerns": [],
        },
        verification_result=result,
    )
    commands = result["commands"]
    assert isinstance(commands, list)
    command = commands[0]
    assert isinstance(command, dict)
    state["options"]["verify_commands"] = [command["command"]]
    state_path.write_text(json.dumps(state))
    return state_path


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
async def test_run_browser_bounds_sanitized_degraded_row_and_snapshot_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _persist_run(tmp_path, "degraded", updated_at="2026-01-02T00:00:00+00:00")
    sensitive_path = "/private/credentials/state.json"

    def fail_loading(_git_common_dir: Path, _run_id: str) -> RunState:
        raise StateError(f"malformed snapshot at {sensitive_path}: {'secret' * 100_000}")

    monkeypatch.setattr(discovery, "load_state", fail_loading)
    snapshot = build_snapshot(ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path))
    oversized_diagnostic = "scan diagnostic\n" * 100_001
    bounded_snapshot = ProjectSnapshot(
        project=snapshot.project,
        runs=snapshot.runs,
        run_details=snapshot.run_details,
        lock=snapshot.lock,
        diagnostics=(SnapshotDiagnostic(code="scan_incomplete", message=oversized_diagnostic),),
    )
    app = RunBrowserApp(bounded_snapshot)
    async with app.run_test():
        row = cast(Any, app.screen.query_one(".run-row").render()).plain
        diagnostic = cast(Any, app.screen.query_one(".snapshot-diagnostic").render()).plain

        for rendered in (row, diagnostic):
            assert len(rendered.encode("utf-8")) <= 256 * 1024
            assert len(rendered.splitlines()) <= 10_000
        assert "Browser output truncated: rendered-output limit reached." in diagnostic
        assert "Run row is unloadable because the snapshot is malformed." in row
        assert "Activity: not evidenced running (inactive at inspection time)" in row
        assert sensitive_path not in row
        assert "secret" not in row


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
        assert "Activity: not evidenced running" in associated_row
        assert "Activity: not evidenced running" in other_row

        await pilot.press("enter")

        detail = cast(Any, app.screen.query_one(".run-detail-summary").render()).plain
        assert "Activity: not evidenced running" in detail
        assert "Lock observation: legacy unverified" in detail
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


def test_run_browser_renders_legacy_stale_and_unverifiable_lock_categories() -> None:
    """Repository-level evidence stays explicit while each run remains non-running."""
    run = RunActivity(run_id="run-1", label=ActivityLabel.NOT_EVIDENCED_RUNNING)
    for activity, expected in (
        (LockActivity.LEGACY_UNVERIFIED, "legacy unverified"),
        (LockActivity.STALE, "stale"),
        (LockActivity.LOCAL_UNVERIFIABLE, "local unverifiable"),
    ):
        lock = LockObservation(
            activity=activity,
            activities=(run,),
            pid=None,
            hostname=None,
            owner_boot_id=None,
            owner_process_start=None,
            started_at=None,
            operation=None,
            run_id=None,
            integration_path=None,
            diagnostic=None,
        )

        assert RunBrowserApp._activity_label("run-1", lock) == "not evidenced running"
        assert RunBrowserApp._render_lock_observation(lock) == (f"Lock observation: {expected}",)


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
async def test_run_browser_refresh_failure_preserves_snapshot_selection_and_closes_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "selected"
    _persist_run(tmp_path, run_id, updated_at="2026-01-04T00:00:00+00:00", phase="auditing")
    _persist_verification_result(tmp_path, run_id, _verification_result(tmp_path, run_id))
    log = tmp_path / "loop-supervisor" / "verification" / run_id / ("a" * 40) / "01.log"
    log.parent.mkdir(parents=True)
    log.write_text("previously opened log")
    snapshot = build_snapshot(ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path))

    def fail_scan(_: ProjectResolution) -> object:
        raise StateError("raw replaced-state-directory path must not be rendered")

    monkeypatch.setattr(browser, "build_snapshot", fail_scan)
    app = RunBrowserApp(snapshot)
    async with app.run_test() as pilot:
        await pilot.press("enter", "tab", "enter")
        assert app.screen.query_one("#verification-log-viewer")

        await pilot.press("r")

        detail = cast(Any, app.screen.query_one(".run-detail-summary").render()).plain
        diagnostic = cast(Any, app.screen.query_one(".refresh-failure").render()).plain
        assert app._snapshot is snapshot
        assert tuple(summary.run_id for summary in app._snapshot.runs) == (run_id,)
        assert "Run ID: selected" in detail
        assert app._selected_run_id == run_id
        assert app._opened_log is None
        assert app._selected_log_reference is None
        assert diagnostic == "Refresh failed: unable to scan supervisor run state."
        assert "raw replaced-state-directory path" not in diagnostic


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
async def test_run_detail_navigation_uses_snapshot_metadata_until_explicit_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _persist_run(tmp_path, "selected", updated_at="2026-01-02T00:00:00+00:00")
    counts = {"current": 0, "history": 0, "verification": 0}
    original_current = snapshot_reader.load_current_run
    original_history = snapshot_reader.load_history
    original_verification = snapshot_reader.discover_verification

    def count_current(git_common_dir: Path, run_id: str) -> object:
        counts["current"] += 1
        return original_current(git_common_dir, run_id)

    def count_history(git_common_dir: Path, run_id: str) -> object:
        counts["history"] += 1
        return original_history(git_common_dir, run_id)

    def count_verification(
        git_common_dir: Path, run_id: str, verification_result: object
    ) -> object:
        counts["verification"] += 1
        return original_verification(git_common_dir, run_id, verification_result)

    monkeypatch.setattr(snapshot_reader, "load_current_run", count_current, raising=False)
    monkeypatch.setattr(snapshot_reader, "load_history", count_history, raising=False)
    monkeypatch.setattr(snapshot_reader, "discover_verification", count_verification, raising=False)
    monkeypatch.setattr(browser, "load_current_run", count_current, raising=False)
    monkeypatch.setattr(browser, "load_history", count_history, raising=False)
    monkeypatch.setattr(browser.verification, "discover_verification", count_verification)
    snapshot = build_snapshot(ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path))
    scan_counts = counts.copy()

    app = RunBrowserApp(snapshot)
    async with app.run_test() as pilot:
        await pilot.press("enter", "enter", "b", "b", "enter")

        assert counts == scan_counts

        await pilot.press("r")

        assert counts == {name: count + 1 for name, count in scan_counts.items()}


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
async def test_record_detail_refreshes_with_r_and_toggles_raw_json_with_e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _persist_run(tmp_path, "selected", updated_at="2026-01-04T00:00:00+00:00", phase="planning")
    snapshot = build_snapshot(ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path))
    refresh_calls: list[ProjectResolution] = []

    def refreshed(project: ProjectResolution) -> ProjectSnapshot:
        refresh_calls.append(project)
        return snapshot

    monkeypatch.setattr(browser, "build_snapshot", refreshed)
    app = RunBrowserApp(snapshot)
    async with app.run_test() as pilot:
        await pilot.press("enter", "enter", "r")

        assert refresh_calls == [snapshot.project]
        assert not app.screen.query(".record-detail-raw-json")

        await pilot.press("e")

        assert app.screen.query_one(".record-detail-raw-json")

    assert ("e", "toggle_raw_json", "Toggle raw JSON") in RunBrowserApp.BINDINGS


@pytest.mark.asyncio
async def test_run_detail_opens_escaped_record_detail_and_expandable_raw_json(
    tmp_path: Path,
) -> None:
    _persist_run(tmp_path, "selected", updated_at="2026-01-04T00:00:00+00:00", phase="planning")
    _persist_history(tmp_path, "selected", "0001-planning.json", seq=1, has_error=True)
    _persist_history(tmp_path, "selected", "0002-planning.json", seq=2, has_result=False)
    record_path = tmp_path / "loop-supervisor" / "runs" / "selected" / "0001-planning.json"
    record = json.loads(record_path.read_text())
    record["result"]["objective"] = "[bold]literal result[/bold]"
    record["error"]["message"] = "[red]literal error[/red]"
    record_path.write_text(json.dumps(record))
    state_path = tmp_path / "loop-supervisor" / "runs" / "selected.json"
    state = json.loads(state_path.read_text())
    state["planner_result"] = {
        "status": "READY",
        "task_id": "current-task",
        "objective": "[bold]current result[/bold]",
        "rationale": "[italic]literal rationale[/italic]",
        "acceptance_criteria": ["[green]literal criterion[/green]"],
        "relevant_files": [],
        "design_questions": [],
        "decision_required": False,
        "decision_question": None,
        "decision_rationale": None,
    }
    state_path.write_text(json.dumps(state))

    loaded_state = load_state(tmp_path, "selected")
    assert loaded_state.planner_result is not None
    current = load_current_run(tmp_path, "selected")
    assert current.loadable is True, current.diagnostic
    assert current.result_detail is not None
    assert "[bold]current result[/bold]" in current.result_detail

    snapshot = build_snapshot(ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path))
    app = RunBrowserApp(snapshot)
    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")

        current_detail = cast(Any, app.screen.query_one(".record-detail").render()).plain
        assert "Planner result" in current_detail
        assert "Objective: [bold]current result[/bold]" in current_detail
        assert '"objective":' not in current_detail
        assert "Error: unavailable (none recorded)." in current_detail
        assert "Raw JSON: collapsed (press e to expand)" in current_detail

        await pilot.press("e")

        current_raw_json = cast(Any, app.screen.query_one(".record-detail-raw-json").render()).plain
        assert '"task_id": "current-task"' in current_raw_json
        assert '"objective": "[bold]current result[/bold]"' in current_raw_json

        await pilot.press("b", "down", "enter")

        detail = cast(Any, app.screen.query_one(".record-detail").render()).plain
        assert "Planner result" in detail
        assert "Objective: [bold]literal result[/bold]" in detail
        assert '"objective":' not in detail
        assert "Operational error" in detail
        assert "Message: [red]literal error[/red]" in detail
        assert '"message":' not in detail
        assert "Raw JSON: collapsed (press e to expand)" in detail

        await pilot.press("e")

        raw_json = cast(Any, app.screen.query_one(".record-detail-raw-json").render()).plain
        assert '"objective": "[bold]literal result[/bold]"' in raw_json
        assert '"message": "[red]literal error[/red]"' in raw_json

        await pilot.press("b")
        await pilot.press("down", "down", "enter")

        unavailable = cast(Any, app.screen.query_one(".record-detail").render()).plain
        assert "Result: unavailable (none recorded)." in unavailable
        assert "Error: unavailable (none recorded)." in unavailable


@pytest.mark.asyncio
async def test_expanded_raw_json_widget_including_marker_stays_within_render_limits(
    tmp_path: Path,
) -> None:
    _persist_run(tmp_path, "selected", updated_at="2026-01-04T00:00:00+00:00", phase="planning")
    _persist_history(tmp_path, "selected", "0001-planning.json", seq=1)
    record_path = tmp_path / "loop-supervisor" / "runs" / "selected" / "0001-planning.json"
    record = json.loads(record_path.read_text())
    record["result"]["objective"] = "x" * (300 * 1024)
    record_path.write_text(json.dumps(record))

    snapshot = build_snapshot(ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path))
    app = RunBrowserApp(snapshot)
    async with app.run_test() as pilot:
        await pilot.press("enter", "down", "enter", "e")

        raw_json = cast(Any, app.screen.query_one(".record-detail-raw-json").render()).plain
        assert len(raw_json.encode("utf-8")) <= 256 * 1024
        assert len(raw_json.splitlines()) <= 10_000
        assert "Raw JSON output truncated: rendered-output limit reached." in raw_json


@pytest.mark.asyncio
async def test_oversized_result_preserves_error_and_raw_json_affordance(tmp_path: Path) -> None:
    _persist_run(tmp_path, "selected", updated_at="2026-01-04T00:00:00+00:00", phase="planning")
    _persist_history(tmp_path, "selected", "0001-planning.json", seq=1, has_error=True)
    record_path = tmp_path / "loop-supervisor" / "runs" / "selected" / "0001-planning.json"
    record = json.loads(record_path.read_text())
    record["result"]["objective"] = "x" * (300 * 1024)
    record_path.write_text(json.dumps(record))

    snapshot = build_snapshot(ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path))
    app = RunBrowserApp(snapshot)
    async with app.run_test() as pilot:
        await pilot.press("enter", "down", "enter")

        detail = cast(Any, app.screen.query_one(".record-detail").render()).plain
        assert "Operational error" in detail
        assert "Message: recorded failure" in detail
        assert "Raw JSON: collapsed (press e to expand); output truncated" in detail


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
async def test_run_detail_renders_verification_attempt_as_openable_without_reading_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "selected"
    _persist_run(tmp_path, run_id, updated_at="2026-01-04T00:00:00+00:00", phase="auditing")
    result = _verification_result(tmp_path, run_id, command="[bold]pytest[/bold]")
    _persist_verification_result(tmp_path, run_id, result)
    log = tmp_path / "loop-supervisor" / "verification" / run_id / ("a" * 40) / "01.log"
    log.parent.mkdir(parents=True)
    log.write_text("unredacted log body must remain unopened")
    calls = 0

    def count_read_log(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(verification, "read_log", count_read_log)
    snapshot = build_snapshot(ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path))
    app = RunBrowserApp(snapshot)
    async with app.run_test() as pilot:
        await pilot.press("enter")

        rendered = cast(Any, app.screen.query_one(".run-detail-verification").render()).plain
        assert "Verification:" in rendered
        assert "Attempt 1" in rendered
        assert f"Commit: {'a' * 40}" in rendered
        assert "Command: [bold]pytest[/bold]" in rendered
        assert "OK: True; Return code: 0; Timed out: False; Duration: 0.1" in rendered
        assert "Summary: [green]literal summary[/green]" in rendered
        assert "Log: available (openable; not opened)" in rendered
        assert "unredacted log body must remain unopened" not in rendered

    assert calls == 0


@pytest.mark.asyncio
async def test_run_detail_renders_verification_diagnostics_and_no_attempts(
    tmp_path: Path,
) -> None:
    run_id = "selected"
    _persist_run(tmp_path, run_id, updated_at="2026-01-04T00:00:00+00:00", phase="auditing")
    result = _verification_result(tmp_path, run_id)
    state_path = _persist_verification_result(tmp_path, run_id, result)

    snapshot = build_snapshot(ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path))
    app = RunBrowserApp(snapshot)
    async with app.run_test() as pilot:
        await pilot.press("enter")

        diagnostic = cast(Any, app.screen.query_one(".run-detail-verification").render()).plain
        assert "Attempt 1" in diagnostic
        assert "Log: unavailable (not openable)" in diagnostic
        assert "Verification diagnostics:" in diagnostic
        assert "01.log: authorized log is unavailable" in diagnostic

    state = json.loads(state_path.read_text())
    state["verification_result"] = {"ok": True, "commands": []}
    state_path.write_text(json.dumps(state))
    empty_snapshot = build_snapshot(
        ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path)
    )
    empty_app = RunBrowserApp(empty_snapshot)
    async with empty_app.run_test() as pilot:
        await pilot.press("enter")

        empty = cast(Any, empty_app.screen.query_one(".run-detail-verification").render()).plain
        assert empty == "Verification: no verification evidence."


def test_verification_rendering_stays_within_output_limits() -> None:
    attempt = VerificationAttempt(
        commit="a" * 40,
        ordinal=1,
        command="command\n" * 100_001,
        ok=True,
        returncode=0,
        timed_out=False,
        duration=0.1,
        summary="summary",
        log=None,
    )

    rendered = RunBrowserApp._render_verification(
        VerificationDiscovery(
            attempts=(attempt,),
            diagnostics=(VerificationDiagnostic("01.log", "authorized log is unavailable"),),
        )
    )

    assert len(rendered.encode("utf-8")) <= 256 * 1024
    assert len(rendered.splitlines()) <= 10_000
    assert "Verification output truncated: rendered-output limit reached." in rendered


def test_verification_rendering_bounds_non_newline_line_separators() -> None:
    attempt = VerificationAttempt(
        commit="a" * 40,
        ordinal=1,
        command="command\r" * 100_001,
        ok=True,
        returncode=0,
        timed_out=False,
        duration=0.1,
        summary="summary",
        log=None,
    )

    rendered = RunBrowserApp._render_verification(
        VerificationDiscovery(attempts=(attempt,), diagnostics=())
    )

    assert len(rendered.encode("utf-8")) <= 256 * 1024
    assert len(rendered.splitlines()) <= 10_000
    assert "Verification output truncated: rendered-output limit reached." in rendered


async def _open_first_verification_log(pilot: Any) -> None:
    """Move keyboard focus from record detail choices to verification log choices."""
    await pilot.press("tab", "enter")


@pytest.mark.asyncio
async def test_run_detail_explicitly_opens_available_verification_log_as_literal_sensitive_text(
    tmp_path: Path,
) -> None:
    run_id = "selected"
    _persist_run(tmp_path, run_id, updated_at="2026-01-04T00:00:00+00:00", phase="auditing")
    result = _verification_result(tmp_path, run_id)
    _persist_verification_result(tmp_path, run_id, result)
    log = tmp_path / "loop-supervisor" / "verification" / run_id / ("a" * 40) / "01.log"
    log.parent.mkdir(parents=True)
    log.write_text("[bold]unredacted literal verification output[/bold]")

    snapshot = build_snapshot(ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path))
    app = RunBrowserApp(snapshot)
    async with app.run_test() as pilot:
        await pilot.press("enter")
        await _open_first_verification_log(pilot)

        viewer = cast(Any, app.screen.query_one(".verification-log-viewer").render()).plain
        heading = cast(
            Any, app.screen.query_one("#verification-log-viewer").query_one(Static).render()
        ).plain
        assert heading == "Verification log — press b to return to the run detail."
        assert "WARNING: Verification output is unredacted and potentially sensitive." in viewer
        assert "[bold]unredacted literal verification output[/bold]" in viewer


@pytest.mark.asyncio
async def test_run_detail_shows_unavailable_diagnostic_when_opening_pruned_verification_log(
    tmp_path: Path,
) -> None:
    run_id = "selected"
    _persist_run(tmp_path, run_id, updated_at="2026-01-04T00:00:00+00:00", phase="auditing")
    result = _verification_result(tmp_path, run_id)
    _persist_verification_result(tmp_path, run_id, result)
    log = tmp_path / "loop-supervisor" / "verification" / run_id / ("a" * 40) / "01.log"
    log.parent.mkdir(parents=True)
    log.write_text("will be pruned")

    snapshot = build_snapshot(ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path))
    app = RunBrowserApp(snapshot)
    async with app.run_test() as pilot:
        await pilot.press("enter")
        log.unlink()
        await _open_first_verification_log(pilot)

        viewer = cast(Any, app.screen.query_one(".verification-log-viewer").render()).plain
        assert "WARNING: Verification output is unredacted and potentially sensitive." in viewer
        assert "Verification log: unavailable (log is unavailable)." in viewer
        assert "will be pruned" not in viewer


@pytest.mark.asyncio
async def test_run_detail_marks_truncated_verification_log_content(tmp_path: Path) -> None:
    run_id = "selected"
    _persist_run(tmp_path, run_id, updated_at="2026-01-04T00:00:00+00:00", phase="auditing")
    result = _verification_result(tmp_path, run_id)
    _persist_verification_result(tmp_path, run_id, result)
    log = tmp_path / "loop-supervisor" / "verification" / run_id / ("a" * 40) / "01.log"
    log.parent.mkdir(parents=True)
    log.write_text("x" * (verification.LOG_RENDER_BYTE_LIMIT + 1))

    snapshot = build_snapshot(ProjectResolution(integration_root=tmp_path, git_common_dir=tmp_path))
    app = RunBrowserApp(snapshot)
    async with app.run_test() as pilot:
        await pilot.press("enter")
        await _open_first_verification_log(pilot)

        viewer = cast(Any, app.screen.query_one(".verification-log-viewer").render()).plain
        assert "Verification log render truncated: rendered-output limit reached." in viewer
        assert "Verification log byte truncated" not in viewer


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
