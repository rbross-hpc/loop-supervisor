import os
from pathlib import Path
from typing import Any

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

    assert len(discovered.attempts) == 1
    assert discovered.attempts[0].log is None
    assert any("duplicate ordinal" in diagnostic.reason for diagnostic in discovered.diagnostics)
    assert any(
        "mismatched output path" in diagnostic.reason for diagnostic in discovered.diagnostics
    )


def test_discover_verification_preserves_attempts_without_metadata_or_available_log(tmp_path):
    missing = discover_verification(tmp_path, "run-1", None)
    assert missing.attempts == ()
    assert missing.diagnostics == ()

    result = _result(tmp_path)
    relative = _result(tmp_path, output_path="loop-supervisor/verification/run-1/path.log")
    traversal = _result(
        tmp_path,
        output_path=str(_directory(tmp_path) / ".." / ".." / "outside.log"),
    )
    for metadata in (result, relative, traversal):
        discovered = discover_verification(tmp_path, "run-1", metadata)
        assert len(discovered.attempts) == 1
        assert discovered.attempts[0].log is None
        assert any(
            "unavailable" in item.reason or "mismatched" in item.reason
            for item in discovered.diagnostics
        )


def test_discovery_preserves_attempt_when_expected_log_is_pruned_or_directory_is_symlinked(
    tmp_path, monkeypatch
):
    expected = _directory(tmp_path) / "01.log"
    pruned = discover_verification(tmp_path, "run-1", _result(tmp_path))
    assert len(pruned.attempts) == 1
    assert pruned.attempts[0].log is None
    assert any("unavailable" in item.reason for item in pruned.diagnostics)

    expected.parent.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    expected.parent.symlink_to(outside, target_is_directory=True)
    symlinked = discover_verification(tmp_path, "run-1", _result(tmp_path))
    assert len(symlinked.attempts) == 1
    assert symlinked.attempts[0].log is None
    assert any("unavailable" in item.reason for item in symlinked.diagnostics)

    # A leaf lost after enumeration remains represented as unavailable.
    expected.parent.unlink()
    expected.parent.mkdir()
    expected.write_text("will disappear")
    original_stat = verification.os.stat

    def remove_leaf_then_stat(name, *args, **kwargs):
        if name == "01.log":
            os.unlink(expected)
        return original_stat(name, *args, **kwargs)

    monkeypatch.setattr(verification.os, "stat", remove_leaf_then_stat)
    disappeared = discover_verification(tmp_path, "run-1", _result(tmp_path))
    assert len(disappeared.attempts) == 1
    assert disappeared.attempts[0].log is None
    assert any("unavailable" in item.reason for item in disappeared.diagnostics)


def test_discovery_keeps_metadata_when_run_directory_is_symlinked(tmp_path):
    directory = _directory(tmp_path)
    metadata = _result(tmp_path, output_path=str(directory / "01.log"))
    os.rmdir(directory)
    run_directory = directory.parent
    os.rmdir(run_directory)
    outside = tmp_path / "outside-run"
    outside.mkdir()
    run_directory.symlink_to(outside, target_is_directory=True)

    discovered = discover_verification(tmp_path, "run-1", metadata)

    assert len(discovered.attempts) == 1
    assert discovered.attempts[0].log is None
    assert any(
        "verification directory is unavailable" in item.reason for item in discovered.diagnostics
    )


def test_discovery_reports_malformed_metadata_without_raising(tmp_path):
    discovered = discover_verification(tmp_path, "run-1", {"commands": []})

    assert discovered.attempts == ()
    assert discovered.diagnostics == (
        verification.VerificationDiagnostic("run-1", "invalid verification metadata"),
    )


def test_discovery_and_read_log_reject_oversized_filename_ordinal(tmp_path, monkeypatch):
    directory = _directory(tmp_path)
    (directory / "01.log").write_text("expected")
    oversized_name = f"{'9' * 129}.log"
    (directory / oversized_name).write_text("oversized ordinal")
    original_isinstance = isinstance

    class GuardedIntMeta(type):
        def __instancecheck__(self, value: object) -> bool:
            return original_isinstance(value, int)

    class GuardedInt(int, metaclass=GuardedIntMeta):
        def __new__(cls, value: Any = 0) -> "GuardedInt":
            if value == oversized_name[:-4]:
                raise AssertionError("oversized ordinal was converted")
            return super().__new__(cls, value)

    monkeypatch.setattr(verification, "int", GuardedInt, raising=False)
    discovered = discover_verification(tmp_path, "run-1", _result(tmp_path))

    assert discovered.attempts[0].log is not None
    assert any(
        item.artifact == oversized_name and item.reason == "log ordinal is too large"
        for item in discovered.diagnostics
    )
    unavailable = read_log(
        tmp_path,
        verification.LogReference("run-1", COMMIT, 9, oversized_name),
    )
    assert not unavailable.available
    assert unavailable.diagnostic == "invalid log reference"


