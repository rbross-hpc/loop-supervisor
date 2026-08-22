#!/usr/bin/env python3
"""Anchored process-group launcher for OpenCode."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import threading


def _write_event(fd: int, value: str) -> None:
    os.write(fd, f"{value}\n".encode())


def _read_command(fd: int) -> str | None:
    partial = b""
    while True:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            return ""
        chunk = os.read(fd, 256)
        if not chunk:
            return None
        partial += chunk
        if b"\n" in partial:
            raw, _ = partial.split(b"\n", 1)
            return raw.decode(errors="replace").strip()


def _forward_stdout(child_fd: int) -> None:
    output_fd = sys.stdout.buffer.fileno()
    while True:
        try:
            ready, _, _ = select.select([child_fd], [], [], 0.1)
        except (OSError, ValueError):
            return
        if not ready:
            continue
        try:
            chunk = os.read(child_fd, 4096)
        except OSError:
            return
        if not chunk:
            return
        try:
            os.write(output_fd, chunk)
        except OSError:
            return


def main() -> int:
    if len(sys.argv) < 4:
        return 1

    event_fd = int(sys.argv[1])
    command_fd = int(sys.argv[2])
    child_command = sys.argv[3:]
    pid = os.getpid()
    pgid = os.getpgid(pid)
    if pgid != pid:
        _write_event(event_fd, f"anchor-error:pgid {pgid} != pid {pid}")
        return 1

    signal.signal(signal.SIGTERM, lambda signum, frame: None)
    forced_identity = os.environ.get("FAKE_LAUNCHER_IDENTITY")
    if forced_identity:
        _write_event(event_fd, forced_identity)
    else:
        _write_event(event_fd, f"anchor-ready:{pid}:{pgid}")

    while True:
        command = _read_command(command_fd)
        if command is None:
            return 1
        if command == "start":
            break

    child_read_fd, child_write_fd = os.pipe()
    child: subprocess.Popen[bytes] | None = None
    try:
        child = subprocess.Popen(
            child_command,
            stdout=child_write_fd,
            stderr=child_write_fd,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        _write_event(event_fd, f"start-error:{exc}")
    finally:
        os.close(child_write_fd)
        if child is None:
            os.close(child_read_fd)

    if child is not None:
        pump = threading.Thread(target=_forward_stdout, args=(child_read_fd,), daemon=True)
        pump.start()
        _write_event(event_fd, f"child-ready:{child.pid}")
    else:
        pump = None

    child_exit_reported = False
    while True:
        if child is not None and child.poll() is not None and not child_exit_reported:
            child_exit_reported = True
            _write_event(event_fd, f"child-exit:{child.returncode}")
        command = _read_command(command_fd)
        if command is None:
            return 1
        if command == "":
            continue
        if command == "term":
            if os.environ.get("FAKE_LAUNCHER_TERM_ERROR_ONCE"):
                os.environ.pop("FAKE_LAUNCHER_TERM_ERROR_ONCE")
                _write_event(event_fd, "term-error:simulated EPERM")
                continue
            try:
                os.killpg(pgid, signal.SIGTERM)
            except OSError as exc:
                _write_event(event_fd, f"term-error:{exc}")
            else:
                _write_event(event_fd, "term-ok")
            continue
        if command == "kill":
            if os.environ.get("FAKE_LAUNCHER_KILL_ERROR_ONCE"):
                os.environ.pop("FAKE_LAUNCHER_KILL_ERROR_ONCE")
                _write_event(event_fd, "kill-error:simulated EPERM")
                continue
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError as exc:
                _write_event(event_fd, f"kill-error:{exc}")
                continue
            _write_event(event_fd, "kill-unexpected-return")
        _write_event(event_fd, f"command-error:{command}")


if __name__ == "__main__":
    raise SystemExit(main())
