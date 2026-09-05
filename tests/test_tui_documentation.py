"""Regression coverage for public read-only TUI documentation."""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TUI_DOCUMENTATION = (
    _REPO_ROOT / "README.md",
    _REPO_ROOT / "docs" / "INSTALLING.md",
    _REPO_ROOT / "src" / "loop_supervisor" / "_skeleton" / "README.md.tmpl",
    _REPO_ROOT / "src" / "loop_supervisor" / "cli.py",
)
_STALE_TUI_CLAIMS = ("no-op stub", "being rebuilt", "currently a no-op", "pending a rebuild")


def test_tui_documentation_describes_the_read_only_run_browser():
    """Public command documentation must not regress to the retired stub."""
    documentation = {path: path.read_text(encoding="utf-8") for path in _TUI_DOCUMENTATION}

    for path, text in documentation.items():
        assert "read-only" in text, path
        assert not any(claim in text for claim in _STALE_TUI_CLAIMS), path

    for path in _TUI_DOCUMENTATION[:3]:
        text = documentation[path]
        assert "loop-supervisor tui" in text, path
        assert "disk-backed" in text, path
        assert "no writes" in text, path
        assert "no mutating lock" in text, path
        assert "automatic polling" in " ".join(text.split()), path
