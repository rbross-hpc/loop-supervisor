"""Tests that cmd_run/cmd_resume/cmd_tui normalize expected application
failures into a single sanitized stderr line and exit code 1, and never
let a traceback escape for failures the runtime is documented to raise."""

import argparse
import os
import signal

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


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_tasks": -5},
        {"max_revisions": -1},
        {"max_replans": -1},
        {"max_architect_retries": -1},
        {"role_timeout": 0.0},
        {"role_timeout": -1.0},
        {"startup_timeout": 0.0},
        {"opencode_executable": ""},
    ],
)
def test_cmd_run_rejects_invalid_options_before_starting(tmp_path, monkeypatch, capsys, overrides):
    """Out-of-range CLI values (negative counts, non-positive timeouts, an
    empty executable) must be rejected before run_new() is ever called --
    the same values RunOptions.from_dict() would reject when loading a
    persisted run. Without this, argparse's type coercion alone would
    accept them, letting a run start, persist invalid options via
    to_dict(), and then become permanently unresumable (from_dict()
    rejects the very file the program itself wrote)."""

    def fake_run_new(*args, **kwargs):
        raise AssertionError("run_new must not be called for invalid options")

    monkeypatch.setattr(cli_mod, "run_new", fake_run_new)
    rc = cli_mod.cmd_run(_run_args(tmp_path, **overrides))

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "Traceback" not in captured.err


def test_cmd_run_accepts_options_from_dict_would_accept(tmp_path, monkeypatch):
    """Round-trip check: anything cmd_run() builds successfully must also
    be accepted by RunOptions.from_dict(), so the two validation paths
    (CLI construction, persisted-state loading) can never silently
    diverge on what counts as valid."""
    from loop_supervisor.state import RunOptions

    captured_options = {}

    class FakeState:
        run_id = "run-123"
        phase = "done"

    def fake_run_new(project_root, options, **kwargs):
        captured_options["options"] = options
        return FakeState()

    monkeypatch.setattr(cli_mod, "run_new", fake_run_new)
    rc = cli_mod.cmd_run(_run_args(tmp_path))

    assert rc == 0
    options = captured_options["options"]
    assert RunOptions.from_dict(options.to_dict()) == options


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


def test_cmd_run_paused_message_printed_even_without_max_steps(tmp_path, monkeypatch, capsys):
    """A genuine pause (e.g. awaiting_input from an unavailable input
    provider) must be announced the same way whether or not --max-steps
    caused the stop: the message follows the outcome, not the flag."""

    class FakeState:
        run_id = "run-123"
        phase = "building"

    monkeypatch.setattr(cli_mod, "run_new", lambda *a, **k: FakeState())
    rc = cli_mod.cmd_run(_run_args(tmp_path))

    assert rc == 1
    captured = capsys.readouterr()
    assert "paused at phase building" in captured.out


@pytest.mark.parametrize("phase", ["failed", "operational_failure"])
def test_cmd_run_no_paused_message_on_failure_phases(tmp_path, monkeypatch, capsys, phase):
    """failed and operational_failure are not pauses: the run did not stop
    awaiting further input or a step budget, it stopped because it failed.
    Labeling either as "paused" would be misleading, even though the exit
    code (1) is the same as a genuine pause."""

    class FakeState:
        run_id = "run-123"
        phase = "unset"

    FakeState.phase = phase

    monkeypatch.setattr(cli_mod, "run_new", lambda *a, **k: FakeState())
    rc = cli_mod.cmd_run(_run_args(tmp_path, max_steps=3))

    assert rc == 1
    captured = capsys.readouterr()
    assert "paused" not in captured.out


def test_cmd_run_no_paused_message_on_done(tmp_path, monkeypatch, capsys):
    class FakeState:
        run_id = "run-123"
        phase = "done"

    monkeypatch.setattr(cli_mod, "run_new", lambda *a, **k: FakeState())
    rc = cli_mod.cmd_run(_run_args(tmp_path, max_steps=3))

    assert rc == 0
    captured = capsys.readouterr()
    assert "paused" not in captured.out


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


def test_cmd_resume_paused_message_printed_even_without_max_steps(tmp_path, monkeypatch, capsys):
    class FakeState:
        phase = "building"

    monkeypatch.setattr(cli_mod, "run_resume", lambda *a, **k: FakeState())
    rc = cli_mod.cmd_resume(_resume_args(tmp_path))

    assert rc == 1
    captured = capsys.readouterr()
    assert "paused at phase building" in captured.out


@pytest.mark.parametrize("phase", ["failed", "operational_failure"])
def test_cmd_resume_no_paused_message_on_failure_phases(tmp_path, monkeypatch, capsys, phase):
    class FakeState:
        phase = "unset"

    FakeState.phase = phase

    monkeypatch.setattr(cli_mod, "run_resume", lambda *a, **k: FakeState())
    rc = cli_mod.cmd_resume(_resume_args(tmp_path, max_steps=2))

    assert rc == 1
    captured = capsys.readouterr()
    assert "paused" not in captured.out


def test_cmd_resume_no_paused_message_on_done(tmp_path, monkeypatch, capsys):
    class FakeState:
        phase = "done"

    monkeypatch.setattr(cli_mod, "run_resume", lambda *a, **k: FakeState())
    rc = cli_mod.cmd_resume(_resume_args(tmp_path, max_steps=2))

    assert rc == 0
    captured = capsys.readouterr()
    assert "paused" not in captured.out


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


