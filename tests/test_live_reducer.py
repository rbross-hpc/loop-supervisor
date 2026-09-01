"""Tests for LiveActivityReducer with normalized OpenCodeEvent inputs."""

import threading
import time
from pathlib import Path

from loop_supervisor.opencode import InvocationRef
from loop_supervisor.opencode_events import (
    OpenCodeEvent,
    OpenCodePart,
    OpenCodeToolState,
    normalize_global_event,
)
from loop_supervisor.tui.live import (
    _MAX_FEED_RECORDS,
    _MAX_INVOCATIONS,
    _MAX_TEXT_TAIL,
    _MAX_TOOLS,
    _MAX_TOUCHED_FILES,
    LiveActivityReducer,
)


def _ref(
    session_id: str = "s1", agent: str = "loop-builder", directory: str = "/repo"
) -> InvocationRef:
    return InvocationRef(
        session_id=session_id,
        agent=agent,
        directory=Path(directory),
        started_monotonic=time.monotonic(),
    )


def _ev(
    event_type: str, session_id: str | None = None, directory: str = "/repo", **kwargs
) -> OpenCodeEvent:
    return OpenCodeEvent(
        directory=directory,
        event_id=None,
        type=event_type,
        session_id=session_id,
        **kwargs,
    )


def _part_ev(
    event_type: str,
    session_id: str,
    message_id: str,
    part: OpenCodePart,
    directory: str = "/repo",
) -> OpenCodeEvent:
    return OpenCodeEvent(
        directory=directory,
        event_id=None,
        type=event_type,
        session_id=session_id,
        message_id=message_id,
        part=part,
    )


def _text_part(
    part_id: str = "p1", session_id: str = "s1", message_id: str = "m1", text: str = ""
) -> OpenCodePart:
    return OpenCodePart(
        part_id=part_id, session_id=session_id, message_id=message_id, type="text", text=text
    )


def _tool_part(
    part_id: str = "p1",
    session_id: str = "s1",
    message_id: str = "m1",
    call_id: str = "t1",
    name: str = "bash",
    status: str = "pending",
) -> OpenCodePart:
    return OpenCodePart(
        part_id=part_id,
        session_id=session_id,
        message_id=message_id,
        type="tool",
        tool_call_id=call_id,
        tool_name=name,
        tool_state=OpenCodeToolState(status=status),
    )


def test_initial_snapshot_empty():
    reducer = LiveActivityReducer()
    snap = reducer.snapshot()
    assert snap.connection.state == "disconnected"
    assert snap.invocations == ()
    assert snap.feed == ()


def test_register_invocation_appears_in_snapshot():
    reducer = LiveActivityReducer()
    ref = _ref("s1")
    reducer.register_invocation(ref)
    snap = reducer.snapshot()
    assert len(snap.invocations) == 1
    assert snap.invocations[0].session_id == "s1"
    assert snap.invocations[0].status == "running"


def test_unregister_marks_done():
    reducer = LiveActivityReducer()
    ref = _ref("s1")
    reducer.register_invocation(ref)
    reducer.unregister_invocation(ref)
    snap = reducer.snapshot()
    assert snap.invocations[0].status == "done"


def test_max_invocations_enforced():
    reducer = LiveActivityReducer()
    refs = [_ref(f"s{i}", directory="/repo") for i in range(_MAX_INVOCATIONS + 3)]
    for ref in refs:
        reducer.register_invocation(ref)
    snap = reducer.snapshot()
    assert len(snap.invocations) <= _MAX_INVOCATIONS


def test_unknown_session_event_ignored():
    reducer = LiveActivityReducer()
    reducer.on_event(_ev("session.idle", session_id="unknown"))
    snap = reducer.snapshot()
    assert snap.feed == ()


def test_event_before_registration_is_applied_after_exact_attribution():
    reducer = LiveActivityReducer()
    reducer.on_event(_ev("session.idle", session_id="s1", directory="/repo"))

    reducer.register_invocation(_ref("s1", directory="/repo"))

    snap = reducer.snapshot()
    assert snap.invocations[0].status == "idle"
    assert len(snap.feed) == 1


def test_event_before_registration_wrong_directory_is_rejected():
    reducer = LiveActivityReducer()
    reducer.on_event(_ev("session.idle", session_id="s1", directory="/repo-other"))

    reducer.register_invocation(_ref("s1", directory="/repo"))

    snap = reducer.snapshot()
    assert snap.invocations[0].status == "running"
    assert snap.feed == ()


