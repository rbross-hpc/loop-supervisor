"""Tests for `config.py`'s `loop-supervisor.toml` parsing and validation."""

import pytest

from loop_supervisor.config import (
    ConfigError,
    ProjectConfig,
    load_project_config,
    parse_project_config,
)


def test_load_project_config_missing_file_returns_all_off_default(tmp_path):
    config = load_project_config(tmp_path / "loop-supervisor.toml")
    assert config == ProjectConfig()
    assert config.provision_commands == ()
    assert config.verify_commands == ()


def test_load_project_config_parses_both_tables(tmp_path):
    path = tmp_path / "loop-supervisor.toml"
    path.write_text(
        "[provision]\n"
        'commands = ["python3 -m venv .venv", ".venv/bin/pip install -e \'.[dev]\'"]\n'
        "timeout = 123\n"
        "\n"
        "[verify]\n"
        'commands = ["pytest -q", "ruff check ."]\n'
        "timeout = 456\n"
    )
    config = load_project_config(path)
    assert config.provision_commands == (
        "python3 -m venv .venv",
        ".venv/bin/pip install -e '.[dev]'",
    )
    assert config.provision_timeout == 123.0
    assert config.verify_commands == ("pytest -q", "ruff check .")
    assert config.verify_timeout == 456.0


def test_load_project_config_defaults_timeouts_when_omitted(tmp_path):
    path = tmp_path / "loop-supervisor.toml"
    path.write_text('[verify]\ncommands = ["pytest -q"]\n')
    config = load_project_config(path)
    assert config.verify_timeout == 900.0
    assert config.provision_timeout == 600.0


def test_load_project_config_raises_on_invalid_toml_syntax(tmp_path):
    path = tmp_path / "loop-supervisor.toml"
    path.write_text("not valid [ toml")
    with pytest.raises(ConfigError):
        load_project_config(path)


def test_parse_project_config_rejects_unknown_table():
    with pytest.raises(ConfigError, match="unknown"):
        parse_project_config({"mystery": {}})


def test_parse_project_config_rejects_unknown_provision_key():
    with pytest.raises(ConfigError, match="provision"):
        parse_project_config({"provision": {"commands": ["x"], "surprise": 1}})


def test_parse_project_config_rejects_unknown_verify_key():
    with pytest.raises(ConfigError, match="verify"):
        parse_project_config({"verify": {"commands": ["x"], "surprise": 1}})


def test_parse_project_config_rejects_non_table_provision():
    with pytest.raises(ConfigError):
        parse_project_config({"provision": "not a table"})


def test_parse_project_config_rejects_non_list_commands():
    with pytest.raises(ConfigError):
        parse_project_config({"verify": {"commands": "pytest -q"}})


def test_parse_project_config_rejects_non_string_command_entry():
    with pytest.raises(ConfigError):
        parse_project_config({"verify": {"commands": [123]}})


def test_parse_project_config_rejects_blank_command():
    with pytest.raises(ConfigError):
        parse_project_config({"verify": {"commands": ["   "]}})


def test_parse_project_config_rejects_unparseable_shell_syntax():
    with pytest.raises(ConfigError):
        parse_project_config({"verify": {"commands": ["pytest 'unterminated"]}})


def test_parse_project_config_rejects_non_number_timeout():
    with pytest.raises(ConfigError):
        parse_project_config({"verify": {"timeout": "fast"}})


def test_parse_project_config_rejects_bool_timeout():
    with pytest.raises(ConfigError):
        parse_project_config({"verify": {"timeout": True}})


def test_parse_project_config_rejects_zero_timeout():
    with pytest.raises(ConfigError):
        parse_project_config({"verify": {"timeout": 0}})


def test_parse_project_config_rejects_negative_timeout():
    with pytest.raises(ConfigError):
        parse_project_config({"provision": {"timeout": -5}})


def test_parse_project_config_empty_document_is_all_off():
    config = parse_project_config({})
    assert config == ProjectConfig()


def test_load_project_config_wraps_unreadable_file_as_config_error(tmp_path):
    path = tmp_path / "loop-supervisor.toml"
    path.write_text('[verify]\ncommands = ["pytest -q"]\n')
    path.chmod(0o000)
    try:
        with pytest.raises(ConfigError):
            load_project_config(path)
    finally:
        path.chmod(0o644)
