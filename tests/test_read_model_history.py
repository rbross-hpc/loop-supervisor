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
    first = _record("run-1", 1)
    first["phase_after"] = "planning"
    _write_history(tmp_path, "0002-planning.json", _record("run-1", 2))
    _write_history(tmp_path, "0001-planning.json", first)

    loaded = load_history(tmp_path, "run-1")

    assert [entry.seq for entry in loaded.entries] == [1, 2]
    assert all(entry.has_result and not entry.has_error for entry in loaded.entries)
    assert loaded.completeness is HistoryStatus.COMPLETE
    assert loaded.diagnostics == ()


@pytest.mark.parametrize("supplied_retry_count", [None, 2])
def test_load_history_accepts_legacy_and_extended_counter_sets(tmp_path, supplied_retry_count):
    record = _record("run-1", 1)
    counters = record["counters"]
    assert isinstance(counters, dict)
    if supplied_retry_count is not None:
        counters["operational_retry_count"] = supplied_retry_count
    _write_history(tmp_path, "0001-planning.json", record)

    loaded = load_history(tmp_path, "run-1")

    assert len(loaded.entries) == 1
    assert loaded.entries[0].counters["operational_retry_count"] == (supplied_retry_count or 0)
    assert loaded.completeness is HistoryStatus.COMPLETE
    assert loaded.diagnostics == ()


@pytest.mark.parametrize(
    "counter_change",
    [
        lambda counters: counters.update(unexpected_counter=0),
        lambda counters: counters.update(operational_retry_count=-1),
        lambda counters: counters.update(operational_retry_count=True),
    ],
)
def test_load_history_rejects_invalid_extended_counter_set_or_value(tmp_path, counter_change):
    record = _record("run-1", 1)
    counters = record["counters"]
    assert isinstance(counters, dict)
    counter_change(counters)
    _write_history(tmp_path, "0001-planning.json", record)

    loaded = load_history(tmp_path, "run-1")

    assert loaded.entries == ()
    assert loaded.completeness is HistoryStatus.INCOMPLETE
    assert [diagnostic.reason for diagnostic in loaded.diagnostics] == ["malformed history record"]


def _consistent_adjacent_records() -> tuple[dict[str, object], dict[str, object]]:
    first = _record("run-1", 1)
    second = _record("run-1", 2, "creating_worktree")
    second["phase_after"] = "planning"
    second["recorded_at"] = "2026-01-01T00:00:01+00:00"
    second["result"] = None
    return first, second


def test_load_history_diagnoses_adjacent_phase_discontinuity_without_omitting_records(tmp_path):
    first, second = _consistent_adjacent_records()
    second["phase"] = "awaiting_input"
    _write_history(tmp_path, "0001-planning.json", first)
    _write_history(tmp_path, "0002-awaiting_input.json", second)

    loaded = load_history(tmp_path, "run-1")

    assert [(entry.seq, entry.phase, entry.phase_after) for entry in loaded.entries] == [
        (1, "planning", "creating_worktree"),
        (2, "awaiting_input", "planning"),
    ]
    assert loaded.completeness is HistoryStatus.INCOMPLETE
    assert any("phase discontinuity" in diagnostic.reason for diagnostic in loaded.diagnostics)


def test_load_history_diagnoses_adjacent_recorded_timestamp_reversal_without_omitting_records(
    tmp_path,
):
    first, second = _consistent_adjacent_records()
    second["recorded_at"] = "2025-12-31T23:59:59+00:00"
    _write_history(tmp_path, "0001-planning.json", first)
    _write_history(tmp_path, "0002-creating_worktree.json", second)

    loaded = load_history(tmp_path, "run-1")

    assert [(entry.seq, entry.recorded_at) for entry in loaded.entries] == [
        (1, "2026-01-01T00:00:00+00:00"),
        (2, "2025-12-31T23:59:59+00:00"),
    ]
    assert loaded.completeness is HistoryStatus.INCOMPLETE
    assert any("timestamp reversal" in diagnostic.reason for diagnostic in loaded.diagnostics)


