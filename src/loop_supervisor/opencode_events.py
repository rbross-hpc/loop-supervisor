"""Canonical normalization boundary for OpenCode ``/global/event`` envelopes.

Delta handling: the canonical OpenCode event for streaming a partial update
is ``message.part.updated`` with an optional ``properties.delta`` string;
there is no canonical standalone ``message.part.delta`` event in the
generated OpenCode API. This module still accepts a standalone
``message.part.delta`` event as a documented compatibility form (for older
or alternate OpenCode versions), but treats it strictly as delta-only
telemetry with an explicit ``delta_field`` ("text" or "reasoning"), never
as a full part snapshot. Downstream code (the live reducer) must never
confuse the two: a ``message.part.updated`` part's `text`/`reasoning` is
always a cumulative snapshot (its accompanying `delta`, if present, is
already reflected in that snapshot and must not also be appended), while a
compatibility ``message.part.delta`` event carries only an increment that
must be appended to the field named by `delta_field`.

The ``/global/event`` SSE stream emits objects shaped as::

    {
        "directory": "/repo/worktree",
        "payload": {
            "id": "event-id",
            "type": "message.part.updated",
            "properties": { ... }
        }
    }

This module is the only place that knows the nested envelope and property
layout. Everything downstream works with ``OpenCodeEvent`` instances only.

Unknown event and part types produce valid ``OpenCodeEvent`` objects (with
``part=None``) so the reducer can count them without knowing the schema.

Malformed envelopes — missing ``directory``, ``payload``, or ``type`` —
raise ``OpenCodeEventError`` so callers can emit a notice and skip them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class OpenCodeEventError(ValueError):
    """Raised when a raw envelope cannot be normalized."""


@dataclass(frozen=True)
class OpenCodeToolState:
    status: str
    input_summary: str = ""
    output: str = ""
    error: str = ""
    title: str = ""


@dataclass(frozen=True)
class OpenCodePart:
    part_id: str
    session_id: str
    message_id: str
    type: str
    text: str = ""
    reasoning: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    tool_state: OpenCodeToolState | None = None


@dataclass(frozen=True)
class OpenCodeEvent:
    directory: str
    event_id: str | None
    type: str
    session_id: str | None = None
    message_id: str | None = None
    role: str | None = None
    status: str | None = None
    part: OpenCodePart | None = None
    delta: str | None = None
    delta_field: str | None = None
    file: str | None = None


def normalize_global_event(raw: Mapping[str, Any]) -> OpenCodeEvent:
    """Normalize one raw SSE JSON object from ``/global/event``.

    Raises ``OpenCodeEventError`` for structurally invalid envelopes.
    Unknown event or part types return a valid ``OpenCodeEvent`` with
    ``part=None`` and ``status=None``.
    """
    directory = raw.get("directory")
    if not isinstance(directory, str) or not directory:
        raise OpenCodeEventError("envelope missing required string 'directory'")

    payload = raw.get("payload")
    if not isinstance(payload, dict):
        raise OpenCodeEventError("envelope missing required object 'payload'")

    event_type = payload.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise OpenCodeEventError("payload missing required string 'type'")

    properties = payload.get("properties")
    if not isinstance(properties, dict):
        properties = {}

    raw_id = payload.get("id")
    event_id = str(raw_id) if raw_id is not None else None

    return _dispatch(directory, event_id, event_type, properties)


def _dispatch(
    directory: str,
    event_id: str | None,
    event_type: str,
    props: dict[str, Any],
) -> OpenCodeEvent:
    if event_type == "server.connected":
        return OpenCodeEvent(directory=directory, event_id=event_id, type=event_type)

    if event_type in ("session.status", "session.idle", "session.error"):
        return _norm_session_status(directory, event_id, event_type, props)

    if event_type == "message.updated":
        return _norm_message_updated(directory, event_id, props)

    if event_type in ("message.part.updated", "message.part.delta", "message.part.removed"):
        return _norm_message_part(directory, event_id, event_type, props)

    if event_type in ("todo.updated", "session.diff"):
        session_id = _str_or_none(props.get("sessionID"))
        return OpenCodeEvent(
            directory=directory,
            event_id=event_id,
            type=event_type,
            session_id=session_id,
        )

    if event_type == "file.edited":
        file_val = _str_or_none(props.get("file"))
        return OpenCodeEvent(
            directory=directory,
            event_id=event_id,
            type=event_type,
            file=file_val,
        )

    return OpenCodeEvent(directory=directory, event_id=event_id, type=event_type)


def _norm_session_status(
    directory: str,
    event_id: str | None,
    event_type: str,
    props: dict[str, Any],
) -> OpenCodeEvent:
    session_id = _str_or_none(props.get("sessionID"))
    status_obj = props.get("status")
    if isinstance(status_obj, dict):
        status_type = _str_or_none(status_obj.get("type"))
    else:
        status_type = None

    if event_type == "session.idle":
        status_type = "idle"
    elif event_type == "session.error":
        status_type = "error"

    return OpenCodeEvent(
        directory=directory,
        event_id=event_id,
        type=event_type,
        session_id=session_id,
        status=status_type,
    )


def _norm_message_updated(
    directory: str,
    event_id: str | None,
    props: dict[str, Any],
) -> OpenCodeEvent:
    info = props.get("info")
    if not isinstance(info, dict):
        info = {}
    session_id = _str_or_none(info.get("sessionID"))
    message_id = _str_or_none(info.get("id"))
    role = _str_or_none(info.get("role"))
    return OpenCodeEvent(
        directory=directory,
        event_id=event_id,
        type="message.updated",
        session_id=session_id,
        message_id=message_id,
        role=role,
    )


def _norm_message_part(
    directory: str,
    event_id: str | None,
    event_type: str,
    props: dict[str, Any],
) -> OpenCodeEvent:
    if event_type == "message.part.delta":
        session_id = _str_or_none(props.get("sessionID"))
        message_id = _str_or_none(props.get("messageID"))
        part_id = _str_or_none(props.get("partID"))
        delta_field = _str_or_none(props.get("field"))
        delta = _str_or_none(props.get("delta"))
        part = OpenCodePart(
            part_id=part_id or "",
            session_id=session_id or "",
            message_id=message_id or "",
            type="delta",
        )
        return OpenCodeEvent(
            directory=directory,
            event_id=event_id,
            type=event_type,
            session_id=session_id,
            message_id=message_id,
            part=part,
            delta=delta,
            delta_field=delta_field,
        )

    if event_type == "message.part.removed":
        session_id = _str_or_none(props.get("sessionID"))
        message_id = _str_or_none(props.get("messageID"))
        part_id = _str_or_none(props.get("partID"))
        part = OpenCodePart(
            part_id=part_id or "",
            session_id=session_id or "",
            message_id=message_id or "",
            type="removed",
        )
        return OpenCodeEvent(
            directory=directory,
            event_id=event_id,
            type=event_type,
            session_id=session_id,
            message_id=message_id,
            part=part,
        )

    raw_part = props.get("part")
    if not isinstance(raw_part, dict):
        raw_part = {}

    part_id = _str_or_none(raw_part.get("id"))
    session_id = _str_or_none(raw_part.get("sessionID"))
    message_id = _str_or_none(raw_part.get("messageID"))
    part_type = _str_or_none(raw_part.get("type")) or ""

    part = _norm_part(raw_part, part_id or "", session_id or "", message_id or "", part_type)

    delta_raw = props.get("delta")
    delta = str(delta_raw) if delta_raw is not None else None
    inferred_delta_field: str | None = None
    if delta is not None:
        if part_type == "text":
            inferred_delta_field = "text"
        elif part_type == "reasoning":
            inferred_delta_field = "reasoning"

    return OpenCodeEvent(
        directory=directory,
        event_id=event_id,
        type=event_type,
        session_id=session_id,
        message_id=message_id,
        part=part,
        delta=delta,
        delta_field=inferred_delta_field,
    )


def _norm_part(
    raw: dict[str, Any],
    part_id: str,
    session_id: str,
    message_id: str,
    part_type: str,
) -> OpenCodePart:
    if part_type == "text":
        return OpenCodePart(
            part_id=part_id,
            session_id=session_id,
            message_id=message_id,
            type="text",
            text=str(raw.get("text", "") or ""),
        )

    if part_type == "reasoning":
        return OpenCodePart(
            part_id=part_id,
            session_id=session_id,
            message_id=message_id,
            type="reasoning",
            reasoning=str(raw.get("text", "") or ""),
        )

    if part_type == "tool":
        call_id = _str_or_none(raw.get("callID")) or ""
        tool_name = _str_or_none(raw.get("tool")) or ""
        state_raw = raw.get("state")
        tool_state = _norm_tool_state(state_raw)
        return OpenCodePart(
            part_id=part_id,
            session_id=session_id,
            message_id=message_id,
            type="tool",
            tool_call_id=call_id,
            tool_name=tool_name,
            tool_state=tool_state,
        )

    return OpenCodePart(
        part_id=part_id,
        session_id=session_id,
        message_id=message_id,
        type=part_type,
    )


def _norm_tool_state(state_raw: Any) -> OpenCodeToolState | None:
    if not isinstance(state_raw, dict):
        return None
    status = _str_or_none(state_raw.get("status")) or "pending"
    title = _str_or_none(state_raw.get("title")) or ""
    input_obj = state_raw.get("input")
    input_summary = str(input_obj)[:512] if input_obj is not None else ""
    output = _str_or_none(state_raw.get("output")) or ""
    error = _str_or_none(state_raw.get("error")) or ""
    return OpenCodeToolState(
        status=status,
        input_summary=input_summary,
        output=output,
        error=error,
        title=title,
    )


def _str_or_none(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, str):
        return val or None
    return str(val)
