import json
import os
import socket
from pathlib import Path

import pytest

from loop_supervisor.locking import IdentityStatus
from loop_supervisor.read_model import (
    ActivityLabel,
    LockActivity,
    RunActivity,
    RunSummary,
    observe_lock,
)

TOKEN = "secret-ownership-token"


def _summary(run_id: str = "run-1", *, loadable: bool = True) -> RunSummary:
    return RunSummary(
        run_id=run_id,
        loadable=loadable,
        phase="planning" if loadable else None,
        created_at=None,
        updated_at=None,
        diagnostic=None if loadable else "unloadable",
    )


def _lock_record(
    tmp_path: Path,
    *,
    schema_version: int = 1,
    hostname: str | None = None,
    pid: int | None = None,
    run_id: str | None = "run-1",
    integration_path: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": schema_version,
        "token": TOKEN,
        "pid": os.getpid() if pid is None else pid,
        "hostname": socket.gethostname() if hostname is None else hostname,
        "started_at": "2026-01-01T00:00:00Z",
        "operation": "run",
        "run_id": run_id,
        "integration_path": str(tmp_path) if integration_path is None else integration_path,
    }
    if schema_version == 2:
        record |= {"owner_boot_id": "boot-id", "owner_process_start": "12345"}
    return record


def _write_lock(tmp_path: Path, record: dict[str, object]) -> Path:
    directory = tmp_path / "loop-supervisor"
    directory.mkdir(exist_ok=True)
    path = directory / "supervisor.lock"
    path.write_text(json.dumps(record))
    return path


@pytest.mark.parametrize("bad_schema_version", [True, False, 2.0, 1.0, "2", None, [2]])
def test_observe_lock_rejects_non_integer_schema_version(tmp_path, bad_schema_version):
    """Mirrors the writer's strict integer check in locking.py: bool is an
    int subclass and float(1.0) == 1, so plain equality would wrongly
    accept a boolean or float schema_version."""
    _write_lock(tmp_path, _lock_record(tmp_path) | {"schema_version": bad_schema_version})

    observation = observe_lock(tmp_path, tmp_path, (_summary(),))

    assert observation.activity is LockActivity.MALFORMED


def test_observe_lock_rejects_float_schema_version_on_genuine_v2_record(tmp_path):
    """A float schema version is malformed even when valid schema-2 owner
    identity fields are present, isolating strict type validation."""
    record = _lock_record(tmp_path) | {
        "schema_version": 2,
        "owner_boot_id": "valid-boot-id",
        "owner_process_start": "valid-process-start",
    }
    assert record["schema_version"] == 2
    assert record["owner_boot_id"] == "valid-boot-id"
    assert record["owner_process_start"] == "valid-process-start"
    _write_lock(tmp_path, record | {"schema_version": 2.0})

    observation = observe_lock(tmp_path, tmp_path, (_summary(),))

    assert observation.activity is LockActivity.MALFORMED
    assert observation.diagnostic == "Supervisor lock could not be read or validated."


def test_observe_lock_sanitizes_selected_path_canonicalization_failure(tmp_path):
    missing_integration_path = tmp_path / "missing-selected-integration"

    observation = observe_lock(tmp_path, missing_integration_path, ())

    assert observation.activity is LockActivity.MISMATCHED
    assert observation.diagnostic == "Selected integration path could not be canonicalized."
    assert str(missing_integration_path) not in observation.diagnostic
    assert "No such file or directory" not in observation.diagnostic


def test_observe_lock_sanitizes_malformed_lock_parser_failure(tmp_path):
    lock_path = tmp_path / "loop-supervisor" / "supervisor.lock"
    lock_path.parent.mkdir()
    lock_path.write_text("{")

    observation = observe_lock(tmp_path, tmp_path, ())

    assert observation.activity is LockActivity.MALFORMED
    assert observation.diagnostic == "Supervisor lock could not be read or validated."
    assert str(lock_path) not in observation.diagnostic
    assert "Expecting property name enclosed in double quotes" not in observation.diagnostic


@pytest.mark.parametrize(
    ("prepare", "expected"),
    [
        (lambda tmp_path: None, LockActivity.ABSENT),
        (
            lambda tmp_path: _write_lock(tmp_path, {"not": "a lock"}),
            LockActivity.MALFORMED,
        ),
        (
            lambda tmp_path: _write_lock(tmp_path, _lock_record(tmp_path, hostname="other-host")),
            LockActivity.REMOTE,
        ),
        (
            lambda tmp_path: _write_lock(tmp_path, _lock_record(tmp_path, pid=999_999_999)),
            LockActivity.LEGACY_UNVERIFIED,
        ),
        (
            lambda tmp_path: _write_lock(
                tmp_path, _lock_record(tmp_path) | {"integration_path": None}
            ),
            LockActivity.LEGACY_UNVERIFIED,
        ),
        (
            lambda tmp_path: _write_lock(tmp_path, _lock_record(tmp_path, run_id=None)),
            LockActivity.LEGACY_UNVERIFIED,
        ),
        (
            lambda tmp_path: _write_lock(tmp_path, _lock_record(tmp_path, run_id="unknown")),
            LockActivity.LEGACY_UNVERIFIED,
        ),
    ],
)
def test_observe_lock_classifies_non_running_taxonomy(tmp_path, prepare, expected):
    prepare(tmp_path)

    observation = observe_lock(tmp_path, tmp_path, (_summary(),))

    assert observation.activity is expected
    assert observation.activities == (
        RunActivity(run_id="run-1", label=ActivityLabel.NOT_EVIDENCED_RUNNING),
    )
    assert TOKEN not in repr(observation)
    assert TOKEN not in str(observation)
    assert "token" not in observation.__dataclass_fields__


