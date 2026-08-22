"""Runtime helpers for invoking the roadmap agent from workflow state."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from adapters.adk.agents.roadmap import (
    root_agent as roadmap_agent,
)
from services.contracts.roadmap import (
    RoadmapBuilderInput,
    RoadmapBuilderOutput,
    validate_roadmap_backlog_coverage,
)
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
from utils.runtime_config import ROADMAP_RUNNER_IDENTITY

logger: logging.Logger = logging.getLogger(name=__name__)

type RoadmapInputContext = dict[str, object]
type ValidationErrors = list[dict[str, object]]


@dataclass(frozen=True)
class _FailureDetails:
    """Structured details describing a roadmap-runtime failure."""

    message: str
    raw_text: str | None = None
    validation_errors: ValidationErrors | None = None
    exception: BaseException | None = None


def _normalize_prior_roadmap_state(value: object) -> str:
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


def _has_clarifying_questions(artifact: dict[str, Any]) -> bool:
    questions = artifact.get("clarifying_questions")
    return isinstance(questions, list) and any(
        isinstance(question, str) and bool(question.strip()) for question in questions
    )


def build_roadmap_input_context(
    state: dict[str, Any],
    *,
    user_input: str | None,
) -> RoadmapInputContext:
    """Project one already-deep-loaded Specification and its exact Backlog."""
    input_context: RoadmapInputContext = {
        "accepted_specification_version_id": state.get(
            "accepted_specification_version_id"
        ),
        "accepted_specification_hash": state.get("accepted_specification_hash"),
        "accepted_specification_json": state.get("accepted_specification_json"),
        "backlog_items": state.get("backlog_items"),
        "product_vision": state.get("product_vision"),
        "time_increment": state.get("time_increment", "Milestone-based"),
        "prior_roadmap_state": _normalize_prior_roadmap_state(
            state.get("prior_roadmap_state")
        ),
        "user_input": user_input or "",
    }
    return input_context


async def _invoke_roadmap_agent(payload: RoadmapBuilderInput) -> str:
    return await invoke_agent_to_text(
        agent=roadmap_agent,
        runner_identity=ROADMAP_RUNNER_IDENTITY,
        payload_json=payload.model_dump_json(exclude_none=True),
        no_text_error="Roadmap agent returned no text response",
    )


def _failure(
    *,
    project_id: int,
    input_context: RoadmapInputContext,
    failure_stage: str,
    details: _FailureDetails,
) -> dict[str, Any]:
    message: str = details.message
    artifact_result: FailureArtifactResult = write_failure_artifact(
        phase="roadmap",
        project_id=project_id,
        failure_stage=failure_stage,
        failure_summary=message,
        raw_output=details.raw_text,
        context={"input_context": input_context},
        model_info={
            **get_agent_model_info(roadmap_agent),
            "app_name": ROADMAP_RUNNER_IDENTITY.app_name,
            "user_id": ROADMAP_RUNNER_IDENTITY.user_id,
        },
        validation_errors=details.validation_errors,
        exception=details.exception,
    )
    metadata: FailureMetadataDict = artifact_result["metadata"]
    if details.exception is not None:
        logger.exception(
            "Roadmap generation failed [artifact_id=%s stage=%s]: %s",
            metadata["failure_artifact_id"],
            failure_stage,
            message,
        )
    else:
        logger.error(
            "Roadmap generation failed [artifact_id=%s stage=%s]: %s",
            metadata["failure_artifact_id"],
            failure_stage,
            message,
        )

    artifact: dict[str, Any] = {
        "error": "ROADMAP_GENERATION_FAILED",
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


async def run_roadmap_agent_from_state(
    state: dict[str, Any],
    *,
    project_id: int,
    user_input: str | None,
) -> dict[str, Any]:
    """Run the roadmap agent from stored workflow state and normalize failures."""
    input_context: RoadmapInputContext = build_roadmap_input_context(
        state,
        user_input=user_input,
    )

    try:
        payload: RoadmapBuilderInput = RoadmapBuilderInput.model_validate(input_context)
    except ValidationError as exc:
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="input_validation",
            details=_FailureDetails(
                message=f"Roadmap input validation failed: {exc}",
                validation_errors=_normalize_validation_errors(exc.errors()),
                exception=exc,
            ),
        )

    try:
        raw_text: str = await _invoke_roadmap_agent(payload)
    except AgentInvocationError as exc:
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="invocation_exception",
            details=_FailureDetails(
                message=f"Roadmap runtime failed: {exc}",
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
                message=f"Roadmap runtime failed: {exc}",
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
                message="Roadmap response is not valid JSON",
                raw_text=raw_text,
            ),
        )

    try:
        output_model: RoadmapBuilderOutput = RoadmapBuilderOutput.model_validate(parsed)
        validate_roadmap_backlog_coverage(
            output_model,
            (item.backlog_item_id for item in payload.backlog_items),
        )
    except (ValidationError, ValueError) as exc:
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="output_validation",
            details=_FailureDetails(
                message=f"Roadmap output validation failed: {exc}",
                raw_text=raw_text,
                validation_errors=(
                    _normalize_validation_errors(exc.errors())
                    if isinstance(exc, ValidationError)
                    else None
                ),
                exception=exc,
            ),
        )

    output_artifact: dict[str, Any] = output_model.model_dump(
        mode="json", exclude_none=True
    )
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
