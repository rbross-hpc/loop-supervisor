"""Tests for `doctor.py`'s preflight checks (`loop-supervisor config
validate`). Each check is exercised independently for both pass and fail,
since the whole point of the report is that a caller can act on the one
thing that's wrong rather than a single opaque failure. See also
test_cli_init.py-style CLI wiring tests in test_cli_runtime.py."""

import json
import subprocess
from pathlib import Path

from loop_supervisor.doctor import (
    _check_agent_files,
    _check_clean_and_attached,
    _check_dotenv_present,
    _check_external_directory_permission,
    _check_git_executable,
    _check_git_repo,
    _check_opencode_executable,
    _check_opencode_json,
    _check_project_config,
    _check_python_version,
    env_status,
    run_checks,
    validate_report,
)


def _run(args, cwd):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run(["init", "-b", "main"], path)
    _run(["config", "user.email", "test@example.com"], path)
    _run(["config", "user.name", "Test"], path)
    (path / "README.md").write_text("hello\n")
    _run(["add", "-A"], path)
    _run(["commit", "-m", "initial"], path)


def test_check_python_version_passes_on_current_interpreter():
    result = _check_python_version()
    assert result.ok is True
    assert result.name == "python_version"


def test_check_python_version_fails_below_minimum(monkeypatch):
    monkeypatch.setattr("loop_supervisor.doctor.sys.version_info", (3, 9, 0))
    result = _check_python_version()
    assert result.ok is False
    assert "3.9" in result.detail


def test_check_git_executable_fails_when_not_on_path(monkeypatch):
    monkeypatch.setattr("loop_supervisor.doctor.shutil.which", lambda name: None)
    result = _check_git_executable()
    assert result.ok is False
    assert "not found on PATH" in result.detail


def test_check_git_executable_passes_when_present():
    result = _check_git_executable()
    assert result.ok is True


def test_check_opencode_executable_fails_when_missing(monkeypatch):
    monkeypatch.setattr("loop_supervisor.doctor.shutil.which", lambda name: None)
    result = _check_opencode_executable("opencode")
    assert result.ok is False
    assert "opencode" in result.detail
    assert "not found on PATH" in result.detail


def test_check_opencode_executable_passes_when_which_and_version_succeed(monkeypatch, tmp_path):
    fake = tmp_path / "opencode"
    fake.write_text("#!/bin/sh\necho fake-opencode 1.0.0\n")
    fake.chmod(0o755)
    monkeypatch.setattr("loop_supervisor.doctor.shutil.which", lambda name: str(fake))
    result = _check_opencode_executable("opencode")
    assert result.ok is True
    assert "fake-opencode" in result.detail


def test_check_opencode_executable_fails_when_version_exits_nonzero(monkeypatch, tmp_path):
    fake = tmp_path / "opencode"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setattr("loop_supervisor.doctor.shutil.which", lambda name: str(fake))
    result = _check_opencode_executable("opencode")
    assert result.ok is False


def test_check_git_repo_passes_for_real_repo(tmp_path):
    _init_repo(tmp_path)
    result = _check_git_repo(tmp_path)
    assert result.ok is True


def test_check_git_repo_fails_for_non_repo(tmp_path):
    result = _check_git_repo(tmp_path)
    assert result.ok is False


def test_check_clean_and_attached_passes_on_clean_branch(tmp_path):
    _init_repo(tmp_path)
    result = _check_clean_and_attached(tmp_path)
    assert result.ok is True
    assert "main" in result.detail


