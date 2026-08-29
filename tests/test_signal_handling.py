"""End-to-end signal-delivery tests against the real `loop-supervisor`
CLI entry point, run as a genuine OS subprocess.

Unlike the synthetic-KeyboardInterrupt tests in test_runtime.py and
test_opencode.py (which raise from a monkeypatched object in-process),
these tests spawn `python -m loop_supervisor.cli run` for real, against
the fake OpenCode fixture, and deliver a real SIGINT/SIGTERM to the real
process. This is the only way to exercise the process's actual signal
disposition: SIGINT's default disposition already raises
KeyboardInterrupt, but nothing about that guarantees SIGTERM behaves the
same way, and in fact -- absent a handler -- it does not (see backlog
item 22 / ADR 0015).

The supervisor is driven into a task in flight (planner has started, the
lock is held, the fake server is up) by blocking the planner's first
`POST /session` call via FAKE_OPENCODE_SESSION_BLOCK_SECONDS, so a signal
sent shortly after the lock file appears is guaranteed to land while the
run is genuinely active rather than racing startup or shutdown.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

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
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class _SupervisorSubprocess:
    """Spawns the real CLI against the fake OpenCode fixture and tracks
    the fake server's own PID (read from the lock-adjacent marker file
    the fixture writes) so orphan-survival can be checked directly by
    PID liveness, the same technique test_opencode.py uses for
    descendant processes."""

    def __init__(self, tmp_path: Path) -> None:
        self.repo_dir = tmp_path / "repo"
        _init_repo(self.repo_dir)
        self.lock_path = self.repo_dir / ".git" / "loop-supervisor" / "supervisor.lock"
        self.server_pid_file = tmp_path / "fake_server.pid"
        self.proc: subprocess.Popen[str] | None = None

    def start(self) -> None:
        env = dict(os.environ)
        env["FAKE_OPENCODE_SESSION_BLOCK_SECONDS"] = "300"
        env["FAKE_OPENCODE_SELF_PID_FILE"] = str(self.server_pid_file)
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "loop_supervisor.cli",
                "run",
                "--project",
                str(self.repo_dir),
                "--opencode-executable",
                str(FIXTURE),
                "--startup-timeout",
                "10",
                "--role-timeout",
                "120",
                "--max-tasks",
                "1",
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def wait_until_active(self, timeout: float = 10.0) -> None:
        assert _wait_for(lambda: self.lock_path.exists(), timeout=timeout), (
            "supervisor lock never appeared; run did not reach an active state"
        )
        # Give the planner's blocked /session POST time to actually be
        # in flight, not just the lock file freshly written.
        time.sleep(0.5)
        assert self.proc is not None and self.proc.poll() is None, (
            "supervisor exited before the run became active"
        )

    def server_pid(self, timeout: float = 5.0) -> int:
        assert _wait_for(lambda: self.server_pid_file.exists(), timeout=timeout), (
            "fake OpenCode server never reported its own pid"
        )
        return int(self.server_pid_file.read_text().strip())

    def send_signal(self, sig: signal.Signals) -> None:
        assert self.proc is not None
        os.kill(self.proc.pid, sig)

    def wait(self, timeout: float = 15.0) -> int:
        assert self.proc is not None
        return self.proc.wait(timeout=timeout)

    def cleanup(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        # test_second_sigterm_during_cleanup_kills_hard deliberately
        # exercises a hard kill that skips cleanup, which orphans the
        # fake OpenCode server by design (that is the behavior under
        # test); reap it here so the test suite itself does not leak
        # processes across runs.
        if self.server_pid_file.exists():
            try:
                pid = int(self.server_pid_file.read_text().strip())
            except (ValueError, OSError):
                return
            if _pid_is_alive(pid):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)


@pytest.fixture
def supervisor(tmp_path: Path) -> Iterator[_SupervisorSubprocess]:
    sup = _SupervisorSubprocess(tmp_path)
    try:
        yield sup
    finally:
        sup.cleanup()


def test_sigint_releases_lock_and_stops_fake_server(supervisor):
    """SIGINT's default disposition already raises KeyboardInterrupt,
    which drives the existing RunSession.__exit__/close() cleanup path.
    This is the parity/regression check: it must keep working exactly as
    it does today once SIGTERM is also wired up."""
    supervisor.start()
    supervisor.wait_until_active()
    server_pid = supervisor.server_pid()
    assert _pid_is_alive(server_pid)

    supervisor.send_signal(signal.SIGINT)
    supervisor.wait()

    assert not supervisor.lock_path.exists(), "lock leaked after SIGINT"
    assert _wait_for(lambda: not _pid_is_alive(server_pid)), (
        "fake OpenCode server was not stopped after SIGINT"
    )


def test_sigterm_releases_lock_and_stops_fake_server(supervisor):
    """The behavior this backlog item (22a) exists to fix: a bare
    `kill <pid>` must release the lock and terminate the OpenCode
    process group, not just SIGINT. Confirmed to fail against the
    pre-fix code (returncode -15, lock retained, server orphaned) before
    the SIGTERM handler was added -- see the commit history for this
    file."""
    supervisor.start()
    supervisor.wait_until_active()
    server_pid = supervisor.server_pid()
    assert _pid_is_alive(server_pid)

    supervisor.send_signal(signal.SIGTERM)
    supervisor.wait()

    assert not supervisor.lock_path.exists(), "lock leaked after SIGTERM"
    assert _wait_for(lambda: not _pid_is_alive(server_pid)), (
        "fake OpenCode server was orphaned after SIGTERM"
    )


def test_second_sigterm_during_cleanup_kills_hard(supervisor):
    """The SIGTERM handler is one-shot: it restores default disposition
    immediately on first delivery, so a second SIGTERM arriving while
    cleanup is still in flight kills the process at default disposition
    instead of raising a second KeyboardInterrupt into the cleanup retry
    loop. This mirrors runtime.py's existing double-Ctrl-C semantics and
    is exercised here by delivering both signals back to back."""
    supervisor.start()
    supervisor.wait_until_active()

    supervisor.send_signal(signal.SIGTERM)
    # A short gap is required, not a race: standard (non-realtime) Unix
    # signals are not queued, so two SIGTERMs delivered in the same
    # instant coalesce into a single pending signal at the kernel level
    # regardless of any application-level handler. The handler restores
    # SIG_DFL synchronously before raising, so once the first delivery
    # has actually reached the interpreter this second send always hits
    # default disposition -- independent of how long cleanup itself
    # takes.
    time.sleep(0.2)
    supervisor.send_signal(signal.SIGTERM)
    returncode = supervisor.wait()

    assert returncode == -signal.SIGTERM, (
        f"expected the second SIGTERM to kill at default disposition, got {returncode}"
    )
