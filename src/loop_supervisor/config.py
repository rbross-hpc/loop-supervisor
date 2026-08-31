"""Project-level configuration for optional worktree provisioning and
supervisor-run verification, read from `loop-supervisor.toml` at the
project root.

This is a distinct channel from `opencode.json` (which belongs to
OpenCode, not loop-supervisor) and distinct from `.env` (a credential
channel, loaded via `python-dotenv`, never a settings channel). See
ADR 0025 for why this project, this format, and this precedence.

`tomllib` is stdlib on Python >= 3.11 (this project's minimum), so
reading this file costs zero new dependencies.

A missing file is not an error: both provisioning and verification
default to off (empty command lists), so a project that never creates
`loop-supervisor.toml` behaves exactly as it did before this module
existed.
"""

from __future__ import annotations

import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_PROVISION_TIMEOUT = 600.0
DEFAULT_VERIFY_TIMEOUT = 900.0

_KNOWN_TABLES = frozenset({"provision", "verify"})
_KNOWN_PROVISION_KEYS = frozenset({"commands", "timeout"})
_KNOWN_VERIFY_KEYS = frozenset({"commands", "timeout"})


class ConfigError(RuntimeError):
    """Raised for an invalid `loop-supervisor.toml`."""


@dataclass(frozen=True)
class ProjectConfig:
    """Parsed, validated project configuration.

    `provision_commands`/`verify_commands` are shell-style command
    lines, parsed with `shlex.split` at the point of execution (never
    `shell=True`) -- see `commands.py`. An empty tuple means the
    corresponding feature is off.
    """

    provision_commands: tuple[str, ...] = ()
    provision_timeout: float = DEFAULT_PROVISION_TIMEOUT
    verify_commands: tuple[str, ...] = ()
    verify_timeout: float = DEFAULT_VERIFY_TIMEOUT


def _validate_commands(value: Any, *, table: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"[{table}].commands must be a list of strings")
    for command in value:
        if not command.strip():
            raise ConfigError(f"[{table}].commands entries must not be blank")
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise ConfigError(f"[{table}].commands entry {command!r} is not valid: {exc}") from exc
        if not argv:
            raise ConfigError(f"[{table}].commands entry {command!r} has no executable token")
    return tuple(value)


def _validate_timeout(value: Any, *, table: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"[{table}].timeout must be a number")
    if value <= 0:
        raise ConfigError(f"[{table}].timeout must be a positive number")
    return float(value)


def parse_project_config(raw: dict[str, Any]) -> ProjectConfig:
    """Validate a parsed TOML document's top-level structure and values.

    Unknown top-level tables and unknown keys within `[provision]`/
    `[verify]` are rejected outright, matching the strictness
    `RunOptions.from_dict` already applies elsewhere in this project:
    a typo in a config key should fail loudly, not be silently
    ignored.
    """
    unknown_tables = set(raw) - _KNOWN_TABLES
    if unknown_tables:
        raise ConfigError(f"unknown loop-supervisor.toml table(s): {sorted(unknown_tables)}")

    provision_commands: tuple[str, ...] = ()
    provision_timeout = DEFAULT_PROVISION_TIMEOUT
    provision = raw.get("provision")
    if provision is not None:
        if not isinstance(provision, dict):
            raise ConfigError("[provision] must be a table")
        unknown_keys = set(provision) - _KNOWN_PROVISION_KEYS
        if unknown_keys:
            raise ConfigError(f"unknown [provision] key(s): {sorted(unknown_keys)}")
        if "commands" in provision:
            provision_commands = _validate_commands(provision["commands"], table="provision")
        if "timeout" in provision:
            provision_timeout = _validate_timeout(provision["timeout"], table="provision")

    verify_commands: tuple[str, ...] = ()
    verify_timeout = DEFAULT_VERIFY_TIMEOUT
    verify = raw.get("verify")
    if verify is not None:
        if not isinstance(verify, dict):
            raise ConfigError("[verify] must be a table")
        unknown_keys = set(verify) - _KNOWN_VERIFY_KEYS
        if unknown_keys:
            raise ConfigError(f"unknown [verify] key(s): {sorted(unknown_keys)}")
        if "commands" in verify:
            verify_commands = _validate_commands(verify["commands"], table="verify")
        if "timeout" in verify:
            verify_timeout = _validate_timeout(verify["timeout"], table="verify")

    return ProjectConfig(
        provision_commands=provision_commands,
        provision_timeout=provision_timeout,
        verify_commands=verify_commands,
        verify_timeout=verify_timeout,
    )


def load_project_config(path: Path) -> ProjectConfig:
    """Load and validate a `loop-supervisor.toml` file at `path`.

    A missing file returns the all-off default rather than raising --
    the file is entirely optional. An existing but malformed file
    (bad TOML syntax, or content that fails `parse_project_config`'s
    validation) raises `ConfigError`, since a project that bothered to
    create the file presumably wanted it honored, not silently
    ignored.
    """
    if not path.is_file():
        return ProjectConfig()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path} could not be read: {exc}") from exc
    return parse_project_config(raw)
