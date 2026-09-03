"""Tests that cmd_run/cmd_resume/cmd_tui normalize expected application
failures into a single sanitized stderr line and exit code 1, and never
let a traceback escape for failures the runtime is documented to raise."""

import argparse
import json
import os
import signal
from typing import Any, cast

import pytest

import loop_supervisor.cli as cli_mod
from loop_supervisor.git import GitError
from loop_supervisor.locking import LockError
from loop_supervisor.runtime import RuntimeError_
from loop_supervisor.supervisor import AdvanceOutcome, FailurePersistenceError, LoopError


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
        max_builder_guidance_attempts=3,
        role_timeout=1800.0,
        recover_stale_lock=False,
        config=None,
        provision_command=None,
        provision_timeout=None,
        no_provision=False,
        verify_command=None,
        verify_timeout=None,
        no_verify=False,
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


def _stub_load_run(monkeypatch, *, phase="building", accepted_task_count=0):
    """cmd_resume() now checks load_run() before run_resume() to reject a
    terminal run early (backlog item 29). Tests exercising cmd_resume()'s
    behavior *after* that check (paused-message wording, max_steps
    passthrough, error normalization from run_resume() itself, signal
    handling) stub load_run() to report a non-terminal phase by default,
    so load_run()'s own real-repository requirement does not interfere
    with what each test is actually exercising."""
    existing = argparse.Namespace(phase=phase, accepted_task_count=accepted_task_count)
    monkeypatch.setattr(cli_mod, "load_run", lambda *a, **k: existing)


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

    _stub_load_run(monkeypatch)
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
        {"max_builder_guidance_attempts": -1},
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


def _capture_options(monkeypatch):
    captured_options = {}

    class FakeState:
        run_id = "run-123"
        phase = "done"

    def fake_run_new(project_root, options, **kwargs):
        captured_options["options"] = options
        return FakeState()

    monkeypatch.setattr(cli_mod, "run_new", fake_run_new)
    return captured_options


def test_cmd_run_defaults_provision_and_verify_to_off(tmp_path, monkeypatch):
    captured = _capture_options(monkeypatch)
    rc = cli_mod.cmd_run(_run_args(tmp_path))
    assert rc == 0
    options = captured["options"]
    assert options.provision_commands == ()
    assert options.verify_commands == ()


def test_cmd_run_reads_provision_and_verify_from_config_file(tmp_path, monkeypatch):
    (tmp_path / "loop-supervisor.toml").write_text(
        '[provision]\ncommands = ["python3 -m venv .venv"]\n[verify]\ncommands = ["pytest -q"]\n'
    )
    captured = _capture_options(monkeypatch)
    rc = cli_mod.cmd_run(_run_args(tmp_path))
    assert rc == 0
    options = captured["options"]
    assert options.provision_commands == ("python3 -m venv .venv",)
    assert options.verify_commands == ("pytest -q",)


def test_cmd_run_provision_command_flag_replaces_config_file_list(tmp_path, monkeypatch):
    (tmp_path / "loop-supervisor.toml").write_text('[provision]\ncommands = ["from-config-file"]\n')
    captured = _capture_options(monkeypatch)
    rc = cli_mod.cmd_run(_run_args(tmp_path, provision_command=["from-cli"]))
    assert rc == 0
    assert captured["options"].provision_commands == ("from-cli",)


def test_cmd_run_verify_command_flag_replaces_config_file_list(tmp_path, monkeypatch):
    (tmp_path / "loop-supervisor.toml").write_text('[verify]\ncommands = ["from-config-file"]\n')
    captured = _capture_options(monkeypatch)
    rc = cli_mod.cmd_run(_run_args(tmp_path, verify_command=["from-cli"]))
    assert rc == 0
    assert captured["options"].verify_commands == ("from-cli",)


def test_cmd_run_no_provision_forces_off_even_with_config_file(tmp_path, monkeypatch):
    (tmp_path / "loop-supervisor.toml").write_text('[provision]\ncommands = ["from-config-file"]\n')
    captured = _capture_options(monkeypatch)
    rc = cli_mod.cmd_run(_run_args(tmp_path, no_provision=True))
    assert rc == 0
    assert captured["options"].provision_commands == ()


