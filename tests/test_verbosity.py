"""Tests for src/loop_supervisor/verbosity.py (the -v/-vv headless
diagnostics: VerboseReporter, StatsConsumer, StatsReportingObserver,
CompositeInvocationObserver)."""

from __future__ import annotations

import io
import re
import time
from pathlib import Path
from unittest.mock import MagicMock

from loop_supervisor.opencode import InvocationRef
from loop_supervisor.opencode_events import normalize_global_event
from loop_supervisor.supervisor import AdvanceOutcome, AdvanceStatus
from loop_supervisor.verbosity import (
    CompositeInvocationObserver,
    StatsConsumer,
    StatsReportingObserver,
    VerboseReporter,
    _task_label,
    _truncate,
)

_TIMESTAMP_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] ")


def _ref(session_id: str = "ses1", agent: str = "loop-builder") -> InvocationRef:
    return InvocationRef(
        session_id=session_id,
        agent=agent,
        directory=Path("/repo"),
        started_monotonic=time.monotonic(),
    )


def _fake_state(*, task_id: str | None = None, objective: str | None = None):
    state = MagicMock()
    state.original_task_id = task_id
    state.planner_result = {"objective": objective} if objective is not None else None
    return state


def _outcome(before: str, after: str, *, state=None) -> AdvanceOutcome:
    return AdvanceOutcome(
        status=AdvanceStatus.ADVANCED,
        state=state if state is not None else _fake_state(),
        phase_before=before,
        phase_after=after,
    )


# --- _truncate / _task_label -------------------------------------------------


def test_truncate_leaves_short_text_unchanged():
    assert _truncate("short") == "short"


def test_truncate_cuts_at_80_chars_with_ellipsis():
    text = "x" * 200
    result = _truncate(text)
    assert len(result) == 80
    assert result.endswith("\u2026")


def test_truncate_strips_surrounding_whitespace():
    assert _truncate("  padded  ") == "padded"


def test_task_label_prefers_task_id_and_objective_combined():
    state = _fake_state(task_id="task-007", objective="Add the frobnicator")
    assert _task_label(state) == "task-007: Add the frobnicator"


def test_task_label_falls_back_to_objective_only():
    state = _fake_state(task_id=None, objective="Add the frobnicator")
    assert _task_label(state) == "Add the frobnicator"


def test_task_label_falls_back_to_task_id_only():
    state = _fake_state(task_id="task-007", objective=None)
    assert _task_label(state) == "task-007"


def test_task_label_empty_when_nothing_available():
    state = _fake_state(task_id=None, objective=None)
    assert _task_label(state) == ""


def test_task_label_truncates_long_combined_label():
    state = _fake_state(task_id="task-007", objective="x" * 200)
    label = _task_label(state)
    assert len(label) == 80


def test_task_label_never_raises_on_missing_attributes():
    """A bare object with none of the expected attributes must not crash
    the reporting hook -- reporting must never be able to fail a run."""

    class _Bare:
        pass

    assert _task_label(_Bare()) == ""


# --- VerboseReporter: invocation lines --------------------------------------


def test_invocation_started_prints_timestamped_agent_line():
    stream = io.StringIO()
    reporter = VerboseReporter(stream=stream)
    reporter.invocation_started(_ref(agent="loop-planner"))
    output = stream.getvalue()
    assert _TIMESTAMP_RE.match(output)
    assert "loop-planner started" in output


def test_invocation_finished_reports_elapsed_time_on_success():
    stream = io.StringIO()
    reporter = VerboseReporter(stream=stream)
    ref = _ref(agent="loop-builder")
    reporter.invocation_started(ref)
    time.sleep(0.05)
    reporter.invocation_finished(ref, None)
    output = stream.getvalue()
    assert "loop-builder finished (" in output
    assert "s)" in output


def test_invocation_finished_reports_error_class_and_message_on_failure():
    stream = io.StringIO()
    reporter = VerboseReporter(stream=stream)
    ref = _ref(agent="loop-auditor")
    reporter.invocation_started(ref)
    reporter.invocation_finished(ref, ValueError("boom"))
    output = stream.getvalue()
    assert "loop-auditor failed" in output
    assert "ValueError: boom" in output


