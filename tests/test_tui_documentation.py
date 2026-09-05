"""Regression coverage for public read-only TUI documentation."""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PUBLIC_TUI_DOCUMENTATION = (
    _REPO_ROOT / "README.md",
    _REPO_ROOT / "docs" / "INSTALLING.md",
    _REPO_ROOT / "src" / "loop_supervisor" / "_skeleton" / "README.md.tmpl",
    _REPO_ROOT / "src" / "loop_supervisor" / "cli.py",
)
_LIVE_TUI_DOCUMENTATION = (*_PUBLIC_TUI_DOCUMENTATION, _REPO_ROOT / "docs" / "OBJECTIVE.md")
_HISTORICAL_TUI_DOCUMENTATION = (
    _REPO_ROOT / "docs" / "plans" / "2026-08-22-post-lifecycle-fix-backlog.md",
    _REPO_ROOT / "docs" / "decisions" / "0035-retire-the-in-process-textual-tui.md",
)
_STALE_TUI_CLAIMS = ("no-op stub", "being rebuilt", "currently a no-op", "pending a rebuild")
_CURRENT_STATUS = (
    "Current status: `loop-supervisor tui` ships a read-only, disk-backed run browser."
)


def test_tui_documentation_describes_the_read_only_run_browser():
    """Live command documentation must not regress to the retired implementation."""
    documentation = {path: path.read_text(encoding="utf-8") for path in _LIVE_TUI_DOCUMENTATION}

    for path, text in documentation.items():
        assert "read-only" in text, path
        assert not any(claim in text for claim in _STALE_TUI_CLAIMS), path

    for path in _PUBLIC_TUI_DOCUMENTATION[:3]:
        text = documentation[path]
        assert "loop-supervisor tui" in text, path
        assert "disk-backed" in text, path
        assert "no writes" in text, path
        assert "no mutating lock" in text, path
        assert "automatic polling" in " ".join(text.split()), path


def test_historical_tui_records_carry_a_current_status_correction():
    """Historical stub records must point readers to the shipped replacement."""
    for path in _HISTORICAL_TUI_DOCUMENTATION:
        assert _CURRENT_STATUS in path.read_text(encoding="utf-8"), path