def test_cmd_run_no_verify_forces_off_even_with_config_file(tmp_path, monkeypatch):
    (tmp_path / "loop-supervisor.toml").write_text('[verify]\ncommands = ["from-config-file"]\n')
    captured = _capture_options(monkeypatch)
    rc = cli_mod.cmd_run(_run_args(tmp_path, no_verify=True))
    assert rc == 0
    assert captured["options"].verify_commands == ()


def test_cmd_run_config_flag_overrides_default_config_path(tmp_path, monkeypatch):
    alt_dir = tmp_path / "alt"
    alt_dir.mkdir()
    alt_config = alt_dir / "custom.toml"
    alt_config.write_text('[verify]\ncommands = ["from-alt-config"]\n')
    (tmp_path / "loop-supervisor.toml").write_text('[verify]\ncommands = ["from-default-config"]\n')
    captured = _capture_options(monkeypatch)
    rc = cli_mod.cmd_run(_run_args(tmp_path, config=str(alt_config)))
    assert rc == 0
    assert captured["options"].verify_commands == ("from-alt-config",)


def test_cmd_run_provision_timeout_flag_overrides_config_file(tmp_path, monkeypatch):
    (tmp_path / "loop-supervisor.toml").write_text('[provision]\ncommands = ["x"]\ntimeout = 111\n')
    captured = _capture_options(monkeypatch)
    rc = cli_mod.cmd_run(_run_args(tmp_path, provision_timeout=222.0))
    assert rc == 0
    assert captured["options"].provision_timeout == 222.0


def test_cmd_run_reports_invalid_config_file_before_starting(tmp_path, monkeypatch, capsys):
    (tmp_path / "loop-supervisor.toml").write_text("not valid [ toml")

    def fake_run_new(*args, **kwargs):
        raise AssertionError("run_new must not be called for an invalid config file")

    monkeypatch.setattr(cli_mod, "run_new", fake_run_new)
    rc = cli_mod.cmd_run(_run_args(tmp_path))

    assert rc == 1
    captured = capsys.readouterr()
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

    _stub_load_run(monkeypatch)
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

    _stub_load_run(monkeypatch)
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

    _stub_load_run(monkeypatch)
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


@pytest.mark.parametrize("phase", ["done", "failed"])
def test_cmd_resume_rejects_terminal_run_before_starting(tmp_path, monkeypatch, capsys, phase):
    """Resuming a run whose persisted phase is already terminal must be
    rejected up front -- before run_resume() (and therefore before the
    lock is acquired or an OpenCode process is spawned) -- with an
    actionable, non-zero-exit message, not a silent no-op reported
    identically to a resume that did real work."""

    def fake_run_resume(*args, **kwargs):
        raise AssertionError("run_resume must not be called for an already-terminal run")

    _stub_load_run(monkeypatch, phase=phase, accepted_task_count=6)
    monkeypatch.setattr(cli_mod, "run_resume", fake_run_resume)
    rc = cli_mod.cmd_resume(_resume_args(tmp_path, run_id="2dba05654b5e"))

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "2dba05654b5e" in captured.err
    assert phase in captured.err
    assert "6" in captured.err
    assert "Traceback" not in captured.err


def test_cmd_resume_terminal_run_rejection_reports_load_run_failure(tmp_path, monkeypatch, capsys):
    """If load_run() itself fails (e.g. the run_id does not exist), that
    failure must be reported the same sanitized way as every other
    expected cmd_resume failure, not let a traceback escape."""

    def fake_load_run(*args, **kwargs):
        raise RuntimeError_("cannot load run 'nonexistent': no saved state")

    def fake_run_resume(*args, **kwargs):
        raise AssertionError("run_resume must not be called when load_run() fails")

    monkeypatch.setattr(cli_mod, "load_run", fake_load_run)
    monkeypatch.setattr(cli_mod, "run_resume", fake_run_resume)
    rc = cli_mod.cmd_resume(_resume_args(tmp_path, run_id="nonexistent"))

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert "Traceback" not in captured.err