@pytest.mark.parametrize(
    ("counter", "phase", "phase_after"),
    [
        ("revision_count", "planning", "building"),
        ("revision_count", "planning", "architecting"),
        ("revision_count", "creating_worktree", "building"),
        ("revision_count", "creating_worktree", "architecting"),
        ("revision_count", "cleanup_branch", "planning"),
        ("replan_count", "cleanup_branch", "planning"),
        ("architect_retry_count", "recording_decision", "building"),
        ("architect_retry_count", "recording_decision", "planning"),
        ("architect_retry_count", "cleanup_branch", "planning"),
        ("builder_guidance_count", "planning", "building"),
        ("builder_guidance_count", "planning", "architecting"),
        ("builder_guidance_count", "creating_worktree", "building"),
        ("builder_guidance_count", "creating_worktree", "architecting"),
        ("builder_guidance_count", "building", "verifying"),
        ("builder_guidance_count", "building", "auditing"),
        ("builder_guidance_count", "cleanup_branch", "planning"),
    ],
)
def test_load_history_accepts_documented_counter_reset_to_zero(
    tmp_path, counter, phase, phase_after
):
    first = _record("run-1", 1)
    first["phase_after"] = phase
    second = _record("run-1", 2, phase)
    second["phase_after"] = phase_after
    second["recorded_at"] = "2026-01-01T00:00:01+00:00"
    second["result"] = None
    first_counters = first["counters"]
    second_counters = second["counters"]
    assert isinstance(first_counters, dict)
    assert isinstance(second_counters, dict)
    first_counters[counter] = 2
    _write_history(tmp_path, "0001-planning.json", first)
    _write_history(tmp_path, f"0002-{phase}.json", second)

    loaded = load_history(tmp_path, "run-1")

    assert [(entry.seq, entry.counters[counter]) for entry in loaded.entries] == [
        (1, 2),
        (2, 0),
    ]
    assert loaded.completeness is HistoryStatus.COMPLETE
    assert not any(
        f"counter regression for {counter}" in diagnostic.reason
        for diagnostic in loaded.diagnostics
    )


@pytest.mark.parametrize("phase_after", ["verifying", "auditing"])
@pytest.mark.parametrize(
    ("status", "expects_regression"),
    [("input_required", True), ("advanced", False)],
)
def test_load_history_permits_building_guidance_reset_only_after_successful_building_transition(
    tmp_path, phase_after, status, expects_regression
):
    first = _record("run-1", 1)
    first["phase_after"] = "building"
    first_counters = first["counters"]
    assert isinstance(first_counters, dict)
    first_counters["builder_guidance_count"] = 2
    following = _record("run-1", 2, "building")
    following["phase_after"] = phase_after
    following["status"] = status
    following["recorded_at"] = "2026-01-01T00:00:01+00:00"
    following["result"] = None
    _write_history(tmp_path, "0001-planning.json", first)
    _write_history(tmp_path, "0002-building.json", following)

    loaded = load_history(tmp_path, "run-1")

    has_regression = any(
        "counter regression for builder_guidance_count" in diagnostic.reason
        for diagnostic in loaded.diagnostics
    )
    assert has_regression is expects_regression


def test_load_history_diagnoses_counter_decrease_to_nonzero_without_omitting_records(tmp_path):
    first, second = _consistent_adjacent_records()
    first_counters = first["counters"]
    second_counters = second["counters"]
    assert isinstance(first_counters, dict)
    assert isinstance(second_counters, dict)
    first_counters["revision_count"] = 2
    second_counters["revision_count"] = 1
    _write_history(tmp_path, "0001-planning.json", first)
    _write_history(tmp_path, "0002-creating_worktree.json", second)

    loaded = load_history(tmp_path, "run-1")

    assert [(entry.seq, entry.counters["revision_count"]) for entry in loaded.entries] == [
        (1, 2),
        (2, 1),
    ]
    assert loaded.completeness is HistoryStatus.INCOMPLETE
    assert any(
        "counter regression for revision_count" in diagnostic.reason
        for diagnostic in loaded.diagnostics
    )


