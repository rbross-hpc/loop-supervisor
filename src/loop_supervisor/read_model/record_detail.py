"""Bounded literal record-detail payloads for read-only presentation."""

from __future__ import annotations

import json
from collections.abc import Mapping

MAX_RECORD_RENDERED_BYTES = 256 * 1024
MAX_RECORD_RENDERED_LINES = 10_000


def serialize_raw_json(value: object) -> tuple[str, bool]:
    """Serialize a validated value and bound it at UTF-8 code-point boundaries."""
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return _truncate(rendered)


def format_opinionated_content(value: object) -> str:
    """Return a bounded, human-oriented view of an already validated payload.

    Raw JSON deliberately remains available separately for troubleshooting.  This
    summary names the fields an operator needs to understand a role outcome or
    operational failure without duplicating that JSON representation.
    """
    if not isinstance(value, dict):
        return "Unavailable: validated detail has an unsupported shape."
    if "error_id" in value:
        rendered = _format_error(value)
    elif "disposition" in value:
        rendered = _format_auditor_result(value)
    elif "implementation_summary" in value:
        rendered = _format_builder_result(value)
    elif "adr" in value and "question" in value:
        rendered = _format_architect_result(value)
    elif "status" in value and "objective" in value:
        rendered = _format_planner_result(value)
    elif "commands" in value:
        rendered = _format_verification_result(value)
    else:
        rendered = "Validated detail is available in the raw JSON view."
    bounded, _truncated = _truncate(rendered)
    return bounded


def _format_planner_result(value: Mapping[object, object]) -> str:
    return _join_fields(
        "Planner result",
        ("Status", value.get("status")),
        ("Task ID", value.get("task_id")),
        ("Objective", value.get("objective")),
        ("Rationale", value.get("rationale")),
        ("Acceptance criteria", value.get("acceptance_criteria")),
        ("Relevant files", value.get("relevant_files")),
        ("Decision required", value.get("decision_required")),
        ("Decision question", value.get("decision_question")),
    )


def _format_architect_result(value: Mapping[object, object]) -> str:
    adr = value.get("adr")
    adr_fields: tuple[tuple[str, object], ...] = ()
    if isinstance(adr, dict):
        adr_fields = (
            ("ADR title", adr.get("title")),
            ("ADR decision", adr.get("decision")),
            ("ADR consequences", adr.get("consequences")),
        )
    return _join_fields(
        "Architect result",
        ("Status", value.get("status")),
        ("Question", value.get("question")),
        ("Rationale", value.get("rationale")),
        ("Input request", value.get("input_request")),
        *adr_fields,
    )


def _format_builder_result(value: Mapping[object, object]) -> str:
    return _join_fields(
        "Builder result",
        ("Status", value.get("status")),
        ("Task ID", value.get("task_id")),
        ("Objective", value.get("objective")),
        ("Implementation summary", value.get("implementation_summary")),
        ("Tests run", value.get("tests_run")),
        ("Test results", value.get("test_results")),
        ("Open concerns", value.get("open_concerns")),
        ("Commit", value.get("commit")),
    )


def _format_auditor_result(value: Mapping[object, object]) -> str:
    return _join_fields(
        "Auditor result",
        ("Disposition", value.get("disposition")),
        ("Task ID", value.get("task_id")),
        ("Objective", value.get("objective")),
        ("Findings", value.get("findings")),
        ("Required changes", value.get("required_changes")),
        ("Decision required", value.get("decision_required")),
        ("Decision question", value.get("decision_question")),
    )


def _format_verification_result(value: Mapping[object, object]) -> str:
    return _join_fields("Verification result", ("Commands", value.get("commands")))


def _format_error(value: Mapping[object, object]) -> str:
    return _join_fields(
        "Operational error",
        ("Kind", value.get("kind")),
        ("Operation", value.get("operation")),
        ("Failed phase", value.get("failed_phase")),
        ("Message", value.get("message")),
        ("Retryable", value.get("retryable")),
        ("Retry phase", value.get("retry_phase")),
        ("Requires repair", value.get("requires_repair")),
        ("Recovery hint", value.get("recovery_hint")),
        ("Occurred", value.get("occurred_at")),
    )


def _join_fields(title: str, *fields: tuple[str, object]) -> str:
    lines = [title]
    for label, value in fields:
        if value is None or value == []:
            continue
        if isinstance(value, list):
            lines.append(f"{label}:")
            lines.extend(f"  - {item}" for item in value)
        else:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


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
