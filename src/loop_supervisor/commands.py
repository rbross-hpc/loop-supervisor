"""Running project-configured commands (worktree provisioning,
supervisor-run verification) as plain subprocesses.

This is deliberately separate from `git.py`'s `_run`, which is
hardcoded to the `git` executable and has no timeout: a project-
configured command is untrusted-until-run in a way `git` invocations
are not (arbitrary argv, no bound on how long it might take), so it
needs its own helper with a mandatory timeout and no `shell=True`
anywhere in the call path.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    """The outcome of running one configured command line to completion
    (or until it timed out)."""

    command: str
    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    duration: float
    timed_out: bool

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.returncode == 0


def run_command(
    command: str,
    *,
    cwd: Path,
    timeout: float,
    env: dict[str, str],
) -> CommandResult:
    """Run one configured command line to completion.

    `command` is parsed with `shlex.split` into argv and executed
    directly -- never via a shell -- so shell metacharacters in a
    configured command line are inert rather than interpreted.
    """
    argv = tuple(shlex.split(command))
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - started
        return CommandResult(
            command=command,
            argv=argv,
            returncode=None,
            stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
            duration=duration,
            timed_out=True,
        )
    except OSError as exc:
        duration = time.monotonic() - started
        return CommandResult(
            command=command,
            argv=argv,
            returncode=None,
            stdout="",
            stderr=str(exc),
            duration=duration,
            timed_out=False,
        )
    duration = time.monotonic() - started
    return CommandResult(
        command=command,
        argv=argv,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        duration=duration,
        timed_out=False,
    )


def run_commands(
    commands: tuple[str, ...],
    *,
    cwd: Path,
    timeout: float,
    env: dict[str, str],
) -> list[CommandResult]:
    """Run each command in order, stopping at the first failure.

    A timeout or a nonzero exit both count as failure and stop the
    sequence; a caller inspects the returned list's last entry's `ok`
    to know whether every command succeeded or the sequence was cut
    short.
    """
    results: list[CommandResult] = []
    for command in commands:
        result = run_command(command, cwd=cwd, timeout=timeout, env=env)
        results.append(result)
        if not result.ok:
            break
    return results
