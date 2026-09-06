import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from loop_supervisor.read_model import ProjectResolutionError, resolve_project


def _run_git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _init_repo(path: Path) -> Path:
    path.mkdir()
    _run_git(["init", "-b", "main"], path)
    _run_git(["config", "user.email", "test@example.com"], path)
    _run_git(["config", "user.name", "Test"], path)
    (path / "README.md").write_text("test\n")
    _run_git(["add", "README.md"], path)
    _run_git(["commit", "-m", "initial"], path)
    return path


def test_resolve_project_uses_current_directory_when_path_is_omitted(tmp_path, monkeypatch):
    project = _init_repo(tmp_path / "project")
    monkeypatch.chdir(project)

    resolved = resolve_project()

    assert resolved.integration_root == project.resolve()
    assert resolved.git_common_dir == (project / ".git").resolve()
    with pytest.raises(FrozenInstanceError):
        resolved.integration_root = tmp_path  # type: ignore[misc]


def test_resolve_project_canonicalizes_an_explicit_path_inside_a_repository(tmp_path):
    project = _init_repo(tmp_path / "project")
    nested = project / "nested" / "directory"
    nested.mkdir(parents=True)

    resolved = resolve_project(nested)

    assert resolved.integration_root == project.resolve()
    assert resolved.git_common_dir == (project / ".git").resolve()


def test_resolve_project_rejects_a_nonexistent_path_without_a_partial_result(tmp_path):
    missing = tmp_path / "missing"

    with pytest.raises(
        ProjectResolutionError,
        match=r"Cannot resolve project .*: Git repository access failed\.",
    ):
        resolve_project(missing)


def test_resolve_project_does_not_expose_git_command_output(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    raw_git_output = "git-command-stderr-secret"
    (project / ".git").write_text(f"gitdir: {tmp_path / raw_git_output}\n")

    with pytest.raises(
        ProjectResolutionError,
        match=r"Cannot resolve project .*: Git repository access failed\.",
    ) as caught:
        resolve_project(project)

    assert raw_git_output not in str(caught.value)
