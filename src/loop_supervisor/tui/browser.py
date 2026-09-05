"""Read-only Textual screens for browsing persisted supervisor runs."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, ListItem, ListView, Static

from ..read_model.current_run import CurrentRun, load_current_run
from ..read_model.discovery import RunSummary
from ..read_model.snapshot import ProjectSnapshot


class RunBrowserApp(App[None]):
    """Browse one immutable project snapshot and inspect authoritative run details."""

    TITLE = "Loop Supervisor"
    SUB_TITLE = "Run browser"
    BINDINGS = [("q", "quit", "Quit"), ("b", "back", "Back")]
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
                            Static(self._render_run(summary), markup=False, classes="run-row"),
                        )
                        for summary in self._snapshot.runs
                    ),
                    id="run-list",
                )
            for diagnostic in self._snapshot.diagnostics:
                yield Static(diagnostic.message, markup=False, classes="snapshot-diagnostic")

    def _compose_detail(self, run_id: str) -> ComposeResult:
        current = load_current_run(self._snapshot.project.git_common_dir, run_id)
        with VerticalScroll(id="run-detail"):
            yield Static("Run detail — press b to return to the browser.", markup=False)
            yield Static(
                self._render_current_run(current), markup=False, classes="run-detail-summary"
            )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Open the selected run using the authoritative current-state reader."""
        self._selected_run_id = self._run_id_by_row_index[event.index]
        self.call_after_refresh(self._show_selected_run)

    def _show_selected_run(self) -> None:
        """Replace the browser widgets after Textual has handled list selection."""
        self.refresh(recompose=True)

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
    def _render_run(summary: RunSummary) -> str:
        """Return literal row text without constructing Rich markup from disk data."""
        if not summary.loadable:
            return f"{summary.run_id} — unloadable: {summary.diagnostic or 'unavailable'}"
        return (
            f"{summary.run_id} — {summary.phase or 'unknown'} — "
            f"updated {summary.updated_at or 'unavailable'}"
        )

    @staticmethod
    def _render_current_run(current: CurrentRun) -> str:
        """Render only validated summary fields, or a safe unavailable diagnostic."""
        if not current.loadable:
            return "\n".join(
                (
                    f"Run ID: {current.run_id}",
                    "Details: unavailable",
                    current.diagnostic or "Unavailable.",
                )
            )
        return "\n".join(
            (
                f"Run ID: {current.run_id}",
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
            )
        )
