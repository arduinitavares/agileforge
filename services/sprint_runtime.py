"""Runtime helpers for invoking the sprint-planning agent from product state."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict, Unpack

from pydantic import ValidationError

from orchestrator_agent.agent_tools.sprint_planner_tool.agent import (
    root_agent as sprint_agent,
)
from orchestrator_agent.agent_tools.sprint_planner_tool.schemes import (
    SprintPlannerInput,
    SprintPlannerOutput,
    validate_task_decomposition_quality,
    validate_task_invariant_bindings,
)
from services.sprint_input import prepare_sprint_input_context
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
from utils.runtime_config import SPRINT_RUNNER_IDENTITY

if TYPE_CHECKING:
    from collections.abc import Sequence

logger: logging.Logger = logging.getLogger(name=__name__)
PUBLIC_TASK_KIND_VALUES = (
    "analysis",
    "design",
    "implementation",
    "testing",
    "documentation",
    "refactor",
)
_DECOMP_OTHER_TASK_KIND_PATTERN = re.compile(
    r"task '(?P<description>[^']+)': 'task_kind' cannot be 'other'",
)
_TASK_KIND_LOC_MIN_DEPTH = 2

type SprintInputContext = dict[str, object]
type ValidationErrorItem = dict[str, object]
type ValidationErrors = list[ValidationErrorItem]


class _RequiredSprintRunOptions(TypedDict):
    team_velocity_assumption: str
    sprint_duration_days: int
    include_task_decomposition: bool


class _SprintRunOptions(_RequiredSprintRunOptions, total=False):
    max_story_points: int | None
    selected_story_ids: list[int] | None
    user_input: str | None


@dataclass(frozen=True)
class _FailureDetails:
    """Structured details describing a sprint-runtime failure."""

    message: str
    raw_text: str | None = None
    validation_errors: ValidationErrors | None = None
    public_validation_errors: list[str] | None = None
    exception: BaseException | None = None
    source_fingerprint: str | None = None


@dataclass(frozen=True)
class _PreparedSprintPayload:
    """Validated sprint input context and model payload ready for invocation."""

    input_context: SprintInputContext
    payload: SprintPlannerInput
    selection_policy: dict[str, Any]
    source_fingerprint: str | None


async def _invoke_sprint_agent(payload: SprintPlannerInput) -> str:
    return await invoke_agent_to_text(
        agent=sprint_agent,
        runner_identity=SPRINT_RUNNER_IDENTITY,
        payload_json=payload.model_dump_json(),
        no_text_error="Sprint agent returned no text response",
    )


def _allowed_task_kind_hint() -> str:
    return ", ".join(PUBLIC_TASK_KIND_VALUES)


def _as_object_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}


def _normalize_input_context(value: object) -> SprintInputContext:
    normalized = _as_object_dict(value)
    return normalized or {}


def _normalize_source_fingerprint(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _normalize_validation_errors(errors: object) -> ValidationErrors:
    normalized: ValidationErrors = []
    if not isinstance(errors, list):
        return normalized
    for error in errors:
        error_map = _as_object_dict(error)
        if error_map is not None:
            normalized.append(error_map)
    return normalized


def _lookup_path(payload: object, path: Sequence[object]) -> object | None:
    current = payload
    for segment in path:
        if isinstance(segment, int):
            if not isinstance(current, list) or segment >= len(current):
                return None
            current = current[segment]
            continue
        current_map = _as_object_dict(current)
        if not isinstance(segment, str) or current_map is None:
            return None
        current = current_map.get(segment)
    return current


def _task_description_for_loc(
    payload: SprintInputContext | None,
    loc: Sequence[object],
) -> str | None:
    if payload is None or len(loc) < _TASK_KIND_LOC_MIN_DEPTH or loc[-1] != "task_kind":
        return None
    parent = _lookup_path(payload, loc[:-1])
    parent_map = _as_object_dict(parent)
    if parent_map is None:
        return None
    description = parent_map.get("description")
    if not isinstance(description, str):
        return None
    trimmed = description.strip()
    return trimmed or None


def _task_kind_hint(
    invalid_value: object,
    *,
    task_description: str | None = None,
) -> str | None:
    if not isinstance(invalid_value, str):
        return None
    trimmed = invalid_value.strip()
    if not trimmed:
        return None
    prefix = (
        f"Task '{task_description}' uses unsupported task_kind '{trimmed}'."
        if task_description
        else f"Unsupported task_kind '{trimmed}'."
    )
    return f"{prefix} Use one of: {_allowed_task_kind_hint()}."


def _public_hint_from_structured_error(
    error: ValidationErrorItem,
    *,
    parsed_output: SprintInputContext | None = None,
) -> str | None:
    msg = error.get("msg")
    message_hint = msg.strip() if isinstance(msg, str) else None
    loc = error.get("loc")
    if isinstance(loc, (list, tuple)) and loc and loc[-1] == "task_kind":
        hint = _task_kind_hint(
            error.get("input"),
            task_description=_task_description_for_loc(parsed_output, loc),
        )
        if hint:
            return hint

        task_description = _task_description_for_loc(parsed_output, loc)
        if task_description:
            return (
                f"Task '{task_description}' has invalid task_kind. "
                f"Use one of: {_allowed_task_kind_hint()}."
            )
        return f"Task has invalid task_kind. Use one of: {_allowed_task_kind_hint()}."

    return message_hint or None


def _compact_public_validation_errors(
    validation_errors: Sequence[object] | None,
    *,
    parsed_output: SprintInputContext | None = None,
) -> list[str]:
    hints: list[str] = []
    seen: set[str] = set()
    for error in validation_errors or []:
        hint: str | None = None
        error_map = _as_object_dict(error)
        if error_map is not None:
            hint = _public_hint_from_structured_error(
                error_map,
                parsed_output=parsed_output,
            )
        elif isinstance(error, str):
            match = _DECOMP_OTHER_TASK_KIND_PATTERN.search(error)
            if match:
                hint = _task_kind_hint(
                    "other",
                    task_description=match.group("description"),
                )
            else:
                trimmed = error.strip()
                hint = trimmed or None
        if hint and hint not in seen:
            seen.add(hint)
            hints.append(hint)
    return hints


def _expected_locked_story_ids(prepared: _PreparedSprintPayload) -> list[int]:
    return [int(story.story_id) for story in prepared.payload.available_stories]


def _actual_selected_story_ids(output_model: SprintPlannerOutput) -> list[int]:
    return [int(story.story_id) for story in output_model.selected_stories]


def _locked_selection_validation_error(
    *,
    expected_story_ids: list[int],
    actual_story_ids: list[int],
) -> ValidationErrorItem:
    return {
        "msg": (
            "selected stories do not match locked Sprint selection: "
            f"expected {expected_story_ids}, actual {actual_story_ids}"
        ),
        "expected_story_ids": expected_story_ids,
        "actual_story_ids": actual_story_ids,
    }


def _locked_capacity_validation_errors(
    *,
    prepared: _PreparedSprintPayload,
    output_model: SprintPlannerOutput,
) -> ValidationErrors:
    capacity = output_model.capacity_analysis
    expected_selected_count = len(prepared.payload.available_stories)
    expected_story_points_used = sum(
        story.story_points or 0 for story in prepared.payload.available_stories
    )
    expected_max_story_points = prepared.payload.max_story_points
    errors: ValidationErrors = []

    if capacity.selected_count != expected_selected_count:
        errors.append(
            {
                "msg": (
                    "capacity analysis does not match locked Sprint selection: "
                    f"selected_count expected {expected_selected_count}, "
                    f"actual {capacity.selected_count}"
                ),
                "field": "capacity_analysis.selected_count",
                "expected": expected_selected_count,
                "actual": capacity.selected_count,
            }
        )
    if capacity.story_points_used != expected_story_points_used:
        errors.append(
            {
                "msg": (
                    "capacity analysis does not match locked Sprint selection: "
                    f"story_points_used expected {expected_story_points_used}, "
                    f"actual {capacity.story_points_used}"
                ),
                "field": "capacity_analysis.story_points_used",
                "expected": expected_story_points_used,
                "actual": capacity.story_points_used,
            }
        )
    if capacity.max_story_points != expected_max_story_points:
        errors.append(
            {
                "msg": (
                    "capacity analysis does not match locked Sprint selection: "
                    f"max_story_points expected {expected_max_story_points}, "
                    f"actual {capacity.max_story_points}"
                ),
                "field": "capacity_analysis.max_story_points",
                "expected": expected_max_story_points,
                "actual": capacity.max_story_points,
            }
        )
    return errors


def _locked_deselected_stories_validation_errors(
    output_model: SprintPlannerOutput,
) -> ValidationErrors:
    if not output_model.deselected_stories:
        return []
    return [
        {
            "msg": (
                "deselected stories are not allowed for locked Sprint selection: "
                f"actual count {len(output_model.deselected_stories)}"
            ),
            "field": "deselected_stories",
            "expected": [],
            "actual_count": len(output_model.deselected_stories),
        }
    ]


def _failure(
    *,
    project_id: int,
    input_context: SprintInputContext,
    failure_stage: str,
    details: _FailureDetails,
    selection_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message = details.message
    source_fingerprint = details.source_fingerprint
    artifact_context: dict[str, Any] = {
        "input_context": input_context,
    }
    if selection_policy is not None:
        artifact_context["selection_policy"] = selection_policy
    if source_fingerprint is not None:
        artifact_context["source_fingerprint"] = source_fingerprint

    artifact_result: FailureArtifactResult = write_failure_artifact(
        phase="sprint",
        project_id=project_id,
        failure_stage=failure_stage,
        failure_summary=message,
        raw_output=details.raw_text,
        context=artifact_context,
        model_info={
            **get_agent_model_info(sprint_agent),
            "app_name": SPRINT_RUNNER_IDENTITY.app_name,
            "user_id": SPRINT_RUNNER_IDENTITY.user_id,
        },
        validation_errors=details.validation_errors,
        exception=details.exception,
    )
    metadata: FailureMetadataDict = artifact_result["metadata"]

    if details.exception is not None:
        logger.exception(
            "Sprint generation failed [artifact_id=%s stage=%s]: %s",
            metadata["failure_artifact_id"],
            failure_stage,
            message,
        )
    else:
        logger.error(
            "Sprint generation failed [artifact_id=%s stage=%s]: %s",
            metadata["failure_artifact_id"],
            failure_stage,
            message,
        )

    artifact: dict[str, Any] = {
        "error": "SPRINT_GENERATION_FAILED",
        "message": message,
        "validation_errors": list(details.public_validation_errors or []),
        "is_complete": False,
        "source_fingerprint": source_fingerprint,
        "failure_artifact_id": metadata["failure_artifact_id"],
        "failure_stage": metadata["failure_stage"],
        "failure_summary": metadata["failure_summary"],
        "raw_output_preview": metadata["raw_output_preview"],
        "has_full_artifact": metadata["has_full_artifact"],
    }

    return {
        "success": False,
        "input_context": input_context,
        "selection_policy": selection_policy,
        "source_fingerprint": source_fingerprint,
        "output_artifact": artifact,
        "is_complete": None,
        "error": message,
        "validation_errors": list(details.public_validation_errors or []),
        "failure_artifact_id": metadata["failure_artifact_id"],
        "failure_stage": metadata["failure_stage"],
        "failure_summary": metadata["failure_summary"],
        "raw_output_preview": metadata["raw_output_preview"],
        "has_full_artifact": metadata["has_full_artifact"],
    }


def _prepare_sprint_payload(
    *,
    project_id: int,
    options: _SprintRunOptions,
) -> _PreparedSprintPayload | dict[str, Any]:
    prepared = prepare_sprint_input_context(
        product_id=project_id,
        team_velocity_assumption=options["team_velocity_assumption"],
        sprint_duration_days=options["sprint_duration_days"],
        user_context=options.get("user_input"),
        max_story_points=options.get("max_story_points"),
        include_task_decomposition=options["include_task_decomposition"],
        selected_story_ids=options.get("selected_story_ids"),
    )
    input_context = _normalize_input_context(prepared.get("input_context"))
    source_fingerprint = _normalize_source_fingerprint(
        prepared.get("source_fingerprint")
    )

    if not prepared.get("success"):
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="input_validation",
            details=_FailureDetails(
                message=str(
                    prepared.get("message") or "Sprint input preparation failed."
                ),
                source_fingerprint=source_fingerprint,
            ),
        )

    selection_policy = prepared.get("selection_policy")
    if not isinstance(selection_policy, dict):
        selection_policy = {}

    try:
        payload = SprintPlannerInput.model_validate(input_context)
    except ValidationError as exc:
        public_validation_errors = _compact_public_validation_errors(exc.errors())
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="input_validation",
            details=_FailureDetails(
                message=f"Sprint input validation failed: {exc}",
                validation_errors=_normalize_validation_errors(exc.errors()),
                public_validation_errors=public_validation_errors,
                exception=exc,
                source_fingerprint=source_fingerprint,
            ),
            selection_policy=selection_policy,
        )
    return _PreparedSprintPayload(
        input_context=input_context,
        payload=payload,
        selection_policy=selection_policy,
        source_fingerprint=source_fingerprint,
    )


async def _invoke_prepared_sprint_payload(
    *,
    project_id: int,
    prepared: _PreparedSprintPayload,
) -> str | dict[str, Any]:
    input_context = prepared.input_context

    try:
        raw_text = await _invoke_sprint_agent(prepared.payload)
    except AgentInvocationError as exc:
        public_validation_errors = _compact_public_validation_errors(
            exc.validation_errors
        )
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="invocation_exception",
            details=_FailureDetails(
                message=f"Sprint runtime failed: {exc}",
                raw_text=exc.partial_output,
                validation_errors=_normalize_validation_errors(exc.validation_errors),
                public_validation_errors=public_validation_errors,
                exception=exc,
                source_fingerprint=prepared.source_fingerprint,
            ),
            selection_policy=prepared.selection_policy,
        )
    except ValueError as exc:
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="invocation_exception",
            details=_FailureDetails(
                message=f"Sprint runtime failed: {exc}",
                exception=exc,
                source_fingerprint=prepared.source_fingerprint,
            ),
            selection_policy=prepared.selection_policy,
        )
    return raw_text


def _locked_selection_output_failure(
    *,
    project_id: int,
    prepared: _PreparedSprintPayload,
    output_model: SprintPlannerOutput,
    raw_text: str,
) -> dict[str, Any] | None:
    input_context = prepared.input_context
    expected_story_ids = _expected_locked_story_ids(prepared)
    actual_story_ids = _actual_selected_story_ids(output_model)
    if actual_story_ids != expected_story_ids:
        structured_errors = [
            _locked_selection_validation_error(
                expected_story_ids=expected_story_ids,
                actual_story_ids=actual_story_ids,
            )
        ]
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="output_validation",
            details=_FailureDetails(
                message=(
                    "Sprint output validation failed: selected stories do not match "
                    "locked Sprint selection"
                ),
                raw_text=raw_text,
                validation_errors=_normalize_validation_errors(structured_errors),
                public_validation_errors=_compact_public_validation_errors(
                    structured_errors
                ),
                source_fingerprint=prepared.source_fingerprint,
            ),
            selection_policy=prepared.selection_policy,
        )

    capacity_errors = _locked_capacity_validation_errors(
        prepared=prepared,
        output_model=output_model,
    )
    if capacity_errors:
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="output_validation",
            details=_FailureDetails(
                message=(
                    "Sprint output validation failed: capacity analysis does not "
                    "match locked Sprint selection"
                ),
                raw_text=raw_text,
                validation_errors=_normalize_validation_errors(capacity_errors),
                public_validation_errors=_compact_public_validation_errors(
                    capacity_errors
                ),
                source_fingerprint=prepared.source_fingerprint,
            ),
            selection_policy=prepared.selection_policy,
        )

    deselected_errors = _locked_deselected_stories_validation_errors(output_model)
    if deselected_errors:
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="output_validation",
            details=_FailureDetails(
                message=(
                    "Sprint output validation failed: deselected stories are not "
                    "allowed for locked Sprint selection"
                ),
                raw_text=raw_text,
                validation_errors=_normalize_validation_errors(deselected_errors),
                public_validation_errors=_compact_public_validation_errors(
                    deselected_errors
                ),
                source_fingerprint=prepared.source_fingerprint,
            ),
            selection_policy=prepared.selection_policy,
        )
    return None


def _validate_sprint_output(
    *,
    project_id: int,
    prepared: _PreparedSprintPayload,
    raw_text: str,
    include_task_decomposition: bool,
) -> dict[str, Any]:
    input_context = prepared.input_context
    parsed = parse_json_payload(raw_text)
    if parsed is None:
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="invalid_json",
            details=_FailureDetails(
                message="Sprint response is not valid JSON",
                raw_text=raw_text,
                source_fingerprint=prepared.source_fingerprint,
            ),
            selection_policy=prepared.selection_policy,
        )

    try:
        output_model = SprintPlannerOutput.model_validate(parsed)
    except ValidationError as exc:
        public_validation_errors = _compact_public_validation_errors(
            exc.errors(),
            parsed_output=parsed,
        )
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="output_validation",
            details=_FailureDetails(
                message=f"Sprint output validation failed: {exc}",
                raw_text=raw_text,
                validation_errors=_normalize_validation_errors(exc.errors()),
                public_validation_errors=public_validation_errors,
                exception=exc,
                source_fingerprint=prepared.source_fingerprint,
            ),
            selection_policy=prepared.selection_policy,
        )

    locked_selection_failure = _locked_selection_output_failure(
        project_id=project_id,
        prepared=prepared,
        output_model=output_model,
        raw_text=raw_text,
    )
    if locked_selection_failure is not None:
        return locked_selection_failure

    has_acceptance_criteria_by_story = {
        story.story_id: bool(story.acceptance_criteria_items)
        for story in prepared.payload.available_stories
    }
    acceptance_criteria_items_by_story = {
        story.story_id: list(story.acceptance_criteria_items or [])
        for story in prepared.payload.available_stories
    }
    decomp_errors = validate_task_decomposition_quality(
        output_model,
        include_task_decomposition=include_task_decomposition,
        has_acceptance_criteria_by_story=has_acceptance_criteria_by_story,
        acceptance_criteria_items_by_story=acceptance_criteria_items_by_story,
    )
    if decomp_errors:
        structured_errors = [{"msg": error} for error in decomp_errors]
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="output_validation",
            details=_FailureDetails(
                message=(
                    "Sprint output validation failed: poor task decomposition quality"
                ),
                raw_text=raw_text,
                validation_errors=_normalize_validation_errors(structured_errors),
                public_validation_errors=_compact_public_validation_errors(
                    decomp_errors
                ),
                source_fingerprint=prepared.source_fingerprint,
            ),
            selection_policy=prepared.selection_policy,
        )

    allowed_invariant_ids_by_story = {
        int(story.story_id): list(story.evaluated_invariant_ids or [])
        for story in prepared.payload.available_stories
    }
    binding_errors = validate_task_invariant_bindings(
        output_model,
        allowed_invariant_ids_by_story=allowed_invariant_ids_by_story,
    )
    if binding_errors:
        structured_errors = [{"msg": error} for error in binding_errors]
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="output_validation",
            details=_FailureDetails(
                message=(
                    "Sprint output validation failed: invalid task invariant bindings"
                ),
                raw_text=raw_text,
                validation_errors=_normalize_validation_errors(structured_errors),
                public_validation_errors=_compact_public_validation_errors(
                    binding_errors
                ),
                source_fingerprint=prepared.source_fingerprint,
            ),
            selection_policy=prepared.selection_policy,
        )

    output_artifact = output_model.model_dump(exclude_none=True)
    output_artifact["is_complete"] = True
    return {
        "success": True,
        "input_context": input_context,
        "selection_policy": prepared.selection_policy,
        "source_fingerprint": prepared.source_fingerprint,
        "output_artifact": output_artifact,
        "is_complete": True,
        "error": None,
        "failure_artifact_id": None,
        "failure_stage": None,
        "failure_summary": None,
        "raw_output_preview": None,
        "has_full_artifact": False,
    }


async def run_sprint_agent_from_state(
    state: dict[str, Any],
    *,
    project_id: int,
    **options: Unpack[_SprintRunOptions],
) -> dict[str, Any]:
    """Run the sprint agent from prepared project state and normalize failures."""
    _ = state
    run_options: _SprintRunOptions = {
        "team_velocity_assumption": options["team_velocity_assumption"],
        "sprint_duration_days": options["sprint_duration_days"],
        "include_task_decomposition": options["include_task_decomposition"],
        "max_story_points": options.get("max_story_points"),
        "selected_story_ids": options.get("selected_story_ids"),
        "user_input": options.get("user_input"),
    }
    prepared: _PreparedSprintPayload | dict[str, Any] = _prepare_sprint_payload(
        project_id=project_id,
        options=run_options,
    )
    if not isinstance(prepared, _PreparedSprintPayload):
        return prepared

    raw_text: str | dict[str, Any] = await _invoke_prepared_sprint_payload(
        project_id=project_id,
        prepared=prepared,
    )
    if not isinstance(raw_text, str):
        return raw_text

    return _validate_sprint_output(
        project_id=project_id,
        prepared=prepared,
        raw_text=raw_text,
        include_task_decomposition=run_options["include_task_decomposition"],
    )
