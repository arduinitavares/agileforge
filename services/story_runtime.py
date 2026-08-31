"""Direct-Specification runtime for regular and targeted Story generation."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from adapters.adk.agents.story import create_user_story_patch_agent
from adapters.adk.agents.story import root_agent as story_agent
from services.contracts.specification_references import (
    AcceptedSpecificationReference,
)
from services.contracts.story import (
    StoryReferenceContentError,
    StorySentinelContentError,
    UserStoryWriterInput,
    UserStoryWriterOutput,
    canonicalize_story_items,
    safe_story_validation_errors,
    safe_story_validation_message,
    story_output_sentinel_fields,
    story_validation_error_sentinel_fields,
)
from services.story_schema_repair import (
    MAX_STORY_SCHEMA_REPAIR_ATTEMPTS,
    with_story_schema_repair_feedback,
)
from utils.adk_runner import (
    get_agent_model_info,
    invoke_agent_to_text,
    parse_json_payload,
)
from utils.agileforge_spec_profile_v2 import SpecificationPayload
from utils.failure_artifacts import (
    AgentInvocationError,
    FailureArtifactResult,
    FailureMetadataDict,
    write_failure_artifact,
)
from utils.runtime_config import STORY_RUNNER_IDENTITY

logger: logging.Logger = logging.getLogger(name=__name__)

_USER_STORY_WRITER_OUTPUT_KEYS: frozenset[str] = frozenset(
    {"user_stories", "is_complete"}
)
_USER_STORY_SENTINEL_SCAN_KEYS: frozenset[str] = frozenset({"user_stories"})
_STORY_INVOCATION_FAILURE_MESSAGE: str = (
    "Story runtime invocation failed before validated output was available."
)

type StoryInputContext = dict[str, object]
type ValidationErrors = list[dict[str, object]]


@dataclass(frozen=True)
class _FailureDetails:
    """Structured details describing one Story-runtime failure."""

    message: str
    raw_text: str | None = None
    validation_errors: ValidationErrors | None = None
    exception: BaseException | None = None
    extra: Mapping[str, object] | None = None


def _normalize_validation_errors(errors: object) -> ValidationErrors:
    normalized: ValidationErrors = []
    if not isinstance(errors, list):
        return normalized
    for error in errors:
        if isinstance(error, Mapping):
            normalized.append({str(key): value for key, value in error.items()})
    return normalized


def _sentinel_validation_message(fields: tuple[str, ...]) -> str:
    """Build one safe validation message from canonical field paths only."""
    return f"Story output validation failed: {StorySentinelContentError(fields)}"


def _reference_validation_errors(fields: tuple[str, ...]) -> ValidationErrors:
    """Build safe reference diagnostics with fixed paths and one bounded type."""
    errors: ValidationErrors = []
    for field in fields:
        errors.append({"path": field, "type": "specification_reference"})
    return errors


def _sentinel_fields_from_raw_text(raw_text: str | None) -> tuple[str, ...]:
    """Pre-scan a partial provider response without retaining its values."""
    if raw_text is None:
        return ()
    parsed = parse_json_payload(
        raw_text,
        required_keys=_USER_STORY_SENTINEL_SCAN_KEYS,
    )
    return story_output_sentinel_fields(parsed)


def _combined_user_input(
    persisted: object,
    current: str | None,
) -> str | None:
    prior = persisted if isinstance(persisted, str) and persisted.strip() else None
    fresh = current if isinstance(current, str) and current.strip() else None
    if prior is None:
        return fresh
    if fresh is None:
        return prior
    return f"{prior}\n\n{fresh}"


def build_story_input_context(
    state: dict[str, Any],
    *,
    current_user_input: str | None = None,
) -> StoryInputContext:
    """Project one deep-loaded Specification and exact immutable Story parents."""
    return {
        "accepted_specification_version_id": state.get(
            "accepted_specification_version_id"
        ),
        "accepted_specification_hash": state.get("accepted_specification_hash"),
        "accepted_specification_json": state.get("accepted_specification_json"),
        "parent_backlog_item_id": state.get("parent_backlog_item_id"),
        "parent_backlog_spec_item_ids": state.get("parent_backlog_spec_item_ids"),
        "roadmap_context": state.get("roadmap_context", ""),
        "user_input": _combined_user_input(state.get("user_input"), current_user_input),
    }


async def _invoke_story_agent(payload: UserStoryWriterInput) -> str:
    return await invoke_agent_to_text(
        agent=story_agent,
        runner_identity=STORY_RUNNER_IDENTITY,
        payload_json=payload.model_dump_json(exclude_none=True),
        no_text_error="Story agent returned no text response",
    )


async def _invoke_story_patch_agent(payload: UserStoryWriterInput) -> str:
    patch_agent = create_user_story_patch_agent()
    return await invoke_agent_to_text(
        agent=patch_agent,
        runner_identity=STORY_RUNNER_IDENTITY,
        payload_json=payload.model_dump_json(exclude_none=True),
        no_text_error="Story correction agent returned no text response",
    )


def _failure(
    *,
    project_id: int,
    input_context: StoryInputContext,
    failure_stage: str,
    details: _FailureDetails,
) -> dict[str, Any]:
    artifact_result: FailureArtifactResult = write_failure_artifact(
        phase="story",
        project_id=project_id,
        failure_stage=failure_stage,
        failure_summary=details.message,
        raw_output=details.raw_text,
        context={"input_context": input_context},
        model_info={
            **get_agent_model_info(story_agent),
            "app_name": STORY_RUNNER_IDENTITY.app_name,
            "user_id": STORY_RUNNER_IDENTITY.user_id,
        },
        validation_errors=details.validation_errors,
        exception=details.exception,
        extra=details.extra,
    )
    metadata: FailureMetadataDict = artifact_result["metadata"]
    logger.error(
        "Story generation failed [artifact_id=%s stage=%s]: %s",
        metadata["failure_artifact_id"],
        failure_stage,
        details.message,
    )
    return {
        "success": False,
        "input_context": input_context,
        "output_artifact": None,
        "is_complete": None,
        "error": details.message,
        **metadata,
    }


def _failure_result(
    result: dict[str, Any],
    *,
    request_payload: StoryInputContext,
) -> dict[str, Any]:
    return {
        **result,
        "classification": "nonreusable_schema_failure",
        "draft_kind": None,
        "is_reusable": False,
        "request_payload": request_payload,
    }


def _specification_reference(
    payload: UserStoryWriterInput,
) -> AcceptedSpecificationReference:
    return AcceptedSpecificationReference(
        spec_version_id=payload.accepted_specification_version_id,
        spec_hash=payload.accepted_specification_hash,
        canonical_specification_json=payload.accepted_specification_json,
        payload=SpecificationPayload.model_validate_json(
            payload.accepted_specification_json
        ),
    )


def _validate_and_canonicalize_output(
    parsed: dict[str, Any],
    *,
    payload: UserStoryWriterInput,
    targeted: bool,
) -> tuple[UserStoryWriterOutput, list[dict[str, Any]]]:
    sentinel_fields = story_output_sentinel_fields(parsed)
    if sentinel_fields:
        raise StorySentinelContentError(sentinel_fields)
    output = UserStoryWriterOutput.model_validate(parsed)
    if targeted and len(output.user_stories) != 1:
        message = "Targeted Story correction must return exactly one replacement item."
        raise ValueError(message)
    canonical_items = canonicalize_story_items(
        _specification_reference(payload),
        parent_backlog_spec_item_ids=payload.parent_backlog_spec_item_ids,
        agent_items=output.user_stories,
    )
    return output, [item.model_dump(mode="json") for item in canonical_items]


async def _run_story_request(  # noqa: C901, PLR0911, PLR0912, PLR0915
    request_payload: StoryInputContext,
    *,
    project_id: int,
    targeted: bool,
    target_story_id: int | None,
) -> dict[str, Any]:
    try:
        payload = UserStoryWriterInput.model_validate(request_payload)
    except ValidationError as exc:
        return _failure_result(
            _failure(
                project_id=project_id,
                input_context=request_payload,
                failure_stage="input_validation",
                details=_FailureDetails(
                    message=f"Story input validation failed: {exc}",
                    validation_errors=_normalize_validation_errors(exc.errors()),
                    exception=exc,
                ),
            ),
            request_payload=request_payload,
        )
    if targeted and (target_story_id is None or target_story_id <= 0):
        return _failure_result(
            _failure(
                project_id=project_id,
                input_context=request_payload,
                failure_stage="input_validation",
                details=_FailureDetails(
                    message=(
                        "Targeted Story correction requires a positive host story_id."
                    )
                ),
            ),
            request_payload=request_payload,
        )

    attempt_payload = payload
    for attempt_index in range(1, MAX_STORY_SCHEMA_REPAIR_ATTEMPTS + 1):
        attempt_context = attempt_payload.model_dump(mode="json")
        try:
            raw_text = await (
                _invoke_story_patch_agent(attempt_payload)
                if targeted
                else _invoke_story_agent(attempt_payload)
            )
        except AgentInvocationError as exc:
            validation_errors = exc.validation_errors
            sentinel_fields = _sentinel_fields_from_raw_text(exc.partial_output)
            if not sentinel_fields:
                sentinel_fields = story_validation_error_sentinel_fields(
                    validation_errors
                )
            if sentinel_fields:
                error = _sentinel_validation_message(sentinel_fields)
                safe_validation_errors = None
            elif validation_errors:
                error = safe_story_validation_message(validation_errors)
                safe_validation_errors = safe_story_validation_errors(
                    validation_errors
                )
            else:
                error = _STORY_INVOCATION_FAILURE_MESSAGE
                safe_validation_errors = None
            if (
                validation_errors or sentinel_fields
            ) and attempt_index < MAX_STORY_SCHEMA_REPAIR_ATTEMPTS:
                attempt_payload = with_story_schema_repair_feedback(
                    attempt_payload,
                    error=error,
                    validation_errors=safe_validation_errors,
                    targeted=targeted,
                )
                continue
            stage = (
                "output_validation"
                if validation_errors or sentinel_fields
                else "invocation_exception"
            )
            return _failure_result(
                _failure(
                    project_id=project_id,
                    input_context=attempt_context,
                    failure_stage=stage,
                    details=_FailureDetails(
                        message=error,
                        raw_text=None,
                        validation_errors=safe_validation_errors,
                        exception=None,
                        extra=(
                            {"invalid_fields": list(sentinel_fields)}
                            if sentinel_fields
                            else None
                        ),
                    ),
                ),
                request_payload=attempt_context,
            )
        except ValueError as exc:
            return _failure_result(
                _failure(
                    project_id=project_id,
                    input_context=attempt_context,
                    failure_stage="invocation_exception",
                    details=_FailureDetails(
                        message=f"Story runtime failed: {exc}", exception=exc
                    ),
                ),
                request_payload=attempt_context,
            )

        parsed = parse_json_payload(
            raw_text,
            required_keys=_USER_STORY_WRITER_OUTPUT_KEYS,
        )
        sentinel_fields: tuple[str, ...] = ()
        reference_fields: tuple[str, ...] = ()
        validation_errors: ValidationErrors | None
        if parsed is None:
            error = "Story response is not valid JSON"
            validation_errors = None
        else:
            try:
                output, canonical_items = _validate_and_canonicalize_output(
                    parsed,
                    payload=attempt_payload,
                    targeted=targeted,
                )
            except (ValidationError, ValueError) as exc:
                if isinstance(exc, StorySentinelContentError):
                    sentinel_fields = exc.fields
                elif isinstance(exc, StoryReferenceContentError):
                    reference_fields = exc.fields
                raw_validation_errors = (
                    _normalize_validation_errors(exc.errors())
                    if isinstance(exc, ValidationError)
                    else None
                )
                if not sentinel_fields:
                    sentinel_fields = story_validation_error_sentinel_fields(
                        raw_validation_errors
                    )
                if sentinel_fields:
                    error = _sentinel_validation_message(sentinel_fields)
                    validation_errors = None
                elif reference_fields:
                    error = f"Story output validation failed: {exc}"
                    validation_errors = _reference_validation_errors(reference_fields)
                elif raw_validation_errors is not None:
                    error = safe_story_validation_message(raw_validation_errors)
                    validation_errors = safe_story_validation_errors(
                        raw_validation_errors
                    )
                else:
                    error = f"Story output validation failed: {exc}"
                    validation_errors = raw_validation_errors
            else:
                output_artifact = output.model_dump(mode="json")
                has_questions = bool(output.clarifying_questions)
                return {
                    "success": True,
                    "input_context": attempt_context,
                    "request_payload": attempt_context,
                    "output_artifact": output_artifact,
                    "canonical_story_items": canonical_items,
                    "is_complete": output.is_complete and not has_questions,
                    "classification": "reusable_content_result",
                    "draft_kind": (
                        "story_correction" if targeted else "complete_draft"
                    ),
                    "is_reusable": True,
                    "target_story_id": target_story_id,
                    "error": None,
                    "failure_artifact_id": None,
                    "failure_stage": None,
                    "failure_summary": None,
                    "raw_output_preview": None,
                    "has_full_artifact": False,
                }

        if attempt_index < MAX_STORY_SCHEMA_REPAIR_ATTEMPTS:
            attempt_payload = with_story_schema_repair_feedback(
                attempt_payload,
                error=error,
                validation_errors=validation_errors,
                targeted=targeted,
            )
            continue
        return _failure_result(
            _failure(
                project_id=project_id,
                input_context=attempt_context,
                failure_stage=(
                    "invalid_json" if parsed is None else "output_validation"
                ),
                details=_FailureDetails(
                    message=error,
                    raw_text=None,
                    validation_errors=validation_errors,
                    extra=(
                        {
                            "invalid_fields": list(
                                sentinel_fields or reference_fields
                            )
                        }
                        if sentinel_fields or reference_fields
                        else None
                    ),
                ),
            ),
            request_payload=attempt_context,
        )

    message = "Story runtime exhausted schema repair attempts."
    return _failure_result(
        _failure(
            project_id=project_id,
            input_context=request_payload,
            failure_stage="output_validation",
            details=_FailureDetails(message=message),
        ),
        request_payload=request_payload,
    )


async def run_story_agent_request(
    request_payload: StoryInputContext,
    *,
    project_id: int,
) -> dict[str, Any]:
    """Run regular Story generation through the exact direct-root contract."""
    return await _run_story_request(
        request_payload,
        project_id=project_id,
        targeted=False,
        target_story_id=None,
    )


async def run_story_patch_agent_request(
    request_payload: StoryInputContext,
    *,
    project_id: int,
    target_story_id: int,
) -> dict[str, Any]:
    """Run one host-selected correction using one full writer output item."""
    return await _run_story_request(
        request_payload,
        project_id=project_id,
        targeted=True,
        target_story_id=target_story_id,
    )


async def run_story_agent_from_state(
    state: dict[str, Any],
    *,
    project_id: int,
    user_input: str | None,
    target_story_id: int | None = None,
) -> dict[str, Any]:
    """Build one direct-root request and execute regular or targeted generation."""
    request_payload = build_story_input_context(
        state,
        current_user_input=user_input,
    )
    if target_story_id is not None:
        return await run_story_patch_agent_request(
            request_payload,
            project_id=project_id,
            target_story_id=target_story_id,
        )
    return await run_story_agent_request(request_payload, project_id=project_id)


__all__ = [
    "MAX_STORY_SCHEMA_REPAIR_ATTEMPTS",
    "StoryInputContext",
    "build_story_input_context",
    "run_story_agent_from_state",
    "run_story_agent_request",
    "run_story_patch_agent_request",
]
