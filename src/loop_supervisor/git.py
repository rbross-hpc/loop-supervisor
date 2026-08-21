"""Git integration: sibling worktrees, task branches, and integration merges.

Ownership model:
- The supervisor creates and destroys task worktrees/branches.
- The builder commits to the task branch inside its worktree.
- The auditor is read-only.
- The supervisor performs the final `--no-ff` merge into the integration
  branch on ACCEPT, and aborts cleanly (never force-resolves) on conflict.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """Raised for any unexpected or unsafe Git state."""


class MergeConflictError(GitError):
    """Raised when a no-FF merge cannot complete cleanly."""

    def __init__(self, task_branch: str, output: str) -> None:
        super().__init__(f"merge conflict integrating {task_branch!r}:\n{output}")
        self.task_branch = task_branch
        self.output = output


_TASK_ID_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def sanitize_task_id(task_id: str) -> str:
    """Turn an arbitrary planner task_id into a safe branch/path fragment."""
    sanitized = _TASK_ID_SANITIZE_RE.sub("-", task_id.strip()).strip("-.")
    if not sanitized:
        raise GitError(f"task_id {task_id!r} sanitizes to an empty string")
    return sanitized


def _run(
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed in {cwd}:\n{result.stdout}\n{result.stderr}")
    return result


@dataclass(frozen=True)
class TaskWorktree:
    """A sibling worktree created for one logical task."""

    path: Path
    branch: str
    original_task_id: str
    base_commit: str


class GitRepo:
    """Wraps Git operations for one integration worktree."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        if not (self.root / ".git").exists() and not self._is_worktree(self.root):
            raise GitError(f"{self.root} is not a git repository")

    @staticmethod
    def _is_worktree(path: Path) -> bool:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(path),
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise GitError("git executable not found on PATH") from exc
        return result.returncode == 0 and result.stdout.strip() == "true"

    def common_dir(self) -> Path:
        result = _run(["rev-parse", "--git-common-dir"], cwd=self.root)
        common = Path(result.stdout.strip())
        if not common.is_absolute():
            common = (self.root / common).resolve()
        return common

    def current_branch(self) -> str:
        result = _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=self.root)
        branch = result.stdout.strip()
        if branch == "HEAD":
            raise GitError("integration worktree is in detached HEAD state")
        return branch

    def head_commit(self, *, cwd: Path | None = None) -> str:
        result = _run(["rev-parse", "HEAD"], cwd=cwd or self.root)
        return result.stdout.strip()

    def is_clean(self, *, cwd: Path | None = None) -> bool:
        result = _run(["status", "--porcelain"], cwd=cwd or self.root)
        return result.stdout.strip() == ""

    def status_snapshot(self, *, cwd: Path | None = None) -> str:
        """A stable, machine-readable snapshot of working-tree state,
        suitable for equality comparison across a pause/resume boundary."""
        result = _run(["status", "--porcelain=v1", "--untracked-files=all"], cwd=cwd or self.root)
        return result.stdout

    def branch_commit(self, branch: str) -> str:
        result = _run(["rev-parse", f"refs/heads/{branch}"], cwd=self.root)
        return result.stdout.strip()

    def commit_exists(self, commit: str) -> bool:
        result = _run(["cat-file", "-e", commit], cwd=self.root, check=False)
        return result.returncode == 0

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = _run(
            ["merge-base", "--is-ancestor", ancestor, descendant], cwd=self.root, check=False
        )
        return result.returncode == 0

    def branch_at_path(self, path: Path) -> str:
        result = _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
        return result.stdout.strip()

    def registered_worktree_paths(self) -> set[Path]:
        result = _run(["worktree", "list", "--porcelain"], cwd=self.root)
        paths: set[Path] = set()
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                paths.add(Path(line[len("worktree ") :]).resolve())
        return paths

    def validate_task_worktree(self, task_worktree: TaskWorktree, *, expected_head: str) -> None:
        """Verify a resumed task worktree/branch against real Git state.

        Checks registration, branch identity, HEAD agreement with the
        persisted checkpoint, and base-commit ancestry. Does not require a
        clean working tree: a builder BLOCKED/INCOMPLETE pause may
        legitimately leave uncommitted edits in the task worktree.
        """
        path = task_worktree.path.resolve()
        if path not in self.registered_worktree_paths():
            raise GitError(f"task worktree {path} is not a registered git worktree")
        if not self.branch_exists(task_worktree.branch):
            raise GitError(f"expected task branch {task_worktree.branch!r} does not exist")

        actual_branch = self.branch_at_path(path)
        if actual_branch != task_worktree.branch:
            raise GitError(
                f"task worktree is on branch {actual_branch!r}, expected {task_worktree.branch!r}"
            )

        branch_head = self.branch_commit(task_worktree.branch)
        worktree_head = self.head_commit(cwd=path)
        if branch_head != worktree_head:
            raise GitError(
                f"task branch ref {branch_head!r} and worktree HEAD {worktree_head!r} disagree"
            )
        if worktree_head != expected_head:
            raise GitError(
                f"task worktree HEAD {worktree_head!r} does not match "
                f"expected checkpoint {expected_head!r}"
            )

        if not self.commit_exists(task_worktree.base_commit):
            raise GitError(f"task base commit {task_worktree.base_commit!r} no longer exists")
        if not self.is_ancestor(task_worktree.base_commit, worktree_head):
            raise GitError("task base commit is not an ancestor of the task worktree HEAD")

    def require_clean_integration(self) -> None:
        if not self.is_clean():
            raise GitError(
                f"integration worktree {self.root} has uncommitted changes; "
                "commit, stash, or discard before starting a run"
            )

    def default_worktree_path(
        self, original_task_id: str, *, worktree_root: Path | None = None
    ) -> Path:
        sanitized = sanitize_task_id(original_task_id)
        base = worktree_root if worktree_root is not None else self.root.parent
        return base / f"{self.root.name}-{sanitized}"

    def branch_name(self, original_task_id: str) -> str:
        return f"loop/{sanitize_task_id(original_task_id)}"

    def branch_exists(self, branch: str) -> bool:
        result = _run(["show-ref", "--verify", f"refs/heads/{branch}"], cwd=self.root, check=False)
        return result.returncode == 0

    def create_task_worktree(
        self,
        original_task_id: str,
        *,
        worktree_root: Path | None = None,
    ) -> TaskWorktree:
        path = self.default_worktree_path(original_task_id, worktree_root=worktree_root)
        branch = self.branch_name(original_task_id)

        if path.exists():
            raise GitError(
                f"task worktree path {path} already exists; "
                "remove it or resume the existing run instead"
            )
        if self.branch_exists(branch):
            raise GitError(
                f"branch {branch!r} already exists; remove it or resume the existing run instead"
            )

        base_commit = self.head_commit()
        _run(
            ["worktree", "add", "-b", branch, str(path), base_commit],
            cwd=self.root,
        )
        return TaskWorktree(
            path=path,
            branch=branch,
            original_task_id=original_task_id,
            base_commit=base_commit,
        )

    def reopen_task_worktree(
        self, original_task_id: str, *, worktree_root: Path | None = None
    ) -> TaskWorktree:
        """Recover a TaskWorktree handle for an already-existing worktree/branch.

        Used when resuming, or when REPLAN continues on the same preserved
        worktree without recreating it.
        """
        path = self.default_worktree_path(original_task_id, worktree_root=worktree_root)
        branch = self.branch_name(original_task_id)

        if not path.exists():
            raise GitError(f"expected task worktree {path} does not exist")
        if not self.branch_exists(branch):
            raise GitError(f"expected task branch {branch!r} does not exist")

        merge_base = _run(
            ["merge-base", branch, self.current_branch()], cwd=self.root
        ).stdout.strip()
        return TaskWorktree(
            path=path,
            branch=branch,
            original_task_id=original_task_id,
            base_commit=merge_base,
        )

    def verify_builder_commit(
        self,
        task_worktree: TaskWorktree,
        reported_commit: str,
    ) -> str:
        """Verify the builder's reported commit against actual repo state.

        Returns the verified commit hash. Raises GitError on any mismatch.
        """
        actual_branch = _run(
            ["rev-parse", "--abbrev-ref", "HEAD"], cwd=task_worktree.path
        ).stdout.strip()
        if actual_branch != task_worktree.branch:
            raise GitError(
                f"task worktree is on branch {actual_branch!r}, expected {task_worktree.branch!r}"
            )

        if not self.is_clean(cwd=task_worktree.path):
            raise GitError(f"task worktree {task_worktree.path} is not clean after COMPLETE")

        actual_head = self.head_commit(cwd=task_worktree.path)
        if actual_head != reported_commit:
            raise GitError(
                f"builder reported commit {reported_commit!r} but actual HEAD is {actual_head!r}"
            )

        count = _run(
            ["rev-list", "--count", f"{task_worktree.base_commit}..{actual_head}"],
            cwd=task_worktree.path,
        ).stdout.strip()
        if count == "0":
            raise GitError(
                f"no commits found on {task_worktree.branch!r} "
                f"since base {task_worktree.base_commit}"
            )

        return actual_head

    def merge_task_branch(self, task_worktree: TaskWorktree) -> str:
        """Merge the task branch into the integration branch with --no-ff.

        On conflict, aborts the merge (restoring the integration worktree)
        and raises MergeConflictError. The task branch/worktree are left
        untouched for diagnosis.
        """
        self.require_clean_integration()
        result = _run(
            ["merge", "--no-ff", "--no-edit", task_worktree.branch],
            cwd=self.root,
            check=False,
        )
        if result.returncode != 0:
            _run(["merge", "--abort"], cwd=self.root, check=False)
            raise MergeConflictError(task_worktree.branch, result.stdout + result.stderr)
        return self.head_commit()

    def remove_task_worktree(self, task_worktree: TaskWorktree) -> None:
        _run(["worktree", "remove", str(task_worktree.path), "--force"], cwd=self.root)
        _run(["branch", "-D", task_worktree.branch], cwd=self.root)

    def diff_summary(self, base: str, head: str, *, cwd: Path | None = None) -> str:
        result = _run(["diff", f"{base}...{head}"], cwd=cwd or self.root)
        return result.stdout

    def log_summary(self, base: str, head: str, *, cwd: Path | None = None) -> str:
        result = _run(["log", "--oneline", f"{base}..{head}"], cwd=cwd or self.root)
        return result.stdout
