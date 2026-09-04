import json
import os
import socket
from pathlib import Path

import pytest

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
    hostname: str | None = None,
    pid: int | None = None,
    run_id: str | None = "run-1",
    integration_path: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "token": TOKEN,
        "pid": os.getpid() if pid is None else pid,
        "hostname": socket.gethostname() if hostname is None else hostname,
        "started_at": "2026-01-01T00:00:00Z",
        "operation": "run",
        "run_id": run_id,
        "integration_path": str(tmp_path) if integration_path is None else integration_path,
    }


def _write_lock(tmp_path: Path, record: dict[str, object]) -> Path:
    directory = tmp_path / "loop-supervisor"
    directory.mkdir(exist_ok=True)
    path = directory / "supervisor.lock"
    path.write_text(json.dumps(record))
    return path


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
            LockActivity.STALE,
        ),
        (
            lambda tmp_path: _write_lock(
                tmp_path, _lock_record(tmp_path) | {"integration_path": None}
            ),
            LockActivity.MISMATCHED,
        ),
        (
            lambda tmp_path: _write_lock(tmp_path, _lock_record(tmp_path, run_id=None)),
            LockActivity.FRESH_RUN_UNASSOCIATED,
        ),
        (
            lambda tmp_path: _write_lock(tmp_path, _lock_record(tmp_path, run_id="unknown")),
            LockActivity.UNASSOCIATED,
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

    assert observation.activity is LockActivity.MISMATCHED


def test_observe_lock_reports_identity_mismatched_named_run(tmp_path, monkeypatch):
    _write_lock(tmp_path, _lock_record(tmp_path))
    import loop_supervisor.read_model.lock_observation as lock_observation

    class State:
        run_id = "different-run"
        integration_path = str(tmp_path)

    monkeypatch.setattr(lock_observation, "load_state", lambda *_: State())

    observation = observe_lock(tmp_path, tmp_path, (_summary(),))

    assert observation.activity is LockActivity.MISMATCHED


def test_observe_lock_labels_only_identity_agreeing_named_run_as_running(tmp_path, monkeypatch):
    _write_lock(tmp_path, _lock_record(tmp_path))
    import loop_supervisor.read_model.lock_observation as lock_observation

    class State:
        run_id = "run-1"
        integration_path = str(tmp_path)

    monkeypatch.setattr(lock_observation, "load_state", lambda *_: State())

    observation = observe_lock(tmp_path, tmp_path, (_summary(), _summary("other")))

    assert observation.activity is LockActivity.LOCAL_LIVE_ASSOCIATED
    assert observation.activities == (
        RunActivity(run_id="run-1", label=ActivityLabel.RUNNING),
        RunActivity(run_id="other", label=ActivityLabel.NOT_EVIDENCED_RUNNING),
    )