@pytest.mark.parametrize(
    ("identity_status", "expected_activity"),
    [
        (IdentityStatus.STALE, LockActivity.STALE),
        (IdentityStatus.UNVERIFIABLE, LockActivity.LOCAL_UNVERIFIABLE),
    ],
)
def test_observe_lock_reports_nonmatching_schema_2_identity_without_running(
    tmp_path, monkeypatch, identity_status, expected_activity
):
    _write_lock(tmp_path, _lock_record(tmp_path, schema_version=2))
    import loop_supervisor.read_model.lock_observation as lock_observation

    class State:
        run_id = "run-1"
        integration_path = str(tmp_path)

    monkeypatch.setattr(lock_observation, "load_state", lambda *_: State())
    monkeypatch.setattr(
        lock_observation,
        "classify_local_owner_identity",
        lambda *_: identity_status,
        raising=False,
    )

    observation = observe_lock(tmp_path, tmp_path, (_summary(),))

    assert observation.activity is expected_activity
    assert observation.activities == (
        RunActivity(run_id="run-1", label=ActivityLabel.NOT_EVIDENCED_RUNNING),
    )
    assert TOKEN not in repr(observation)


def test_observe_lock_labels_matching_schema_2_identity_as_running(tmp_path, monkeypatch):
    _write_lock(tmp_path, _lock_record(tmp_path, schema_version=2))
    import loop_supervisor.read_model.lock_observation as lock_observation

    class State:
        run_id = "run-1"
        integration_path = str(tmp_path)

    monkeypatch.setattr(lock_observation, "load_state", lambda *_: State())
    monkeypatch.setattr(
        lock_observation, "classify_local_owner_identity", lambda *_: IdentityStatus.MATCHING
    )

    observation = observe_lock(tmp_path, tmp_path, (_summary(),))

    assert observation.activity is LockActivity.LOCAL_IDENTITY_ASSOCIATED
    assert observation.owner_boot_id == "boot-id"
    assert observation.owner_process_start == "12345"
    assert observation.activities == (RunActivity(run_id="run-1", label=ActivityLabel.RUNNING),)


def test_observe_lock_reports_live_schema_1_lock_as_legacy_unverified(tmp_path):
    _write_lock(tmp_path, _lock_record(tmp_path))

    observation = observe_lock(tmp_path, tmp_path, (_summary(),))

    assert observation.activity is LockActivity.LEGACY_UNVERIFIED
    assert observation.activities == (
        RunActivity(run_id="run-1", label=ActivityLabel.NOT_EVIDENCED_RUNNING),
    )


def test_observe_lock_reports_absent_when_verified_directory_has_no_lock_leaf(tmp_path):
    (tmp_path / "loop-supervisor").mkdir()

    observation = observe_lock(tmp_path, tmp_path, ())

    assert observation.activity is LockActivity.ABSENT


def test_observe_lock_rejects_oversized_symlinked_and_non_regular_leaves(tmp_path):
    directory = tmp_path / "loop-supervisor"
    directory.mkdir()
    lock = directory / "supervisor.lock"
    lock.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    assert observe_lock(tmp_path, tmp_path, ()).activity is LockActivity.MALFORMED

    lock.unlink()
    outside = tmp_path / "outside.lock"
    outside.write_text(json.dumps(_lock_record(tmp_path)))
    lock.symlink_to(outside)
    assert observe_lock(tmp_path, tmp_path, ()).activity is LockActivity.MALFORMED

    lock.unlink()
    lock.mkdir()
    assert observe_lock(tmp_path, tmp_path, ()).activity is LockActivity.MALFORMED


def test_observe_lock_mismatch_precedes_null_or_unknown_run_association(tmp_path):
    _write_lock(
        tmp_path,
        _lock_record(tmp_path, run_id=None, integration_path=str(tmp_path / "other")),
    )

    observation = observe_lock(tmp_path, tmp_path, (_summary(),))

    assert observation.activity is LockActivity.LEGACY_UNVERIFIED


def test_observe_lock_reports_identity_mismatched_named_run(tmp_path, monkeypatch):
    _write_lock(tmp_path, _lock_record(tmp_path, schema_version=2))
    import loop_supervisor.read_model.lock_observation as lock_observation

    class State:
        run_id = "different-run"
        integration_path = str(tmp_path)

    monkeypatch.setattr(lock_observation, "load_state", lambda *_: State())
    monkeypatch.setattr(
        lock_observation, "classify_local_owner_identity", lambda *_: IdentityStatus.MATCHING
    )

    observation = observe_lock(tmp_path, tmp_path, (_summary(),))

    assert observation.activity is LockActivity.MISMATCHED


def test_observe_lock_labels_only_identity_agreeing_schema_2_run_as_running(tmp_path, monkeypatch):
    _write_lock(tmp_path, _lock_record(tmp_path, schema_version=2))
    import loop_supervisor.read_model.lock_observation as lock_observation

    class State:
        run_id = "run-1"
        integration_path = str(tmp_path)

    monkeypatch.setattr(lock_observation, "load_state", lambda *_: State())
    monkeypatch.setattr(
        lock_observation, "classify_local_owner_identity", lambda *_: IdentityStatus.MATCHING
    )

    observation = observe_lock(tmp_path, tmp_path, (_summary(), _summary("other")))

    assert observation.activity is LockActivity.LOCAL_IDENTITY_ASSOCIATED
    assert observation.activities == (
        RunActivity(run_id="run-1", label=ActivityLabel.RUNNING),
        RunActivity(run_id="other", label=ActivityLabel.NOT_EVIDENCED_RUNNING),
    )