def test_pending_registration_events_are_bounded():
    pending_limit = 256
    reducer = LiveActivityReducer()
    for index in range(pending_limit + 1):
        reducer.on_event(_ev("session.idle", session_id=f"s{index}", directory=f"/repo/{index}"))

    reducer.register_invocation(_ref("s0", directory="/repo/0"))
    reducer.register_invocation(_ref(f"s{pending_limit}", directory=f"/repo/{pending_limit}"))

    snap = reducer.snapshot()
    by_session = {inv.session_id: inv for inv in snap.invocations}
    assert by_session["s0"].status == "running"
    assert by_session[f"s{pending_limit}"].status == "idle"


def test_session_idle_with_wrong_directory_ignored():
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    reducer.on_event(_ev("session.idle", session_id="s1", directory="/other"))
    snap = reducer.snapshot()
    assert snap.feed == ()
    assert snap.invocations[0].status == "running"


def test_session_idle_exact_directory_accepted():
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    reducer.on_event(_ev("session.idle", session_id="s1", directory="/repo"))
    snap = reducer.snapshot()
    assert snap.invocations[0].status == "idle"
    assert len(snap.feed) == 1


def test_session_status_busy_becomes_running():
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    reducer.on_event(_ev("session.status", session_id="s1", directory="/repo", status="busy"))
    snap = reducer.snapshot()
    assert snap.invocations[0].status == "running"


def test_file_edited_exact_directory_accepted():
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    reducer.on_event(_ev("file.edited", directory="/repo", file="/repo/foo.py"))
    snap = reducer.snapshot()
    assert "/repo/foo.py" in snap.touched_files


def test_file_edited_wrong_directory_ignored():
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    reducer.on_event(_ev("file.edited", directory="/other", file="/other/foo.py"))
    snap = reducer.snapshot()
    assert snap.touched_files == ()


def test_file_edited_child_directory_not_matched():
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    reducer.on_event(_ev("file.edited", directory="/repo/subdir", file="/repo/subdir/x.py"))
    snap = reducer.snapshot()
    assert snap.touched_files == ()


def test_file_edited_prefix_sibling_ignored():
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    reducer.on_event(_ev("file.edited", directory="/repo-other", file="/repo-other/x.py"))
    snap = reducer.snapshot()
    assert snap.touched_files == ()


def test_message_part_text_update():
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    part = _text_part(text="hello world")
    reducer.on_event(_part_ev("message.part.updated", "s1", "m1", part))
    snap = reducer.snapshot()
    msg = snap.invocations[0].latest_message
    assert msg is not None
    assert msg.text_tail == "hello world"


def _delta_part(
    part_id: str = "p1", session_id: str = "s1", message_id: str = "m1"
) -> OpenCodePart:
    """Matches what the real normalizer produces for a standalone
    (compatibility) message.part.delta event: a part with type="delta",
    never a full text/reasoning snapshot."""
    return OpenCodePart(part_id=part_id, session_id=session_id, message_id=message_id, type="delta")


def test_compatibility_message_part_delta_appends_to_text():
    """Standalone message.part.delta events carry only an increment, named
    by delta_field, and must be appended — this is the compatibility path,
    distinct from canonical message.part.updated snapshots."""
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    ev1 = OpenCodeEvent(
        directory="/repo",
        event_id=None,
        type="message.part.delta",
        session_id="s1",
        message_id="m1",
        part=_delta_part(),
        delta="hello ",
        delta_field="text",
    )
    ev2 = OpenCodeEvent(
        directory="/repo",
        event_id=None,
        type="message.part.delta",
        session_id="s1",
        message_id="m1",
        part=_delta_part(),
        delta="world",
        delta_field="text",
    )
    reducer.on_event(ev1)
    reducer.on_event(ev2)
    snap = reducer.snapshot()
    msg = snap.invocations[0].latest_message
    assert msg is not None
    assert msg.text_tail == "hello world"


def test_compatibility_message_part_delta_appends_to_reasoning():
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    ev1 = OpenCodeEvent(
        directory="/repo",
        event_id=None,
        type="message.part.delta",
        session_id="s1",
        message_id="m1",
        part=_delta_part(),
        delta="thinking ",
        delta_field="reasoning",
    )
    ev2 = OpenCodeEvent(
        directory="/repo",
        event_id=None,
        type="message.part.delta",
        session_id="s1",
        message_id="m1",
        part=_delta_part(),
        delta="more",
        delta_field="reasoning",
    )
    reducer.on_event(ev1)
    reducer.on_event(ev2)
    snap = reducer.snapshot()
    msg = snap.invocations[0].latest_message
    assert msg is not None
    assert msg.reasoning_tail == "thinking more"
    assert msg.text_tail == ""


