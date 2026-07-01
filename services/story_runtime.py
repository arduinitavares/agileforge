"""Runtime helpers for invoking the story-generation agent from workflow state."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, TypedDict

from pydantic import ValidationError

from orchestrator_agent.agent_tools.user_story_writer_tool.agent import (
    create_user_story_patch_agent,
)
from orchestrator_agent.agent_tools.user_story_writer_tool.agent import (
    root_agent as story_agent,
)
from orchestrator_agent.agent_tools.user_story_writer_tool.schemes import (
    STORY_QUALITY_SCHEMA_VERSION,
    UserStoryPatchOutput,
    UserStoryWriterInput,
    UserStoryWriterOutput,
)
from services.interview_runtime import hydrate_story_runtime_from_legacy
from services.story_scope import (
    _record_matches_story_scope,
    _requirement_extension_metadata,
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
from utils.runtime_config import STORY_RUNNER_IDENTITY

logger: logging.Logger = logging.getLogger(name=__name__)


_GENERIC_CLARIFYING_QUESTIONS: set[str] = {
    "please clarify the requirements",
    "please clarify the requirement",
    "what should happen",
    "what is expected",
    "need more details",
    "clarify requirements",
    "clarify the requirements",
}
_GENERIC_CLARIFYING_QUESTION_PHRASES: tuple[str, ...] = (
    "clarify the requirements",
    "clarify the requirement",
    "clarify requirements",
    "what should happen",
    "what is expected",
    "more details",
)
_MIN_ACTIONABLE_QUESTION_WORDS: int = 5
MAX_STORY_SCHEMA_REPAIR_ATTEMPTS: int = 2
MAX_STORY_SCHEMA_REPAIR_FEEDBACK_CHARS: int = 4000
MAX_STORIES_PER_ATTEMPT: int = 8
_USER_STORY_WRITER_OUTPUT_KEYS = frozenset(
    {
        "parent_requirement",
        "user_stories",
        "is_complete",
        "clarifying_questions",
    }
)
_USER_STORY_PATCH_OUTPUT_KEYS = frozenset(
    {
        "parent_requirement",
        "target_refinement_slot",
        "story",
        "is_complete",
        "clarifying_questions",
    }
)
_INVEST_SCORES: tuple[str, ...] = ("High", "Medium", "Low")
_REQUESTED_STORY_COUNT_PATTERN = re.compile(
    r"(?:~|about|around|approximately|approx\.?)?\s*"
    r"(\d{1,3})\s+(?:smaller\s+)?(?:sub-?stories|stories|story)",
    flags=re.IGNORECASE,
)


class StoryInputContext(TypedDict):
    """Serialized request payload expected by the story-generation agent."""

    parent_requirement: str
    requirement_context: str
    technical_spec: str
    compiled_authority: str
    global_roadmap_context: str
    already_generated_milestone_stories: str
    artifact_registry: dict[str, str]


type ValidationErrors = list[dict[str, object]]


@dataclass(frozen=True)
class _FailureDetails:
    """Structured details describing a story-runtime failure."""

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


def _normalize_validation_errors(errors: object) -> ValidationErrors:
    normalized: ValidationErrors = []
    if not isinstance(errors, list):
        return normalized

    for error in errors:
        if not isinstance(error, dict):
            continue
        normalized.append({str(key): value for key, value in error.items()})
    return normalized


def _story_input_context_from_model(
    payload: UserStoryWriterInput,
) -> StoryInputContext:
    """Return the serializable Story request context for a validated payload."""
    return {
        "parent_requirement": payload.parent_requirement,
        "requirement_context": payload.requirement_context,
        "technical_spec": payload.technical_spec,
        "compiled_authority": payload.compiled_authority,
        "global_roadmap_context": payload.global_roadmap_context,
        "already_generated_milestone_stories": (
            payload.already_generated_milestone_stories
        ),
        "artifact_registry": dict(payload.artifact_registry),
    }


def _schema_repair_feedback_text(
    *,
    error: str,
    validation_errors: object | None = None,
    schema_name: str = "UserStoryWriterOutput",
    required_fields: str = (
        "parent_requirement, user_stories, is_complete, and clarifying_questions"
    ),
) -> str:
    """Build bounded validation feedback for a Story schema-repair attempt."""
    details = ""
    if validation_errors is not None:
        details = json.dumps(validation_errors, sort_keys=True, default=str)
    feedback = (
        "SYSTEM_FEEDBACK: Your previous User Story response failed validation.\n"
        f"ERROR: {error}\n"
        f"VALIDATION_ERRORS: {details}\n"
        f"Return JSON only. Match the {schema_name} schema exactly. "
        f"Required top-level fields are {required_fields}. "
        "Do not add wrapper fields."
    )
    return feedback[:MAX_STORY_SCHEMA_REPAIR_FEEDBACK_CHARS]


def _payload_with_schema_repair_feedback(
    payload: UserStoryWriterInput,
    *,
    error: str,
    validation_errors: object | None = None,
    schema_name: str = "UserStoryWriterOutput",
    required_fields: str = (
        "parent_requirement, user_stories, is_complete, and clarifying_questions"
    ),
) -> UserStoryWriterInput:
    """Return a Story payload with validation feedback appended to context."""
    feedback = _schema_repair_feedback_text(
        error=error,
        validation_errors=validation_errors,
        schema_name=schema_name,
        required_fields=required_fields,
    )
    requirement_context = f"{payload.requirement_context}\n\n{feedback}"
    return payload.model_copy(update={"requirement_context": requirement_context})


def _has_clarifying_questions(
    output: UserStoryWriterOutput | UserStoryPatchOutput,
) -> bool:
    return any(question.strip() for question in output.clarifying_questions)


def _normalized_question_text(question: str) -> str:
    return " ".join(question.strip().rstrip(".?").lower().split())


def _actionable_clarifying_questions(questions: list[str]) -> list[str]:
    actionable: list[str] = []
    for question in questions:
        if not isinstance(question, str):
            continue
        stripped = question.strip()
        if not stripped:
            continue
        normalized = _normalized_question_text(stripped)
        if normalized in _GENERIC_CLARIFYING_QUESTIONS:
            continue
        if any(
            phrase in normalized
            for phrase in _GENERIC_CLARIFYING_QUESTION_PHRASES
        ):
            continue
        if len(stripped.split()) < _MIN_ACTIONABLE_QUESTION_WORDS:
            continue
        actionable.append(stripped)
    return actionable


def _story_count(output: UserStoryWriterOutput) -> int:
    return len(output.user_stories)


def _zero_invest_score_counts() -> dict[str, int]:
    return dict.fromkeys(_INVEST_SCORES, 0)


def _invest_score_counts(output: UserStoryWriterOutput) -> dict[str, int]:
    counts = _zero_invest_score_counts()
    for story in output.user_stories:
        counts[story.invest_score] = counts.get(story.invest_score, 0) + 1
    return counts


def _request_text_for_quality(request_payload: StoryInputContext) -> str:
    return request_payload.get("requirement_context", "")


def _requested_story_count(request_payload: StoryInputContext) -> int | None:
    text = _request_text_for_quality(request_payload)
    requested_counts: list[int] = []
    for match in _REQUESTED_STORY_COUNT_PATTERN.finditer(text):
        try:
            requested_counts.append(int(match.group(1)))
        except ValueError:
            continue
    return max(requested_counts) if requested_counts else None


def _finding(
    *,
    code: str,
    severity: str,
    message: str,
    affected_story_indexes: list[int] | None = None,
    affected_story_titles: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "affected_story_indexes": affected_story_indexes or [],
        "affected_story_titles": affected_story_titles or [],
    }


def _finding_codes(findings: list[dict[str, Any]]) -> set[str]:
    codes: set[str] = set()
    for finding in findings:
        code = finding.get("code")
        if isinstance(code, str):
            codes.add(code)
    return codes


def _all_story_indexes(output: UserStoryWriterOutput) -> list[int]:
    return list(range(1, len(output.user_stories) + 1))


def _all_story_titles(output: UserStoryWriterOutput) -> list[str]:
    return [story.story_title for story in output.user_stories]


def _evaluate_story_quality(
    output: UserStoryWriterOutput,
    *,
    request_payload: StoryInputContext,
    has_questions: bool,
) -> dict[str, Any]:
    """Return deterministic Story quality metadata for one generated draft."""
    story_count = _story_count(output)
    invest_score_counts = _invest_score_counts(output)
    requested_count = _requested_story_count(request_payload)
    remaining_scope = [
        item.strip() for item in output.remaining_scope if isinstance(item, str)
    ]
    quality_findings = [
        finding.model_dump(exclude_none=True) for finding in output.quality_findings
    ]
    existing_codes = _finding_codes(quality_findings)

    if (
        output.is_complete
        and story_count > 0
        and invest_score_counts.get("Low", 0) == story_count
        and "ALL_STORIES_LOW_INVEST" not in existing_codes
    ):
        quality_findings.append(
            _finding(
                code="ALL_STORIES_LOW_INVEST",
                severity="blocking",
                message="Every generated story has invest_score Low.",
                affected_story_indexes=_all_story_indexes(output),
                affected_story_titles=_all_story_titles(output),
            )
        )
        existing_codes.add("ALL_STORIES_LOW_INVEST")

    if (
        output.is_complete
        and output.coverage_status == "complete"
        and requested_count is not None
        and requested_count > MAX_STORIES_PER_ATTEMPT
        and story_count >= MAX_STORIES_PER_ATTEMPT
        and "REQUESTED_STORY_COUNT_EXCEEDS_CAP" not in existing_codes
    ):
        quality_findings.append(
            _finding(
                code="REQUESTED_STORY_COUNT_EXCEEDS_CAP",
                severity="blocking",
                message=(
                    f"Requested about {requested_count} stories, but one "
                    f"bounded attempt can contain at most {MAX_STORIES_PER_ATTEMPT}."
                ),
                affected_story_indexes=_all_story_indexes(output),
                affected_story_titles=_all_story_titles(output),
            )
        )
        existing_codes.add("REQUESTED_STORY_COUNT_EXCEEDS_CAP")

    if (
        output.coverage_status == "partial_capacity_limited"
        and "PARTIAL_CAPACITY_LIMITED" not in existing_codes
    ):
        quality_findings.append(
            _finding(
                code="PARTIAL_CAPACITY_LIMITED",
                severity="blocking",
                message=(
                    "The bounded Story generation attempt did not cover all "
                    "requested scope. Regenerate with a narrower slice or continue "
                    "with the listed remaining scope."
                ),
                affected_story_indexes=_all_story_indexes(output),
                affected_story_titles=_all_story_titles(output),
            )
        )
        existing_codes.add("PARTIAL_CAPACITY_LIMITED")

    if (
        output.coverage_status != "complete"
        and not remaining_scope
        and not has_questions
        and "REMAINING_SCOPE_REQUIRED" not in existing_codes
    ):
        quality_findings.append(
            _finding(
                code="REMAINING_SCOPE_REQUIRED",
                severity="blocking",
                message=(
                    "Incomplete coverage must list concrete remaining scope or "
                    "ask an actionable clarifying question."
                ),
            )
        )

    blocking_findings = [
        finding
        for finding in quality_findings
        if finding.get("severity") == "blocking"
    ]
    saveable = (
        output.is_complete
        and not has_questions
        and output.coverage_status == "complete"
        and not blocking_findings
        and not (
            story_count > 0 and invest_score_counts.get("Low", 0) == story_count
        )
    )
    return {
        "schema_version": STORY_QUALITY_SCHEMA_VERSION,
        "coverage_status": output.coverage_status,
        "remaining_scope": remaining_scope,
        "story_count": story_count,
        "invest_score_counts": invest_score_counts,
        "requested_story_count": requested_count,
        "quality_findings": quality_findings,
        "blocking_findings": blocking_findings,
        "saveable": saveable,
    }


def _as_object_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}


def _build_requirement_context(
    roadmap_releases: list[object],
    *,
    parent_requirement: str,
) -> str:
    requirement_context = f"Requirement: {parent_requirement}"

    for release in roadmap_releases:
        release_map: dict[str, object] | None = _as_object_dict(release)
        if release_map is None:
            continue
        items: object = release_map.get("items")
        if not isinstance(items, list) or parent_requirement not in items:
            continue

        theme: object = release_map.get("theme", "No theme specified")
        reasoning: object = release_map.get("reasoning", "No reasoning specified")
        focus: object = release_map.get("focus_area", "No focus area specified")
        return (
            f"Part of Release: {release_map.get('release_name', 'Unknown')}\n"
            f"Theme: {theme}\n"
            f"Focus Area: {focus}\n"
            f"Strategic Reasoning: {reasoning}"
        )

    return requirement_context


def _build_global_roadmap_context(roadmap_releases: list[object]) -> str:
    lines: list[str] = [
        "Global Roadmap Constraints (Do not overlap with sibling requirements):"
    ]
    for index, release in enumerate(roadmap_releases, start=1):
        release_map: dict[str, object] | None = _as_object_dict(release)
        if release_map is None:
            continue
        lines.append(f"Milestone {index}: {release_map.get('release_name', 'Unnamed')}")
        items: object = release_map.get("items")
        if not isinstance(items, list):
            continue
        lines.extend(f"  - {item}" for item in items)
    return "\n".join(lines)


def _story_summary_line(
    story: dict[str, object],
    *,
    dependency_ref: str | None = None,
) -> str:
    title: object = story.get("story_title", "Untitled")
    statement: object = story.get("statement", "")
    if dependency_ref:
        return (
            f"  - {title}: {statement} "
            f"[dependency_ref: {dependency_ref}; "
            "use this ref in dependency_candidates.prerequisite_ref]"
        )
    return f"  - {title}: {statement}"


def _build_already_generated_story_context(
    story_outputs: dict[str, object],
    *,
    parent_requirement: str,
) -> tuple[str, dict[str, str]]:
    artifact_registry: dict[str, str] = {}
    sections: list[str] = ["Already Generated Stories (Do not duplicate these):"]
    added_any_stories = False

    for req_name, artifact in story_outputs.items():
        artifact_map: dict[str, object] | None = _as_object_dict(artifact)
        if req_name == parent_requirement or artifact_map is None:
            continue
        stories = artifact_map.get("user_stories")
        if not isinstance(stories, list) or not stories:
            continue

        added_any_stories = True
        sections.extend(("", f"Requirement: '{req_name}' contains:"))
        for slot, story in enumerate(stories, start=1):
            story_map: dict[str, object] | None = _as_object_dict(story)
            if story_map is None:
                continue
            sections.append(
                _story_summary_line(
                    story_map,
                    dependency_ref=f"{req_name}#{slot}",
                )
            )
            produced_artifacts = story_map.get("produced_artifacts")
            if not isinstance(produced_artifacts, list):
                continue
            for produced_artifact in produced_artifacts:
                if isinstance(produced_artifact, str) and produced_artifact.strip():
                    artifact_registry[produced_artifact.strip()] = req_name

    if not added_any_stories:
        return "No stories generated yet for other requirements.", artifact_registry

    return "\n".join(sections).strip(), artifact_registry


def build_story_input_context(
    state: dict[str, Any],
    *,
    parent_requirement: str,
) -> StoryInputContext:
    """Build the prompt context used by the story-generation agent."""
    roadmap_releases = state.get("roadmap_releases") or []
    if not isinstance(roadmap_releases, list):
        roadmap_releases = []

    story_outputs = state.get("story_outputs") or {}
    if not isinstance(story_outputs, dict):
        story_outputs = {}

    already_generated, artifact_registry = _build_already_generated_story_context(
        story_outputs,
        parent_requirement=parent_requirement,
    )
    return {
        "parent_requirement": parent_requirement,
        "requirement_context": _build_requirement_context(
            roadmap_releases, parent_requirement=parent_requirement
        ),
        "technical_spec": _as_text(state.get("pending_spec_content")),
        "compiled_authority": _as_text(state.get("compiled_authority_cached")),
        "global_roadmap_context": _build_global_roadmap_context(roadmap_releases),
        "already_generated_milestone_stories": already_generated.strip(),
        "artifact_registry": artifact_registry,
    }


def _target_story_summary(
    state: dict[str, Any],
    *,
    parent_requirement: str,
    target_refinement_slot: int,
) -> str | None:
    story_outputs = state.get("story_outputs")
    if not isinstance(story_outputs, dict):
        return None
    artifact = story_outputs.get(parent_requirement)
    if not isinstance(artifact, dict):
        return None
    stories = artifact.get("user_stories")
    if not isinstance(stories, list):
        return None
    target_index = target_refinement_slot - 1
    if target_index < 0 or target_index >= len(stories):
        return None
    story = stories[target_index]
    if not isinstance(story, dict):
        return None
    return _story_summary_line({str(key): item for key, item in story.items()})


def _with_story_patch_target_context(
    request_payload: StoryInputContext,
    state: dict[str, Any],
    *,
    parent_requirement: str,
    target_story_id: int | None,
    target_refinement_slot: int,
) -> StoryInputContext:
    target_lines = [
        "--- TARGETED STORY PATCH ---",
        f"target_refinement_slot: {target_refinement_slot}",
    ]
    if target_story_id is not None:
        target_lines.append(f"target_story_id: {target_story_id}")
    target_summary = _target_story_summary(
        state,
        parent_requirement=parent_requirement,
        target_refinement_slot=target_refinement_slot,
    )
    if target_summary:
        target_lines.extend(["Existing target story:", target_summary])
    target_lines.extend(
        [
            "Return one UserStoryPatchOutput artifact.",
            "Do not return user_stories or sibling stories.",
        ]
    )
    return {
        **request_payload,
        "requirement_context": (
            request_payload["requirement_context"]
            + "\n\n"
            + "\n".join(target_lines)
        ),
    }


async def _invoke_story_agent(payload: UserStoryWriterInput) -> str:
    return await invoke_agent_to_text(
        agent=story_agent,
        runner_identity=STORY_RUNNER_IDENTITY,
        payload_json=payload.model_dump_json(),
        no_text_error="Story agent returned no text response",
    )


async def _invoke_story_patch_agent(payload: UserStoryWriterInput) -> str:
    return await invoke_agent_to_text(
        agent=create_user_story_patch_agent(),
        runner_identity=STORY_RUNNER_IDENTITY,
        payload_json=payload.model_dump_json(),
        no_text_error="Story patch agent returned no text response",
    )


def _failure(
    *,
    project_id: int,
    parent_requirement: str,
    input_context: StoryInputContext,
    failure_stage: str,
    details: _FailureDetails,
) -> dict[str, Any]:
    """Build a normalized failed story-runtime response with artifact metadata."""
    message = details.message
    artifact_result: FailureArtifactResult = write_failure_artifact(
        phase="story",
        project_id=project_id,
        failure_stage=failure_stage,
        failure_summary=message,
        raw_output=details.raw_text,
        context={
            "parent_requirement": parent_requirement,
            "input_context": input_context,
        },
        model_info={
            **get_agent_model_info(story_agent),
            "app_name": STORY_RUNNER_IDENTITY.app_name,
            "user_id": STORY_RUNNER_IDENTITY.user_id,
        },
        validation_errors=details.validation_errors,
        exception=details.exception,
    )
    metadata: FailureMetadataDict = artifact_result["metadata"]

    if details.exception is not None:
        logger.exception(
            "Story generation failed [artifact_id=%s stage=%s]: %s",
            metadata["failure_artifact_id"],
            failure_stage,
            message,
        )
    else:
        logger.error(
            "Story generation failed [artifact_id=%s stage=%s]: %s",
            metadata["failure_artifact_id"],
            failure_stage,
            message,
        )

    artifact: dict[str, Any] = {
        "error": "STORY_GENERATION_FAILED",
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
        "failure_artifact_id": metadata["failure_artifact_id"],
        "failure_stage": metadata["failure_stage"],
        "failure_summary": metadata["failure_summary"],
        "raw_output_preview": metadata["raw_output_preview"],
        "has_full_artifact": metadata["has_full_artifact"],
    }


def _with_failure_metadata(
    result: dict[str, Any],
    *,
    classification: str,
    draft_kind: str | None,
    is_reusable: bool,
    request_payload: StoryInputContext,
) -> dict[str, Any]:
    result.update(
        {
            "classification": classification,
            "draft_kind": draft_kind,
            "is_reusable": is_reusable,
            "request_payload": request_payload,
        }
    )
    return result


def _validate_story_output_consistency(
    output: UserStoryWriterOutput,
    *,
    raw_text: str,
    project_id: int,
    parent_requirement: str,
    input_context: StoryInputContext,
) -> dict[str, Any] | None:
    has_questions = _has_clarifying_questions(output)
    will_be_incomplete = not output.is_complete or has_questions
    if not will_be_incomplete:
        return None

    actionable_questions = _actionable_clarifying_questions(
        output.clarifying_questions
    )
    if actionable_questions:
        return None
    if (
        output.coverage_status == "partial_capacity_limited"
        and any(item.strip() for item in output.remaining_scope)
    ):
        return None

    return _with_failure_metadata(
        _failure(
            project_id=project_id,
            parent_requirement=parent_requirement,
            input_context=input_context,
            failure_stage="output_validation",
            details=_FailureDetails(
                message=(
                    "Story output validation failed: incomplete drafts must include "
                    "at least one actionable clarifying question."
                ),
                raw_text=raw_text,
            ),
        ),
        classification="nonreusable_schema_failure",
        draft_kind=None,
        is_reusable=False,
        request_payload=input_context,
    )


def _story_success_result(
    output: UserStoryWriterOutput,
    *,
    raw_text: str,
    project_id: int,
    parent_requirement: str,
    request_payload: StoryInputContext,
) -> dict[str, Any]:
    consistency_failure = _validate_story_output_consistency(
        output,
        raw_text=raw_text,
        project_id=project_id,
        parent_requirement=parent_requirement,
        input_context=request_payload,
    )
    if consistency_failure is not None:
        return consistency_failure

    output_artifact: dict[str, Any] = output.model_dump(exclude_none=True)
    has_questions = _has_clarifying_questions(output)
    quality = _evaluate_story_quality(
        output,
        request_payload=request_payload,
        has_questions=has_questions,
    )
    output_artifact["remaining_scope"] = quality["remaining_scope"]
    output_artifact["quality_findings"] = quality["quality_findings"]
    output_artifact["quality"] = quality
    effective_is_complete: bool = (
        output.is_complete
        and not has_questions
        and quality["coverage_status"] == "complete"
        and not quality["blocking_findings"]
    )
    output_artifact["is_complete"] = effective_is_complete
    quality_blocked = bool(quality["blocking_findings"])
    return {
        "success": True,
        "input_context": request_payload,
        "output_artifact": output_artifact,
        "classification": (
            "quality_gate_failed" if quality_blocked else "reusable_content_result"
        ),
        "draft_kind": (
            "quality_blocked_draft"
            if quality_blocked
            else "complete_draft"
            if effective_is_complete
            else "incomplete_draft"
        ),
        "is_reusable": not quality_blocked,
        "is_complete": effective_is_complete,
        "quality": quality,
        "request_payload": request_payload,
        "error": None,
        "failure_artifact_id": None,
        "failure_stage": None,
        "failure_summary": None,
        "raw_output_preview": None,
        "has_full_artifact": False,
    }


def _story_patch_quality_summary(
    output: UserStoryPatchOutput,
    *,
    has_questions: bool,
) -> dict[str, Any]:
    quality_findings = [
        finding.model_dump(exclude_none=True) for finding in output.quality_findings
    ]
    blocking_findings = [
        finding for finding in quality_findings if finding.get("severity") == "blocking"
    ]
    invest_score_counts = dict.fromkeys(_INVEST_SCORES, 0)
    invest_score_counts[output.story.invest_score] = (
        invest_score_counts.get(output.story.invest_score, 0) + 1
    )
    all_low = output.story.invest_score == "Low"
    saveable = (
        output.is_complete
        and not has_questions
        and output.coverage_status == "complete"
        and not blocking_findings
        and not all_low
    )
    return {
        "schema_version": STORY_QUALITY_SCHEMA_VERSION,
        "coverage_status": output.coverage_status,
        "remaining_scope": [
            item.strip() for item in output.remaining_scope if isinstance(item, str)
        ],
        "story_count": 1,
        "invest_score_counts": invest_score_counts,
        "requested_story_count": None,
        "quality_findings": quality_findings,
        "blocking_findings": blocking_findings,
        "saveable": saveable,
    }


def _story_patch_success_result(  # noqa: PLR0913
    output: UserStoryPatchOutput,
    *,
    raw_text: str,
    project_id: int,
    parent_requirement: str,
    request_payload: StoryInputContext,
    target_story_id: int | None,
    target_refinement_slot: int,
) -> dict[str, Any]:
    if output.target_refinement_slot != target_refinement_slot:
        return _with_failure_metadata(
            _failure(
                project_id=project_id,
                parent_requirement=parent_requirement,
                input_context=request_payload,
                failure_stage="output_validation",
                details=_FailureDetails(
                    message=(
                        "Story patch output validation failed: target_refinement_slot "
                        "does not match the requested target."
                    ),
                    raw_text=raw_text,
                ),
            ),
            classification="nonreusable_schema_failure",
            draft_kind=None,
            is_reusable=False,
            request_payload=request_payload,
        )

    output_artifact = output.model_dump(exclude_none=True)
    output_artifact["artifact_kind"] = "story_patch"
    output_artifact["target_refinement_slot"] = target_refinement_slot
    if target_story_id is not None:
        output_artifact["target_story_id"] = target_story_id
    has_questions = _has_clarifying_questions(output)
    quality = _story_patch_quality_summary(output, has_questions=has_questions)
    output_artifact["remaining_scope"] = quality["remaining_scope"]
    output_artifact["quality_findings"] = quality["quality_findings"]
    output_artifact["quality"] = quality
    effective_is_complete: bool = (
        output.is_complete
        and not has_questions
        and quality["coverage_status"] == "complete"
        and not quality["blocking_findings"]
    )
    output_artifact["is_complete"] = effective_is_complete
    quality_blocked = bool(quality["blocking_findings"])
    return {
        "success": True,
        "input_context": request_payload,
        "output_artifact": output_artifact,
        "classification": (
            "quality_gate_failed" if quality_blocked else "reusable_content_result"
        ),
        "draft_kind": "quality_blocked_draft" if quality_blocked else "story_patch",
        "is_reusable": not quality_blocked,
        "is_complete": effective_is_complete,
        "quality": quality,
        "request_payload": request_payload,
        "error": None,
        "failure_artifact_id": None,
        "failure_stage": None,
        "failure_summary": None,
        "raw_output_preview": None,
        "has_full_artifact": False,
    }


def _get_latest_reusable_story_artifact(
    state: dict[str, Any],
    *,
    parent_requirement: str,
    extension_metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    runtime = hydrate_story_runtime_from_legacy(
        state,
        parent_requirement=parent_requirement,
    )
    draft_projection = runtime.get("draft_projection") or {}
    if not _record_matches_story_scope(
        draft_projection if isinstance(draft_projection, dict) else None,
        extension_metadata,
    ):
        return None
    attempt_id = draft_projection.get("latest_reusable_attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        return None

    for attempt in reversed(runtime.get("attempt_history") or []):
        if not isinstance(attempt, dict):
            continue
        if attempt.get("attempt_id") != attempt_id:
            continue
        if not _record_matches_story_scope(attempt, extension_metadata):
            continue
        artifact = attempt.get("output_artifact")
        return artifact if isinstance(artifact, dict) else None
    return None


def _collect_unabsorbed_feedback_text(
    runtime: dict[str, Any],
    *,
    extension_metadata: dict[str, Any] | None,
) -> list[str]:
    feedback_projection = runtime.get("feedback_projection") or {}
    if not isinstance(feedback_projection, dict):
        return []

    items = feedback_projection.get("items") or []
    if not isinstance(items, list):
        return []

    feedback_text: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("status") != "unabsorbed":
            continue
        if not _record_matches_story_scope(item, extension_metadata):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            feedback_text.append(text)
    return feedback_text


def build_story_request_payload(
    state: dict[str, Any],
    *,
    parent_requirement: str,
    current_user_input: str | None = None,
) -> StoryInputContext:
    """Build a deterministic story-agent request payload from runtime state."""
    input_context: StoryInputContext = build_story_input_context(
        state, parent_requirement=parent_requirement
    )
    runtime = hydrate_story_runtime_from_legacy(
        state,
        parent_requirement=parent_requirement,
    )
    extension_metadata = _requirement_extension_metadata(
        state,
        parent_requirement=parent_requirement,
    )

    reusable_artifact = _get_latest_reusable_story_artifact(
        state,
        parent_requirement=parent_requirement,
        extension_metadata=extension_metadata,
    )
    if reusable_artifact:
        try:
            reusable_artifact_json = json.dumps(reusable_artifact, indent=2)
        except (TypeError, ValueError):
            logger.warning(
                (
                    "Skipping reusable story draft injection due to "
                    "unserializable artifact"
                ),
                extra={"parent_requirement": parent_requirement},
            )
        else:
            input_context["requirement_context"] += (
                f"\n\n--- PREVIOUS DRAFT TO REFINE ---\n{reusable_artifact_json}"
            )

    feedback_items = _collect_unabsorbed_feedback_text(
        runtime,
        extension_metadata=extension_metadata,
    )
    if isinstance(current_user_input, str) and current_user_input.strip():
        feedback_items.append(current_user_input)
    if feedback_items:
        input_context["requirement_context"] += (
            "\n\n--- USER REFINEMENT FEEDBACK ---\n" + "\n".join(feedback_items)
        )

    return input_context


async def run_story_agent_request(  # noqa: PLR0911
    request_payload: StoryInputContext,
    *,
    project_id: int,
    parent_requirement: str,
) -> dict[str, Any]:
    """Run the story agent for a prepared request payload and normalize failures."""
    try:
        payload = UserStoryWriterInput.model_validate(request_payload)
    except ValidationError as exc:
        return _with_failure_metadata(
            _failure(
                project_id=project_id,
                parent_requirement=parent_requirement,
                input_context=request_payload,
                failure_stage="input_validation",
                details=_FailureDetails(
                    message=f"Story input validation failed: {exc}",
                    validation_errors=_normalize_validation_errors(exc.errors()),
                    exception=exc,
                ),
            ),
            classification="nonreusable_schema_failure",
            draft_kind=None,
            is_reusable=False,
            request_payload=request_payload,
        )

    attempt_payload = payload
    for attempt_index in range(1, MAX_STORY_SCHEMA_REPAIR_ATTEMPTS + 1):
        attempt_request_payload = _story_input_context_from_model(attempt_payload)
        try:
            raw_text = await _invoke_story_agent(attempt_payload)
        except AgentInvocationError as exc:
            validation_errors = exc.validation_errors
            if (
                validation_errors
                and attempt_index < MAX_STORY_SCHEMA_REPAIR_ATTEMPTS
            ):
                attempt_payload = _payload_with_schema_repair_feedback(
                    attempt_payload,
                    error=str(exc),
                    validation_errors=validation_errors,
                )
                continue
            return _with_failure_metadata(
                _failure(
                    project_id=project_id,
                    parent_requirement=parent_requirement,
                    input_context=attempt_request_payload,
                    failure_stage="output_validation"
                    if validation_errors
                    else "invocation_exception",
                    details=_FailureDetails(
                        message=(
                            f"Story output validation failed: {exc}"
                            if validation_errors
                            else f"Story runtime failed: {exc}"
                        ),
                        raw_text=exc.partial_output,
                        validation_errors=validation_errors,
                        exception=exc,
                    ),
                ),
                classification="nonreusable_schema_failure"
                if validation_errors
                else "nonreusable_provider_failure",
                draft_kind=None,
                is_reusable=False,
                request_payload=attempt_request_payload,
            )
        except ValueError as exc:
            return _with_failure_metadata(
                _failure(
                    project_id=project_id,
                    parent_requirement=parent_requirement,
                    input_context=attempt_request_payload,
                    failure_stage="invocation_exception",
                    details=_FailureDetails(
                        message=f"Story runtime failed: {exc}",
                        exception=exc,
                    ),
                ),
                classification="nonreusable_provider_failure",
                draft_kind=None,
                is_reusable=False,
                request_payload=attempt_request_payload,
            )

        parsed = parse_json_payload(
            raw_text,
            required_keys=_USER_STORY_WRITER_OUTPUT_KEYS,
        )
        if parsed is None:
            error = "Story response is not valid JSON"
            if attempt_index < MAX_STORY_SCHEMA_REPAIR_ATTEMPTS:
                attempt_payload = _payload_with_schema_repair_feedback(
                    attempt_payload,
                    error=error,
                )
                continue
            return _with_failure_metadata(
                _failure(
                    project_id=project_id,
                    parent_requirement=parent_requirement,
                    input_context=attempt_request_payload,
                    failure_stage="invalid_json",
                    details=_FailureDetails(
                        message=error,
                        raw_text=raw_text,
                    ),
                ),
                classification="nonreusable_schema_failure",
                draft_kind=None,
                is_reusable=False,
                request_payload=attempt_request_payload,
            )

        try:
            output_model: UserStoryWriterOutput = UserStoryWriterOutput.model_validate(
                parsed
            )
        except ValidationError as exc:
            error = f"Story output validation failed: {exc}"
            validation_errors = _normalize_validation_errors(exc.errors())
            if attempt_index < MAX_STORY_SCHEMA_REPAIR_ATTEMPTS:
                attempt_payload = _payload_with_schema_repair_feedback(
                    attempt_payload,
                    error=error,
                    validation_errors=validation_errors,
                )
                continue
            return _with_failure_metadata(
                _failure(
                    project_id=project_id,
                    parent_requirement=parent_requirement,
                    input_context=attempt_request_payload,
                    failure_stage="output_validation",
                    details=_FailureDetails(
                        message=error,
                        raw_text=raw_text,
                        validation_errors=validation_errors,
                        exception=exc,
                    ),
                ),
                classification="nonreusable_schema_failure",
                draft_kind=None,
                is_reusable=False,
                request_payload=attempt_request_payload,
            )

        return _story_success_result(
            output_model,
            raw_text=raw_text,
            project_id=project_id,
            parent_requirement=parent_requirement,
            request_payload=attempt_request_payload,
        )

    msg = "Story runtime exhausted schema repair attempts."
    return _with_failure_metadata(
        _failure(
            project_id=project_id,
            parent_requirement=parent_requirement,
            input_context=request_payload,
            failure_stage="output_validation",
            details=_FailureDetails(message=msg),
        ),
        classification="nonreusable_schema_failure",
        draft_kind=None,
        is_reusable=False,
        request_payload=request_payload,
    )


async def run_story_patch_agent_request(  # noqa: PLR0911
    request_payload: StoryInputContext,
    *,
    project_id: int,
    parent_requirement: str,
    target_story_id: int | None,
    target_refinement_slot: int,
) -> dict[str, Any]:
    """Run the targeted story patch agent and normalize failures."""
    try:
        payload = UserStoryWriterInput.model_validate(request_payload)
    except ValidationError as exc:
        return _with_failure_metadata(
            _failure(
                project_id=project_id,
                parent_requirement=parent_requirement,
                input_context=request_payload,
                failure_stage="input_validation",
                details=_FailureDetails(
                    message=f"Story patch input validation failed: {exc}",
                    validation_errors=_normalize_validation_errors(exc.errors()),
                    exception=exc,
                ),
            ),
            classification="nonreusable_schema_failure",
            draft_kind=None,
            is_reusable=False,
            request_payload=request_payload,
        )

    attempt_payload = payload
    for attempt_index in range(1, MAX_STORY_SCHEMA_REPAIR_ATTEMPTS + 1):
        attempt_request_payload = _story_input_context_from_model(attempt_payload)
        try:
            raw_text = await _invoke_story_patch_agent(attempt_payload)
        except AgentInvocationError as exc:
            validation_errors = exc.validation_errors
            if (
                validation_errors
                and attempt_index < MAX_STORY_SCHEMA_REPAIR_ATTEMPTS
            ):
                attempt_payload = _payload_with_schema_repair_feedback(
                    attempt_payload,
                    error=str(exc),
                    validation_errors=validation_errors,
                    schema_name="UserStoryPatchOutput",
                    required_fields=(
                        "artifact_kind, parent_requirement, target_refinement_slot, "
                        "story, is_complete, and clarifying_questions"
                    ),
                )
                continue
            return _with_failure_metadata(
                _failure(
                    project_id=project_id,
                    parent_requirement=parent_requirement,
                    input_context=attempt_request_payload,
                    failure_stage="output_validation"
                    if validation_errors
                    else "invocation_exception",
                    details=_FailureDetails(
                        message=(
                            f"Story patch output validation failed: {exc}"
                            if validation_errors
                            else f"Story patch runtime failed: {exc}"
                        ),
                        raw_text=exc.partial_output,
                        validation_errors=validation_errors,
                        exception=exc,
                    ),
                ),
                classification="nonreusable_schema_failure"
                if validation_errors
                else "nonreusable_provider_failure",
                draft_kind=None,
                is_reusable=False,
                request_payload=attempt_request_payload,
            )
        except ValueError as exc:
            return _with_failure_metadata(
                _failure(
                    project_id=project_id,
                    parent_requirement=parent_requirement,
                    input_context=attempt_request_payload,
                    failure_stage="invocation_exception",
                    details=_FailureDetails(
                        message=f"Story patch runtime failed: {exc}",
                        exception=exc,
                    ),
                ),
                classification="nonreusable_provider_failure",
                draft_kind=None,
                is_reusable=False,
                request_payload=attempt_request_payload,
            )

        parsed = parse_json_payload(
            raw_text,
            required_keys=_USER_STORY_PATCH_OUTPUT_KEYS,
        )
        if parsed is None:
            error = "Story patch response is not valid JSON"
            if attempt_index < MAX_STORY_SCHEMA_REPAIR_ATTEMPTS:
                attempt_payload = _payload_with_schema_repair_feedback(
                    attempt_payload,
                    error=error,
                    schema_name="UserStoryPatchOutput",
                    required_fields=(
                        "artifact_kind, parent_requirement, target_refinement_slot, "
                        "story, is_complete, and clarifying_questions"
                    ),
                )
                continue
            return _with_failure_metadata(
                _failure(
                    project_id=project_id,
                    parent_requirement=parent_requirement,
                    input_context=attempt_request_payload,
                    failure_stage="invalid_json",
                    details=_FailureDetails(
                        message=error,
                        raw_text=raw_text,
                    ),
                ),
                classification="nonreusable_schema_failure",
                draft_kind=None,
                is_reusable=False,
                request_payload=attempt_request_payload,
            )

        try:
            output_model = UserStoryPatchOutput.model_validate(parsed)
        except ValidationError as exc:
            error = f"Story patch output validation failed: {exc}"
            validation_errors = _normalize_validation_errors(exc.errors())
            if attempt_index < MAX_STORY_SCHEMA_REPAIR_ATTEMPTS:
                attempt_payload = _payload_with_schema_repair_feedback(
                    attempt_payload,
                    error=error,
                    validation_errors=validation_errors,
                    schema_name="UserStoryPatchOutput",
                    required_fields=(
                        "artifact_kind, parent_requirement, target_refinement_slot, "
                        "story, is_complete, and clarifying_questions"
                    ),
                )
                continue
            return _with_failure_metadata(
                _failure(
                    project_id=project_id,
                    parent_requirement=parent_requirement,
                    input_context=attempt_request_payload,
                    failure_stage="output_validation",
                    details=_FailureDetails(
                        message=error,
                        raw_text=raw_text,
                        validation_errors=validation_errors,
                        exception=exc,
                    ),
                ),
                classification="nonreusable_schema_failure",
                draft_kind=None,
                is_reusable=False,
                request_payload=attempt_request_payload,
            )

        return _story_patch_success_result(
            output_model,
            raw_text=raw_text,
            project_id=project_id,
            parent_requirement=parent_requirement,
            request_payload=attempt_request_payload,
            target_story_id=target_story_id,
            target_refinement_slot=target_refinement_slot,
        )

    msg = "Story patch runtime exhausted schema repair attempts."
    return _with_failure_metadata(
        _failure(
            project_id=project_id,
            parent_requirement=parent_requirement,
            input_context=request_payload,
            failure_stage="output_validation",
            details=_FailureDetails(message=msg),
        ),
        classification="nonreusable_schema_failure",
        draft_kind=None,
        is_reusable=False,
        request_payload=request_payload,
    )


async def run_story_agent_from_state(  # noqa: PLR0913
    state: dict[str, Any],
    *,
    project_id: int,
    parent_requirement: str,
    user_input: str | None,
    target_story_id: int | None = None,
    target_refinement_slot: int | None = None,
) -> dict[str, Any]:
    """Build a story request from state and execute it through the story agent."""
    request_payload: StoryInputContext = build_story_request_payload(
        state,
        parent_requirement=parent_requirement,
        current_user_input=user_input,
    )
    if target_story_id is not None or target_refinement_slot is not None:
        if target_refinement_slot is None:
            msg = "target_refinement_slot is required for targeted Story patch runtime"
            return _with_failure_metadata(
                _failure(
                    project_id=project_id,
                    parent_requirement=parent_requirement,
                    input_context=request_payload,
                    failure_stage="input_validation",
                    details=_FailureDetails(message=msg),
                ),
                classification="nonreusable_schema_failure",
                draft_kind=None,
                is_reusable=False,
                request_payload=request_payload,
            )
        patch_payload = _with_story_patch_target_context(
            request_payload,
            state,
            parent_requirement=parent_requirement,
            target_story_id=target_story_id,
            target_refinement_slot=target_refinement_slot,
        )
        return await run_story_patch_agent_request(
            patch_payload,
            project_id=project_id,
            parent_requirement=parent_requirement,
            target_story_id=target_story_id,
            target_refinement_slot=target_refinement_slot,
        )
    return await run_story_agent_request(
        request_payload,
        project_id=project_id,
        parent_requirement=parent_requirement,
    )
