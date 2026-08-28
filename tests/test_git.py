import subprocess
from pathlib import Path

import pytest

from loop_supervisor.git import (
    GitError,
    GitRepo,
    MergeConflictError,
    sanitize_task_id,
)


def _run(args, cwd):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _init_repo(path: Path) -> GitRepo:
    path.mkdir(parents=True)
    _run(["init", "-b", "main"], path)
    _run(["config", "user.email", "test@example.com"], path)
    _run(["config", "user.name", "Test"], path)
    (path / "README.md").write_text("hello\n")
    _run(["add", "-A"], path)
    _run(["commit", "-m", "initial"], path)
    return GitRepo(path)


def test_sanitize_task_id_basic():
    assert sanitize_task_id("task-007") == "task-007"


def test_sanitize_task_id_strips_bad_chars():
    assert sanitize_task_id("Task 007 / weird!!") == "Task-007-weird"


def test_sanitize_task_id_empty_raises():
    with pytest.raises(GitError):
        sanitize_task_id("///")


def test_create_task_worktree_is_sibling(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")

    assert worktree.path == tmp_path / "project-task-007"
    assert worktree.path.exists()
    assert worktree.branch == "loop/task-007"
    assert (worktree.path / "README.md").exists()


def test_create_task_worktree_rejects_collision(tmp_path):
    repo = _init_repo(tmp_path / "project")
    repo.create_task_worktree("task-007")
    with pytest.raises(GitError):
        repo.create_task_worktree("task-007")


def test_require_clean_integration_fails_when_dirty(tmp_path):
    repo = _init_repo(tmp_path / "project")
    (repo.root / "dirty.txt").write_text("oops\n")
    with pytest.raises(GitError):
        repo.require_clean_integration()


def test_verify_builder_commit_success(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")

    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)
    head = repo.head_commit(cwd=worktree.path)

    verified = repo.verify_builder_commit(worktree, head)
    assert verified == head


def test_verify_builder_commit_rejects_wrong_commit(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")

    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)

    with pytest.raises(GitError):
        repo.verify_builder_commit(worktree, "0" * 40)


def test_verify_builder_commit_rejects_no_new_commits(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")
    head = repo.head_commit(cwd=worktree.path)

    with pytest.raises(GitError):
        repo.verify_builder_commit(worktree, head)


def test_verify_builder_commit_rejects_dirty_worktree(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")

    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)
    (worktree.path / "uncommitted.txt").write_text("oops\n")

    head = repo.head_commit(cwd=worktree.path)
    with pytest.raises(GitError):
        repo.verify_builder_commit(worktree, head)


def test_verify_builder_commit_accepts_seven_char_abbreviation(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")

    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)
    head = repo.head_commit(cwd=worktree.path)

    verified = repo.verify_builder_commit(worktree, head[:7])
    assert verified == head


def test_verify_builder_commit_accepts_twelve_char_abbreviation(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")

    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)
    head = repo.head_commit(cwd=worktree.path)

    verified = repo.verify_builder_commit(worktree, head[:12])
    assert verified == head


def test_verify_builder_commit_always_returns_full_hash(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")

    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)
    head = repo.head_commit(cwd=worktree.path)

    verified = repo.verify_builder_commit(worktree, head[:7])
    assert len(verified) == 40
    assert verified == head


def test_verify_builder_commit_rejects_empty_string(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")

    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)

    with pytest.raises(GitError):
        repo.verify_builder_commit(worktree, "")


def test_verify_builder_commit_rejects_whitespace_only(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")

    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)

    with pytest.raises(GitError):
        repo.verify_builder_commit(worktree, "   ")


def test_verify_builder_commit_rejects_head_revspec(tmp_path):
    # A builder reporting "HEAD" would trivially "match" whatever the
    # worktree happens to be at, defeating the point of verification.
    # Only hash-shaped values are ever resolved.
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")

    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)

    with pytest.raises(GitError):
        repo.verify_builder_commit(worktree, "HEAD")


def test_verify_builder_commit_rejects_branch_name(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")

    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)

    with pytest.raises(GitError):
        repo.verify_builder_commit(worktree, worktree.branch)


def test_verify_builder_commit_rejects_nonexistent_hex_prefix(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")

    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)

    with pytest.raises(GitError):
        repo.verify_builder_commit(worktree, "deadbee")


def test_verify_builder_commit_rejects_abbreviation_of_different_commit(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")

    base = repo.head_commit(cwd=worktree.path)
    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)

    # base is a real, resolvable commit, but it is not HEAD: reporting an
    # abbreviation of it must still be rejected as a mismatch, not
    # silently accepted just because it resolves to *something*.
    with pytest.raises(GitError):
        repo.verify_builder_commit(worktree, base[:7])


def test_merge_accepted_task_and_cleanup(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")

    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)

    merged_commit = repo.merge_task_branch(worktree)
    assert repo.head_commit() == merged_commit
    assert (repo.root / "feature.txt").exists()

    repo.remove_task_worktree(worktree)
    assert not worktree.path.exists()
    assert not repo.branch_exists(worktree.branch)


def test_remove_task_worktree_only_preserves_tracked_modification(tmp_path):
    """A tracked file modified after the reviewed commit was merged must be
    preserved: removal must fail rather than force-delete unreviewed
    content."""
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")

    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)
    repo.merge_task_branch(worktree)

    (worktree.path / "feature.txt").write_text("unreviewed edit\n")

    with pytest.raises(GitError):
        repo.remove_task_worktree_only(worktree)

    assert worktree.path.exists()
    assert (worktree.path / "feature.txt").read_text() == "unreviewed edit\n"
    assert repo.branch_exists(worktree.branch)


