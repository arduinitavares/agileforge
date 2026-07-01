"""Story phase application service helpers."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any, cast

from orchestrator_agent.agent_tools.story_linkage import (
    normalize_requirement_key,
)
from orchestrator_agent.agent_tools.user_story_writer_tool.tools import (
    SaveStoriesInput,
    SaveStoryPatchInput,
)
from orchestrator_agent.fsm.states import OrchestratorState
from services.agent_workbench.fingerprints import canonical_hash
from services.interview_runtime import hydrate_story_runtime_from_legacy
from services.phases import workflow_state
from services.story_feedback_quality import evaluate_story_feedback_quality
from services.story_scope import (
    _coerce_int,
    _metadata_matches_extension_scope,
    _record_matches_story_scope,
    _release_extension_metadata,
    _requirement_extension_metadata,
    _scope_extension_context,
    _string_list,
)

VALID_FSM_STATES = {state.value for state in OrchestratorState}
_EFFORT_TO_STORY_POINTS: dict[str, int] = {
    "XS": 1,
    "S": 2,
    "M": 3,
    "L": 5,
    "XL": 8,
}
_INVEST_SCORES: tuple[str, ...] = ("High", "Medium", "Low")
_STORY_QUALITY_SCHEMA_VERSION = "agileforge.story_quality.v1"
_STORY_COMPLETION_SCOPE_SCHEMA_VERSION = "agileforge.story_completion_scope.v1"
_STORY_COMPLETION_SCOPE_REPAIR_IDEMPOTENCY_KEY = (
    "story_completion_scope_repair_idempotency"
)
REQUIREMENT_RECONCILIATION_SCHEMA_VERSION = (
    "agileforge.requirement_reconciliation.v1"
)
REQUIREMENT_RECONCILIATION_STATE_KEY = "requirement_reconciliations"
REQUIREMENT_RECONCILIATION_HISTORY_KEY = "requirement_reconciliation_history"
REQUIREMENT_RECONCILIATION_IDEMPOTENCY_KEY = (
    "requirement_reconciliation_idempotency"
)
REQUIREMENT_RECONCILIATION_ACTIONS = frozenset(
    {
        "keep",
        "archive",
        "defer",
        "supersede",
        "already-implemented",
        "duplicate",
        "rewrite-needed",
    }
)
REQUIREMENT_RECONCILIATION_SATISFIED_ACTIONS = frozenset(
    {
        "archive",
        "defer",
        "supersede",
        "already-implemented",
        "duplicate",
    }
)
REQUIREMENT_RECONCILIATION_ALLOWED_STATES = frozenset(
    {
        OrchestratorState.STORY_INTERVIEW.value,
        OrchestratorState.STORY_PERSISTENCE.value,
        OrchestratorState.SPRINT_SETUP.value,
        OrchestratorState.SPRINT_COMPLETE.value,
    }
)
STORY_IDEMPOTENCY_REUSED_MESSAGE = (
    "Story phase idempotency key reused with different request"
)


class StoryPhaseError(Exception):
    """Domain-level story phase error for router translation."""

    def __init__(self, detail: str, *, status_code: int = 409) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def get_all_roadmap_requirements(state: dict[str, Any]) -> list[str]:
    """Extract all assigned backlog items from saved roadmap releases."""
    releases = state.get("roadmap_releases") or []
    reqs: list[str] = []
    for release in releases:
        items = release.get("items") or []
        reqs.extend(items)
    return reqs


def requirement_reconciliation_key(requirement: str) -> str:
    """Return the stable lookup key for one roadmap requirement string."""
    return " ".join(requirement.strip().split()).casefold()


def _idempotency_evidence_links(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _raise_story_idempotency_reused() -> None:
    raise StoryPhaseError(STORY_IDEMPOTENCY_REUSED_MESSAGE, status_code=409)


def _ensure_idempotency_identity_matches(
    *,
    existing_identity: dict[str, Any],
    current_identity: dict[str, Any],
) -> None:
    if existing_identity != current_identity:
        _raise_story_idempotency_reused()


def requirement_reconciliation_request_identity(
    *,
    requirement: str,
    action: str,
    reason: str,
    changed_by: str,
    evidence_links: list[str] | None,
) -> dict[str, Any]:
    """Return the canonical request identity for requirement reconciliation."""
    return {
        "requirement_key": requirement_reconciliation_key(requirement),
        "action": action.strip().lower(),
        "reason": reason.strip(),
        "changed_by": changed_by.strip(),
        "evidence_links": _idempotency_evidence_links(evidence_links or []),
    }


def requirement_reconciliation_payload_identity(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Return the canonical idempotency identity represented by a payload."""
    return {
        "requirement_key": requirement_reconciliation_key(
            str(payload.get("requirement", ""))
        ),
        "action": str(payload.get("action", "")).strip().lower(),
        "reason": str(payload.get("reason", "")).strip(),
        "changed_by": str(payload.get("changed_by", "")).strip(),
        "evidence_links": _idempotency_evidence_links(payload.get("evidence_links")),
    }


def requirement_reconciliation_for(
    state: dict[str, Any],
    *,
    parent_requirement: str,
) -> dict[str, Any] | None:
    """Return the latest requirement reconciliation decision."""
    reconciliations = state.get(REQUIREMENT_RECONCILIATION_STATE_KEY)
    if not isinstance(reconciliations, dict):
        return None
    candidate = reconciliations.get(requirement_reconciliation_key(parent_requirement))
    return candidate if isinstance(candidate, dict) else None


def requirement_reconciliation_satisfies_story_requirement(
    state: dict[str, Any],
    *,
    parent_requirement: str,
) -> bool:
    """Return whether a reconciliation means no Story work is required now."""
    reconciliation = requirement_reconciliation_for(
        state,
        parent_requirement=parent_requirement,
    )
    if reconciliation is None:
        return False
    action = str(reconciliation.get("action") or "").strip().lower()
    return action in REQUIREMENT_RECONCILIATION_SATISFIED_ACTIONS


def _roadmap_requirement_matches(
    state: dict[str, Any],
    *,
    requirement: str,
) -> list[str]:
    target_key = requirement_reconciliation_key(requirement)
    return [
        item
        for item in get_all_roadmap_requirements(state)
        if isinstance(item, str)
        and requirement_reconciliation_key(item) == target_key
    ]


def _roadmap_milestone_requirements(
    state: dict[str, Any],
    *,
    scope_id: str,
) -> list[str] | None:
    """Return requirement names for a milestone scope, or None when absent."""
    release_data = _roadmap_milestone_release(state, scope_id=scope_id)
    if release_data is None:
        return None
    return release_data[1]


def _roadmap_milestone_release(
    state: dict[str, Any],
    *,
    scope_id: str,
) -> tuple[dict[str, Any], list[str]] | None:
    """Return a milestone release and its requirement names, or None when absent."""
    roadmap_releases = state.get("roadmap_releases")
    if not isinstance(roadmap_releases, list):
        return None

    for release_index, release in enumerate(roadmap_releases):
        if not isinstance(release, dict):
            continue
        if f"milestone_{release_index}" != scope_id:
            continue
        release_data = cast("dict[str, Any]", release)
        items = release_data.get("items")
        if not isinstance(items, list):
            return release_data, []
        return release_data, [item for item in items if isinstance(item, str)]
    return None


def _story_saved_metadata(
    state: dict[str, Any],
    *,
    parent_requirement: str,
) -> dict[str, Any] | None:
    saved_metadata = state.get("story_saved_metadata")
    if not isinstance(saved_metadata, dict):
        return None
    metadata = saved_metadata.get(parent_requirement)
    return metadata if isinstance(metadata, dict) else None


def _story_saved_for_scope(
    state: dict[str, Any],
    *,
    parent_requirement: str,
    saved_reqs_dict: dict[str, Any],
    extension_metadata: dict[str, Any] | None,
) -> bool:
    if saved_reqs_dict.get(parent_requirement) is not True:
        return False
    if extension_metadata is None:
        return True
    return _metadata_matches_extension_scope(
        _story_saved_metadata(state, parent_requirement=parent_requirement),
        extension_metadata,
    )


def _mark_story_saved(
    state: dict[str, Any],
    *,
    parent_requirement: str,
) -> None:
    saved_reqs_dict = state.get("story_saved", {})
    if not isinstance(saved_reqs_dict, dict):
        saved_reqs_dict = {}
    saved_reqs_dict[parent_requirement] = True
    state["story_saved"] = saved_reqs_dict

    extension_metadata = _requirement_extension_metadata(
        state,
        parent_requirement=parent_requirement,
    )
    saved_metadata = state.get("story_saved_metadata")
    if not isinstance(saved_metadata, dict):
        saved_metadata = {}
        state["story_saved_metadata"] = saved_metadata
    if extension_metadata is not None:
        saved_metadata[parent_requirement] = extension_metadata
    else:
        saved_metadata.pop(parent_requirement, None)


