"""Read-only Textual landing screen for browsing persisted supervisor runs."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Static

from ..read_model.discovery import RunSummary
from ..read_model.snapshot import ProjectSnapshot


class RunBrowserApp(App[None]):
    """Display one immutable read-model snapshot without polling or writes."""

    TITLE = "Loop Supervisor"
    SUB_TITLE = "Run browser"
    BINDINGS = [("q", "quit", "Quit")]
    CSS = """
    #run-browser {
        padding: 1 2;
    }

    .run-row {
        margin-bottom: 1;
    }
    """

    def __init__(self, snapshot: ProjectSnapshot) -> None:
        super().__init__()
        self._snapshot = snapshot

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="run-browser"):
            yield Static(
                f"Project: {self._snapshot.project.integration_root}",
                markup=False,
                classes="project-path",
            )
            if not self._snapshot.runs:
                yield Static("No discovered runs.", markup=False, classes="empty-runs")
            for summary in self._snapshot.runs:
                yield Static(self._render_run(summary), markup=False, classes="run-row")
            for diagnostic in self._snapshot.diagnostics:
                yield Static(diagnostic.message, markup=False, classes="snapshot-diagnostic")
        yield Footer()

    @staticmethod
    def _render_run(summary: RunSummary) -> str:
        """Return literal row text without constructing Rich markup from disk data."""
        if not summary.loadable:
            return f"{summary.run_id} — unloadable: {summary.diagnostic or 'unavailable'}"
        return (
            f"{summary.run_id} — {summary.phase or 'unknown'} — "
            f"updated {summary.updated_at or 'unavailable'}"
        )