def test_remove_task_worktree_only_preserves_untracked_file(tmp_path):
    """An untracked file left in the worktree after the reviewed commit was
    merged must be preserved: removal must fail rather than force-delete
    it."""
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")

    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)
    repo.merge_task_branch(worktree)

    (worktree.path / "untracked.txt").write_text("unreviewed\n")

    with pytest.raises(GitError):
        repo.remove_task_worktree_only(worktree)

    assert worktree.path.exists()
    assert (worktree.path / "untracked.txt").exists()
    assert repo.branch_exists(worktree.branch)


def test_remove_task_worktree_only_succeeds_when_clean(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")

    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)
    repo.merge_task_branch(worktree)

    repo.remove_task_worktree_only(worktree)

    assert not worktree.path.exists()


def test_remove_task_worktree_only_idempotent_when_already_removed(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")

    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)
    repo.merge_task_branch(worktree)
    repo.remove_task_worktree_only(worktree)

    repo.remove_task_worktree_only(worktree)


def test_delete_task_branch_only_idempotent_when_already_deleted(tmp_path):
    """Branch deletion must be idempotent: if a crash occurs after the
    branch is deleted but before state records it, a resumed cleanup_branch
    that deletes again must not fail."""
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")

    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)
    repo.merge_task_branch(worktree)
    repo.remove_task_worktree_only(worktree)
    repo.delete_task_branch_only(worktree)
    assert not repo.branch_exists(worktree.branch)

    repo.delete_task_branch_only(worktree)
    assert not repo.branch_exists(worktree.branch)


