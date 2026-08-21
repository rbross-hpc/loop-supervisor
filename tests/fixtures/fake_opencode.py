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
- FAKE_OPENCODE_RESPONSE: JSON text returned as `structured_output` for
    every /session/{id}/message call.
- FAKE_OPENCODE_ERROR: if set, /session/{id}/message returns this string
    as the assistant message's `error` field instead of output.
- FAKE_OPENCODE_HTTP_STATUS: if set, /session/{id}/message responds with
    this HTTP status code and an empty body.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


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

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/global/health"):
            self._send_json(200, {"healthy": True, "version": "fake"})
            return
        self._send_json(404, {"error": "not found"})

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

            raw_response = os.environ.get("FAKE_OPENCODE_RESPONSE", "{}")
            self._send_json(
                200,
                {
                    "info": {"structured_output": json.loads(raw_response)},
                    "parts": [],
                },
            )
            return

        if path_only.startswith("/session/") and path_only.endswith("/abort"):
            self._send_json(200, True)
            return

        if path_only == "/session":
            self._send_json(200, {"id": "ses_fake123"})
            return

        self._send_json(404, {"error": "not found"})


def main() -> int:
    mode = os.environ.get("FAKE_OPENCODE_MODE", "normal")

    if len(sys.argv) < 2 or sys.argv[1] != "serve":
        print("usage: fake_opencode.py serve ...", file=sys.stderr)
        return 1

    hostname, port = _parse_args(sys.argv[2:])

    if mode == "exit_early":
        print("simulated early failure", file=sys.stderr)
        return 1

    server = ThreadingHTTPServer((hostname, port), Handler)
    actual_port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    if mode == "never_ready":
        thread.join()
        return 0

    print(f"opencode server listening on http://{hostname}:{actual_port}", flush=True)
    thread.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
