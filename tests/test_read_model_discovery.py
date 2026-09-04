import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import loop_supervisor.read_model.discovery as discovery
from loop_supervisor.read_model import RunSummary, discover_runs
from loop_supervisor.state import (
    STATE_SCHEMA_VERSION,
    RunOptions,
    RunState,
    StateError,
    save_state,
)


def _make_options() -> RunOptions:
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
    state = RunState(
        schema_version=STATE_SCHEMA_VERSION,
        run_id=run_id,
        git_common_dir=str(git_common_dir),
        integration_path="/repo",
        integration_branch="main",
        integration_commit_at_start="abc123",
        options=_make_options(),
        integration_expected_head="abc123",
        integration_status_snapshot="",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at=updated_at,
    )
    save_state(git_common_dir, state)
    # save_state records its own current timestamp; set a stable ordering value.
    path = git_common_dir / "loop-supervisor" / "runs" / f"{run_id}.json"
    text = path.read_text()
    path.write_text(text.replace(state.updated_at, updated_at))


def test_discover_runs_returns_loadable_and_degraded_summaries_without_paths(tmp_path):
    older = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    newer = datetime.now(UTC).isoformat()
    _save_state(tmp_path, "older", updated_at=older)
    _save_state(tmp_path, "newer", updated_at=newer)
    runs = tmp_path / "loop-supervisor" / "runs"
    (runs / "broken.json").write_text("not JSON")

    summaries = discover_runs(tmp_path)

    assert all(isinstance(summary, RunSummary) for summary in summaries)
    assert [summary.run_id for summary in summaries] == ["newer", "older", "broken"]
    observed = [
        (summary.loadable, summary.phase, summary.created_at, summary.updated_at)
        for summary in summaries
    ]
    assert observed == [
        (True, "planning", "2026-01-01T00:00:00+00:00", newer),
        (True, "planning", "2026-01-01T00:00:00+00:00", older),
        (False, None, None, None),
    ]
    assert summaries[2].diagnostic is not None


def test_discover_runs_returns_no_summaries_when_the_runs_directory_is_absent(tmp_path):
    assert discover_runs(tmp_path) == []


def test_discover_runs_refuses_a_symlinked_runs_directory(tmp_path):
    target = tmp_path / "outside-runs"
    target.mkdir()
    supervisor = tmp_path / "loop-supervisor"
    supervisor.mkdir()
    (supervisor / "runs").symlink_to(target, target_is_directory=True)

    with pytest.raises(StateError, match="refusing symbolic link or unsafe path"):
        discover_runs(tmp_path)


def test_discover_runs_keeps_symlinked_and_non_regular_leaves_degraded(tmp_path):
    runs = tmp_path / "loop-supervisor" / "runs"
    runs.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (runs / "linked.json").symlink_to(outside)
    (runs / "directory.json").mkdir()

    summaries = discover_runs(tmp_path)

    assert [summary.run_id for summary in summaries] == ["directory", "linked"]
    assert all(not summary.loadable for summary in summaries)
    assert all(summary.phase is None and summary.updated_at is None for summary in summaries)
    assert "symbolic link" in (summaries[1].diagnostic or "")
    assert "regular file" in (summaries[0].diagnostic or "")


def test_discover_runs_skips_invalid_filename_stems(tmp_path):
    _save_state(tmp_path, "valid", updated_at="2026-01-02T00:00:00+00:00")
    runs = tmp_path / "loop-supervisor" / "runs"
    (runs / ".json").write_text("{}")
    (runs / "-invalid.json").write_text("{}")

    summaries = discover_runs(tmp_path)

    assert [summary.run_id for summary in summaries] == ["valid"]


def test_discover_runs_keeps_a_candidate_that_disappears_during_load(tmp_path, monkeypatch):
    _save_state(tmp_path, "vanishing", updated_at="2026-01-02T00:00:00+00:00")
    original_load_state = discovery.load_state

    def remove_before_loading(git_common_dir: Path, run_id: str) -> RunState:
        os.unlink(git_common_dir / "loop-supervisor" / "runs" / f"{run_id}.json")
        return original_load_state(git_common_dir, run_id)

    monkeypatch.setattr(discovery, "load_state", remove_before_loading)

    summaries = discover_runs(tmp_path)

    assert len(summaries) == 1
    assert summaries[0].run_id == "vanishing"
    assert not summaries[0].loadable
    assert summaries[0].phase is None
    assert summaries[0].updated_at is None
    assert "unloadable" in (summaries[0].diagnostic or "")
