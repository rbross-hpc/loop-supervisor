"""Bounded literal record-detail payloads for read-only presentation."""

from __future__ import annotations

import json

MAX_RECORD_RENDERED_BYTES = 256 * 1024
MAX_RECORD_RENDERED_LINES = 10_000


def serialize_raw_json(value: object) -> tuple[str, bool]:
    """Serialize a validated value and bound it at UTF-8 code-point boundaries."""
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return _truncate(rendered)


def format_opinionated_content(value: object) -> str:
    """Return a bounded readable literal representation of validated content."""
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    bounded, _truncated = _truncate(rendered)
    return bounded


def _truncate(value: str) -> tuple[str, bool]:
    """Apply the ADR's byte and line limits without splitting Unicode code points."""
    lines = value.splitlines(keepends=True)
    selected: list[str] = []
    used_bytes = 0
    for line in lines[:MAX_RECORD_RENDERED_LINES]:
        available = MAX_RECORD_RENDERED_BYTES - used_bytes
        bounded_line = _prefix_for_bytes(line, available)
        selected.append(bounded_line)
        used_bytes += len(bounded_line.encode("utf-8"))
        if bounded_line != line:
            return "".join(selected), True
    if len(lines) > MAX_RECORD_RENDERED_LINES:
        return "".join(selected), True
    return value, False


def _prefix_for_bytes(value: str, limit: int) -> str:
    """Return the longest prefix whose UTF-8 encoding fits within ``limit``."""
    if len(value.encode("utf-8")) <= limit:
        return value
    selected: list[str] = []
    used_bytes = 0
    for character in value:
        character_bytes = len(character.encode("utf-8"))
        if used_bytes + character_bytes > limit:
            break
        selected.append(character)
        used_bytes += character_bytes
    return "".join(selected)
