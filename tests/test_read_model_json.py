import os
from pathlib import Path

import pytest

from loop_supervisor.read_model.json_reader import (
    JSON_READ_LIMIT,
    JSON_READ_SENTINEL_LIMIT,
    MAX_JSON_MEMBERS,
    MAX_JSON_NESTING,
    MAX_JSON_NODES,
    MAX_JSON_STRING_BYTES,
    BoundedJsonError,
    read_bounded_json,
)


def _read(tmp_path: Path, name: str):
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return read_bounded_json(directory_fd, name, tmp_path / name)
    finally:
        os.close(directory_fd)


def test_read_bounded_json_accepts_a_file_at_the_byte_limit(tmp_path):
    (tmp_path / "at-limit.json").write_bytes(b"{}" + b" " * (JSON_READ_LIMIT - 2))

    assert _read(tmp_path, "at-limit.json") == {}


def test_read_bounded_json_rejects_a_file_with_the_sentinel_byte(tmp_path):
    (tmp_path / "over-limit.json").write_bytes(b" " * JSON_READ_SENTINEL_LIMIT)

    with pytest.raises(BoundedJsonError, match="oversized"):
        _read(tmp_path, "over-limit.json")


@pytest.mark.parametrize(
    ("name", "payload", "match"),
    [
        ("string.json", b'"' + b"x" * (MAX_JSON_STRING_BYTES + 1) + b'"', "string"),
        (
            "deep.json",
            b"[" * (MAX_JSON_NESTING + 1) + b"0" + b"]" * (MAX_JSON_NESTING + 1),
            "nesting",
        ),
        ("nodes.json", b"[" + b"0," * MAX_JSON_NODES + b"0]", "node"),
        ("members.json", b"[" + b"0," * MAX_JSON_MEMBERS + b"0]", "members"),
    ],
)
def test_read_bounded_json_rejects_excessive_shape(tmp_path, name, payload, match):
    (tmp_path / name).write_bytes(payload)

    with pytest.raises(BoundedJsonError, match=match):
        _read(tmp_path, name)


def test_read_bounded_json_rejects_a_non_regular_target(tmp_path):
    (tmp_path / "directory.json").mkdir()

    with pytest.raises(BoundedJsonError, match="regular file"):
        _read(tmp_path, "directory.json")
