import json
import os
import sys
from pathlib import Path

import pytest

from loop_supervisor.opencode import (
    AgentInvocationError,
    OpenCodeServer,
    OpenCodeServerConfig,
    ServerStartupError,
)

FIXTURE = str(Path(__file__).parent / "fixtures" / "fake_opencode.py")


def _config(**overrides) -> OpenCodeServerConfig:
    env = dict(os.environ)
    env.update(overrides.pop("env", {}))
    return OpenCodeServerConfig(
        executable=sys.executable,
        startup_timeout=overrides.pop("startup_timeout", 5.0),
        env=env,
        **overrides,
    )


def _argv_config(**env_overrides) -> OpenCodeServerConfig:
    env = dict(os.environ)
    env.update(env_overrides)
    return OpenCodeServerConfig(executable=sys.executable, startup_timeout=5.0, env=env)


class _FakeServer(OpenCodeServer):
    """Prepends the fixture script path so `self.config.executable` (python)
    actually runs our fake fixture module with `serve` as argv[1]."""

    def start(self) -> None:
        import subprocess

        original_popen = subprocess.Popen

        def patched_popen(args, **kwargs):
            args = [args[0], FIXTURE, *args[1:]]
            return original_popen(args, **kwargs)

        subprocess.Popen = patched_popen  # type: ignore[assignment]
        try:
            super().start()
        finally:
            subprocess.Popen = original_popen  # type: ignore[assignment]


def test_server_starts_and_health_check(tmp_path):
    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    with _FakeServer(tmp_path, config) as server:
        health = server.health()
        assert health["healthy"] is True


def test_server_startup_error_on_early_exit(tmp_path):
    config = _argv_config(FAKE_OPENCODE_MODE="exit_early")
    with pytest.raises(ServerStartupError):
        with _FakeServer(tmp_path, config):
            pass


def test_server_startup_timeout_when_never_ready(tmp_path):
    config = _argv_config(FAKE_OPENCODE_MODE="never_ready")
    config.startup_timeout = 1.0
    with pytest.raises(ServerStartupError):
        with _FakeServer(tmp_path, config):
            pass


def test_missing_executable_raises_startup_error(tmp_path):
    config = OpenCodeServerConfig(executable="/nonexistent/opencode-binary")
    server = OpenCodeServer(tmp_path, config)
    with pytest.raises(ServerStartupError):
        server.start()


def test_run_agent_returns_structured_output_as_text(tmp_path):
    response = {"status": "COMPLETE"}
    config = _argv_config(
        FAKE_OPENCODE_MODE="normal",
        FAKE_OPENCODE_RESPONSE=json.dumps(response),
    )
    with _FakeServer(tmp_path, config) as server:
        raw = server.run_agent(agent="loop-planner", directory=tmp_path, prompt="go")
        assert json.loads(raw) == response


def test_run_agent_raises_on_assistant_error(tmp_path):
    config = _argv_config(FAKE_OPENCODE_MODE="normal", FAKE_OPENCODE_ERROR="model exploded")
    with _FakeServer(tmp_path, config) as server:
        with pytest.raises(AgentInvocationError):
            server.run_agent(agent="loop-planner", directory=tmp_path, prompt="go")


def test_run_agent_raises_on_http_error_status(tmp_path):
    config = _argv_config(FAKE_OPENCODE_MODE="normal", FAKE_OPENCODE_HTTP_STATUS="500")
    with _FakeServer(tmp_path, config) as server:
        with pytest.raises(AgentInvocationError):
            server.run_agent(agent="loop-planner", directory=tmp_path, prompt="go")


def test_stop_is_idempotent(tmp_path):
    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    server = _FakeServer(tmp_path, config)
    server.start()
    server.stop()
    server.stop()  # must not raise


def test_client_property_raises_before_start(tmp_path):
    from loop_supervisor.opencode import OpenCodeError

    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    server = _FakeServer(tmp_path, config)
    with pytest.raises(OpenCodeError):
        _ = server.client