def test_cmd_resume_paused_message_printed_to_stdout_on_early_stop(tmp_path, monkeypatch, capsys):
    class FakeState:
        phase = "building"

    _stub_load_run(monkeypatch)
    monkeypatch.setattr(cli_mod, "run_resume", lambda *a, **k: FakeState())
    rc = cli_mod.cmd_resume(_resume_args(tmp_path, max_steps=2))

    assert rc == 1
    captured = capsys.readouterr()
    assert "paused at phase building" in captured.out
    assert captured.err == ""


def test_cmd_resume_paused_message_printed_even_without_max_steps(tmp_path, monkeypatch, capsys):
    class FakeState:
        phase = "building"

    _stub_load_run(monkeypatch)
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

    _stub_load_run(monkeypatch)
    monkeypatch.setattr(cli_mod, "run_resume", lambda *a, **k: FakeState())
    rc = cli_mod.cmd_resume(_resume_args(tmp_path, max_steps=2))

    assert rc == 1
    captured = capsys.readouterr()
    assert "paused" not in captured.out


def test_cmd_resume_no_paused_message_on_done(tmp_path, monkeypatch, capsys):
    class FakeState:
        phase = "done"

    _stub_load_run(monkeypatch)
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


def test_build_parser_verbose_defaults_to_zero_for_run():
    parser = cli_mod.build_parser()
    args = parser.parse_args(["run"])
    assert args.verbose == 0


def test_build_parser_counts_repeated_v_for_run():
    parser = cli_mod.build_parser()
    assert parser.parse_args(["run", "-v"]).verbose == 1
    assert parser.parse_args(["run", "-vv"]).verbose == 2
    assert parser.parse_args(["run", "-v", "-v"]).verbose == 2
    assert parser.parse_args(["run", "--verbose", "--verbose"]).verbose == 2


def test_build_parser_counts_repeated_v_for_resume():
    parser = cli_mod.build_parser()
    assert parser.parse_args(["resume", "run-1", "-vv"]).verbose == 2


def test_build_verbosity_hooks_returns_all_none_at_zero():
    observer, consumers, on_advance = cli_mod._build_verbosity_hooks(0)
    assert observer is None
    assert consumers == []
    assert on_advance is None


def test_build_verbosity_hooks_at_one_returns_reporter_only():
    observer, consumers, on_advance = cli_mod._build_verbosity_hooks(1)
    assert isinstance(observer, cli_mod.VerboseReporter)
    assert consumers == []
    assert on_advance is not None


def test_build_verbosity_hooks_at_two_adds_stats_consumer_and_composite_observer():
    observer, consumers, on_advance = cli_mod._build_verbosity_hooks(2)
    assert isinstance(observer, cli_mod.CompositeInvocationObserver)
    assert len(consumers) == 1
    assert isinstance(consumers[0], cli_mod.StatsConsumer)
    assert on_advance is not None


def test_build_on_advance_always_records_history_even_at_verbosity_zero(tmp_path, monkeypatch):
    """ADR 0034: PhaseHistoryRecorder is composed into on_advance
    unconditionally, one level below -v -- verbosity 0 still gets a
    non-None on_advance, unlike _build_verbosity_hooks alone."""
    observer, consumers, on_advance = cli_mod._build_on_advance(0)
    assert observer is None
    assert consumers == []
    assert on_advance is not None

    recorded: list[Any] = []
    monkeypatch.setattr(cli_mod, "PhaseHistoryRecorder", lambda: _FakeRecorder(recorded))
    observer, consumers, on_advance = cli_mod._build_on_advance(0)
    outcome = cast(AdvanceOutcome, object())
    on_advance(outcome)
    assert recorded == [outcome]


class _FakeRecorder:
    def __init__(self, sink: list[Any]) -> None:
        self._sink = sink

    def on_advance(self, outcome: AdvanceOutcome) -> None:
        self._sink.append(outcome)


