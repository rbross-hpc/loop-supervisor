"""Tests for src/loop_supervisor/permissions.py (PermissionDenier)."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from loop_supervisor.permissions import PermissionDenier

_FIXTURE = Path(__file__).parent / "fixtures" / "fake_opencode.py"


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


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_permission_asked_triggers_reject_reply(tmp_path):
    log_path = tmp_path / "replies.jsonl"
    server = _FakeServer(
        {
            "FAKE_OPENCODE_SSE_PERMISSION_ASK": "per_abc123",
            "FAKE_OPENCODE_PERMISSION_REPLY_LOG": str(log_path),
        }
    )
    server.start()
    denier = PermissionDenier(server.base_url)
    try:
        denier.start()
        assert _wait_for(lambda: log_path.exists() and log_path.read_text().strip() != "")
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["request_id"] == "per_abc123"
        assert record["body"] == {"reply": "reject"}
    finally:
        denier.stop()
        server.stop()


def test_non_permission_events_do_not_trigger_a_reply(tmp_path):
    log_path = tmp_path / "replies.jsonl"
    server = _FakeServer({"FAKE_OPENCODE_PERMISSION_REPLY_LOG": str(log_path)})
    server.start()
    denier = PermissionDenier(server.base_url)
    try:
        denier.start()
        time.sleep(0.5)
        assert not log_path.exists()
    finally:
        denier.stop()
        server.stop()


def test_event_with_id_but_wrong_type_is_ignored():
    """An event whose raw properties happen to carry an 'id' field but
    whose type is not permission.asked must not be treated as a
    permission request. This specifically exercises the event-type
    filter, distinct from the id-presence check exercised elsewhere."""
    denier = PermissionDenier("http://127.0.0.1:1")
    denier._on_event(
        {
            "directory": "/repo",
            "payload": {
                "type": "permission.replied",
                "properties": {
                    "id": "per_should_not_count",
                    "sessionID": "ses1",
                    "permission": "bash",
                },
            },
        }
    )
    assert denier.denied_count == 0


def test_denied_count_and_summary_track_permission_key(tmp_path):
    log_path = tmp_path / "replies.jsonl"
    server = _FakeServer(
        {
            "FAKE_OPENCODE_SSE_PERMISSION_ASK": "per_xyz",
            "FAKE_OPENCODE_PERMISSION_REPLY_LOG": str(log_path),
        }
    )
    server.start()
    denier = PermissionDenier(server.base_url)
    try:
        denier.start()
        assert _wait_for(lambda: denier.denied_count == 1)
        assert denier.denied_summary == ["bash"]
    finally:
        denier.stop()
        server.stop()


def test_reply_http_failure_is_swallowed(tmp_path):
    """A reply POST that gets a non-2xx status must not raise out of the
    SSE event handler or otherwise propagate; SSE failure is strictly
    non-fatal per sse.py's own contract."""
    server = _FakeServer(
        {
            "FAKE_OPENCODE_SSE_PERMISSION_ASK": "per_fail",
            "FAKE_OPENCODE_PERMISSION_REPLY_STATUS": "500",
        }
    )
    server.start()
    denier = PermissionDenier(server.base_url)
    try:
        denier.start()
        # No exception should escape; the SSE client thread must stay
        # alive and the denial is still counted (the reply attempt was
        # made, whether or not the server accepted it).
        assert _wait_for(lambda: denier.denied_count == 1)
    finally:
        denier.stop()
        server.stop()


def test_reply_transport_error_is_swallowed(monkeypatch):
    """A network-level failure posting the reply (not just a bad HTTP
    status) must not raise out of the event handler. Exercised directly
    against _on_event/_reply_reject (rather than through a live SSE
    connection) because both PermissionDenier and SSEClient share the
    same httpx module, so a global httpx.Client patch would also break
    the SSE transport itself, not just the reply POST.
    """
    import loop_supervisor.permissions as permissions_module

    denier = PermissionDenier("http://127.0.0.1:1")

    class _BoomClient:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, *args, **kwargs):
            raise permissions_module.httpx.ConnectError("boom")

        def close(self):
            pass

    monkeypatch.setattr(permissions_module.httpx, "Client", _BoomClient)

    denier._on_event(
        {
            "directory": "/repo",
            "payload": {
                "type": "permission.asked",
                "properties": {"id": "per_neterr", "sessionID": "ses1", "permission": "bash"},
            },
        }
    )
    assert denier.denied_count == 1


def test_malformed_permission_asked_missing_id_is_ignored(tmp_path):
    """A permission.asked event whose properties lack a usable id must
    not increment the denied count or crash the handler."""
    denier = PermissionDenier("http://127.0.0.1:1")
    denier._on_event(
        {
            "directory": "/repo",
            "payload": {"type": "permission.asked", "properties": {}},
        }
    )
    assert denier.denied_count == 0


def test_stop_before_start_is_safe():
    denier = PermissionDenier("http://127.0.0.1:9")
    denier.stop()


def test_stop_is_idempotent(tmp_path):
    server = _FakeServer()
    server.start()
    denier = PermissionDenier(server.base_url)
    try:
        denier.start()
        time.sleep(0.1)
        denier.stop()
        denier.stop()
    finally:
        server.stop()
