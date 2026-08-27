"""Strict structured-output contracts for the loop roles.

Each role (planner, architect, builder, auditor) must return exactly one
JSON object matching one of the models below. Models are configured to
forbid unknown fields so that drift between an agent prompt and the
supervisor's expectations fails loudly instead of silently.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator


class ContractError(ValueError):
    """Raised when a role's structured output cannot be validated."""


def _parse_json_object(raw: str) -> dict[str, Any]:
    """Parse `raw` as a single JSON object, rejecting anything else.

    Tolerates two specific model habits, both despite explicit
    instructions not to do them:

    - A single leading/trailing markdown code fence (```json ... ```)
      wrapping the entire output.
    - A leading prose preamble before the object (e.g. "Confirmed: ...
      Here is the plan:\n\n{...}"), by decoding from the first '{' in
      the text once whole-text parsing fails. This also covers a
      preamble followed by a fenced object (the fence's own opening
      ```` ```json ```` line is skipped over the same way other prose
      is; only its closing ``` needs explicit tolerance, since it is
      the sole content left after the object).

    Deliberately does NOT tolerate: trailing content after the object
    (other than a bare leftover fence closer), a second object anywhere
    in the text, or a preamble that itself contains an unrelated '{'.
    In each of those cases the first '{' either fails to decode as a
    complete object or leaves other non-whitespace text after it, and
    parsing fails loudly rather than guessing which of several
    candidate objects was the real one -- a validation layer should
    reject ambiguous output, not silently resolve it.
    """
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        start = text.find("{")
        if start == -1:
            raise ContractError(f"output is not valid JSON: {exc}") from exc
        try:
            value, end = json.JSONDecoder().raw_decode(text, start)
        except json.JSONDecodeError:
            raise ContractError(f"output is not valid JSON: {exc}") from exc
        trailing = text[end:].strip()
        if trailing == "```":
            trailing = ""
        if trailing:
            raise ContractError(f"output is not valid JSON: {exc}") from exc

    if not isinstance(value, dict):
        raise ContractError("output must be a single JSON object")

    return value


class PlannerStatus(str, Enum):
    READY = "READY"
    COMPLETE = "COMPLETE"


class BuilderStatus(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    BLOCKED = "BLOCKED"


class AuditorDisposition(str, Enum):
    ACCEPT = "ACCEPT"
    REVISE = "REVISE"
    REPLAN = "REPLAN"


class ArchitectStatus(str, Enum):
    DECIDED = "DECIDED"
    NEEDS_INPUT = "NEEDS_INPUT"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlannerResult(StrictModel):
    status: PlannerStatus
    task_id: str | None = None
    objective: str | None = None
    rationale: str | None = None
    acceptance_criteria: list[str] = []
    relevant_files: list[str] = []
    design_questions: list[str] = []
    decision_required: bool = False
    decision_question: str | None = None
    decision_rationale: str | None = None

    @model_validator(mode="after")
    def _check_status_fields(self) -> PlannerResult:
        if self.status is PlannerStatus.READY:
            missing = [
                name
                for name in ("task_id", "objective", "rationale")
                if getattr(self, name) in (None, "")
            ]
            if missing:
                raise ValueError(f"status READY requires non-empty fields: {', '.join(missing)}")
            if not self.acceptance_criteria:
                raise ValueError("status READY requires at least one acceptance criterion")
        if self.decision_required:
            if self.status is not PlannerStatus.READY:
                raise ValueError("decision_required=true requires status READY")
            if not self.decision_question:
                raise ValueError("decision_required=true requires decision_question")
            if not self.decision_rationale:
                raise ValueError("decision_required=true requires decision_rationale")
        return self

    @classmethod
    def parse(cls, raw: str) -> PlannerResult:
        return _parse_model(cls, raw)


class BuilderResult(StrictModel):
    task_id: str
    objective: str
    status: BuilderStatus
    implementation_summary: str
    implementation_strategy: list[str] = []
    tests_run: list[str] = []
    test_results: list[str] = []
    files_changed: list[str] = []
    commit: str | None = None
    open_concerns: list[str] = []

    @model_validator(mode="after")
    def _check_status_fields(self) -> BuilderResult:
        if self.status is BuilderStatus.COMPLETE and not self.commit:
            raise ValueError("status COMPLETE requires a commit hash")
        return self

    @classmethod
    def parse(cls, raw: str) -> BuilderResult:
        return _parse_model(cls, raw)


class AuditorResult(StrictModel):
    task_id: str
    objective: str
    disposition: AuditorDisposition
    findings: list[str] = []
    required_changes: list[str] = []
    design_observations: list[str] = []
    decision_required: bool = False
    decision_question: str | None = None
    decision_rationale: str | None = None

    @model_validator(mode="after")
    def _check_decision_fields(self) -> AuditorResult:
        if self.decision_required:
            if self.disposition is not AuditorDisposition.REPLAN:
                raise ValueError("decision_required=true requires disposition REPLAN")
            if not self.decision_question:
                raise ValueError("decision_required=true requires decision_question")
            if not self.decision_rationale:
                raise ValueError("decision_required=true requires decision_rationale")
        if self.disposition is AuditorDisposition.REVISE and not self.required_changes:
            raise ValueError("disposition REVISE requires at least one required_changes entry")
        return self

    @classmethod
    def parse(cls, raw: str) -> AuditorResult:
        return _parse_model(cls, raw)


class ArchitectADR(StrictModel):
    title: str
    context: str
    decision: str
    consequences: list[str] = []


class ArchitectResult(StrictModel):
    status: ArchitectStatus
    question: str
    rationale: str
    adr: ArchitectADR | None = None
    input_request: str | None = None

    @model_validator(mode="after")
    def _check_status_fields(self) -> ArchitectResult:
        if self.status is ArchitectStatus.DECIDED and self.adr is None:
            raise ValueError("status DECIDED requires an adr object")
        if self.status is ArchitectStatus.NEEDS_INPUT and not self.input_request:
            raise ValueError("status NEEDS_INPUT requires input_request")
        return self

    @classmethod
    def parse(cls, raw: str) -> ArchitectResult:
        return _parse_model(cls, raw)


def _parse_model(model_cls: type[BaseModel], raw: str) -> Any:
    data = _parse_json_object(raw)
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        raise ContractError(f"{model_cls.__name__} failed validation: {exc}") from exc


def check_decision_answered(*, requested_question: str, answered_question: str) -> None:
    """Raise ContractError if the architect's result answers a different
    question than the one it was asked to resolve."""
    if answered_question != requested_question:
        raise ContractError(
            f"architect answered {answered_question!r} but was asked "
            f"to resolve {requested_question!r}"
        )


def check_task_identity(
    *,
    task_id: str,
    objective: str,
    other_task_id: str,
    other_objective: str,
    other_role: str,
) -> None:
    """Raise ContractError if a downstream role's task identity drifted.

    `task_id` must match exactly: it is the sole identifier carrying task
    identity across roles, and drift there means the role is reporting on
    the wrong task. `objective` is not required to match verbatim -- no
    role's prompt asks for a verbatim echo, and downstream roles routinely
    (and legitimately) paraphrase or summarize the objective rather than
    quoting it. It is only checked for presence, to catch a role that
    dropped the field entirely.
    """
    if other_task_id != task_id:
        raise ContractError(
            f"{other_role} task_id {other_task_id!r} does not match expected {task_id!r}"
        )
    if not other_objective.strip():
        raise ContractError(f"{other_role} objective must not be empty for {task_id!r}")
