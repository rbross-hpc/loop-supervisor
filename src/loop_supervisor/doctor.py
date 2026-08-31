"""Preflight checks for "can a loop-supervisor run actually start here",
surfaced as `loop-supervisor config validate`.

This is deliberately a fast, offline preflight -- it never makes a
network call or starts `opencode serve`. It answers "is the local
environment plausibly set up" (executables on PATH, a parseable
`opencode.json`, a clean non-detached git worktree, required env vars
present), not "will the configured provider/model actually respond".
The latter needs credentials and a live endpoint, which turns a
sub-second preflight into a slow, flaky one; see ADR 0022.

Each check is independently reported by name so a caller (a human, or
an agent following the `adopt-loop-supervisor` skill) can act on the
single thing that's wrong rather than re-deriving it from one opaque
failure. Values of environment variables are never included in the
report -- only whether each is set -- so this is safe to print, log,
or hand to an LLM without leaking secrets.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ConfigError, load_project_config
from .git import GitError, GitRepo

# Env vars this project's own skeleton and `.env.example` reference.
# Presence is informational, not blocking -- a project may use a
# different provider with different variable names entirely, so an
# unset var here is reported but does not fail `ok`.
_KNOWN_PROVIDER_ENVS: tuple[str, ...] = ("ARGO_API_KEY", "FALDA_TOKEN", "FALDA_TENANT")

_REQUIRED_AGENT_FILES: tuple[str, ...] = (
    "loop-planner.md",
    "loop-architect.md",
    "loop-builder.md",
    "loop-auditor.md",
)

_MIN_PYTHON = (3, 11)


@dataclass(frozen=True)
class CheckResult:
    """One named preflight check's outcome.

    `ok=True` with a non-empty `detail` is still reported (e.g. "opencode
    version 1.18.22 found") -- `detail` is informational, `ok` is what
    gates the overall report.
    """

    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "detail": self.detail}


def _check_python_version() -> CheckResult:
    actual = sys.version_info[:2]
    if actual >= _MIN_PYTHON:
        return CheckResult(
            "python_version",
            True,
            f"Python {actual[0]}.{actual[1]} (>= {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]} required)",
        )
    return CheckResult(
        "python_version",
        False,
        f"Python {actual[0]}.{actual[1]} found; loop-supervisor requires "
        f">= {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}",
    )


def _check_git_executable() -> CheckResult:
    path = shutil.which("git")
    if path is None:
        return CheckResult("git_executable", False, "git not found on PATH")
    try:
        result = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult("git_executable", False, f"git found at {path} but failed to run: {exc}")
    if result.returncode != 0:
        return CheckResult("git_executable", False, f"git --version exited {result.returncode}")
    return CheckResult("git_executable", True, result.stdout.strip())


def _check_opencode_executable(executable: str) -> CheckResult:
    path = shutil.which(executable)
    if path is None:
        return CheckResult(
            "opencode_executable",
            False,
            f"{executable!r} not found on PATH; install OpenCode "
            "(https://opencode.ai) or pass --opencode-executable",
        )
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(
            "opencode_executable", False, f"{executable!r} found at {path} but failed to run: {exc}"
        )
    if result.returncode != 0:
        return CheckResult(
            "opencode_executable", False, f"{executable!r} --version exited {result.returncode}"
        )
    version = result.stdout.strip() or result.stderr.strip()
    return CheckResult("opencode_executable", True, f"{path} ({version})")


def _check_git_repo(project_root: Path) -> CheckResult:
    try:
        GitRepo(project_root)
    except GitError as exc:
        return CheckResult("git_repository", False, str(exc))
    return CheckResult("git_repository", True, f"{project_root} is a git repository")


def _check_clean_and_attached(project_root: Path) -> CheckResult:
    try:
        repo = GitRepo(project_root)
    except GitError as exc:
        return CheckResult("git_clean_worktree", False, f"cannot check working tree state: {exc}")
    try:
        branch = repo.current_branch()
    except GitError as exc:
        return CheckResult("git_clean_worktree", False, str(exc))
    if not repo.is_clean():
        return CheckResult(
            "git_clean_worktree",
            False,
            f"working tree on branch {branch!r} has uncommitted changes",
        )
    return CheckResult("git_clean_worktree", True, f"clean working tree on branch {branch!r}")


def _check_opencode_json(project_root: Path) -> CheckResult:
    config_path = project_root / "opencode.json"
    if not config_path.exists():
        return CheckResult("opencode_json", False, f"{config_path} does not exist")
    try:
        raw = config_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult("opencode_json", False, f"{config_path} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        return CheckResult("opencode_json", False, f"{config_path} does not contain a JSON object")
    return CheckResult("opencode_json", True, f"{config_path} parses as valid JSON")


def _check_external_directory_permission(project_root: Path) -> CheckResult:
    """The supervisor creates task worktrees as siblings one directory
    above the project root by default, so an agent that reads outside
    its own worktree (e.g. the auditor reading a sibling task's diff,
    or reading logs under the project's parent) needs
    `external_directory` to allow that parent path -- not just the
    project root itself. See `cli.py`'s `cmd_init_copy` and ADR 0014.
    """
    config_path = project_root / "opencode.json"
    if not config_path.exists():
        return CheckResult("external_directory_permission", False, f"{config_path} does not exist")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult(
            "external_directory_permission", False, f"{config_path} is not valid JSON: {exc}"
        )
    if not isinstance(data, dict):
        return CheckResult(
            "external_directory_permission", False, f"{config_path} does not contain a JSON object"
        )

    external = data.get("permission", {}).get("external_directory")
    parent = str(project_root.parent)

    if external == "allow":
        return CheckResult(
            "external_directory_permission",
            True,
            "external_directory is unconditionally 'allow'",
        )
    if not isinstance(external, dict):
        return CheckResult(
            "external_directory_permission",
            False,
            "permission.external_directory is not set; task worktrees are "
            f"created under {parent}, which will be denied by default",
        )

    matched_allow = any(
        pattern in (parent, "*") and action == "allow" for pattern, action in external.items()
    )
    if matched_allow:
        return CheckResult(
            "external_directory_permission",
            True,
            f"permission.external_directory allows {parent}",
        )
    return CheckResult(
        "external_directory_permission",
        False,
        f"permission.external_directory does not appear to allow {parent} "
        "(the sibling task-worktree parent directory); agents that read "
        "outside their own worktree will be denied",
    )


def _check_agent_files(project_root: Path) -> CheckResult:
    agents_dir = project_root / ".opencode" / "agents"
    if not agents_dir.is_dir():
        return CheckResult("agent_definitions", False, f"{agents_dir} does not exist")
    missing = [name for name in _REQUIRED_AGENT_FILES if not (agents_dir / name).is_file()]
    if missing:
        return CheckResult(
            "agent_definitions", False, f"missing agent definitions: {', '.join(missing)}"
        )
    return CheckResult(
        "agent_definitions", True, f"all four agent definitions present in {agents_dir}"
    )


def _check_dotenv_present(project_root: Path) -> CheckResult:
    env_path = project_root / ".env"
    if not env_path.is_file():
        return CheckResult(
            "dotenv_file",
            False,
            f"{env_path} does not exist (copy .env.example to .env and fill in credentials)",
        )
    return CheckResult("dotenv_file", True, f"{env_path} exists")


def _check_project_config(project_root: Path) -> CheckResult:
    """Parse `loop-supervisor.toml` and confirm each configured command's
    executable token resolves on `PATH` -- but never execute a command.
    A missing file is `ok`: both worktree provisioning and supervisor-run
    verification are opt-in, off-by-default features (see ADR 0025), so
    a project that never created this file needs no diagnostic at all.
    Executing a command here would violate the offline/fast preflight
    contract this module otherwise holds (ADR 0022).
    """
    config_path = project_root / "loop-supervisor.toml"
    if not config_path.is_file():
        return CheckResult(
            "project_config", True, f"{config_path} does not exist (provisioning/verify disabled)"
        )
    try:
        config = load_project_config(config_path)
    except ConfigError as exc:
        return CheckResult("project_config", False, f"{config_path} is invalid: {exc}")

    # Mirror build_agent_env's PATH construction (opencode.py) so a
    # command that only resolves via the project's own .venv/bin isn't
    # reported as missing: verification commands are expected to run
    # inside a task worktree with exactly that PATH prepended, not the
    # ambient PATH this preflight itself runs under.
    venv_bin = str(project_root / ".venv" / "bin")
    search_path = os.pathsep.join([venv_bin, os.environ.get("PATH", "")])

    unresolved = []
    for command in (*config.provision_commands, *config.verify_commands):
        executable = command.split(maxsplit=1)[0]
        if shutil.which(executable, path=search_path) is None:
            unresolved.append(executable)
    if unresolved:
        return CheckResult(
            "project_config",
            False,
            f"{config_path} parses, but not found on PATH: {', '.join(sorted(set(unresolved)))}",
        )
    return CheckResult(
        "project_config",
        True,
        f"{config_path} parses; "
        f"{len(config.provision_commands)} provision command(s), "
        f"{len(config.verify_commands)} verify command(s) configured",
    )


def env_status() -> dict[str, dict[str, Any]]:
    """Set/unset status of each known provider env var. Values are never
    included, only whether each is set -- safe to print or log."""
    return {var: {"set": bool(os.environ.get(var))} for var in _KNOWN_PROVIDER_ENVS}


def run_checks(project_root: Path, *, opencode_executable: str = "opencode") -> list[CheckResult]:
    """Run every preflight check and return results in a fixed, stable
    order (used for both human and --json output)."""
    return [
        _check_python_version(),
        _check_git_executable(),
        _check_opencode_executable(opencode_executable),
        _check_git_repo(project_root),
        _check_clean_and_attached(project_root),
        _check_opencode_json(project_root),
        _check_external_directory_permission(project_root),
        _check_agent_files(project_root),
        _check_dotenv_present(project_root),
        _check_project_config(project_root),
    ]


def validate_report(project_root: Path, *, opencode_executable: str = "opencode") -> dict[str, Any]:
    """Structured validation result for `--json` consumers (including the
    `adopt-loop-supervisor` skill's step-0 check): overall pass/fail, the
    per-check breakdown, and env-var set/unset status -- enough for a
    caller to decide what to fix without re-implementing this module's
    checks."""
    checks = run_checks(project_root, opencode_executable=opencode_executable)
    return {
        "ok": all(c.ok for c in checks),
        "checks": {c.name: c.to_dict() for c in checks},
        "env": env_status(),
    }