def test_build_on_advance_chains_verbosity_reporter_on_top(monkeypatch, capsys):
    calls: list[Any] = []
    monkeypatch.setattr(cli_mod, "PhaseHistoryRecorder", lambda: _FakeRecorder(calls))
    observer, consumers, on_advance = cli_mod._build_on_advance(1)
    assert isinstance(observer, cli_mod.VerboseReporter)

    class _FakeState:
        planner_result = None
        original_task_id = None

    class _FakeOutcome:
        state = _FakeState()
        phase_before = "planning"
        phase_after = "building"

    outcome = cast(AdvanceOutcome, _FakeOutcome())
    on_advance(outcome)
    # The (fake) recorder ran, and the real VerboseReporter.on_advance
    # ran too (printed a phase-transition line to stderr) -- confirming
    # both halves of the composed callback fired, not just one.
    assert calls == [outcome]
    assert "planning -> building" in capsys.readouterr().err


def test_cmd_run_passes_verbosity_hooks_to_run_new(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    class FakeState:
        run_id = "run-1"
        phase = "done"

    def fake_run_new(*args, **kwargs):
        captured.update(kwargs)
        return FakeState()

    monkeypatch.setattr(cli_mod, "run_new", fake_run_new)
    rc = cli_mod.cmd_run(_run_args(tmp_path, verbose=1))

    assert rc == 0
    assert isinstance(captured["server_observer"], cli_mod.VerboseReporter)
    assert captured["session_event_consumers"] == []
    assert captured["on_advance"] is not None


def test_cmd_run_omits_verbosity_hooks_when_not_requested(tmp_path, monkeypatch):
    """At verbosity 0, the `-v`/`-vv` observer/consumers are omitted, but
    `on_advance` is still populated: ADR 0034's phase-history recorder is
    always on, one level below `-v` (see `_build_on_advance`)."""
    captured: dict[str, object] = {}

    class FakeState:
        run_id = "run-1"
        phase = "done"

    def fake_run_new(*args, **kwargs):
        captured.update(kwargs)
        return FakeState()

    monkeypatch.setattr(cli_mod, "run_new", fake_run_new)
    rc = cli_mod.cmd_run(_run_args(tmp_path, verbose=0))

    assert rc == 0
    assert captured["server_observer"] is None
    assert captured["session_event_consumers"] == []
    assert captured["on_advance"] is not None


def test_cmd_resume_passes_verbosity_hooks_to_run_resume(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    class FakeState:
        run_id = "run-1"
        phase = "done"

    _stub_load_run(monkeypatch)

    def fake_run_resume(*args, **kwargs):
        captured.update(kwargs)
        return FakeState()

    monkeypatch.setattr(cli_mod, "run_resume", fake_run_resume)
    rc = cli_mod.cmd_resume(_resume_args(tmp_path, verbose=2))

    assert rc == 0
    assert isinstance(captured["server_observer"], cli_mod.CompositeInvocationObserver)
    consumers = captured["session_event_consumers"]
    assert isinstance(consumers, list)
    assert len(consumers) == 1
    assert captured["on_advance"] is not None


def test_cmd_tui_is_a_no_op_stub(tmp_path, capsys):
    """The interactive TUI has been retired pending a rebuild; `tui`
    prints a notice and exits 0 rather than launching anything."""
    rc = cli_mod.cmd_tui(_resume_args(tmp_path, run_id=None))

    assert rc == 0
    captured = capsys.readouterr()
    assert "run" in captured.err
    assert "resume" in captured.err
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

    _stub_load_run(monkeypatch)
    monkeypatch.setattr(cli_mod, "run_resume", fake_run_resume)
    with pytest.raises(KeyboardInterrupt):
        cli_mod.cmd_resume(_resume_args(tmp_path))

    assert seen["disposition"] not in (signal.SIG_DFL, signal.default_int_handler)
    assert signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, signal.default_int_handler), (
        "the bridge must restore the prior disposition once cmd_resume returns/raises"
    )


def test_cmd_tui_is_not_wrapped_by_the_sigterm_bridge(tmp_path):
    """cmd_tui must not install the SIGTERM bridge. It is currently a
    no-op stub, but the eventual TUI replacement will need its own
    signal-handling UX decision (see the docstring on
    `_bridge_sigterm_to_keyboard_interrupt`), so this pins the
    "never wrapped" invariant regardless of what `cmd_tui` does
    internally."""
    before = signal.getsignal(signal.SIGTERM)

    rc = cli_mod.cmd_tui(_resume_args(tmp_path, run_id=None))
    after = signal.getsignal(signal.SIGTERM)

    assert rc == 0
    assert after is before, "cmd_tui must not touch SIGTERM disposition at all"


def test_build_parser_wires_config_validate():
    parser = cli_mod.build_parser()
    args = parser.parse_args(["config", "validate", "--project", "/tmp/x"])
    assert args.func is cli_mod.cmd_config_validate
    assert args.project == "/tmp/x"
    assert args.opencode_executable == "opencode"
    assert args.json is False


def test_build_parser_config_validate_requires_a_config_subcommand():
    parser = cli_mod.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["config"])


