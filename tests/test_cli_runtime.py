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
        step=False,
        max_steps=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _resume_args(tmp_path, run_id="run-1", **overrides):
    defaults = dict(
        project=str(tmp_path),
        run_id=run_id,
        recover_stale_lock=False,
        step=False,
        max_steps=None,
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


def test_cmd_run_passes_max_steps_through(tmp_path, monkeypatch):
    received = {}

    class FakeState:
        run_id = "run-123"
        phase = "building"

    def fake_run_new(*args, **kwargs):
        received.update(kwargs)
        return FakeState()

    monkeypatch.setattr(cli_mod, "run_new", fake_run_new)
    rc = cli_mod.cmd_run(_run_args(tmp_path, max_steps=3))

    assert received["max_steps"] == 3
    assert rc == 1  # non-terminal phase


def test_cmd_run_step_is_shorthand_for_max_steps_one(tmp_path, monkeypatch):
    received = {}

    class FakeState:
        run_id = "run-123"
        phase = "building"

    def fake_run_new(*args, **kwargs):
        received.update(kwargs)
        return FakeState()

    monkeypatch.setattr(cli_mod, "run_new", fake_run_new)
    cli_mod.cmd_run(_run_args(tmp_path, step=True))

    assert received["max_steps"] == 1


def test_cmd_run_rejects_max_steps_below_one(tmp_path, monkeypatch, capsys):
    def fake_run_new(*args, **kwargs):
        raise AssertionError("run_new must not be called when --max-steps is invalid")

    monkeypatch.setattr(cli_mod, "run_new", fake_run_new)
    rc = cli_mod.cmd_run(_run_args(tmp_path, max_steps=0))

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")


def test_cmd_run_paused_message_printed_to_stdout_on_early_stop(tmp_path, monkeypatch, capsys):
    class FakeState:
        run_id = "run-123"
        phase = "building"

    monkeypatch.setattr(cli_mod, "run_new", lambda *a, **k: FakeState())
    rc = cli_mod.cmd_run(_run_args(tmp_path, max_steps=3))

    assert rc == 1
    captured = capsys.readouterr()
    assert "paused at phase building" in captured.out
    assert captured.err == ""


def test_cmd_run_no_paused_message_when_max_steps_not_set(tmp_path, monkeypatch, capsys):
    class FakeState:
        run_id = "run-123"
        phase = "building"

    monkeypatch.setattr(cli_mod, "run_new", lambda *a, **k: FakeState())
    rc = cli_mod.cmd_run(_run_args(tmp_path))

    assert rc == 1
    captured = capsys.readouterr()
    assert "paused at phase" not in captured.out


def test_cmd_resume_passes_max_steps_through(tmp_path, monkeypatch):
    received = {}

    class FakeState:
        phase = "building"

    def fake_run_resume(*args, **kwargs):
        received.update(kwargs)
        return FakeState()

    monkeypatch.setattr(cli_mod, "run_resume", fake_run_resume)
    rc = cli_mod.cmd_resume(_resume_args(tmp_path, max_steps=2))

    assert received["max_steps"] == 2
    assert rc == 1


def test_cmd_resume_step_is_shorthand_for_max_steps_one(tmp_path, monkeypatch):
    received = {}

    class FakeState:
        phase = "building"

    def fake_run_resume(*args, **kwargs):
        received.update(kwargs)
        return FakeState()

    monkeypatch.setattr(cli_mod, "run_resume", fake_run_resume)
    cli_mod.cmd_resume(_resume_args(tmp_path, step=True))

    assert received["max_steps"] == 1


def test_cmd_resume_rejects_max_steps_below_one(tmp_path, monkeypatch, capsys):
    def fake_run_resume(*args, **kwargs):
        raise AssertionError("run_resume must not be called when --max-steps is invalid")

    monkeypatch.setattr(cli_mod, "run_resume", fake_run_resume)
    rc = cli_mod.cmd_resume(_resume_args(tmp_path, max_steps=-1))

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")


def test_cmd_resume_invalid_max_steps_with_explicit_run_id_does_not_list_runs(
    tmp_path, monkeypatch
):
    """With an explicit run_id, an invalid --max-steps must be rejected
    without ever consulting list_run_ids (that path is only for the
    run_id-omitted listing mode)."""

    def fake_list_run_ids(*args, **kwargs):
        raise AssertionError("list_run_ids must not be called when run_id is given")

    monkeypatch.setattr(cli_mod, "list_run_ids", fake_list_run_ids)
    rc = cli_mod.cmd_resume(_resume_args(tmp_path, max_steps=0))

    assert rc == 1


def test_cmd_resume_invalid_max_steps_with_no_run_id_is_rejected_not_listed(
    tmp_path, monkeypatch, capsys
):
    """With run_id omitted, an invalid --max-steps must be rejected up
    front, not silently ignored in favor of falling through to the
    saved-run listing behavior."""

    def fake_list_run_ids(*args, **kwargs):
        raise AssertionError("list_run_ids must not be called when --max-steps is invalid")

    monkeypatch.setattr(cli_mod, "list_run_ids", fake_list_run_ids)
    rc = cli_mod.cmd_resume(_resume_args(tmp_path, run_id=None, max_steps=0))

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert "available runs" not in captured.out


def test_cmd_resume_paused_message_printed_to_stdout_on_early_stop(tmp_path, monkeypatch, capsys):
    class FakeState:
        phase = "building"

    monkeypatch.setattr(cli_mod, "run_resume", lambda *a, **k: FakeState())
    rc = cli_mod.cmd_resume(_resume_args(tmp_path, max_steps=2))

    assert rc == 1
    captured = capsys.readouterr()
    assert "paused at phase building" in captured.out
    assert captured.err == ""


def test_build_parser_rejects_step_and_max_steps_together_for_run():
    parser = cli_mod.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--step", "--max-steps", "3"])


def test_build_parser_rejects_step_and_max_steps_together_for_resume():
    parser = cli_mod.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["resume", "run-1", "--step", "--max-steps", "3"])


def test_build_parser_accepts_max_steps_on_resume():
    parser = cli_mod.build_parser()
    args = parser.parse_args(["resume", "run-1", "--max-steps", "5"])
    assert args.max_steps == 5


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
