"""Unit tests for tui/renderers.py's Rich renderables.

These render against real RunState/AdvanceOutcome-shaped inputs and
capture output via a Rich Console(record=True), rather than exercising
Textual widgets -- see test_tui_app.py for lifecycle-level coverage.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console

from loop_supervisor.state import STATE_SCHEMA_VERSION, RunOptions, RunState
from loop_supervisor.tui.renderers import render_durable_summary


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
    )
    defaults.update(overrides)
    return RunOptions(**defaults)


def _make_state(run_id: str = "run-1", **overrides) -> RunState:
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


def _render_text(renderable) -> str:
    console = Console(record=True, width=100)
    console.print(renderable)
    return console.export_text()


def test_render_durable_summary_omits_denied_permissions_row_by_default():
    state = _make_state()
    text = _render_text(render_durable_summary(state))
    assert "Denied permissions" not in text


def test_render_durable_summary_shows_denied_permission_count_and_keys():
    state = _make_state()
    text = _render_text(
        render_durable_summary(
            state,
            denied_permission_count=3,
            denied_permission_summary=["bash", "edit"],
        )
    )
    assert "Denied permissions" in text
    assert "3" in text
    assert "bash" in text
    assert "edit" in text


def test_render_durable_summary_omits_row_when_count_is_zero_even_with_summary():
    """A count of zero must not render the row, regardless of what the
    summary list contains -- the count, not the summary, gates
    visibility (mirrors RunSession.denied_permission_count's own
    "Zero ... if no request ever arrived" contract)."""
    state = _make_state()
    text = _render_text(
        render_durable_summary(
            state,
            denied_permission_count=0,
            denied_permission_summary=["bash"],
        )
    )
    assert "Denied permissions" not in text
