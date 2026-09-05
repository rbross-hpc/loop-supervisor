"""Read-only Textual screens for browsing persisted supervisor runs."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, ListItem, ListView, Static

from ..read_model import verification
from ..read_model.current_run import CurrentRun
from ..read_model.discovery import RunSummary
from ..read_model.history import HistoryEntry, HistoryLoad, HistoryStatus
from ..read_model.lock_observation import ActivityLabel, LockActivity, LockObservation
from ..read_model.snapshot import ProjectSnapshot, build_snapshot


class RunBrowserApp(App[None]):
    """Browse one immutable project snapshot and inspect authoritative run details."""

    _MAX_TIMELINE_RENDERED_BYTES = 256 * 1024
    _MAX_TIMELINE_RENDERED_LINES = 10_000
    _TIMELINE_TRUNCATION_MARKER = "Timeline output truncated: rendered-output limit reached."
    _MAX_SUMMARY_RENDERED_BYTES = 256 * 1024
    _MAX_SUMMARY_RENDERED_LINES = 10_000
    _SUMMARY_TRUNCATION_MARKER = "Summary output truncated: rendered-output limit reached."
    _MAX_RECORD_DETAIL_RENDERED_BYTES = 256 * 1024
    _MAX_RECORD_DETAIL_RENDERED_LINES = 10_000
    _RECORD_DETAIL_TRUNCATION_MARKER = (
        "Record detail output truncated: rendered-output limit reached."
    )
    _RAW_JSON_TRUNCATION_MARKER = "Raw JSON output truncated: rendered-output limit reached."
    _MAX_VERIFICATION_RENDERED_BYTES = 256 * 1024
    _MAX_VERIFICATION_RENDERED_LINES = 10_000
    _VERIFICATION_TRUNCATION_MARKER = (
        "Verification output truncated: rendered-output limit reached."
    )
    _LOG_VIEWER_TRUNCATION_MARKER = (
        "Verification log viewer truncated: rendered-output limit reached."
    )
    _SENSITIVE_LOG_WARNING = "WARNING: Verification output is unredacted and potentially sensitive."

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
        self._selected_record_index: int | None = None
        self._selected_log_reference: verification.LogReference | None = None
        self._opened_log: verification.LogContent | None = None
        self._raw_json_expanded = False
        self._detail_records: tuple[CurrentRun | HistoryEntry, ...] = ()
        self._openable_logs: tuple[verification.LogReference, ...] = ()
        self._run_id_by_row_index = tuple(summary.run_id for summary in snapshot.runs)

    def compose(self) -> ComposeResult:
        yield Header()
        if self._selected_run_id is None:
            yield from self._compose_browser()
        elif self._selected_log_reference is not None:
            yield from self._compose_log_viewer()
        elif self._selected_record_index is None:
            yield from self._compose_detail(self._selected_run_id)
        else:
            yield from self._compose_record_detail()
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
        detail = self._snapshot.detail_for(run_id)
        current = detail.current
        history = detail.history
        discovered_verification = detail.verification
        with VerticalScroll(id="run-detail"):
            yield Static("Run detail — press b to return to the browser.", markup=False)
            yield Static(
                self._render_current_run(current, self._snapshot.lock),
                markup=False,
                classes="run-detail-summary",
            )
            yield Static(self._render_history(history), markup=False, classes="run-detail-timeline")
            yield Static(
                self._render_verification(discovered_verification),
                markup=False,
                classes="run-detail-verification",
            )
            self._openable_logs = tuple(
                attempt.log
                for attempt in discovered_verification.attempts
                if attempt.log is not None
            )
            self._detail_records = (current, *history.entries)
            record_rows = (
                ListItem(Static(self._record_label(record), markup=False))
                for record in self._detail_records
            )
            yield ListView(*record_rows, id="record-list")
            if self._openable_logs:
                log_rows = (
                    ListItem(
                        Static(
                            f"Attempt {reference.ordinal} log (open; unredacted sensitive output)",
                            markup=False,
                        )
                    )
                    for reference in self._openable_logs
                )
                yield ListView(*log_rows, id="verification-log-list")

    def _compose_record_detail(self) -> ComposeResult:
        record = self._detail_records[self._selected_record_index or 0]
        with VerticalScroll(id="record-detail"):
            yield Static("Record detail — press b to return to the run detail.", markup=False)
            yield Static(self._render_record_detail(record), markup=False, classes="record-detail")
            if self._raw_json_expanded:
                yield Static(
                    self._render_raw_json(record),
                    markup=False,
                    classes="record-detail-raw-json",
                )

    def _compose_log_viewer(self) -> ComposeResult:
        """Render one explicitly requested log through Textual's literal-text path."""
        assert self._opened_log is not None
        with VerticalScroll(id="verification-log-viewer"):
            yield Static("Verification log — press b to return to the run detail.", markup=False)
            yield Static(
                self._render_log_content(self._opened_log),
                markup=False,
                classes="verification-log-viewer",
            )

    @classmethod
    def _render_log_content(cls, content: verification.LogContent) -> str:
        """Render the explicit, unredacted bounded-read result with its evidence markers."""
        lines = [cls._SENSITIVE_LOG_WARNING]
        if not content.available:
            lines.append(f"Verification log: unavailable ({content.diagnostic or 'unavailable'}).")
            return "\n".join(lines)
        if content.byte_truncated:
            lines.append("Verification log byte truncated: read limit reached.")
        if content.render_truncated:
            lines.append("Verification log render truncated: rendered-output limit reached.")
        if content.changed_during_read:
            lines.append("Verification log changed during read: content may be inconsistent.")
        lines.append(content.text)
        rendered = "\n".join(lines)
        if (
            len(rendered.encode("utf-8")) <= cls._MAX_VERIFICATION_RENDERED_BYTES
            and len(rendered.splitlines()) <= cls._MAX_VERIFICATION_RENDERED_LINES
        ):
            return rendered
        marker = cls._LOG_VIEWER_TRUNCATION_MARKER
        payload = cls._truncate_literal(
            rendered,
            cls._MAX_VERIFICATION_RENDERED_BYTES - len(marker.encode("utf-8")) - 1,
            cls._MAX_VERIFICATION_RENDERED_LINES - 1,
        )
        separator = "" if cls._ends_with_line_separator(payload) else "\n"
        return f"{payload}{separator}{marker}"

    @classmethod
    def _render_raw_json(cls, record: CurrentRun | HistoryEntry) -> str:
        """Render raw JSON and its required marker within the ADR display limits."""
        raw_json = record.raw_json or "Raw JSON: unavailable."
        if not record.raw_json_truncated:
            return raw_json
        marker = cls._RAW_JSON_TRUNCATION_MARKER
        payload = cls._truncate_literal(
            raw_json,
            cls._MAX_RECORD_DETAIL_RENDERED_BYTES - len(marker.encode("utf-8")) - 1,
            cls._MAX_RECORD_DETAIL_RENDERED_LINES - 1,
        )
        return f"{payload}\n{marker}"

    @classmethod
    def _render_verification(cls, discovery: verification.VerificationDiscovery) -> str:
        """Render discovery metadata only; logs remain explicitly unopened."""
        if not discovery.attempts:
            lines = ["Verification: no verification evidence."]
        else:
            lines = ["Verification:"]
            for attempt in discovery.attempts:
                lines.extend(
                    (
                        f"Attempt {attempt.ordinal}",
                        f"  Commit: {attempt.commit or 'unavailable'}",
                        f"  Command: {attempt.command}",
                        "  OK: "
                        f"{attempt.ok}; Return code: {attempt.returncode}; "
                        f"Timed out: {attempt.timed_out}; Duration: {attempt.duration}",
                        f"  Summary: {attempt.summary}",
                        "  Log: available (openable; not opened)"
                        if attempt.log is not None
                        else "  Log: unavailable (not openable)",
                    )
                )
        if discovery.diagnostics:
            lines.append("Verification diagnostics:")
            lines.extend(f"  {item.artifact}: {item.reason}" for item in discovery.diagnostics)
        return cls._bound_verification_lines(lines)

    @classmethod
    def _bound_verification_lines(cls, lines: list[str]) -> str:
        """Bound literal verification output and retain an explicit marker."""
        rendered = "\n".join(lines)
        if (
            len(rendered.encode("utf-8")) <= cls._MAX_VERIFICATION_RENDERED_BYTES
            and len(rendered.splitlines()) <= cls._MAX_VERIFICATION_RENDERED_LINES
        ):
            return rendered

        marker = cls._VERIFICATION_TRUNCATION_MARKER
        available_bytes = cls._MAX_VERIFICATION_RENDERED_BYTES - len(marker.encode("utf-8")) - 1
        payload = cls._truncate_literal(
            rendered, available_bytes, cls._MAX_VERIFICATION_RENDERED_LINES - 1
        )
        separator = "" if cls._ends_with_line_separator(payload) else "\n"
        return f"{payload}{separator}{marker}"

    @staticmethod
    def _ends_with_line_separator(text: str) -> bool:
        """Return whether ``text`` already ends with a ``str.splitlines`` separator."""
        return text.endswith(
            ("\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
        )

    @staticmethod
    def _record_label(record: CurrentRun | HistoryEntry) -> str:
        if isinstance(record, HistoryEntry):
            return f"Sequence {record.seq}: {record.phase} (open detail)"
        return "Current state (open detail)"

    def _render_record_detail(self, record: CurrentRun | HistoryEntry) -> str:
        result = record.result_detail or "unavailable (none recorded)."
        error = record.error_detail or "unavailable (none recorded)."
        raw_status = "expanded" if self._raw_json_expanded else "collapsed (press r to expand)"
        if record.raw_json_truncated:
            raw_status = f"{raw_status}; output truncated"
        return self._bound_record_detail(
            [f"Result: {result}", f"Error: {error}", f"Raw JSON: {raw_status}"]
        )

    @classmethod
    def _bound_record_detail(cls, lines: list[str]) -> str:
        """Bound content while retaining result, error, and raw-view guidance."""
        result, error, raw_status = lines
        rendered = "\n".join(lines)
        if cls._within_record_detail_limits(rendered):
            return rendered

        marker = cls._RECORD_DETAIL_TRUNCATION_MARKER
        result_availability = (
            "Result: unavailable (none recorded)."
            if result == "unavailable (none recorded)."
            else "Result: available (detail truncated)."
        )
        error_availability = (
            "Error: unavailable (none recorded)."
            if error == "unavailable (none recorded)."
            else "Error: available (detail may be truncated)."
        )
        required = (result_availability, error_availability, f"Raw JSON: {raw_status}", marker)
        available_bytes = (
            cls._MAX_RECORD_DETAIL_RENDERED_BYTES - len("\n".join(required).encode("utf-8")) - 1
        )
        error_detail = cls._truncate_literal(
            error,
            available_bytes,
            cls._MAX_RECORD_DETAIL_RENDERED_LINES - len(required),
        )
        return "\n".join(
            (
                result_availability,
                error_availability,
                error_detail,
                f"Raw JSON: {raw_status}",
                marker,
            )
        )

    @classmethod
    def _truncate_literal(cls, text: str, max_bytes: int, max_lines: int) -> str:
        """Return a Unicode-safe literal prefix within byte and ``splitlines`` limits."""
        selected: list[str] = []
        used_bytes = 0
        for line in text.splitlines(keepends=True)[:max_lines]:
            for character in line:
                character_bytes = len(character.encode("utf-8"))
                if used_bytes + character_bytes > max_bytes:
                    return "".join(selected)
                selected.append(character)
                used_bytes += character_bytes
        return "".join(selected)

    @classmethod
    def _within_record_detail_limits(cls, rendered: str) -> bool:
        """Return whether literal record detail fits the ADR display limits."""
        return (
            len(rendered.encode("utf-8")) <= cls._MAX_RECORD_DETAIL_RENDERED_BYTES
            and len(rendered.splitlines()) <= cls._MAX_RECORD_DETAIL_RENDERED_LINES
        )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Open a selected run, record, or explicitly requested authorized log."""
        if event.list_view.id == "record-list":
            self._selected_record_index = event.index
            self._raw_json_expanded = False
        elif event.list_view.id == "verification-log-list":
            reference = self._openable_logs[event.index]
            self._selected_log_reference = reference
            self._opened_log = verification.read_log(
                self._snapshot.project.git_common_dir, reference
            )
        else:
            self._selected_run_id = self._run_id_by_row_index[event.index]
        self.call_after_refresh(self._show_selected_run)

    def _show_selected_run(self) -> None:
        """Replace the browser widgets after Textual has handled list selection."""
        self.refresh(recompose=True)
        if (
            self._selected_run_id is not None
            and self._selected_record_index is None
            and self._selected_log_reference is None
        ):
            self.call_after_refresh(self._focus_record_list)

    def _focus_record_list(self) -> None:
        """Keep the selected run's current and history records keyboard-accessible."""
        self.query_one("#record-list", ListView).focus()

    def action_refresh(self) -> None:
        """Replace the displayed snapshot with a fresh disk scan by selected run ID."""
        if self._selected_record_index is not None:
            self._raw_json_expanded = not self._raw_json_expanded
            self.refresh(recompose=True)
            return
        self._selected_log_reference = None
        self._opened_log = None
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
        if self._selected_log_reference is not None:
            self._selected_log_reference = None
            self._opened_log = None
            self.refresh(recompose=True)
            self.call_after_refresh(self._focus_record_list)
        elif self._selected_record_index is not None:
            self._selected_record_index = None
            self._raw_json_expanded = False
            self.refresh(recompose=True)
            self.call_after_refresh(self._focus_record_list)
        elif self._selected_run_id is not None:
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

    @classmethod
    def _render_current_run(cls, current: CurrentRun, lock: LockObservation) -> str:
        """Render bounded state and evidence while retaining its essential classification."""
        activity = cls._activity_label(current.run_id, lock)
        classification_lines = [
            f"Run ID: {current.run_id}",
            f"Activity: {activity}",
            f"Lock observation: {lock.activity.value.replace('_', ' ')}",
        ]
        lock_lines = cls._render_lock_observation(lock)[1:]
        if not current.loadable:
            return cls._bound_summary_lines(
                [
                    *classification_lines,
                    "Details: unavailable",
                    *lock_lines,
                    current.diagnostic or "Unavailable.",
                ]
            )
        return cls._bound_summary_lines(
            [
                *classification_lines,
                *lock_lines,
                f"Durable phase: {current.phase}",
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
            ]
        )

    @classmethod
    def _bound_summary_lines(cls, lines: list[str]) -> str:
        """Bound summary output while preserving its activity and lock classification."""
        rendered = "\n".join(lines)
        if cls._within_summary_limits(rendered):
            return rendered

        marker = cls._SUMMARY_TRUNCATION_MARKER
        classification_lines = lines[:3]
        reserved = "\n".join((*classification_lines, marker))
        available_bytes = cls._MAX_SUMMARY_RENDERED_BYTES - len(reserved.encode("utf-8"))
        available_lines = cls._MAX_SUMMARY_RENDERED_LINES - len(classification_lines) - 1
        rendered_evidence: list[str] = []
        for line in "\n".join(lines[3:]).splitlines():
            line_bytes = len(line.encode("utf-8")) + 1
            if len(rendered_evidence) == available_lines or line_bytes > available_bytes:
                break
            rendered_evidence.append(line)
            available_bytes -= line_bytes
        return "\n".join((*classification_lines, *rendered_evidence, marker))

    @classmethod
    def _within_summary_limits(cls, rendered: str) -> bool:
        """Return whether literal summary text fits the ADR display limits."""
        return (
            len(rendered.encode("utf-8")) <= cls._MAX_SUMMARY_RENDERED_BYTES
            and len(rendered.splitlines()) <= cls._MAX_SUMMARY_RENDERED_LINES
        )