def test_load_history_diagnoses_counter_decrease_to_zero_on_unlisted_transition(tmp_path):
    first, second = _consistent_adjacent_records()
    first_counters = first["counters"]
    assert isinstance(first_counters, dict)
    first_counters["revision_count"] = 2
    _write_history(tmp_path, "0001-planning.json", first)
    _write_history(tmp_path, "0002-creating_worktree.json", second)

    loaded = load_history(tmp_path, "run-1")

    assert [(entry.seq, entry.counters["revision_count"]) for entry in loaded.entries] == [
        (1, 2),
        (2, 0),
    ]
    assert loaded.completeness is HistoryStatus.INCOMPLETE
    assert any(
        "counter regression for revision_count" in diagnostic.reason
        for diagnostic in loaded.diagnostics
    )


def test_load_history_retains_validated_detail_and_round_trippable_raw_json(tmp_path):
    record = _record("run-1", 1)
    result = record["result"]
    assert isinstance(result, dict)
    result["objective"] = "[bold]literal objective[/bold]"
    record["error"] = {
        "error_id": "history-error",
        "kind": "operational",
        "operation": "planning",
        "failed_phase": "planning",
        "retry_phase": None,
        "exception_type": "RuntimeError",
        "message": "[red]literal error[/red]",
        "retryable": False,
        "requires_repair": False,
        "recovery_hint": None,
        "occurred_at": "2026-01-01T00:00:00+00:00",
    }
    _write_history(tmp_path, "0001-planning.json", record)

    entry = load_history(tmp_path, "run-1").entries[0]

    assert entry.result_detail is not None
    assert "Planner result" in entry.result_detail
    assert "Status: COMPLETE" in entry.result_detail
    assert "Objective: [bold]literal objective[/bold]" in entry.result_detail
    assert '"objective":' not in entry.result_detail
    assert entry.error_detail is not None
    assert "Operational error" in entry.error_detail
    assert "Kind: operational" in entry.error_detail
    assert "Message: [red]literal error[/red]" in entry.error_detail
    assert '"message":' not in entry.error_detail
    assert entry.raw_json_truncated is False
    assert json.loads(entry.raw_json) == record


def test_load_history_truncates_oversized_serialized_raw_json_without_rejecting_record(tmp_path):
    record = _record("run-1", 1)
    result = record["result"]
    assert isinstance(result, dict)
    result["objective"] = "x" * (256 * 1024)
    _write_history(tmp_path, "0001-planning.json", record)

    entry = load_history(tmp_path, "run-1").entries[0]

    assert entry.has_result is True
    assert entry.raw_json_truncated is True
    assert len(entry.raw_json.encode("utf-8")) <= 256 * 1024
    assert len(entry.raw_json.splitlines()) <= 10_000


def test_load_history_reports_sequence_gaps_without_filling_them(tmp_path):
    _write_history(tmp_path, "0001-planning.json", _record("run-1", 1))
    _write_history(tmp_path, "0003-planning.json", _record("run-1", 3))

    loaded = load_history(tmp_path, "run-1")

    assert [entry.seq for entry in loaded.entries] == [1, 3]
    assert loaded.completeness is HistoryStatus.INCOMPLETE
    assert any("gap" in diagnostic.reason for diagnostic in loaded.diagnostics)


def test_load_history_does_not_compare_records_across_sequence_gap(tmp_path):
    first = _record("run-1", 1)
    first_counters = first["counters"]
    assert isinstance(first_counters, dict)
    first_counters["revision_count"] = 2
    following = _record("run-1", 3, "awaiting_input")
    following["phase_after"] = "planning"
    following["recorded_at"] = "2025-12-31T23:59:59+00:00"
    following["result"] = None
    following_counters = following["counters"]
    assert isinstance(following_counters, dict)
    following_counters["revision_count"] = 1
    _write_history(tmp_path, "0001-planning.json", first)
    _write_history(tmp_path, "0003-awaiting_input.json", following)

    loaded = load_history(tmp_path, "run-1")

    assert [entry.seq for entry in loaded.entries] == [1, 3]
    assert loaded.completeness is HistoryStatus.INCOMPLETE
    assert [(diagnostic.seq, diagnostic.reason) for diagnostic in loaded.diagnostics] == [
        (2, "sequence gap at 2")
    ]


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


