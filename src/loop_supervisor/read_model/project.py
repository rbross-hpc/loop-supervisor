"""Typed, presentation-independent resolution of a selected Git project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..git import GitError, GitRepo


class ProjectResolutionError(RuntimeError):
    """Raised when a supplied project cannot safely identify a Git repository."""


@dataclass(frozen=True)
class ProjectResolution:
    """Canonical roots owned by the selected project's supervisor instance."""

    integration_root: Path
    git_common_dir: Path


def resolve_project(project_path: Path | str | None = None) -> ProjectResolution:
    """Resolve ``project_path`` (or the current directory) to canonical Git roots.

    The selected path may name any directory inside a supported Git worktree.
    Git, rather than path normalization alone, identifies the integration root.
    """
    path = Path.cwd() if project_path is None else Path(project_path)
    try:
        repo = GitRepo(path)
        return ProjectResolution(
            integration_root=repo.integration_root(),
            git_common_dir=repo.common_dir(),
        )
    except (GitError, OSError) as exc:
        raise ProjectResolutionError(f"Cannot resolve project {path}: {exc}") from exc
