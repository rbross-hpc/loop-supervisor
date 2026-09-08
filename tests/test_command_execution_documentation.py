"""Regression coverage for `[provision]`/`[verify]` command documentation.

Every configured command string is tokenized with `shlex.split` and
executed directly (`commands.py`) -- never through an implicit shell.
Shell operators like `&&` are therefore inert rather than interpreted,
and a compound example that relies on one is actively misleading (it
fails immediately if copied verbatim). These tests pin every
user-facing example to that contract and reject the `&&`-joined
provisioning example that used to appear here.
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

_COMMAND_EXAMPLE_DOCUMENTATION = (
    _REPO_ROOT / "README.md",
    _REPO_ROOT / "src" / "loop_supervisor" / "_skeleton" / "README.md.tmpl",
    _REPO_ROOT
    / "src"
    / "loop_supervisor"
    / "_skills"
    / "adopt-loop-supervisor"
    / "references"
    / "toolchain.md",
    _REPO_ROOT
    / "docs"
    / "decisions"
    / "0025-loop-supervisor-toml-is-the-project-config-channel.md",
)

# A `commands = [...]` list entry joined with a literal `&&` inside one
# quoted string is invalid: each entry is one argv, not a shell line.
_INVALID_COMPOUND_COMMAND_RE = re.compile(r"commands\s*=\s*\[[^\]]*&&[^\]]*\]")


def test_no_documentation_shows_an_invalid_shell_joined_command_entry():
    for path in _COMMAND_EXAMPLE_DOCUMENTATION:
        text = path.read_text(encoding="utf-8")
        assert not _INVALID_COMPOUND_COMMAND_RE.search(text), path


def test_provisioning_examples_use_separate_list_entries():
    """The canonical two-step venv-provisioning example must be two
    separate `commands` entries, not one `&&`-joined string."""
    for path in _COMMAND_EXAMPLE_DOCUMENTATION:
        text = path.read_text(encoding="utf-8")
        if "python3 -m venv .venv" not in text and "uv venv" not in text:
            continue
        assert (
            '"uv venv", "uv pip install' in text
            or "'.[dev]'\"]" in text
            or ("python3 -m venv .venv" in text and ".venv/bin/pip install" in text)
        ), path


def test_readme_states_the_no_shell_execution_contract():
    text = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "never through a shell" in text or "not through a shell" in text
    assert "shlex" in text


def test_cli_help_states_direct_argv_execution():
    text = (_REPO_ROOT / "src" / "loop_supervisor" / "cli.py").read_text(encoding="utf-8")
    assert "not through a shell" in text


def test_provisioning_recovery_hint_does_not_recommend_editing_the_config_file():
    """resume reconstructs provision/verify commands from the persisted
    run, never from loop-supervisor.toml -- the recovery hint for a
    failed provisioning command must not claim otherwise."""
    text = (_REPO_ROOT / "src" / "loop_supervisor" / "supervisor.py").read_text(encoding="utf-8")
    marker = text.index("isinstance(exc, ProvisioningError)")
    hint_source = text[marker : marker + 700]
    assert "cannot repair this run" in hint_source
    assert "start a new run" in hint_source