def test_cmd_config_validate_exit_code_matches_report_ok(tmp_path, monkeypatch, capsys):
    args = argparse.Namespace(project=str(tmp_path), opencode_executable="opencode", json=False)

    monkeypatch.setattr(
        cli_mod,
        "validate_report",
        lambda project_root, *, opencode_executable: {
            "ok": False,
            "checks": {"dotenv_file": {"ok": False, "detail": "missing"}},
            "env": {},
        },
    )
    rc = cli_mod.cmd_config_validate(args)
    assert rc == 1
    out = capsys.readouterr()
    assert "dotenv_file" in out.out
    assert "one or more checks failed" in out.err


def test_cmd_config_validate_prints_json_when_requested(tmp_path, monkeypatch, capsys):
    args = argparse.Namespace(project=str(tmp_path), opencode_executable="opencode", json=True)
    report = {"ok": True, "checks": {"dotenv_file": {"ok": True, "detail": "present"}}, "env": {}}
    monkeypatch.setattr(
        cli_mod, "validate_report", lambda project_root, *, opencode_executable: report
    )
    rc = cli_mod.cmd_config_validate(args)
    assert rc == 0
    out = capsys.readouterr()
    parsed = json.loads(out.out)
    assert parsed == report


def test_cmd_config_validate_ok_true_exits_zero(tmp_path, monkeypatch, capsys):
    args = argparse.Namespace(project=str(tmp_path), opencode_executable="opencode", json=False)
    monkeypatch.setattr(
        cli_mod,
        "validate_report",
        lambda project_root, *, opencode_executable: {"ok": True, "checks": {}, "env": {}},
    )
    rc = cli_mod.cmd_config_validate(args)
    assert rc == 0
    out = capsys.readouterr()
    assert "all checks passed" in out.out


def test_build_parser_wires_runs_prune():
    parser = cli_mod.build_parser()
    args = parser.parse_args(["runs", "prune", "--project", "/tmp/x", "--keep-last", "3"])
    assert args.func is cli_mod.cmd_runs_prune
    assert args.project == "/tmp/x"
    assert args.keep_last == 3
    assert args.older_than is None
    assert args.run is None
    assert args.include_verification is False
    assert args.yes is False


def test_build_parser_runs_requires_a_subcommand():
    parser = cli_mod.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["runs"])


def _init_repo_for_prune(tmp_path):
    import subprocess

    def run(args, cwd):
        result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    project = tmp_path / "project"
    project.mkdir()
    run(["init", "-b", "main"], project)
    run(["config", "user.email", "test@example.com"], project)
    run(["config", "user.name", "Test"], project)
    (project / "README.md").write_text("hello\n")
    run(["add", "-A"], project)
    run(["commit", "-m", "initial"], project)
    return project


def _write_prune_fixture_run(project, run_id):
    from loop_supervisor.git import GitRepo
    from loop_supervisor.state import STATE_SCHEMA_VERSION, RunState, save_state
    from loop_supervisor.supervisor import _default_run_options

    repo = GitRepo(project)
    state = RunState(
        schema_version=STATE_SCHEMA_VERSION,
        run_id=run_id,
        git_common_dir=str(repo.common_dir()),
        integration_path=str(repo.root),
        integration_branch=repo.current_branch(),
        integration_commit_at_start=repo.head_commit(),
        options=_default_run_options(),
        integration_expected_head=repo.head_commit(),
        integration_status_snapshot=repo.status_snapshot(),
        phase="done",
    )
    save_state(repo.common_dir(), state)
    return repo