@pytest.mark.parametrize(
    ("record_change", "raw_reason"),
    [
        (lambda record: record.update(seq=2), "embedded seq does not match filename"),
        (
            lambda record: record.update(run_id="other-run"),
            "embedded run_id does not match selected run",
        ),
        (lambda record: record.update(phase="building"), "embedded phase does not match filename"),
    ],
)
def test_load_history_sanitizes_embedded_filename_identity_mismatch(
    tmp_path, record_change, raw_reason
):
    record = _record("run-1", 1)
    record_change(record)
    _write_history(tmp_path, "0001-planning.json", record)

    loaded = load_history(tmp_path, "run-1")

    assert loaded.entries == ()
    assert loaded.completeness is HistoryStatus.INCOMPLETE
    assert [diagnostic.reason for diagnostic in loaded.diagnostics] == [
        "history record embedded seq, run_id, or phase mismatch"
    ]
    assert all(raw_reason not in diagnostic.reason for diagnostic in loaded.diagnostics)


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

    class _Entry:
        name = oversized_name

    class _Entries:
        def __enter__(self):
            return iter([_Entry()])

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(history.os, "scandir", lambda _fd: _Entries())

    loaded = load_history(tmp_path, "run-1")

    assert loaded.entries == ()
    assert loaded.completeness is HistoryStatus.INCOMPLETE
    assert len(loaded.diagnostics) == 1
    assert loaded.diagnostics[0].reason == "filename sequence is too large"
    assert len(loaded.diagnostics[0].artifact) < 300


def test_load_history_reports_huge_sparse_sequence_as_one_bounded_gap(tmp_path):
    huge_seq = 99_999_999_999_999_999_999
    first = _record("run-1", 1)
    following = _record("run-1", huge_seq, "creating_worktree")
    following["phase_after"] = "planning"
    following["recorded_at"] = "2026-01-01T00:00:01+00:00"
    following["result"] = None
    _write_history(tmp_path, "0001-planning.json", first)
    _write_history(tmp_path, f"{huge_seq}-creating_worktree.json", following)

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


def test_load_history_bounds_enumeration_and_reports_excess_leaves(tmp_path):
    _write_history(tmp_path, "0001-planning.json", _record("run-1", 1))
    directory = tmp_path / "loop-supervisor" / "runs" / "run-1"
    for index in range(10_000):
        (directory / f"temporary-{index}").touch()

    loaded = load_history(tmp_path, "run-1")

    assert [entry.seq for entry in loaded.entries] == [1]
    assert loaded.completeness is HistoryStatus.INCOMPLETE
    assert any(
        diagnostic.reason == "history directory has excess entries"
        for diagnostic in loaded.diagnostics
    )


def test_load_history_excludes_duplicate_sequence_with_unknown_phase_filename(tmp_path):
    _write_history(tmp_path, "0001-planning.json", _record("run-1", 1))
    _write_history(tmp_path, "0001-unknown.json", _record("run-1", 1))

    loaded = load_history(tmp_path, "run-1")

    assert loaded.entries == ()
    assert loaded.completeness is HistoryStatus.INCOMPLETE
    assert any(
        diagnostic.seq == 1 and "duplicate" in diagnostic.reason
        for diagnostic in loaded.diagnostics
    )


def test_load_history_diagnostics_redact_paths_and_unchecked_values_and_bound_artifacts(tmp_path):
    record = _record("run-1", 1)
    record["result"] = {"status": "NOT_A_STATUS", "task_id": "secret-sentinel"}
    _write_history(tmp_path, "0001-planning.json", record)
    _write_history(tmp_path, "0002-planning.json", "{")
    long_phase = "a" * 230
    _write_history(tmp_path, f"0003-{long_phase}.json", _record("run-1", 3))
    _write_history(tmp_path, f"0003-{'b' * 230}.json", _record("run-1", 3))

    loaded = load_history(tmp_path, "run-1")

    assert loaded.completeness is HistoryStatus.INCOMPLETE
    assert all(len(diagnostic.artifact) <= 256 for diagnostic in loaded.diagnostics)
    assert all(str(tmp_path) not in diagnostic.reason for diagnostic in loaded.diagnostics)
    assert all("NOT_A_STATUS" not in diagnostic.reason for diagnostic in loaded.diagnostics)
    assert all("secret-sentinel" not in diagnostic.reason for diagnostic in loaded.diagnostics)
