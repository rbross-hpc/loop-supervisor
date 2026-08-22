import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from loop_supervisor.opencode import (
    AgentInvocationError,
    OpenCodeCleanupError,
    OpenCodeError,
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
    def _serve_command(self, port: int) -> list[str]:
        return [
            sys.executable,
            FIXTURE,
            "serve",
            "--hostname",
            self.config.hostname,
            "--port",
            str(port),
        ]


def test_server_starts_and_health_check(tmp_path):
    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    with _FakeServer(tmp_path, config) as server:
        health = server.health()
        assert health["healthy"] is True


def test_server_startup_error_on_early_exit(tmp_path):
    config = _argv_config(FAKE_OPENCODE_MODE="exit_early")
    server = _FakeServer(tmp_path, config)
    with pytest.raises(ServerStartupError):
        server.start()
    assert server._owner is None
    assert server._client is None
    assert server._stdout_thread is None


def test_server_startup_timeout_when_never_ready(tmp_path):
    config = _argv_config(FAKE_OPENCODE_MODE="never_ready")
    config.startup_timeout = 1.0
    server = _FakeServer(tmp_path, config)
    with pytest.raises(ServerStartupError):
        server.start()
    assert server._owner is None
    assert server._client is None
    assert server._stdout_thread is None


def test_server_startup_partial_line_does_not_hang(tmp_path):
    """A subprocess that writes a non-newline-terminated fragment and never
    completes it must still time out and raise ServerStartupError within
    (roughly) the configured deadline, not hang indefinitely behind a
    blocked readline()."""
    import time

    config = _argv_config(FAKE_OPENCODE_MODE="partial_line_never_ready")
    config.startup_timeout = 1.0
    server = _FakeServer(tmp_path, config)

    start = time.monotonic()
    with pytest.raises(ServerStartupError) as excinfo:
        server.start()
    elapsed = time.monotonic() - start

    assert elapsed < 5.0
    assert "almost there" in str(excinfo.value)
    assert server._owner is None
    assert server._client is None
    assert server._stdout_thread is None


def test_server_startup_handles_ready_line_split_across_writes(tmp_path):
    """A ready line written in two separate chunks (e.g. a small pipe
    write followed by the remainder shortly after) must still be
    correctly reassembled and recognized, proving the pump's partial-line
    buffering works across reads, not only within one read() call."""
    config = _argv_config(FAKE_OPENCODE_MODE="split_ready_line")
    with _FakeServer(tmp_path, config) as server:
        assert server.base_url is not None
        health = server.health()
        assert health["healthy"] is True


def test_server_startup_cleans_up_process_on_unexpected_exception(tmp_path, monkeypatch):
    """Any exception raised while parsing readiness output (not just the
    two dedicated ServerStartupError paths) must still terminate the
    subprocess rather than leaking it."""
    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    server = _FakeServer(tmp_path, config)

    def _boom(self, launcher):
        raise RuntimeError("simulated unexpected failure while awaiting readiness")

    monkeypatch.setattr(OpenCodeServer, "_await_ready", _boom)
    with pytest.raises(RuntimeError):
        server.start()
    assert server._owner is None


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


# -- stop() exception safety across stages -----------------------------------


def test_stop_terminates_process_even_if_client_close_fails(tmp_path, monkeypatch):
    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    server = _FakeServer(tmp_path, config)
    server.start()
    owner = server._owner
    assert owner is not None
    launcher = owner.launcher

    def _boom(self):
        raise RuntimeError("simulated client close failure")

    monkeypatch.setattr(type(server._client), "close", _boom)

    with pytest.raises(OpenCodeCleanupError):
        server.stop()

    assert launcher.poll() is not None  # launcher was still terminated
    assert server._owner is None
    # Client close failed, so ownership is retained for retry rather than
    # silently discarded.
    assert server._client is not None


def test_stop_terminates_process_even_if_stdout_join_fails(tmp_path, monkeypatch):
    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    server = _FakeServer(tmp_path, config)
    server.start()
    owner = server._owner
    assert owner is not None
    launcher = owner.launcher

    def _boom(self, timeout=None):
        raise RuntimeError("simulated join failure")

    import threading

    monkeypatch.setattr(threading.Thread, "join", _boom)

    with pytest.raises(OpenCodeCleanupError):
        server.stop()

    assert launcher.poll() is not None  # launcher was still terminated
    assert server._owner is None
    assert server._client is None


def test_stop_reports_multiple_failures_as_exception_group(tmp_path, monkeypatch):
    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    server = _FakeServer(tmp_path, config)
    server.start()

    def _boom_client(self):
        raise RuntimeError("simulated client close failure")

    monkeypatch.setattr(type(server._client), "close", _boom_client)

    def _boom_join(self, timeout=None):
        raise RuntimeError("simulated join failure")

    import threading

    monkeypatch.setattr(threading.Thread, "join", _boom_join)

    with pytest.raises(ExceptionGroup) as excinfo:
        server.stop()
    assert len(excinfo.value.exceptions) == 2


def test_stop_retains_stdout_thread_on_join_timeout(tmp_path, monkeypatch):
    """A timed-out join must not silently declare the pump thread dead:
    stop() reports incomplete cleanup and keeps the thread reference so a
    later stop() can retry."""
    from loop_supervisor.opencode import OpenCodeCleanupError

    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    server = _FakeServer(tmp_path, config)
    server.start()

    real_thread = server._stdout_thread
    assert real_thread is not None

    import threading

    alive = {"v": True}
    original_join = threading.Thread.join
    original_is_alive = threading.Thread.is_alive

    def _fake_join(self, timeout=None):
        if self is real_thread:
            return
        return original_join(self, timeout)

    def _fake_is_alive(self):
        if self is real_thread:
            return alive["v"]
        return original_is_alive(self)

    monkeypatch.setattr(threading.Thread, "join", _fake_join)
    monkeypatch.setattr(threading.Thread, "is_alive", _fake_is_alive)

    with pytest.raises(OpenCodeCleanupError):
        server.stop()
    assert server._stdout_thread is real_thread  # retained

    # Now let it "die" and retry: the reference should clear.
    alive["v"] = False
    server.stop()
    assert server._stdout_thread is None


def test_stop_retains_owner_when_launcher_wait_is_unconfirmed(tmp_path, monkeypatch):
    from loop_supervisor.opencode import _ProcessOwner

    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    server = _FakeServer(tmp_path, config)
    server.start()
    owner = server._owner
    assert owner is not None

    original = _ProcessOwner.shutdown_confirmed
    monkeypatch.setattr(_ProcessOwner, "shutdown_confirmed", lambda self: False)
    with pytest.raises((OpenCodeCleanupError, ExceptionGroup)):
        server.stop()
    assert server._owner is owner

    monkeypatch.setattr(_ProcessOwner, "shutdown_confirmed", original)
    server.stop()
    assert server._owner is None


# -- process-group ownership of descendants -----------------------------


def _wait_for_pid_file(pid_file: Path, timeout: float = 5.0) -> int:
    """Wait for pid_file to appear and return the PID written in it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid_file.exists():
            try:
                return int(pid_file.read_text().strip())
            except (ValueError, OSError):
                pass
        time.sleep(0.02)
    raise AssertionError(f"descendant pid file {pid_file} never appeared")


def _pid_is_alive(pid: int) -> bool:
    """True if the process with this PID exists and is not a zombie."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_for_marker(marker: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"descendant marker {marker} never appeared")


def _marker_mtime(marker: Path) -> float | None:
    try:
        return marker.stat().st_mtime
    except FileNotFoundError:
        return None


def _descendant_is_alive(marker: Path, settle: float = 0.3) -> bool:
    """True if the marker file's mtime keeps advancing."""
    first = _marker_mtime(marker)
    if first is None:
        return False
    time.sleep(settle)
    second = _marker_mtime(marker)
    if second is None:
        return False
    return second > first


def test_stop_terminates_descendant_in_process_group(tmp_path):
    """A tool/agent-like descendant that inherits the server's process
    group must be terminated by stop(), not left running after stop()
    returns successfully. Verified by PID liveness, not mtime."""
    pid_file = tmp_path / "descendant.pid"
    config = _argv_config(
        FAKE_OPENCODE_MODE="normal",
        FAKE_OPENCODE_DESCENDANT_PID_FILE=str(pid_file),
    )
    server = _FakeServer(tmp_path, config)
    server.start()
    descendant_pid: int | None = None
    try:
        descendant_pid = _wait_for_pid_file(pid_file)
        assert _pid_is_alive(descendant_pid)
        assert server._owner is not None
    finally:
        server.stop()

    assert server._owner is None
    if descendant_pid is not None:
        time.sleep(0.1)
        assert not _pid_is_alive(descendant_pid), (
            f"descendant pid {descendant_pid} is still alive after stop()"
        )


def test_stop_escalates_to_sigkill_when_descendant_ignores_sigterm(tmp_path, monkeypatch):
    """A descendant that ignores SIGTERM must still be terminated by
    stop() escalating to SIGKILL against the whole process group."""
    import loop_supervisor.opencode as oc_module

    monkeypatch.setattr(oc_module, "_GROUP_TERM_WAIT_SECONDS", 0.5)

    pid_file = tmp_path / "descendant.pid"
    config = _argv_config(
        FAKE_OPENCODE_MODE="normal",
        FAKE_OPENCODE_DESCENDANT_PID_FILE=str(pid_file),
        FAKE_OPENCODE_DESCENDANT_IGNORE_SIGTERM="1",
    )
    server = _FakeServer(tmp_path, config)
    server.start()
    descendant_pid: int | None = None
    try:
        descendant_pid = _wait_for_pid_file(pid_file)
        assert _pid_is_alive(descendant_pid)

        start = time.monotonic()
        server.stop()
        elapsed = time.monotonic() - start
    finally:
        pass

    assert elapsed < 15.0
    assert server._owner is None
    if descendant_pid is not None:
        time.sleep(0.1)
        assert not _pid_is_alive(descendant_pid)


def test_stop_reaps_descendant_after_leader_exits_early(tmp_path):
    """If the opencode child exits early but a descendant it spawned
    survives, the internal stop() during startup failure must still kill
    that descendant via the anchored process group."""
    pid_file = tmp_path / "descendant.pid"
    config = _argv_config(
        FAKE_OPENCODE_MODE="exit_early",
        FAKE_OPENCODE_DESCENDANT_PID_FILE=str(pid_file),
    )
    server = _FakeServer(tmp_path, config)

    with pytest.raises(ServerStartupError):
        server.start()

    descendant_pid: int | None = None
    try:
        descendant_pid = _wait_for_pid_file(pid_file, timeout=3.0)
    except AssertionError:
        pass

    if descendant_pid is not None:
        time.sleep(0.2)
        assert not _pid_is_alive(descendant_pid), (
            f"descendant {descendant_pid} survived startup-failure cleanup"
        )


def test_stop_retries_kill_command_after_first_write_failure(tmp_path, monkeypatch):
    from loop_supervisor.opencode import _ProcessOwner

    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    server = _FakeServer(tmp_path, config)
    server.start()
    owner = server._owner
    assert owner is not None
    original = _ProcessOwner.send
    failed = {"value": False}

    def _fail_first_kill(self, command):
        if command == "kill" and not failed["value"]:
            failed["value"] = True
            raise OSError("simulated command write failure")
        return original(self, command)

    monkeypatch.setattr(_ProcessOwner, "send", _fail_first_kill)
    with pytest.raises((OpenCodeCleanupError, ExceptionGroup)):
        server.stop()
    assert server._owner is owner
    server.stop()
    assert server._owner is None


def test_stop_is_idempotent_after_group_already_gone(tmp_path):
    """Calling stop() again after everything is cleaned up must be a no-op."""
    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    server = _FakeServer(tmp_path, config)
    server.start()
    server.stop()
    assert server._owner is None
    server.stop()  # must not raise


def test_startup_failure_takes_precedence_over_stop_cleanup_failure(tmp_path, monkeypatch):
    """If start() fails and the internal best-effort stop() during that
    failure also has problems, the original ServerStartupError must still
    be what's raised — not a cleanup-related exception."""
    config = _argv_config(FAKE_OPENCODE_MODE="exit_early")
    server = _FakeServer(tmp_path, config)

    def _boom(self):
        raise RuntimeError("simulated client close failure")

    monkeypatch.setattr(OpenCodeServer, "stop", lambda self: (_ for _ in ()).throw(RuntimeError()))

    with pytest.raises(ServerStartupError):
        server.start()


def test_client_property_raises_before_start(tmp_path):
    from loop_supervisor.opencode import OpenCodeError

    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    server = _FakeServer(tmp_path, config)
    with pytest.raises(OpenCodeError):
        _ = server.client


# -- bounded invocation timeout (session creation + prompt) ------------------


def test_run_agent_timeout_during_session_creation(tmp_path):
    """A session-creation request that hangs longer than the invocation
    timeout must raise PhaseTimeoutError, bounded by the invocation
    timeout — not hang indefinitely on the control client's timeout=None
    default."""
    from loop_supervisor.opencode import PhaseTimeoutError

    config = _argv_config(
        FAKE_OPENCODE_MODE="normal",
        FAKE_OPENCODE_SESSION_BLOCK_SECONDS="5",
    )
    with _FakeServer(tmp_path, config) as server:
        import time

        start = time.monotonic()
        with pytest.raises(PhaseTimeoutError):
            server.run_agent(agent="loop-planner", directory=tmp_path, prompt="go", timeout=1.0)
        elapsed = time.monotonic() - start
        assert elapsed < 4.0

        # No invocation is left registered: session creation never
        # succeeded, so there is no session ID to have registered.
        assert server.active_invocations() == []


def test_run_agent_session_creation_consumes_invocation_budget(tmp_path):
    """Time spent creating the session is charged against the same
    deadline as the prompt: a slow-but-successful session creation must
    leave less time for the prompt, not reset a fresh full timeout."""
    config = _argv_config(
        FAKE_OPENCODE_MODE="normal",
        FAKE_OPENCODE_SESSION_BLOCK_SECONDS="0.5",
        FAKE_OPENCODE_MESSAGE_BLOCK_SECONDS="0.5",
    )
    with _FakeServer(tmp_path, config) as server:
        from loop_supervisor.opencode import PhaseTimeoutError

        with pytest.raises(PhaseTimeoutError):
            server.run_agent(agent="loop-planner", directory=tmp_path, prompt="go", timeout=0.8)


# -- malformed JSON classification -------------------------------------------


def test_create_session_rejects_non_json_body(tmp_path):
    config = _argv_config(FAKE_OPENCODE_MODE="normal", FAKE_OPENCODE_SESSION_MALFORMED="not_json")
    with _FakeServer(tmp_path, config) as server:
        with pytest.raises(AgentInvocationError):
            server.run_agent(agent="loop-planner", directory=tmp_path, prompt="go")


def test_create_session_rejects_array_body(tmp_path):
    config = _argv_config(FAKE_OPENCODE_MODE="normal", FAKE_OPENCODE_SESSION_MALFORMED="array")
    with _FakeServer(tmp_path, config) as server:
        with pytest.raises(AgentInvocationError):
            server.run_agent(agent="loop-planner", directory=tmp_path, prompt="go")


def test_create_session_rejects_missing_id(tmp_path):
    config = _argv_config(FAKE_OPENCODE_MODE="normal", FAKE_OPENCODE_SESSION_MALFORMED="no_id")
    with _FakeServer(tmp_path, config) as server:
        with pytest.raises(AgentInvocationError):
            server.run_agent(agent="loop-planner", directory=tmp_path, prompt="go")


def test_create_session_rejects_non_string_id(tmp_path):
    config = _argv_config(
        FAKE_OPENCODE_MODE="normal", FAKE_OPENCODE_SESSION_MALFORMED="non_string_id"
    )
    with _FakeServer(tmp_path, config) as server:
        with pytest.raises(AgentInvocationError):
            server.run_agent(agent="loop-planner", directory=tmp_path, prompt="go")


def test_send_prompt_rejects_non_json_body(tmp_path):
    config = _argv_config(FAKE_OPENCODE_MODE="normal", FAKE_OPENCODE_MESSAGE_MALFORMED="not_json")
    with _FakeServer(tmp_path, config) as server:
        with pytest.raises(AgentInvocationError):
            server.run_agent(agent="loop-planner", directory=tmp_path, prompt="go")


def test_send_prompt_rejects_array_body(tmp_path):
    config = _argv_config(FAKE_OPENCODE_MODE="normal", FAKE_OPENCODE_MESSAGE_MALFORMED="array")
    with _FakeServer(tmp_path, config) as server:
        with pytest.raises(AgentInvocationError):
            server.run_agent(agent="loop-planner", directory=tmp_path, prompt="go")


def test_send_prompt_rejects_non_object_info(tmp_path):
    config = _argv_config(
        FAKE_OPENCODE_MODE="normal", FAKE_OPENCODE_MESSAGE_MALFORMED="info_not_object"
    )
    with _FakeServer(tmp_path, config) as server:
        with pytest.raises(AgentInvocationError):
            server.run_agent(agent="loop-planner", directory=tmp_path, prompt="go")


def test_send_prompt_rejects_non_list_parts(tmp_path):
    config = _argv_config(
        FAKE_OPENCODE_MODE="normal", FAKE_OPENCODE_MESSAGE_MALFORMED="parts_not_list"
    )
    with _FakeServer(tmp_path, config) as server:
        with pytest.raises(AgentInvocationError):
            server.run_agent(agent="loop-planner", directory=tmp_path, prompt="go")


# -- timeout precedence over abort failure ------------------------------------


def test_timeout_precedence_over_abort_failure(tmp_path):
    """If abort_session() fails after a prompt timeout, the caller must
    still see the original PhaseTimeoutError, not whatever exception the
    best-effort abort raised."""
    from loop_supervisor.opencode import PhaseTimeoutError

    config = _argv_config(
        FAKE_OPENCODE_MODE="normal",
        FAKE_OPENCODE_MESSAGE_BLOCK_SECONDS="5",
        FAKE_OPENCODE_ABORT_FAIL="1",
    )
    with _FakeServer(tmp_path, config) as server:
        with pytest.raises(PhaseTimeoutError):
            server.run_agent(agent="loop-planner", directory=tmp_path, prompt="go", timeout=1.0)


def test_abort_session_uses_bounded_timeout_not_hang(tmp_path):
    """abort_session() must return within roughly _ABORT_TIMEOUT_SECONDS
    even if the server hangs on the abort request, proving it uses a
    dedicated bounded client rather than the shared timeout=None control
    client."""
    import time

    from loop_supervisor.opencode import _ABORT_TIMEOUT_SECONDS

    config = _argv_config(
        FAKE_OPENCODE_MODE="normal",
        FAKE_OPENCODE_ABORT_BLOCK_SECONDS=str(_ABORT_TIMEOUT_SECONDS + 10),
    )
    from loop_supervisor.opencode import OpenCodeError

    with _FakeServer(tmp_path, config) as server:
        session_id = server.create_session(tmp_path, title="t")
        start = time.monotonic()
        with pytest.raises(OpenCodeError):
            server.abort_session(session_id)
        elapsed = time.monotonic() - start
        assert elapsed < _ABORT_TIMEOUT_SECONDS + 5.0


def test_abort_session_does_not_use_shared_control_client(tmp_path):
    """abort_session() must issue its request through a freshly created
    httpx.Client, never the long-lived shared control client (whose
    default timeout=None would make abort cleanup unbounded)."""
    import httpx

    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    with _FakeServer(tmp_path, config) as server:
        session_id = server.create_session(tmp_path, title="t")

        shared_client_used = {"v": False}
        original_post = server.client.post

        def _spy_post(*args, **kwargs):
            shared_client_used["v"] = True
            return original_post(*args, **kwargs)

        server.client.post = _spy_post  # type: ignore[method-assign]

        real_client_init = httpx.Client.__init__
        created_clients = []

        def _spy_init(self, *args, **kwargs):
            created_clients.append(kwargs.get("timeout"))
            return real_client_init(self, *args, **kwargs)

        import unittest.mock as mock

        with mock.patch.object(httpx.Client, "__init__", _spy_init):
            server.abort_session(session_id)

        assert shared_client_used["v"] is False
        # A dedicated client was created with a finite timeout, not
        # timeout=None (the shared control client's default).
        assert any(t is not None for t in created_clients)


def test_abort_active_sessions_continues_after_one_session_fails(tmp_path):
    """If aborting one session fails (network error, non-2xx, or a
    timeout), abort_active_sessions() must still attempt the remaining
    sessions rather than stopping early."""
    config = _argv_config(FAKE_OPENCODE_MODE="normal", FAKE_OPENCODE_ABORT_FAIL="1")
    with _FakeServer(tmp_path, config) as server:
        s1 = server.create_session(tmp_path, title="one")
        s2 = server.create_session(tmp_path, title="two")
        with server._active_sessions_lock:
            import time as _time

            from loop_supervisor.opencode import InvocationRef

            server._active_sessions[s1] = InvocationRef(
                session_id=s1, agent="a", directory=tmp_path, started_monotonic=_time.monotonic()
            )
            server._active_sessions[s2] = InvocationRef(
                session_id=s2, agent="a", directory=tmp_path, started_monotonic=_time.monotonic()
            )

        # Must not raise even though the fake server returns 500 for
        # every abort call.
        server.abort_active_sessions()


def test_observer_receives_the_original_timeout_error(tmp_path):
    """The observer's invocation_finished() must receive the exact same
    PhaseTimeoutError instance the caller sees, even when a subsequent
    best-effort abort fails."""
    from loop_supervisor.opencode import InvocationRef, PhaseTimeoutError

    received: list[BaseException | None] = []

    class _Observer:
        def invocation_started(self, invocation: InvocationRef) -> None:
            pass

        def invocation_finished(self, invocation, error) -> None:
            received.append(error)

    config = _argv_config(
        FAKE_OPENCODE_MODE="normal",
        FAKE_OPENCODE_MESSAGE_BLOCK_SECONDS="5",
        FAKE_OPENCODE_ABORT_FAIL="1",
    )
    with _FakeServer(tmp_path, config) as server:
        server.add_observer(_Observer())
        caught: PhaseTimeoutError | None = None
        try:
            server.run_agent(agent="loop-planner", directory=tmp_path, prompt="go", timeout=1.0)
        except PhaseTimeoutError as exc:
            caught = exc

    assert caught is not None
    assert len(received) == 1
    assert received[0] is caught


# -- bounded, precedence-safe client.close() ---------------------------------


def _patch_close_for_finite_timeout_clients(monkeypatch, replacement):
    """Patch httpx.Client.close so `replacement` only applies to
    request-local clients (finite timeout), leaving the shared
    long-lived control client (timeout=None) closing normally. Without
    this, a blanket Client.close() patch would also break the fake
    server's own __exit__/stop() teardown at the end of the `with`
    block, unrelated to what the test is exercising."""
    import httpx

    real_close = httpx.Client.close

    def _dispatch(self):
        if self.timeout.read is None:
            return real_close(self)
        return replacement(self)

    monkeypatch.setattr(httpx.Client, "close", _dispatch)


def test_prompt_timeout_survives_throwing_close(tmp_path, monkeypatch):
    """A prompt timeout must be what's raised even if the request-local
    client's close() raises afterward."""
    from loop_supervisor.opencode import PhaseTimeoutError

    def _boom_close(self):
        raise RuntimeError("simulated close failure")

    _patch_close_for_finite_timeout_clients(monkeypatch, _boom_close)

    config = _argv_config(
        FAKE_OPENCODE_MODE="normal",
        FAKE_OPENCODE_MESSAGE_BLOCK_SECONDS="5",
    )
    with _FakeServer(tmp_path, config) as server:
        session_id = server.create_session(tmp_path, title="t")
        with pytest.raises(PhaseTimeoutError):
            server.send_prompt(
                session_id=session_id, directory=tmp_path, agent="a", prompt="go", timeout=1.0
            )


def test_prompt_timeout_survives_hanging_close(tmp_path, monkeypatch):
    """A prompt timeout must be returned within a bounded time even if
    the request-local client's close() hangs indefinitely afterward."""
    import httpx

    import loop_supervisor.opencode as oc_module
    from loop_supervisor.opencode import PhaseTimeoutError

    monkeypatch.setattr(oc_module, "_CLIENT_CLOSE_TIMEOUT_SECONDS", 0.1)

    release = threading.Event()
    entered = threading.Event()
    real_close = httpx.Client.close

    def _hanging_close(self):
        entered.set()
        release.wait(timeout=10)
        return real_close(self)

    _patch_close_for_finite_timeout_clients(monkeypatch, _hanging_close)

    config = _argv_config(
        FAKE_OPENCODE_MODE="normal",
        FAKE_OPENCODE_MESSAGE_BLOCK_SECONDS="5",
    )
    try:
        with _FakeServer(tmp_path, config) as server:
            session_id = server.create_session(tmp_path, title="t")
            start = time.monotonic()
            with pytest.raises(PhaseTimeoutError):
                server.send_prompt(
                    session_id=session_id,
                    directory=tmp_path,
                    agent="a",
                    prompt="go",
                    timeout=1.0,
                )
            elapsed = time.monotonic() - start
            assert elapsed < 3.0
            assert entered.is_set()
    finally:
        release.set()


def test_session_creation_timeout_survives_throwing_close(tmp_path, monkeypatch):
    """A session-creation timeout must be what's raised even if the
    request-local client's close() raises afterward."""
    from loop_supervisor.opencode import PhaseTimeoutError

    def _boom_close(self):
        raise RuntimeError("simulated close failure")

    _patch_close_for_finite_timeout_clients(monkeypatch, _boom_close)

    config = _argv_config(
        FAKE_OPENCODE_MODE="normal",
        FAKE_OPENCODE_SESSION_BLOCK_SECONDS="5",
    )
    with _FakeServer(tmp_path, config) as server:
        with pytest.raises(PhaseTimeoutError):
            server.create_session(tmp_path, title="t", timeout=1.0)
        assert server.active_invocations() == []


def test_run_agent_still_aborts_and_reports_exact_timeout_despite_close_failure(
    tmp_path, monkeypatch
):
    """run_agent() must still perform its best-effort abort and deliver
    the exact original PhaseTimeoutError to both the caller and the
    observer, even when send_prompt's client.close() raises. Session
    creation's own close() must succeed normally so only send_prompt's
    close is under test."""
    from loop_supervisor.opencode import InvocationRef, PhaseTimeoutError

    call_count = {"n": 0}

    def _boom_after_first_close(self):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return  # session creation's close succeeds normally
        raise RuntimeError("simulated close failure")

    _patch_close_for_finite_timeout_clients(monkeypatch, _boom_after_first_close)

    received: list[BaseException | None] = []

    class _Observer:
        def invocation_started(self, invocation: InvocationRef) -> None:
            pass

        def invocation_finished(self, invocation, error) -> None:
            received.append(error)

    config = _argv_config(
        FAKE_OPENCODE_MODE="normal",
        FAKE_OPENCODE_MESSAGE_BLOCK_SECONDS="5",
    )
    with _FakeServer(tmp_path, config) as server:
        server.add_observer(_Observer())
        caught: PhaseTimeoutError | None = None
        try:
            server.run_agent(agent="loop-planner", directory=tmp_path, prompt="go", timeout=1.0)
        except PhaseTimeoutError as exc:
            caught = exc

    assert caught is not None
    assert server.active_invocations() == []
    assert len(received) == 1
    assert received[0] is caught


def test_abort_survives_throwing_close(tmp_path, monkeypatch):
    """abort_session()'s own request-level timeout/error must be what's
    raised even if its request-local client's close() also raises."""
    import httpx

    from loop_supervisor.opencode import _ABORT_TIMEOUT_SECONDS, OpenCodeError

    real_close = httpx.Client.close
    call_count = {"n": 0}

    def _selective_boom_close(self):
        call_count["n"] += 1
        # Only the abort client (bound to the abort timeout) should
        # raise; let ordinary request-local clients close normally so
        # session creation above isn't affected.
        if self.timeout.read == _ABORT_TIMEOUT_SECONDS:
            raise RuntimeError("simulated abort-client close failure")
        return real_close(self)

    monkeypatch.setattr(httpx.Client, "close", _selective_boom_close)

    config = _argv_config(
        FAKE_OPENCODE_MODE="normal",
        FAKE_OPENCODE_ABORT_BLOCK_SECONDS=str(_ABORT_TIMEOUT_SECONDS + 10),
    )
    with _FakeServer(tmp_path, config) as server:
        session_id = server.create_session(tmp_path, title="t")
        with pytest.raises(OpenCodeError) as excinfo:
            server.abort_session(session_id)
        assert "did not respond within" in str(excinfo.value)


def test_hanging_abort_close_is_wall_clock_bounded(tmp_path, monkeypatch):
    """A successful abort response whose client.close() hangs must still
    return (raising OpenCodeCleanupError) within the close bound, not
    hang indefinitely."""
    import httpx

    import loop_supervisor.opencode as oc_module
    from loop_supervisor.opencode import OpenCodeCleanupError

    monkeypatch.setattr(oc_module, "_CLIENT_CLOSE_TIMEOUT_SECONDS", 0.1)

    release = threading.Event()
    real_close = httpx.Client.close

    def _hanging_close(self):
        release.wait(timeout=10)
        return real_close(self)

    _patch_close_for_finite_timeout_clients(monkeypatch, _hanging_close)

    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    try:
        with _FakeServer(tmp_path, config) as server:
            session_id = server.create_session(tmp_path, title="t")
            start = time.monotonic()
            with pytest.raises(OpenCodeCleanupError):
                server.abort_session(session_id)
            elapsed = time.monotonic() - start
            assert elapsed < 3.0
    finally:
        release.set()


def test_successful_prompt_with_close_failure_raises_cleanup_error(tmp_path, monkeypatch):
    """A successful prompt response whose client.close() raises must
    surface as OpenCodeCleanupError, not silently return the result with
    unconfirmed cleanup."""
    from loop_supervisor.opencode import OpenCodeCleanupError

    def _boom_close(self):
        raise RuntimeError("simulated close failure")

    _patch_close_for_finite_timeout_clients(monkeypatch, _boom_close)

    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    with _FakeServer(tmp_path, config) as server:
        session_id = server.create_session(tmp_path, title="t")
        with pytest.raises(OpenCodeCleanupError):
            server.send_prompt(
                session_id=session_id, directory=tmp_path, agent="a", prompt="go", timeout=5.0
            )


def test_stop_continues_after_hanging_shared_client_close(tmp_path, monkeypatch):
    """stop() must still terminate the process and stdout pump even if
    the shared control client's close() hangs past the close bound, and
    must report OpenCodeCleanupError while retaining the client for
    retry."""
    import httpx

    import loop_supervisor.opencode as oc_module

    monkeypatch.setattr(oc_module, "_CLIENT_CLOSE_TIMEOUT_SECONDS", 0.1)

    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    server = _FakeServer(tmp_path, config)
    server.start()
    owner = server._owner
    assert owner is not None
    launcher = owner.launcher

    release = threading.Event()
    real_close = httpx.Client.close

    def _hanging_close(self):
        release.wait(timeout=10)
        return real_close(self)

    monkeypatch.setattr(type(server._client), "close", _hanging_close)

    try:
        with pytest.raises((OpenCodeCleanupError, ExceptionGroup)):
            server.stop()
        # Process termination and stdout cleanup must still have happened
        # despite the hung client close.
        assert launcher.poll() is not None
        assert server._owner is None
        # The client is retained because its close was never confirmed.
        assert server._client is not None
    finally:
        release.set()

    # Once the background close finally finishes, a retried stop() must
    # observe (not restart) it and complete cleanly.
    for _ in range(100):
        if server._client is None:
            break
        try:
            server.stop()
        except Exception:
            pass
        time.sleep(0.05)
    assert server._client is None


def test_retried_stop_does_not_invoke_concurrent_close(tmp_path, monkeypatch):
    """A stop() retried while a prior bounded close is still running must
    not start a second concurrent close() call against the same client."""
    import httpx

    import loop_supervisor.opencode as oc_module
    from loop_supervisor.opencode import OpenCodeCleanupError

    monkeypatch.setattr(oc_module, "_CLIENT_CLOSE_TIMEOUT_SECONDS", 0.1)

    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    server = _FakeServer(tmp_path, config)
    server.start()

    release = threading.Event()
    call_count = {"n": 0}
    real_close = httpx.Client.close

    def _hanging_close(self):
        call_count["n"] += 1
        release.wait(timeout=10)
        return real_close(self)

    monkeypatch.setattr(type(server._client), "close", _hanging_close)

    try:
        with pytest.raises((OpenCodeCleanupError, ExceptionGroup)):
            server.stop()
        with pytest.raises((OpenCodeCleanupError, ExceptionGroup)):
            server.stop()
        assert call_count["n"] == 1
    finally:
        release.set()

    for _ in range(100):
        if server._client is None:
            break
        try:
            server.stop()
        except Exception:
            pass
        time.sleep(0.05)
    assert server._client is None
    assert call_count["n"] == 1


# -- new audit-required tests: lifecycle correctness -------------------------


def test_launcher_stays_alive_after_opencode_child_exits(tmp_path):
    config = _argv_config(FAKE_OPENCODE_MODE="exit_after_ready")
    server = _FakeServer(tmp_path, config)
    server.start()
    owner = server._owner
    assert owner is not None

    try:
        deadline = time.monotonic() + 5.0
        child_exit_seen = False
        while time.monotonic() < deadline:
            event = owner.read_event(0.1)
            if event is not None and event.startswith("child-exit:"):
                child_exit_seen = True
                break
        assert child_exit_seen
        assert owner.launcher.poll() is None
        assert os.getpgid(owner.launcher.pid) == owner.pgid
    finally:
        server.stop()

    assert server._owner is None
    assert owner.launcher.poll() == -signal.SIGKILL


def test_stop_attaches_cleanup_note_to_startup_failure(tmp_path, monkeypatch):
    """When start() fails and the internal launcher cleanup also fails,
    the primary ServerStartupError must have a note describing the cleanup
    failure (per ADR 0009) rather than silently swallowing it."""
    import subprocess as _subprocess

    config = _argv_config(FAKE_OPENCODE_MODE="never_ready")
    config.startup_timeout = 1.0
    server = _FakeServer(tmp_path, config)

    def _boom_wait(self, timeout=None):
        raise RuntimeError("simulated wait failure during cleanup")

    monkeypatch.setattr(_subprocess.Popen, "wait", _boom_wait)

    with pytest.raises(ServerStartupError) as excinfo:
        server.start()

    exc = excinfo.value
    notes = getattr(exc, "__notes__", [])
    # The cleanup failure must be attached as a note, not silently swallowed.
    assert any("cleanup" in n or "wait" in n.lower() or "failed" in n.lower() for n in notes), (
        f"Expected cleanup note on {exc!r}; got notes={notes!r}"
    )


def test_concurrent_stop_calls_are_serialized(tmp_path):
    """Two concurrent stop() calls must not both attempt to kill the group;
    the cleanup lock must serialize them so only one issues the kill command
    and the second observes the already-reaped owner."""
    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    server = _FakeServer(tmp_path, config)
    server.start()

    errors: list[Exception] = []
    results: list[str] = []

    def _stop():
        try:
            server.stop()
            results.append("ok")
        except Exception as e:
            errors.append(e)
            results.append("err")

    t1 = threading.Thread(target=_stop)
    t2 = threading.Thread(target=_stop)
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    # Both must finish.
    assert len(results) == 2, f"not all stop() calls finished: {results}"
    # No unhandled exceptions (OpenCodeCleanupError is acceptable).
    for e in errors:
        assert isinstance(e, (OpenCodeCleanupError, ExceptionGroup)), (
            f"unexpected exception type: {e!r}"
        )
    assert server._owner is None


def test_start_rejected_while_owner_unresolved(tmp_path, monkeypatch):
    """start() must be rejected if a prior lifecycle resource is still
    unresolved (e.g. _owner is not None from a previous failed stop())."""
    from loop_supervisor.opencode import OpenCodeError, _ProcessOwner

    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    server = _FakeServer(tmp_path, config)
    server.start()
    owner = server._owner
    assert owner is not None

    original = _ProcessOwner.shutdown_confirmed
    monkeypatch.setattr(_ProcessOwner, "shutdown_confirmed", lambda self: False)
    with pytest.raises((OpenCodeCleanupError, ExceptionGroup)):
        server.stop()

    assert server._owner is not None
    with pytest.raises(OpenCodeError, match="unresolved"):
        server.start()

    monkeypatch.setattr(_ProcessOwner, "shutdown_confirmed", original)
    server.stop()


def test_stop_after_start_stop_start_succeeds(tmp_path):
    """A full stop()+start() cycle on the same instance must work correctly:
    readiness events must be reset so the second start() does not falsely
    see the first run's ready line."""
    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    server = _FakeServer(tmp_path, config)
    server.start()
    health1 = server.health()
    assert health1["healthy"] is True
    server.stop()
    assert server._owner is None

    server.start()
    health2 = server.health()
    assert health2["healthy"] is True
    server.stop()
    assert server._owner is None


def test_launcher_kill_permission_error_retains_owner(tmp_path, monkeypatch):
    from loop_supervisor.opencode import _ProcessOwner

    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    server = _FakeServer(tmp_path, config)
    server.start()
    owner = server._owner
    assert owner is not None
    original = _ProcessOwner.send

    def _eperm(self, command):
        if command == "kill":
            raise PermissionError("simulated EPERM")
        return original(self, command)

    monkeypatch.setattr(_ProcessOwner, "send", _eperm)
    with pytest.raises((OpenCodeCleanupError, ExceptionGroup)):
        server.stop()
    assert server._owner is owner

    monkeypatch.setattr(_ProcessOwner, "send", original)
    server.stop()
    assert server._owner is None


def test_leak_safe_finally_on_descendant_assertion_failure(tmp_path):
    """Even if an assertion inside the test body fails, server.stop() must
    still be attempted so the launcher and descendants are not leaked."""
    pid_file = tmp_path / "descendant.pid"
    config = _argv_config(
        FAKE_OPENCODE_MODE="normal",
        FAKE_OPENCODE_DESCENDANT_PID_FILE=str(pid_file),
    )
    server = _FakeServer(tmp_path, config)
    server.start()
    launcher_pid = server._owner.launcher.pid
    descendant_pid: int | None = None
    try:
        descendant_pid = _wait_for_pid_file(pid_file)
    finally:
        server.stop()

    assert server._owner is None
    assert owner_launcher_reaped(server, launcher_pid)
    if descendant_pid is not None:
        time.sleep(0.1)
        assert not _pid_is_alive(descendant_pid)


def owner_launcher_reaped(server: OpenCodeServer, launcher_pid: int) -> bool:
    """True if the launcher with launcher_pid is no longer alive."""
    time.sleep(0.05)
    return not _pid_is_alive(launcher_pid)


def test_startup_failure_does_not_signal_unverified_pgid(tmp_path, monkeypatch):
    """If identity verification fails, the cleanup path must use Popen.kill()
    only — it must NOT call killpg() with a non-zero (kill/term) signal on an
    unverified PGID, which could hit an unrelated process group if reused."""
    import loop_supervisor.opencode as oc_module

    kill_signals: list[tuple[int, int]] = []
    original_killpg = os.killpg

    def _spy_killpg(pgid: int, sig: int) -> None:
        if sig != 0:
            kill_signals.append((pgid, sig))
        return original_killpg(pgid, sig)

    monkeypatch.setattr(oc_module.os, "killpg", _spy_killpg)

    config = _argv_config(FAKE_OPENCODE_MODE="exit_early")
    server = _FakeServer(tmp_path, config)

    with pytest.raises(ServerStartupError):
        server.start()

    assert kill_signals == [], (
        f"killpg() with a kill/term signal should not be called during "
        f"identity-failure cleanup, but was called with: {kill_signals}"
    )


def test_concurrent_starts_create_only_one_owner(tmp_path):
    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    server = _FakeServer(tmp_path, config)
    barrier = threading.Barrier(3)
    results: list[str] = []

    def _start():
        barrier.wait()
        try:
            server.start()
            results.append("started")
        except OpenCodeError:
            results.append("rejected")

    threads = [threading.Thread(target=_start) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=20)
    try:
        assert sorted(results) == ["rejected", "started"]
        assert server._owner is not None
    finally:
        server.stop()


def test_concurrent_start_and_stop_cannot_resurrect_client(tmp_path, monkeypatch):
    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    server = _FakeServer(tmp_path, config)
    entered = threading.Event()
    release = threading.Event()
    original = OpenCodeServer._await_ready

    def _blocked(self, launcher):
        entered.set()
        release.wait(timeout=10)
        return original(self, launcher)

    monkeypatch.setattr(OpenCodeServer, "_await_ready", _blocked)
    errors: list[BaseException] = []

    def _start():
        try:
            server.start()
        except BaseException as exc:
            errors.append(exc)

    starter = threading.Thread(target=_start)
    starter.start()
    assert entered.wait(timeout=10)
    stopper = threading.Thread(target=server.stop)
    stopper.start()
    time.sleep(0.1)
    assert stopper.is_alive()
    release.set()
    starter.join(timeout=20)
    stopper.join(timeout=20)
    assert server._owner is None
    assert server._client is None
    assert not starter.is_alive()
    assert not stopper.is_alive()


def test_production_launcher_uses_python_and_preserves_executable(tmp_path, monkeypatch):
    config = OpenCodeServerConfig(executable="custom-opencode")
    server = OpenCodeServer(tmp_path, config)
    captured: list[str] = []

    class _Stop(Exception):
        pass

    def _capture(args, **kwargs):
        captured.extend(args)
        raise _Stop

    monkeypatch.setattr("loop_supervisor.opencode.subprocess.Popen", _capture)
    with pytest.raises(_Stop):
        server.start()
    assert captured[0] == sys.executable
    assert captured[1].endswith("_launcher.py")
    assert "custom-opencode" in captured[4:]


def test_no_parent_pgid_probe_after_anchor_reaped(tmp_path, monkeypatch):
    import loop_supervisor.opencode as oc_module

    calls: list[tuple[int, int]] = []
    real = os.killpg

    def _spy(pgid, sig):
        calls.append((pgid, sig))
        return real(pgid, sig)

    monkeypatch.setattr(oc_module.os, "killpg", _spy)
    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    server = _FakeServer(tmp_path, config)
    server.start()
    server.stop()
    assert calls == []


def test_stop_retries_term_after_first_command_write_failure(tmp_path, monkeypatch):
    from loop_supervisor.opencode import _ProcessOwner

    config = _argv_config(FAKE_OPENCODE_MODE="normal")
    server = _FakeServer(tmp_path, config)
    server.start()
    owner = server._owner
    assert owner is not None
    original = _ProcessOwner.send
    failed = {"value": False}

    def _fail_first_term(self, command):
        if command == "term" and not failed["value"]:
            failed["value"] = True
            raise OSError("simulated TERM command write failure")
        return original(self, command)

    monkeypatch.setattr(_ProcessOwner, "send", _fail_first_term)
    try:
        with pytest.raises((OpenCodeCleanupError, ExceptionGroup)):
            server.stop()
        assert server._owner is owner
        assert owner.term_sent is False
        assert owner.kill_sent is False
        assert owner.launcher.poll() is None

        server.stop()
        assert server._owner is None
        assert owner.term_sent is True
        assert owner.kill_sent is True
    finally:
        if server._owner is not None:
            monkeypatch.setattr(_ProcessOwner, "send", original)
            server.stop()


def test_malformed_anchor_identity_retains_pending_until_reaped(tmp_path):
    config = _argv_config(
        FAKE_OPENCODE_MODE="normal",
        FAKE_LAUNCHER_IDENTITY="malformed-anchor-event",
    )
    server = _FakeServer(tmp_path, config)
    with pytest.raises(ServerStartupError, match="unexpected anchor event"):
        server.start()
    assert server._pending_launcher is None
    assert server._owner is None


def test_term_grace_waits_when_child_ignores_sigterm(tmp_path, monkeypatch):
    import loop_supervisor.opencode as oc_module

    monkeypatch.setattr(oc_module, "_GROUP_TERM_WAIT_SECONDS", 0.25)
    pid_file = tmp_path / "descendant.pid"
    config = _argv_config(
        FAKE_OPENCODE_MODE="ignore_sigterm",
        FAKE_OPENCODE_DESCENDANT_PID_FILE=str(pid_file),
        FAKE_OPENCODE_DESCENDANT_IGNORE_SIGTERM="1",
    )
    server = _FakeServer(tmp_path, config)
    server.start()
    try:
        _wait_for_pid_file(pid_file)
        started = time.monotonic()
        server.stop()
        assert time.monotonic() - started >= 0.2
    finally:
        if server._owner is not None:
            server.stop()


def test_launcher_side_term_error_is_retryable(tmp_path):
    config = _argv_config(
        FAKE_OPENCODE_MODE="normal",
        FAKE_LAUNCHER_TERM_ERROR_ONCE="1",
    )
    server = _FakeServer(tmp_path, config)
    server.start()
    owner = server._owner
    assert owner is not None
    try:
        with pytest.raises((OpenCodeCleanupError, ExceptionGroup)):
            server.stop()
        assert server._owner is owner
        assert owner.term_sent is False
        assert owner.kill_sent is False
        assert owner.launcher.poll() is None
        server.stop()
        assert server._owner is None
    finally:
        if server._owner is not None:
            server.stop()


def test_launcher_side_kill_error_is_retryable(tmp_path):
    config = _argv_config(
        FAKE_OPENCODE_MODE="normal",
        FAKE_LAUNCHER_KILL_ERROR_ONCE="1",
    )
    server = _FakeServer(tmp_path, config)
    server.start()
    owner = server._owner
    assert owner is not None
    try:
        with pytest.raises((OpenCodeCleanupError, ExceptionGroup)):
            server.stop()
        assert server._owner is owner
        assert owner.kill_sent is False
        assert owner.launcher.poll() is None
        server.stop()
        assert server._owner is None
    finally:
        if server._owner is not None:
            server.stop()
