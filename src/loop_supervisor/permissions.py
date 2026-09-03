"""Headless consumer of OpenCode's ``/global/event`` SSE stream.

The headless supervisor path has no TUI and therefore no consumer of the
rich event stream OpenCode publishes over SSE: no permission-response
channel, no live-activity display. ``SessionMonitor`` is the single
headless subscriber to ``GET /global/event`` (mirroring the TUI's own
subscription, but for a different purpose); it fans every normalized
event out to a list of attached consumers rather than hard-coding any one
behavior, so multiple independent concerns (auto-denying permission asks,
verbose event-timing diagnostics, and potentially more later) can observe
the same stream without each opening its own connection.

Consumer contract: every consumer's ``on_event`` is called for every
normalized event, in attachment order. A consumer that raises produces a
notice (see ``_on_notice``) but never breaks another consumer's turn and
never fails the run — this mirrors ``sse.py``'s own "SSE failure is
strictly non-fatal" contract, which ``SessionMonitor`` itself builds on.
This module performs no control action of its own (nothing here aborts,
retries, or bounds a session); consumers are strictly observers today.

``PermissionPolicy`` is the first consumer, carried over unchanged from
this module's original single-purpose form: it closes the headless
permission-ask gap by auto-replying "reject" to every ``permission.asked``
event over ``GET /global/event``.

``Permission.evaluate`` in the OpenCode server falls back to ``ask`` for
any permission key/pattern combination that no configured rule matches
(this is a hard-coded ``??`` fallback, not something ``opencode.json``
can eliminate for every possible key). Without a reply, the blocked
prompt sits until ``send_prompt``'s ``timeout`` (default 1800s) fires
``PhaseTimeoutError`` — a long, silent stall. Unlike the OpenCode client's
own ``mode: "auto"`` (which replies ``"once"``, approving), this always
replies ``"reject"`` — the supervisor's fail-loud posture prefers an
immediate, diagnosable denial over silently granting an unreviewed
privilege.

Reply contract, verified against the OpenCode 1.18.22 server binary:

- Event: ``permission.asked``, with ``properties`` matching
  ``PermissionRequest`` (``id``, ``sessionID``, ``permission``,
  ``patterns``, ...).
- Route: ``POST /permission/{requestID}/reply`` (the deprecated
  ``/session/{sessionID}/permissions/{permissionID}`` alias is not
  used), with the ask's own ``directory`` (from the SSE envelope) passed
  as a query parameter. This is not optional: the route is not
  implicitly scoped to the session that raised the ask, and omitting
  ``directory`` resolves the reply against the server's default/current
  instance instead of the one that actually owns the request -- see ADR
  0016 for the live-confirmed failure this caused for every ask
  originating in a task worktree (i.e. most of a real run, since the
  project-root instance is only used during planning).
- Body: ``{"reply": "once" | "always" | "reject", "message": str?}``.

Failure handling mirrors ``sse.py``'s own contract: a fault here must
never fail a run. Every reply attempt is wrapped and swallowed; the
denial count/summary is purely an in-memory diagnostic aid (see
``RunSession.denied_permission_count`` / ``denied_permission_summary``)
consumed by the CLI, not persisted to ``RunState`` (see the backlog
item on squashing schema migrations for why persistence was deferred).

Never failing a run is not the same as never being observable. A
denial is only counted/reported if the server actually accepted the
reject (2xx); a non-2xx or transport failure prints a distinct
"failed to deny" warning instead, since an unconfirmed reject leaves
the agent still blocked, which the run-outcome-safe swallow above
must not hide. Reaching a live SSE subscription is itself reported
(one line, once, on the transition to ``SSEConnectionState.LIVE``),
and SSE-level notices (reconnects, malformed events, non-2xx stream
responses) are forwarded rather than discarded. Together these close
an observability gap where "the monitor attached and saw nothing" and
"the monitor never attached at all" were indistinguishable from the
outside -- both looked like silence.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .opencode_events import OpenCodeEvent, OpenCodeEventError, normalize_global_event
from .sse import SSEClient, SSEConnectionState

_REPLY_TIMEOUT_SECONDS = 5.0


class SessionEventConsumer(Protocol):
    """A consumer attached to a :class:`SessionMonitor`.

    ``on_event`` is called once per normalized event, in attachment
    order, for every event the ``/global/event`` stream delivers -- not
    just the ones a given consumer cares about; each consumer is
    responsible for its own filtering (see ``PermissionPolicy.on_event``
    for the canonical example). A raising consumer is caught by the
    monitor and reported as a notice; it never prevents other consumers
    from seeing the same event and never fails the run.
    """

    def on_event(self, raw_event: dict[str, Any], event: OpenCodeEvent) -> None: ...


@dataclass(frozen=True)
class _ReplyOutcome:
    """Result of one reject-reply POST attempt.

    ``detail`` is always populated, on success and failure alike, so a
    caller building a diagnostic message never needs a second branch to
    ask "why" separately from "did it work" -- the two questions were
    previously conflated into a single discarded bool, which is why the
    one confirmed live failure of this path required manually cross-
    referencing OpenCode's own log to explain at all.
    """

    accepted: bool
    detail: str


class PermissionPolicy:
    """Auto-replies "reject" to every ``permission.asked`` event.

    Attached to a :class:`SessionMonitor`; does not own any connection
    itself. Every externally observable outcome (a denial the server
    actually accepted, or a denial attempt the server rejected) is
    printed to stderr via the callback supplied at construction, so "the
    monitor attached and saw nothing to deny" and "the monitor never
    attached at all" remain distinguishable from the outside.
    """

    def __init__(self, base_url: str, *, notice: Any = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._notice = notice if notice is not None else _default_notice
        self._lock = threading.Lock()
        self._denied_count = 0
        self._denied_summary: list[str] = []

    @property
    def denied_count(self) -> int:
        with self._lock:
            return self._denied_count

    @property
    def denied_summary(self) -> list[str]:
        """Distinct ``permission`` keys denied so far, in first-seen order."""
        with self._lock:
            return list(self._denied_summary)

    def on_event(self, raw_event: dict[str, Any], event: OpenCodeEvent) -> None:
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

        outcome = self._reply_reject(request_id, directory=event.directory)
        if not outcome.accepted:
            self._notice(
                f"failed to deny permission request {request_id!r} "
                f"({permission_key!r}): {outcome.detail}; the request may still be pending"
            )
            return

        with self._lock:
            self._denied_count += 1
            if permission_key not in self._denied_summary:
                self._denied_summary.append(permission_key)

        self._notice(f"denied permission request {request_id!r} ({permission_key!r})")

    def _reply_reject(self, request_id: str, *, directory: str) -> _ReplyOutcome:
        """POST the reject reply. ``accepted`` is True only if the server
        actually returned 2xx -- a denial must never be counted or
        reported unless the reject was confirmed, since an uncounted
        failure here leaves the agent still blocked on the original ask,
        which is a materially different (and worse) situation than a
        clean denial. ``detail`` always describes the outcome (the HTTP
        status on a non-2xx response, or the exception type/message on a
        client-construction or transport failure) so a caller reporting
        a failure never has to fall back on a bare "it failed" with no
        further information to act on.

        ``directory`` scopes the reply to the OpenCode instance that
        actually owns the request. ``POST /permission/{requestID}/reply``
        is not implicitly scoped to whichever session raised the ask: the
        server resolves an unscoped call against its current/default
        instance, per the client SDK's own definition of this route
        (which accepts ``directory``/``workspace`` query parameters, the
        same pair the built-in TUI's own permission-reply call sites
        always pass). A task-worktree session ask omitting this parameter
        resolves against the *project root* instance instead, which
        returns 404 for a request ID it never issued -- confirmed live
        against the real OpenCode 1.18.22 server (see ADR 0016). Every
        ``permission.asked`` event's envelope already carries
        ``directory`` (``normalize_global_event``'s ``OpenCodeEvent``),
        so this requires no extra lookup."""
        try:
            client = httpx.Client(base_url=self._base_url, timeout=_REPLY_TIMEOUT_SECONDS)
        except Exception as exc:
            return _ReplyOutcome(False, f"{type(exc).__name__}: {exc}")
        try:
            response = client.post(
                f"/permission/{request_id}/reply",
                params={"directory": directory},
                json={"reply": "reject"},
            )
            if response.status_code < 400:
                return _ReplyOutcome(True, f"HTTP {response.status_code}")
            return _ReplyOutcome(False, f"HTTP {response.status_code}")
        except Exception as exc:
            return _ReplyOutcome(False, f"{type(exc).__name__}: {exc}")
        finally:
            try:
                client.close()
            except Exception:
                pass


def _default_notice(message: str) -> None:
    print(f"loop-supervisor: {message}", file=sys.stderr)


class SessionMonitor:
    """Owns the single headless subscription to ``GET /global/event`` and
    fans every normalized event out to attached consumers.

    Call :meth:`start` once the server is ready and :meth:`stop` during
    teardown, before the server itself is stopped. Attach consumers
    before calling :meth:`start` (via the constructor's ``consumers``)
    or any time after via :meth:`add_consumer` -- events delivered before
    a consumer is attached are simply not seen by it, same as any other
    subscribe-after-publish gap.
    """

    def __init__(
        self,
        base_url: str,
        *,
        consumers: list[SessionEventConsumer] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._consumers: list[SessionEventConsumer] = list(consumers or [])
        self._sse = SSEClient(
            base_url,
            on_event=self._on_event,
            on_state_change=self._on_state_change,
            on_notice=self._on_notice,
        )

    def add_consumer(self, consumer: SessionEventConsumer) -> None:
        self._consumers.append(consumer)

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

    def _on_event(self, raw_event: dict[str, Any]) -> None:
        try:
            event = normalize_global_event(raw_event)
        except OpenCodeEventError:
            return
        for consumer in self._consumers:
            try:
                consumer.on_event(raw_event, event)
            except Exception as exc:
                self._on_notice(
                    f"session monitor consumer {type(consumer).__name__} raised "
                    f"{type(exc).__name__}: {exc}"
                )

    def _on_state_change(self, state: SSEConnectionState, _reason: str) -> None:
        if state == SSEConnectionState.LIVE:
            print(
                f"loop-supervisor: session monitor watching {self._base_url}",
                file=sys.stderr,
            )

    def _on_notice(self, notice: str) -> None:
        print(f"loop-supervisor: {notice}", file=sys.stderr)
