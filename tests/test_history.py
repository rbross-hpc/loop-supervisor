"""Tests for src/loop_supervisor/history.py (ADR 0034 phase history capture)."""

import json
import subprocess
from pathlib import Path

import pytest

from loop_supervisor.git import GitError, GitRepo
from loop_supervisor.history import (
    PhaseHistoryRecorder,
    PruneError,
    history_dir,
    prune_runs,
    select_prune_candidates,
)
from loop_supervisor.locking import SupervisorLock
from loop_supervisor.state import RunOptions, list_runs
from loop_supervisor.supervisor import Supervisor, _default_run_options


def _run(args, cwd):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _init_repo(path: Path) -> GitRepo:
    path.mkdir(parents=True)
    _run(["init", "-b", "main"], path)
    _run(["config", "user.email", "test@example.com"], path)
    _run(["config", "user.name", "Test"], path)
    (path / "README.md").write_text("hello\n")
    _run(["add", "-A"], path)
    _run(["commit", "-m", "initial"], path)
    return GitRepo(path)


class ScriptedRunner:
    def __init__(self, responses):
        self.responses = {k: list(v) for k, v in responses.items()}
        self._commit_counter = 0

    def run_agent(self, *, agent, directory, prompt, json_schema=None, timeout=1800.0):
        queue = self.responses.get(agent)
        if not queue:
            raise AssertionError(f"no more scripted responses for agent {agent!r}")
        raw = queue.pop(0)
        if agent == "loop-builder":
            data = json.loads(raw)
            if data.get("status") == "COMPLETE":
                self._commit_counter += 1
                fname = f"change-{self._commit_counter}.txt"
                (directory / fname).write_text("change\n")
                _run(["add", "-A"], directory)
                _run(["commit", "-m", f"builder change {self._commit_counter}"], directory)
                data["commit"] = _run(["rev-parse", "HEAD"], directory).strip()
                raw = json.dumps(data)
        return raw


class ScriptedInput:
    def __init__(self, answers=()):
        self.answers = list(answers)

    def request(self, *, kind, message, context):
        if not self.answers:
            return None
        return self.answers.pop(0)


def _planner_ready(task_id="task-1", objective="Do a thing"):
    return json.dumps(
        {
            "status": "READY",
            "task_id": task_id,
            "objective": objective,
            "rationale": "because",
            "acceptance_criteria": ["works"],
            "relevant_files": [],
            "design_questions": [],
            "decision_required": False,
            "decision_question": None,
            "decision_rationale": None,
        }
    )


def _builder(task_id="task-1", objective="Do a thing", status="COMPLETE", **extra):
    payload = {
        "task_id": task_id,
        "objective": objective,
        "status": status,
        "implementation_summary": "did it",
        "implementation_strategy": [],
        "tests_run": [],
        "test_results": [],
        "files_changed": [],
        "commit": None,
        "open_concerns": [],
    }
    payload.update(extra)
    return json.dumps(payload)


def _auditor(task_id="task-1", objective="Do a thing", disposition="ACCEPT", **extra):
    payload = {
        "task_id": task_id,
        "objective": objective,
        "disposition": disposition,
        "findings": [],
        "required_changes": ["fix it"] if disposition == "REVISE" else [],
        "design_observations": [],
        "decision_required": False,
        "decision_question": None,
        "decision_rationale": None,
    }
    payload.update(extra)
    return json.dumps(payload)


def _make_options(**overrides):
    base = _default_run_options()
    return RunOptions(
        max_accepted_tasks=overrides.get("max_accepted_tasks", base.max_accepted_tasks),
        max_revisions_per_task=overrides.get("max_revisions_per_task", base.max_revisions_per_task),
        max_replans_per_task=overrides.get("max_replans_per_task", base.max_replans_per_task),
        max_architect_retries=overrides.get("max_architect_retries", base.max_architect_retries),
        max_builder_guidance_attempts=overrides.get(
            "max_builder_guidance_attempts", base.max_builder_guidance_attempts
        ),
        malformed_output_retries=overrides.get(
            "malformed_output_retries", base.malformed_output_retries
        ),
        role_timeout=overrides.get("role_timeout", base.role_timeout),
        worktree_root=overrides.get("worktree_root", base.worktree_root),
        require_decision_approval=overrides.get(
            "require_decision_approval", base.require_decision_approval
        ),
        opencode_executable=overrides.get("opencode_executable", base.opencode_executable),
        opencode_startup_timeout=overrides.get(
            "opencode_startup_timeout", base.opencode_startup_timeout
        ),
        provision_commands=overrides.get("provision_commands", base.provision_commands),
        provision_timeout=overrides.get("provision_timeout", base.provision_timeout),
        verify_commands=overrides.get("verify_commands", base.verify_commands),
        verify_timeout=overrides.get("verify_timeout", base.verify_timeout),
    )