def test_compatibility_delta_with_unknown_field_does_not_append_to_text():
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    ev = OpenCodeEvent(
        directory="/repo",
        event_id=None,
        type="message.part.delta",
        session_id="s1",
        message_id="m1",
        part=_delta_part(),
        delta="mystery",
        delta_field=None,
    )
    reducer.on_event(ev)
    snap = reducer.snapshot()
    msg = snap.invocations[0].latest_message
    assert msg is not None
    assert msg.text_tail == ""
    assert msg.reasoning_tail == ""
    assert snap.invocations[0].unknown_part_count == 1


def test_canonical_updated_snapshot_with_delta_is_not_duplicated():
    """A canonical message.part.updated part carries a cumulative
    snapshot; its accompanying delta must not also be appended on top,
    or the text would be duplicated."""
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    part = _text_part(text="hello world")
    ev = OpenCodeEvent(
        directory="/repo",
        event_id=None,
        type="message.part.updated",
        session_id="s1",
        message_id="m1",
        part=part,
        delta="world",
        delta_field="text",
    )
    reducer.on_event(ev)
    snap = reducer.snapshot()
    msg = snap.invocations[0].latest_message
    assert msg is not None
    assert msg.text_tail == "hello world"


def test_text_tail_bounded():
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    big_text = "x" * (_MAX_TEXT_TAIL * 3)
    part = _text_part(text=big_text)
    reducer.on_event(_part_ev("message.part.updated", "s1", "m1", part))
    snap = reducer.snapshot()
    msg = snap.invocations[0].latest_message
    assert msg is not None
    assert len(msg.text_tail.encode()) <= _MAX_TEXT_TAIL


def test_canonical_tool_part_stored():
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    part = _tool_part(call_id="t1", name="bash", status="completed")
    part = OpenCodePart(
        part_id="p1",
        session_id="s1",
        message_id="m1",
        type="tool",
        tool_call_id="t1",
        tool_name="bash",
        tool_state=OpenCodeToolState(status="completed", output="result", title="bash"),
    )
    reducer.on_event(_part_ev("message.part.updated", "s1", "m1", part))
    snap = reducer.snapshot()
    msg = snap.invocations[0].latest_message
    assert msg is not None
    assert len(msg.tools) == 1
    assert msg.tools[0].name == "bash"
    assert msg.tools[0].status == "completed"
    assert msg.tools[0].result_summary == "result"


def test_tools_bounded():
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    for i in range(_MAX_TOOLS + 10):
        part = OpenCodePart(
            part_id=f"p{i}",
            session_id="s1",
            message_id="m1",
            type="tool",
            tool_call_id=f"t{i}",
            tool_name=f"tool_{i}",
            tool_state=OpenCodeToolState(status="pending"),
        )
        reducer.on_event(_part_ev("message.part.updated", "s1", "m1", part))
    snap = reducer.snapshot()
    msg = snap.invocations[0].latest_message
    assert msg is not None
    assert len(msg.tools) <= _MAX_TOOLS


def test_touched_files_bounded():
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    for i in range(_MAX_TOUCHED_FILES + 10):
        reducer.on_event(_ev("file.edited", directory="/repo", file=f"/repo/f{i}.py"))
    snap = reducer.snapshot()
    assert len(snap.touched_files) <= _MAX_TOUCHED_FILES


def test_feed_bounded():
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    for _ in range(_MAX_FEED_RECORDS + 10):
        reducer.on_event(_ev("session.idle", session_id="s1", directory="/repo"))
    snap = reducer.snapshot()
    assert len(snap.feed) <= _MAX_FEED_RECORDS


def test_unknown_event_increments_counter():
    reducer = LiveActivityReducer()
    reducer.on_event(_ev("unknown.new.type"))
    snap = reducer.snapshot()
    assert snap.unknown_event_count == 1


def test_event_deduplication():
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    for _ in range(3):
        event = _ev("session.idle", session_id="s1", directory="/repo")
        event = OpenCodeEvent(
            directory="/repo", event_id="evt-1", type="session.idle", session_id="s1"
        )
        reducer.on_event(event)
    snap = reducer.snapshot()
    assert len(snap.feed) == 1


