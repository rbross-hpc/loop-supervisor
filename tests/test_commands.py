"""Tests for `commands.py`'s subprocess execution of project-configured
commands. Every command is run via `shlex.split` argv, never `shell=True`
-- these tests confirm shell metacharacters in a configured command line
are inert rather than interpreted."""

import os

from loop_supervisor.commands import run_command, run_commands


def _env() -> dict[str, str]:
    return dict(os.environ)


def test_run_command_captures_stdout_and_returncode(tmp_path):
    result = run_command("echo hello", cwd=tmp_path, timeout=5, env=_env())
    assert result.ok is True
    assert result.returncode == 0
    assert "hello" in result.stdout
    assert result.timed_out is False


def test_run_command_captures_nonzero_exit(tmp_path):
    result = run_command("sh -c 'exit 3'", cwd=tmp_path, timeout=5, env=_env())
    assert result.ok is False
    assert result.returncode == 3


def test_run_command_never_interprets_shell_metacharacters(tmp_path):
    marker = tmp_path / "should-not-exist"
    # If this were run with shell=True, the semicolon would start a
    # second command that touches `marker`. Run as argv, "echo" just
    # receives the whole string (including the semicolon) as one
    # literal argument.
    result = run_command(f"echo hi; touch {marker}", cwd=tmp_path, timeout=5, env=_env())
    assert result.ok is True
    assert not marker.exists()


def test_run_command_times_out(tmp_path):
    result = run_command("sleep 5", cwd=tmp_path, timeout=0.1, env=_env())
    assert result.timed_out is True
    assert result.ok is False
    assert result.returncode is None


def test_run_command_reports_missing_executable_without_raising(tmp_path):
    result = run_command("definitely-not-a-real-command-xyz", cwd=tmp_path, timeout=5, env=_env())
    assert result.ok is False
    assert result.timed_out is False
    assert result.returncode is None
    assert result.stderr


def test_run_command_runs_in_the_given_cwd(tmp_path):
    (tmp_path / "marker.txt").write_text("present\n")
    result = run_command("cat marker.txt", cwd=tmp_path, timeout=5, env=_env())
    assert result.ok is True
    assert "present" in result.stdout


def test_run_commands_stops_at_first_failure(tmp_path):
    marker = tmp_path / "second-ran"
    results = run_commands(
        ("sh -c 'exit 1'", f"touch {marker}"),
        cwd=tmp_path,
        timeout=5,
        env=_env(),
    )
    assert len(results) == 1
    assert results[0].ok is False
    assert not marker.exists()


def test_run_commands_runs_all_on_success(tmp_path):
    results = run_commands(
        ("echo one", "echo two", "echo three"),
        cwd=tmp_path,
        timeout=5,
        env=_env(),
    )
    assert len(results) == 3
    assert all(r.ok for r in results)
    assert "two" in results[1].stdout


def test_run_commands_empty_list_returns_empty(tmp_path):
    results = run_commands((), cwd=tmp_path, timeout=5, env=_env())
    assert results == []


def test_run_commands_stop_on_failure_true_is_the_default(tmp_path):
    marker = tmp_path / "second-ran"
    results = run_commands(
        ("sh -c 'exit 1'", f"touch {marker}"),
        cwd=tmp_path,
        timeout=5,
        env=_env(),
    )
    assert len(results) == 1
    assert not marker.exists()


def test_run_commands_stop_on_failure_false_runs_every_command(tmp_path):
    marker = tmp_path / "second-ran"
    results = run_commands(
        ("sh -c 'exit 1'", f"touch {marker}"),
        cwd=tmp_path,
        timeout=5,
        env=_env(),
        stop_on_failure=False,
    )
    assert len(results) == 2
    assert results[0].ok is False
    assert results[1].ok is True
    assert marker.exists()


def test_run_commands_stop_on_failure_false_records_every_result_regardless_of_position(tmp_path):
    results = run_commands(
        ("echo one", "sh -c 'exit 1'", "echo three"),
        cwd=tmp_path,
        timeout=5,
        env=_env(),
        stop_on_failure=False,
    )
    assert [r.ok for r in results] == [True, False, True]
    assert "one" in results[0].stdout
    assert "three" in results[2].stdout