def _make_supervisor(tmp_path, runner, *, options=None):
    repo = _init_repo(tmp_path / "project")
    common_dir = repo.common_dir()
    options = options or _make_options()
    supervisor = Supervisor(
        repo=repo,
        runner=runner,
        git_common_dir=common_dir,
        input_provider=ScriptedInput(),
        options=options,
    )
    return supervisor, repo


def test_one_record_per_advance_in_sequence_order(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(), json.dumps({"status": "COMPLETE"})],
            "loop-builder": [
                _builder(status="COMPLETE"),
                _builder(status="COMPLETE"),
            ],
            "loop-auditor": [
                _auditor(disposition="REVISE", required_changes=["fix it"]),
                _auditor(disposition="ACCEPT"),
            ],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    recorder = PhaseHistoryRecorder()

    while state.phase not in ("done", "failed"):
        outcome = supervisor.advance(state)
        recorder.on_advance(outcome)
        state = outcome.state

    directory = history_dir(Path(state.git_common_dir), state.run_id)
    files = sorted(directory.glob("*.json"))
    assert len(files) >= 6

    seqs = [json.loads(f.read_text())["seq"] for f in files]
    assert seqs == sorted(seqs)
    assert seqs == list(range(1, len(files) + 1))

    phases = [f.name.split("-", 1)[1].removesuffix(".json") for f in files]
    assert phases[0] == "planning"
    assert "building" in phases
    assert "auditing" in phases


def test_revision_preserves_both_builder_results(tmp_path):
    """RunState overwrites builder_result on every REVISE cycle; the
    history directory must retain both, unlike the state snapshot."""
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready(), json.dumps({"status": "COMPLETE"})],
            "loop-builder": [
                _builder(status="COMPLETE", implementation_summary="first attempt"),
                _builder(status="COMPLETE", implementation_summary="second attempt"),
            ],
            "loop-auditor": [
                _auditor(disposition="REVISE", required_changes=["fix it"]),
                _auditor(disposition="ACCEPT"),
            ],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    recorder = PhaseHistoryRecorder()

    while state.phase not in ("done", "failed"):
        outcome = supervisor.advance(state)
        recorder.on_advance(outcome)
        state = outcome.state

    directory = history_dir(Path(state.git_common_dir), state.run_id)
    building_records = [
        json.loads(f.read_text())
        for f in sorted(directory.glob("*-building.json"))
    ]
    assert len(building_records) == 2
    summaries = {r["result"]["implementation_summary"] for r in building_records}
    assert summaries == {"first attempt", "second attempt"}


def test_verifying_record_has_no_log_body(tmp_path):
    """The captured record must be exactly `state.verification_result`
    (the compact per-command summary `_summarize_verification` already
    builds -- command/ok/returncode/duration/output_path/truncated
    summary), never the full stdout/stderr body that only ever lives in
    the log file at `output_path` (ADR 0027, ADR 0028)."""
    marker = "distinctive-verification-output-body-xyz"
    repeat_count = 200  # long enough to exceed the summary's truncation cap
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready()],
            "loop-builder": [_builder(status="COMPLETE")],
        }
    )
    options = _make_options(
        verify_commands=(f"python3 -c \"print('{marker}' * {repeat_count})\"",)
    )
    supervisor, repo = _make_supervisor(tmp_path, runner, options=options)
    state = supervisor.start_new_run()
    recorder = PhaseHistoryRecorder()

    for _ in range(4):  # planning -> creating_worktree -> building -> verifying -> auditing
        outcome = supervisor.advance(state)
        recorder.on_advance(outcome)
        state = outcome.state
        if state.phase == "auditing":
            break

    directory = history_dir(Path(state.git_common_dir), state.run_id)
    verifying_files = list(directory.glob("*-verifying.json"))
    assert len(verifying_files) == 1
    record = json.loads(verifying_files[0].read_text())
    assert record["result"] == state.verification_result

    full_output = marker * repeat_count
    log_path = Path(record["result"]["commands"][0]["output_path"])
    log_body = log_path.read_text()
    assert full_output in log_body  # the full output lives only on disk...

    raw_text = verifying_files[0].read_text()
    assert full_output not in raw_text  # ...never duplicated into the history record


