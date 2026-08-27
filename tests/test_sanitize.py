"""Tests for supervisor.py's persisted-error-message sanitization.

`_sanitize_message()` is the sole gate between an exception's raw text
(which can carry HTTP response bodies, server stdout, or repository
content) and the durable `OperationalErrorRecord.message` field that
ADR 0009 documents as never containing secrets or environment variables.
These tests pin what it actually does: best-effort redaction of
known-secret environment values and common credential formats, plus
head+tail truncation that keeps both the earliest and latest content
instead of discarding everything after a fixed prefix.
"""

from loop_supervisor.supervisor import _sanitize_message


def test_long_secret_named_env_value_is_redacted(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-" + "x" * 95)
    secret = "sk-ant-api03-" + "x" * 95
    msg = f"opencode serve failed: auth rejected for key {secret}"

    out = _sanitize_message(msg)

    assert secret not in out
    assert "[redacted:ANTHROPIC_API_KEY]" in out


def test_short_secret_named_env_value_is_not_redacted(monkeypatch):
    # A misconfigured/placeholder secret-named var with a short, common
    # value (e.g. a username typed into the wrong field) must not cause
    # unrelated substrings to be silently mangled. This is the concrete
    # hazard: OPENAI_API_KEY=rross would otherwise shred every mention
    # of a user's home directory.
    monkeypatch.setenv("OPENAI_API_KEY", "rross")
    msg = "GitError: git rev-parse HEAD failed in /home/rross/projects/repo"

    out = _sanitize_message(msg)

    assert out == msg


def test_non_secret_named_env_values_are_never_redacted(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "areallylongusernamethatisnotsecret")
    monkeypatch.setenv("HOME", "/home/areallylongusernamethatisnotsecret")
    msg = "path /home/areallylongusernamethatisnotsecret/repo not found"

    out = _sanitize_message(msg)

    assert out == msg


def test_pattern_backstop_redacts_key_not_in_our_environment(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # The OpenCode child process's own provider key, echoed back in a
    # crash message, is never in *our* environment at all -- only the
    # format-pattern backstop can catch this.
    msg = "provider rejected credential sk-ant-api03-abcdefghijklmnopqrstuvwx1234567890"

    out = _sanitize_message(msg)

    assert "sk-ant-api03-abcdefghijklmnopqrstuvwx1234567890" not in out
    assert "[redacted]" in out


def test_authorization_header_value_is_redacted():
    msg = "request failed\nAuthorization: Bearer sk-live-abcdefghijklmnopqrstuvwxyz\nstatus 401"

    out = _sanitize_message(msg)

    assert "sk-live-abcdefghijklmnopqrstuvwxyz" not in out
    assert "Bearer" not in out or "[redacted]" in out


def test_redaction_happens_before_truncation(monkeypatch):
    monkeypatch.setenv("FALDA_TOKEN", "f" * 48)
    secret = "f" * 48
    # Place the secret in the middle of a message long enough that a
    # naive truncate-then-redact ordering (or truncating before redacting
    # at all) could let it survive by luck of position, or could clip it
    # in half and defeat exact-match redaction.
    padding_before = "x" * 1900
    padding_after = "y" * 3000
    msg = f"{padding_before} secret={secret} {padding_after}"

    out = _sanitize_message(msg)

    assert secret not in out


def test_truncation_keeps_head_and_tail():
    head = "START-MARKER " + ("a" * 400)
    middle = "b" * 5000
    tail = "END-MARKER " + ("c" * 400)
    msg = f"{head}{middle}{tail}"

    out = _sanitize_message(msg)

    assert "START-MARKER" in out
    assert "END-MARKER" in out
    assert len(out) < len(msg)


def test_short_message_passes_through_unchanged():
    msg = "PhaseTimeoutError: agent loop-planner timed out after 1800.0s"

    out = _sanitize_message(msg)

    assert out == msg
