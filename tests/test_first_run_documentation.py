"""Regression coverage for `--max-steps 1`/first-run documentation.

`run --max-steps 1` persists the planner's chosen task; it never prints
it. The only supported inspection path is the read-only TUI's "Current
state" record, and continuing that same run requires `resume <run-id>`
-- a second `run` starts an unrelated run and invokes the planner again.
These tests pin every live first-run surface to that contract and
reject the stale claims that used to appear here (see the corrective
commit this test was added in).
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

_FIRST_RUN_DOCUMENTATION = (
    _REPO_ROOT / "docs" / "INSTALLING.md",
    _REPO_ROOT / "docs" / "ADOPTING.md",
    _REPO_ROOT / "src" / "loop_supervisor" / "_skills" / "adopt-loop-supervisor" / "SKILL.md",
    _REPO_ROOT
    / "src"
    / "loop_supervisor"
    / "_skills"
    / "adopt-loop-supervisor"
    / "references"
    / "first-run.md",
    _REPO_ROOT
    / "src"
    / "loop_supervisor"
    / "_skills"
    / "use-loop-supervisor"
    / "references"
    / "bounding-a-run.md",
)

# ADOPTING.md deliberately delegates the concrete step-by-step first-run
# procedure to the adopt-loop-supervisor skill; it only needs to point at
# `resume` for continuation, not restate the TUI/phase-name detail.
_FIRST_RUN_DETAIL_DOCUMENTATION = tuple(
    path for path in _FIRST_RUN_DOCUMENTATION if path.name != "ADOPTING.md"
)

# Only the docs that actually walk through interpreting a single-step
# run need to name the healthy pause phase; bounding-a-run.md is about
# choosing between flags, not interpreting one specific invocation.
_FIRST_RUN_INTERPRETATION_DOCUMENTATION = (
    _REPO_ROOT / "docs" / "INSTALLING.md",
    _REPO_ROOT
    / "src"
    / "loop_supervisor"
    / "_skills"
    / "adopt-loop-supervisor"
    / "references"
    / "first-run.md",
)

_STALE_CONTINUATION_CLAIMS = (
    "visible via `loop-supervisor resume`",
    "final phase: planning",
)


def test_first_run_documentation_directs_continuation_through_resume():
    """Every first-run guide must continue the same run with `resume
    <run-id>`, never a second `run` (which would start an unrelated run
    and invoke the planner again)."""
    for path in _FIRST_RUN_DOCUMENTATION:
        text = path.read_text(encoding="utf-8")
        assert "resume <run-id>" in text or "resume <run_id>" in text, path
        for claim in _STALE_CONTINUATION_CLAIMS:
            assert claim not in text, (path, claim)


def test_first_run_documentation_directs_inspection_through_the_tui():
    """A single-step run does not print the planner's chosen task; every
    guide must point at the TUI's Current state record instead."""
    for path in _FIRST_RUN_DETAIL_DOCUMENTATION:
        text = path.read_text(encoding="utf-8")
        assert "loop-supervisor tui" in text, path
        assert "Current state" in text, path


def test_first_run_documentation_describes_the_healthy_pause_phase():
    """A healthy first READY step ends at creating_worktree, not
    planning -- the worktree itself has not been created yet."""
    for path in _FIRST_RUN_INTERPRETATION_DOCUMENTATION:
        text = path.read_text(encoding="utf-8")
        assert "creating_worktree" in text, path


def test_step_budget_wording_covers_input_required_advances():
    """--max-steps counts completed advance() calls, including ones that
    pause for input without changing phase -- not just phase transitions."""
    cli_path = _REPO_ROOT / "src" / "loop_supervisor" / "cli.py"
    bounding_path = (
        _REPO_ROOT
        / "src"
        / "loop_supervisor"
        / "_skills"
        / "use-loop-supervisor"
        / "references"
        / "bounding-a-run.md"
    )
    readme_path = _REPO_ROOT / "README.md"
    for path in (cli_path, bounding_path, readme_path):
        text = path.read_text(encoding="utf-8")
        assert "advance()" in text or "completed step" in text, path


def test_bounding_a_run_documents_the_default_max_tasks_limit():
    """--max-tasks defaults to 20; there is no unbounded value, so a run
    reaching `done` at that limit must not be conflated with the planner
    reporting COMPLETE."""
    path = (
        _REPO_ROOT
        / "src"
        / "loop_supervisor"
        / "_skills"
        / "use-loop-supervisor"
        / "references"
        / "bounding-a-run.md"
    )
    text = path.read_text(encoding="utf-8")
    assert "20" in text
    assert "no flag value" in text or "no unbounded value" in text

    readme_text = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "default 20" in readme_text
