"""Tests for `loop-supervisor skill list`/`show`/`export` (the bundled
Agent Skills: `adopt-loop-supervisor` and `use-loop-supervisor`).

opencode only registers a skill if its `SKILL.md` opens with a
`---`-delimited frontmatter block containing at least a string `name`
field (opencode's `isSkillFrontmatter`) -- a `SKILL.md` without that
block is silently dropped, no error or warning, so the agent proceeds
without any of the skill's guidance. See wake's test_cli_skill.py,
which found and documented this the hard way; these tests assert the
same contract for both of loop-supervisor's bundled skills, and that
each survives `skill export` unchanged. `adopt-loop-supervisor` is the
default skill for `show`/`export` when no name is given, preserving
the exact CLI behavior these subcommands had before a second skill was
added.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from loop_supervisor.cli import main

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SKILLS_DIR = _REPO_ROOT / "src" / "loop_supervisor" / "_skills"
_DEFAULT_SKILL_NAME = "adopt-loop-supervisor"
_SKILL_NAMES = ["adopt-loop-supervisor", "use-loop-supervisor"]


def _skill_md(name: str) -> Path:
    return _SKILLS_DIR / name / "SKILL.md"


_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_NAME_LINE_RE = re.compile(r"^name:\s*(\S.*)$", re.MULTILINE)
_DESCRIPTION_LINE_RE = re.compile(r"^description:\s*(\S.*)$", re.MULTILINE)


def _frontmatter_block(text: str) -> str:
    m = _FRONTMATTER_RE.match(text)
    assert m, "SKILL.md must open with a '---'-delimited frontmatter block"
    return m.group(1)


def _frontmatter_name(text: str) -> str | None:
    m = _NAME_LINE_RE.search(_frontmatter_block(text))
    return m.group(1).strip() if m else None


def _frontmatter_description(text: str) -> str | None:
    m = _DESCRIPTION_LINE_RE.search(_frontmatter_block(text))
    return m.group(1).strip() if m else None


def _run_cli(argv, capsys):
    code: int
    with patch.object(sys, "argv", ["loop-supervisor", *argv]):
        try:
            code = main()
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, capsys.readouterr()


@pytest.mark.parametrize("skill_name", _SKILL_NAMES)
def test_bundled_skill_md_has_valid_frontmatter(skill_name):
    text = _skill_md(skill_name).read_text(encoding="utf-8")
    assert _frontmatter_name(text) == skill_name
    description = _frontmatter_description(text)
    assert description and description.strip()


def test_skill_list_prints_both_bundled_skill_names(capsys):
    code, captured = _run_cli(["skill", "list"], capsys)
    assert code == 0
    assert captured.out.splitlines() == sorted(_SKILL_NAMES)


def test_skill_show_defaults_to_adopt_loop_supervisor(capsys):
    code, captured = _run_cli(["skill", "show"], capsys)
    assert code == 0
    assert captured.out == _skill_md(_DEFAULT_SKILL_NAME).read_text(encoding="utf-8")


@pytest.mark.parametrize("skill_name", _SKILL_NAMES)
def test_skill_show_prints_the_requested_bundled_skill_md(skill_name, capsys):
    code, captured = _run_cli(["skill", "show", "--name", skill_name], capsys)
    assert code == 0
    assert captured.out == _skill_md(skill_name).read_text(encoding="utf-8")


def test_skill_show_unknown_name_fails_cleanly(capsys):
    code, captured = _run_cli(["skill", "show", "--name", "does-not-exist"], capsys)
    assert code == 1
    assert "does-not-exist" in captured.err
    for skill_name in _SKILL_NAMES:
        assert skill_name in captured.err


def test_skill_export_defaults_to_adopt_loop_supervisor(tmp_path, capsys):
    dest = tmp_path / "exported"
    code, _ = _run_cli(["skill", "export", str(dest)], capsys)
    assert code == 0

    exported = dest / "SKILL.md"
    assert exported.exists()
    text = exported.read_text(encoding="utf-8")
    assert _frontmatter_name(text) == _DEFAULT_SKILL_NAME


@pytest.mark.parametrize("skill_name", _SKILL_NAMES)
def test_skill_export_preserves_frontmatter(skill_name, tmp_path, capsys):
    dest = tmp_path / ".opencode" / "skills" / skill_name
    code, _ = _run_cli(["skill", "export", str(dest), "--name", skill_name], capsys)
    assert code == 0

    exported = dest / "SKILL.md"
    assert exported.exists()
    text = exported.read_text(encoding="utf-8")
    assert _frontmatter_name(text) == skill_name
    description = _frontmatter_description(text)
    assert description and description.strip()


@pytest.mark.parametrize("skill_name", _SKILL_NAMES)
def test_skill_export_copies_reference_files(skill_name, tmp_path, capsys):
    dest = tmp_path / "exported"
    code, _ = _run_cli(["skill", "export", str(dest), "--name", skill_name], capsys)
    assert code == 0

    references = dest / "references"
    exported_names = {p.name for p in references.iterdir()}
    source_names = {p.name for p in (_skill_md(skill_name).parent / "references").iterdir()}
    assert exported_names == source_names


def test_skill_export_unknown_name_fails_cleanly(tmp_path, capsys):
    dest = tmp_path / "exported"
    code, captured = _run_cli(["skill", "export", str(dest), "--name", "does-not-exist"], capsys)
    assert code == 1
    assert "does-not-exist" in captured.err
    assert not dest.exists()


def test_skill_export_requires_path_argument(capsys):
    code, captured = _run_cli(["skill", "export"], capsys)
    assert code != 0
    assert "PATH" in captured.err or "path" in captured.err.lower()


def test_skill_export_rejects_nonempty_destination_without_force(tmp_path, capsys):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "existing.txt").write_text("already here\n")

    code, captured = _run_cli(["skill", "export", str(dest)], capsys)
    assert code == 1
    assert "already exists" in captured.err
    assert not (dest / "SKILL.md").exists()


def test_skill_export_overwrites_with_force(tmp_path, capsys):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "existing.txt").write_text("already here\n")

    code, _ = _run_cli(["skill", "export", str(dest), "--force"], capsys)
    assert code == 0
    assert (dest / "SKILL.md").exists()
    assert not (dest / "existing.txt").exists()


def test_skill_requires_a_skill_action(capsys):
    code, _ = _run_cli(["skill"], capsys)
    assert code != 0
