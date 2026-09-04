import os
from pathlib import Path

import loop_supervisor.read_model.verification as verification
from loop_supervisor.read_model import discover_verification, read_log

COMMIT = "a" * 40


def _directory(tmp_path: Path) -> Path:
    path = tmp_path / "loop-supervisor" / "verification" / "run-1" / COMMIT
    path.mkdir(parents=True, exist_ok=True)
    return path


def _result(tmp_path: Path, *, output_path: str | None = None) -> dict[str, object]:
    path = output_path or str(_directory(tmp_path) / "01.log")
    return {
        "ok": True,
        "commands": [
            {
                "command": "pytest",
                "ok": True,
                "returncode": 0,
                "timed_out": False,
                "duration": 0.1,
                "output_path": path,
                "summary": "safe summary",
            }
        ],
    }


def test_discover_verification_associates_only_exact_authorized_log_leaf(tmp_path):
    directory = _directory(tmp_path)
    (directory / "01.log").write_text("expected")
    (directory / "bad.log").write_text("ignored")
    (directory / "02.log").mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("outside")
    (directory / "03.log").symlink_to(outside)

    discovered = discover_verification(tmp_path, "run-1", _result(tmp_path))

    assert len(discovered.attempts) == 1
    attempt = discovered.attempts[0]
    assert attempt.commit == COMMIT
    assert attempt.ordinal == 1
    assert attempt.command == "pytest"
    assert attempt.log is not None
    assert {diagnostic.artifact for diagnostic in discovered.diagnostics} >= {
        "bad.log",
        "02.log",
        "03.log",
    }


def test_discover_verification_rejects_mismatched_output_path_and_duplicate_ordinals(tmp_path):
    directory = _directory(tmp_path)
    (directory / "01.log").write_text("one")
    (directory / "001.log").write_text("duplicate ordinal")

    discovered = discover_verification(
        tmp_path, "run-1", _result(tmp_path, output_path=str(tmp_path / "outside.log"))
    )

    assert discovered.attempts == ()
    assert any("duplicate ordinal" in diagnostic.reason for diagnostic in discovered.diagnostics)
    assert any(
        "mismatched output path" in diagnostic.reason for diagnostic in discovered.diagnostics
    )


def test_read_log_bounds_bytes_and_rendered_text(tmp_path):
    directory = _directory(tmp_path)
    (directory / "01.log").write_bytes(b"x" * (1024 * 1024 + 10))
    reference = discover_verification(tmp_path, "run-1", _result(tmp_path)).attempts[0].log
    assert reference is not None

    content = read_log(tmp_path, reference)

    assert content.available
    assert content.byte_truncated
    assert content.render_truncated
    assert len(content.text.encode("utf-8")) <= 256 * 1024


def test_read_log_bounds_lines_replaces_invalid_utf8_and_reports_disappearance(
    tmp_path, monkeypatch
):
    directory = _directory(tmp_path)
    log = directory / "01.log"
    log.write_bytes((b"\xff\n") * 10_001)
    reference = discover_verification(tmp_path, "run-1", _result(tmp_path)).attempts[0].log
    assert reference is not None

    content = read_log(tmp_path, reference)
    assert content.available
    assert "\ufffd" in content.text
    assert content.render_truncated
    assert len(content.text.splitlines()) <= 10_000

    original_open = verification.os.open

    def remove_then_open(name, flags, *args, **kwargs):
        if name == "01.log":
            os.unlink(log)
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(verification.os, "open", remove_then_open)
    unavailable = read_log(tmp_path, reference)
    assert not unavailable.available
    assert unavailable.text == ""
    assert unavailable.diagnostic == "log is unavailable"


def test_verification_diagnostics_do_not_expose_unchecked_metadata(tmp_path):
    _directory(tmp_path)
    secret = "secret-output-path-token"
    discovered = discover_verification(
        tmp_path, "run-1", _result(tmp_path, output_path=f"/{secret}")
    )

    assert all(secret not in diagnostic.reason for diagnostic in discovered.diagnostics)
    assert all(str(tmp_path) not in diagnostic.reason for diagnostic in discovered.diagnostics)
