"""Typed Textual messages for inter-component communication.

All reducer mutations happen on the Textual event-loop thread via these
messages. Worker threads (SSE, invocation observers) must post messages
via ``app.call_from_thread(app.post_message, ...)`` and must never touch
the reducer directly.
"""

from __future__ import annotations

from textual.message import Message

from ..opencode import InvocationRef
from ..opencode_events import OpenCodeEvent
from ..sse import SSEConnectionState
from ..supervisor import AdvanceOutcome
from .live import LiveActivitySnapshot


class AdvanceCompleted(Message):
    """Posted by the transition worker when advance() returns."""

    def __init__(self, outcome: AdvanceOutcome) -> None:
        super().__init__()
        self.outcome = outcome


class AdvanceFailed(Message):
    """Posted by the transition worker when advance() raises unexpectedly."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error


class LiveUpdated(Message):
    """Posted by the Textual event-loop handlers after reducer mutation."""

    def __init__(self, snapshot: LiveActivitySnapshot) -> None:
        super().__init__()
        self.snapshot = snapshot


class LiveDisconnected(Message):
    """Posted when SSE disconnects."""

    def __init__(self, reason: str) -> None:
        super().__init__()
        self.reason = reason


class OpenCodeEventReceived(Message):
    """Posted by the SSE thread (via call_from_thread) with a normalized event.

    The Textual event handler applies it to the reducer."""

    def __init__(self, event: OpenCodeEvent) -> None:
        super().__init__()
        self.event = event


class LiveConnectionChanged(Message):
    """Posted by the SSE thread when connection state changes."""

    def __init__(self, state: SSEConnectionState, reason: str) -> None:
        super().__init__()
        self.state = state
        self.reason = reason


class InvocationStarted(Message):
    """Posted by the invocation observer when an agent starts."""

    def __init__(self, invocation: InvocationRef) -> None:
        super().__init__()
        self.invocation = invocation


class InvocationFinished(Message):
    """Posted by the invocation observer when an agent finishes."""

    def __init__(self, invocation: InvocationRef, error: BaseException | None) -> None:
        super().__init__()
        self.invocation = invocation
        self.error = error


class RunSelected(Message):
    """Posted when the user selects a run from the browser."""

    def __init__(self, run_id: str | None) -> None:
        super().__init__()
        self.run_id = run_id


class InputSubmitted(Message):
    """Posted when the user submits a pending-input form."""

    def __init__(self, answer: str) -> None:
        super().__init__()
        self.answer = answer
