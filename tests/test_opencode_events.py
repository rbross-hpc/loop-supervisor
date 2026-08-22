"""Tests for canonical OpenCode global-event normalization."""

import pytest

from loop_supervisor.opencode_events import (
    OpenCodeEventError,
    normalize_global_event,
)


def _wrap(
    event_type: str, properties: dict, directory: str = "/repo", event_id: str | None = None
) -> dict:
    payload: dict = {"type": event_type, "properties": properties}
    if event_id is not None:
        payload["id"] = event_id
    return {"directory": directory, "payload": payload}


def test_missing_directory_raises():
    with pytest.raises(OpenCodeEventError, match="directory"):
        normalize_global_event({"payload": {"type": "server.connected", "properties": {}}})


def test_missing_payload_raises():
    with pytest.raises(OpenCodeEventError, match="payload"):
        normalize_global_event({"directory": "/repo"})


def test_missing_type_raises():
    with pytest.raises(OpenCodeEventError, match="type"):
        normalize_global_event({"directory": "/repo", "payload": {"properties": {}}})


def test_server_connected():
    event = normalize_global_event(_wrap("server.connected", {}))
    assert event.type == "server.connected"
    assert event.directory == "/repo"
    assert event.session_id is None


def test_event_id_extracted():
    event = normalize_global_event(_wrap("server.connected", {}, event_id="abc123"))
    assert event.event_id == "abc123"


def test_numeric_event_id_stringified():
    raw = {
        "directory": "/repo",
        "payload": {"id": 42, "type": "server.connected", "properties": {}},
    }
    event = normalize_global_event(raw)
    assert event.event_id == "42"


def test_session_status_busy():
    raw = _wrap("session.status", {"sessionID": "s1", "status": {"type": "busy"}})
    event = normalize_global_event(raw)
    assert event.type == "session.status"
    assert event.session_id == "s1"
    assert event.status == "busy"


def test_session_status_idle_type():
    raw = _wrap("session.status", {"sessionID": "s1", "status": {"type": "idle"}})
    event = normalize_global_event(raw)
    assert event.status == "idle"


def test_session_idle_event():
    raw = _wrap("session.idle", {"sessionID": "s1"})
    event = normalize_global_event(raw)
    assert event.status == "idle"
    assert event.session_id == "s1"


def test_session_error_event():
    raw = _wrap("session.error", {"sessionID": "s1"})
    event = normalize_global_event(raw)
    assert event.status == "error"


def test_message_updated_extracts_info():
    raw = _wrap("message.updated", {"info": {"sessionID": "s1", "id": "m1", "role": "assistant"}})
    event = normalize_global_event(raw)
    assert event.session_id == "s1"
    assert event.message_id == "m1"
    assert event.role == "assistant"


def test_message_part_updated_text():
    raw = _wrap(
        "message.part.updated",
        {
            "part": {
                "id": "p1",
                "sessionID": "s1",
                "messageID": "m1",
                "type": "text",
                "text": "hello world",
            }
        },
    )
    event = normalize_global_event(raw)
    assert event.session_id == "s1"
    assert event.message_id == "m1"
    assert event.part is not None
    assert event.part.type == "text"
    assert event.part.text == "hello world"


def test_message_part_updated_tool():
    raw = _wrap(
        "message.part.updated",
        {
            "part": {
                "id": "p1",
                "sessionID": "s1",
                "messageID": "m1",
                "type": "tool",
                "callID": "c1",
                "tool": "bash",
                "state": {"status": "completed", "output": "done", "title": "bash", "input": {}},
            }
        },
    )
    event = normalize_global_event(raw)
    assert event.part is not None
    assert event.part.type == "tool"
    assert event.part.tool_call_id == "c1"
    assert event.part.tool_name == "bash"
    assert event.part.tool_state is not None
    assert event.part.tool_state.status == "completed"
    assert event.part.tool_state.output == "done"


def test_message_part_updated_text_with_delta_infers_text_field():
    """Canonical message.part.updated may carry an optional delta; the
    normalizer must infer its target field from the part type so the
    reducer never has to guess."""
    raw = _wrap(
        "message.part.updated",
        {
            "part": {
                "id": "p1",
                "sessionID": "s1",
                "messageID": "m1",
                "type": "text",
                "text": "hello world",
            },
            "delta": "world",
        },
    )
    event = normalize_global_event(raw)
    assert event.delta == "world"
    assert event.delta_field == "text"
    assert event.part.text == "hello world"


