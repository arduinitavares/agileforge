"""Story phase application service helpers."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, cast

from orchestrator_agent.agent_tools.story_linkage import (
    normalize_requirement_key,
)
from orchestrator_agent.agent_tools.user_story_writer_tool.tools import (
    SaveStoriesInput,
)
from orchestrator_agent.fsm.states import OrchestratorState
from services.agent_workbench.fingerprints import canonical_hash
from services.interview_runtime import hydrate_story_runtime_from_legacy
from services.phases import workflow_state
from services.story_feedback_quality import evaluate_story_feedback_quality

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


def _roadmap_milestone_requirements(
    state: dict[str, Any],
    *,
    scope_id: str,
) -> list[str] | None:
    """Return requirement names for a milestone scope, or None when absent."""
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
            return []
        return [item for item in items if isinstance(item, str)]
    return None


def _story_completion_scope_requirements(
    state: dict[str, Any],
    *,
    scope: str | None,
    scope_id: str | None,
) -> tuple[list[str], dict[str, Any] | None]:
    """Resolve Story completion requirements for full or scoped completion."""
    normalized_scope = scope.strip() if isinstance(scope, str) else None
    normalized_scope_id = scope_id.strip() if isinstance(scope_id, str) else None
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

    requirements = _roadmap_milestone_requirements(
        state,
        scope_id=normalized_scope_id,
    )
    if requirements is None:
        raise StoryPhaseError(
            "Story completion scope "
            f"{normalized_scope_id} does not match any roadmap milestone.",
            status_code=400,
        )

    return requirements, {
        "schema_version": "agileforge.story_completion_scope.v1",
        "scope": normalized_scope,
        "scope_id": normalized_scope_id,
        "requirements": requirements,
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


def _story_current_draft_artifact(
    runtime: dict[str, Any],
) -> dict[str, Any] | None:
    draft_projection = runtime.get("draft_projection") or {}
    attempt_id = draft_projection.get("latest_reusable_attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        return None

    attempt = _find_attempt_by_id(runtime, attempt_id)
    artifact = (attempt or {}).get("output_artifact")
    if not isinstance(artifact, dict):
        return None

    stories = artifact.get("user_stories")
    if not isinstance(stories, list) or len(stories) == 0:
        return None
    return artifact


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
    draft_projection = runtime.get("draft_projection") or {}
    if draft_projection.get("kind") != "complete_draft":
        return None

    artifact = _story_current_draft_artifact(runtime)
    if not isinstance(artifact, dict):
        return None
    if _story_merge_recommendation_from_artifact(artifact):
        return None
    if not artifact.get("is_complete"):
        return None
    if not story_quality_saveable(artifact):
        return None
    return artifact


def _story_counts_from_artifact(artifact: dict[str, Any]) -> tuple[int, dict[str, int]]:
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


def _quality_findings_from_artifact(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    quality = artifact.get("quality")
    findings = (
        quality.get("quality_findings")
        if isinstance(quality, dict)
        else artifact.get("quality_findings")
    )
    if not isinstance(findings, list):
        return []
    return [finding for finding in findings if isinstance(finding, dict)]


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
    if story_current_resolution(runtime):
        return True

    draft_projection = runtime.get("draft_projection") or {}
    if draft_projection:
        return True

    request_projection = runtime.get("request_projection") or {}
    if isinstance(request_projection.get("payload"), dict):
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
        for item in items
    )


def story_has_prior_attempt(runtime: dict[str, Any]) -> bool:
    attempts = runtime.get("attempt_history") or []
    if not isinstance(attempts, list):
        return False

    return any(
        isinstance(attempt, dict) and attempt.get("trigger") != "reset"
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


def story_interview_summary(runtime: dict[str, Any]) -> dict[str, Any]:
    draft_projection = runtime.get("draft_projection") or {}
    retry_target_attempt_id = story_retry_target_attempt_id(runtime)
    save_payload = story_save_payload(runtime)
    latest_attempt = _latest_story_attempt(runtime)
    latest_artifact = _attempt_output_artifact(latest_attempt)

    current_draft = None
    if draft_projection:
        current_draft = {
            "attempt_id": draft_projection.get("latest_reusable_attempt_id"),
            "kind": draft_projection.get("kind"),
            "is_complete": bool(draft_projection.get("is_complete", False)),
        }
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


def _story_pending_items(state: dict[str, Any]) -> dict[str, Any]:
    roadmap_releases = state.get("roadmap_releases") or []
    if not isinstance(roadmap_releases, list):
        roadmap_releases = []

    attempts_dict = state.get("story_attempts")
    if not isinstance(attempts_dict, dict):
        attempts_dict = {}

    saved_reqs_dict = state.get("story_saved", {})
    if not isinstance(saved_reqs_dict, dict):
        saved_reqs_dict = {}

    grouped_items = []
    total_count = 0
    saved_count = 0

    for release_index, rel in enumerate(roadmap_releases):
        if not isinstance(rel, dict):
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

            if saved_reqs_dict.get(req):
                status = "Saved"
                saved_count += 1
            elif story_current_resolution(runtime):
                status = "Merged"
            elif story_has_working_state(runtime):
                status = "Attempted"
            else:
                status = "Pending"

            milestone_group["requirements"].append(
                {
                    "requirement": req,
                    "status": status,
                    "attempt_count": len(attempts),
                }
            )
            total_count += 1

        grouped_items.append(milestone_group)

    return {
        "grouped_items": grouped_items,
        "total_count": total_count,
        "saved_count": saved_count,
    }


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
) -> dict[str, Any]:
    state = await load_state()
    return _story_pending_items(state)


async def generate_story_draft(
    *,
    project_id: int,
    parent_requirement: str,
    user_input: str | None,
    force_feedback: bool = False,
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

    has_working_state = story_has_working_state(runtime)
    has_prior_attempt = story_has_prior_attempt(runtime)
    normalized_user_input = user_input.strip() if isinstance(user_input, str) else None
    if has_working_state and not normalized_user_input:
        raise StoryPhaseError(
            "User input is required to refine an existing story.",
            status_code=400,
        )

    feedback_quality: dict[str, Any] | None = None
    if has_prior_attempt and normalized_user_input:
        feedback_quality = evaluate_story_feedback_quality(
            normalized_user_input,
            parent_requirement=normalized_parent_requirement,
            force=force_feedback,
        )
        if feedback_quality["needs_revision"] and not force_feedback:
            state["fsm_state"] = OrchestratorState.STORY_INTERVIEW.value
            save_state(state)
            return {
                "fsm_state": OrchestratorState.STORY_INTERVIEW.value,
                "parent_requirement": normalized_parent_requirement,
                "data": {
                    "generation_ran": False,
                    "feedback_quality": feedback_quality,
                    **story_interview_summary(runtime),
                },
            }

        append_feedback_entry(
            runtime,
            normalized_user_input,
            now_iso(),
            feedback_quality=feedback_quality,
        )

    included_feedback_ids = story_unabsorbed_feedback_ids(runtime)
    story_result = await run_story_agent_from_state(
        state,
        project_id=project_id,
        parent_requirement=normalized_parent_requirement,
        user_input=None if included_feedback_ids else user_input,
    )

    request_payload = _story_request_payload(story_result.get("request_payload"))
    created_at = now_iso()
    draft_basis_attempt_id = (runtime.get("draft_projection") or {}).get(
        "latest_reusable_attempt_id"
    )
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
        if story_save_payload(runtime)
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
            **story_interview_summary(runtime),
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
        if story_save_payload(runtime)
        else OrchestratorState.STORY_INTERVIEW.value
    )
    state["fsm_state"] = next_state
    save_state(state)

    return {
        "fsm_state": next_state,
        "parent_requirement": normalized_parent_requirement,
        "data": {
            "output_artifact": story_result.get("output_artifact"),
            **story_interview_summary(runtime),
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
    attempt_history = runtime.get("attempt_history") or []
    return {
        "parent_requirement": normalized_parent_requirement,
        "data": {
            "items": attempt_history,
            "count": len(attempt_history),
            **story_interview_summary(runtime),
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

    assessment = story_save_payload(runtime)

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

    saved_reqs_dict = context.state.get("story_saved", {})
    if not isinstance(saved_reqs_dict, dict):
        saved_reqs_dict = {}
    saved_reqs_dict[normalized_parent_requirement] = True
    context.state["story_saved"] = saved_reqs_dict
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

    runtime["resolution_projection"] = {
        "status": "merged",
        "owner_requirement": recommendation["owner_requirement"],
        "reason": recommendation["reason"],
        "acceptance_criteria_to_move": recommendation["acceptance_criteria_to_move"],
        "resolved_at": now_iso(),
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


async def complete_story_phase(
    *,
    expected_state: str | None,
    idempotency_key: str | None,
    scope: str | None = None,
    scope_id: str | None = None,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
) -> dict[str, Any]:
    if expected_state != OrchestratorState.STORY_PERSISTENCE.value:
        raise StoryPhaseError(
            "story complete requires --expected-state STORY_PERSISTENCE",
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
            return dict(existing)

    current_state = _normalize_fsm_state(state.get("fsm_state"))
    if current_state != OrchestratorState.STORY_PERSISTENCE.value:
        raise StoryPhaseError(
            "Story phase cannot complete unless current state is STORY_PERSISTENCE.",
            status_code=409,
        )

    req_names, scope_payload = _story_completion_scope_requirements(
        state,
        scope=scope,
        scope_id=scope_id,
    )
    saved_reqs_dict = state.get("story_saved", {})
    if not isinstance(saved_reqs_dict, dict):
        saved_reqs_dict = {}

    saved_count = 0
    merged_count = 0
    for requirement in req_names:
        if saved_reqs_dict.get(requirement) is True:
            saved_count += 1
            continue

        runtime = existing_story_runtime(state, parent_requirement=requirement)
        if runtime is not None and story_current_resolution(runtime):
            merged_count += 1

    total_count = len(req_names)
    covered_count = saved_count + merged_count
    if covered_count != total_count:
        scope_prefix = (
            f" for {scope_payload['scope_id']}" if scope_payload is not None else ""
        )
        raise StoryPhaseError(
            f"Story phase cannot complete{scope_prefix}: "
            f"{covered_count} of {total_count} roadmap requirements are saved "
            "or merged.",
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
        state.pop("story_completion_scope", None)

    payload: dict[str, Any] = {
        "fsm_state": OrchestratorState.SPRINT_SETUP.value,
        "coverage": {
            "saved": saved_count,
            "merged": merged_count,
            "total": total_count,
        },
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
