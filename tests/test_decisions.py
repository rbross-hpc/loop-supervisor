from pathlib import Path

import pytest

from loop_supervisor.contracts import ArchitectADR
from loop_supervisor.decisions import (
    DecisionError,
    adr_content_hash,
    next_adr_number,
    render_adr,
    slugify,
    validate_adr_target,
    validate_decisions_subpath,
    write_adr,
    write_adr_idempotent,
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


def _make_worktree(tmp_path: Path) -> tuple[Path, Path]:
    worktree_root = tmp_path / "worktree"
    decisions_dir = worktree_root / "docs" / "decisions"
    decisions_dir.mkdir(parents=True)
    return worktree_root, decisions_dir


def test_write_adr_idempotent_creates_missing_target(tmp_path):
    worktree_root, decisions_dir = _make_worktree(tmp_path)
    adr = ArchitectADR(title="First Decision", context="C", decision="D", consequences=[])
    content = render_adr(adr)
    target = decisions_dir / "0001-first-decision.md"

    result = write_adr_idempotent(
        decisions_dir,
        adr,
        worktree_root=worktree_root,
        target_path=str(target),
        expected_hash=adr_content_hash(content),
    )
    assert result == target
    assert target.read_text() == content


def test_write_adr_idempotent_accepts_existing_exact_target(tmp_path):
    worktree_root, decisions_dir = _make_worktree(tmp_path)
    adr = ArchitectADR(title="First Decision", context="C", decision="D", consequences=[])
    content = render_adr(adr)
    target = decisions_dir / "0001-first-decision.md"
    target.write_text(content)

    result = write_adr_idempotent(
        decisions_dir,
        adr,
        worktree_root=worktree_root,
        target_path=str(target),
        expected_hash=adr_content_hash(content),
    )
    assert result == target
    assert target.read_text() == content


def test_write_adr_idempotent_rejects_existing_different_content(tmp_path):
    worktree_root, decisions_dir = _make_worktree(tmp_path)
    adr = ArchitectADR(title="First Decision", context="C", decision="D", consequences=[])
    content = render_adr(adr)
    target = decisions_dir / "0001-first-decision.md"
    target.write_text("different content\n")

    with pytest.raises(DecisionError):
        write_adr_idempotent(
            decisions_dir,
            adr,
            worktree_root=worktree_root,
            target_path=str(target),
            expected_hash=adr_content_hash(content),
        )


def test_write_adr_idempotent_rejects_hash_mismatch_before_write(tmp_path):
    """The persisted expected_hash is authoritative: if the rendered ADR
    content does not match it, refuse to write anything at all."""
    worktree_root, decisions_dir = _make_worktree(tmp_path)
    adr = ArchitectADR(title="First Decision", context="C", decision="D", consequences=[])
    target = decisions_dir / "0001-first-decision.md"

    with pytest.raises(DecisionError):
        write_adr_idempotent(
            decisions_dir,
            adr,
            worktree_root=worktree_root,
            target_path=str(target),
            expected_hash="0" * 64,
        )
    assert not target.exists()


def test_write_adr_idempotent_rejects_absolute_escape(tmp_path):
    worktree_root, decisions_dir = _make_worktree(tmp_path)
    adr = ArchitectADR(title="First Decision", context="C", decision="D", consequences=[])
    content = render_adr(adr)
    escape_target = tmp_path / "outside" / "0001-first-decision.md"

    with pytest.raises(DecisionError):
        write_adr_idempotent(
            decisions_dir,
            adr,
            worktree_root=worktree_root,
            target_path=str(escape_target),
            expected_hash=adr_content_hash(content),
        )


def test_write_adr_idempotent_rejects_traversal(tmp_path):
    worktree_root, decisions_dir = _make_worktree(tmp_path)
    adr = ArchitectADR(title="First Decision", context="C", decision="D", consequences=[])
    content = render_adr(adr)
    escape_target = decisions_dir / ".." / ".." / "evil.md"

    with pytest.raises(DecisionError):
        write_adr_idempotent(
            decisions_dir,
            adr,
            worktree_root=worktree_root,
            target_path=str(escape_target),
            expected_hash=adr_content_hash(content),
        )


def test_write_adr_idempotent_rejects_prefix_confusion_sibling(tmp_path):
    worktree_root, decisions_dir = _make_worktree(tmp_path)
    sibling_dir = decisions_dir.parent / "decisions-evil"
    sibling_dir.mkdir()
    adr = ArchitectADR(title="First Decision", context="C", decision="D", consequences=[])
    content = render_adr(adr)
    escape_target = sibling_dir / "0001-first-decision.md"

    with pytest.raises(DecisionError):
        write_adr_idempotent(
            decisions_dir,
            adr,
            worktree_root=worktree_root,
            target_path=str(escape_target),
            expected_hash=adr_content_hash(content),
        )


def test_write_adr_idempotent_rejects_nested_subdirectory(tmp_path):
    worktree_root, decisions_dir = _make_worktree(tmp_path)
    nested = decisions_dir / "nested"
    nested.mkdir()
    adr = ArchitectADR(title="First Decision", context="C", decision="D", consequences=[])
    content = render_adr(adr)
    escape_target = nested / "0001-first-decision.md"

    with pytest.raises(DecisionError):
        write_adr_idempotent(
            decisions_dir,
            adr,
            worktree_root=worktree_root,
            target_path=str(escape_target),
            expected_hash=adr_content_hash(content),
        )


def test_write_adr_idempotent_rejects_bad_filename_form(tmp_path):
    worktree_root, decisions_dir = _make_worktree(tmp_path)
    adr = ArchitectADR(title="First Decision", context="C", decision="D", consequences=[])
    content = render_adr(adr)
    bad_target = decisions_dir / "not-numbered.md"

    with pytest.raises(DecisionError):
        write_adr_idempotent(
            decisions_dir,
            adr,
            worktree_root=worktree_root,
            target_path=str(bad_target),
            expected_hash=adr_content_hash(content),
        )


def test_write_adr_idempotent_rejects_existing_symlink_target(tmp_path):
    worktree_root, decisions_dir = _make_worktree(tmp_path)
    adr = ArchitectADR(title="First Decision", context="C", decision="D", consequences=[])
    content = render_adr(adr)
    real_file = tmp_path / "real.md"
    real_file.write_text(content)
    target = decisions_dir / "0001-first-decision.md"
    target.symlink_to(real_file)

    with pytest.raises(DecisionError):
        write_adr_idempotent(
            decisions_dir,
            adr,
            worktree_root=worktree_root,
            target_path=str(target),
            expected_hash=adr_content_hash(content),
        )


def test_write_adr_idempotent_rejects_symlinked_decisions_dir_escaping_worktree(tmp_path):
    worktree_root = tmp_path / "worktree"
    worktree_root.mkdir()
    outside_dir = tmp_path / "outside-decisions"
    outside_dir.mkdir()
    decisions_dir = worktree_root / "docs" / "decisions"
    decisions_dir.parent.mkdir(parents=True)
    decisions_dir.symlink_to(outside_dir)

    adr = ArchitectADR(title="First Decision", context="C", decision="D", consequences=[])
    content = render_adr(adr)
    target = decisions_dir / "0001-first-decision.md"

    with pytest.raises(DecisionError):
        write_adr_idempotent(
            decisions_dir,
            adr,
            worktree_root=worktree_root,
            target_path=str(target),
            expected_hash=adr_content_hash(content),
        )


def test_write_adr_idempotent_creation_race_raises_decision_error(tmp_path, monkeypatch):
    """Simulate a TOCTOU race: the `exists()` check reports False, but by
    the time the exclusive-create open() runs, a concurrent writer has
    already created the file. This must surface as DecisionError, not an
    uncaught FileExistsError."""
    worktree_root, decisions_dir = _make_worktree(tmp_path)
    adr = ArchitectADR(title="First Decision", context="C", decision="D", consequences=[])
    content = render_adr(adr)
    target = decisions_dir / "0001-first-decision.md"

    resolved_target = target.resolve()
    original_exists = Path.exists

    def racy_exists(self):
        if self == resolved_target or self == target:
            target.write_text("raced content\n")
            return False
        return original_exists(self)

    monkeypatch.setattr(Path, "exists", racy_exists)

    with pytest.raises(DecisionError):
        write_adr_idempotent(
            decisions_dir,
            adr,
            worktree_root=worktree_root,
            target_path=str(target),
            expected_hash=adr_content_hash(content),
        )


def test_validate_decisions_subpath_rejects_absolute():
    with pytest.raises(DecisionError):
        validate_decisions_subpath(Path("/etc/decisions"))


def test_validate_decisions_subpath_rejects_traversal():
    with pytest.raises(DecisionError):
        validate_decisions_subpath(Path("../decisions"))


def test_validate_decisions_subpath_accepts_relative():
    validate_decisions_subpath(Path("docs/decisions"))


def test_validate_adr_target_accepts_direct_child(tmp_path):
    worktree_root, decisions_dir = _make_worktree(tmp_path)
    target = decisions_dir / "0001-first-decision.md"
    resolved = validate_adr_target(
        worktree_root=worktree_root, decisions_dir=decisions_dir, target_path=target
    )
    assert resolved == target.resolve()


# -- OSError classification --------------------------------------------------


def test_write_adr_wraps_mkdir_oserror(tmp_path, monkeypatch):
    adr = ArchitectADR(title="T", context="C", decision="D", consequences=[])
    decisions_dir = tmp_path / "docs" / "decisions"

    def _boom(self, *a, **kw):
        raise PermissionError("simulated permission denied")

    monkeypatch.setattr(Path, "mkdir", _boom)
    with pytest.raises(DecisionError):
        write_adr(decisions_dir, adr)


def test_write_adr_idempotent_wraps_mkdir_oserror(tmp_path, monkeypatch):
    adr = ArchitectADR(title="T", context="C", decision="D", consequences=[])
    worktree_root = tmp_path
    decisions_dir = tmp_path / "docs" / "decisions"

    def _boom(self, *a, **kw):
        raise PermissionError("simulated permission denied")

    monkeypatch.setattr(Path, "mkdir", _boom)
    with pytest.raises(DecisionError):
        write_adr_idempotent(decisions_dir, adr, worktree_root=worktree_root)


def test_write_adr_idempotent_wraps_read_oserror(tmp_path, monkeypatch):
    worktree_root, decisions_dir = _make_worktree(tmp_path)
    adr = ArchitectADR(title="Use worktrees", context="C", decision="D", consequences=[])
    content = render_adr(adr)
    target = decisions_dir / "0001-use-worktrees.md"
    target.write_text(content)

    def _boom(self, *a, **kw):
        raise OSError("simulated I/O error")

    monkeypatch.setattr(Path, "read_text", _boom)
    with pytest.raises(DecisionError):
        write_adr_idempotent(
            decisions_dir,
            adr,
            worktree_root=worktree_root,
            target_path=str(target),
            expected_hash=adr_content_hash(content),
        )


def test_write_adr_idempotent_wraps_write_oserror(tmp_path, monkeypatch):
    worktree_root, decisions_dir = _make_worktree(tmp_path)
    adr = ArchitectADR(title="Use worktrees", context="C", decision="D", consequences=[])
    content = render_adr(adr)
    target = decisions_dir / "0001-use-worktrees.md"

    original_open = Path.open

    def _boom(self, mode="r", *a, **kw):
        if self == target and mode == "x":
            raise OSError("simulated disk full")
        return original_open(self, mode, *a, **kw)

    monkeypatch.setattr(Path, "open", _boom)
    with pytest.raises(DecisionError):
        write_adr_idempotent(
            decisions_dir,
            adr,
            worktree_root=worktree_root,
            target_path=str(target),
            expected_hash=adr_content_hash(content),
        )
    assert not target.exists()


def test_next_adr_number_wraps_glob_oserror(tmp_path, monkeypatch):
    decisions_dir = tmp_path / "docs" / "decisions"
    decisions_dir.mkdir(parents=True)

    def _boom(self, *a, **kw):
        raise OSError("simulated I/O error")

    monkeypatch.setattr(Path, "glob", _boom)
    with pytest.raises(DecisionError):
        next_adr_number(decisions_dir)


def test_validate_adr_target_wraps_resolve_oserror(tmp_path, monkeypatch):
    worktree_root, decisions_dir = _make_worktree(tmp_path)
    target = decisions_dir / "0001-first-decision.md"

    def _boom(self, *a, **kw):
        raise OSError("simulated resolve failure")

    monkeypatch.setattr(Path, "resolve", _boom)
    with pytest.raises(DecisionError):
        validate_adr_target(
            worktree_root=worktree_root, decisions_dir=decisions_dir, target_path=target
        )