def test_message_part_updated_reasoning_with_delta_infers_reasoning_field():
    raw = _wrap(
        "message.part.updated",
        {
            "part": {
                "id": "p1",
                "sessionID": "s1",
                "messageID": "m1",
                "type": "reasoning",
                "text": "thinking more",
            },
            "delta": "more",
        },
    )
    event = normalize_global_event(raw)
    assert event.delta == "more"
    assert event.delta_field == "reasoning"
    assert event.part.reasoning == "thinking more"


def test_message_part_updated_without_delta_has_no_delta_field():
    raw = _wrap(
        "message.part.updated",
        {
            "part": {
                "id": "p1",
                "sessionID": "s1",
                "messageID": "m1",
                "type": "text",
                "text": "hello",
            }
        },
    )
    event = normalize_global_event(raw)
    assert event.delta is None
    assert event.delta_field is None


def test_message_part_delta():
    raw = _wrap(
        "message.part.delta",
        {
            "sessionID": "s1",
            "messageID": "m1",
            "partID": "p1",
            "field": "text",
            "delta": "hello",
        },
    )
    event = normalize_global_event(raw)
    assert event.type == "message.part.delta"
    assert event.session_id == "s1"
    assert event.delta == "hello"
    assert event.delta_field == "text"
    assert event.part is not None
    assert event.part.type == "delta"


def test_message_part_removed():
    raw = _wrap(
        "message.part.removed",
        {
            "sessionID": "s1",
            "messageID": "m1",
            "partID": "p1",
        },
    )
    event = normalize_global_event(raw)
    assert event.type == "message.part.removed"
    assert event.session_id == "s1"
    assert event.part is not None
    assert event.part.type == "removed"


def test_file_edited_maps_file_property():
    raw = _wrap("file.edited", {"file": "/repo/src/foo.py"})
    event = normalize_global_event(raw)
    assert event.file == "/repo/src/foo.py"
    assert event.type == "file.edited"


def test_todo_updated():
    raw = _wrap("todo.updated", {"sessionID": "s1"})
    event = normalize_global_event(raw)
    assert event.session_id == "s1"


def test_unknown_event_type_produces_base_event():
    raw = _wrap("custom.unknown.type", {})
    event = normalize_global_event(raw)
    assert event.type == "custom.unknown.type"
    assert event.part is None
    assert event.session_id is None


def test_unknown_part_type_produces_base_part():
    raw = _wrap(
        "message.part.updated",
        {
            "part": {
                "id": "p1",
                "sessionID": "s1",
                "messageID": "m1",
                "type": "step-start",
            }
        },
    )
    event = normalize_global_event(raw)
    assert event.part is not None
    assert event.part.type == "step-start"
    assert event.part.text == ""
    assert event.part.tool_state is None


def test_missing_properties_falls_back_gracefully():
    raw = {"directory": "/repo", "payload": {"type": "server.connected"}}
    event = normalize_global_event(raw)
    assert event.type == "server.connected"


def test_tool_state_pending():
    raw = _wrap(
        "message.part.updated",
        {
            "part": {
                "id": "p1",
                "sessionID": "s1",
                "messageID": "m1",
                "type": "tool",
                "callID": "c1",
                "tool": "bash",
                "state": {"status": "pending", "input": {"cmd": "ls"}, "raw": "ls"},
            }
        },
    )
    event = normalize_global_event(raw)
    assert event.part.tool_state.status == "pending"


def test_tool_state_error():
    raw = _wrap(
        "message.part.updated",
        {
            "part": {
                "id": "p1",
                "sessionID": "s1",
                "messageID": "m1",
                "type": "tool",
                "callID": "c1",
                "tool": "bash",
                "state": {"status": "error", "input": {}, "error": "permission denied"},
            }
        },
    )
    event = normalize_global_event(raw)
    assert event.part.tool_state.status == "error"
    assert event.part.tool_state.error == "permission denied"


def test_directory_preserved_exactly():
    raw = _wrap("server.connected", {}, directory="/a/b/c")
    event = normalize_global_event(raw)
    assert event.directory == "/a/b/c"


def test_flat_object_without_payload_raises():
    with pytest.raises(OpenCodeEventError):
        normalize_global_event({"type": "session.idle", "sessionID": "s1"})
