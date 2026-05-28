"""Runtime helpers for invoking the backlog agent from workflow state."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from orchestrator_agent.agent_tools.backlog_primer.agent import (
    root_agent as backlog_agent,
)
from orchestrator_agent.agent_tools.backlog_primer.schemes import (
    InputSchema,
    OutputSchema,
)
from services.agent_workbench.as_built_assessment import cached_assessment_for_backlog
from utils.adk_runner import (
    get_agent_model_info,
    invoke_agent_to_text,
    parse_json_payload,
)
from utils.failure_artifacts import (
    AgentInvocationError,
    FailureArtifactResult,
    FailureMetadataDict,
    write_failure_artifact,
)
from utils.runtime_config import BACKLOG_RUNNER_IDENTITY

logger: logging.Logger = logging.getLogger(name=__name__)

type BacklogInputContext = dict[str, object]
type ValidationErrors = list[dict[str, object]]


@dataclass(frozen=True)
class _FailureDetails:
    """Structured details describing a backlog-runtime failure."""

    message: str
    raw_text: str | None = None
    validation_errors: ValidationErrors | None = None
    exception: BaseException | None = None


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _normalize_prior_backlog_state(value: object) -> str:
    if value is None:
        return "NO_HISTORY"
    if isinstance(value, str):
        text = value.strip()
        return text if text else "NO_HISTORY"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "NO_HISTORY"


def _normalize_validation_errors(errors: object) -> ValidationErrors:
    normalized: ValidationErrors = []
    if not isinstance(errors, list):
        return normalized

    for error in errors:
        if not isinstance(error, Mapping):
            continue
        normalized.append({str(key): value for key, value in error.items()})
    return normalized


def build_backlog_input_context(
    state: dict[str, Any],
    *,
    user_input: str | None,
) -> BacklogInputContext:
    """Build the serialized backlog-agent input payload from workflow state."""
    vision_assessment = state.get("product_vision_assessment") or {}
    vision_stmt = vision_assessment.get("product_vision_statement") or ""
    implementation_evidence = _as_text(
        state.get("implementation_evidence_cached")
    ).strip()

    return {
        "product_vision_statement": vision_stmt,
        "technical_spec": _as_text(state.get("pending_spec_content")),
        "compiled_authority": _as_text(state.get("compiled_authority_cached")),
        "prior_backlog_state": _normalize_prior_backlog_state(
            state.get("backlog_items")
        ),
        "as_built_assessment": cached_assessment_for_backlog(state),
        "implementation_evidence": implementation_evidence or "NO_EVIDENCE",
        "user_input": user_input or "",
    }


async def _invoke_backlog_agent(payload: InputSchema) -> str:
    return await invoke_agent_to_text(
        agent=backlog_agent,
        runner_identity=BACKLOG_RUNNER_IDENTITY,
        payload_json=payload.model_dump_json(),
        no_text_error="Backlog agent returned no text response",
    )


def _failure(
    *,
    project_id: int,
    input_context: BacklogInputContext,
    failure_stage: str,
    details: _FailureDetails,
) -> dict[str, Any]:
    message: str = details.message
    artifact_result: FailureArtifactResult = write_failure_artifact(
        phase="backlog",
        project_id=project_id,
        failure_stage=failure_stage,
        failure_summary=message,
        raw_output=details.raw_text,
        context={"input_context": input_context},
        model_info={
            **get_agent_model_info(backlog_agent),
            "app_name": BACKLOG_RUNNER_IDENTITY.app_name,
            "user_id": BACKLOG_RUNNER_IDENTITY.user_id,
        },
        validation_errors=details.validation_errors,
        exception=details.exception,
    )
    metadata: FailureMetadataDict = artifact_result["metadata"]
    if details.exception is not None:
        logger.exception(
            "Backlog generation failed [artifact_id=%s stage=%s]: %s",
            metadata["failure_artifact_id"],
            failure_stage,
            message,
        )
    else:
        logger.error(
            "Backlog generation failed [artifact_id=%s stage=%s]: %s",
            metadata["failure_artifact_id"],
            failure_stage,
            message,
        )

    artifact: dict[str, Any] = {
        "error": "BACKLOG_GENERATION_FAILED",
        "message": message,
        "is_complete": False,
        "clarifying_questions": [],
        "failure_artifact_id": metadata["failure_artifact_id"],
        "failure_stage": metadata["failure_stage"],
        "failure_summary": metadata["failure_summary"],
        "raw_output_preview": metadata["raw_output_preview"],
        "has_full_artifact": metadata["has_full_artifact"],
    }

    return {
        "success": False,
        "input_context": input_context,
        "output_artifact": artifact,
        "is_complete": None,
        "error": message,
        **metadata,
    }


async def run_backlog_agent_from_state(
    state: dict[str, Any],
    *,
    project_id: int,
    user_input: str | None,
) -> dict[str, Any]:
    """Run the backlog agent from stored workflow state and normalize failures."""
    input_context: BacklogInputContext = build_backlog_input_context(
        state,
        user_input=user_input,
    )

    try:
        payload: InputSchema = InputSchema.model_validate(input_context)
    except ValidationError as exc:
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="input_validation",
            details=_FailureDetails(
                message=f"Backlog input validation failed: {exc}",
                validation_errors=_normalize_validation_errors(exc.errors()),
                exception=exc,
            ),
        )

    try:
        raw_text: str = await _invoke_backlog_agent(payload)
    except AgentInvocationError as exc:
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="invocation_exception",
            details=_FailureDetails(
                message=f"Backlog runtime failed: {exc}",
                raw_text=exc.partial_output,
                exception=exc,
            ),
        )
    except ValueError as exc:
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="invocation_exception",
            details=_FailureDetails(
                message=f"Backlog runtime failed: {exc}",
                exception=exc,
            ),
        )

    parsed: dict[str, Any] | None = parse_json_payload(raw_text)
    if parsed is None:
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="invalid_json",
            details=_FailureDetails(
                message="Backlog response is not valid JSON",
                raw_text=raw_text,
            ),
        )

    try:
        output_model: OutputSchema = OutputSchema.model_validate(parsed)
    except ValidationError as exc:
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="output_validation",
            details=_FailureDetails(
                message=f"Backlog output validation failed: {exc}",
                raw_text=raw_text,
                validation_errors=_normalize_validation_errors(exc.errors()),
                exception=exc,
            ),
        )

    output_artifact: dict[str, Any] = output_model.model_dump(exclude_none=True)
    if _has_clarifying_questions(output_artifact):
        output_artifact["is_complete"] = False
    return {
        "success": True,
        "input_context": input_context,
        "output_artifact": output_artifact,
        "is_complete": bool(output_artifact.get("is_complete", False)),
        "error": None,
        "failure_artifact_id": None,
        "failure_stage": None,
        "failure_summary": None,
        "raw_output_preview": None,
        "has_full_artifact": False,
    }


def _has_clarifying_questions(output_artifact: dict[str, Any]) -> bool:
    """Return whether output still contains blocking clarification questions."""
    questions = output_artifact.get("clarifying_questions")
    return isinstance(questions, list) and any(
        isinstance(question, str) and bool(question.strip()) for question in questions
    )