def _story_resolution_for_scope(
    runtime: dict[str, Any],
    *,
    extension_metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    resolution = story_current_resolution(runtime)
    if resolution is None:
        return None
    if extension_metadata is None:
        return resolution
    resolution_projection = runtime.get("resolution_projection")
    if not isinstance(resolution_projection, dict):
        return None
    if not _metadata_matches_extension_scope(resolution_projection, extension_metadata):
        return None
    return resolution


def _scope_extension_metadata_for_requirements(
    state: dict[str, Any],
    *,
    requirements: list[str],
) -> dict[str, Any]:
    if not requirements:
        return {}

    metadata_items = [
        _requirement_extension_metadata(state, parent_requirement=requirement)
        for requirement in requirements
    ]
    if not all(isinstance(item, dict) for item in metadata_items):
        return {}

    source_item_ids: list[str] = []
    accepted_spec_version_id: int | None = None
    for item in cast("list[dict[str, Any]]", metadata_items):
        if accepted_spec_version_id is None:
            accepted_spec_version_id = _coerce_int(
                item.get("accepted_spec_version_id")
            )
        for source_item_id in _string_list(item.get("source_item_ids")):
            if source_item_id not in source_item_ids:
                source_item_ids.append(source_item_id)

    metadata: dict[str, Any] = {"extension_scope": True}
    if accepted_spec_version_id is not None:
        metadata["accepted_spec_version_id"] = accepted_spec_version_id
    if source_item_ids:
        metadata["source_item_ids"] = source_item_ids
    return metadata


def _normalized_parent_requirements(
    parent_requirements: list[str] | None,
) -> list[str]:
    """Return non-empty parent requirements deduplicated by normalized key."""
    if parent_requirements is None:
        return []

    requirements: list[str] = []
    seen_keys: set[str] = set()
    for parent_requirement in parent_requirements:
        requirement = parent_requirement.strip()
        if not requirement:
            continue

        requirement_key = normalize_requirement_key(requirement)
        if requirement_key in seen_keys:
            continue

        requirements.append(requirement)
        seen_keys.add(requirement_key)
    return requirements


def _selection_scope_id(requirements: list[str]) -> str:
    """Return deterministic completion scope ID for selected requirements."""
    return "selection:" + canonical_hash(
        {"scope": "selection", "requirements": requirements}
    )


def _roadmap_ordered_selection_requirements(
    state: dict[str, Any],
    *,
    parent_requirements: list[str],
) -> list[str]:
    """Resolve selected parent requirements into saved roadmap order."""
    selected_by_key = {
        normalize_requirement_key(requirement): requirement
        for requirement in parent_requirements
    }
    matched_keys: set[str] = set()
    selected_requirements: list[str] = []

    for roadmap_requirement in get_all_roadmap_requirements(state):
        if not isinstance(roadmap_requirement, str):
            continue

        requirement_key = normalize_requirement_key(roadmap_requirement)
        if requirement_key not in selected_by_key or requirement_key in matched_keys:
            continue

        selected_requirements.append(roadmap_requirement)
        matched_keys.add(requirement_key)

    for requirement in parent_requirements:
        requirement_key = normalize_requirement_key(requirement)
        if requirement_key not in matched_keys:
            raise StoryPhaseError(
                "Story completion selection includes unknown roadmap requirement: "
                f"{requirement}.",
                status_code=400,
            )

    return selected_requirements


def _story_completion_scope_requirements(
    state: dict[str, Any],
    *,
    scope: str | None,
    scope_id: str | None,
    parent_requirements: list[str] | None = None,
) -> tuple[list[str], dict[str, Any] | None]:
    """Resolve Story completion requirements for full or scoped completion."""
    normalized_scope = scope.strip() if isinstance(scope, str) else None
    normalized_scope_id = scope_id.strip() if isinstance(scope_id, str) else None
    if parent_requirements is not None and normalized_scope != "selection":
        raise StoryPhaseError(
            "--parent-requirement is only supported with --scope selection",
            status_code=400,
        )
    if normalized_scope == "selection":
        if normalized_scope_id:
            raise StoryPhaseError(
                "story complete --scope selection does not accept --scope-id",
                status_code=400,
            )

        normalized_parent_requirements = _normalized_parent_requirements(
            parent_requirements
        )
        if not normalized_parent_requirements:
            raise StoryPhaseError(
                "story complete --scope selection requires at least one "
                "--parent-requirement",
                status_code=400,
            )

        requirements = _roadmap_ordered_selection_requirements(
            state,
            parent_requirements=normalized_parent_requirements,
        )
        return requirements, {
            "schema_version": _STORY_COMPLETION_SCOPE_SCHEMA_VERSION,
            "scope": normalized_scope,
            "scope_id": _selection_scope_id(requirements),
            "requirements": requirements,
            **_scope_extension_metadata_for_requirements(
                state,
                requirements=requirements,
            ),
        }

    if not normalized_scope and not normalized_scope_id:
        return get_all_roadmap_requirements(state), None
    if not normalized_scope or not normalized_scope_id:
        raise StoryPhaseError(
            "story complete scope requires both --scope and --scope-id",
            status_code=400,
        )
    if normalized_scope != "milestone":
        raise StoryPhaseError(
            f"Unsupported story completion scope: {normalized_scope}",
            status_code=400,
        )

    release_data = _roadmap_milestone_release(
        state,
        scope_id=normalized_scope_id,
    )
    if release_data is None:
        raise StoryPhaseError(
            "Story completion scope "
            f"{normalized_scope_id} does not match any roadmap milestone.",
            status_code=400,
        )
    release, requirements = release_data
    extension_context = _scope_extension_context(state)
    extension_metadata = (
        _release_extension_metadata(release, extension_context=extension_context)
        if extension_context is not None
        else None
    )

    return requirements, {
        "schema_version": _STORY_COMPLETION_SCOPE_SCHEMA_VERSION,
        "scope": normalized_scope,
        "scope_id": normalized_scope_id,
        "requirements": requirements,
        **(extension_metadata or {}),
    }


def _story_completion_scope_identity(
    scope_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(scope_payload, dict):
        return {
            "scope": None,
            "scope_id": None,
            "requirements": [],
        }
    return {
        "scope": scope_payload.get("scope"),
        "scope_id": scope_payload.get("scope_id"),
        "requirements": _string_list(scope_payload.get("requirements")),
    }


def story_parent_rank(state: dict[str, Any], parent_requirement: str) -> int | None:
    """Return 1-based Roadmap order for a parent requirement."""
    parent_key = normalize_requirement_key(parent_requirement)
    roadmap_releases = state.get("roadmap_releases")
    if not isinstance(roadmap_releases, list):
        return None

    rank = 0
    for release in roadmap_releases:
        if not isinstance(release, dict):
            continue
        items = release.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, str) or not item.strip():
                continue
            rank += 1
            if normalize_requirement_key(item) == parent_key:
                return rank
    return None


def _story_points_from_effort(estimated_effort: object) -> int:
    if not isinstance(estimated_effort, str):
        raise StoryPhaseError(
            "Story readiness repair requires estimated_effort for every story.",
            status_code=409,
        )
    effort = estimated_effort.strip().upper()
    story_points = _EFFORT_TO_STORY_POINTS.get(effort)
    if story_points is None:
        raise StoryPhaseError(
            f"Story readiness repair cannot map estimated_effort {estimated_effort!r}.",
            status_code=409,
        )
    return story_points


def _story_output_parent_requirement(
    output_key: object,
    output: dict[str, Any],
) -> str:
    parent_requirement = output.get("parent_requirement")
    if isinstance(parent_requirement, str) and parent_requirement.strip():
        return parent_requirement.strip()
    if isinstance(output_key, str) and output_key.strip():
        return output_key.strip()
    raise StoryPhaseError(
        "Story readiness repair requires parent_requirement for saved Story outputs.",
        status_code=409,
    )


def _ordered_story_outputs(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    story_outputs = state.get("story_outputs")
    if not isinstance(story_outputs, dict):
        raise StoryPhaseError(
            "Story readiness repair requires saved Story outputs.",
            status_code=409,
        )
    if not story_outputs:
        raise StoryPhaseError(
            "Story readiness repair requires saved Story outputs.",
            status_code=409,
        )

    available: list[tuple[str, dict[str, Any]]] = []
    for output_key, output in story_outputs.items():
        if not isinstance(output, dict):
            raise StoryPhaseError(
                "Story readiness repair found malformed saved Story output.",
                status_code=409,
            )
        available.append((_story_output_parent_requirement(output_key, output), output))

    ordered: list[tuple[str, dict[str, Any]]] = []
    used_indexes: set[int] = set()
    for roadmap_requirement in get_all_roadmap_requirements(state):
        roadmap_key = normalize_requirement_key(roadmap_requirement)
        for index, (parent_requirement, output) in enumerate(available):
            if index in used_indexes:
                continue
            if normalize_requirement_key(parent_requirement) != roadmap_key:
                continue
            ordered.append((parent_requirement, output))
            used_indexes.add(index)
            break

    ordered.extend(
        item for index, item in enumerate(available) if index not in used_indexes
    )
    return ordered


def _story_readiness_repair_items(
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    repair_items: list[dict[str, Any]] = []
    for parent_requirement, output in _ordered_story_outputs(state):
        parent_rank = story_parent_rank(state, parent_requirement)
        if parent_rank is None:
            message = (
                "Story readiness repair requires Roadmap order for saved Story outputs."
            )
            raise StoryPhaseError(
                message,
                status_code=409,
            )

        user_stories = output.get("user_stories")
        if not isinstance(user_stories, list):
            raise StoryPhaseError(
                "Story readiness repair requires user_stories in saved Story outputs.",
                status_code=409,
            )

        for slot, story in enumerate(user_stories, start=1):
            if not isinstance(story, dict):
                raise StoryPhaseError(
                    "Story readiness repair found malformed saved Story item.",
                    status_code=409,
                )
            story_data = cast("dict[str, Any]", story)
            repair_items.append(
                {
                    "parent_requirement": parent_requirement,
                    "parent_rank": parent_rank,
                    "slot": slot,
                    "story_points": _story_points_from_effort(
                        story_data.get("estimated_effort")
                    ),
                    "rank": str(parent_rank * 100 + slot),
                }
            )

    if not repair_items:
        raise StoryPhaseError(
            "Story readiness repair requires saved Story outputs.",
            status_code=409,
        )
    return repair_items


def ensure_story_runtime(
    state: dict[str, Any],
    *,
    parent_requirement: str,
) -> dict[str, Any]:
    return hydrate_story_runtime_from_legacy(
        state,
        parent_requirement=parent_requirement,
    )


def existing_story_runtime(
    state: dict[str, Any],
    *,
    parent_requirement: str,
) -> dict[str, Any] | None:
    interview_runtime = state.get("interview_runtime")
    if not isinstance(interview_runtime, dict):
        return None

    story_runtime = interview_runtime.get("story")
    if not isinstance(story_runtime, dict):
        return None

    runtime = story_runtime.get(parent_requirement)
    if not isinstance(runtime, dict):
        return None
    return runtime


def story_retryable(classification: str | None) -> bool:
    return classification in {
        "nonreusable_provider_failure",
        "nonreusable_transport_failure",
    }


def _attempt_has_clarifying_questions(attempt: dict[str, Any] | None) -> bool:
    if not isinstance(attempt, dict):
        return False
    artifact = attempt.get("output_artifact")
    if not isinstance(artifact, dict):
        return False
    questions = artifact.get("clarifying_questions")
    if not isinstance(questions, list):
        return False
    return any(isinstance(question, str) and question.strip() for question in questions)


def _should_soft_gate_story_feedback(
    *,
    feedback_quality: dict[str, Any],
    force_feedback: bool,
    has_working_state: bool,
    latest_attempt: dict[str, Any] | None,
) -> bool:
    if not feedback_quality.get("needs_revision") or force_feedback:
        return False
    latest_classification = (
        latest_attempt.get("classification")
        if isinstance(latest_attempt, dict)
        else None
    )
    if latest_classification == "quality_gate_failed":
        return not _attempt_has_clarifying_questions(latest_attempt)
    if (
        isinstance(latest_classification, str)
        and latest_classification.startswith("nonreusable_")
    ):
        return False
    return has_working_state


def _find_attempt_by_id(
    runtime: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any] | None:
    for attempt in reversed(runtime.get("attempt_history") or []):
        if not isinstance(attempt, dict):
            continue
        if attempt.get("attempt_id") == attempt_id:
            return attempt
    return None


def _story_current_draft_attempt(
    runtime: dict[str, Any],
) -> dict[str, Any] | None:
    draft_projection = runtime.get("draft_projection") or {}
    attempt_id = draft_projection.get("latest_reusable_attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        return None
    return _find_attempt_by_id(runtime, attempt_id)


def _story_attempt_artifact_for_scope(
    attempt: dict[str, Any] | None,
    *,
    extension_metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not _record_matches_story_scope(attempt, extension_metadata):
        return None
    artifact = (attempt or {}).get("output_artifact")
    if not isinstance(artifact, dict):
        return None

    if artifact.get("artifact_kind") == "story_patch" and isinstance(
        artifact.get("story"),
        dict,
    ):
        return artifact

    stories = artifact.get("user_stories")
    if not isinstance(stories, list) or len(stories) == 0:
        return None
    return artifact


def _story_current_draft_artifact(
    runtime: dict[str, Any],
) -> dict[str, Any] | None:
    return _story_attempt_artifact_for_scope(
        _story_current_draft_attempt(runtime),
        extension_metadata=None,
    )


def _story_artifact_fingerprint(
    parent_requirement: str,
    output_artifact: dict[str, Any],
) -> str:
    fingerprint_artifact = {
        key: value
        for key, value in output_artifact.items()
        if key not in {"attempt_id", "artifact_fingerprint"}
    }
    return canonical_hash(
        {
            "phase": "story",
            "parent_requirement": parent_requirement,
            "output_artifact": fingerprint_artifact,
        }
    )


def _attach_story_attempt_guards(
    runtime: dict[str, Any],
    *,
    attempt_id: str,
    parent_requirement: str,
) -> None:
    attempt = _find_attempt_by_id(runtime, attempt_id)
    if not isinstance(attempt, dict):
        return

    output_artifact = attempt.get("output_artifact")
    if not isinstance(output_artifact, dict):
        return

    artifact_fingerprint = _story_artifact_fingerprint(
        parent_requirement,
        output_artifact,
    )
    attempt["artifact_fingerprint"] = artifact_fingerprint
    output_artifact["artifact_fingerprint"] = artifact_fingerprint

    draft_projection = runtime.get("draft_projection")
    if (
        isinstance(draft_projection, dict)
        and draft_projection.get("latest_reusable_attempt_id") == attempt_id
    ):
        draft_projection["artifact_fingerprint"] = artifact_fingerprint


def _story_merge_recommendation_from_artifact(
    artifact: dict[str, Any],
) -> dict[str, Any] | None:
    stories = artifact.get("user_stories")
    if not isinstance(stories, list):
        return None

    for story in stories:
        if not isinstance(story, dict):
            continue
        if story.get("invest_score") != "Low":
            continue

        warning = story.get("decomposition_warning")
        if not isinstance(warning, str) or not warning.strip():
            continue

        normalized_warning = " ".join(warning.lower().split())
        if not any(
            signal in normalized_warning
            for signal in (
                "recommend consolidating",
                "merge this",
                "retire this separate requirement",
                "retire this requirement",
                "merge into",
                "consolidated into",
                "may be redundant",
                "requirement may be redundant",
            )
        ):
            continue

        owner_match = re.search(r"owned by '([^']+)'", warning, flags=re.IGNORECASE)
        if not owner_match:
            continue

        acceptance_criteria = story.get("acceptance_criteria")
        if not isinstance(acceptance_criteria, list):
            acceptance_criteria = []

        return {
            "action": "merge_into_requirement",
            "owner_requirement": owner_match.group(1).strip(),
            "reason": warning.strip(),
            "acceptance_criteria_to_move": [
                item
                for item in acceptance_criteria
                if isinstance(item, str) and item.strip()
            ],
        }

    return None


def story_save_payload(runtime: dict[str, Any]) -> dict[str, Any] | None:
    return story_save_payload_for_scope(runtime, extension_metadata=None)


def story_save_payload_for_scope(
    runtime: dict[str, Any],
    *,
    extension_metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    draft_projection = runtime.get("draft_projection") or {}
    if draft_projection.get("kind") != "complete_draft":
        return None
    if not _record_matches_story_scope(draft_projection, extension_metadata):
        return None

    artifact = _story_attempt_artifact_for_scope(
        _story_current_draft_attempt(runtime),
        extension_metadata=extension_metadata,
    )
    if artifact is None:
        return None
    if _story_merge_recommendation_from_artifact(artifact):
        return None
    if not artifact.get("is_complete") or not story_quality_saveable(artifact):
        return None
    return artifact


def story_patch_save_payload_for_scope(
    runtime: dict[str, Any],
    *,
    extension_metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    draft_projection = runtime.get("draft_projection") or {}
    if draft_projection.get("kind") != "story_patch":
        return None
    if not _record_matches_story_scope(draft_projection, extension_metadata):
        return None

    artifact = _story_attempt_artifact_for_scope(
        _story_current_draft_attempt(runtime),
        extension_metadata=extension_metadata,
    )
    if artifact is None or artifact.get("artifact_kind") != "story_patch":
        return None
    if not isinstance(artifact.get("story"), dict):
        return None
    if not artifact.get("is_complete") or not story_quality_saveable(artifact):
        return None
    return artifact


def _story_counts_from_artifact(artifact: dict[str, Any]) -> tuple[int, dict[str, int]]:
    if artifact.get("artifact_kind") == "story_patch":
        story = artifact.get("story")
        if not isinstance(story, dict):
            return 0, _zero_invest_score_counts()
        counts = _zero_invest_score_counts()
        score = story.get("invest_score")
        if isinstance(score, str):
            counts[score] = counts.get(score, 0) + 1
        return 1, counts

    stories = artifact.get("user_stories")
    if not isinstance(stories, list):
        return 0, _zero_invest_score_counts()

    counts = _zero_invest_score_counts()
    for story in stories:
        if not isinstance(story, dict):
            continue
        score = story.get("invest_score")
        if isinstance(score, str):
            counts[score] = counts.get(score, 0) + 1
    return len(stories), counts


def _zero_invest_score_counts() -> dict[str, int]:
    return dict.fromkeys(_INVEST_SCORES, 0)


def _runtime_failure_finding_from_artifact(
    artifact: dict[str, Any],
) -> dict[str, Any] | None:
    if not (
        artifact.get("error") == "STORY_GENERATION_FAILED"
        or artifact.get("failure_stage")
    ):
        return None
    message = str(
        artifact.get("failure_summary")
        or artifact.get("message")
        or artifact.get("error")
        or "Story generation failed."
    )
    finding: dict[str, Any] = {
        "code": "STORY_RUNTIME_FAILURE",
        "severity": "blocking",
        "message": message,
    }
    failure_stage = artifact.get("failure_stage")
    if isinstance(failure_stage, str) and failure_stage:
        finding["failure_stage"] = failure_stage
    failure_artifact_id = artifact.get("failure_artifact_id")
    if isinstance(failure_artifact_id, str) and failure_artifact_id:
        finding["failure_artifact_id"] = failure_artifact_id
    return finding


def _runtime_failure_finding_key(finding: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (
        finding.get("code"),
        finding.get("failure_artifact_id"),
        finding.get("failure_stage"),
    )


def _quality_findings_from_artifact(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    quality = artifact.get("quality")
    findings = (
        quality.get("quality_findings")
        if isinstance(quality, dict)
        else artifact.get("quality_findings")
    )
    quality_findings = (
        [finding for finding in findings if isinstance(finding, dict)]
        if isinstance(findings, list)
        else []
    )
    runtime_failure_finding = _runtime_failure_finding_from_artifact(artifact)
    if runtime_failure_finding is not None and _runtime_failure_finding_key(
        runtime_failure_finding,
    ) not in {_runtime_failure_finding_key(finding) for finding in quality_findings}:
        quality_findings.append(runtime_failure_finding)
    return quality_findings


def story_quality_summary(artifact: dict[str, Any] | None) -> dict[str, Any]:
    """Return save/review quality summary for a Story draft artifact."""
    if not isinstance(artifact, dict):
        return {
            "schema_version": _STORY_QUALITY_SCHEMA_VERSION,
            "coverage_status": "needs_clarification",
            "remaining_scope": [],
            "story_count": 0,
            "invest_score_counts": _zero_invest_score_counts(),
            "requested_story_count": None,
            "quality_findings": [],
            "blocking_findings": [],
            "saveable": False,
        }

    quality = artifact.get("quality")
    quality = quality if isinstance(quality, dict) else {}
    story_count, invest_score_counts = _story_counts_from_artifact(artifact)
    coverage_status = quality.get("coverage_status") or artifact.get(
        "coverage_status",
    )
    if not isinstance(coverage_status, str):
        coverage_status = (
            "complete" if artifact.get("is_complete") else "needs_clarification"
        )
    remaining_scope = quality.get("remaining_scope") or artifact.get("remaining_scope")
    if not isinstance(remaining_scope, list):
        remaining_scope = []
    remaining_scope = [item for item in remaining_scope if isinstance(item, str)]
    findings = _quality_findings_from_artifact(artifact)
    blocking = [
        finding for finding in findings if finding.get("severity") == "blocking"
    ]
    all_low = story_count > 0 and invest_score_counts.get("Low", 0) == story_count
    computed_saveable = (
        bool(artifact.get("is_complete"))
        and coverage_status == "complete"
        and not blocking
        and not all_low
    )
    if quality.get("saveable") is False:
        computed_saveable = False
    return {
        "schema_version": quality.get(
            "schema_version",
            _STORY_QUALITY_SCHEMA_VERSION,
        ),
        "coverage_status": coverage_status,
        "remaining_scope": remaining_scope,
        "story_count": story_count,
        "invest_score_counts": invest_score_counts,
        "requested_story_count": quality.get("requested_story_count"),
        "quality_findings": findings,
        "blocking_findings": blocking,
        "saveable": computed_saveable,
    }


def story_quality_saveable(artifact: dict[str, Any] | None) -> bool:
    """Return whether a draft artifact passes the Story quality save gate."""
    return bool(story_quality_summary(artifact)["saveable"])


def _quality_finding_from_dependency_preflight(
    finding: dict[str, Any],
) -> dict[str, Any]:
    """Return Story quality finding fields from dependency preflight output."""
    return {
        "code": str(finding.get("code") or "STORY_DEPENDENCY_CANDIDATE_INVALID"),
        "severity": str(finding.get("severity") or "blocking"),
        "message": str(finding.get("message") or "Story dependency candidate failed."),
        "affected_story_indexes": [
            int(index)
            for index in finding.get("affected_story_indexes") or []
            if isinstance(index, int)
        ],
        "affected_story_titles": [
            title
            for title in finding.get("affected_story_titles") or []
            if isinstance(title, str)
        ],
    }


def _append_quality_findings(
    artifact: dict[str, Any],
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    quality = artifact.get("quality")
    quality = dict(quality) if isinstance(quality, dict) else {}
    existing = _quality_findings_from_artifact(artifact)
    seen = {
        (
            finding.get("code"),
            tuple(finding.get("affected_story_indexes") or []),
        )
        for finding in existing
    }
    for finding in findings:
        key = (
            finding.get("code"),
            tuple(finding.get("affected_story_indexes") or []),
        )
        if key in seen:
            continue
        existing.append(finding)
        seen.add(key)
    blocking = [
        finding for finding in existing if finding.get("severity") == "blocking"
    ]
    quality["quality_findings"] = existing
    quality["blocking_findings"] = blocking
    if blocking:
        quality["saveable"] = False
    else:
        quality["saveable"] = story_quality_summary(
            {**artifact, "quality": quality, "quality_findings": existing}
        )["saveable"]
    artifact["quality"] = quality
    artifact["quality_findings"] = existing
    return quality


def _apply_dependency_preflight_to_story_result(
    story_result: dict[str, Any],
    *,
    project_id: int,
    parent_requirement: str,
    parent_rank: int | None,
    dependency_preflight: Callable[[SaveStoriesInput], dict[str, Any]] | None,
) -> dict[str, Any]:
    if dependency_preflight is None or not story_result.get("success"):
        return story_result
    artifact = story_result.get("output_artifact")
    if not isinstance(artifact, dict):
        return story_result
    stories = artifact.get("user_stories")
    if not isinstance(stories, list):
        return story_result
    if not any(
        isinstance(story, dict) and story.get("dependency_candidates")
        for story in stories
    ):
        return story_result

    preflight = dependency_preflight(
        SaveStoriesInput(
            product_id=project_id,
            parent_requirement=parent_requirement,
            parent_rank=parent_rank,
            idempotency_key=f"story-dependency-preflight-{project_id}",
            stories=[story for story in stories if isinstance(story, dict)],
        )
    )
    blocking = [
        _quality_finding_from_dependency_preflight(finding)
        for finding in preflight.get("blocking_findings") or []
        if isinstance(finding, dict)
    ]
    warnings = [
        _quality_finding_from_dependency_preflight(finding)
        for finding in preflight.get("warning_findings") or []
        if isinstance(finding, dict)
    ]
    if not blocking and not warnings:
        return story_result

    quality = _append_quality_findings(artifact, [*blocking, *warnings])
    story_result["quality"] = quality
    story_result["output_artifact"] = artifact
    if blocking:
        artifact["is_complete"] = False
        story_result["classification"] = "quality_gate_failed"
        story_result["draft_kind"] = "quality_blocked_draft"
        story_result["is_reusable"] = False
        story_result["is_complete"] = False
    return story_result


def _validate_story_save_required_guards(
    *,
    attempt_id: str | None,
    expected_artifact_fingerprint: str | None,
    expected_state: str | None,
    idempotency_key: str | None,
) -> None:
    if attempt_id is None or not attempt_id.strip():
        raise StoryPhaseError("story save requires --attempt-id", status_code=400)
    if (
        expected_artifact_fingerprint is None
        or not expected_artifact_fingerprint.strip()
    ):
        raise StoryPhaseError(
            "story save requires --expected-artifact-fingerprint",
            status_code=400,
        )
    if expected_state != OrchestratorState.STORY_REVIEW.value:
        raise StoryPhaseError(
            "story save requires --expected-state STORY_REVIEW",
            status_code=400,
        )
    if idempotency_key is None or not idempotency_key.strip():
        raise StoryPhaseError(
            "story save requires --idempotency-key",
            status_code=400,
        )


def _story_save_replay_payload(
    state: dict[str, Any],
    *,
    idempotency_key: str,
    parent_requirement: str,
    attempt_id: str,
    expected_artifact_fingerprint: str,
    operation: str | None = None,
    target_story_id: int | None = None,
    target_refinement_slot: int | None = None,
) -> dict[str, Any] | None:
    idempotency_registry = state.get("story_save_idempotency")
    if not isinstance(idempotency_registry, dict):
        return None

    existing = idempotency_registry.get(idempotency_key)
    if not isinstance(existing, dict):
        return None

    if existing.get("attempt_id") != attempt_id:
        raise StoryPhaseError(
            "story save attempt mismatch; refresh history and review the current draft",
            status_code=409,
        )
    if existing.get("artifact_fingerprint") != expected_artifact_fingerprint:
        raise StoryPhaseError(
            "story save artifact fingerprint mismatch; refresh history and review the current draft",  # noqa: E501
            status_code=409,
        )
    if existing.get("parent_requirement") != parent_requirement:
        raise StoryPhaseError(
            "story save idempotency key does not match this requirement",
            status_code=409,
        )
    existing_operation = existing.get("operation")
    if operation is None and existing_operation is not None:
        raise StoryPhaseError(
            "story save idempotency key does not match this operation",
            status_code=409,
        )
    if operation is not None and existing_operation != operation:
        raise StoryPhaseError(
            "story save idempotency key does not match this operation",
            status_code=409,
        )
    if (
        operation == "story_patch"
        and existing.get("target_story_id") != target_story_id
    ):
        raise StoryPhaseError(
            "story save idempotency key does not match this target story",
            status_code=409,
        )
    if (
        operation == "story_patch"
        and existing.get("target_refinement_slot") != target_refinement_slot
    ):
        raise StoryPhaseError(
            "story save idempotency key does not match this target slot",
            status_code=409,
        )
    return dict(existing)


def _validate_story_save_current_attempt_artifact(
    runtime: dict[str, Any],
    *,
    parent_requirement: str,
    attempt_id: str,
    expected_artifact_fingerprint: str,
) -> None:
    attempt = _find_attempt_by_id(runtime, attempt_id)
    output_artifact = (attempt or {}).get("output_artifact")
    if not isinstance(attempt, dict) or not isinstance(output_artifact, dict):
        raise StoryPhaseError(
            "story save attempt mismatch; refresh history and review the current draft",
            status_code=409,
        )

    if attempt.get("artifact_fingerprint") != expected_artifact_fingerprint:
        raise StoryPhaseError(
            "story save artifact fingerprint mismatch; refresh history and review the current draft",  # noqa: E501
            status_code=409,
        )

    current_artifact_fingerprint = _story_artifact_fingerprint(
        parent_requirement,
        output_artifact,
    )
    if current_artifact_fingerprint != expected_artifact_fingerprint:
        raise StoryPhaseError(
            "story save artifact fingerprint mismatch; refresh history and review the current draft",  # noqa: E501
            status_code=409,
        )


def _validate_story_patch_target_selector(
    *,
    target_story_id: int | None,
    target_refinement_slot: int | None,
) -> None:
    if (target_story_id is None) == (target_refinement_slot is None):
        raise StoryPhaseError(
            "story save-patch requires exactly one target selector",
            status_code=400,
        )


def _validate_story_generate_target_selector(
    *,
    target_story_id: int | None,
    target_refinement_slot: int | None,
) -> None:
    if target_story_id is not None and target_refinement_slot is not None:
        raise StoryPhaseError(
            "Exactly one of target_story_id or target_refinement_slot is allowed.",
            status_code=400,
        )


def _resolve_story_patch_target_slot(
    *,
    project_id: int,
    parent_requirement: str,
    target_story_id: int | None,
    target_refinement_slot: int | None,
    resolve_target_refinement_slot: Callable[[int, str, int], int | None] | None,
) -> int:
    if target_refinement_slot is not None:
        return target_refinement_slot
    if target_story_id is None:
        raise StoryPhaseError(
            "story save-patch requires exactly one target selector",
            status_code=400,
        )
    if resolve_target_refinement_slot is None:
        raise StoryPhaseError(
            "story save-patch cannot resolve --target-story-id",
            status_code=400,
        )
    resolved = resolve_target_refinement_slot(
        project_id,
        parent_requirement,
        target_story_id,
    )
    if resolved is None:
        raise StoryPhaseError(
            "story save-patch target does not belong to the requested requirement",
            status_code=409,
        )
    return resolved


def _story_patch_artifact_for_save(
    runtime: dict[str, Any],
    *,
    attempt_id: str,
    target_story_id: int | None,
    target_refinement_slot: int,
) -> dict[str, Any]:
    attempt = _find_attempt_by_id(runtime, attempt_id)
    artifact = _attempt_output_artifact(attempt)
    if not isinstance(attempt, dict) or attempt.get("draft_kind") != "story_patch":
        raise StoryPhaseError(
            "story save-patch requires a story_patch draft",
            status_code=409,
        )
    if not isinstance(artifact, dict) or artifact.get("artifact_kind") != "story_patch":
        raise StoryPhaseError(
            "story save-patch artifact is not a story_patch",
            status_code=409,
        )
    story = artifact.get("story")
    if not isinstance(story, dict):
        raise StoryPhaseError(
            "story save-patch target story is invalid",
            status_code=409,
        )

    artifact_slot = artifact.get("target_refinement_slot")
    if artifact_slot != target_refinement_slot:
        raise StoryPhaseError(
            "story save-patch target mismatch; refresh history and review the current draft",  # noqa: E501
            status_code=409,
        )
    attempt_slot = attempt.get("target_refinement_slot")
    if attempt_slot is not None and attempt_slot != target_refinement_slot:
        raise StoryPhaseError(
            "story save-patch target mismatch; refresh history and review the current draft",  # noqa: E501
            status_code=409,
        )

    artifact_story_id = artifact.get("target_story_id")
    attempt_story_id = attempt.get("target_story_id")
    if target_story_id is not None:
        known_story_ids = [
            story_id
            for story_id in (artifact_story_id, attempt_story_id)
            if story_id is not None
        ]
        if any(story_id != target_story_id for story_id in known_story_ids):
            raise StoryPhaseError(
                "story save-patch target mismatch; refresh history and review the current draft",  # noqa: E501
                status_code=409,
            )
    return artifact


def _story_patch_merged_output(
    state: dict[str, Any],
    *,
    parent_requirement: str,
    patch_artifact: dict[str, Any],
    patch_story: dict[str, Any],
    target_refinement_slot: int,
) -> dict[str, Any]:
    target_index = target_refinement_slot - 1
    story_outputs = state.get("story_outputs")
    existing = (
        story_outputs.get(parent_requirement)
        if isinstance(story_outputs, dict)
        else None
    )
    existing_stories = (
        existing.get("user_stories") if isinstance(existing, dict) else None
    )
    if not isinstance(existing_stories, list):
        if target_index == 0:
            return {
                "parent_requirement": parent_requirement,
                "user_stories": [deepcopy(patch_story)],
                "is_complete": bool(patch_artifact.get("is_complete", True)),
                "clarifying_questions": list(
                    patch_artifact.get("clarifying_questions") or []
                ),
            }
        raise StoryPhaseError(
            "story save-patch cannot reconstruct sibling story output",
            status_code=409,
        )
    if target_index < 0 or target_index >= len(existing_stories):
        raise StoryPhaseError(
            "story save-patch target slot is outside the existing story output",
            status_code=409,
        )

    merged = deepcopy(existing) if isinstance(existing, dict) else {}
    merged_stories = deepcopy(existing_stories)
    merged_stories[target_index] = deepcopy(patch_story)
    merged["parent_requirement"] = parent_requirement
    merged["user_stories"] = merged_stories
    return merged


def story_current_resolution(
    runtime: dict[str, Any],
) -> dict[str, Any] | None:
    resolution_projection = runtime.get("resolution_projection") or {}
    if not isinstance(resolution_projection, dict) or not resolution_projection:
        return None
    if resolution_projection.get("status") != "merged":
        return None

    owner_requirement = resolution_projection.get("owner_requirement")
    reason = resolution_projection.get("reason")
    criteria = resolution_projection.get("acceptance_criteria_to_move")
    if not isinstance(owner_requirement, str) or not owner_requirement.strip():
        return None
    if not isinstance(reason, str) or not reason.strip():
        return None
    if not isinstance(criteria, list):
        criteria = []

    return {
        "status": "merged",
        "owner_requirement": owner_requirement,
        "reason": reason,
        "acceptance_criteria_to_move": [
            item for item in criteria if isinstance(item, str) and item.strip()
        ],
        "resolved_at": resolution_projection.get("resolved_at"),
    }


def story_merge_recommendation_payload(
    runtime: dict[str, Any],
) -> dict[str, Any] | None:
    artifact = _story_current_draft_artifact(runtime)
    if not isinstance(artifact, dict):
        return None
    return _story_merge_recommendation_from_artifact(artifact)


def story_resolution_summary(runtime: dict[str, Any]) -> dict[str, Any]:
    current = story_current_resolution(runtime)
    recommendation = None if current else story_merge_recommendation_payload(runtime)
    return {
        "available": bool(recommendation),
        "current": current,
        "recommendation": recommendation,
    }


def story_has_working_state(runtime: dict[str, Any]) -> bool:
    return story_has_working_state_for_scope(runtime, extension_metadata=None)


def story_has_working_state_for_scope(
    runtime: dict[str, Any],
    *,
    extension_metadata: dict[str, Any] | None,
) -> bool:
    if story_current_resolution(runtime):
        resolution_projection = runtime.get("resolution_projection")
        return _record_matches_story_scope(
            resolution_projection if isinstance(resolution_projection, dict) else None,
            extension_metadata,
        )

    draft_projection = runtime.get("draft_projection") or {}
    if draft_projection and _record_matches_story_scope(
        draft_projection if isinstance(draft_projection, dict) else None,
        extension_metadata,
    ):
        return True

    request_projection = runtime.get("request_projection") or {}
    if isinstance(request_projection.get("payload"), dict) and (
        _record_matches_story_scope(request_projection, extension_metadata)
    ):
        return True

    feedback_projection = runtime.get("feedback_projection") or {}
    items = feedback_projection.get("items") or []
    if not isinstance(items, list):
        return False

    return any(
        isinstance(item, dict)
        and item.get("status") == "unabsorbed"
        and isinstance(item.get("text"), str)
        and item.get("text").strip()
        and _record_matches_story_scope(item, extension_metadata)
        for item in items
    )


def story_has_prior_attempt(runtime: dict[str, Any]) -> bool:
    return story_has_prior_attempt_for_scope(runtime, extension_metadata=None)


def story_has_prior_attempt_for_scope(
    runtime: dict[str, Any],
    *,
    extension_metadata: dict[str, Any] | None,
) -> bool:
    attempts = runtime.get("attempt_history") or []
    if not isinstance(attempts, list):
        return False

    return any(
        isinstance(attempt, dict)
        and attempt.get("trigger") != "reset"
        and _record_matches_story_scope(attempt, extension_metadata)
        for attempt in attempts
    )


def story_retry_target_attempt_id(runtime: dict[str, Any]) -> str | None:
    attempts = runtime.get("attempt_history") or []
    latest_attempt = attempts[-1] if attempts else {}
    request_projection = runtime.get("request_projection") or {}
    if not (
        isinstance(latest_attempt, dict)
        and latest_attempt.get("retryable")
        and isinstance(request_projection.get("payload"), dict)
    ):
        return None

    attempt_id = latest_attempt.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        return None
    return attempt_id


def _latest_story_attempt(runtime: dict[str, Any]) -> dict[str, Any] | None:
    attempts = runtime.get("attempt_history") or []
    for attempt in reversed(attempts):
        if isinstance(attempt, dict):
            return attempt
    return None


def _attempt_output_artifact(attempt: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(attempt, dict):
        return None
    artifact = attempt.get("output_artifact")
    return artifact if isinstance(artifact, dict) else None


def story_interview_summary(
    runtime: dict[str, Any],
    *,
    extension_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    draft_projection = runtime.get("draft_projection") or {}
    retry_target_attempt_id = story_retry_target_attempt_id(runtime)
    save_payload = story_save_payload_for_scope(
        runtime,
        extension_metadata=extension_metadata,
    ) or story_patch_save_payload_for_scope(
        runtime,
        extension_metadata=extension_metadata,
    )
    latest_attempt = _latest_story_attempt(runtime)
    latest_artifact = _attempt_output_artifact(latest_attempt)

    current_draft = None
    current_draft_attempt = _story_current_draft_attempt(runtime)
    if draft_projection and _record_matches_story_scope(
        draft_projection if isinstance(draft_projection, dict) else None,
        extension_metadata,
    ) and _record_matches_story_scope(
        current_draft_attempt,
        extension_metadata,
    ):
        current_draft = {
            "attempt_id": draft_projection.get("latest_reusable_attempt_id"),
            "kind": draft_projection.get("kind"),
            "is_complete": bool(draft_projection.get("is_complete", False)),
        }
        if "target_story_id" in draft_projection:
            current_draft["target_story_id"] = draft_projection.get("target_story_id")
        if "target_refinement_slot" in draft_projection:
            current_draft["target_refinement_slot"] = draft_projection.get(
                "target_refinement_slot",
            )
        artifact_fingerprint = draft_projection.get("artifact_fingerprint")
        if isinstance(artifact_fingerprint, str) and artifact_fingerprint:
            current_draft["artifact_fingerprint"] = artifact_fingerprint

    summary_attempt = latest_attempt
    summary_artifact = latest_artifact
    if summary_attempt is None and current_draft is not None:
        summary_attempt = _find_attempt_by_id(
            runtime,
            str(current_draft.get("attempt_id")),
        )
        summary_artifact = _attempt_output_artifact(summary_attempt)
    quality = story_quality_summary(summary_artifact)
    attempt_id = (
        summary_attempt.get("attempt_id") if isinstance(summary_attempt, dict) else None
    )
    artifact_fingerprint = None
    if isinstance(summary_attempt, dict):
        artifact_fingerprint = summary_attempt.get("artifact_fingerprint")
    if not isinstance(artifact_fingerprint, str) and isinstance(
        summary_artifact,
        dict,
    ):
        artifact_fingerprint = summary_artifact.get("artifact_fingerprint")

    save_summary: dict[str, Any] = {
        "available": bool(save_payload),
    }
    draft_attempt_id = draft_projection.get("latest_reusable_attempt_id")
    draft_fingerprint = draft_projection.get("artifact_fingerprint")
    if (
        save_payload
        and isinstance(draft_attempt_id, str)
        and draft_attempt_id
        and isinstance(draft_fingerprint, str)
        and draft_fingerprint
    ):
        save_summary.update(
            {
                "attempt_id": draft_attempt_id,
                "artifact_fingerprint": draft_fingerprint,
                "expected_state": OrchestratorState.STORY_REVIEW.value,
            }
        )

    return {
        "attempt_id": attempt_id if isinstance(attempt_id, str) else None,
        "artifact_fingerprint": (
            artifact_fingerprint if isinstance(artifact_fingerprint, str) else None
        ),
        "story_count": quality["story_count"],
        "invest_score_counts": quality["invest_score_counts"],
        "is_reusable": bool(
            summary_attempt.get("is_reusable")
            if isinstance(summary_attempt, dict)
            else False
        ),
        "quality": quality,
        "current_draft": current_draft,
        "retry": {
            "available": bool(retry_target_attempt_id),
            "target_attempt_id": retry_target_attempt_id,
        },
        "save": save_summary,
        "resolution": story_resolution_summary(runtime),
    }


def story_unabsorbed_feedback_ids(runtime: dict[str, Any]) -> list[str]:
    return story_unabsorbed_feedback_ids_for_scope(runtime, extension_metadata=None)


def story_unabsorbed_feedback_ids_for_scope(
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

    feedback_ids: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("status") != "unabsorbed":
            continue
        if not _record_matches_story_scope(item, extension_metadata):
            continue
        feedback_id = item.get("feedback_id")
        if isinstance(feedback_id, str) and feedback_id:
            feedback_ids.append(feedback_id)
    return feedback_ids


def _normalize_story_requirement(
    state: dict[str, Any],
    parent_requirement: str,
) -> str:
    normalized_parent_requirement = (
        parent_requirement.strip()
        if isinstance(parent_requirement, str)
        else parent_requirement
    )

    candidate_names: list[str] = []
    seen: set[str] = set()

    def add_candidate(name: Any) -> None:
        if not isinstance(name, str) or name in seen:
            return
        seen.add(name)
        candidate_names.append(name)

    for req in get_all_roadmap_requirements(state):
        add_candidate(req)

    interview_runtime = state.get("interview_runtime")
    if isinstance(interview_runtime, dict):
        story_runtime = interview_runtime.get("story")
        if isinstance(story_runtime, dict):
            for key in story_runtime:
                add_candidate(key)

    for key in ("story_saved", "story_outputs", "story_attempts"):
        values = state.get(key)
        if isinstance(values, dict):
            for name in values:
                add_candidate(name)

    if parent_requirement in candidate_names:
        return parent_requirement

    if isinstance(normalized_parent_requirement, str) and normalized_parent_requirement:
        normalized_parent_key = normalize_requirement_key(normalized_parent_requirement)
        for candidate in candidate_names:
            if candidate.strip() == normalized_parent_requirement:
                return candidate
            if normalize_requirement_key(candidate) == normalized_parent_key:
                return candidate

        if not candidate_names:
            return normalized_parent_requirement

    raise StoryPhaseError(
        f"Requirement '{parent_requirement}' not found in saved story state.",
        status_code=400,
    )


def _story_pending_selection_metadata(
    *,
    status: str,
    is_consumed: bool,
    stories_metadata: dict[str, Any] | None,
    normalized_requirement_key: str,
) -> dict[str, Any]:
    """Return sprint selection metadata for a Story pending requirement row."""
    req_meta = (
        (stories_metadata or {}).get(normalized_requirement_key)
        if stories_metadata is not None
        else {}
    )
    if not isinstance(req_meta, dict):
        req_meta = {}
    req_stories = req_meta.get("stories") if stories_metadata is not None else []
    story_ids = req_meta.get("story_ids") if stories_metadata is not None else []
    if not isinstance(req_stories, list):
        req_stories = []
    if not isinstance(story_ids, list):
        story_ids = []

    sprint_eligible = False
    sprint_eligibility_reason = "pending_refinement"
    if status == "Reconciled":
        sprint_eligibility_reason = "reconciled"
    elif status == "Attempted":
        sprint_eligibility_reason = "attempt_in_progress"
    elif status == "Pending":
        sprint_eligibility_reason = "pending_refinement"
    elif is_consumed:
        sprint_eligibility_reason = "already_consumed"
    elif stories_metadata is None:
        sprint_eligible = status in ("Saved", "Merged")
        sprint_eligibility_reason = "eligible" if sprint_eligible else "no_stories"
    elif not req_stories:
        sprint_eligibility_reason = "no_stories"
    elif req_meta.get("has_candidates"):
        sprint_eligible = True
        sprint_eligibility_reason = "eligible"
    elif req_meta.get("all_superseded"):
        sprint_eligibility_reason = "all_stories_superseded"
    elif req_meta.get("all_completed"):
        sprint_eligibility_reason = "all_stories_completed"
    elif req_meta.get("all_in_active_sprint"):
        sprint_eligibility_reason = "in_active_sprint"
    else:
        sprint_eligibility_reason = "no_backlog_stories"

    return {
        "sprint_eligible": sprint_eligible,
        "sprint_eligibility_reason": sprint_eligibility_reason,
        "stories": req_stories,
        "story_ids": story_ids,
    }


def _story_pending_items(  # noqa: PLR0915
    state: dict[str, Any],
    stories_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    roadmap_releases = state.get("roadmap_releases") or []
    if not isinstance(roadmap_releases, list):
        roadmap_releases = []
    extension_context = _scope_extension_context(state)

    attempts_dict = state.get("story_attempts")
    if not isinstance(attempts_dict, dict):
        attempts_dict = {}

    saved_reqs_dict = state.get("story_saved", {})
    if not isinstance(saved_reqs_dict, dict):
        saved_reqs_dict = {}

    grouped_items = []
    total_count = 0
    saved_count = 0
    merged_count = 0
    reconciled_count = 0

    for release_index, rel in enumerate(roadmap_releases):
        if not isinstance(rel, dict):
            continue
        extension_metadata = (
            _release_extension_metadata(rel, extension_context=extension_context)
            if extension_context is not None
            else None
        )
        if extension_context is not None and extension_metadata is None:
            continue

        reqs = rel.get("items") or []
        if not isinstance(reqs, list):
            reqs = []
        theme = rel.get("theme", "Milestone Context")
        reasoning = rel.get("reasoning", "")

        milestone_group = {
            "group_id": f"milestone_{release_index}",
            "theme": theme,
            "reasoning": reasoning,
            "requirements": [],
        }
        if extension_metadata is not None:
            milestone_group.update(extension_metadata)

        for req in reqs:
            if not isinstance(req, str):
                continue

            runtime = ensure_story_runtime(
                state,
                parent_requirement=req,
            )
            attempts = attempts_dict.get(req, [])
            if not isinstance(attempts, list):
                attempts = []
            reconciliation = requirement_reconciliation_for(
                state,
                parent_requirement=req,
            )

            # Get normalized key for mapping stories
            norm_key = normalize_requirement_key(req)

            # Check completed/consumed scope from state
            is_consumed = False
            scope_data = state.get("story_completion_scope")
            if isinstance(scope_data, dict):
                scope_reqs = scope_data.get("requirements") or []
                if isinstance(scope_reqs, list):
                    is_consumed = any(
                        normalize_requirement_key(str(r)) == norm_key
                        for r in scope_reqs
                    )

            if _story_saved_for_scope(
                state,
                parent_requirement=req,
                saved_reqs_dict=saved_reqs_dict,
                extension_metadata=extension_metadata,
            ):
                status = "Saved"
                saved_count += 1
            elif _story_resolution_for_scope(
                runtime,
                extension_metadata=extension_metadata,
            ):
                status = "Merged"
                merged_count += 1
            elif requirement_reconciliation_satisfies_story_requirement(
                state,
                parent_requirement=req,
            ):
                status = "Reconciled"
                reconciled_count += 1
            elif story_has_working_state_for_scope(
                runtime,
                extension_metadata=extension_metadata,
            ):
                status = "Attempted"
            else:
                status = "Pending"

            attempt_count = len(attempts)
            if extension_metadata is not None:
                attempt_count = sum(
                    1
                    for attempt in runtime.get("attempt_history") or []
                    if isinstance(attempt, dict)
                    and attempt.get("trigger") != "reset"
                    and _record_matches_story_scope(attempt, extension_metadata)
                )

            item = {
                "requirement": req,
                "status": status,
                "attempt_count": attempt_count,
                **(extension_metadata or {}),
            }
            item.update(
                _story_pending_selection_metadata(
                    status=status,
                    is_consumed=is_consumed,
                    stories_metadata=stories_metadata,
                    normalized_requirement_key=norm_key,
                )
            )
            if reconciliation is not None:
                item["reconciliation"] = {
                    "action": reconciliation.get("action"),
                    "reason": reconciliation.get("reason"),
                    "evidence_links": reconciliation.get("evidence_links") or [],
                    "changed_by": reconciliation.get("changed_by"),
                    "reconciled_at": reconciliation.get("reconciled_at"),
                    "terminal": bool(reconciliation.get("terminal")),
                }
            milestone_group["requirements"].append(item)
            total_count += 1

        grouped_items.append(milestone_group)

    return {
        "grouped_items": grouped_items,
        "total_count": total_count,
        "saved_count": saved_count,
        "reconciled_count": reconciled_count,
        "handled_count": saved_count + merged_count + reconciled_count,
    }


def _story_save_extension_metadata(
    state: dict[str, Any],
    *,
    parent_requirement: str,
) -> dict[str, Any]:
    metadata = _requirement_extension_metadata(
        state,
        parent_requirement=parent_requirement,
    )
    if metadata is None:
        return {}
    result: dict[str, Any] = {"story_origin": "scope_extension"}
    accepted_spec_version_id = _coerce_int(metadata.get("accepted_spec_version_id"))
    if accepted_spec_version_id is not None:
        result["accepted_spec_version_id"] = accepted_spec_version_id
    return result


def _story_request_payload(request_payload: Any) -> dict[str, Any]:
    return request_payload if isinstance(request_payload, dict) else {}


def _story_request_hash(request_payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(request_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def sync_story_legacy_mirrors(
    state: dict[str, Any],
    *,
    parent_requirement: str,
    runtime: dict[str, Any],
) -> None:
    story_attempts = state.get("story_attempts")
    if not isinstance(story_attempts, dict):
        story_attempts = {}
        state["story_attempts"] = story_attempts

    story_attempts[parent_requirement] = [
        {
            "created_at": attempt.get("created_at"),
            "trigger": attempt.get("trigger"),
            "input_context": (
                attempt.get("input_context")
                if isinstance(attempt.get("input_context"), dict)
                else {}
            ),
            "output_artifact": attempt.get("output_artifact"),
            "is_complete": bool(
                (
                    (attempt.get("output_artifact") or {})
                    if isinstance(attempt, dict)
                    else {}
                ).get("is_complete")
            ),
            "failure_artifact_id": attempt.get("failure_artifact_id"),
            "failure_stage": attempt.get("failure_stage"),
            "failure_summary": attempt.get("failure_summary"),
            "raw_output_preview": attempt.get("raw_output_preview"),
            "has_full_artifact": bool(attempt.get("has_full_artifact", False)),
        }
        for attempt in runtime.get("attempt_history") or []
        if isinstance(attempt, dict) and attempt.get("trigger") != "reset"
    ]

    story_outputs = state.get("story_outputs")
    if not isinstance(story_outputs, dict):
        story_outputs = {}
        state["story_outputs"] = story_outputs

    reusable = story_save_payload(runtime)
    if reusable:
        story_outputs[parent_requirement] = reusable
    else:
        story_outputs.pop(parent_requirement, None)


def _normalize_fsm_state(value: str | None) -> str:
    if isinstance(value, str):
        normalized = value.strip().upper()
        if normalized in VALID_FSM_STATES:
            return normalized
    return OrchestratorState.SETUP_REQUIRED.value


async def get_story_pending(
    *,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    stories_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = await load_state()
    return _story_pending_items(state, stories_metadata=stories_metadata)


async def generate_story_draft(  # noqa: PLR0915
    *,
    project_id: int,
    parent_requirement: str,
    user_input: str | None,
    force_feedback: bool = False,
    target_story_id: int | None = None,
    target_refinement_slot: int | None = None,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
    run_story_agent_from_state: Callable[..., Awaitable[dict[str, Any]]],
    append_feedback_entry: Callable[..., dict[str, Any]],
    set_request_projection: Callable[..., dict[str, Any]],
    append_attempt: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    promote_reusable_draft: Callable[..., dict[str, Any]],
    mark_feedback_absorbed: Callable[..., list[dict[str, Any]]],
    failure_meta: Callable[..., dict[str, Any]],
    dependency_preflight: Callable[[SaveStoriesInput], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    state = await load_state()
    try:
        workflow_state.assert_downstream_backlog_not_stale(state)
    except workflow_state.DownstreamBacklogStaleError as exc:
        raise StoryPhaseError(str(exc)) from exc

    normalized_parent_requirement = _normalize_story_requirement(
        state,
        parent_requirement,
    )
    _validate_story_generate_target_selector(
        target_story_id=target_story_id,
        target_refinement_slot=target_refinement_slot,
    )
    patch_generation = target_story_id is not None or target_refinement_slot is not None
    target_metadata: dict[str, Any] = {}
    if target_story_id is not None:
        target_metadata["target_story_id"] = target_story_id
    if target_refinement_slot is not None:
        target_metadata["target_refinement_slot"] = target_refinement_slot
    runtime = ensure_story_runtime(
        state,
        parent_requirement=normalized_parent_requirement,
    )
    extension_metadata = _requirement_extension_metadata(
        state,
        parent_requirement=normalized_parent_requirement,
    )

    has_working_state = story_has_working_state_for_scope(
        runtime,
        extension_metadata=extension_metadata,
    )
    has_prior_attempt = story_has_prior_attempt_for_scope(
        runtime,
        extension_metadata=extension_metadata,
    )
    normalized_user_input = user_input.strip() if isinstance(user_input, str) else None
    if has_working_state and not normalized_user_input:
        raise StoryPhaseError(
            "User input is required to refine an existing story.",
            status_code=400,
        )

    feedback_quality: dict[str, Any] | None = None
    if has_prior_attempt and normalized_user_input:
        latest_attempt = _latest_story_attempt(runtime)
        feedback_quality = evaluate_story_feedback_quality(
            normalized_user_input,
            parent_requirement=normalized_parent_requirement,
            force=force_feedback,
        )
        if _should_soft_gate_story_feedback(
            feedback_quality=feedback_quality,
            force_feedback=force_feedback,
            has_working_state=has_working_state,
            latest_attempt=latest_attempt,
        ):
            state["fsm_state"] = OrchestratorState.STORY_INTERVIEW.value
            save_state(state)
            return {
                "fsm_state": OrchestratorState.STORY_INTERVIEW.value,
                "parent_requirement": normalized_parent_requirement,
                "data": {
                    "generation_ran": False,
                    "feedback_quality": feedback_quality,
                    **story_interview_summary(
                        runtime,
                        extension_metadata=extension_metadata,
                    ),
                },
            }

        feedback_entry = append_feedback_entry(
            runtime,
            normalized_user_input,
            now_iso(),
            feedback_quality=feedback_quality,
        )
        if extension_metadata is not None and isinstance(feedback_entry, dict):
            feedback_entry.update(extension_metadata)

    included_feedback_ids = story_unabsorbed_feedback_ids_for_scope(
        runtime,
        extension_metadata=extension_metadata,
    )
    story_agent_kwargs: dict[str, Any] = {
        "project_id": project_id,
        "parent_requirement": normalized_parent_requirement,
        "user_input": None if included_feedback_ids else user_input,
    }
    if patch_generation:
        story_agent_kwargs.update(
            {
                "target_story_id": target_story_id,
                "target_refinement_slot": target_refinement_slot,
            }
        )
    story_result = await run_story_agent_from_state(state, **story_agent_kwargs)
    if patch_generation:
        output_artifact = story_result.get("output_artifact")
        if isinstance(output_artifact, dict):
            output_artifact = dict(output_artifact)
            output_artifact.setdefault("artifact_kind", "story_patch")
            output_artifact.update(target_metadata)
            story_result["output_artifact"] = output_artifact
    story_result = _apply_dependency_preflight_to_story_result(
        story_result,
        project_id=project_id,
        parent_requirement=normalized_parent_requirement,
        parent_rank=story_parent_rank(state, normalized_parent_requirement),
        dependency_preflight=dependency_preflight,
    )

    request_payload = _story_request_payload(story_result.get("request_payload"))
    created_at = now_iso()
    draft_projection = runtime.get("draft_projection") or {}
    if not _record_matches_story_scope(
        draft_projection if isinstance(draft_projection, dict) else None,
        extension_metadata,
    ):
        draft_projection = {}
    draft_basis_attempt_id = draft_projection.get("latest_reusable_attempt_id")
    request_projection = set_request_projection(
        runtime,
        request_snapshot_id=(
            f"request-{len(runtime.get('attempt_history') or []) + 1}"
        ),
        payload=request_payload,
        request_hash=_story_request_hash(request_payload),
        created_at=created_at,
        draft_basis_attempt_id=draft_basis_attempt_id
        if isinstance(draft_basis_attempt_id, str)
        else None,
        included_feedback_ids=included_feedback_ids,
        context_version="story-runtime.v1",
    )
    if patch_generation and isinstance(request_projection, dict):
        request_projection.update(target_metadata)
    if extension_metadata is not None and isinstance(request_projection, dict):
        request_projection.update(extension_metadata)

    attempt_id = f"attempt-{len(runtime.get('attempt_history') or []) + 1}"
    append_attempt(
        runtime,
        {
            "attempt_id": attempt_id,
            "created_at": created_at,
            "trigger": "manual_refine" if normalized_user_input else "auto_transition",
            "request_snapshot_id": request_projection.get("request_snapshot_id"),
            "draft_basis_attempt_id": request_projection.get("draft_basis_attempt_id"),
            "included_feedback_ids": list(included_feedback_ids),
            "input_context": story_result.get("input_context") or request_payload,
            "classification": story_result.get("classification"),
            "is_reusable": bool(story_result.get("is_reusable", False)),
            "retryable": story_retryable(story_result.get("classification")),
            "draft_kind": story_result.get("draft_kind"),
            "output_artifact": story_result.get("output_artifact") or {},
            **target_metadata,
            **(extension_metadata or {}),
            **failure_meta(story_result, fallback_summary=story_result.get("error")),
        },
    )

    if story_result.get("is_reusable"):
        runtime["resolution_projection"] = {}
        promote_reusable_draft(
            runtime,
            attempt_id=attempt_id,
            kind=story_result.get("draft_kind") or "incomplete_draft",
            is_complete=bool(story_result.get("is_complete", False)),
            updated_at=created_at,
        )
        if patch_generation:
            runtime["draft_projection"].update(target_metadata)
        if extension_metadata is not None:
            runtime["draft_projection"].update(extension_metadata)
        mark_feedback_absorbed(
            runtime,
            feedback_ids=included_feedback_ids,
            attempt_id=attempt_id,
        )
    _attach_story_attempt_guards(
        runtime,
        attempt_id=attempt_id,
        parent_requirement=normalized_parent_requirement,
    )

    sync_story_legacy_mirrors(
        state,
        parent_requirement=normalized_parent_requirement,
        runtime=runtime,
    )
    next_state = (
        OrchestratorState.STORY_REVIEW.value
        if story_save_payload_for_scope(
            runtime,
            extension_metadata=extension_metadata,
        )
        or story_patch_save_payload_for_scope(
            runtime,
            extension_metadata=extension_metadata,
        )
        else OrchestratorState.STORY_INTERVIEW.value
    )
    state["fsm_state"] = next_state
    save_state(state)

    return {
        "fsm_state": next_state,
        "parent_requirement": normalized_parent_requirement,
        "data": {
            "generation_ran": True,
            "feedback_quality": feedback_quality,
            "output_artifact": story_result.get("output_artifact"),
            **story_interview_summary(
                runtime,
                extension_metadata=extension_metadata,
            ),
        },
    }


async def retry_story_draft(
    *,
    project_id: int,
    parent_requirement: str,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
    run_story_agent_request: Callable[..., Awaitable[dict[str, Any]]],
    append_attempt: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    promote_reusable_draft: Callable[..., dict[str, Any]],
    mark_feedback_absorbed: Callable[..., list[dict[str, Any]]],
    failure_meta: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    state = await load_state()
    try:
        workflow_state.assert_downstream_backlog_not_stale(state)
    except workflow_state.DownstreamBacklogStaleError as exc:
        raise StoryPhaseError(str(exc)) from exc

    normalized_parent_requirement = _normalize_story_requirement(
        state,
        parent_requirement,
    )
    runtime = ensure_story_runtime(
        state,
        parent_requirement=normalized_parent_requirement,
    )
    extension_metadata = _requirement_extension_metadata(
        state,
        parent_requirement=normalized_parent_requirement,
    )

    request_projection = runtime.get("request_projection") or {}
    request_payload = request_projection.get("payload")
    if not isinstance(request_payload, dict):
        raise StoryPhaseError(
            "No replayable story request is available.",
            status_code=409,
        )
    if not story_retry_target_attempt_id(runtime):
        raise StoryPhaseError(
            "The latest story attempt is not eligible for retry.",
            status_code=409,
        )

    story_result = await run_story_agent_request(
        request_payload,
        project_id=project_id,
        parent_requirement=normalized_parent_requirement,
    )

    created_at = now_iso()
    included_feedback_ids = list(request_projection.get("included_feedback_ids") or [])
    attempt_id = f"attempt-{len(runtime.get('attempt_history') or []) + 1}"
    append_attempt(
        runtime,
        {
            "attempt_id": attempt_id,
            "created_at": created_at,
            "trigger": "retry_same_input",
            "request_snapshot_id": request_projection.get("request_snapshot_id"),
            "draft_basis_attempt_id": request_projection.get("draft_basis_attempt_id"),
            "included_feedback_ids": included_feedback_ids,
            "input_context": story_result.get("input_context") or request_payload,
            "classification": story_result.get("classification"),
            "is_reusable": bool(story_result.get("is_reusable", False)),
            "retryable": story_retryable(story_result.get("classification")),
            "draft_kind": story_result.get("draft_kind"),
            "output_artifact": story_result.get("output_artifact") or {},
            **(extension_metadata or {}),
            **failure_meta(story_result, fallback_summary=story_result.get("error")),
        },
    )

    if story_result.get("is_reusable"):
        runtime["resolution_projection"] = {}
        promote_reusable_draft(
            runtime,
            attempt_id=attempt_id,
            kind=story_result.get("draft_kind") or "incomplete_draft",
            is_complete=bool(story_result.get("is_complete", False)),
            updated_at=created_at,
        )
        if extension_metadata is not None:
            runtime["draft_projection"].update(extension_metadata)
        mark_feedback_absorbed(
            runtime,
            feedback_ids=included_feedback_ids,
            attempt_id=attempt_id,
        )
    _attach_story_attempt_guards(
        runtime,
        attempt_id=attempt_id,
        parent_requirement=normalized_parent_requirement,
    )

    sync_story_legacy_mirrors(
        state,
        parent_requirement=normalized_parent_requirement,
        runtime=runtime,
    )
    next_state = (
        OrchestratorState.STORY_REVIEW.value
        if story_save_payload_for_scope(
            runtime,
            extension_metadata=extension_metadata,
        )
        else OrchestratorState.STORY_INTERVIEW.value
    )
    state["fsm_state"] = next_state
    save_state(state)

    return {
        "fsm_state": next_state,
        "parent_requirement": normalized_parent_requirement,
        "data": {
            "output_artifact": story_result.get("output_artifact"),
            **story_interview_summary(
                runtime,
                extension_metadata=extension_metadata,
            ),
        },
    }


async def get_story_history(
    *,
    parent_requirement: str,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    state = await load_state()
    normalized_parent_requirement = _normalize_story_requirement(
        state,
        parent_requirement,
    )
    runtime = ensure_story_runtime(
        state,
        parent_requirement=normalized_parent_requirement,
    )
    extension_metadata = _requirement_extension_metadata(
        state,
        parent_requirement=normalized_parent_requirement,
    )
    attempt_history = runtime.get("attempt_history") or []
    return {
        "parent_requirement": normalized_parent_requirement,
        "data": {
            "items": attempt_history,
            "count": len(attempt_history),
            **story_interview_summary(
                runtime,
                extension_metadata=extension_metadata,
            ),
        },
    }


async def save_story_draft(
    *,
    project_id: int,
    parent_requirement: str,
    attempt_id: str | None,
    expected_artifact_fingerprint: str | None,
    expected_state: str | None,
    idempotency_key: str | None,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    hydrate_context: Callable[[str, int], Awaitable[Any]],
    build_tool_context: Callable[[Any], Any],
    save_stories_tool: Callable[[Any, Any], dict[str, Any]],
) -> dict[str, Any]:
    state = await load_state()
    normalized_parent_requirement = _normalize_story_requirement(
        state,
        parent_requirement,
    )
    runtime = ensure_story_runtime(
        state,
        parent_requirement=normalized_parent_requirement,
    )
    extension_metadata = _requirement_extension_metadata(
        state,
        parent_requirement=normalized_parent_requirement,
    )
    _validate_story_save_required_guards(
        attempt_id=attempt_id,
        expected_artifact_fingerprint=expected_artifact_fingerprint,
        expected_state=expected_state,
        idempotency_key=idempotency_key,
    )
    attempt_id = cast("str", attempt_id)
    expected_artifact_fingerprint = cast("str", expected_artifact_fingerprint)
    idempotency_key = cast("str", idempotency_key)

    replay_payload = _story_save_replay_payload(
        state,
        idempotency_key=idempotency_key,
        parent_requirement=normalized_parent_requirement,
        attempt_id=attempt_id,
        expected_artifact_fingerprint=expected_artifact_fingerprint,
    )
    if replay_payload is not None:
        return replay_payload

    current_state = _normalize_fsm_state(state.get("fsm_state"))
    if current_state != OrchestratorState.STORY_REVIEW.value:
        raise StoryPhaseError(
            "story save requires FSM state STORY_REVIEW",
            status_code=409,
        )

    draft_projection = runtime.get("draft_projection") or {}
    current_attempt_id = draft_projection.get("latest_reusable_attempt_id")
    if current_attempt_id != attempt_id:
        raise StoryPhaseError(
            "story save attempt mismatch; refresh history and review the current draft",
            status_code=409,
        )

    current_fingerprint = draft_projection.get("artifact_fingerprint")
    if current_fingerprint != expected_artifact_fingerprint:
        raise StoryPhaseError(
            "story save artifact fingerprint mismatch; refresh history and review the current draft",  # noqa: E501
            status_code=409,
        )
    _validate_story_save_current_attempt_artifact(
        runtime,
        parent_requirement=normalized_parent_requirement,
        attempt_id=attempt_id,
        expected_artifact_fingerprint=expected_artifact_fingerprint,
    )
    current_attempt = _find_attempt_by_id(runtime, attempt_id)
    if (
        isinstance(current_attempt, dict)
        and current_attempt.get("draft_kind") == "story_patch"
    ) or draft_projection.get("kind") == "story_patch":
        raise StoryPhaseError(
            "story save requires a complete draft; use story save-patch for story_patch drafts",  # noqa: E501
            status_code=409,
        )

    assessment = story_save_payload_for_scope(
        runtime,
        extension_metadata=extension_metadata,
    )

    if not assessment:
        raise StoryPhaseError(
            f"No story draft available for '{normalized_parent_requirement}'",
            status_code=409,
        )

    stories = assessment.get("user_stories")
    if not isinstance(stories, list) or len(stories) == 0:
        raise StoryPhaseError("Stories are empty", status_code=409)

    context = await hydrate_context(str(project_id), project_id)
    result = save_stories_tool(
        SaveStoriesInput(
            product_id=project_id,
            parent_requirement=normalized_parent_requirement,
            parent_rank=story_parent_rank(state, normalized_parent_requirement),
            idempotency_key=idempotency_key,
            stories=stories,
            **_story_save_extension_metadata(
                state,
                parent_requirement=normalized_parent_requirement,
            ),
        ),
        build_tool_context(context),
    )

    if not result.get("success"):
        error_code = result.get("error_code")
        if error_code == "STORY_REPLACEMENT_UNSAFE":
            message = result.get("error", "Story replacement is unsafe")
            raise StoryPhaseError(
                f"{error_code}: {message}",
                status_code=409,
            )
        raise StoryPhaseError(
            result.get("error", "Failed to save stories"),
            status_code=500,
        )

    _mark_story_saved(
        context.state,
        parent_requirement=normalized_parent_requirement,
    )
    context.state["fsm_state"] = OrchestratorState.STORY_PERSISTENCE.value
    sync_story_legacy_mirrors(
        context.state,
        parent_requirement=normalized_parent_requirement,
        runtime=runtime,
    )

    idempotency_registry = context.state.get("story_save_idempotency")
    if not isinstance(idempotency_registry, dict):
        idempotency_registry = {}
        context.state["story_save_idempotency"] = idempotency_registry

    payload = {
        "parent_requirement": normalized_parent_requirement,
        "attempt_id": attempt_id,
        "artifact_fingerprint": expected_artifact_fingerprint,
        "fsm_state": OrchestratorState.STORY_PERSISTENCE.value,
        "data": {
            "save_result": result,
        },
    }
    idempotency_registry[idempotency_key] = payload
    save_state(context.state)
    return payload


async def save_story_patch(
    *,
    project_id: int,
    parent_requirement: str,
    attempt_id: str | None,
    expected_artifact_fingerprint: str | None,
    expected_state: str | None,
    idempotency_key: str | None,
    target_story_id: int | None,
    target_refinement_slot: int | None,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    hydrate_context: Callable[[str, int], Awaitable[Any]],
    build_tool_context: Callable[[Any], Any],
    save_story_patch_tool: Callable[[Any, Any], dict[str, Any]],
    resolve_target_refinement_slot: Callable[[int, str, int], int | None] | None,
) -> dict[str, Any]:
    state = await load_state()
    normalized_parent_requirement = _normalize_story_requirement(
        state,
        parent_requirement,
    )
    _validate_story_patch_target_selector(
        target_story_id=target_story_id,
        target_refinement_slot=target_refinement_slot,
    )
    runtime = ensure_story_runtime(
        state,
        parent_requirement=normalized_parent_requirement,
    )
    _validate_story_save_required_guards(
        attempt_id=attempt_id,
        expected_artifact_fingerprint=expected_artifact_fingerprint,
        expected_state=expected_state,
        idempotency_key=idempotency_key,
    )
    attempt_id = cast("str", attempt_id)
    expected_artifact_fingerprint = cast("str", expected_artifact_fingerprint)
    idempotency_key = cast("str", idempotency_key)
    target_refinement_slot = _resolve_story_patch_target_slot(
        project_id=project_id,
        parent_requirement=normalized_parent_requirement,
        target_story_id=target_story_id,
        target_refinement_slot=target_refinement_slot,
        resolve_target_refinement_slot=resolve_target_refinement_slot,
    )

    replay_payload = _story_save_replay_payload(
        state,
        idempotency_key=idempotency_key,
        parent_requirement=normalized_parent_requirement,
        attempt_id=attempt_id,
        expected_artifact_fingerprint=expected_artifact_fingerprint,
        operation="story_patch",
        target_story_id=target_story_id,
        target_refinement_slot=target_refinement_slot,
    )
    if replay_payload is not None:
        return replay_payload

    current_state = _normalize_fsm_state(state.get("fsm_state"))
    if current_state != OrchestratorState.STORY_REVIEW.value:
        raise StoryPhaseError(
            "story save requires FSM state STORY_REVIEW",
            status_code=409,
        )

    draft_projection = runtime.get("draft_projection") or {}
    current_attempt_id = draft_projection.get("latest_reusable_attempt_id")
    if current_attempt_id != attempt_id:
        raise StoryPhaseError(
            "story save attempt mismatch; refresh history and review the current draft",
            status_code=409,
        )

    current_fingerprint = draft_projection.get("artifact_fingerprint")
    if current_fingerprint != expected_artifact_fingerprint:
        raise StoryPhaseError(
            "story save artifact fingerprint mismatch; refresh history and review the current draft",  # noqa: E501
            status_code=409,
        )
    _validate_story_save_current_attempt_artifact(
        runtime,
        parent_requirement=normalized_parent_requirement,
        attempt_id=attempt_id,
        expected_artifact_fingerprint=expected_artifact_fingerprint,
    )

    patch_artifact = _story_patch_artifact_for_save(
        runtime,
        attempt_id=attempt_id,
        target_story_id=target_story_id,
        target_refinement_slot=target_refinement_slot,
    )
    target_story = cast("dict[str, Any]", patch_artifact["story"])

    context = await hydrate_context(str(project_id), project_id)
    result = save_story_patch_tool(
        SaveStoryPatchInput(
            product_id=project_id,
            parent_requirement=normalized_parent_requirement,
            parent_rank=story_parent_rank(state, normalized_parent_requirement),
            idempotency_key=idempotency_key,
            target_story_id=target_story_id,
            target_refinement_slot=(
                target_refinement_slot if target_story_id is None else None
            ),
            story=target_story,
            **_story_save_extension_metadata(
                state,
                parent_requirement=normalized_parent_requirement,
            ),
        ),
        build_tool_context(context),
    )
    if not result.get("success"):
        error_code = result.get("error_code")
        if error_code in {"STORY_REPLACEMENT_UNSAFE", "STORY_PATCH_TARGET_MISMATCH"}:
            message = result.get("error", "Story patch is unsafe")
            raise StoryPhaseError(
                f"{error_code}: {message}",
                status_code=409,
            )
        raise StoryPhaseError(
            result.get("error", "Failed to save story patch"),
            status_code=500,
        )

    _mark_story_saved(
        context.state,
        parent_requirement=normalized_parent_requirement,
    )
    context.state["fsm_state"] = OrchestratorState.STORY_PERSISTENCE.value
    merged_output = _story_patch_merged_output(
        context.state,
        parent_requirement=normalized_parent_requirement,
        patch_artifact=patch_artifact,
        patch_story=target_story,
        target_refinement_slot=target_refinement_slot,
    )
    sync_story_legacy_mirrors(
        context.state,
        parent_requirement=normalized_parent_requirement,
        runtime=runtime,
    )
    story_outputs = context.state.get("story_outputs")
    if not isinstance(story_outputs, dict):
        story_outputs = {}
        context.state["story_outputs"] = story_outputs
    story_outputs[normalized_parent_requirement] = merged_output

    idempotency_registry = context.state.get("story_save_idempotency")
    if not isinstance(idempotency_registry, dict):
        idempotency_registry = {}
        context.state["story_save_idempotency"] = idempotency_registry

    payload = {
        "operation": "story_patch",
        "parent_requirement": normalized_parent_requirement,
        "attempt_id": attempt_id,
        "artifact_fingerprint": expected_artifact_fingerprint,
        "target_story_id": target_story_id,
        "target_refinement_slot": target_refinement_slot,
        "fsm_state": OrchestratorState.STORY_PERSISTENCE.value,
        "data": {
            "save_result": result,
        },
    }
    idempotency_registry[idempotency_key] = payload
    save_state(context.state)
    return payload


async def merge_story_resolution(
    *,
    parent_requirement: str,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
) -> dict[str, Any]:
    state = await load_state()
    normalized_parent_requirement = _normalize_story_requirement(
        state,
        parent_requirement,
    )
    runtime = ensure_story_runtime(
        state,
        parent_requirement=normalized_parent_requirement,
    )

    recommendation = story_merge_recommendation_payload(runtime)
    if not recommendation:
        raise StoryPhaseError(
            "No merge recommendation is available for this requirement.",
            status_code=409,
        )

    extension_metadata = _requirement_extension_metadata(
        state,
        parent_requirement=normalized_parent_requirement,
    )
    runtime["resolution_projection"] = {
        "status": "merged",
        "owner_requirement": recommendation["owner_requirement"],
        "reason": recommendation["reason"],
        "acceptance_criteria_to_move": recommendation["acceptance_criteria_to_move"],
        "resolved_at": now_iso(),
        **(extension_metadata or {}),
    }

    sync_story_legacy_mirrors(
        state,
        parent_requirement=normalized_parent_requirement,
        runtime=runtime,
    )
    save_state(state)

    return {
        "parent_requirement": normalized_parent_requirement,
        "data": {
            "resolution": story_resolution_summary(runtime),
        },
    }


async def delete_story_requirement(
    *,
    parent_requirement: str,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
    delete_requirement_stories: Callable[[str], int],
    reset_subject_working_set: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    state = await load_state()
    normalized_parent_requirement = _normalize_story_requirement(
        state,
        parent_requirement,
    )
    normalized_repository_requirement = normalize_requirement_key(
        normalized_parent_requirement
    )
    deleted_count = delete_requirement_stories(normalized_repository_requirement)
    runtime = ensure_story_runtime(
        state,
        parent_requirement=normalized_parent_requirement,
    )
    reset_subject_working_set(
        runtime,
        created_at=now_iso(),
        summary="Stories deleted and state reset by user.",
    )

    story_saved = state.get("story_saved")
    if isinstance(story_saved, dict):
        story_saved.pop(normalized_parent_requirement, None)
    story_saved_metadata = state.get("story_saved_metadata")
    if isinstance(story_saved_metadata, dict):
        story_saved_metadata.pop(normalized_parent_requirement, None)

    sync_story_legacy_mirrors(
        state,
        parent_requirement=normalized_parent_requirement,
        runtime=runtime,
    )
    save_state(state)

    return {
        "parent_requirement": normalized_parent_requirement,
        "data": {
            "deleted_count": deleted_count,
            "message": "Stories deleted successfully",
        },
    }


async def reopen_story_requirement(
    *,
    parent_requirement: str,
    expected_state: str | None,
    idempotency_key: str | None,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
    assert_reopen_safe: Callable[[str], None],
    reset_subject_working_set: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Reopen one saved Story requirement before Sprint work exists."""
    if expected_state != OrchestratorState.SPRINT_SETUP.value:
        raise StoryPhaseError(
            "story reopen requires --expected-state SPRINT_SETUP",
            status_code=400,
        )
    if idempotency_key is None or not idempotency_key.strip():
        raise StoryPhaseError(
            "story reopen requires --idempotency-key",
            status_code=400,
        )

    state = await load_state()
    normalized_idempotency_key = idempotency_key.strip()
    idempotency_registry = state.get("story_reopen_idempotency")
    if isinstance(idempotency_registry, dict):
        existing = idempotency_registry.get(normalized_idempotency_key)
        if isinstance(existing, dict):
            normalized_parent_requirement = _normalize_story_requirement(
                state,
                parent_requirement,
            )
            _ensure_idempotency_identity_matches(
                existing_identity={
                    "parent_requirement_key": requirement_reconciliation_key(
                        str(existing.get("parent_requirement", ""))
                    )
                },
                current_identity={
                    "parent_requirement_key": requirement_reconciliation_key(
                        normalized_parent_requirement
                    )
                },
            )
            return dict(existing)

    current_state = _normalize_fsm_state(state.get("fsm_state"))
    if current_state != OrchestratorState.SPRINT_SETUP.value:
        raise StoryPhaseError(
            "Story correction can reopen only from SPRINT_SETUP.",
            status_code=409,
        )

    normalized_parent_requirement = _normalize_story_requirement(
        state,
        parent_requirement,
    )

    story_saved = state.get("story_saved")
    if not (
        isinstance(story_saved, dict) and story_saved.get(normalized_parent_requirement)
    ):
        raise StoryPhaseError(
            "Story correction can reopen only saved Story requirements.",
            status_code=409,
        )

    assert_reopen_safe(normalized_parent_requirement)

    if isinstance(story_saved, dict):
        story_saved.pop(normalized_parent_requirement, None)
    story_saved_metadata = state.get("story_saved_metadata")
    if isinstance(story_saved_metadata, dict):
        story_saved_metadata.pop(normalized_parent_requirement, None)

    story_outputs = state.get("story_outputs")
    if isinstance(story_outputs, dict):
        story_outputs.pop(normalized_parent_requirement, None)

    reopened_at = now_iso()
    runtime = ensure_story_runtime(
        state,
        parent_requirement=normalized_parent_requirement,
    )
    reset_subject_working_set(
        runtime,
        created_at=reopened_at,
        summary="Story reopened for correction before Sprint planning.",
    )
    sync_story_legacy_mirrors(
        state,
        parent_requirement=normalized_parent_requirement,
        runtime=runtime,
    )

    state["fsm_state"] = OrchestratorState.STORY_INTERVIEW.value
    state["fsm_state_entered_at"] = reopened_at
    payload = {
        "parent_requirement": normalized_parent_requirement,
        "fsm_state": OrchestratorState.STORY_INTERVIEW.value,
        "idempotency_key": normalized_idempotency_key,
    }
    if not isinstance(idempotency_registry, dict):
        idempotency_registry = {}
        state["story_reopen_idempotency"] = idempotency_registry
    idempotency_registry[normalized_idempotency_key] = payload
    save_state(state)
    return payload


async def repair_story_readiness(
    *,
    project_id: int,
    expected_state: str | None,
    idempotency_key: str | None,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    repair_rows: Callable[[dict[str, Any]], dict[str, Any]],
    assert_repair_safe: Callable[[int], None],
) -> dict[str, Any]:
    """Backfill Story planning metadata before Sprint work starts."""
    if expected_state != OrchestratorState.SPRINT_SETUP.value:
        raise StoryPhaseError(
            "story repair-readiness requires --expected-state SPRINT_SETUP",
            status_code=400,
        )
    if idempotency_key is None or not idempotency_key.strip():
        raise StoryPhaseError(
            "story repair-readiness requires --idempotency-key",
            status_code=400,
        )

    state = await load_state()
    normalized_idempotency_key = idempotency_key.strip()
    idempotency_registry = state.get("story_readiness_repair_idempotency")
    if isinstance(idempotency_registry, dict):
        existing = idempotency_registry.get(normalized_idempotency_key)
        if isinstance(existing, dict):
            return dict(existing)

    current_state = _normalize_fsm_state(state.get("fsm_state"))
    if current_state != OrchestratorState.SPRINT_SETUP.value:
        raise StoryPhaseError(
            "Story readiness repair can run only from SPRINT_SETUP.",
            status_code=409,
        )

    assert_repair_safe(project_id)
    repair_result = repair_rows(
        {
            "project_id": project_id,
            "items": _story_readiness_repair_items(state),
        }
    )
    payload = {
        "project_id": project_id,
        "fsm_state": OrchestratorState.SPRINT_SETUP.value,
        "idempotency_key": normalized_idempotency_key,
        "repair_result": repair_result,
    }
    if not isinstance(idempotency_registry, dict):
        idempotency_registry = {}
        state["story_readiness_repair_idempotency"] = idempotency_registry
    idempotency_registry[normalized_idempotency_key] = payload
    save_state(state)
    return payload


async def repair_story_completion_scope(
    *,
    project_id: int,
    expected_state: str | None,
    expected_scope_id: str | None,
    idempotency_key: str | None,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
) -> dict[str, Any]:
    """Clear a stale Story completion scope without mutating Story rows."""
    if expected_state != OrchestratorState.SPRINT_SETUP.value:
        raise StoryPhaseError(
            "story repair-completion-scope requires --expected-state SPRINT_SETUP",
            status_code=400,
        )
    if expected_scope_id is None or not expected_scope_id.strip():
        raise StoryPhaseError(
            "story repair-completion-scope requires --expected-scope-id",
            status_code=400,
        )
    if idempotency_key is None or not idempotency_key.strip():
        raise StoryPhaseError(
            "story repair-completion-scope requires --idempotency-key",
            status_code=400,
        )

    state = await load_state()
    normalized_scope_id = expected_scope_id.strip()
    normalized_idempotency_key = idempotency_key.strip()
    request_identity = {"expected_scope_id": normalized_scope_id}
    idempotency_registry = state.get(_STORY_COMPLETION_SCOPE_REPAIR_IDEMPOTENCY_KEY)
    if isinstance(idempotency_registry, dict):
        existing = idempotency_registry.get(normalized_idempotency_key)
        if isinstance(existing, dict):
            cleared_scope = existing.get("cleared_story_completion_scope")
            existing_scope_id = (
                str(cleared_scope.get("scope_id")).strip()
                if isinstance(cleared_scope, dict)
                else None
            )
            _ensure_idempotency_identity_matches(
                existing_identity={"expected_scope_id": existing_scope_id},
                current_identity=request_identity,
            )
            return dict(existing)

    current_state = _normalize_fsm_state(state.get("fsm_state"))
    if current_state != OrchestratorState.SPRINT_SETUP.value:
        raise StoryPhaseError(
            "Story completion scope repair can run only from SPRINT_SETUP.",
            status_code=409,
        )

    scope_payload = state.get("story_completion_scope")
    if not isinstance(scope_payload, dict):
        raise StoryPhaseError(
            "Story completion scope repair requires an active Story completion scope.",
            status_code=409,
        )
    current_scope_id = str(scope_payload.get("scope_id") or "").strip()
    if current_scope_id != normalized_scope_id:
        raise StoryPhaseError(
            "Story completion scope repair expected scope does not match "
            "current scope.",
            status_code=409,
        )

    cleared_at = now_iso()
    cleared_scope = dict(scope_payload)
    state["story_completion_scope"] = None
    payload: dict[str, Any] = {
        "project_id": project_id,
        "fsm_state": OrchestratorState.SPRINT_SETUP.value,
        "cleared_story_completion_scope": cleared_scope,
        "cleared_at": cleared_at,
        "idempotency_key": normalized_idempotency_key,
    }
    if not isinstance(idempotency_registry, dict):
        idempotency_registry = {}
        state[_STORY_COMPLETION_SCOPE_REPAIR_IDEMPOTENCY_KEY] = idempotency_registry
    idempotency_registry[normalized_idempotency_key] = payload
    save_state(state)
    return payload


async def reconcile_requirement(
    *,
    project_id: int,
    requirement: str,
    action: str,
    reason: str,
    idempotency_key: str,
    changed_by: str = "cli-agent",
    evidence_links: list[str] | None = None,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
) -> dict[str, Any]:
    """Record a requirement-level reconciliation decision."""
    normalized_requirement = requirement.strip()
    normalized_action = action.strip().lower()
    normalized_reason = reason.strip()
    normalized_idempotency_key = idempotency_key.strip()
    if not normalized_requirement:
        raise StoryPhaseError(
            "requirement reconcile requires --requirement",
            status_code=400,
        )
    if normalized_action not in REQUIREMENT_RECONCILIATION_ACTIONS:
        raise StoryPhaseError(
            "Unsupported requirement reconciliation action.",
            status_code=400,
        )
    if not normalized_reason:
        raise StoryPhaseError(
            "requirement reconcile requires --reason",
            status_code=400,
        )
    if not normalized_idempotency_key:
        raise StoryPhaseError(
            "requirement reconcile requires --idempotency-key",
            status_code=400,
        )

    state = await load_state()
    current_state = _normalize_fsm_state(state.get("fsm_state"))
    if current_state not in REQUIREMENT_RECONCILIATION_ALLOWED_STATES:
        raise StoryPhaseError(
            "requirement reconcile is only available during Story/Sprint "
            "planning states.",
            status_code=409,
        )

    matches = _roadmap_requirement_matches(state, requirement=normalized_requirement)
    if not matches:
        raise StoryPhaseError(
            "Requirement reconciliation target was not found in saved roadmap "
            "releases.",
            status_code=400,
        )
    if len(matches) > 1:
        raise StoryPhaseError(
            "Requirement reconciliation target is ambiguous in saved roadmap "
            "releases.",
            status_code=400,
        )

    matched_requirement = matches[0]
    request_identity = requirement_reconciliation_request_identity(
        requirement=matched_requirement,
        action=normalized_action,
        reason=normalized_reason,
        changed_by=changed_by,
        evidence_links=evidence_links,
    )
    idempotency_registry = state.get(REQUIREMENT_RECONCILIATION_IDEMPOTENCY_KEY)
    if isinstance(idempotency_registry, dict):
        existing = idempotency_registry.get(normalized_idempotency_key)
        if isinstance(existing, dict):
            _ensure_idempotency_identity_matches(
                existing_identity=requirement_reconciliation_payload_identity(existing),
                current_identity=request_identity,
            )
            return dict(existing)

    payload: dict[str, Any] = {
        "schema_version": REQUIREMENT_RECONCILIATION_SCHEMA_VERSION,
        "project_id": project_id,
        "requirement": matched_requirement,
        "action": normalized_action,
        "reason": normalized_reason,
        "evidence_links": request_identity["evidence_links"],
        "changed_by": request_identity["changed_by"],
        "reconciled_at": now_iso(),
        "idempotency_key": normalized_idempotency_key,
        "terminal": (
            normalized_action in REQUIREMENT_RECONCILIATION_SATISFIED_ACTIONS
        ),
    }

    reconciliations = state.get(REQUIREMENT_RECONCILIATION_STATE_KEY)
    if not isinstance(reconciliations, dict):
        reconciliations = {}
        state[REQUIREMENT_RECONCILIATION_STATE_KEY] = reconciliations
    reconciliations[requirement_reconciliation_key(matched_requirement)] = payload

    history = state.get(REQUIREMENT_RECONCILIATION_HISTORY_KEY)
    if not isinstance(history, list):
        history = []
        state[REQUIREMENT_RECONCILIATION_HISTORY_KEY] = history
    history.append(payload)

    if not isinstance(idempotency_registry, dict):
        idempotency_registry = {}
        state[REQUIREMENT_RECONCILIATION_IDEMPOTENCY_KEY] = idempotency_registry
    idempotency_registry[normalized_idempotency_key] = payload
    save_state(state)
    return payload


async def complete_story_phase(  # noqa: PLR0915
    *,
    expected_state: str | None,
    idempotency_key: str | None,
    scope: str | None = None,
    scope_id: str | None = None,
    parent_requirements: list[str] | None = None,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
) -> dict[str, Any]:
    allowed_expected_states = {
        OrchestratorState.STORY_PERSISTENCE.value,
        OrchestratorState.STORY_INTERVIEW.value,
    }
    if expected_state not in allowed_expected_states:
        raise StoryPhaseError(
            "story complete requires --expected-state STORY_PERSISTENCE "
            "or STORY_INTERVIEW",
            status_code=400,
        )
    if idempotency_key is None or not idempotency_key.strip():
        raise StoryPhaseError(
            "story complete requires --idempotency-key",
            status_code=400,
        )

    state = await load_state()
    normalized_idempotency_key = idempotency_key.strip()

    idempotency_registry = state.get("story_complete_idempotency")
    if isinstance(idempotency_registry, dict):
        existing = idempotency_registry.get(normalized_idempotency_key)
        if isinstance(existing, dict):
            _, replay_scope_payload = _story_completion_scope_requirements(
                state,
                scope=scope,
                scope_id=scope_id,
                parent_requirements=parent_requirements,
            )
            existing_scope_payload = existing.get("story_completion_scope")
            existing_scope = (
                existing_scope_payload
                if isinstance(existing_scope_payload, dict)
                else None
            )
            existing_scope_identity = _story_completion_scope_identity(
                existing_scope
            )
            _ensure_idempotency_identity_matches(
                existing_identity=existing_scope_identity,
                current_identity=_story_completion_scope_identity(replay_scope_payload),
            )
            return dict(existing)

    current_state = _normalize_fsm_state(state.get("fsm_state"))
    if current_state != expected_state:
        raise StoryPhaseError(
            f"Story phase cannot complete unless current state is {expected_state}.",
            status_code=409,
        )

    req_names, scope_payload = _story_completion_scope_requirements(
        state,
        scope=scope,
        scope_id=scope_id,
        parent_requirements=parent_requirements,
    )
    saved_reqs_dict = state.get("story_saved", {})
    if not isinstance(saved_reqs_dict, dict):
        saved_reqs_dict = {}

    saved_count = 0
    merged_count = 0
    reconciled_count = 0
    extension_metadata = (
        scope_payload
        if isinstance(scope_payload, dict)
        and scope_payload.get("extension_scope") is True
        else None
    )
    for requirement in req_names:
        if _story_saved_for_scope(
            state,
            parent_requirement=requirement,
            saved_reqs_dict=saved_reqs_dict,
            extension_metadata=extension_metadata,
        ):
            saved_count += 1
            continue

        runtime = existing_story_runtime(state, parent_requirement=requirement)
        if runtime is not None and _story_resolution_for_scope(
            runtime,
            extension_metadata=extension_metadata,
        ):
            merged_count += 1
            continue

        if requirement_reconciliation_satisfies_story_requirement(
            state,
            parent_requirement=requirement,
        ):
            reconciled_count += 1

    total_count = len(req_names)
    covered_count = saved_count + merged_count + reconciled_count
    if covered_count != total_count:
        scope_prefix = (
            f" for {scope_payload['scope_id']}" if scope_payload is not None else ""
        )
        raise StoryPhaseError(
            f"Story phase cannot complete{scope_prefix}: "
            f"{covered_count} of {total_count} roadmap requirements are saved, "
            "merged, or terminal-reconciled.",
            status_code=409,
        )

    completed_at = now_iso()
    state["fsm_state"] = OrchestratorState.SPRINT_SETUP.value
    state["fsm_state_entered_at"] = completed_at
    state["story_phase_completed_at"] = completed_at
    if scope_payload is not None:
        scope_payload = {**scope_payload, "completed_at": completed_at}
        state["story_completion_scope"] = scope_payload
    else:
        state["story_completion_scope"] = None

    coverage: dict[str, int] = {
        "saved": saved_count,
        "merged": merged_count,
        "total": total_count,
    }
    if reconciled_count:
        coverage["reconciled"] = reconciled_count
    payload: dict[str, Any] = {
        "fsm_state": OrchestratorState.SPRINT_SETUP.value,
        "coverage": coverage,
        "idempotency_key": normalized_idempotency_key,
    }
    if scope_payload is not None:
        payload["story_completion_scope"] = scope_payload
    if not isinstance(idempotency_registry, dict):
        idempotency_registry = {}
        state["story_complete_idempotency"] = idempotency_registry
    idempotency_registry[normalized_idempotency_key] = payload
    save_state(state)
    return payload