def test_discovery_reports_bounded_scans(tmp_path, monkeypatch):
    directory = _directory(tmp_path)
    (directory / "01.log").write_text("expected")
    (directory / "02.log").write_text("overflow")
    monkeypatch.setattr(verification, "_MAX_LOG_LEAVES", 1)
    logs_bounded = discover_verification(tmp_path, "run-1", _result(tmp_path))
    assert any("log scan is incomplete" in item.reason for item in logs_bounded.diagnostics)

    (directory.parent / ("b" * 40)).mkdir()
    monkeypatch.setattr(verification, "_MAX_COMMIT_DIRECTORIES", 1)
    commits_bounded = discover_verification(tmp_path, "run-1", _result(tmp_path))
    assert any(
        "commit directory scan is incomplete" in item.reason for item in commits_bounded.diagnostics
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


def test_discovery_diagnoses_nonregular_leaf_without_opening_it(tmp_path, monkeypatch):
    directory = _directory(tmp_path)
    nonregular = directory / "01.log"
    nonregular.mkdir()
    original_open = verification.os.open
    opened_leaves: list[str] = []

    def track_leaf_open(name, flags, *args, **kwargs):
        if name == "01.log":
            opened_leaves.append(name)
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(verification.os, "open", track_leaf_open)

    discovered = discover_verification(tmp_path, "run-1", _result(tmp_path))

    assert discovered.attempts[0].log is None
    assert any(item.artifact == "01.log" for item in discovered.diagnostics)
    assert opened_leaves == []


def test_read_log_is_unavailable_when_leaf_disappears_after_read(tmp_path, monkeypatch):
    directory = _directory(tmp_path)
    log = directory / "01.log"
    log.write_text("read before pruning")
    reference = discover_verification(tmp_path, "run-1", _result(tmp_path)).attempts[0].log
    assert reference is not None
    original_open = verification.os.open
    leaf_opens = 0

    def prune_before_post_read_check(name, flags, *args, **kwargs):
        nonlocal leaf_opens
        if name == "01.log":
            leaf_opens += 1
            if leaf_opens == 2:
                os.unlink(log)
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(verification.os, "open", prune_before_post_read_check)

    content = read_log(tmp_path, reference)

    assert leaf_opens == 2
    assert not content.available
    assert content.text == ""
    assert content.diagnostic == "log is unavailable"


def test_read_log_reports_replacement_after_first_fstat(tmp_path, monkeypatch):
    directory = _directory(tmp_path)
    log = directory / "01.log"
    log.write_text("opened inode")
    reference = discover_verification(tmp_path, "run-1", _result(tmp_path)).attempts[0].log
    assert reference is not None
    original_open = verification.os.open
    leaf_opens = 0

    def replace_before_post_read_open(name, flags, *args, **kwargs):
        nonlocal leaf_opens
        if name == "01.log":
            leaf_opens += 1
            if leaf_opens == 2:
                replacement = directory / "replacement.log"
                replacement.write_text("replacement inode")
                os.replace(replacement, log)
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(verification.os, "open", replace_before_post_read_open)

    content = read_log(tmp_path, reference)

    assert leaf_opens == 2
    assert content.available
    assert content.text == "opened inode"
    assert content.changed_during_read is True


def test_read_log_reports_in_place_mutation_of_opened_inode(tmp_path, monkeypatch):
    directory = _directory(tmp_path)
    log = directory / "01.log"
    log.write_text("before mutation")
    reference = discover_verification(tmp_path, "run-1", _result(tmp_path)).attempts[0].log
    assert reference is not None
    original_fstat = verification.os.fstat
    fstat_calls = 0

    def mutate_before_post_read_fstat(fd):
        nonlocal fstat_calls
        fstat_calls += 1
        if fstat_calls == 2:
            log.write_text("after in-place mutation has a different size")
        return original_fstat(fd)

    monkeypatch.setattr(verification.os, "fstat", mutate_before_post_read_fstat)

    content = read_log(tmp_path, reference)

    assert fstat_calls == 3
    assert content.available
    assert content.text == "before mutation"
    assert content.changed_during_read is True
