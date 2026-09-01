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

_COMMIT_HASH_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


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

    def create_or_reconcile_task_worktree(
        self,
        *,
        original_task_id: str,
        path: Path,
        branch: str,
        base_commit: str,
        worktree_root: Path | None = None,
    ) -> TaskWorktree:
        """Create the exact persisted worktree intent, or reconcile it against
        an already-existing worktree/branch left behind by a crash.

        This never substitutes current mutable Git state (e.g. the current
        integration HEAD) for a persisted intent: `path`, `branch`, and
        `base_commit` are treated as immutable and must be honored exactly,
        or the call fails closed with GitError.
        """
        expected_path = self.default_worktree_path(
            original_task_id, worktree_root=worktree_root
        ).resolve()
        expected_branch = self.branch_name(original_task_id)

        if path.resolve() != expected_path:
            raise GitError(
                f"pending worktree path {path} does not match the path derived "
                f"from task_id {original_task_id!r} ({expected_path}); "
                "refusing to act on tampered or inconsistent intent"
            )
        if branch != expected_branch:
            raise GitError(
                f"pending worktree branch {branch!r} does not match the branch "
                f"derived from task_id {original_task_id!r} ({expected_branch!r}); "
                "refusing to act on tampered or inconsistent intent"
            )
        if not self.commit_exists(base_commit):
            raise GitError(f"pending worktree base commit {base_commit!r} no longer exists")

        path_exists = path.exists()
        branch_exists = self.branch_exists(branch)

        if not path_exists and not branch_exists:
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

        if path_exists != branch_exists:
            raise GitError(
                f"partial worktree state for {original_task_id!r}: "
                f"path exists={path_exists}, branch exists={branch_exists}; "
                "remove both before retrying"
            )

        # Both the path and branch already exist. This can only happen after
        # a crash between Git worktree creation and saving the resulting task
        # identity. Reconcile by proving the existing worktree is exactly the
        # one this intent describes; never repair or force anything.
        registered = self.registered_worktree_paths()
        resolved_path = path.resolve()
        if resolved_path not in registered:
            raise GitError(
                f"path {path} exists but is not a registered git worktree; "
                "refusing to reuse an unrelated directory"
            )

        actual_branch = self.branch_at_path(path)
        if actual_branch != branch:
            raise GitError(
                f"existing worktree at {path} is on branch {actual_branch!r}, expected {branch!r}"
            )

        branch_head = self.branch_commit(branch)
        worktree_head = self.head_commit(cwd=path)
        if branch_head != worktree_head:
            raise GitError(
                f"branch ref {branch_head!r} and worktree HEAD {worktree_head!r} "
                f"disagree for {branch!r}"
            )
        if worktree_head != base_commit:
            raise GitError(
                f"existing worktree HEAD {worktree_head!r} does not match the "
                f"persisted creating_worktree base {base_commit!r}; refusing to "
                "reuse it (no builder phase has run against it yet)"
            )
        if not self.is_clean(cwd=path):
            raise GitError(
                f"existing worktree at {path} has uncommitted changes, but no "
                "builder phase has run against it yet; this content is "
                "unexplained and must be inspected and removed before it can "
                "be reused"
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

        `reported_commit` must be a hash (full 40-character SHA or an
        unambiguous abbreviation of at least 7 hex characters, matching
        Git's own default `core.abbrev`); anything else -- including
        revspecs like "HEAD" or a branch name, which would trivially
        "match" whatever the worktree happens to be at -- is rejected
        without ever being resolved.

        Returns the verified, full-length commit hash regardless of
        whether an abbreviation was reported. Raises GitError on any
        mismatch.
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

        candidate = reported_commit
        if not _COMMIT_HASH_RE.fullmatch(candidate):
            raise GitError(
                f"builder reported {reported_commit!r}, which is not a commit hash "
                "(expected 7-40 hex characters); refusing to resolve it as a revspec"
            )

        result = _run(
            ["rev-parse", "--verify", "--end-of-options", f"{candidate}^{{commit}}"],
            cwd=task_worktree.path,
            check=False,
        )
        if result.returncode != 0:
            raise GitError(
                f"builder reported commit {reported_commit!r}, which could not be "
                f"resolved in the task worktree: {result.stderr.strip()}"
            )
        resolved = result.stdout.strip()

        if resolved != actual_head:
            raise GitError(
                f"builder reported commit {reported_commit!r} (resolved to {resolved!r}) "
                f"but actual HEAD is {actual_head!r}"
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

    def commit_parents(self, commit: str) -> list[str]:
        result = _run(["rev-parse", f"{commit}^@"], cwd=self.root, check=False)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _is_merge_in_progress(self) -> bool:
        result = _run(["rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=self.root, check=False)
        return result.returncode == 0

    def _has_unmerged_paths(self) -> bool:
        result = _run(["status", "--porcelain=v1"], cwd=self.root, check=False)
        return any(line.startswith(("U", "AA", "DD")) for line in result.stdout.splitlines())

    def _find_existing_merge_of(
        self, *, pre_head: str, task_head: str, search_head: str
    ) -> str | None:
        """Search the integration first-parent chain between pre_head and
        search_head for a merge commit whose second parent is exactly
        task_head. Returns the qualifying merge commit, or None."""
        result = _run(
            ["rev-list", "--first-parent", f"{pre_head}..{search_head}"],
            cwd=self.root,
            check=False,
        )
        if result.returncode != 0:
            return None
        for candidate in result.stdout.splitlines():
            candidate = candidate.strip()
            if not candidate:
                continue
            parents = self.commit_parents(candidate)
            if len(parents) >= 2 and parents[1] == task_head:
                return candidate
        return None

    def reconcile_or_merge_task(self, *, pre_head: str, task_head: str) -> str:
        """Return the exact merge commit satisfying persisted merge intent.

        `pre_head` and `task_head` are immutable intent captured before the
        merge was attempted: the integration HEAD immediately before, and
        the exact task-branch commit under audit. This never merges a
        mutable branch name; it always merges the immutable `task_head`
        SHA, and never substitutes a moved branch tip.

        If a qualifying merge already exists on the integration first-parent
        chain (e.g. because a previous attempt crashed after Git committed
        the merge but before state was saved), that exact commit is
        returned without performing another merge. Otherwise, performs a
        fresh `--no-ff` merge and verifies the resulting commit's parents.
        """
        if not self.commit_exists(pre_head):
            raise GitError(f"merge_pre_head {pre_head!r} no longer exists")
        if not self.commit_exists(task_head):
            raise GitError(f"merge_task_head {task_head!r} no longer exists")

        current_head = self.head_commit()
        if current_head != pre_head and not self.is_ancestor(pre_head, current_head):
            raise GitError(
                f"integration HEAD {current_head!r} is not {pre_head!r} or a descendant "
                "of it; refusing to merge onto diverged/rewound history"
            )

        existing = self._find_existing_merge_of(
            pre_head=pre_head, task_head=task_head, search_head=current_head
        )
        if existing is not None:
            return existing

        self.require_clean_integration()
        result = _run(
            ["merge", "--no-ff", "--no-edit", task_head],
            cwd=self.root,
            check=False,
        )
        if result.returncode != 0:
            is_conflict = self._is_merge_in_progress() or self._has_unmerged_paths()
            abort = _run(["merge", "--abort"], cwd=self.root, check=False)
            restored_head = self.head_commit()
            if restored_head != current_head or not self.is_clean():
                raise GitError(
                    "merge failed and the integration worktree could not be restored to "
                    f"its pre-merge state (expected HEAD {current_head!r}, got "
                    f"{restored_head!r}); manual repair required. "
                    f"merge output: {result.stdout + result.stderr}\n"
                    f"abort output: {abort.stdout + abort.stderr}"
                )
            if is_conflict:
                raise MergeConflictError(task_head, result.stdout + result.stderr)
            raise GitError(
                f"git merge {task_head!r} failed for a reason other than a content "
                f"conflict:\n{result.stdout}\n{result.stderr}"
            )

        merge_commit = self.head_commit()
        parents = self.commit_parents(merge_commit)
        if len(parents) != 2 or parents[0] != current_head or parents[1] != task_head:
            raise GitError(
                f"merge commit {merge_commit!r} does not have the expected parents "
                f"(first={current_head!r}, second={task_head!r}); found {parents!r}"
            )
        return merge_commit

    def remove_task_worktree(self, task_worktree: TaskWorktree) -> None:
        self.remove_task_worktree_only(task_worktree)
        self.delete_task_branch_only(task_worktree)

    def remove_task_worktree_only(self, task_worktree: TaskWorktree) -> None:
        """Remove the task worktree directory idempotently.

        If the path is no longer registered as a Git worktree, does nothing.
        Never removes a directory not registered under task_worktree.branch.
        Refuses to remove a worktree with uncommitted (tracked or untracked)
        changes: by this phase the reviewed commit has already been merged,
        so any remaining dirty content is either accidental or unreviewed
        and must be preserved for manual inspection rather than silently
        discarded via `--force`."""
        registered = self.registered_worktree_paths()
        path = task_worktree.path.resolve()
        if path not in registered:
            return
        actual_branch = self.branch_at_path(task_worktree.path)
        if actual_branch != task_worktree.branch:
            raise GitError(
                f"refusing to remove worktree at {path}: it is on branch {actual_branch!r}, "
                f"not the expected {task_worktree.branch!r}"
            )
        if not self.is_clean(cwd=task_worktree.path):
            raise GitError(
                f"refusing to remove worktree at {path}: it has uncommitted changes; "
                "the reviewed commit has already been merged, so this content is "
                "unreviewed and must be inspected and committed or discarded manually "
                "before cleanup can proceed"
            )
        _run(["worktree", "remove", str(task_worktree.path)], cwd=self.root)

    def delete_task_branch_only(self, task_worktree: TaskWorktree) -> None:
        """Delete the task branch idempotently.

        If the branch no longer exists, does nothing. Refuses to delete a branch
        whose tip is not an ancestor of the integration branch (i.e. unintegrated work)."""
        if not self.branch_exists(task_worktree.branch):
            return
        integration_head = self.head_commit()
        task_tip = self.branch_commit(task_worktree.branch)
        if not self.is_ancestor(task_tip, integration_head):
            raise GitError(
                f"refusing to delete branch {task_worktree.branch!r}: its tip {task_tip!r} "
                "has not been integrated into the integration branch"
            )
        _run(["branch", "-D", task_worktree.branch], cwd=self.root)

    def diff_summary(self, base: str, head: str, *, cwd: Path | None = None) -> str:
        result = _run(["diff", f"{base}...{head}"], cwd=cwd or self.root)
        return result.stdout

    def log_summary(self, base: str, head: str, *, cwd: Path | None = None) -> str:
        result = _run(["log", "--oneline", f"{base}..{head}"], cwd=cwd or self.root)
        return result.stdout