def test_cmd_runs_prune_dry_run_by_default(tmp_path, capsys):
    project = _init_repo_for_prune(tmp_path)
    _write_prune_fixture_run(project, "run0000000001")

    args = argparse.Namespace(
        project=str(project),
        run=["run0000000001"],
        keep_last=None,
        older_than=None,
        include_verification=False,
        yes=False,
    )
    rc = cli_mod.cmd_runs_prune(args)
    assert rc == 0
    out = capsys.readouterr()
    assert "would remove: run0000000001" in out.out
    assert "re-run with --yes" in out.out

    from loop_supervisor.git import GitRepo
    from loop_supervisor.state import list_runs

    assert "run0000000001" in list_runs(GitRepo(project).common_dir())


def test_cmd_runs_prune_with_yes_actually_deletes(tmp_path, capsys):
    project = _init_repo_for_prune(tmp_path)
    _write_prune_fixture_run(project, "run0000000001")

    args = argparse.Namespace(
        project=str(project),
        run=["run0000000001"],
        keep_last=None,
        older_than=None,
        include_verification=False,
        yes=True,
    )
    rc = cli_mod.cmd_runs_prune(args)
    assert rc == 0
    out = capsys.readouterr()
    assert "removed 1 run(s)" in out.out

    from loop_supervisor.git import GitRepo
    from loop_supervisor.state import list_runs

    assert "run0000000001" not in list_runs(GitRepo(project).common_dir())


def test_cmd_runs_prune_rejects_run_with_keep_last(tmp_path, capsys):
    project = _init_repo_for_prune(tmp_path)
    args = argparse.Namespace(
        project=str(project),
        run=["run0000000001"],
        keep_last=1,
        older_than=None,
        include_verification=False,
        yes=False,
    )
    rc = cli_mod.cmd_runs_prune(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "mutually exclusive" in err


def test_cmd_runs_prune_no_candidates(tmp_path, capsys):
    project = _init_repo_for_prune(tmp_path)
    args = argparse.Namespace(
        project=str(project),
        run=None,
        keep_last=100,
        older_than=None,
        include_verification=False,
        yes=False,
    )
    rc = cli_mod.cmd_runs_prune(args)
    assert rc == 0
    assert "no runs selected" in capsys.readouterr().out


def test_cmd_runs_prune_refuses_while_locked(tmp_path, capsys):
    from loop_supervisor.git import GitRepo
    from loop_supervisor.locking import SupervisorLock

    project = _init_repo_for_prune(tmp_path)
    _write_prune_fixture_run(project, "run0000000001")
    repo = GitRepo(project)
    lock = SupervisorLock(repo.common_dir(), operation="run", integration_path=str(repo.root))
    lock.acquire()
    try:
        args = argparse.Namespace(
            project=str(project),
            run=["run0000000001"],
            keep_last=None,
            older_than=None,
            include_verification=False,
            yes=True,
        )
        rc = cli_mod.cmd_runs_prune(args)
        assert rc == 1
        assert "refusing to prune" in capsys.readouterr().err
    finally:
        lock.release()


def test_cmd_runs_prune_marks_unloadable_run_and_still_deletes_it(tmp_path, capsys):
    """A --run id whose state file fails load_state() (e.g. orphaned by
    a STATE_SCHEMA_VERSION bump, per ADR 0024's no-migration policy)
    must be visibly annotated as unloadable and still be prunable."""
    import json

    project = _init_repo_for_prune(tmp_path)
    runs_dir = project / ".git" / "loop-supervisor" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    state_path = runs_dir / "run0000000001.json"
    state_path.write_text(json.dumps({"schema_version": 999, "run_id": "run0000000001"}))

    args = argparse.Namespace(
        project=str(project),
        run=["run0000000001"],
        keep_last=None,
        older_than=None,
        include_verification=False,
        yes=False,
    )
    rc = cli_mod.cmd_runs_prune(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "would remove: run0000000001 (phase=?, updated_at=?) [unloadable]" in out

    args.yes = True
    rc = cli_mod.cmd_runs_prune(args)
    assert rc == 0
    assert "removed 1 run(s)" in capsys.readouterr().out
    assert not state_path.exists()
