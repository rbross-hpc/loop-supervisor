import json
import os
from pathlib import Path

import pytest

import loop_supervisor.read_model.history as history
from loop_supervisor.read_model import HistoryStatus, load_history


def _record(run_id: str, seq: int, phase: str = "planning") -> dict[str, object]:
    return {
        "seq": seq,
        "run_id": run_id,
        "phase": phase,
        "phase_after": "creating_worktree",
        "status": "advanced",
        "recorded_at": "2026-01-01T00:00:00+00:00",
        "original_task_id": None,
        "counters": {
            "accepted_task_count": 0,
            "revision_count": 0,
            "replan_count": 0,
            "architect_retry_count": 0,
            "builder_guidance_count": 0,
        },
        "result": {
            "status": "COMPLETE",
            "task_id": None,
            "objective": None,
            "rationale": None,
            "acceptance_criteria": [],
            "relevant_files": [],
            "design_questions": [],
            "decision_required": False,
            "decision_question": None,
            "decision_rationale": None,
        },
        "error": None,
    }


def _write_history(tmp_path: Path, name: str, record: dict[str, object] | str) -> Path:
    directory = tmp_path / "loop-supervisor" / "runs" / "run-1"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(record if isinstance(record, str) else json.dumps(record))
    return path


def test_load_history_reports_absent_when_history_directory_is_missing(tmp_path):
    loaded = load_history(tmp_path, "run-1")

    assert loaded.entries == ()
    assert loaded.completeness is HistoryStatus.ABSENT
    assert loaded.diagnostics == ()


def test_load_history_returns_valid_records_in_numeric_sequence_order(tmp_path):
    _write_history(tmp_path, "0002-planning.json", _record("run-1", 2))
    _write_history(tmp_path, "0001-planning.json", _record("run-1", 1))

    loaded = load_history(tmp_path, "run-1")

    assert [entry.seq for entry in loaded.entries] == [1, 2]
    assert all(entry.has_result and not entry.has_error for entry in loaded.entries)
    assert loaded.completeness is HistoryStatus.COMPLETE
    assert loaded.diagnostics == ()


def test_load_history_reports_sequence_gaps_without_filling_them(tmp_path):
    _write_history(tmp_path, "0001-planning.json", _record("run-1", 1))
    _write_history(tmp_path, "0003-planning.json", _record("run-1", 3))

    loaded = load_history(tmp_path, "run-1")

    assert [entry.seq for entry in loaded.entries] == [1, 3]
    assert loaded.completeness is HistoryStatus.INCOMPLETE
    assert any("gap" in diagnostic.reason for diagnostic in loaded.diagnostics)


def test_load_history_excludes_all_duplicate_sequence_records(tmp_path):
    _write_history(tmp_path, "0001-planning.json", _record("run-1", 1))
    _write_history(tmp_path, "0001-building.json", _record("run-1", 1, "building"))
    _write_history(tmp_path, "0002-planning.json", _record("run-1", 2))

    loaded = load_history(tmp_path, "run-1")

    assert [entry.seq for entry in loaded.entries] == [2]
    assert loaded.completeness is HistoryStatus.INCOMPLETE
    assert any("duplicate" in diagnostic.reason for diagnostic in loaded.diagnostics)


def test_load_history_excludes_truncated_record_but_preserves_valid_siblings(tmp_path):
    _write_history(tmp_path, "0001-planning.json", _record("run-1", 1))
    _write_history(tmp_path, "0002-planning.json", '{"seq": 2')
    _write_history(tmp_path, "0003-planning.json", _record("run-1", 3))

    loaded = load_history(tmp_path, "run-1")

    assert [entry.seq for entry in loaded.entries] == [1, 3]
    assert loaded.completeness is HistoryStatus.INCOMPLETE
    assert any(
        diagnostic.seq == 2 and "malformed" in diagnostic.reason
        for diagnostic in loaded.diagnostics
    )


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_load_history_rejects_symlinked_and_non_regular_leaves(tmp_path, kind):
    target = _write_history(tmp_path, "valid.json", "{}")
    directory = target.parent
    if kind == "symlink":
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps(_record("run-1", 1)))
        (directory / "0001-planning.json").symlink_to(outside)
    else:
        (directory / "0001-planning.json").mkdir()

    loaded = load_history(tmp_path, "run-1")

    assert loaded.entries == ()
    assert loaded.completeness is HistoryStatus.INCOMPLETE
    assert any(
        "regular file" in diagnostic.reason or "open" in diagnostic.reason
        for diagnostic in loaded.diagnostics
    )


def test_load_history_excludes_identity_mismatched_record(tmp_path):
    _write_history(tmp_path, "0001-planning.json", _record("other-run", 1))

    loaded = load_history(tmp_path, "run-1")

    assert loaded.entries == ()
    assert loaded.completeness is HistoryStatus.INCOMPLETE
    assert any("run_id" in diagnostic.reason for diagnostic in loaded.diagnostics)


def test_load_history_reports_record_that_disappears_after_enumeration(tmp_path, monkeypatch):
    record_path = _write_history(tmp_path, "0001-planning.json", _record("run-1", 1))
    original_read = history.read_bounded_json

    def remove_then_read(directory_fd: int, name: str, display_path: Path):
        os.unlink(record_path)
        return original_read(directory_fd, name, display_path)

    monkeypatch.setattr(history, "read_bounded_json", remove_then_read)

    loaded = load_history(tmp_path, "run-1")

    assert loaded.entries == ()
    assert loaded.completeness is HistoryStatus.INCOMPLETE
    assert any(
        diagnostic.seq == 1 and "open" in diagnostic.reason for diagnostic in loaded.diagnostics
    )


def test_load_history_diagnoses_oversized_filename_sequence_without_crashing(tmp_path, monkeypatch):
    directory = tmp_path / "loop-supervisor" / "runs" / "run-1"
    directory.mkdir(parents=True)
    oversized_name = f"{'9' * 5_000}-planning.json"
    monkeypatch.setattr(history.os, "listdir", lambda _fd: [oversized_name])

    loaded = load_history(tmp_path, "run-1")

    assert loaded.entries == ()
    assert loaded.completeness is HistoryStatus.INCOMPLETE
    assert len(loaded.diagnostics) == 1
    assert loaded.diagnostics[0].reason == "filename sequence is too large"
    assert len(loaded.diagnostics[0].artifact) < 300


def test_load_history_reports_huge_sparse_sequence_as_one_bounded_gap(tmp_path):
    huge_seq = 99_999_999_999_999_999_999
    _write_history(tmp_path, "0001-planning.json", _record("run-1", 1))
    _write_history(tmp_path, f"{huge_seq}-planning.json", _record("run-1", huge_seq))

    loaded = load_history(tmp_path, "run-1")

    assert [entry.seq for entry in loaded.entries] == [1, huge_seq]
    assert loaded.completeness is HistoryStatus.INCOMPLETE
    assert [(diagnostic.seq, diagnostic.reason) for diagnostic in loaded.diagnostics] == [
        (2, f"sequence gap from 2 to {huge_seq - 1}")
    ]


def test_history_entry_counters_are_immutable(tmp_path):
    _write_history(tmp_path, "0001-planning.json", _record("run-1", 1))

    entry = load_history(tmp_path, "run-1").entries[0]

    with pytest.raises(TypeError, match="mappingproxy.*does not support item assignment"):
        entry.counters["revision_count"] = 1
    assert entry.counters["revision_count"] == 0