def test_failed_phase_captures_sanitized_error(tmp_path, monkeypatch):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready()],
            "loop-builder": [_builder(status="COMPLETE")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    recorder = PhaseHistoryRecorder()

    outcome = supervisor.advance(state)  # planning -> creating_worktree
    recorder.on_advance(outcome)
    state = outcome.state

    secret = "sk-abcdefghijklmnopqrstuvwx"
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    def boom(*args, **kwargs):
        raise GitError(f"agent failed with key {secret}")

    monkeypatch.setattr(supervisor.repo, "verify_builder_commit", boom)

    outcome = supervisor.advance(state)  # creating_worktree -> building
    recorder.on_advance(outcome)
    state = outcome.state

    outcome = supervisor.advance(state)  # building -> operational_failure
    recorder.on_advance(outcome)
    state = outcome.state

    directory = history_dir(Path(state.git_common_dir), state.run_id)
    building_files = sorted(directory.glob("*-building.json"))
    assert len(building_files) == 1
    record = json.loads(building_files[0].read_text())
    assert record["status"] == "operational_failure"
    assert record["error"] is not None
    assert secret not in json.dumps(record)


def test_resume_continues_sequence_numbering(tmp_path):
    runner = ScriptedRunner(
        {
            "loop-planner": [_planner_ready()],
            "loop-builder": [_builder(status="COMPLETE")],
        }
    )
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()

    recorder1 = PhaseHistoryRecorder()
    outcome = supervisor.advance(state)
    recorder1.on_advance(outcome)
    state = outcome.state
    outcome = supervisor.advance(state)
    recorder1.on_advance(outcome)
    state = outcome.state

    directory = history_dir(Path(state.git_common_dir), state.run_id)
    assert len(list(directory.glob("*.json"))) == 2

    # Simulate a fresh process resuming: a brand new recorder instance
    # with no in-memory _next_seq cache must still continue from 3, not
    # restart at 1 and silently clobber 0001-planning.json.
    recorder2 = PhaseHistoryRecorder()
    outcome = supervisor.advance(state)
    recorder2.on_advance(outcome)

    files = sorted(directory.glob("*.json"))
    assert len(files) == 3
    seqs = sorted(json.loads(f.read_text())["seq"] for f in files)
    assert seqs == [1, 2, 3]


def test_recorder_write_failure_does_not_raise(tmp_path, monkeypatch, capsys):
    runner = ScriptedRunner({"loop-planner": [_planner_ready()]})
    supervisor, repo = _make_supervisor(tmp_path, runner)
    state = supervisor.start_new_run()
    recorder = PhaseHistoryRecorder()

    monkeypatch.setattr(
        "loop_supervisor.history.history_dir",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )

    outcome = supervisor.advance(state)
    recorder.on_advance(outcome)  # must not raise

    captured = capsys.readouterr()
    assert "could not record phase history" in captured.err


# -- runs prune ------------------------------------------------------------


def _write_run(tmp_path, repo, run_id, *, phase="done", updated_at=None):
    """Write a minimal standalone run-state file directly (bypassing the
    supervisor) so prune tests can set up several runs cheaply."""
    from loop_supervisor.state import STATE_SCHEMA_VERSION, RunState, save_state

    options = _make_options()
    state = RunState(
        schema_version=STATE_SCHEMA_VERSION,
        run_id=run_id,
        git_common_dir=str(repo.common_dir()),
        integration_path=str(repo.root),
        integration_branch=repo.current_branch(),
        integration_commit_at_start=repo.head_commit(),
        options=options,
        integration_expected_head=repo.head_commit(),
        integration_status_snapshot=repo.status_snapshot(),
        phase=phase,
    )
    save_state(repo.common_dir(), state)
    if updated_at is not None:
        path = repo.common_dir() / "loop-supervisor" / "runs" / f"{run_id}.json"
        data = json.loads(path.read_text())
        data["created_at"] = updated_at
        data["updated_at"] = updated_at
        path.write_text(json.dumps(data))
    return state


def test_select_prune_candidates_by_keep_last(tmp_path):
    repo = _init_repo(tmp_path / "project")
    _write_run(tmp_path, repo, "run0000000001", updated_at="2020-01-01T00:00:00+00:00")
    _write_run(tmp_path, repo, "run0000000002", updated_at="2021-01-01T00:00:00+00:00")
    _write_run(tmp_path, repo, "run0000000003", updated_at="2022-01-01T00:00:00+00:00")

    candidates = select_prune_candidates(repo.common_dir(), keep_last=1)
    ids = {c.run_id for c in candidates}
    assert ids == {"run0000000001", "run0000000002"}


def test_select_prune_candidates_by_older_than(tmp_path):
    repo = _init_repo(tmp_path / "project")
    from datetime import UTC, datetime, timedelta

    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    _write_run(tmp_path, repo, "run0000000001", updated_at=old)
    _write_run(tmp_path, repo, "run0000000002", updated_at=recent)

    candidates = select_prune_candidates(repo.common_dir(), older_than_days=7)
    ids = {c.run_id for c in candidates}
    assert ids == {"run0000000001"}


def test_select_prune_candidates_by_explicit_run_ids(tmp_path):
    repo = _init_repo(tmp_path / "project")
    _write_run(tmp_path, repo, "run0000000001")
    _write_run(tmp_path, repo, "run0000000002")

    candidates = select_prune_candidates(repo.common_dir(), run_ids=["run0000000002"])
    assert [c.run_id for c in candidates] == ["run0000000002"]


def test_prune_runs_removes_state_and_history_but_keeps_verification_by_default(tmp_path):
    repo = _init_repo(tmp_path / "project")
    state = _write_run(tmp_path, repo, "run0000000001")
    history_path = history_dir(repo.common_dir(), state.run_id)
    history_path.mkdir(parents=True)
    (history_path / "0001-planning.json").write_text("{}")
    verification_path = repo.common_dir() / "loop-supervisor" / "verification" / state.run_id
    verification_path.mkdir(parents=True)
    (verification_path / "somecommit").mkdir()

    candidates = select_prune_candidates(repo.common_dir(), run_ids=[state.run_id])
    removed = prune_runs(repo.common_dir(), candidates)

    assert removed == [state.run_id]
    assert state.run_id not in list_runs(repo.common_dir())
    assert not history_path.exists()
    assert verification_path.exists()  # kept: --include-verification not passed


def test_prune_runs_with_include_verification_removes_it_too(tmp_path):
    repo = _init_repo(tmp_path / "project")
    state = _write_run(tmp_path, repo, "run0000000001")
    verification_path = repo.common_dir() / "loop-supervisor" / "verification" / state.run_id
    verification_path.mkdir(parents=True)
    (verification_path / "somecommit").mkdir()

    candidates = select_prune_candidates(repo.common_dir(), run_ids=[state.run_id])
    prune_runs(repo.common_dir(), candidates, include_verification=True)

    assert not verification_path.exists()


def test_prune_runs_refuses_while_lock_is_present(tmp_path):
    repo = _init_repo(tmp_path / "project")
    state = _write_run(tmp_path, repo, "run0000000001")
    candidates = select_prune_candidates(repo.common_dir(), run_ids=[state.run_id])

    lock = SupervisorLock(repo.common_dir(), operation="run", integration_path=str(repo.root))
    lock.acquire()
    try:
        with pytest.raises(PruneError):
            prune_runs(repo.common_dir(), candidates)
    finally:
        lock.release()

    # Not removed: the refusal must happen before anything is deleted.
    assert state.run_id in list_runs(repo.common_dir())
