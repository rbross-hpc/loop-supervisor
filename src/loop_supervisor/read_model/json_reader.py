"""Bounded, descriptor-relative JSON reads for supervisor artifacts."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

JSON_READ_LIMIT = 4 * 1024 * 1024
"""Maximum structured JSON bytes accepted before parsing."""

JSON_READ_SENTINEL_LIMIT = JSON_READ_LIMIT + 1
"""Bytes read to detect an artifact exceeding :data:`JSON_READ_LIMIT`."""

MAX_JSON_NESTING = 64
MAX_JSON_NODES = 100_000
MAX_JSON_MEMBERS = 10_000
MAX_JSON_STRING_BYTES = 1024 * 1024


class BoundedJsonError(RuntimeError):
    """Raised when a structured artifact cannot be securely read or bounded."""


def read_bounded_json(directory_fd: int, name: str, display_path: Path) -> Any:
    """Securely read, parse, and shape-check one JSON leaf below ``directory_fd``.

    The descriptor-relative, no-follow, nonblocking open and regular-file check
    keep callers from following a substituted leaf or blocking on special files.
    No bytes reach the JSON parser when the sentinel proves the configured hard
    read limit was exceeded.
    """
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | _required_open_flag("O_NOFOLLOW"),
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise BoundedJsonError(f"cannot securely open JSON file {display_path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BoundedJsonError(f"JSON file {display_path} is not a regular file")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(JSON_READ_SENTINEL_LIMIT)
    finally:
        if fd != -1:
            os.close(fd)

    if len(raw) == JSON_READ_SENTINEL_LIMIT:
        raise BoundedJsonError(
            f"JSON file {display_path} is oversized (limit is {JSON_READ_LIMIT} bytes)"
        )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise BoundedJsonError(f"JSON file {display_path} is malformed: {exc}") from exc
    _check_json_shape(value, display_path)
    return value


def _required_open_flag(name: str) -> int:
    value = getattr(os, name, None)
    if not isinstance(value, int):
        raise BoundedJsonError(f"secure JSON reads require os.{name}; this platform is unsupported")
    return value


def _check_string_size(value: str, display_path: Path) -> None:
    if len(value.encode("utf-8")) > MAX_JSON_STRING_BYTES:
        raise BoundedJsonError(
            f"JSON file {display_path} exceeds the {MAX_JSON_STRING_BYTES} byte string limit"
        )


def _check_json_shape(value: Any, display_path: Path) -> None:
    """Iteratively enforce JSON shape limits without recursive traversal."""
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise BoundedJsonError(
                f"JSON file {display_path} exceeds the {MAX_JSON_NODES} node limit"
            )
        if isinstance(item, str):
            _check_string_size(item, display_path)
            continue
        if isinstance(item, dict):
            if depth >= MAX_JSON_NESTING:
                raise BoundedJsonError(
                    f"JSON file {display_path} exceeds the {MAX_JSON_NESTING} nesting limit"
                )
            if len(item) > MAX_JSON_MEMBERS:
                raise BoundedJsonError(
                    f"JSON file {display_path} exceeds the {MAX_JSON_MEMBERS} members limit"
                )
            for key, child in item.items():
                _check_string_size(key, display_path)
                stack.append((child, depth + 1))
            continue
        if isinstance(item, list):
            if depth >= MAX_JSON_NESTING:
                raise BoundedJsonError(
                    f"JSON file {display_path} exceeds the {MAX_JSON_NESTING} nesting limit"
                )
            if len(item) > MAX_JSON_MEMBERS:
                raise BoundedJsonError(
                    f"JSON file {display_path} exceeds the {MAX_JSON_MEMBERS} members limit"
                )
            stack.extend((child, depth + 1) for child in item)
