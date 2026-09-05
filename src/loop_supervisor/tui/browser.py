"""Read-only Textual screens for browsing persisted supervisor runs."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, ListItem, ListView, Static

from ..read_model.current_run import CurrentRun, load_current_run
from ..read_model.discovery import RunSummary
from ..read_model.history import HistoryEntry, HistoryLoad, HistoryStatus, load_history
from ..read_model.lock_observation import ActivityLabel, LockActivity, LockObservation
from ..read_model.snapshot import ProjectSnapshot, build_snapshot


class RunBrowserApp(App[None]):
    """Browse one immutable project snapshot and inspect authoritative run details."""

    _MAX_TIMELINE_RENDERED_BYTES = 256 * 1024
    _MAX_TIMELINE_RENDERED_LINES = 10_000
    _TIMELINE_TRUNCATION_MARKER = "Timeline output truncated: rendered-output limit reached."

    TITLE = "Loop Supervisor"
    SUB_TITLE = "Run browser"
    BINDINGS = [("q", "quit", "Quit"), ("b", "back", "Back"), ("r", "refresh", "Refresh")]
    CSS = """
    #run-browser, #run-detail {
        padding: 1 2;
    }

    .run-row {
        margin-bottom: 1;
    }
    """

    def __init__(self, snapshot: ProjectSnapshot) -> None:
        super().__init__()
        self._snapshot = snapshot
        self._selected_run_id: str | None = None
        self._run_id_by_row_index = tuple(summary.run_id for summary in snapshot.runs)

    def compose(self) -> ComposeResult:
        yield Header()
        if self._selected_run_id is None:
            yield from self._compose_browser()
        else:
            yield from self._compose_detail(self._selected_run_id)
        yield Footer()

    def on_mount(self) -> None:
        """Give keyboard users a selected row as soon as the browser appears."""
        if self._selected_run_id is None and self._snapshot.runs:
            self.query_one(ListView).focus()

    def _compose_browser(self) -> ComposeResult:
        with VerticalScroll(id="run-browser"):
            yield Static(
                f"Project: {self._snapshot.project.integration_root}",
                markup=False,
                classes="project-path",
            )
            if not self._snapshot.runs:
                yield Static("No discovered runs.", markup=False, classes="empty-runs")
            else:
                yield ListView(
                    *(
                        ListItem(
                            Static(
                                self._render_run(summary, self._snapshot.lock),
                                markup=False,
                                classes="run-row",
                            ),
                        )
                        for summary in self._snapshot.runs
                    ),
                    id="run-list",
                )
            for diagnostic in self._snapshot.diagnostics:
                yield Static(diagnostic.message, markup=False, classes="snapshot-diagnostic")

    def _compose_detail(self, run_id: str) -> ComposeResult:
        current = load_current_run(self._snapshot.project.git_common_dir, run_id)
        history = load_history(self._snapshot.project.git_common_dir, run_id)
        with VerticalScroll(id="run-detail"):
            yield Static("Run detail — press b to return to the browser.", markup=False)
            yield Static(
                self._render_current_run(current, self._snapshot.lock),
                markup=False,
                classes="run-detail-summary",
            )
            yield Static(self._render_history(history), markup=False, classes="run-detail-timeline")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Open the selected run using the authoritative current-state reader."""
        self._selected_run_id = self._run_id_by_row_index[event.index]
        self.call_after_refresh(self._show_selected_run)

    def _show_selected_run(self) -> None:
        """Replace the browser widgets after Textual has handled list selection."""
        self.refresh(recompose=True)

    def action_refresh(self) -> None:
        """Replace the displayed snapshot with a fresh disk scan by selected run ID."""
        selected_run_id = self._selected_run_id
        self._snapshot = build_snapshot(self._snapshot.project)
        self._run_id_by_row_index = tuple(summary.run_id for summary in self._snapshot.runs)
        if selected_run_id not in self._run_id_by_row_index:
            self._selected_run_id = None
        self.refresh(recompose=True)
        if self._selected_run_id is None and self._snapshot.runs:
            self.call_after_refresh(self._focus_run_list)

    async def action_back(self) -> None:
        """Return from a run detail to the immutable browser snapshot."""
        if self._selected_run_id is not None:
            self._selected_run_id = None
            self.refresh(recompose=True)
            self.call_after_refresh(self._focus_run_list)

    def _focus_run_list(self) -> None:
        """Restore keyboard navigation after the browser has been recomposed."""
        self.query_one(ListView).focus()

    @staticmethod
    def _render_run(summary: RunSummary, lock: LockObservation) -> str:
        """Return literal durable state and lock evidence without inferring activity."""
        activity = RunBrowserApp._activity_label(summary.run_id, lock)
        if not summary.loadable:
            return (
                f"{summary.run_id} — unloadable: {summary.diagnostic or 'unavailable'} — "
                f"Activity: {activity}"
            )
        return (
            f"{summary.run_id} — {summary.phase or 'unknown'} — "
            f"updated {summary.updated_at or 'unavailable'} — Activity: {activity}"
        )

    @staticmethod
    def _activity_label(run_id: str, lock: LockObservation) -> str:
        """Render only the per-run classification returned by the lock reader."""
        label = next((item.label for item in lock.activities if item.run_id == run_id), None)
        if label is ActivityLabel.RUNNING:
            return "running"
        if lock.activity is LockActivity.ABSENT:
            return "not evidenced running (inactive at inspection time)"
        return "not evidenced running"

    @staticmethod
    def _render_lock_observation(lock: LockObservation) -> tuple[str, ...]:
        """Render safe repository-level evidence without exposing lock ownership credentials."""
        lines = [f"Lock observation: {lock.activity.value.replace('_', ' ')}"]
        if lock.started_at is not None:
            lines.append(f"Lock started: {lock.started_at}")
        if lock.hostname is not None:
            lines.append(f"Lock hostname: {lock.hostname}")
        if lock.pid is not None:
            lines.append(f"Lock PID: {lock.pid}")
        if lock.operation is not None:
            lines.append(f"Lock operation: {lock.operation}")
        if lock.run_id is not None:
            lines.append(f"Lock association: {lock.run_id}")
        if lock.integration_path is not None:
            lines.append(f"Lock integration path: {lock.integration_path}")
        if lock.diagnostic is not None:
            lines.append(f"Lock diagnostic: {lock.diagnostic}")
        return tuple(lines)

    @classmethod
    def _render_history(cls, history: HistoryLoad) -> str:
        """Render bounded best-effort workflow evidence without inferring transitions."""
        if history.completeness is HistoryStatus.ABSENT:
            return "Workflow timeline: unavailable (no recorded history)."

        status_line = f"Workflow timeline: {history.completeness.value}"
        diagnostic_lines = [
            f"  {diagnostic.artifact}: {diagnostic.reason}" for diagnostic in history.diagnostics
        ]
        entry_lines = [
            line for entry in history.entries for line in cls._render_history_entry(entry)
        ]
        evidence_lines = [
            *(["Timeline diagnostics:", *diagnostic_lines] if diagnostic_lines else []),
            *entry_lines,
        ]
        return cls._bound_history_lines(status_line, evidence_lines)

    @staticmethod
    def _render_history_entry(entry: HistoryEntry) -> tuple[str, ...]:
        """Render one validated history entry as literal timeline lines."""
        counters = entry.counters
        return (
            f"Sequence {entry.seq}",
            f"  Phase: {entry.phase} → {entry.phase_after}",
            f"  Outcome: {entry.status.value}",
            f"  Recorded: {entry.recorded_at}",
            "  Counters: "
            f"accepted tasks={counters['accepted_task_count']}, "
            f"revisions={counters['revision_count']}, "
            f"replans={counters['replan_count']}, "
            f"architect retries={counters['architect_retry_count']}, "
            f"builder guidance={counters['builder_guidance_count']}",
            "  Result: "
            f"{'available' if entry.has_result else 'unavailable'}; "
            f"Error: {'available' if entry.has_error else 'unavailable'}",
        )

    @classmethod
    def _bound_history_lines(cls, status_line: str, evidence_lines: list[str]) -> str:
        """Limit timeline output while retaining the status and bounded evidence."""
        complete_lines = [status_line, *evidence_lines]
        if cls._within_history_limits(complete_lines):
            return "\n".join(complete_lines)

        marker = cls._TIMELINE_TRUNCATION_MARKER
        available_lines = cls._MAX_TIMELINE_RENDERED_LINES - 2
        available_bytes = cls._MAX_TIMELINE_RENDERED_BYTES - sum(
            len(line.encode("utf-8")) + 1 for line in (status_line, marker)
        )
        rendered_evidence: list[str] = []
        for line in evidence_lines:
            line_bytes = len(line.encode("utf-8")) + 1
            if len(rendered_evidence) == available_lines or line_bytes > available_bytes:
                break
            rendered_evidence.append(line)
            available_bytes -= line_bytes
        return "\n".join((status_line, *rendered_evidence, marker))

    @classmethod
    def _within_history_limits(cls, lines: list[str]) -> bool:
        """Return whether rendered literal lines fit the ADR display limits."""
        return len(lines) <= cls._MAX_TIMELINE_RENDERED_LINES and (
            sum(len(line.encode("utf-8")) for line in lines) + len(lines) - 1
            <= cls._MAX_TIMELINE_RENDERED_BYTES
        )

    @staticmethod
    def _render_current_run(current: CurrentRun, lock: LockObservation) -> str:
        """Render validated state separately from safe, evidence-based lock information."""
        activity = RunBrowserApp._activity_label(current.run_id, lock)
        if not current.loadable:
            return "\n".join(
                (
                    f"Run ID: {current.run_id}",
                    "Details: unavailable",
                    f"Activity: {activity}",
                    *RunBrowserApp._render_lock_observation(lock),
                    current.diagnostic or "Unavailable.",
                )
            )
        return "\n".join(
            (
                f"Run ID: {current.run_id}",
                f"Durable phase: {current.phase}",
                f"Activity: {activity}",
                *RunBrowserApp._render_lock_observation(lock),
                f"Created: {current.created_at}",
                f"Updated: {current.updated_at}",
                f"Integration branch: {current.integration_branch}",
                f"Current task: {current.current_task_id or 'unavailable'}",
                "Loop counters:",
                f"  Accepted tasks: {current.accepted_task_count}",
                f"  Revisions: {current.revision_count}",
                f"  Replans: {current.replan_count}",
                f"  Architect retries: {current.architect_retry_count}",
                f"  Builder guidance: {current.builder_guidance_count}",
                f"Pending question: {current.pending_question or 'unavailable'}",
                f"Latest operational error: {current.latest_operational_error or 'unavailable'}",
            )
        )
