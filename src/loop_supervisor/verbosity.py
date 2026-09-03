"""Operator-facing `-v`/`-vv` diagnostics for headless `run`/`resume`.

Before this module, a headless run printed exactly one line for the
whole invocation ("session monitor watching ...") and then nothing
until it either finished or hit `role_timeout` -- there was no way to
tell a slow-but-working phase apart from a stalled one without
attaching a TUI. This module adds two cumulative, ssh-style verbosity
levels, entirely to stderr so stdout's `run_id:`/`final phase:` lines
stay machine-parseable:

- `-v`: one timestamped line per agent invocation start/finish (with the
  task/objective, truncated to 80 chars) and one per phase transition.
- `-vv` (implies `-v`): adds one end-of-invocation summary line of
  event-timing statistics gathered from the same `/global/event` stream
  `SessionMonitor` already subscribes to.

Everything here is strictly observational: nothing in this module
aborts, retries, delays, or otherwise influences a session or a phase.
`-vv`'s statistics exist to help a human judge "slow vs. stalled" after
the fact (or while watching a live log), not to make that judgement or
act on it automatically -- see ADR 0033.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TextIO

from .opencode import InvocationObserver, InvocationRef
from .opencode_events import OpenCodeEvent
from .supervisor import AdvanceOutcome

_TASK_LABEL_MAX_LEN = 80


def _timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _truncate(text: str, *, max_len: int = _TASK_LABEL_MAX_LEN) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "\u2026"


def _task_label(state: Any) -> str:
    """Best-effort single-line label for the run's current task, truncated
    to `_TASK_LABEL_MAX_LEN` chars. Falls back gracefully: an in-progress
    or COMPLETE planner result may have no objective at all (e.g. the
    planner just reported COMPLETE), in which case the original task id
    alone, or nothing, is shown -- never an exception from a reporting
    hook.
    """
    objective: str | None = None
    planner_result = getattr(state, "planner_result", None)
    if isinstance(planner_result, dict):
        raw_objective = planner_result.get("objective")
        if isinstance(raw_objective, str) and raw_objective:
            objective = raw_objective

    task_id = getattr(state, "original_task_id", None)

    if objective and task_id:
        return _truncate(f"{task_id}: {objective}")
    if objective:
        return _truncate(objective)
    if task_id:
        return _truncate(str(task_id))
    return ""


class VerboseReporter:
    """`-v` reporter: timestamped invocation start/finish and phase
    transition lines to stderr.

    Implements `InvocationObserver` (attach via
    `RunSession`/`OpenCodeServer.add_observer`) for invocation lines, and
    exposes `on_advance` (pass as `Supervisor.run(on_advance=...)`) for
    phase-transition lines. Both are independent hooks so a caller can
    use either without the other, though `-v`/`-vv` always wire up both.

    Never raises: every print is best-effort, matching the "an
    observability hook must not be able to fail the run" contract every
    other observer in this codebase already follows
    (`OpenCodeServer._notify_started`/`_notify_finished` catch observer
    exceptions at that layer; this class simply never gives them a
    reason to).
    """

    def __init__(self, *, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._lock = threading.Lock()
        self._started_monotonic: dict[str, float] = {}

    def _print(self, message: str) -> None:
        try:
            print(f"[{_timestamp()}] {message}", file=self._stream)
        except Exception:
            pass

    def invocation_started(self, invocation: InvocationRef) -> None:
        with self._lock:
            self._started_monotonic[invocation.session_id] = invocation.started_monotonic
        self._print(f"{invocation.agent} started")

    def invocation_finished(
        self,
        invocation: InvocationRef,
        error: BaseException | None,
    ) -> None:
        with self._lock:
            started = self._started_monotonic.pop(invocation.session_id, None)
        elapsed = time.monotonic() - started if started is not None else None
        elapsed_label = f"{elapsed:.1f}s" if elapsed is not None else "?s"
        if error is None:
            self._print(f"{invocation.agent} finished ({elapsed_label})")
        else:
            self._print(
                f"{invocation.agent} failed ({elapsed_label}): {type(error).__name__}: {error}"
            )

    def on_advance(self, outcome: AdvanceOutcome) -> None:
        label = _task_label(outcome.state)
        suffix = f"  {label}" if label else ""
        if outcome.phase_before == outcome.phase_after:
            return
        self._print(f"{outcome.phase_before} -> {outcome.phase_after}{suffix}")


@dataclass
class _GapStats:
    """Streaming min/mean/max/count over a sequence of gap durations.

    Deliberately O(1) per sample and retains no history: only running
    aggregates are kept, matching the "diagnostic-only, no retained
    samples" scope for this pass. Mean is a running sum/count, not an
    online-variance formula, since only the mean is reported.
    """

    count: int = 0
    total: float = 0.0
    minimum: float | None = None
    maximum: float | None = None

    def add(self, gap: float) -> None:
        self.count += 1
        self.total += gap
        self.minimum = gap if self.minimum is None else min(self.minimum, gap)
        self.maximum = gap if self.maximum is None else max(self.maximum, gap)

    @property
    def mean(self) -> float | None:
        if self.count == 0:
            return None
        return self.total / self.count

    def summary(self) -> str:
        if self.count == 0:
            return "n=0"
        mean = self.mean
        assert mean is not None
        assert self.minimum is not None
        assert self.maximum is not None
        return f"n={self.count} min/mean/max={self.minimum:.1f}/{mean:.1f}/{self.maximum:.1f}s"


@dataclass
class _SessionStats:
    event_count: int = 0
    part_count: int = 0
    tool_state_count: int = 0
    all_gap: _GapStats = field(default_factory=_GapStats)
    delta_gap: _GapStats = field(default_factory=_GapStats)
    last_event_monotonic: float | None = None
    last_delta_monotonic: float | None = None


class StatsConsumer:
    """`-vv` consumer: gathers event-timing statistics per active session
    from the same `/global/event` stream `SessionMonitor` already
    subscribes to, and prints one summary line to stderr when each
    invocation finishes.

    Tracks two gap tracks per the design:

    - "all-event" gap: time between any two consecutive events for a
      session, regardless of type -- the overall liveness signal.
    - "delta" gap: time between consecutive `message.part.updated`
      events that carry `delta` text (token-level output) -- the "is the
      model actually streaming tokens" signal. A large gap here while
      the all-event gap stays small usually means a tool call is running
      (tool-state events keep arriving; no new tokens do); a large gap
      in *both* is the closer approximation of the "indicator went dark"
      symptom this was built to help diagnose.

    Strictly observational: computing and printing these numbers is the
    entire behavior. Nothing here acts on them.
    """

    def __init__(self, *, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._lock = threading.Lock()
        self._by_session: dict[str, _SessionStats] = {}

    def on_event(self, raw_event: dict[str, Any], event: OpenCodeEvent) -> None:
        session_id = event.session_id
        if not session_id:
            return
        now = time.monotonic()
        with self._lock:
            stats = self._by_session.setdefault(session_id, _SessionStats())
            stats.event_count += 1
            if stats.last_event_monotonic is not None:
                stats.all_gap.add(now - stats.last_event_monotonic)
            stats.last_event_monotonic = now

            if event.part is not None:
                stats.part_count += 1
                if event.part.tool_state is not None:
                    stats.tool_state_count += 1

            if event.type == "message.part.updated" and event.delta is not None:
                if stats.last_delta_monotonic is not None:
                    stats.delta_gap.add(now - stats.last_delta_monotonic)
                stats.last_delta_monotonic = now

    def pop_summary_line(self, session_id: str, *, agent: str) -> str | None:
        """Return (and discard) the accumulated summary for `session_id`,
        or None if no events were ever observed for it (e.g. the
        invocation failed before the server emitted anything, or this
        consumer was attached after the session already finished).
        """
        with self._lock:
            stats = self._by_session.pop(session_id, None)
        if stats is None or stats.event_count == 0:
            return None
        silence = (
            f"{time.monotonic() - stats.last_event_monotonic:.1f}s"
            if stats.last_event_monotonic is not None
            else "?s"
        )
        return (
            f"{agent} events={stats.event_count} parts={stats.part_count} "
            f"tool_states={stats.tool_state_count} "
            f"all-gap[{stats.all_gap.summary()}] "
            f"delta-gap[{stats.delta_gap.summary()}] "
            f"silence_at_end={silence}"
        )


class StatsReportingObserver:
    """Bridges `StatsConsumer` (an SSE-stream consumer, keyed by
    session id) to `InvocationObserver` (keyed by the same session id,
    notified on finish) so a `-vv` summary line prints exactly once per
    invocation, right after its own start/finish lines from
    `VerboseReporter`.

    A separate class rather than a method on `StatsConsumer` because the
    two live on different call paths (the SSE worker thread delivers
    events; `OpenCodeServer` calls `invocation_finished` on whichever
    thread ran the invocation) and have different lifetimes (a
    `StatsConsumer` outlives any single invocation; this observer's only
    job is the finish-time bridge).
    """

    def __init__(self, stats: StatsConsumer, *, stream: TextIO | None = None) -> None:
        self._stats = stats
        self._stream = stream if stream is not None else sys.stderr

    def invocation_started(self, invocation: InvocationRef) -> None:
        return None

    def invocation_finished(
        self,
        invocation: InvocationRef,
        error: BaseException | None,
    ) -> None:
        line = self._stats.pop_summary_line(invocation.session_id, agent=invocation.agent)
        if line is None:
            return
        try:
            print(f"[{_timestamp()}] {line}", file=self._stream)
        except Exception:
            pass


class CompositeInvocationObserver:
    """Fan an `InvocationObserver` notification out to several observers.

    `OpenCodeServer.add_observer` takes exactly one `server_observer` at
    session construction; `-v`/`-vv` need to install both a
    `VerboseReporter` and (at `-vv`) a `StatsReportingObserver` on the
    same server. Mirrors `SessionMonitor`'s own fan-out contract: each
    observer is notified regardless of whether an earlier one raised,
    and a raising observer never reaches the caller (matching
    `OpenCodeServer._notify_started`/`_notify_finished`'s own
    try/except-per-observer loop, which this composite sits behind).
    """

    def __init__(self, observers: list[InvocationObserver]) -> None:
        self._observers = list(observers)

    def invocation_started(self, invocation: InvocationRef) -> None:
        for observer in self._observers:
            try:
                observer.invocation_started(invocation)
            except Exception:
                pass

    def invocation_finished(
        self,
        invocation: InvocationRef,
        error: BaseException | None,
    ) -> None:
        for observer in self._observers:
            try:
                observer.invocation_finished(invocation, error)
            except Exception:
                pass
