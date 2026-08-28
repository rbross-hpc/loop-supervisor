"""Headless auto-deny listener for OpenCode permission requests.

``Permission.evaluate`` in the OpenCode server falls back to ``ask`` for
any permission key/pattern combination that no configured rule matches
(this is a hard-coded ``??`` fallback, not something ``opencode.json``
can eliminate for every possible key). The headless supervisor path has
no consumer for the resulting ``permission.asked`` SSE event and no
permission-response channel: SSE is otherwise TUI-only. Without a
reply, the blocked prompt sits until ``send_prompt``'s ``timeout``
(default 1800s) fires ``PhaseTimeoutError`` — a long, silent stall.

This module closes that gap the same way OpenCode's own client can:
subscribing to ``GET /global/event`` and replying to every
``permission.asked`` event. Unlike the client's own ``mode: "auto"``
(which replies ``"once"``, approving), this always replies
``"reject"`` — the supervisor's fail-loud posture prefers an immediate,
diagnosable denial over silently granting an unreviewed privilege.

Reply contract, verified against the OpenCode 1.18.22 server binary:

- Event: ``permission.asked``, with ``properties`` matching
  ``PermissionRequest`` (``id``, ``sessionID``, ``permission``,
  ``patterns``, ...).
- Route: ``POST /permission/{requestID}/reply`` (the deprecated
  ``/session/{sessionID}/permissions/{permissionID}`` alias is not
  used).
- Body: ``{"reply": "once" | "always" | "reject", "message": str?}``.

Failure handling mirrors ``sse.py``'s own contract: a fault here must
never fail a run. Every reply attempt is wrapped and swallowed; the
denial count/summary is purely an in-memory diagnostic aid (see
``RunSession.denied_permission_count`` / ``denied_permission_summary``)
consumed by the CLI, not persisted to ``RunState`` (see the backlog
item on squashing schema migrations for why persistence was deferred).
"""

from __future__ import annotations

import sys
import threading
from typing import Any

import httpx

from .opencode_events import OpenCodeEventError, normalize_global_event
from .sse import SSEClient

_REPLY_TIMEOUT_SECONDS = 5.0


class PermissionDenier:
    """Auto-replies "reject" to every ``permission.asked`` event.

    Owns an :class:`SSEClient` subscribed to ``GET /global/event`` on
    the given OpenCode server. Call :meth:`start` once the server is
    ready and :meth:`stop` during teardown, before the server itself is
    stopped.
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._lock = threading.Lock()
        self._denied_count = 0
        self._denied_summary: list[str] = []
        self._sse = SSEClient(
            base_url,
            on_event=self._on_event,
            on_notice=self._on_notice,
        )

    def start(self) -> None:
        self._sse.start()

    def stop(self) -> None:
        """Stop the underlying SSE subscription.

        May raise ``SSECleanupError`` if the worker thread could not be
        confirmed stopped within the bounded join timeout (see
        ``SSEClient.stop``); callers already treat that as best-effort
        cleanup, matching every other SSE/session teardown path.
        """
        self._sse.stop()

    @property
    def denied_count(self) -> int:
        with self._lock:
            return self._denied_count

    @property
    def denied_summary(self) -> list[str]:
        """Distinct ``permission`` keys denied so far, in first-seen order."""
        with self._lock:
            return list(self._denied_summary)

    def _on_event(self, raw_event: dict[str, Any]) -> None:
        try:
            event = normalize_global_event(raw_event)
        except OpenCodeEventError:
            return
        if event.type != "permission.asked":
            return

        payload = raw_event.get("payload")
        properties = payload.get("properties") if isinstance(payload, dict) else None
        if not isinstance(properties, dict):
            return
        request_id = properties.get("id")
        if not isinstance(request_id, str) or not request_id:
            return
        permission = properties.get("permission")
        permission_key = permission if isinstance(permission, str) else "<unknown>"

        self._reply_reject(request_id)

        with self._lock:
            self._denied_count += 1
            if permission_key not in self._denied_summary:
                self._denied_summary.append(permission_key)

        print(
            f"loop-supervisor: denied permission request {request_id!r} ({permission_key!r})",
            file=sys.stderr,
        )

    def _reply_reject(self, request_id: str) -> None:
        try:
            client = httpx.Client(base_url=self._base_url, timeout=_REPLY_TIMEOUT_SECONDS)
        except Exception:
            return
        try:
            client.post(
                f"/permission/{request_id}/reply",
                json={"reply": "reject"},
            )
        except Exception:
            pass
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _on_notice(self, _notice: str) -> None:
        pass
