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