def test_merge_conflict_preserves_task_branch(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")

    (worktree.path / "README.md").write_text("task version\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "task edits README"], worktree.path)

    (repo.root / "README.md").write_text("integration version\n")
    _run(["add", "-A"], repo.root)
    _run(["commit", "-m", "integration edits README"], repo.root)

    with pytest.raises(MergeConflictError):
        repo.merge_task_branch(worktree)

    assert repo.is_clean()
    assert worktree.path.exists()
    assert repo.branch_exists(worktree.branch)


def test_merge_allows_integration_drift_when_no_conflict(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")

    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)

    (repo.root / "unrelated.txt").write_text("other work\n")
    _run(["add", "-A"], repo.root)
    _run(["commit", "-m", "unrelated integration work"], repo.root)

    merged_commit = repo.merge_task_branch(worktree)
    assert repo.head_commit() == merged_commit
    assert (repo.root / "unrelated.txt").exists()
    assert (repo.root / "feature.txt").exists()


def test_reopen_task_worktree_recovers_handle(tmp_path):
    repo = _init_repo(tmp_path / "project")
    created = repo.create_task_worktree("task-007")

    (created.path / "wip.txt").write_text("partial\n")
    _run(["add", "-A"], created.path)
    _run(["commit", "-m", "partial work"], created.path)

    reopened = repo.reopen_task_worktree("task-007")
    assert reopened.path == created.path
    assert reopened.branch == created.branch


def test_common_dir_resolves_for_worktree(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")
    task_repo = GitRepo(worktree.path)

    assert task_repo.common_dir() == repo.common_dir()


# -- create_or_reconcile_task_worktree ------------------------------------


def test_create_or_reconcile_creates_from_persisted_base(tmp_path):
    repo = _init_repo(tmp_path / "project")
    base = repo.head_commit()
    path = repo.default_worktree_path("task-007")
    branch = repo.branch_name("task-007")

    worktree = repo.create_or_reconcile_task_worktree(
        original_task_id="task-007", path=path, branch=branch, base_commit=base
    )
    assert worktree.path == path
    assert worktree.base_commit == base
    assert repo.head_commit(cwd=path) == base


def test_create_or_reconcile_uses_persisted_base_not_current_head(tmp_path):
    repo = _init_repo(tmp_path / "project")
    base = repo.head_commit()
    (repo.root / "later.txt").write_text("later\n")
    _run(["add", "-A"], repo.root)
    _run(["commit", "-m", "integration moved"], repo.root)
    assert repo.head_commit() != base

    path = repo.default_worktree_path("task-007")
    branch = repo.branch_name("task-007")
    worktree = repo.create_or_reconcile_task_worktree(
        original_task_id="task-007", path=path, branch=branch, base_commit=base
    )
    assert repo.head_commit(cwd=worktree.path) == base


def test_create_or_reconcile_recognizes_existing_exact_match(tmp_path):
    """Simulates a crash after Git worktree creation but before saving the
    resulting task identity: reconciliation must recognize the exact
    existing worktree rather than creating a second one."""
    repo = _init_repo(tmp_path / "project")
    base = repo.head_commit()
    path = repo.default_worktree_path("task-007")
    branch = repo.branch_name("task-007")
    _run(["worktree", "add", "-b", branch, str(path), base], repo.root)

    worktree = repo.create_or_reconcile_task_worktree(
        original_task_id="task-007", path=path, branch=branch, base_commit=base
    )
    assert worktree.path == path
    assert worktree.branch == branch
    remaining = _run(["worktree", "list", "--porcelain"], repo.root)
    assert remaining.count(str(path)) == 1


def test_create_or_reconcile_rejects_moved_head(tmp_path):
    repo = _init_repo(tmp_path / "project")
    base = repo.head_commit()
    path = repo.default_worktree_path("task-007")
    branch = repo.branch_name("task-007")
    _run(["worktree", "add", "-b", branch, str(path), base], repo.root)
    (path / "extra.txt").write_text("moved\n")
    _run(["add", "-A"], path)
    _run(["commit", "-m", "moved past base"], path)

    with pytest.raises(GitError):
        repo.create_or_reconcile_task_worktree(
            original_task_id="task-007", path=path, branch=branch, base_commit=base
        )


def test_create_or_reconcile_rejects_wrong_branch(tmp_path):
    repo = _init_repo(tmp_path / "project")
    base = repo.head_commit()
    path = repo.default_worktree_path("task-007")
    branch = repo.branch_name("task-007")
    other_branch = "loop/other-task"
    _run(["worktree", "add", "-b", other_branch, str(path), base], repo.root)

    with pytest.raises(GitError):
        repo.create_or_reconcile_task_worktree(
            original_task_id="task-007", path=path, branch=branch, base_commit=base
        )


def test_create_or_reconcile_rejects_tampered_path(tmp_path):
    repo = _init_repo(tmp_path / "project")
    base = repo.head_commit()
    branch = repo.branch_name("task-007")
    wrong_path = tmp_path / "somewhere-else"

    with pytest.raises(GitError):
        repo.create_or_reconcile_task_worktree(
            original_task_id="task-007", path=wrong_path, branch=branch, base_commit=base
        )


def test_create_or_reconcile_rejects_partial_state(tmp_path):
    repo = _init_repo(tmp_path / "project")
    base = repo.head_commit()
    path = repo.default_worktree_path("task-007")
    branch = repo.branch_name("task-007")
    path.mkdir(parents=True)
    (path / "not-a-worktree.txt").write_text("stray\n")

    with pytest.raises(GitError):
        repo.create_or_reconcile_task_worktree(
            original_task_id="task-007", path=path, branch=branch, base_commit=base
        )


def test_create_or_reconcile_rejects_missing_base_commit(tmp_path):
    repo = _init_repo(tmp_path / "project")
    path = repo.default_worktree_path("task-007")
    branch = repo.branch_name("task-007")

    with pytest.raises(GitError):
        repo.create_or_reconcile_task_worktree(
            original_task_id="task-007", path=path, branch=branch, base_commit="0" * 40
        )


# -- reconcile_or_merge_task -----------------------------------------------


def test_reconcile_or_merge_performs_fresh_merge(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")
    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)
    task_head = repo.head_commit(cwd=worktree.path)
    pre_head = repo.head_commit()

    merge_commit = repo.reconcile_or_merge_task(pre_head=pre_head, task_head=task_head)
    assert repo.head_commit() == merge_commit
    parents = repo.commit_parents(merge_commit)
    assert parents == [pre_head, task_head]


def test_reconcile_or_merge_recognizes_existing_merge_after_crash(tmp_path):
    """Simulate a crash after Git committed the merge but before merge_commit
    was saved: reconciliation must recognize the exact existing merge
    rather than merging again."""
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")
    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)
    task_head = repo.head_commit(cwd=worktree.path)
    pre_head = repo.head_commit()

    real_merge_commit = repo.merge_task_branch(worktree)

    reconciled = repo.reconcile_or_merge_task(pre_head=pre_head, task_head=task_head)
    assert reconciled == real_merge_commit
    assert repo.head_commit() == real_merge_commit


def test_reconcile_or_merge_finds_merge_past_unrelated_commits(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")
    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)
    task_head = repo.head_commit(cwd=worktree.path)
    pre_head = repo.head_commit()

    real_merge_commit = repo.merge_task_branch(worktree)
    (repo.root / "later.txt").write_text("later\n")
    _run(["add", "-A"], repo.root)
    _run(["commit", "-m", "later unrelated commit"], repo.root)

    reconciled = repo.reconcile_or_merge_task(pre_head=pre_head, task_head=task_head)
    assert reconciled == real_merge_commit


def test_reconcile_or_merge_rejects_fast_forward_impersonation(tmp_path):
    """A fast-forward or cherry-pick that happens to contain the task's
    content must not be accepted as satisfying the merge intent."""
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")
    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)
    task_head = repo.head_commit(cwd=worktree.path)
    pre_head = repo.head_commit()

    _run(["merge", "--ff-only", worktree.branch], repo.root)
    assert repo.head_commit() == task_head

    with pytest.raises(GitError):
        repo.reconcile_or_merge_task(pre_head=pre_head, task_head=task_head)


def test_reconcile_or_merge_rejects_diverged_integration(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")
    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)
    task_head = repo.head_commit(cwd=worktree.path)

    (repo.root / "before-pre-head.txt").write_text("integration progressed\n")
    _run(["add", "-A"], repo.root)
    _run(["commit", "-m", "integration progressed"], repo.root)
    pre_head = repo.head_commit()

    # Rewrite integration history so current HEAD is no longer pre_head
    # nor a descendant of it (simulates a rewound/rebased integration branch).
    _run(["reset", "--hard", "HEAD~1"], repo.root)
    (repo.root / "rewritten.txt").write_text("rewritten\n")
    _run(["add", "-A"], repo.root)
    _run(["commit", "-m", "rewritten history"], repo.root)
    assert repo.head_commit() != pre_head
    assert not repo.is_ancestor(pre_head, repo.head_commit())

    with pytest.raises(GitError):
        repo.reconcile_or_merge_task(pre_head=pre_head, task_head=task_head)


def test_reconcile_or_merge_conflict_raises_merge_conflict_error(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")
    (worktree.path / "README.md").write_text("task version\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "task edits README"], worktree.path)
    task_head = repo.head_commit(cwd=worktree.path)

    (repo.root / "README.md").write_text("integration version\n")
    _run(["add", "-A"], repo.root)
    _run(["commit", "-m", "integration edits README"], repo.root)
    pre_head = repo.head_commit()

    with pytest.raises(MergeConflictError):
        repo.reconcile_or_merge_task(pre_head=pre_head, task_head=task_head)

    assert repo.is_clean()
    assert repo.head_commit() == pre_head


def test_reconcile_or_merge_rejects_missing_pre_head(tmp_path):
    repo = _init_repo(tmp_path / "project")
    worktree = repo.create_task_worktree("task-007")
    (worktree.path / "feature.txt").write_text("new feature\n")
    _run(["add", "-A"], worktree.path)
    _run(["commit", "-m", "implement feature"], worktree.path)
    task_head = repo.head_commit(cwd=worktree.path)

    with pytest.raises(GitError):
        repo.reconcile_or_merge_task(pre_head="0" * 40, task_head=task_head)


def test_reconcile_or_merge_rejects_missing_task_head(tmp_path):
    repo = _init_repo(tmp_path / "project")
    pre_head = repo.head_commit()

    with pytest.raises(GitError):
        repo.reconcile_or_merge_task(pre_head=pre_head, task_head="0" * 40)
