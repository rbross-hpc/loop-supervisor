"""Rich renderables for supervisor state display."""

from __future__ import annotations

from typing import Any

from rich.markup import escape
from rich.table import Table
from rich.text import Text

from ..state import RunState
from .live import LiveActivitySnapshot


def render_phase_badge(phase: str) -> Text:
    color_map = {
        "planning": "cyan",
        "architecting": "blue",
        "building": "yellow",
        "verifying": "yellow",
        "auditing": "magenta",
        "awaiting_input": "orange3",
        "creating_worktree": "cyan",
        "recording_decision": "blue",
        "merging": "green",
        "cleanup_worktree": "green",
        "cleanup_branch": "green",
        "operational_failure": "red",
        "done": "green",
        "failed": "bright_red",
    }
    color = color_map.get(phase, "white")
    return Text(phase.upper(), style=f"bold {color}")


def render_durable_summary(
    state: RunState,
    *,
    denied_permission_count: int = 0,
    denied_permission_summary: list[str] | None = None,
) -> Table:
    """`denied_permission_count`/`denied_permission_summary` come from
    `RunSession.denied_permission_count`/`denied_permission_summary`
    (`runtime.py`), not from `RunState` -- the headless permission
    denier's tally is an in-memory diagnostic, never persisted. See
    backlog item 31 and ADR 0021 for why the TUI runs this same denier
    (via `RunSession.start_server()`) without previously surfacing what
    it denied."""
    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="dim", min_width=18)
    grid.add_column()

    grid.add_row("Phase", render_phase_badge(state.phase))
    grid.add_row("Run ID", escape(state.run_id))
    grid.add_row("Branch", escape(state.integration_branch))

    if state.original_task_id:
        grid.add_row("Task", escape(state.original_task_id))
    if state.accepted_task_count:
        grid.add_row("Accepted tasks", str(state.accepted_task_count))
    if state.revision_count:
        grid.add_row("Revisions", str(state.revision_count))

    if denied_permission_count:
        keys = ", ".join(denied_permission_summary or [])
        grid.add_row(
            "Denied permissions",
            Text(f"{denied_permission_count} ({keys})", style="yellow"),
        )

    if state.last_error:
        err_msg = escape(str(state.last_error.get("message", ""))[:120])
        grid.add_row("Last error", Text(err_msg, style="red"))

    return grid


def render_live_summary(snapshot: LiveActivitySnapshot) -> Table:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="dim", min_width=14)
    grid.add_column()

    conn = snapshot.connection
    conn_color = "green" if conn.state == "live" else "red"
    grid.add_row("SSE", Text(conn.state, style=conn_color))

    for inv in snapshot.invocations:
        status_color = "yellow" if inv.status == "running" else "dim"
        agent_label = Text(f"[{inv.agent}]")
        grid.add_row(agent_label, Text(inv.status, style=status_color))
        if inv.latest_message and inv.latest_message.text_tail:
            tail = inv.latest_message.text_tail[-200:]
            grid.add_row("", Text(tail, style="dim", overflow="fold"))

    if snapshot.unknown_event_count:
        grid.add_row("Unknown events", str(snapshot.unknown_event_count))

    return grid


def render_pending_input(question: dict) -> str:
    kind = question.get("kind", "")
    message = question.get("message", "")
    return f"[{kind}] {escape(message)}"


def render_operational_failure(error: dict[str, Any]) -> str:
    lines = ["[bold red]Operational failure[/bold red]"]
    msg = escape(str(error.get("message", ""))[:200])
    if msg:
        lines.append(f"Error: {msg}")
    kind = error.get("kind", "")
    if kind:
        lines.append(f"Kind: {escape(kind)}")
    phase = error.get("failed_phase", "")
    if phase:
        lines.append(f"Failed phase: {escape(phase)}")
    retry_phase = error.get("retry_phase", "")
    if retry_phase:
        lines.append(f"Retry phase: {escape(retry_phase)}")
    if error.get("requires_repair"):
        lines.append("[yellow]Repair required before retry.[/yellow]")
    hint = error.get("recovery_hint", "")
    if hint:
        lines.append(f"[dim]{escape(str(hint))}[/dim]")
    return "\n".join(lines)