def test_invocation_finished_without_matching_start_does_not_raise():
    """finished for a session id never seen by started() (e.g. the
    observer was attached mid-invocation) must degrade gracefully, not
    raise -- a reporting hook must never be able to fail the run it is
    only observing."""
    stream = io.StringIO()
    reporter = VerboseReporter(stream=stream)
    reporter.invocation_finished(_ref(session_id="never-started"), None)
    assert "finished" in stream.getvalue()


def test_reporter_print_never_raises_even_if_stream_is_broken():
    class _BoomStream:
        def write(self, *a, **k):
            raise OSError("broken pipe")

        def flush(self):
            pass

    reporter = VerboseReporter(stream=_BoomStream())  # type: ignore[arg-type]
    reporter.invocation_started(_ref())  # must not raise


# --- VerboseReporter: phase transitions --------------------------------------


def test_on_advance_prints_phase_transition_with_task_label():
    stream = io.StringIO()
    reporter = VerboseReporter(stream=stream)
    state = _fake_state(task_id="task-001", objective="Do the thing")
    reporter.on_advance(_outcome("planning", "building", state=state))
    output = stream.getvalue()
    assert _TIMESTAMP_RE.match(output)
    assert "planning -> building" in output
    assert "task-001: Do the thing" in output


def test_on_advance_skips_line_when_phase_unchanged():
    """An advance() call that loops without a phase transition (e.g.
    INPUT_REQUIRED) must not print a same-phase 'X -> X' line, which
    would be noise rather than signal."""
    stream = io.StringIO()
    reporter = VerboseReporter(stream=stream)
    reporter.on_advance(_outcome("awaiting_input", "awaiting_input"))
    assert stream.getvalue() == ""


def test_on_advance_omits_label_when_no_task_available():
    stream = io.StringIO()
    reporter = VerboseReporter(stream=stream)
    reporter.on_advance(_outcome("planning", "done", state=_fake_state()))
    output = stream.getvalue().strip()
    assert output.endswith("planning -> done")


# --- StatsConsumer ------------------------------------------------------------


def _event(raw: dict):
    return normalize_global_event(raw)


def _part_updated_event(session_id: str, *, delta: str | None = "hello") -> dict:
    return {
        "directory": "/repo",
        "payload": {
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "id": "part1",
                    "sessionID": session_id,
                    "messageID": "msg1",
                    "type": "text",
                    "text": "hello",
                },
                "delta": delta,
            },
        },
    }


def _tool_event(session_id: str) -> dict:
    return {
        "directory": "/repo",
        "payload": {
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "id": "part2",
                    "sessionID": session_id,
                    "messageID": "msg1",
                    "type": "tool",
                    "callID": "call1",
                    "tool": "bash",
                    "state": {"status": "running", "title": "bash"},
                }
            },
        },
    }


def test_stats_consumer_counts_events_and_parts():
    stats = StatsConsumer()
    raw = _part_updated_event("ses1")
    stats.on_event(raw, _event(raw))
    line = stats.pop_summary_line("ses1", agent="loop-builder")
    assert line is not None
    assert "events=1" in line
    assert "parts=1" in line


def test_stats_consumer_counts_tool_state_separately_from_parts():
    stats = StatsConsumer()
    raw = _tool_event("ses1")
    stats.on_event(raw, _event(raw))
    line = stats.pop_summary_line("ses1", agent="loop-builder")
    assert line is not None
    assert "tool_states=1" in line


def test_stats_consumer_ignores_events_without_a_session_id():
    stats = StatsConsumer()
    raw = {"directory": "/repo", "payload": {"type": "server.connected"}}
    stats.on_event(raw, _event(raw))
    assert stats.pop_summary_line("ses1", agent="loop-builder") is None


def test_stats_consumer_all_event_gap_tracks_min_mean_max():
    stats = StatsConsumer()
    raw = _tool_event("ses1")
    event = _event(raw)
    stats.on_event(raw, event)
    time.sleep(0.05)
    stats.on_event(raw, event)
    time.sleep(0.1)
    stats.on_event(raw, event)
    line = stats.pop_summary_line("ses1", agent="loop-builder")
    assert line is not None
    assert "all-gap[n=2" in line


