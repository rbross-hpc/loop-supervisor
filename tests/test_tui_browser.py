"""Textual coverage for the read-only run-browser landing screen."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from loop_supervisor.read_model import ProjectResolution, build_snapshot
from loop_supervisor.state import STATE_SCHEMA_VERSION, RunOptions, RunState, save_state
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

        await pilot.press("q")

    assert app.is_running is False