def test_set_connection():
    reducer = LiveActivityReducer()
    reducer.set_connection("live", "connected")
    snap = reducer.snapshot()
    assert snap.connection.state == "live"
    assert snap.connection.reason == "connected"


def test_server_connected_sets_live():
    reducer = LiveActivityReducer()
    reducer.on_event(_ev("server.connected"))
    snap = reducer.snapshot()
    assert snap.connection.state == "live"


def test_unregistered_session_events_after_finish_ignored():
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    reducer.unregister_invocation(ref)
    reducer.on_event(_ev("session.idle", session_id="s1", directory="/repo"))
    snap = reducer.snapshot()
    assert snap.feed == ()


def test_stale_session_different_directory_rejected():
    reducer = LiveActivityReducer()
    ref_a = _ref("s1", directory="/repo/task-a")
    ref_b = _ref("s2", directory="/repo/task-b")
    reducer.register_invocation(ref_a)
    reducer.register_invocation(ref_b)
    reducer.on_event(_ev("session.idle", session_id="s1", directory="/repo/task-b"))
    snap = reducer.snapshot()
    assert snap.feed == ()


def test_owner_thread_assertion():
    reducer = LiveActivityReducer(owner_thread=threading.current_thread())
    result = []

    def _call_from_other():
        try:
            reducer.set_connection("live", "x")
            result.append("no error")
        except RuntimeError:
            result.append("error")

    t = threading.Thread(target=_call_from_other)
    t.start()
    t.join()
    assert result == ["error"]


def test_todo_updated_requires_attribution():
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    reducer.on_event(_ev("todo.updated", session_id="s1", directory="/repo"))
    snap = reducer.snapshot()
    assert len(snap.feed) == 1

    reducer2 = LiveActivityReducer()
    reducer2.register_invocation(ref)
    reducer2.on_event(_ev("todo.updated", session_id="s1", directory="/other"))
    snap2 = reducer2.snapshot()
    assert snap2.feed == ()


def test_foreign_events_do_not_increment_unknown_counter():
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    reducer.on_event(_ev("session.idle", session_id="unknown-session", directory="/repo"))
    snap = reducer.snapshot()
    assert snap.unknown_event_count == 0
    assert snap.feed == ()


# -- integration: raw envelope -> normalize_global_event -> reducer ------


def _raw_part_updated(*, part: dict, delta: str | None = None, directory: str = "/repo") -> dict:
    props: dict = {"part": part}
    if delta is not None:
        props["delta"] = delta
    return {
        "directory": directory,
        "payload": {"type": "message.part.updated", "properties": props},
    }


def _raw_part_delta(
    *, session_id: str, message_id: str, part_id: str, field: str, delta: str, directory="/repo"
) -> dict:
    return {
        "directory": directory,
        "payload": {
            "type": "message.part.delta",
            "properties": {
                "sessionID": session_id,
                "messageID": message_id,
                "partID": part_id,
                "field": field,
                "delta": delta,
            },
        },
    }


def test_integration_canonical_text_update_produces_text_once():
    reducer = LiveActivityReducer()
    reducer.register_invocation(_ref("s1", directory="/repo"))
    raw = _raw_part_updated(
        part={
            "id": "p1",
            "sessionID": "s1",
            "messageID": "m1",
            "type": "text",
            "text": "hello world",
        },
        delta="world",
    )
    event = normalize_global_event(raw)
    reducer.on_event(event)
    msg = reducer.snapshot().invocations[0].latest_message
    assert msg is not None
    assert msg.text_tail == "hello world"


def test_integration_canonical_cumulative_updates_do_not_duplicate():
    reducer = LiveActivityReducer()
    reducer.register_invocation(_ref("s1", directory="/repo"))
    for text in ("hello", "hello world", "hello world!"):
        raw = _raw_part_updated(
            part={"id": "p1", "sessionID": "s1", "messageID": "m1", "type": "text", "text": text}
        )
        reducer.on_event(normalize_global_event(raw))
    msg = reducer.snapshot().invocations[0].latest_message
    assert msg is not None
    assert msg.text_tail == "hello world!"


def test_integration_canonical_reasoning_update_affects_only_reasoning():
    reducer = LiveActivityReducer()
    reducer.register_invocation(_ref("s1", directory="/repo"))
    raw = _raw_part_updated(
        part={
            "id": "p1",
            "sessionID": "s1",
            "messageID": "m1",
            "type": "reasoning",
            "text": "thinking hard",
        }
    )
    reducer.on_event(normalize_global_event(raw))
    msg = reducer.snapshot().invocations[0].latest_message
    assert msg is not None
    assert msg.reasoning_tail == "thinking hard"
    assert msg.text_tail == ""


