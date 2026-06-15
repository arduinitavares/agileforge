"""Runtime helpers for invoking the roadmap agent from workflow state."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from orchestrator_agent.agent_tools.roadmap_builder.agent import (
    root_agent as roadmap_agent,
)
from orchestrator_agent.agent_tools.roadmap_builder.schemes import (
    BacklogItem as RoadmapBacklogItem,
)
from orchestrator_agent.agent_tools.roadmap_builder.schemes import (
    RoadmapBuilderInput,
    RoadmapBuilderOutput,
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

_ROADMAP_BACKLOG_ITEM_FIELDS: frozenset[str] = frozenset(
    RoadmapBacklogItem.model_fields
)


@dataclass(frozen=True)
class _FailureDetails:
    """Structured details describing a roadmap-runtime failure."""

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


def _normalize_prior_roadmap_state(value: object) -> str:
    if value is None:
        return "NO_HISTORY"
    if isinstance(value, str):
        text = value.strip()
        return text if text else "NO_HISTORY"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "NO_HISTORY"


def _json_safe_copy(value: object) -> object:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _saved_scope_extension_context(
    state: Mapping[str, Any],
) -> dict[str, object] | None:
    context = state.get("scope_extension_context")
    if not isinstance(context, Mapping):
        return None
    if not context.get("backlog_extension_saved_at"):
        return None
    if context.get("roadmap_extension_saved_at"):
        return None
    added_source_item_ids = _string_list(context.get("added_source_item_ids"))
    if not added_source_item_ids:
        return None

    projected: dict[str, object] = {}
    for key in (
        "schema",
        "base_spec_version_id",
        "base_spec_hash",
        "amended_spec_version_id",
        "amended_spec_hash",
        "backlog_extension_saved_at",
        "backlog_extension_attempt_id",
        "backlog_extension_artifact_fingerprint",
    ):
        if key in context:
            projected[key] = context[key]
    projected["added_source_item_ids"] = added_source_item_ids
    return projected


def _scope_extension_base_releases(state: Mapping[str, Any]) -> object:
    context = state.get("scope_extension_context")
    if isinstance(context, Mapping):
        base_releases = context.get("roadmap_extension_base_releases")
        if isinstance(base_releases, list):
            return _json_safe_copy(base_releases)

    releases = state.get("roadmap_releases")
    return _json_safe_copy(releases) if isinstance(releases, list) else []


def _is_extension_backlog_item(
    item: Mapping[str, Any],
    *,
    amended_spec_version_id: int | None,
    added_source_item_ids: list[str],
) -> bool:
    if item.get("story_origin") == "scope_extension":
        return True
    if (
        amended_spec_version_id is not None
        and _coerce_int(item.get("accepted_spec_version_id"))
        == amended_spec_version_id
    ):
        return True
    item_source_ids = set(_string_list(item.get("source_item_ids")))
    return bool(item_source_ids.intersection(added_source_item_ids))


def _extension_backlog_items(
    state: Mapping[str, Any],
    scope_extension: Mapping[str, object],
) -> list[dict[str, object]]:
    rows = _extension_backlog_item_rows(state, scope_extension)
    added_source_item_ids = _string_list(scope_extension.get("added_source_item_ids"))
    amended_spec_version_id = _coerce_int(
        scope_extension.get("amended_spec_version_id")
    )
    projected: list[dict[str, object]] = []
    for item_map in rows:
        requirement = item_map.get("requirement") or item_map.get("title")
        if not isinstance(requirement, str) or not requirement.strip():
            continue
        source_item_ids = _string_list(item_map.get("source_item_ids"))
        projected.append(
            {
                "requirement": requirement.strip(),
                "accepted_spec_version_id": amended_spec_version_id,
                "source_item_ids": source_item_ids or added_source_item_ids,
            }
        )
    return projected


def _extension_backlog_item_rows(
    state: Mapping[str, Any],
    scope_extension: Mapping[str, object],
) -> list[dict[str, object]]:
    backlog_items = state.get("backlog_items")
    if not isinstance(backlog_items, list):
        return []

    added_source_item_ids = _string_list(scope_extension.get("added_source_item_ids"))
    amended_spec_version_id = _coerce_int(
        scope_extension.get("amended_spec_version_id")
    )
    rows: list[dict[str, object]] = []
    for item in backlog_items:
        if not isinstance(item, Mapping):
            continue
        item_map = {str(key): value for key, value in item.items()}
        if not _is_extension_backlog_item(
            item_map,
            amended_spec_version_id=amended_spec_version_id,
            added_source_item_ids=added_source_item_ids,
        ):
            continue
        rows.append(item_map)
    return rows


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


def _project_roadmap_backlog_items(value: object) -> list[object]:
    """Return backlog items with host refinement metadata stripped."""
    if not isinstance(value, list):
        return []
    projected: list[object] = []
    for item in value:
        if isinstance(item, Mapping):
            projected.append(
                {
                    str(key): raw_value
                    for key, raw_value in item.items()
                    if isinstance(key, str) and key in _ROADMAP_BACKLOG_ITEM_FIELDS
                }
            )
        else:
            projected.append(item)
    return projected


def build_roadmap_input_context(
    state: dict[str, Any],
    *,
    user_input: str | None,
) -> RoadmapInputContext:
    """Build the serialized roadmap-agent input payload from workflow state."""
    vision_assessment = state.get("product_vision_assessment") or {}
    vision_stmt = vision_assessment.get("product_vision_statement") or ""

    # backlog_items comes from session state; strip refinement lineage metadata
    # before passing nested items into RoadmapBuilderInput(extra="forbid").
    scope_extension = _saved_scope_extension_context(state)
    extension_backlog_items = (
        _extension_backlog_items(state, scope_extension)
        if scope_extension is not None
        else []
    )
    raw_backlog_items: object = (
        _extension_backlog_item_rows(state, scope_extension)
        if scope_extension is not None
        else state.get("backlog_items")
    )
    backlog_items = _project_roadmap_backlog_items(raw_backlog_items)

    input_context: RoadmapInputContext = {
        "backlog_items": backlog_items,
        "product_vision": vision_stmt,
        "technical_spec": _as_text(state.get("pending_spec_content")),
        "compiled_authority": _as_text(state.get("compiled_authority_cached")),
        "time_increment": "Milestone-based",
        "prior_roadmap_state": _normalize_prior_roadmap_state(
            state.get("roadmap_releases")
        ),
        "user_input": user_input or "",
    }
    if scope_extension is not None:
        input_context.update(
            {
                "generation_mode": "scope_extension",
                "prior_roadmap_state": "NO_HISTORY",
                "existing_roadmap_context": _scope_extension_base_releases(state),
                "scope_extension": dict(scope_extension),
                "extension_backlog_items": extension_backlog_items,
            }
        )
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
    except ValidationError as exc:
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="output_validation",
            details=_FailureDetails(
                message=f"Roadmap output validation failed: {exc}",
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
