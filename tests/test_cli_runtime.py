"""Tests that cmd_run/cmd_resume/cmd_tui normalize expected application
failures into a single sanitized stderr line and exit code 1, and never
let a traceback escape for failures the runtime is documented to raise."""

import argparse

import pytest

import loop_supervisor.cli as cli_mod
from loop_supervisor.git import GitError
from loop_supervisor.locking import LockError
from loop_supervisor.runtime import RuntimeError_
from loop_supervisor.supervisor import FailurePersistenceError, LoopError


def _run_args(tmp_path, **overrides):
    defaults = dict(
        project=str(tmp_path),
        opencode_executable="opencode",
        startup_timeout=30.0,
        require_decision_approval=False,
        worktree_root=None,
        max_tasks=20,
        max_revisions=5,
        max_replans=3,
        max_architect_retries=3,
        role_timeout=1800.0,
        recover_stale_lock=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _resume_args(tmp_path, run_id="run-1", **overrides):
    defaults = dict(
        project=str(tmp_path),
        run_id=run_id,
        recover_stale_lock=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError_("cannot open repository: boom"),
        LockError("lock is held"),
        GitError("git blew up"),
        LoopError("terminal loop failure"),
        FailurePersistenceError("could not persist failure record"),
    ],
)
def test_cmd_run_normalizes_expected_errors(tmp_path, monkeypatch, capsys, exc):
    def fake_run_new(*args, **kwargs):
        raise exc

    monkeypatch.setattr(cli_mod, "run_new", fake_run_new)
    rc = cli_mod.cmd_run(_run_args(tmp_path))

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError_("cannot open repository: boom"),
        LockError("lock is held"),
        GitError("git blew up"),
        LoopError("resume validation failed"),
        FailurePersistenceError("could not persist failure record"),
    ],
)
def test_cmd_resume_normalizes_expected_errors(tmp_path, monkeypatch, capsys, exc):
    def fake_run_resume(*args, **kwargs):
        raise exc

    monkeypatch.setattr(cli_mod, "run_resume", fake_run_resume)
    rc = cli_mod.cmd_resume(_resume_args(tmp_path))

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "Traceback" not in captured.err


def test_cmd_run_does_not_catch_keyboard_interrupt(tmp_path, monkeypatch):
    def fake_run_new(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli_mod, "run_new", fake_run_new)
    with pytest.raises(KeyboardInterrupt):
        cli_mod.cmd_run(_run_args(tmp_path))


def test_cmd_resume_does_not_catch_keyboard_interrupt(tmp_path, monkeypatch):
    def fake_run_resume(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli_mod, "run_resume", fake_run_resume)
    with pytest.raises(KeyboardInterrupt):
        cli_mod.cmd_resume(_resume_args(tmp_path))


def test_cmd_run_success_prints_run_id_and_phase(tmp_path, monkeypatch):
    class FakeState:
        run_id = "run-123"
        phase = "done"

    monkeypatch.setattr(cli_mod, "run_new", lambda *a, **k: FakeState())
    rc = cli_mod.cmd_run(_run_args(tmp_path))
    assert rc == 0


def test_cmd_tui_normalizes_prelaunch_errors(tmp_path, monkeypatch, capsys):
    import loop_supervisor.tui.app as app_mod

    def fake_init(self, *args, **kwargs):
        raise LockError("lock is held by another process")

    monkeypatch.setattr(app_mod.LoopSupervisorApp, "__init__", fake_init)

    rc = cli_mod.cmd_tui(_resume_args(tmp_path, run_id=None))

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert "Traceback" not in captured.err
