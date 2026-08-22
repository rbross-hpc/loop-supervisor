"""Server-Sent Events transport for OpenCode live telemetry.

Subscribes to ``GET /global/event`` (not ``/event``) because planner,
architect, builder, and auditor sessions may run in different worktree
directories. ``/global/event`` covers all active sessions on the server.

SSE failure is strictly non-fatal: it never modifies ``RunState`` and
never fails a supervisor run. The blocking prompt response in
``opencode.py`` remains the authoritative source for role completion and
structured output.

Every reconnect is treated as potentially lossy because the global SSE
stream has no replay cursor.

Shutdown behaviour
------------------
``stop()`` sets the stop event, then actively closes the current
``httpx.Response`` and client so that ``response.iter_lines()`` unblocks
immediately — it does not wait for the read timeout (which may be 60 s).

``_thread`` is cleared, and the externally visible ``STOPPED`` state is
trusted from the worker's own exit, only after the thread has actually
terminated. If the bounded join times out, the worker may still be alive:
``stop()`` then raises ``SSECleanupError`` and retains ``_thread`` so a
later ``stop()`` can retry, rather than falsely reporting ``STOPPED``.
Calling ``stop()`` while no stream is active, or calling it multiple
times, is safe.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from typing import Any

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]
        pass


import httpx


class SSECleanupError(RuntimeError):
    """Raised by stop() when the worker thread could not be confirmed
    stopped within the join timeout. Ownership of the still-live thread is
    retained so a later stop() can retry."""


class SSEConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    LIVE = "live"
    RECONNECTING = "reconnecting"
    STOPPED = "stopped"


_CRLF = b"\r\n"
_MAX_RECONNECT_DELAY = 30.0
_BASE_RECONNECT_DELAY = 1.0
_RECONNECT_BACKOFF = 2.0
_SHUTDOWN_JOIN_TIMEOUT = 5.0


def iter_sse_json(
    lines: Iterable[str],
    *,
    max_event_bytes: int = 1 << 20,
    on_notice: Callable[[str], None] | None = None,
) -> Iterable[dict[str, Any]]:
    """Pure SSE parser that yields parsed JSON objects from an event stream.

    Supports:
    - Multiline ``data:`` fields.
    - Blank-line dispatch.
    - Comment lines and heartbeats (``:``) — skipped silently.
    - CRLF line endings.
    - Malformed JSON — emits a notice via ``on_notice`` and continues.
    - Object-only JSON — non-object values are silently skipped.
    - Maximum event size — events exceeding ``max_event_bytes`` are dropped.
    - Incomplete final record (no terminating blank line) — discarded.
    """
    data_parts: list[str] = []
    byte_count = 0
    oversized = False

    for raw_line in lines:
        line = raw_line.rstrip("\r\n")

        if not line:
            if data_parts and not oversized:
                combined = "\n".join(data_parts)
                data_parts = []
                byte_count = 0
                try:
                    obj = json.loads(combined)
                except json.JSONDecodeError as exc:
                    if on_notice:
                        on_notice(f"SSE malformed JSON: {exc}")
                    continue
                if isinstance(obj, dict):
                    yield obj
            else:
                data_parts = []
                byte_count = 0
                oversized = False
            continue

        if line.startswith(":"):
            continue

        if line.startswith("data:"):
            if oversized:
                continue
            payload = line[5:].lstrip(" ")
            byte_count += len(payload.encode())
            if byte_count > max_event_bytes:
                if on_notice:
                    on_notice(f"SSE event exceeded {max_event_bytes} bytes; dropping")
                data_parts = []
                byte_count = 0
                oversized = True
            else:
                data_parts.append(payload)


class SSEClient:
    """Long-running SSE subscription to ``GET /global/event``.

    Runs in a daemon thread. Calls ``on_event`` for each received JSON
    object and ``on_state_change`` when the connection state changes.
    Reconnects automatically with capped exponential backoff.

    Call ``stop()`` to request shutdown. The active stream is closed
    immediately so the thread exits promptly without waiting for the
    read timeout.
    """

    def __init__(
        self,
        base_url: str,
        *,
        on_event: Callable[[dict[str, Any]], None],
        on_state_change: Callable[[SSEConnectionState, str], None] | None = None,
        on_notice: Callable[[str], None] | None = None,
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._on_event = on_event
        self._on_state_change = on_state_change
        self._on_notice = on_notice
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._lifecycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = SSEConnectionState.DISCONNECTED

        self._active_lock = threading.Lock()
        self._active_response: httpx.Response | None = None
        self._active_client: httpx.Client | None = None

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None:
                return
            self._stop_event.clear()
            t = threading.Thread(
                target=self._run_loop,
                name="sse-client",
                daemon=True,
            )
            self._thread = t
            t.start()

    def stop(self) -> None:
        """Stop the SSE worker, preserving ownership on an unclean stop.

        Sets the stop event and closes the active response/client so a
        blocked ``iter_lines()`` unblocks immediately rather than waiting
        for the read timeout. Then joins the worker for a bounded time.

        Ownership-preserving: ``_thread`` is cleared, and the externally
        visible ``STOPPED`` state is trusted from the worker's own exit,
        only when the worker is confirmed dead. If the bounded join times
        out, the worker may still be alive, so ``stop()`` raises
        ``SSECleanupError`` and retains ``_thread`` (a later ``stop()`` can
        retry) instead of falsely reporting STOPPED. Repeated close of an
        already-published resource is closed by the worker itself if it
        observes the stop request after publishing.
        """
        with self._lifecycle_lock:
            self._stop_event.set()

            with self._active_lock:
                response = self._active_response
                client = self._active_client

            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

            thread = self._thread
            if thread is None:
                # No worker was ever started (or a prior stop already
                # confirmed termination). Safe to publish STOPPED directly.
                self._set_state(SSEConnectionState.STOPPED, "stopped by caller")
                return

            thread.join(timeout=_SHUTDOWN_JOIN_TIMEOUT)
            if thread.is_alive():
                raise SSECleanupError(
                    "SSE worker thread did not stop within the join timeout; retaining it for retry"
                )
            self._thread = None
            # The worker's own run-loop exit publishes STOPPED; do not
            # override it here on the false assumption of success.

    def _set_state(self, state: SSEConnectionState, reason: str) -> None:
        self._state = state
        if self._on_state_change:
            try:
                self._on_state_change(state, reason)
            except Exception:
                pass

    def _notice(self, msg: str) -> None:
        if self._on_notice:
            try:
                self._on_notice(msg)
            except Exception:
                pass

    def _run_loop(self) -> None:
        delay = _BASE_RECONNECT_DELAY
        first = True

        while not self._stop_event.is_set():
            if not first:
                self._set_state(SSEConnectionState.RECONNECTING, f"reconnect in {delay:.1f}s")
                self._notice(
                    f"SSE disconnected; activity during disconnect may be missing. "
                    f"Reconnecting in {delay:.1f}s."
                )
                self._stop_event.wait(timeout=delay)
                delay = min(delay * _RECONNECT_BACKOFF, _MAX_RECONNECT_DELAY)
            else:
                first = False

            if self._stop_event.is_set():
                break

            self._set_state(SSEConnectionState.CONNECTING, "connecting")
            try:
                self._connect_and_stream()
                if not self._stop_event.is_set():
                    delay = _BASE_RECONNECT_DELAY
            except StopIteration:
                break
            except Exception as exc:
                if not self._stop_event.is_set():
                    self._notice(f"SSE error: {exc}")

        self._set_state(SSEConnectionState.STOPPED, "loop exited")

    def _connect_and_stream(self) -> None:
        url = f"{self._base_url}/global/event"
        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._read_timeout,
            write=10.0,
            pool=5.0,
        )
        client = httpx.Client(timeout=timeout)
        with self._active_lock:
            self._active_client = client
        # If stop() fired between the run-loop check and publishing this
        # client, close it now rather than proceeding to open a stream the
        # caller's stop() snapshot may have already missed.
        if self._stop_event.is_set():
            client.close()
            with self._active_lock:
                if self._active_client is client:
                    self._active_client = None
            raise StopIteration

        try:
            with client.stream(
                "GET",
                url,
                headers={"Accept": "text/event-stream"},
            ) as response:
                with self._active_lock:
                    self._active_response = response
                if self._stop_event.is_set():
                    raise StopIteration

                try:
                    if response.status_code >= 400:
                        self._notice(
                            f"SSE endpoint returned HTTP {response.status_code}; will retry"
                        )
                        return

                    self._set_state(SSEConnectionState.LIVE, "connected")

                    for obj in iter_sse_json(
                        response.iter_lines(),
                        on_notice=self._notice,
                    ):
                        if self._stop_event.is_set():
                            raise StopIteration

                        try:
                            self._on_event(obj)
                        except Exception as exc:
                            self._notice(f"SSE event handler error: {exc}")
                finally:
                    with self._active_lock:
                        if self._active_response is response:
                            self._active_response = None
        except (httpx.ReadError, httpx.StreamClosed, httpx.RemoteProtocolError) as exc:
            if self._stop_event.is_set():
                raise StopIteration from exc
            raise
        finally:
            with self._active_lock:
                if self._active_client is client:
                    self._active_client = None
            try:
                client.close()
            except Exception:
                pass