def test_check_clean_and_attached_fails_on_dirty_tree(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("uncommitted\n")
    result = _check_clean_and_attached(tmp_path)
    assert result.ok is False
    assert "uncommitted" in result.detail


def test_check_clean_and_attached_fails_on_detached_head(tmp_path):
    _init_repo(tmp_path)
    head = _run(["rev-parse", "HEAD"], tmp_path).strip()
    _run(["checkout", head], tmp_path)
    result = _check_clean_and_attached(tmp_path)
    assert result.ok is False


def test_check_opencode_json_fails_when_missing(tmp_path):
    result = _check_opencode_json(tmp_path)
    assert result.ok is False
    assert "does not exist" in result.detail


def test_check_opencode_json_fails_on_invalid_json(tmp_path):
    (tmp_path / "opencode.json").write_text("{not valid json")
    result = _check_opencode_json(tmp_path)
    assert result.ok is False
    assert "not valid JSON" in result.detail


def test_check_opencode_json_passes_on_valid_object(tmp_path):
    (tmp_path / "opencode.json").write_text(json.dumps({"lsp": True}))
    result = _check_opencode_json(tmp_path)
    assert result.ok is True


def test_check_external_directory_permission_fails_when_absent(tmp_path):
    (tmp_path / "opencode.json").write_text(json.dumps({"lsp": True}))
    result = _check_external_directory_permission(tmp_path)
    assert result.ok is False
    assert "external_directory" in result.detail


def test_check_external_directory_permission_fails_when_only_root_allowed(tmp_path):
    """The supervisor creates task worktrees as *siblings* of the project
    root by default (cli.py's cmd_init_copy), so allowing only the
    project root itself -- not its parent -- is still a failure."""
    config = {"permission": {"external_directory": {"*": "deny", str(tmp_path): "allow"}}}
    (tmp_path / "opencode.json").write_text(json.dumps(config))
    result = _check_external_directory_permission(tmp_path)
    assert result.ok is False


def test_check_external_directory_permission_passes_when_parent_allowed(tmp_path):
    parent = tmp_path.parent
    config = {"permission": {"external_directory": {"*": "deny", str(parent): "allow"}}}
    (tmp_path / "opencode.json").write_text(json.dumps(config))
    result = _check_external_directory_permission(tmp_path)
    assert result.ok is True


def test_check_external_directory_permission_passes_on_blanket_allow(tmp_path):
    config = {"permission": {"external_directory": "allow"}}
    (tmp_path / "opencode.json").write_text(json.dumps(config))
    result = _check_external_directory_permission(tmp_path)
    assert result.ok is True


def test_check_external_directory_permission_passes_on_wildcard_allow(tmp_path):
    config = {"permission": {"external_directory": {"*": "allow"}}}
    (tmp_path / "opencode.json").write_text(json.dumps(config))
    result = _check_external_directory_permission(tmp_path)
    assert result.ok is True


def test_check_agent_files_fails_when_directory_missing(tmp_path):
    result = _check_agent_files(tmp_path)
    assert result.ok is False


def test_check_agent_files_fails_when_one_missing(tmp_path):
    agents_dir = tmp_path / ".opencode" / "agents"
    agents_dir.mkdir(parents=True)
    for name in ("loop-planner.md", "loop-architect.md", "loop-builder.md"):
        (agents_dir / name).write_text("stub\n")
    result = _check_agent_files(tmp_path)
    assert result.ok is False
    assert "loop-auditor.md" in result.detail


def test_check_agent_files_passes_when_all_present(tmp_path):
    agents_dir = tmp_path / ".opencode" / "agents"
    agents_dir.mkdir(parents=True)
    for name in (
        "loop-planner.md",
        "loop-architect.md",
        "loop-builder.md",
        "loop-auditor.md",
    ):
        (agents_dir / name).write_text("stub\n")
    result = _check_agent_files(tmp_path)
    assert result.ok is True


def test_check_dotenv_present_fails_when_missing(tmp_path):
    result = _check_dotenv_present(tmp_path)
    assert result.ok is False


def test_check_dotenv_present_passes_when_present(tmp_path):
    (tmp_path / ".env").write_text("ARGO_API_KEY=x\n")
    result = _check_dotenv_present(tmp_path)
    assert result.ok is True


def test_check_project_config_passes_when_file_absent(tmp_path):
    result = _check_project_config(tmp_path)
    assert result.ok is True


def test_check_project_config_fails_on_invalid_toml(tmp_path):
    (tmp_path / "loop-supervisor.toml").write_text("not valid [ toml")
    result = _check_project_config(tmp_path)
    assert result.ok is False


def test_check_project_config_fails_on_unknown_executable(tmp_path):
    (tmp_path / "loop-supervisor.toml").write_text(
        '[verify]\ncommands = ["definitely-not-a-real-command-xyz --flag"]\n'
    )
    result = _check_project_config(tmp_path)
    assert result.ok is False
    assert "definitely-not-a-real-command-xyz" in result.detail


def test_check_project_config_passes_when_executable_on_path(tmp_path):
    (tmp_path / "loop-supervisor.toml").write_text('[verify]\ncommands = ["git status"]\n')
    result = _check_project_config(tmp_path)
    assert result.ok is True


def test_check_project_config_resolves_via_project_venv_bin(tmp_path):
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_tool = venv_bin / "fake-verify-tool"
    fake_tool.write_text("#!/bin/sh\nexit 0\n")
    fake_tool.chmod(0o755)
    (tmp_path / "loop-supervisor.toml").write_text('[verify]\ncommands = ["fake-verify-tool -q"]\n')
    result = _check_project_config(tmp_path)
    assert result.ok is True, result.detail


def test_env_status_never_includes_values(monkeypatch):
    monkeypatch.setenv("ARGO_API_KEY", "super-secret-value")
    status = env_status()
    assert status["ARGO_API_KEY"]["set"] is True
    serialized = json.dumps(status)
    assert "super-secret-value" not in serialized


def test_run_checks_returns_all_named_checks(tmp_path):
    _init_repo(tmp_path)
    results = run_checks(tmp_path)
    names = {r.name for r in results}
    assert names == {
        "python_version",
        "git_executable",
        "opencode_executable",
        "git_repository",
        "git_clean_worktree",
        "opencode_json",
        "external_directory_permission",
        "agent_definitions",
        "dotenv_file",
        "project_config",
    }


def test_validate_report_ok_false_when_any_check_fails(tmp_path):
    _init_repo(tmp_path)
    report = validate_report(tmp_path)
    assert report["ok"] is False
    assert isinstance(report["checks"], dict)
    assert "env" in report


def test_validate_report_ok_true_when_every_check_passes(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / ".env").write_text("ARGO_API_KEY=x\n")
    parent = tmp_path.parent
    config = {"permission": {"external_directory": {"*": "deny", str(parent): "allow"}}}
    (tmp_path / "opencode.json").write_text(json.dumps(config))
    agents_dir = tmp_path / ".opencode" / "agents"
    agents_dir.mkdir(parents=True)
    for name in (
        "loop-planner.md",
        "loop-architect.md",
        "loop-builder.md",
        "loop-auditor.md",
    ):
        (agents_dir / name).write_text("stub\n")
    _run(["add", "-A"], tmp_path)
    _run(["commit", "-m", "config"], tmp_path)

    report = validate_report(tmp_path)
    assert report["ok"] is True, report["checks"]


def test_validate_report_never_leaks_secret_values(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    monkeypatch.setenv("FALDA_TOKEN", "definitely-a-secret-token")
    report = validate_report(tmp_path)
    serialized = json.dumps(report)
    assert "definitely-a-secret-token" not in serialized
