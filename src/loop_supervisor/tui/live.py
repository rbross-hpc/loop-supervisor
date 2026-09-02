"""Bounded, immutable live-event reducer for OpenCode SSE telemetry.

All models are immutable. The reducer returns a new snapshot on every
update. Unknown event and part types increment counters but do not fail.

Session-bearing events are accepted only when both the session ID is active
or retained in the bounded recently-finished set AND the event envelope
directory exactly matches the directory registered for that session. No
prefix or child-path matching.

Directory-only events (``file.edited``) are accepted only when the
directory exactly matches one of the active invocation directories.

The reducer is NOT thread-safe. All calls must come from the same thread
(the Textual event-loop thread). Worker callbacks must post typed messages
and let the handler invoke the reducer.
"""

from __future__ import annotations

import collections
import threading
from dataclasses import dataclass, field, replace

from ..opencode import InvocationRef
from ..opencode_events import OpenCodeEvent

_MAX_INVOCATIONS = 4
_MAX_FINISHED_INVOCATIONS = 4
_MAX_FEED_RECORDS = 200
_MAX_TEXT_TAIL = 16 * 1024
_MAX_TOOLS = 100
_MAX_TOUCHED_FILES = 200
_MAX_TOOL_RESULT_SUMMARY = 1024
_MAX_EVENT_IDS = 2048
_MAX_PENDING_EVENTS = 256
_MAX_NOTICES = 20


@dataclass(frozen=True)
class LiveConnection:
    state: str = "disconnected"
    reason: str = ""


@dataclass(frozen=True)
class LiveTool:
    tool_id: str
    name: str
    status: str
    input_summary: str = ""
    result_summary: str = ""


@dataclass(frozen=True)
class LiveMessage:
    message_id: str
    role: str
    text_tail: str = ""
    reasoning_tail: str = ""
    tools: tuple[LiveTool, ...] = ()


@dataclass(frozen=True)
class LiveInvocation:
    session_id: str
    agent: str
    directory: str
    status: str = "idle"
    latest_message: LiveMessage | None = None
    unknown_part_count: int = 0


@dataclass(frozen=True)
class LiveFeedItem:
    kind: str
    session_id: str
    summary: str


@dataclass(frozen=True)
class LiveActivitySnapshot:
    connection: LiveConnection = field(default_factory=LiveConnection)
    invocations: tuple[LiveInvocation, ...] = ()
    feed: tuple[LiveFeedItem, ...] = ()
    touched_files: tuple[str, ...] = ()
    notices: tuple[str, ...] = ()
    unknown_event_count: int = 0


def _tail(text: str, max_bytes: int) -> str:
    encoded = text.encode()
    if len(encoded) <= max_bytes:
        return text
    tail_bytes = encoded[-max_bytes:]
    while True:
        try:
            return tail_bytes.decode("utf-8")
        except UnicodeDecodeError:
            tail_bytes = tail_bytes[1:]
            if not tail_bytes:
                return ""


