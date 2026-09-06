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
from ..read_model.snapshot import CurrentStateDisagreement, ProjectSnapshot, build_snapshot
from ..state import StateError


class RunBrowserApp(App[None]):
    """Browse one immutable project snapshot and inspect authoritative run details."""

    _MAX_RENDERED_BYTES = 256 * 1024
    _MAX_RENDERED_LINES = 10_000
    _TIMELINE_TRUNCATION_MARKER = "Timeline output truncated: rendered-output limit reached."
    _SUMMARY_TRUNCATION_MARKER = "Summary output truncated: rendered-output limit reached."
    _RECORD_DETAIL_TRUNCATION_MARKER = (
        "Record detail output truncated: rendered-output limit reached."
    )
    _RAW_JSON_TRUNCATION_MARKER = "Raw JSON output truncated: rendered-output limit reached."
    _VERIFICATION_TRUNCATION_MARKER = (
        "Verification output truncated: rendered-output limit reached."
    )
    _LOG_VIEWER_TRUNCATION_MARKER = (
        "Verification log viewer truncated: rendered-output limit reached."
    )
    _SENSITIVE_LOG_WARNING = "WARNING: Verification output is unredacted and potentially sensitive."
    _REFRESH_FAILURE_DIAGNOSTIC = "Refresh failed: unable to scan supervisor run state."
    _BROWSER_TRUNCATION_MARKER = "Browser output truncated: rendered-output limit reached."

    TITLE = "Loop Supervisor"
    SUB_TITLE = "Run browser"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("b", "back", "Back"),
        ("r", "refresh", "Refresh"),
        ("e", "toggle_raw_json", "Toggle raw JSON"),
    ]
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
        self._refresh_failure: str | None = None
        self._raw_json_expanded = False
        self._detail_records: tuple[CurrentRun | HistoryEntry, ...] = ()
        self._openable_logs: tuple[verification.LogReference, ...] = ()
        self._run_id_by_row_index = tuple(summary.run_id for summary in snapshot.runs)
        self._browser_highlighted_run_id: str | None = None
        self._record_highlighted_identity_by_run: dict[str, tuple[str, int | None]] = {}

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
        if self._refresh_failure is not None:
            yield Static(self._refresh_failure, markup=False, classes="refresh-failure")
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
                    initial_index=self._browser_highlight_index(),
                    id="run-list",
                )
            for diagnostic in self._snapshot.diagnostics:
                yield Static(
                    self._bound_browser_output(diagnostic.message),
                    markup=False,
                    classes="snapshot-diagnostic",
                )

    def _compose_detail(self, run_id: str) -> ComposeResult:
        detail = self._snapshot.detail_for(run_id)
        current = detail.current
        history = detail.history
        discovered_verification = detail.verification
        with VerticalScroll(id="run-detail"):
            yield Static("Run detail — press b to return to the browser.", markup=False)
            yield Static(
                self._render_current_run(
                    current, self._snapshot.lock, detail.current_state_disagreements
                ),
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
            yield ListView(
                *record_rows,
                initial_index=self._record_highlight_index(),
                id="record-list",
            )
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
        return cls._bound_rendered_output("\n".join(lines), cls._LOG_VIEWER_TRUNCATION_MARKER)

    @classmethod
    def _render_raw_json(cls, record: CurrentRun | HistoryEntry) -> str:
        """Render raw JSON and its required marker within the ADR display limits."""
        raw_json = record.raw_json or "Raw JSON: unavailable."
        if not record.raw_json_truncated:
            return raw_json
        return cls._bound_rendered_output(
            raw_json,
            cls._RAW_JSON_TRUNCATION_MARKER,
            force_truncation=True,
            always_separate_marker=True,
        )

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
        return cls._bound_rendered_output("\n".join(lines), cls._VERIFICATION_TRUNCATION_MARKER)

    @classmethod
    def _bound_rendered_output(
        cls,
        text: str,
        marker: str,
        *,
        force_truncation: bool = False,
        always_separate_marker: bool = False,
    ) -> str:
        """Bound literal text with a view-specific truncation marker."""
        if not force_truncation and cls._rendered_output_within_limits(text):
            return text
        payload = cls._truncate_literal(
            text,
            cls._MAX_RENDERED_BYTES - len(marker.encode("utf-8")) - 1,
            cls._MAX_RENDERED_LINES - 1,
        )
        separator = "\n" if always_separate_marker else ""
        if not separator and not cls._ends_with_line_separator(payload):
            separator = "\n"
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

    @staticmethod
    def _record_identity(record: CurrentRun | HistoryEntry) -> tuple[str, int | None]:
        """Return the snapshot-stable identity for a selectable detail record."""
        if isinstance(record, HistoryEntry):
            return ("history", record.seq)
        return ("current", None)

    def _records_for_run(self, run_id: str) -> tuple[CurrentRun | HistoryEntry, ...]:
        """Return the current record followed by its sequence-identified history."""
        detail = self._snapshot.detail_for(run_id)
        return (detail.current, *detail.history.entries)

    def _render_record_detail(self, record: CurrentRun | HistoryEntry) -> str:
        result = record.result_detail or "unavailable (none recorded)."
        error = record.error_detail or "unavailable (none recorded)."
        raw_status = "expanded" if self._raw_json_expanded else "collapsed (press e to expand)"
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
        if cls._rendered_output_within_limits(rendered):
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
        available_bytes = cls._MAX_RENDERED_BYTES - len("\n".join(required).encode("utf-8")) - 1
        error_detail = cls._truncate_literal(
            error,
            available_bytes,
            cls._MAX_RENDERED_LINES - len(required),
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
    def _rendered_output_within_limits(cls, text: str) -> bool:
        """Return whether literal text fits the shared ADR display limits."""
        return (
            len(text.encode("utf-8")) <= cls._MAX_RENDERED_BYTES
            and len(text.splitlines()) <= cls._MAX_RENDERED_LINES
        )

    def _find_list_view(self, selector: str) -> ListView | None:
        """Return the named list view if it is currently mounted, else ``None``.

        A selected/refresh-scoped state field (``_selected_run_id``,
        ``_selected_record_index``) is not proof that the corresponding list
        widget is mounted: a pending recompose from a just-handled selection
        or refresh can leave the DOM momentarily out of sync with that
        state, most easily reproduced by coalesced keystrokes (e.g. two
        keys delivered in one input batch). Callers must tolerate ``None``
        rather than let a bare ``query_one`` raise ``NoMatches`` here.
        """
        matches = self.query(selector)
        if not matches:
            return None
        widget = matches.first()
        assert isinstance(widget, ListView)
        return widget

    def _remember_browser_highlight(self) -> None:
        """Capture the current browser cursor before replacing the list widget."""
        run_list = self._find_list_view("#run-list")
        if run_list is not None and run_list.index is not None:
            self._browser_highlighted_run_id = self._run_id_by_row_index[run_list.index]

    def _remember_record_highlight(self) -> None:
        """Capture the current run's record cursor before replacing the list widget."""
        record_list = self._find_list_view("#record-list")
        if (
            record_list is not None
            and record_list.index is not None
            and self._selected_run_id is not None
            and record_list.index < len(self._detail_records)
        ):
            self._record_highlighted_identity_by_run[self._selected_run_id] = self._record_identity(
                self._detail_records[record_list.index]
            )

    def _record_highlight_index(self) -> int:
        """Return this run's refreshed record index for the remembered record, or the first."""
        if self._selected_run_id is None:
            return 0
        highlighted_identity = self._record_highlighted_identity_by_run.get(self._selected_run_id)
        if highlighted_identity is None:
            return 0
        for index, record in enumerate(self._detail_records):
            if self._record_identity(record) == highlighted_identity:
                return index
        return 0

    def _browser_highlight_index(self) -> int:
        """Return the refreshed row index for the remembered run, or the safe first row."""
        if self._browser_highlighted_run_id is None:
            return 0
        try:
            return self._run_id_by_row_index.index(self._browser_highlighted_run_id)
        except ValueError:
            return 0

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Open a selected run, record, or explicitly requested authorized log."""
        if event.list_view.id == "record-list":
            if self._selected_run_id is None or event.index >= len(self._detail_records):
                # A stale event from a list that outlived the run selection
                # it was scoped to (e.g. the selected run disappeared on a
                # refresh that raced this event's delivery), or an index
                # from a since-shrunk record list. Ignore rather than crash
                # or record a highlight under the wrong or no run.
                return
            self._selected_record_index = event.index
            self._record_highlighted_identity_by_run[self._selected_run_id] = self._record_identity(
                self._detail_records[event.index]
            )
            self._raw_json_expanded = False
        elif event.list_view.id == "verification-log-list":
            reference = self._openable_logs[event.index]
            self._selected_log_reference = reference
            self._opened_log = verification.read_log(
                self._snapshot.project.git_common_dir, reference
            )
        else:
            self._selected_run_id = self._run_id_by_row_index[event.index]
            self._selected_record_index = None
            self._raw_json_expanded = False
            self._browser_highlighted_run_id = self._selected_run_id
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
        record_list = self._find_list_view("#record-list")
        if record_list is not None:
            record_list.focus()

    def action_toggle_raw_json(self) -> None:
        """Toggle the opt-in raw JSON view for the open record detail."""
        if self._selected_record_index is not None:
            self._raw_json_expanded = not self._raw_json_expanded
            self.refresh(recompose=True)

    def action_refresh(self) -> None:
        """Replace the displayed snapshot with a fresh disk scan by selected run ID."""
        if self._selected_run_id is None and self._snapshot.runs:
            self._remember_browser_highlight()
        elif (
            self._selected_run_id is not None
            and self._selected_record_index is None
            and self._selected_log_reference is None
        ):
            self._remember_record_highlight()
        self._selected_log_reference = None
        self._opened_log = None
        selected_run_id = self._selected_run_id
        selected_record_identity = (
            self._record_identity(self._detail_records[self._selected_record_index])
            if self._selected_record_index is not None
            else None
        )
        browser_highlighted_run_id = self._browser_highlighted_run_id
        try:
            refreshed_snapshot = build_snapshot(self._snapshot.project)
        except (StateError, OSError):
            # The old snapshot remains the only complete read model when a
            # scan-wide failure prevents a safe replacement. Do not expose raw
            # filesystem exception text, which may include sensitive paths.
            self._refresh_failure = self._REFRESH_FAILURE_DIAGNOSTIC
            self.refresh(recompose=True)
            self.call_after_refresh(self._restore_refresh_focus)
            return
        self._snapshot = refreshed_snapshot
        self._refresh_failure = None
        self._run_id_by_row_index = tuple(summary.run_id for summary in self._snapshot.runs)
        self._record_highlighted_identity_by_run = {
            run_id: identity
            for run_id, identity in self._record_highlighted_identity_by_run.items()
            if run_id in self._run_id_by_row_index
        }
        if browser_highlighted_run_id not in self._run_id_by_row_index:
            self._browser_highlighted_run_id = None
        if selected_run_id not in self._run_id_by_row_index:
            self._selected_run_id = None
            self._selected_record_index = None
            if selected_run_id is not None:
                self._record_highlighted_identity_by_run.pop(selected_run_id, None)
            self._raw_json_expanded = False
        elif selected_record_identity is not None:
            refreshed_records = self._records_for_run(selected_run_id)
            self._detail_records = refreshed_records
            self._selected_record_index = next(
                (
                    index
                    for index, record in enumerate(refreshed_records)
                    if self._record_identity(record) == selected_record_identity
                ),
                None,
            )
            if self._selected_record_index is None:
                self._raw_json_expanded = False
        self.refresh(recompose=True)
        self.call_after_refresh(self._restore_refresh_focus)

    def _restore_refresh_focus(self) -> None:
        """Return focus to the refreshed list from which the user requested refresh."""
        if self._selected_run_id is None:
            if self._snapshot.runs:
                self._focus_run_list()
        elif self._selected_record_index is None and self._selected_log_reference is None:
            self._focus_record_list()

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
        """Restore the browser cursor by run ID after the list has been recomposed."""
        run_list = self._find_list_view("#run-list")
        if run_list is None:
            return
        if self._browser_highlighted_run_id is not None:
            try:
                run_list.index = self._run_id_by_row_index.index(self._browser_highlighted_run_id)
            except ValueError:
                self._browser_highlighted_run_id = None
        run_list.focus()

    @classmethod
    def _bound_browser_output(cls, text: str) -> str:
        """Bound a browser row or project diagnostic with a visible marker."""
        return cls._bound_rendered_output(text, cls._BROWSER_TRUNCATION_MARKER)

    @classmethod
    def _render_run(cls, summary: RunSummary, lock: LockObservation) -> str:
        """Return bounded literal durable state and lock evidence."""
        activity = cls._activity_label(summary.run_id, lock)
        if not summary.loadable:
            rendered = (
                f"{summary.run_id} — unloadable: {summary.diagnostic or 'unavailable'} — "
                f"Activity: {activity}"
            )
        else:
            rendered = (
                f"{summary.run_id} — {summary.phase or 'unknown'} — "
                f"updated {summary.updated_at or 'unavailable'} — Activity: {activity}"
            )
        return cls._bound_browser_output(rendered)

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
        rendered = "\n".join(complete_lines)
        if cls._rendered_output_within_limits(rendered):
            return rendered

        marker = cls._TIMELINE_TRUNCATION_MARKER
        available_lines = cls._MAX_RENDERED_LINES - 2
        available_bytes = cls._MAX_RENDERED_BYTES - sum(
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
    def _render_current_run(
        cls,
        current: CurrentRun,
        lock: LockObservation,
        disagreements: tuple[CurrentStateDisagreement, ...],
    ) -> str:
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
                *cls._render_current_state_disagreements(disagreements),
            ]
        )

    @staticmethod
    def _render_current_state_disagreements(
        disagreements: tuple[CurrentStateDisagreement, ...],
    ) -> tuple[str, ...]:
        """Render ADR 0039 mismatches between the newest history record and
        current RunState. Diagnostic only: current RunState remains
        authoritative and every history entry is retained regardless."""
        return tuple(
            f"State/history disagreement: {disagreement.field} "
            f"(history record {disagreement.history_seq})"
            for disagreement in disagreements
        )

    @classmethod
    def _bound_summary_lines(cls, lines: list[str]) -> str:
        """Bound summary output while preserving its activity and lock classification."""
        rendered = "\n".join(lines)
        if cls._rendered_output_within_limits(rendered):
            return rendered

        marker = cls._SUMMARY_TRUNCATION_MARKER
        classification_lines = lines[:3]
        reserved = "\n".join((*classification_lines, marker))
        available_bytes = cls._MAX_RENDERED_BYTES - len(reserved.encode("utf-8"))
        available_lines = cls._MAX_RENDERED_LINES - len(classification_lines) - 1
        rendered_evidence: list[str] = []
        for line in "\n".join(lines[3:]).splitlines():
            line_bytes = len(line.encode("utf-8")) + 1
            if len(rendered_evidence) == available_lines or line_bytes > available_bytes:
                break
            rendered_evidence.append(line)
            available_bytes -= line_bytes
        return "\n".join((*classification_lines, *rendered_evidence, marker))
