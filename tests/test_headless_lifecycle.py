"""Process-level acceptance coverage for the headless lifecycle.

Signal-driven graceful shutdown is covered separately in
``test_signal_handling.py``.  These tests cover the complementary item-13
scenarios: an uncatchable supervisor crash followed by explicit stale-lock
recovery, and retryable ``RunSession`` cleanup backed by a real spawned process.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import loop_supervisor.opencode as opencode_module
from loop_supervisor.locking import _lock_path
from loop_supervisor.runtime import RuntimeError_, SessionState, new_run_session
from loop_supervisor.state import RunOptions

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "fake_opencode.py"


def _run_git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    _run_git(["init", "-b", "main"], path)
    _run_git(["config", "user.email", "test@example.com"], path)
    _run_git(["config", "user.name", "Test"], path)
    (path / "README.md").write_text("hello\n")
    _run_git(["add", "-A"], path)
    _run_git(["commit", "-m", "initial"], path)


def _pid_is_alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    except OSError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    # A zombie cannot mutate the repository and is awaiting reaping by its
    # current parent, so it is dead for lifecycle-ownership purposes.
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _wait_for_pid(path: Path, timeout: float = 10.0) -> int:
    assert _wait_for(path.exists, timeout), f"process did not write pid file {path}"
    return int(path.read_text().strip())


def _options() -> RunOptions:
    return RunOptions(
        max_accepted_tasks=1,
        max_revisions_per_task=1,
        max_replans_per_task=1,
        max_architect_retries=1,
        malformed_output_retries=0,
        role_timeout=30.0,
        worktree_root=None,
        require_decision_approval=False,
        opencode_executable=str(FIXTURE),
        opencode_startup_timeout=10.0,
        provision_commands=(),
        provision_timeout=30.0,
        verify_commands=(),
        verify_timeout=30.0,
    )


def _cli_command(repo: Path, *, recover: bool = False) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "loop_supervisor.cli",
        "run",
        "--project",
        str(repo),
        "--opencode-executable",
        str(FIXTURE),
        "--startup-timeout",
        "10",
        "--role-timeout",
        "30",
        "--max-tasks",
        "1",
        "--max-steps",
        "1",
    ]
    if recover:
        command.append("--recover-stale-lock")
    return command


@pytest.fixture
def spawned_pids() -> Iterator[list[int]]:
    pids: list[int] = []
    yield pids
    for pid in pids:
        if _pid_is_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def test_hard_crash_leaves_group_and_requires_explicit_stale_lock_recovery(
    tmp_path: Path, spawned_pids: list[int]
) -> None:
    """SIGKILL cannot unwind cleanup: the group and lock survive, the lock
    rejects an ordinary successor, and explicit recovery succeeds only after
    the operator has terminated the surviving process group."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    lock_path = repo / ".git" / "loop-supervisor" / "supervisor.lock"
    server_pid_file = tmp_path / "server.pid"
    descendant_pid_file = tmp_path / "descendant.pid"
    env = dict(os.environ)
    env.update(
        {
            "FAKE_OPENCODE_SESSION_BLOCK_SECONDS": "300",
            "FAKE_OPENCODE_SELF_PID_FILE": str(server_pid_file),
            "FAKE_OPENCODE_DESCENDANT_PID_FILE": str(descendant_pid_file),
            "FAKE_OPENCODE_DESCENDANT_IGNORE_SIGTERM": "1",
        }
    )
    supervisor = subprocess.Popen(
        _cli_command(repo),
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_for(lock_path.exists), "supervisor lock never appeared"
        server_pid = _wait_for_pid(server_pid_file)
        descendant_pid = _wait_for_pid(descendant_pid_file)
        spawned_pids.extend([server_pid, descendant_pid])
        process_group = os.getpgid(server_pid)
        assert os.getpgid(descendant_pid) == process_group

        supervisor.kill()
        assert supervisor.wait(timeout=10) == -signal.SIGKILL

        assert _pid_is_alive(server_pid), "server unexpectedly died with its supervisor"
        assert _pid_is_alive(descendant_pid), "server descendant unexpectedly died"
        stale_record = json.loads(lock_path.read_text())
        assert stale_record["pid"] == supervisor.pid
        assert not _pid_is_alive(supervisor.pid)

        rejected = subprocess.run(
            _cli_command(repo),
            cwd=str(REPO_ROOT),
            env={**os.environ, "FAKE_OPENCODE_RESPONSE": json.dumps({"status": "COMPLETE"})},
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert rejected.returncode == 1
        assert "stale lock from dead process" in rejected.stderr.lower()
        assert json.loads(lock_path.read_text()) == stale_record

        os.killpg(process_group, signal.SIGKILL)
        assert _wait_for(lambda: not _pid_is_alive(server_pid))
        assert _wait_for(lambda: not _pid_is_alive(descendant_pid))

        recovered = subprocess.run(
            _cli_command(repo, recover=True),
            cwd=str(REPO_ROOT),
            env={**os.environ, "FAKE_OPENCODE_RESPONSE": json.dumps({"status": "COMPLETE"})},
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        assert "final phase: done" in recovered.stdout
        assert not lock_path.exists()
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=10)


def test_repeated_cleanup_retries_real_process_before_releasing_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spawned_pids: list[int]
) -> None:
    """An unconfirmed bounded attempt retains real process ownership and
    the repository lock; a later close retries the same server and releases
    both only after the launcher confirms process-group shutdown."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    server_pid_file = tmp_path / "server.pid"
    term_block_file = tmp_path / "block-term"
    term_block_file.touch()
    monkeypatch.setenv("FAKE_OPENCODE_SELF_PID_FILE", str(server_pid_file))
    monkeypatch.setenv("FAKE_LAUNCHER_TERM_BLOCK_FILE", str(term_block_file))
    launcher = tmp_path / "launcher.py"
    shutil.copyfile(REPO_ROOT / "src" / "loop_supervisor" / "_launcher.py", launcher)
    monkeypatch.setattr(opencode_module, "_LAUNCHER_SCRIPT", str(launcher))

    session = new_run_session(repo, _options())
    session.__enter__()
    try:
        session.start_server()
        server_pid = _wait_for_pid(server_pid_file)
        spawned_pids.append(server_pid)
        server = session._server
        assert server is not None
        owner = server._owner
        assert owner is not None

        session.stop_server()

        assert session.state is SessionState.STARTED
        assert session._server is server
        assert server._owner is owner
        assert _pid_is_alive(server_pid)
        assert _lock_path(repo / ".git").exists()

        term_block_file.unlink()
        session.close()

        assert session.state is SessionState.CLOSED
        assert server._owner is None
        assert _wait_for(lambda: not _pid_is_alive(server_pid))
        assert not _lock_path(repo / ".git").exists()
    finally:
        if session.state is not SessionState.CLOSED:
            try:
                session.close()
            except RuntimeError_:
                pass
