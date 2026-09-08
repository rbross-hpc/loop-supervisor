"""Regression coverage for merge-conflict recovery documentation.

A merge conflict aborts the attempted merge and leaves no conflict
markers to resolve in place; the supported repair is a manual
`git merge --no-ff` of the exact persisted `merge_task_head` commit
(never the mutable task branch name), verified to have exactly two
parents with `merge_task_head` as the second before resuming. These
tests pin the public README, the bundled use-loop-supervisor skill,
and the runtime recovery hint to that contract.
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

_SKILL_MD_PATH = (
    _REPO_ROOT / "src" / "loop_supervisor" / "_skills" / "use-loop-supervisor" / "SKILL.md"
)
_MERGE_CONFLICT_REFERENCE_PATH = (
    _REPO_ROOT
    / "src"
    / "loop_supervisor"
    / "_skills"
    / "use-loop-supervisor"
    / "references"
    / "recovering-a-merge-conflict.md"
)

# SKILL.md deliberately delegates the exact repair recipe to its linked
# reference file, matching every other topic's split there; only the
# detail-bearing docs need to state the load-bearing invariants.
_MERGE_CONFLICT_DETAIL_DOCUMENTATION = (
    _REPO_ROOT / "README.md",
    _MERGE_CONFLICT_REFERENCE_PATH,
)

_REQUIRED_INVARIANTS = (
    "merge_task_head",
    "--no-ff",
    "second parent",
)


def test_merge_conflict_documentation_states_the_load_bearing_invariants():
    for path in _MERGE_CONFLICT_DETAIL_DOCUMENTATION:
        text = path.read_text(encoding="utf-8")
        for token in _REQUIRED_INVARIANTS:
            assert token in text, (path, token)


def test_skill_md_routes_to_the_dedicated_merge_conflict_reference():
    text = _SKILL_MD_PATH.read_text(encoding="utf-8")
    assert "recovering-a-merge-conflict.md" in text


def test_merge_conflict_reference_is_indexed():
    index_path = (
        _REPO_ROOT
        / "src"
        / "loop_supervisor"
        / "_skills"
        / "use-loop-supervisor"
        / "references"
        / "reference.md"
    )
    text = index_path.read_text(encoding="utf-8")
    assert "recovering-a-merge-conflict.md" in text


def test_merge_conflict_reference_rejects_prohibited_substitutes():
    text = _MERGE_CONFLICT_REFERENCE_PATH.read_text(encoding="utf-8")
    for token in ("fast-forward", "squash", "cherry-pick", "rebas"):
        assert token in text, (_MERGE_CONFLICT_REFERENCE_PATH, token)


def test_readme_no_longer_says_resolve_the_conflict_alone():
    text = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Resolve the conflict manually in the integration worktree, then resume." not in text


def test_runtime_recovery_hint_matches_the_documented_recipe():
    text = (_REPO_ROOT / "src" / "loop_supervisor" / "supervisor.py").read_text(encoding="utf-8")
    marker = text.index("isinstance(exc, MergeConflictError)")
    hint_source = text[marker : marker + 700]
    assert "merge_task_head" in hint_source
    assert "--no-ff" in hint_source
    assert "second parent" in hint_source
