"""Tests for src/loop_supervisor/sse.py"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from loop_supervisor.sse import SSEClient, SSEConnectionState, iter_sse_json

_FIXTURE = Path(__file__).parent / "fixtures" / "fake_opencode.py"


def _parse(lines: list[str], **kwargs) -> list[dict]:
    return list(iter_sse_json(lines, **kwargs))


def test_simple_event():
    lines = ['data: {"type": "ping"}', ""]
    result = _parse(lines)
    assert result == [{"type": "ping"}]


def test_multiline_data_valid_json():
    lines = ['data: {"type":', 'data: "hello"}', ""]
    result = _parse(lines)
    assert result == [{"type": "hello"}]


def test_multiline_data_invalid_json_calls_notice():
    lines = ["data: {", "data: broken", ""]
    notices: list[str] = []
    result = _parse(lines, on_notice=notices.append)
    assert result == []
    assert len(notices) == 1


def test_blank_line_dispatches():
    lines = [
        'data: {"a": 1}',
        "",
        'data: {"b": 2}',
        "",
    ]
    result = _parse(lines)
    assert result == [{"a": 1}, {"b": 2}]


def test_comment_lines_skipped():
    lines = [": heartbeat", 'data: {"x": 1}', ""]
    result = _parse(lines)
    assert result == [{"x": 1}]


def test_crlf_line_endings():
    lines = ['data: {"y": 2}\r', "\r"]
    result = _parse(lines)
    assert result == [{"y": 2}]


def test_non_object_json_skipped():
    lines = ["data: [1,2,3]", "", 'data: "string"', "", 'data: {"ok": true}', ""]
    result = _parse(lines)
    assert result == [{"ok": True}]


def test_malformed_json_calls_on_notice():
    notices: list[str] = []
    lines = ["data: {broken", ""]
    result = _parse(lines, on_notice=notices.append)
    assert result == []
    assert len(notices) == 1
    assert "malformed" in notices[0].lower()


def test_event_size_limit_drops_oversized():
    notices: list[str] = []
    big = "x" * 1000
    lines = [f'data: {{"{big}": 1}}', ""]
    result = _parse(lines, max_event_bytes=100, on_notice=notices.append)
    assert result == []
    assert any("exceeded" in n for n in notices)


def test_incomplete_final_record_discarded():
    lines = ['data: {"incomplete": true}']
    result = _parse(lines)
    assert result == []


def test_empty_data_after_blank():
    lines = ["data: ", ""]
    notices: list[str] = []
    result = _parse(lines, on_notice=notices.append)
    assert result == []


def test_id_field_not_required():
    lines = ["id: 1", 'data: {"t": "x"}', ""]
    result = _parse(lines)
    assert result == [{"t": "x"}]


def test_event_field_ignored():
    lines = ["event: custom", 'data: {"t": "y"}', ""]
    result = _parse(lines)
    assert result == [{"t": "y"}]


def test_oversized_record_partial_suffix_not_dispatched():
    notices: list[str] = []
    big = "x" * 500
    lines = [
        f'data: {{"{big}": 1}}',
        'data: "suffix"',
        "",
        'data: {"good": true}',
        "",
    ]
    result = _parse(lines, max_event_bytes=100, on_notice=notices.append)
    assert result == [{"good": True}]
    assert any("exceeded" in n for n in notices)


class _FakeServer:
    """Starts fake_opencode.py and returns its base_url."""

    def __init__(self, env_overrides: dict | None = None):
        self._env = dict(os.environ)
        if env_overrides:
            self._env.update(env_overrides)
        self._proc: subprocess.Popen | None = None
        self.base_url = ""

    def start(self):
        self._proc = subprocess.Popen(
            [sys.executable, str(_FIXTURE), "serve", "--hostname", "127.0.0.1", "--port", "0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=self._env,
        )
        assert self._proc.stdout is not None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            line = self._proc.stdout.readline()
            if "listening on" in line:
                self.base_url = line.split("listening on")[1].strip()
                return
        raise RuntimeError("fake server did not start")

    def stop(self):
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()


def test_sse_client_requests_correct_path():
    server = _FakeServer()
    server.start()
    try:
        received: list[dict] = []
        states = []
        client = SSEClient(
            server.base_url,
            on_event=received.append,
            on_state_change=lambda s, r: states.append(s),
        )
        client.start()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if SSEConnectionState.LIVE in states:
                break
            time.sleep(0.05)
        client.stop()
        assert SSEConnectionState.LIVE in states
    finally:
        server.stop()


def test_sse_stop_terminates_promptly_despite_long_read_timeout():
    server = _FakeServer()
    server.start()
    try:
        client = SSEClient(
            server.base_url,
            on_event=lambda e: None,
            read_timeout=60.0,
        )
        client.start()
        time.sleep(0.2)

        t_start = time.monotonic()
        client.stop()
        elapsed = time.monotonic() - t_start

        assert elapsed < 5.0, f"stop() took {elapsed:.2f}s; expected < 5s"
    finally:
        server.stop()


def test_sse_stop_thread_is_dead_after_stop():
    server = _FakeServer()
    server.start()
    try:
        client = SSEClient(server.base_url, on_event=lambda e: None)
        client.start()
        time.sleep(0.2)
        client.stop()

        assert client._thread is None or not client._thread.is_alive()
    finally:
        server.stop()


def test_sse_stop_before_start_is_safe():
    client = SSEClient("http://127.0.0.1:9", on_event=lambda e: None)
    client.stop()


def test_sse_stop_is_idempotent():
    server = _FakeServer()
    server.start()
    try:
        client = SSEClient(server.base_url, on_event=lambda e: None)
        client.start()
        time.sleep(0.1)
        client.stop()
        client.stop()
    finally:
        server.stop()


def test_sse_intentional_stop_does_not_emit_error_notice():
    server = _FakeServer()
    server.start()
    try:
        notices: list[str] = []
        client = SSEClient(
            server.base_url,
            on_event=lambda e: None,
            on_notice=notices.append,
        )
        client.start()
        time.sleep(0.2)
        client.stop()
        error_notices = [n for n in notices if "error" in n.lower() and "handler" not in n.lower()]
        assert error_notices == [], f"Got unexpected error notices: {error_notices}"
    finally:
        server.stop()


def test_sse_receives_server_connected_event():
    server = _FakeServer()
    server.start()
    try:
        received: list[dict] = []
        client = SSEClient(server.base_url, on_event=received.append)
        client.start()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not received:
            time.sleep(0.05)
        client.stop()
        assert any(e.get("payload", {}).get("type") == "server.connected" for e in received)
    finally:
        server.stop()


def test_sse_stop_retains_thread_on_join_timeout(monkeypatch):
    """If the worker cannot be joined within the timeout, stop() must raise
    SSECleanupError, retain _thread, and not falsely report STOPPED."""
    import threading

    from loop_supervisor.sse import SSECleanupError

    server = _FakeServer()
    server.start()
    try:
        client = SSEClient(server.base_url, on_event=lambda e: None)
        client.start()
        time.sleep(0.2)

        real_thread = client._thread
        assert real_thread is not None

        original_join = threading.Thread.join
        original_is_alive = threading.Thread.is_alive
        forced_alive = {"v": True}

        def _fake_join(self, timeout=None):
            if self is real_thread:
                return
            return original_join(self, timeout)

        def _fake_is_alive(self):
            if self is real_thread and forced_alive["v"]:
                return True
            return original_is_alive(self)

        monkeypatch.setattr(threading.Thread, "join", _fake_join)
        monkeypatch.setattr(threading.Thread, "is_alive", _fake_is_alive)

        with pytest.raises(SSECleanupError):
            client.stop()
        assert client._thread is real_thread  # retained
        assert client._state is not SSEConnectionState.STOPPED

        # Let it really stop and retry. Restore real join/is_alive so the
        # retry actually waits for and observes the now-terminated worker.
        monkeypatch.undo()
        real_thread.join(timeout=5.0)
        client.stop()
        assert client._thread is None
    finally:
        server.stop()
