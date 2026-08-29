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
        assert record["directory"] == "/repo"
    finally:
        denier.stop()
        server.stop()


def test_reply_is_scoped_to_the_ask_s_own_directory(tmp_path, capsys):
    """The bare POST /permission/{requestID}/reply route is not
    implicitly scoped to whichever session raised the ask -- confirmed
    live against the real OpenCode 1.18.22 server (ADR 0016), where a
    reply omitting the `directory` query parameter resolved against the
    server's default/current instance instead of the task-worktree
    instance that actually issued the request, and 404'd. This exercises
    the fixture's equivalent of that instance-mismatch: the ask reports
    a directory the reply's scoping check does not match, so the reply
    must fail exactly like the real server's 404 did, and must not be
    counted as a denial."""
    server = _FakeServer(
        {
            "FAKE_OPENCODE_SSE_PERMISSION_ASK": "per_wrongscope",
            "FAKE_OPENCODE_SSE_PERMISSION_ASK_DIRECTORY": "/worktrees/task-001",
            "FAKE_OPENCODE_PERMISSION_REPLY_REQUIRE_DIRECTORY": "/some/other/instance",
        }
    )
    server.start()
    denier = PermissionDenier(server.base_url)
    seen_stderr = ""

    def _saw_failure() -> bool:
        nonlocal seen_stderr
        seen_stderr += capsys.readouterr().err
        return "failed to deny permission request 'per_wrongscope'" in seen_stderr

    try:
        denier.start()
        assert _wait_for(_saw_failure)
        assert "HTTP 404" in seen_stderr
        assert denier.denied_count == 0
        assert denier.denied_summary == []
    finally:
        denier.stop()
        server.stop()


def test_reply_carries_the_ask_s_own_directory_and_succeeds_when_scoped(tmp_path):
    """The positive case for the same scoping mechanism: when the ask's
    directory and the reply's required directory match, the reply must
    succeed and be counted, and the reply the denier actually sent must
    carry that same directory -- not a hardcoded default, and not the
    project root, but whatever the specific ask's own envelope said."""
    log_path = tmp_path / "replies.jsonl"
    server = _FakeServer(
        {
            "FAKE_OPENCODE_SSE_PERMISSION_ASK": "per_rightscope",
            "FAKE_OPENCODE_SSE_PERMISSION_ASK_DIRECTORY": "/worktrees/task-002",
            "FAKE_OPENCODE_PERMISSION_REPLY_REQUIRE_DIRECTORY": "/worktrees/task-002",
            "FAKE_OPENCODE_PERMISSION_REPLY_LOG": str(log_path),
        }
    )
    server.start()
    denier = PermissionDenier(server.base_url)
    try:
        denier.start()
        assert _wait_for(lambda: denier.denied_count == 1)
        assert denier.denied_summary == ["bash"]
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["directory"] == "/worktrees/task-002"
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


def test_reply_http_failure_is_not_counted_as_denied(tmp_path, capsys):
    """A reply POST that gets a non-2xx status must not raise out of the
    SSE event handler (SSE failure is strictly non-fatal per sse.py's own
    contract), and must NOT be counted as a denial: the server never
    accepted the reject, so the agent is still blocked on the ask, not
    denied. Counting it anyway would overstate what actually happened.

    The warning must also name the actual HTTP status, not just say
    "it failed": the one confirmed live occurrence of this failure mode
    required manually cross-referencing OpenCode's own log to find out
    why, specifically because the original bare bool discarded that
    information at the source."""
    server = _FakeServer(
        {
            "FAKE_OPENCODE_SSE_PERMISSION_ASK": "per_fail",
            "FAKE_OPENCODE_PERMISSION_REPLY_STATUS": "500",
        }
    )
    server.start()
    denier = PermissionDenier(server.base_url)
    seen_stderr = ""

    def _saw_failure() -> bool:
        nonlocal seen_stderr
        seen_stderr += capsys.readouterr().err
        return "failed to deny permission request 'per_fail'" in seen_stderr

    try:
        denier.start()
        assert _wait_for(_saw_failure)
        assert "HTTP 500" in seen_stderr
        assert denier.denied_count == 0
        assert denier.denied_summary == []
    finally:
        denier.stop()
        server.stop()


def test_reply_transport_error_is_not_counted_as_denied(monkeypatch, capsys):
    """A network-level failure posting the reply (not just a bad HTTP
    status) must not raise out of the event handler, and must not be
    counted as a denial for the same reason as a non-2xx status: the
    reject was never actually accepted. Exercised directly against
    _on_event/_reply_reject (rather than through a live SSE connection)
    because both PermissionDenier and SSEClient share the same httpx
    module, so a global httpx.Client patch would also break the SSE
    transport itself, not just the reply POST.
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
    assert denier.denied_count == 0
    assert denier.denied_summary == []
    stderr = capsys.readouterr().err
    assert "failed to deny permission request 'per_neterr'" in stderr
    assert "ConnectError" in stderr


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


def test_start_prints_attach_confirmation_once_live(capsys):
    """A successful SSE attach must be positively observable, not just
    inferable from the absence of a start failure -- otherwise "the
    denier attached and saw nothing" is indistinguishable from "the
    denier never attached at all."""
    server = _FakeServer()
    server.start()
    denier = PermissionDenier(server.base_url)
    try:
        denier.start()
        assert _wait_for(
            lambda: f"permission denier watching {server.base_url}" in capsys.readouterr().err
        )
    finally:
        denier.stop()
        server.stop()


def test_successful_denial_still_counts_and_reports(tmp_path, capsys):
    """A reply that the server actually accepts must still be counted
    and reported, distinguishing the healthy path from the two failure
    tests above."""
    server = _FakeServer(
        {
            "FAKE_OPENCODE_SSE_PERMISSION_ASK": "per_ok",
            "FAKE_OPENCODE_PERMISSION_REPLY_LOG": str(tmp_path / "replies.jsonl"),
        }
    )
    server.start()
    denier = PermissionDenier(server.base_url)
    try:
        denier.start()
        assert _wait_for(lambda: denier.denied_count == 1)
        assert denier.denied_summary == ["bash"]
        assert "denied permission request 'per_ok'" in capsys.readouterr().err
    finally:
        denier.stop()
        server.stop()


def test_sse_notice_is_forwarded_to_stderr(capsys):
    """SSE-level notices (reconnects, malformed events, non-2xx stream
    responses) must not be silently discarded; they are diagnosable
    conditions the operator should be able to see."""
    denier = PermissionDenier("http://127.0.0.1:1")
    denier._on_notice("SSE error: boom")
    captured = capsys.readouterr()
    assert "loop-supervisor:" in captured.err
    assert "SSE error: boom" in captured.err


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