def test_stats_consumer_delta_gap_is_separate_from_all_event_gap():
    """A session that emits frequent tool-state events but infrequent
    text deltas must show a small all-event gap and a larger delta gap
    -- this divergence is the whole point of tracking both."""
    stats = StatsConsumer()
    tool_raw = _tool_event("ses1")
    tool_event = _event(tool_raw)
    delta_raw = _part_updated_event("ses1", delta="chunk")
    delta_event = _event(delta_raw)

    stats.on_event(delta_raw, delta_event)
    stats.on_event(tool_raw, tool_event)
    stats.on_event(tool_raw, tool_event)
    stats.on_event(tool_raw, tool_event)
    time.sleep(0.05)
    stats.on_event(delta_raw, delta_event)

    line = stats.pop_summary_line("ses1", agent="loop-builder")
    assert line is not None
    assert "delta-gap[n=1" in line
    assert "all-gap[n=4" in line


def test_stats_consumer_message_part_updated_without_delta_does_not_count_as_delta():
    stats = StatsConsumer()
    raw = _part_updated_event("ses1", delta=None)
    stats.on_event(raw, _event(raw))
    raw2 = _part_updated_event("ses1", delta=None)
    stats.on_event(raw2, _event(raw2))
    line = stats.pop_summary_line("ses1", agent="loop-builder")
    assert line is not None
    assert "delta-gap[n=0]" in line


def test_stats_consumer_pop_summary_line_clears_state():
    """Popping a session's summary must discard it, so a later
    invocation reusing the same session id (should never happen in
    practice, but defensively) starts from a clean slate rather than
    inheriting stale counts."""
    stats = StatsConsumer()
    raw = _tool_event("ses1")
    stats.on_event(raw, _event(raw))
    first = stats.pop_summary_line("ses1", agent="loop-builder")
    assert first is not None
    assert stats.pop_summary_line("ses1", agent="loop-builder") is None


def test_stats_consumer_multiple_sessions_tracked_independently():
    stats = StatsConsumer()
    raw1 = _tool_event("ses1")
    raw2 = _tool_event("ses2")
    stats.on_event(raw1, _event(raw1))
    stats.on_event(raw2, _event(raw2))
    stats.on_event(raw2, _event(raw2))

    line1 = stats.pop_summary_line("ses1", agent="loop-planner")
    line2 = stats.pop_summary_line("ses2", agent="loop-builder")
    assert line1 is not None and "events=1" in line1
    assert line2 is not None and "events=2" in line2


# --- StatsReportingObserver bridge -------------------------------------------


def test_stats_reporting_observer_prints_summary_on_finish():
    stream = io.StringIO()
    stats = StatsConsumer()
    raw = _tool_event("ses1")
    stats.on_event(raw, _event(raw))

    observer = StatsReportingObserver(stats, stream=stream)
    observer.invocation_started(_ref(session_id="ses1"))  # no-op, must not raise
    observer.invocation_finished(_ref(session_id="ses1", agent="loop-auditor"), None)

    output = stream.getvalue()
    assert _TIMESTAMP_RE.match(output)
    assert "loop-auditor events=1" in output


def test_stats_reporting_observer_silent_when_no_events_seen():
    stream = io.StringIO()
    stats = StatsConsumer()
    observer = StatsReportingObserver(stats, stream=stream)
    observer.invocation_finished(_ref(session_id="never-seen"), None)
    assert stream.getvalue() == ""


# --- CompositeInvocationObserver ---------------------------------------------


def test_composite_observer_notifies_all_observers_in_order():
    calls: list[str] = []

    class _Recorder:
        def __init__(self, name):
            self._name = name

        def invocation_started(self, invocation):
            calls.append(f"{self._name}:started")

        def invocation_finished(self, invocation, error):
            calls.append(f"{self._name}:finished")

    composite = CompositeInvocationObserver([_Recorder("a"), _Recorder("b")])
    composite.invocation_started(_ref())
    composite.invocation_finished(_ref(), None)
    assert calls == ["a:started", "b:started", "a:finished", "b:finished"]


def test_composite_observer_one_raising_does_not_block_others():
    calls: list[str] = []

    class _Boom:
        def invocation_started(self, invocation):
            raise RuntimeError("boom")

        def invocation_finished(self, invocation, error):
            raise RuntimeError("boom")

    class _Recorder:
        def invocation_started(self, invocation):
            calls.append("started")

        def invocation_finished(self, invocation, error):
            calls.append("finished")

    composite = CompositeInvocationObserver([_Boom(), _Recorder()])
    composite.invocation_started(_ref())
    composite.invocation_finished(_ref(), None)
    assert calls == ["started", "finished"]