# -- SIGTERM-to-KeyboardInterrupt bridge (backlog item 22a / ADR 0015) --
#
# These are in-process unit tests of the bridge's own hygiene (handler
# install/restore, raise behavior, one-shot semantics). The end-to-end
# proof that this actually drives real cleanup against a real subprocess
# is tests/test_signal_handling.py; that is what a test claiming "SIGTERM
# releases the lock" must be backed by, not this file's synthetic raises.


def test_bridge_installs_and_restores_previous_handler():
    previous = signal.getsignal(signal.SIGTERM)
    with cli_mod._bridge_sigterm_to_keyboard_interrupt():
        assert signal.getsignal(signal.SIGTERM) is not previous
    assert signal.getsignal(signal.SIGTERM) is previous


def test_bridge_restores_previous_handler_even_if_body_raises():
    previous = signal.getsignal(signal.SIGTERM)
    with pytest.raises(ValueError):
        with cli_mod._bridge_sigterm_to_keyboard_interrupt():
            raise ValueError("boom")
    assert signal.getsignal(signal.SIGTERM) is previous


def test_sigterm_inside_bridge_raises_keyboard_interrupt():
    with pytest.raises(KeyboardInterrupt):
        with cli_mod._bridge_sigterm_to_keyboard_interrupt():
            os.kill(os.getpid(), signal.SIGTERM)


def test_bridge_is_one_shot_second_sigterm_hits_default_disposition(capsys):
    """The first delivery must restore SIG_DFL before raising, so a
    caller that catches the resulting KeyboardInterrupt and re-enters
    ordinary code is not left with the handler still installed."""
    with pytest.raises(KeyboardInterrupt):
        with cli_mod._bridge_sigterm_to_keyboard_interrupt():
            os.kill(os.getpid(), signal.SIGTERM)
    assert signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, signal.default_int_handler)
    captured = capsys.readouterr()
    assert "received SIGTERM" in captured.err


def test_cmd_run_wraps_run_new_in_the_sigterm_bridge(tmp_path, monkeypatch):
    """cmd_run must wrap run_new in the bridge, not just call it plainly.

    Checked by observing the SIGTERM disposition from *inside* run_new,
    rather than by actually delivering a real SIGTERM to this test
    process: if the bridge were ever removed, a real self-signal here
    would kill the whole pytest process at default disposition instead
    of failing this one test cleanly (confirmed by direct reproduction
    while developing this test). test_signal_handling.py is where a real
    SIGTERM against a real subprocess belongs; this test only needs to
    know the handler was installed for the duration of the call.
    """
    seen = {}

    def fake_run_new(*args, **kwargs):
        seen["disposition"] = signal.getsignal(signal.SIGTERM)
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli_mod, "run_new", fake_run_new)
    with pytest.raises(KeyboardInterrupt):
        cli_mod.cmd_run(_run_args(tmp_path))

    assert seen["disposition"] not in (signal.SIG_DFL, signal.default_int_handler)
    assert signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, signal.default_int_handler), (
        "the bridge must restore the prior disposition once cmd_run returns/raises"
    )


def test_cmd_resume_wraps_run_resume_in_the_sigterm_bridge(tmp_path, monkeypatch):
    seen = {}

    def fake_run_resume(*args, **kwargs):
        seen["disposition"] = signal.getsignal(signal.SIGTERM)
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli_mod, "run_resume", fake_run_resume)
    with pytest.raises(KeyboardInterrupt):
        cli_mod.cmd_resume(_resume_args(tmp_path))

    assert seen["disposition"] not in (signal.SIG_DFL, signal.default_int_handler)
    assert signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, signal.default_int_handler), (
        "the bridge must restore the prior disposition once cmd_resume returns/raises"
    )


def test_cmd_tui_is_not_wrapped_by_the_sigterm_bridge(tmp_path, monkeypatch):
    """cmd_tui must not install the SIGTERM bridge: Textual's Linux
    driver already disables terminal-level SIGINT delivery in raw mode,
    and injecting an externally raised KeyboardInterrupt into Textual's
    running event loop is untested and could leave the terminal stuck in
    raw mode. Verified by observing the disposition *during* the
    LoopSupervisorApp construction call, not just before/after cmd_tui
    returns -- checking only before/after is insufficient because the
    bridge's own `finally` restores the prior disposition before
    propagating, which would mask an accidental wrap. Actually
    delivering a real SIGTERM with default disposition here would
    terminate this test process outright, which is the whole point:
    that must not be caught and converted."""
    import loop_supervisor.tui.app as app_mod

    before = signal.getsignal(signal.SIGTERM)
    seen = {}

    def fake_init(self, *args, **kwargs):
        seen["disposition"] = signal.getsignal(signal.SIGTERM)
        raise LockError("lock is held by another process")

    monkeypatch.setattr(app_mod.LoopSupervisorApp, "__init__", fake_init)

    rc = cli_mod.cmd_tui(_resume_args(tmp_path, run_id=None))
    after = signal.getsignal(signal.SIGTERM)

    assert rc == 1
    assert seen["disposition"] is before, "cmd_tui must not touch SIGTERM disposition at all"
    assert after is before
