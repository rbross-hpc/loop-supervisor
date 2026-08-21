"""OpenCode process/HTTP adapter.

Owns all details of starting `opencode serve`, creating sessions, and
sending prompts with structured output. The supervisor state machine talks
to the `AgentRunner` protocol instead, so it can be tested with a fake.

Never logs environment variables, full config, or authorization headers.
"""

from __future__ import annotations

import os
import re
import select
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

_READY_RE = re.compile(r"opencode server listening on (https?://\S+)")


class OpenCodeError(RuntimeError):
    """Base class for all OpenCode adapter errors."""


class ServerStartupError(OpenCodeError):
    """Raised when the server process fails to become ready in time."""


class PhaseTimeoutError(OpenCodeError):
    """Raised when a role invocation exceeds its allotted time."""


class AgentInvocationError(OpenCodeError):
    """Raised when the OpenCode API returns a non-2xx or error response."""


class AgentRunner(Protocol):
    """Minimal interface the supervisor needs from an OpenCode backend."""

    def run_agent(
        self,
        *,
        agent: str,
        directory: Path,
        prompt: str,
        json_schema: dict[str, Any] | None = None,
        timeout: float = 1800.0,
    ) -> str:
        """Run one role invocation in a fresh session and return the raw
        text output (expected to be a single JSON object as text, or the
        structured_output field serialized to text)."""
        ...


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class OpenCodeServerConfig:
    executable: str = "opencode"
    hostname: str = "127.0.0.1"
    port: int | None = None
    startup_timeout: float = 30.0
    env: dict[str, str] | None = None


class OpenCodeServer:
    """Manages the lifecycle of one `opencode serve` process."""

    def __init__(self, project_dir: Path, config: OpenCodeServerConfig | None = None) -> None:
        self.project_dir = project_dir
        self.config = config or OpenCodeServerConfig()
        self._process: subprocess.Popen[str] | None = None
        self.base_url: str | None = None
        self._client: httpx.Client | None = None

    def __enter__(self) -> OpenCodeServer:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def start(self) -> None:
        if self._process is not None:
            raise OpenCodeError("server already started")

        port = self.config.port if self.config.port is not None else _free_port()
        env = dict(self.config.env if self.config.env is not None else os.environ)

        try:
            process = subprocess.Popen(
                [
                    self.config.executable,
                    "serve",
                    "--hostname",
                    self.config.hostname,
                    "--port",
                    str(port),
                ],
                cwd=str(self.project_dir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise ServerStartupError(
                f"opencode executable {self.config.executable!r} not found on PATH"
            ) from exc

        self._process = process
        deadline = time.monotonic() + self.config.startup_timeout
        collected: list[str] = []

        assert process.stdout is not None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ServerStartupError(
                    f"opencode serve exited early (code {process.returncode}): "
                    + "\n".join(collected[-20:])
                )
            remaining = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([process.stdout], [], [], min(0.2, remaining))
            if not ready:
                continue
            line = process.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            collected.append(line.rstrip())
            match = _READY_RE.search(line)
            if match:
                base_url = match.group(1)
                self.base_url = base_url
                self._client = httpx.Client(base_url=base_url, timeout=None)
                return

        self.stop()
        raise ServerStartupError(
            f"opencode serve did not become ready within {self.config.startup_timeout}s: "
            + "\n".join(collected[-20:])
        )

    def stop(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)
        if process.stdout is not None:
            process.stdout.close()

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            raise OpenCodeError("server is not started")
        return self._client

    def health(self) -> dict[str, Any]:
        response = self.client.get("/global/health")
        _raise_for_status(response, "health check")
        return response.json()

    def create_session(self, directory: Path, *, title: str) -> str:
        response = self.client.post(
            "/session",
            params={"directory": str(directory)},
            json={"title": title},
        )
        _raise_for_status(response, "create session")
        return response.json()["id"]

    def send_prompt(
        self,
        *,
        session_id: str,
        directory: Path,
        agent: str,
        prompt: str,
        json_schema: dict[str, Any] | None = None,
        timeout: float = 1800.0,
    ) -> str:
        body: dict[str, Any] = {
            "agent": agent,
            "parts": [{"type": "text", "text": prompt}],
        }
        if json_schema is not None:
            body["format"] = {"type": "json_schema", "schema": json_schema}

        if self.base_url is None:
            raise OpenCodeError("server is not started")

        client = httpx.Client(base_url=self.base_url, timeout=timeout)
        try:
            response = client.post(
                f"/session/{session_id}/message",
                params={"directory": str(directory)},
                json=body,
            )
        except httpx.TimeoutException as exc:
            raise PhaseTimeoutError(f"agent {agent!r} did not respond within {timeout}s") from exc
        finally:
            client.close()

        _raise_for_status(response, f"prompt for agent {agent!r}")
        data = response.json()
        return _extract_text(data, agent=agent)

    def abort_session(self, session_id: str) -> None:
        self.client.post(f"/session/{session_id}/abort")

    def run_agent(
        self,
        *,
        agent: str,
        directory: Path,
        prompt: str,
        json_schema: dict[str, Any] | None = None,
        timeout: float = 1800.0,
    ) -> str:
        session_id = self.create_session(directory, title=f"loop:{agent}")
        try:
            return self.send_prompt(
                session_id=session_id,
                directory=directory,
                agent=agent,
                prompt=prompt,
                json_schema=json_schema,
                timeout=timeout,
            )
        except PhaseTimeoutError:
            self.abort_session(session_id)
            raise


def _raise_for_status(response: httpx.Response, action: str) -> None:
    if response.status_code >= 400:
        raise AgentInvocationError(
            f"{action} failed with HTTP {response.status_code}: {response.text[:500]}"
        )


def _extract_text(data: dict[str, Any], *, agent: str) -> str:
    info = data.get("info", {})
    error = info.get("error")
    if error:
        raise AgentInvocationError(f"agent {agent!r} returned an error: {error}")

    structured = info.get("structured_output")
    if structured is not None:
        import json as _json

        return _json.dumps(structured)

    parts = data.get("parts", [])
    text_parts = [p.get("text", "") for p in parts if p.get("type") == "text"]
    if not text_parts:
        raise AgentInvocationError(f"agent {agent!r} returned no text output")
    return "\n".join(text_parts).strip()