def test_integration_compatibility_text_delta_appends():
    reducer = LiveActivityReducer()
    reducer.register_invocation(_ref("s1", directory="/repo"))
    for chunk in ("hello ", "world"):
        raw = _raw_part_delta(
            session_id="s1", message_id="m1", part_id="p1", field="text", delta=chunk
        )
        reducer.on_event(normalize_global_event(raw))
    msg = reducer.snapshot().invocations[0].latest_message
    assert msg is not None
    assert msg.text_tail == "hello world"


def test_integration_compatibility_reasoning_delta_appends():
    reducer = LiveActivityReducer()
    reducer.register_invocation(_ref("s1", directory="/repo"))
    for chunk in ("thinking ", "more"):
        raw = _raw_part_delta(
            session_id="s1", message_id="m1", part_id="p1", field="reasoning", delta=chunk
        )
        reducer.on_event(normalize_global_event(raw))
    msg = reducer.snapshot().invocations[0].latest_message
    assert msg is not None
    assert msg.reasoning_tail == "thinking more"


def test_integration_duplicate_event_id_not_applied_twice():
    reducer = LiveActivityReducer()
    reducer.register_invocation(_ref("s1", directory="/repo"))
    raw = _raw_part_delta(session_id="s1", message_id="m1", part_id="p1", field="text", delta="x")
    raw["payload"]["id"] = "evt-dup"
    reducer.on_event(normalize_global_event(raw))
    reducer.on_event(normalize_global_event(raw))
    msg = reducer.snapshot().invocations[0].latest_message
    assert msg is not None
    assert msg.text_tail == "x"


def test_integration_wrong_directory_delta_ignored():
    reducer = LiveActivityReducer()
    reducer.register_invocation(_ref("s1", directory="/repo/task-a"))
    raw = _raw_part_delta(
        session_id="s1",
        message_id="m1",
        part_id="p1",
        field="text",
        delta="x",
        directory="/repo/task-b",
    )
    reducer.on_event(normalize_global_event(raw))
    snap = reducer.snapshot()
    assert snap.invocations[0].latest_message is None


def test_integration_unknown_session_delta_ignored():
    reducer = LiveActivityReducer()
    reducer.register_invocation(_ref("s1", directory="/repo"))
    raw = _raw_part_delta(
        session_id="unknown-session", message_id="m1", part_id="p1", field="text", delta="x"
    )
    reducer.on_event(normalize_global_event(raw))
    snap = reducer.snapshot()
    assert snap.invocations[0].latest_message is None


def test_integration_multibyte_text_stays_valid_within_bound():
    reducer = LiveActivityReducer()
    reducer.register_invocation(_ref("s1", directory="/repo"))
    big_text = "\u00e9" * (_MAX_TEXT_TAIL)
    raw = _raw_part_updated(
        part={"id": "p1", "sessionID": "s1", "messageID": "m1", "type": "text", "text": big_text}
    )
    reducer.on_event(normalize_global_event(raw))
    msg = reducer.snapshot().invocations[0].latest_message
    assert msg is not None
    assert len(msg.text_tail.encode()) <= _MAX_TEXT_TAIL
    msg.text_tail.encode("utf-8").decode("utf-8")


def test_integration_delta_before_message_updated_still_attributes_correctly():
    reducer = LiveActivityReducer()
    reducer.register_invocation(_ref("s1", directory="/repo"))
    raw = _raw_part_delta(session_id="s1", message_id="m1", part_id="p1", field="text", delta="hi")
    reducer.on_event(normalize_global_event(raw))
    msg = reducer.snapshot().invocations[0].latest_message
    assert msg is not None
    assert msg.message_id == "m1"
    assert msg.text_tail == "hi"


def test_integration_delta_after_invocation_finished_ignored():
    reducer = LiveActivityReducer()
    ref = _ref("s1", directory="/repo")
    reducer.register_invocation(ref)
    reducer.unregister_invocation(ref)
    raw = _raw_part_delta(session_id="s1", message_id="m1", part_id="p1", field="text", delta="x")
    reducer.on_event(normalize_global_event(raw))
    snap = reducer.snapshot()
    msg = snap.invocations[0].latest_message
    assert msg is None
