import pytest

from loop_supervisor.contracts import ArchitectADR
from loop_supervisor.decisions import (
    DecisionError,
    next_adr_number,
    render_adr,
    slugify,
    write_adr,
)


def test_slugify_basic():
    assert slugify("Separate Artifacts From Identity") == "separate-artifacts-from-identity"


def test_slugify_strips_punctuation():
    assert slugify("Use no-FF merges!!") == "use-no-ff-merges"


def test_slugify_empty_falls_back():
    assert slugify("!!!") == "decision"


def test_render_adr_contains_sections():
    adr = ArchitectADR(
        title="Use worktrees",
        context="We need isolated builds.",
        decision="Use sibling Git worktrees.",
        consequences=["Simpler cleanup", "More disk usage"],
    )
    text = render_adr(adr)
    assert "# Use worktrees" in text
    assert "## Status" in text
    assert "Accepted" in text
    assert "## Context" in text
    assert "## Decision" in text
    assert "## Consequences" in text
    assert "- Simpler cleanup" in text


def test_render_adr_no_consequences_placeholder():
    adr = ArchitectADR(title="T", context="C", decision="D", consequences=[])
    text = render_adr(adr)
    assert "(none noted)" in text


def test_next_adr_number_empty_dir(tmp_path):
    assert next_adr_number(tmp_path) == 1


def test_next_adr_number_increments(tmp_path):
    (tmp_path / "0001-first.md").write_text("x")
    (tmp_path / "0002-second.md").write_text("x")
    assert next_adr_number(tmp_path) == 3


def test_next_adr_number_ignores_non_matching(tmp_path):
    (tmp_path / "README.md").write_text("x")
    assert next_adr_number(tmp_path) == 1


def test_write_adr_creates_file(tmp_path):
    adr = ArchitectADR(title="First Decision", context="C", decision="D", consequences=[])
    path = write_adr(tmp_path, adr)
    assert path.name == "0001-first-decision.md"
    assert path.read_text() == render_adr(adr)


def test_write_adr_increments_across_calls(tmp_path):
    adr1 = ArchitectADR(title="First", context="C", decision="D", consequences=[])
    adr2 = ArchitectADR(title="Second", context="C", decision="D", consequences=[])
    p1 = write_adr(tmp_path, adr1)
    p2 = write_adr(tmp_path, adr2)
    assert p1.name.startswith("0001-")
    assert p2.name.startswith("0002-")


def test_write_adr_rejects_collision(tmp_path, monkeypatch):
    adr = ArchitectADR(title="Dup", context="C", decision="D", consequences=[])
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "0001-dup.md").write_text("existing")

    import loop_supervisor.decisions as decisions_mod

    monkeypatch.setattr(decisions_mod, "next_adr_number", lambda d: 1)
    with pytest.raises(DecisionError):
        write_adr(tmp_path, adr)
