#!/usr/bin/env python3
"""A minimal fake `opencode` executable for testing the adapter in
opencode.py without needing the real binary or network access.

Behavior is controlled entirely through environment variables so the
test process can configure it before spawning:

- FAKE_OPENCODE_MODE:
    "normal"        -- serve normally (default)
    "exit_early"     -- exit(1) immediately instead of serving
    "never_ready"    -- start listening on a socket but never print the
                        ready line, to exercise startup timeout handling
    "partial_line_never_ready" -- write a non-newline-terminated fragment
                        and never complete it, to exercise readiness
                        detection that must not block on a partial line
    "split_ready_line" -- write the ready line in two separate writes
                        with a short delay between them, to exercise
                        reassembly of a line split across reads
    "oversized_line_then_ready" -- write a single non-newline-terminated
                        fragment larger than the pump's fragment bound,
                        terminate it with a newline, then print the real
                        ready line, to exercise recovery after an
                        oversized line is dropped
- FAKE_OPENCODE_RESPONSE: JSON text returned as `structured_output` for
    every /session/{id}/message call.
- FAKE_OPENCODE_ERROR: if set, /session/{id}/message returns this string
    as the assistant message's `error` field instead of output.
- FAKE_OPENCODE_HTTP_STATUS: if set, /session/{id}/message responds with
    this HTTP status code and an empty body.
- FAKE_OPENCODE_DESCENDANT_PID_FILE: if set, spawn a plain (non-session-
    leader) child process that writes its own PID to this path immediately
    on startup, then loops sleeping. The child is started without its own
    new session, so it inherits this process's process group — exactly like
    a real tool subprocess OpenCode spawns would. Tests read the PID and
    probe it with kill(pid, 0) to determine liveness.
- FAKE_OPENCODE_DESCENDANT_IGNORE_SIGTERM: if set (and a descendant pid
    file is configured), the descendant ignores SIGTERM so tests can
    exercise stop()'s escalation to SIGKILL against the whole process
    group.
- FAKE_OPENCODE_SSE_PERMISSION_ASK: if set to a request id, the SSE
    stream emits one `permission.asked` event (with that id and a fixed
    `permission` key of "bash") immediately after `server.connected`.
- FAKE_OPENCODE_SSE_PERMISSION_ASK_DIRECTORY: overrides the `directory`
    field of that `permission.asked` event's envelope (default "/repo").
    Used to simulate an ask raised by a different OpenCode instance
    (e.g. a task worktree) than whatever directory a reply might
    default to, together with
    FAKE_OPENCODE_PERMISSION_REPLY_REQUIRE_DIRECTORY below.
- FAKE_OPENCODE_PERMISSION_REPLY_LOG: if set, every
    `POST /permission/{requestID}/reply` appends one JSON line
    `{"request_id": ..., "body": ..., "directory": ...}` to this file
    (`directory` is the request's own `?directory=` query parameter, or
    null if absent), then responds `200 true`.
- FAKE_OPENCODE_PERMISSION_REPLY_REQUIRE_DIRECTORY: if set, every
    `POST /permission/{requestID}/reply` is scoped: the reply succeeds
    only if the request's own `?directory=` query parameter equals this
    value exactly. A missing or mismatched `directory` gets a 404, the
    same instance-mismatch failure mode confirmed against the real
    OpenCode 1.18.22 server (see ADR 0016) -- the real server resolves
    this route against whatever instance the query's `directory`
    identifies, not against the session that raised the ask.
- FAKE_OPENCODE_SELF_PID_FILE: if set, this process writes its own pid
    to this path immediately after it starts listening, so a test
    driving the real supervisor CLI as a subprocess (rather than calling
    OpenCodeServer directly) can still discover this process's pid to
    assert on its liveness after signaling the supervisor.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs


def _parse_args(argv: list[str]) -> tuple[str, int]:
    hostname = "127.0.0.1"
    port = 0
    it = iter(argv)
    for arg in it:
        if arg == "--hostname":
            hostname = next(it)
        elif arg == "--port":
            port = int(next(it))
    return hostname, port


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def _send_json(
        self,
        status: int,
        payload: object,
        *,
        trickle_interval: float = 0.0,
        completion_marker: str | None = None,
    ) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if trickle_interval <= 0:
            self.wfile.write(body)
            return

        import time as _time

        try:
            for byte in body:
                self.wfile.write(bytes((byte,)))
                self.wfile.flush()
                _time.sleep(trickle_interval)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        if completion_marker is not None:
            with open(completion_marker, "w") as marker:
                marker.write("complete")

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/global/health"):
            self._send_json(200, {"healthy": True, "version": "fake"})
            return
        if self.path.startswith("/global/event"):
            self._handle_sse()
            return
        self._send_json(404, {"error": "not found"})

    def _handle_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        import time

        payload = json.dumps(
            {"directory": "/repo", "payload": {"type": "server.connected", "properties": {}}}
        )
        try:
            self.wfile.write(f"data: {payload}\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

        ask_request_id = os.environ.get("FAKE_OPENCODE_SSE_PERMISSION_ASK")
        if ask_request_id:
            ask_directory = os.environ.get("FAKE_OPENCODE_SSE_PERMISSION_ASK_DIRECTORY", "/repo")
            ask_payload = json.dumps(
                {
                    "directory": ask_directory,
                    "payload": {
                        "type": "permission.asked",
                        "properties": {
                            "id": ask_request_id,
                            "sessionID": "ses_fake123",
                            "permission": "bash",
                            "patterns": ["*"],
                        },
                    },
                }
            )
            try:
                self.wfile.write(f"data: {ask_payload}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        sse_mode = os.environ.get("FAKE_OPENCODE_SSE", "hold")
        if sse_mode == "disconnect":
            return
        while True:
            try:
                time.sleep(0.1)
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                break

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            json.loads(raw or b"{}")
        except json.JSONDecodeError:
            pass

        path_only = self.path.split("?", 1)[0]
        if path_only.startswith("/session/") and path_only.endswith("/message"):
            status_override = os.environ.get("FAKE_OPENCODE_HTTP_STATUS")
            if status_override:
                self.send_response(int(status_override))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            error = os.environ.get("FAKE_OPENCODE_ERROR")
            if error:
                self._send_json(200, {"info": {"error": error}, "parts": []})
                return

            block_seconds = os.environ.get("FAKE_OPENCODE_MESSAGE_BLOCK_SECONDS")
            if block_seconds:
                import time as _time

                _time.sleep(float(block_seconds))

            malformed = os.environ.get("FAKE_OPENCODE_MESSAGE_MALFORMED")
            if malformed == "not_json":
                body = b"not json at all {{{"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if malformed == "array":
                self._send_json(200, [1, 2, 3])
                return
            if malformed == "info_not_object":
                self._send_json(200, {"info": "not an object", "parts": []})
                return
            if malformed == "parts_not_list":
                self._send_json(200, {"info": {}, "parts": "not a list"})
                return

            raw_response = os.environ.get("FAKE_OPENCODE_RESPONSE", "{}")
            message_trickle = float(os.environ.get("FAKE_OPENCODE_MESSAGE_TRICKLE_INTERVAL", "0"))
            self._send_json(
                200,
                {
                    "info": {"structured_output": json.loads(raw_response)},
                    "parts": [],
                },
                trickle_interval=message_trickle,
                completion_marker=os.environ.get("FAKE_OPENCODE_MESSAGE_COMPLETION_MARKER"),
            )
            return

        if path_only.startswith("/session/") and path_only.endswith("/abort"):
            block_seconds = os.environ.get("FAKE_OPENCODE_ABORT_BLOCK_SECONDS")
            if block_seconds:
                import time as _time

                _time.sleep(float(block_seconds))
            if os.environ.get("FAKE_OPENCODE_ABORT_FAIL"):
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_json(200, True)
            return

        if path_only.startswith("/permission/") and path_only.endswith("/reply"):
            request_id = path_only[len("/permission/") : -len("/reply")]
            status_override = os.environ.get("FAKE_OPENCODE_PERMISSION_REPLY_STATUS")
            if status_override:
                self.send_response(int(status_override))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            required_directory = os.environ.get("FAKE_OPENCODE_PERMISSION_REPLY_REQUIRE_DIRECTORY")
            if required_directory is not None:
                query = self.path.split("?", 1)[1] if "?" in self.path else ""
                given_directory = parse_qs(query).get("directory", [None])[0]
                if given_directory != required_directory:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            log_path = os.environ.get("FAKE_OPENCODE_PERMISSION_REPLY_LOG")
            if log_path:
                reply_body: object = None
                try:
                    reply_body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    pass
                query = self.path.split("?", 1)[1] if "?" in self.path else ""
                logged_directory = parse_qs(query).get("directory", [None])[0]
                with open(log_path, "a") as f:
                    f.write(
                        json.dumps(
                            {
                                "request_id": request_id,
                                "body": reply_body,
                                "directory": logged_directory,
                            }
                        )
                        + "\n"
                    )
            self._send_json(200, True)
            return

        if path_only == "/session":
            block_seconds = os.environ.get("FAKE_OPENCODE_SESSION_BLOCK_SECONDS")
            if block_seconds:
                import time as _time

                _time.sleep(float(block_seconds))

            malformed = os.environ.get("FAKE_OPENCODE_SESSION_MALFORMED")
            if malformed == "not_json":
                body = b"not json at all {{{"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if malformed == "array":
                self._send_json(200, [1, 2, 3])
                return
            if malformed == "no_id":
                self._send_json(200, {})
                return
            if malformed == "non_string_id":
                self._send_json(200, {"id": 12345})
                return

            session_trickle = float(os.environ.get("FAKE_OPENCODE_SESSION_TRICKLE_INTERVAL", "0"))
            self._send_json(
                200,
                {"id": "ses_fake123"},
                trickle_interval=session_trickle,
                completion_marker=os.environ.get("FAKE_OPENCODE_SESSION_COMPLETION_MARKER"),
            )
            return

        self._send_json(404, {"error": "not found"})


_DESCENDANT_SCRIPT = """
import os, signal, sys, time
pid_file = sys.argv[1]
if len(sys.argv) > 2 and sys.argv[2] == "ignore_sigterm":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(pid_file, "w") as f:
    f.write(str(os.getpid()))