class LiveActivityReducer:
    """Mutable accumulator that produces immutable ``LiveActivitySnapshot``s.

    All methods must be called from the same thread. Pass an
    ``owner_thread`` at construction to enable ownership assertions.

    Usage::

        reducer = LiveActivityReducer()
        reducer.register_invocation(ref)
        reducer.on_event(event)        # accepts OpenCodeEvent
        snapshot = reducer.snapshot()
    """

    def __init__(self, *, owner_thread: threading.Thread | None = None) -> None:
        self._owner = owner_thread
        self._connection = LiveConnection()
        self._invocations: dict[str, LiveInvocation] = {}
        self._invocation_order: list[str] = []
        self._feed: collections.deque[LiveFeedItem] = collections.deque(maxlen=_MAX_FEED_RECORDS)
        self._touched_files: list[str] = []
        self._touched_files_set: set[str] = set()
        self._unknown_event_count = 0
        self._notices: collections.deque[str] = collections.deque(maxlen=_MAX_NOTICES)
        self._active_invocations: dict[str, str] = {}
        self._finished_invocations: collections.OrderedDict[str, str] = collections.OrderedDict()
        self._seen_event_ids: collections.deque[str] = collections.deque(maxlen=_MAX_EVENT_IDS)
        self._pending_events: collections.deque[OpenCodeEvent] = collections.deque(
            maxlen=_MAX_PENDING_EVENTS
        )

    def _assert_owner(self) -> None:
        if self._owner is not None and threading.current_thread() is not self._owner:
            raise RuntimeError(
                f"LiveActivityReducer called from {threading.current_thread().name!r} "
                f"but owned by {self._owner.name!r}"
            )

    def set_connection(self, state: str, reason: str) -> None:
        self._assert_owner()
        self._connection = LiveConnection(state=state, reason=reason)

    def add_notice(self, notice: str) -> None:
        self._assert_owner()
        self._notices.append(notice)

    def register_invocation(self, ref: InvocationRef) -> None:
        self._assert_owner()
        self._finished_invocations.pop(ref.session_id, None)
        self._active_invocations[ref.session_id] = str(ref.directory)
        inv = LiveInvocation(
            session_id=ref.session_id,
            agent=ref.agent,
            directory=str(ref.directory),
            status="running",
        )
        self._invocations[ref.session_id] = inv
        self._invocation_order.append(ref.session_id)
        if len(self._invocation_order) > _MAX_INVOCATIONS:
            old_id = self._invocation_order.pop(0)
            self._invocations.pop(old_id, None)

        pending = tuple(self._pending_events)
        self._pending_events.clear()
        for event in pending:
            if event.session_id == ref.session_id:
                if event.directory == str(ref.directory):
                    self.on_event(event)
                continue
            self._pending_events.append(event)

    def unregister_invocation(self, ref: InvocationRef) -> None:
        self._assert_owner()
        active_directory = self._active_invocations.pop(ref.session_id, None)
        if active_directory is not None and active_directory == str(ref.directory):
            self._finished_invocations[ref.session_id] = active_directory
            self._finished_invocations.move_to_end(ref.session_id)
            while len(self._finished_invocations) > _MAX_FINISHED_INVOCATIONS:
                self._finished_invocations.popitem(last=False)
        if ref.session_id in self._invocations:
            inv = self._invocations[ref.session_id]
            self._invocations[ref.session_id] = replace(inv, status="done")

    def on_event(self, event: OpenCodeEvent) -> None:
        """Process a normalized OpenCodeEvent. Must be called on the owner thread."""
        self._assert_owner()

        event_type = event.type

        if self._should_buffer(event):
            self._pending_events.append(event)
            return

        event_id = event.event_id
        if event_id is not None:
            if event_id in self._seen_event_ids:
                return
            self._seen_event_ids.append(event_id)

        if event_type == "server.connected":
            self._connection = LiveConnection(state="live", reason="connected")
            return

        if event_type in ("session.status", "session.idle", "session.error"):
            if not self._is_attributed(event):
                return
            session_id = event.session_id or ""
            status_map = {
                "session.status": event.status or "running",
                "session.idle": "idle",
                "session.error": "error",
            }
            status = status_map[event_type]
            if status == "busy":
                status = "running"
            if session_id in self._active_invocations and session_id in self._invocations:
                self._invocations[session_id] = replace(
                    self._invocations[session_id], status=status
                )
            self._feed.append(LiveFeedItem(kind=event_type, session_id=session_id, summary=status))
            return

        if event_type in ("message.updated", "message.part.updated", "message.part.delta"):
            if not self._is_attributed(event):
                return
            self._apply_message_event(event)
            return

        if event_type == "message.part.removed":
            return

        if event_type in ("todo.updated", "session.diff"):
            if not self._is_attributed(event):
                return
            session_id = event.session_id or ""
            self._feed.append(
                LiveFeedItem(kind=event_type, session_id=session_id, summary=event_type)
            )
            return

        if event_type == "file.edited":
            if not self._is_directory_attributed(event):
                return
            file_path = event.file
            if file_path and file_path not in self._touched_files_set:
                if len(self._touched_files) < _MAX_TOUCHED_FILES:
                    self._touched_files.append(file_path)
                    self._touched_files_set.add(file_path)
            return

        self._unknown_event_count += 1

    def _should_buffer(self, event: OpenCodeEvent) -> bool:
        return (
            event.session_id is not None
            and event.session_id not in self._active_invocations
            and event.session_id not in self._finished_invocations
            and event.type
            in {
                "session.status",
                "session.idle",
                "session.error",
                "message.updated",
                "message.part.updated",
                "message.part.delta",
                "todo.updated",
                "session.diff",
            }
        )

    def _is_attributed(self, event: OpenCodeEvent) -> bool:
        """Accept iff an active or retained session's directory exactly matches."""
        session_id = event.session_id
        if not session_id:
            return False
        expected_dir = self._active_invocations.get(session_id)
        if expected_dir is None:
            expected_dir = self._finished_invocations.get(session_id)
        if expected_dir is None:
            return False
        return event.directory == expected_dir

    def _is_directory_attributed(self, event: OpenCodeEvent) -> bool:
        """Accept iff directory exactly matches any active invocation directory."""
        return event.directory in self._active_invocations.values()

    def _apply_message_event(self, event: OpenCodeEvent) -> None:
        session_id = event.session_id or ""
        inv = self._invocations.get(session_id)
        if inv is None:
            return

        if event.type == "message.updated":
            message_id = event.message_id or ""
            current_msg = inv.latest_message
            if current_msg is None or current_msg.message_id != message_id:
                current_msg = LiveMessage(message_id=message_id, role=event.role or "assistant")
            inv = replace(inv, latest_message=current_msg)
            self._invocations[session_id] = inv
            return

        part = event.part
        if part is None:
            return

        message_id = part.message_id or event.message_id or ""
        current_msg = inv.latest_message
        if current_msg is None or current_msg.message_id != message_id:
            current_msg = LiveMessage(message_id=message_id, role="assistant")

        part_type = part.type

        if part_type == "delta":
            # Standalone (non-canonical) compatibility event: there is no
            # full part snapshot here, only a delta targeting an explicit
            # field. Never guess the target from an unknown/missing field,
            # and never let a reasoning delta land in text or vice versa.
            if event.delta_field == "text":
                new_text = current_msg.text_tail + (event.delta or "")
                current_msg = replace(current_msg, text_tail=_tail(new_text, _MAX_TEXT_TAIL))
            elif event.delta_field == "reasoning":
                new_reasoning = current_msg.reasoning_tail + (event.delta or "")
                current_msg = replace(
                    current_msg, reasoning_tail=_tail(new_reasoning, _MAX_TEXT_TAIL)
                )
            else:
                inv = replace(inv, unknown_part_count=inv.unknown_part_count + 1)

        elif part_type == "text":
            # Canonical message.part.updated: `part.text` is always a
            # cumulative snapshot, not an increment. Any accompanying
            # `event.delta` is already reflected in that snapshot, so it
            # must never also be appended (that would duplicate it).
            new_text = part.text if part.text else current_msg.text_tail
            current_msg = replace(
                current_msg,
                text_tail=_tail(new_text, _MAX_TEXT_TAIL),
            )

        elif part_type == "reasoning":
            new_reasoning = part.reasoning if part.reasoning else current_msg.reasoning_tail
            current_msg = replace(
                current_msg,
                reasoning_tail=_tail(new_reasoning, _MAX_TEXT_TAIL),
            )

        elif part_type == "tool":
            tool_id = part.tool_call_id or part.part_id
            tool_name = part.tool_name
            tool_state = part.tool_state
            if tool_state is not None:
                status = tool_state.status
                result_raw = tool_state.output or tool_state.error or ""
                result_summary = result_raw[:_MAX_TOOL_RESULT_SUMMARY]
                input_summary = tool_state.input_summary
            else:
                status = "pending"
                result_summary = ""
                input_summary = ""
            existing_tools = {t.tool_id: t for t in current_msg.tools}
            existing_tools[tool_id] = LiveTool(
                tool_id=tool_id,
                name=tool_name,
                status=status,
                input_summary=input_summary,
                result_summary=result_summary,
            )
            limited = dict(list(existing_tools.items())[-_MAX_TOOLS:])
            current_msg = replace(current_msg, tools=tuple(limited.values()))

        elif part_type == "removed":
            pass

        else:
            inv = replace(inv, unknown_part_count=inv.unknown_part_count + 1)

        inv = replace(inv, latest_message=current_msg)
        self._invocations[session_id] = inv

    def snapshot(self) -> LiveActivitySnapshot:
        self._assert_owner()
        ordered_invs = []
        for sid in self._invocation_order:
            if sid in self._invocations:
                ordered_invs.append(self._invocations[sid])

        return LiveActivitySnapshot(
            connection=self._connection,
            invocations=tuple(ordered_invs),
            feed=tuple(self._feed),
            touched_files=tuple(self._touched_files),
            notices=tuple(self._notices),
            unknown_event_count=self._unknown_event_count,
        )