while True:
    time.sleep(0.05)
"""


def _maybe_spawn_descendant() -> subprocess.Popen[bytes] | None:
    pid_file = os.environ.get("FAKE_OPENCODE_DESCENDANT_PID_FILE")
    if not pid_file:
        return None
    args = [sys.executable, "-c", _DESCENDANT_SCRIPT, pid_file]
    if os.environ.get("FAKE_OPENCODE_DESCENDANT_IGNORE_SIGTERM"):
        args.append("ignore_sigterm")
    # Deliberately no start_new_session here: this child must inherit the
    # fake server's own process group, exactly like a real tool/agent
    # subprocess OpenCode spawns would, so it is only reachable via
    # group-wide signaling (killpg), not by signaling the leader alone.
    return subprocess.Popen(args)


def main() -> int:
    mode = os.environ.get("FAKE_OPENCODE_MODE", "normal")

    if len(sys.argv) < 2 or sys.argv[1] != "serve":
        print("usage: fake_opencode.py serve ...", file=sys.stderr)
        return 1

    hostname, port = _parse_args(sys.argv[2:])

    if mode == "exit_early":
        _maybe_spawn_descendant()
        print("simulated early failure", file=sys.stderr)
        return 1

    _maybe_spawn_descendant()

    server = ThreadingHTTPServer((hostname, port), Handler)
    actual_port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    if mode == "never_ready":
        thread.join()
        return 0

    if mode == "ignore_sigterm":
        import signal as _signal

        _signal.signal(_signal.SIGTERM, _signal.SIG_IGN)

    if mode == "partial_line_never_ready":
        # Write a fragment with no trailing newline and never complete it,
        # to exercise the case where a naive readline()-based readiness
        # reader would block indefinitely on the partial line rather than
        # honoring the startup deadline.
        sys.stdout.write("opencode server starting up, almost there")
        sys.stdout.flush()
        thread.join()
        return 0

    if mode == "split_ready_line":
        import time as _time

        ready_line = f"opencode server listening on http://{hostname}:{actual_port}\n"
        split_at = len(ready_line) // 2
        sys.stdout.write(ready_line[:split_at])
        sys.stdout.flush()
        _time.sleep(0.2)
        sys.stdout.write(ready_line[split_at:])
        sys.stdout.flush()
        thread.join()
        return 0

    if mode == "oversized_line_then_ready":
        # Write a single line far larger than the pump's fragment bound,
        # with no newline for a while, to exercise the pump's oversized-
        # line handling: it must drop the accumulated bytes instead of
        # growing without limit, then correctly resume normal line
        # handling (recognizing the real ready line) once this line is
        # finally terminated.
        sys.stdout.write("x" * (200 * 1024))
        sys.stdout.flush()
        sys.stdout.write("\n")
        sys.stdout.write(f"opencode server listening on http://{hostname}:{actual_port}\n")
        sys.stdout.flush()
        thread.join()
        return 0

    print(f"opencode server listening on http://{hostname}:{actual_port}", flush=True)
    self_pid_file = os.environ.get("FAKE_OPENCODE_SELF_PID_FILE")
    if self_pid_file:
        with open(self_pid_file, "w") as f:
            f.write(str(os.getpid()))
    if mode == "exit_after_ready":
        import time as _time

        _time.sleep(0.2)
        server.shutdown()
        thread.join()
        return 0
    thread.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
