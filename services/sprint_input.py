# services/sprint_input.py

"""Helpers for loading and normalizing sprint planner input context."""

from __future__ import annotations

import json
from typing import Any, NotRequired, Protocol, TypedDict, Unpack, cast

from orchestrator_agent.agent_tools.story_linkage import normalize_requirement_key
from services.agent_workbench.fingerprints import canonical_hash
from services.orchestrator_query_service import fetch_sprint_candidates
from services.sprint_selection import (
    SprintSelectionError,
    derive_group_slot,
    derive_parent_group,
    select_sprint_story_rows,
)

DEFAULT_PRIORITY: int = 999
STANDARD_SPRINT_CANDIDATE_BLOCKING_CODES: set[str] = {
    "SPRINT_CANDIDATES_UNSIZED",
    "SPRINT_CANDIDATES_DEFAULT_PRIORITY",
}


class _SprintCandidateFetcher(Protocol):
    def __call__(self, *, product_id: int) -> dict[str, Any]: ...


class _PrepareSprintInputOptions(TypedDict):
    team_velocity_assumption: object
    sprint_duration_days: object
    user_context: str | None
    max_story_points: object
    include_task_decomposition: object
    selected_story_ids: NotRequired[list[int] | None]
    fetch_candidates: NotRequired[_SprintCandidateFetcher | None]
    story_completion_scope: NotRequired[object]


def as_text(value: object) -> str:
    """Normalize arbitrary values into text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def normalize_velocity(value: object) -> str:
    """Normalize velocity input to Low/Medium/High."""
    normalized = as_text(value).strip().lower()
    if normalized == "low":
        return "Low"
    if normalized == "high":
        return "High"
    return "Medium"


def normalize_duration_days(value: object) -> int:
    """Clamp sprint duration to schema-safe bounds."""
    try:
        parsed = int(as_text(value).strip())
    except ValueError:
        return 14
    return max(1, min(parsed, 31))


def normalize_positive_int(value: object) -> int | None:
    """Normalize optional positive integer fields."""
    if value is None:
        return None
    try:
        parsed = int(as_text(value).strip())
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def coerce_priority(value: object, fallback: int) -> int:
    """Ensure priority is always an integer >= 1."""
    parsed = normalize_positive_int(value)
    return parsed if parsed is not None else max(1, fallback)


def _sprint_candidate_readiness(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Return planning-readiness diagnostics for normalized sprint candidates."""
    unsized_ids = [
        int(candidate["story_id"])
        for candidate in candidates
        if candidate.get("story_id") is not None
        and candidate.get("story_points") is None
    ]
    default_priority_ids = [
        int(candidate["story_id"])
        for candidate in candidates
        if candidate.get("story_id") is not None
        and candidate.get("priority") == DEFAULT_PRIORITY
    ]
    blocking_codes: list[str] = []
    if unsized_ids:
        blocking_codes.append("SPRINT_CANDIDATES_UNSIZED")
    if default_priority_ids:
        blocking_codes.append("SPRINT_CANDIDATES_DEFAULT_PRIORITY")
    return {
        "status": "blocked" if blocking_codes else "ready",
        "unsized_count": len(unsized_ids),
        "default_priority_count": len(default_priority_ids),
        "blocking_codes": blocking_codes,
        "blocking_story_ids": sorted(set(unsized_ids + default_priority_ids)),
    }


def _merge_sprint_candidate_readiness(
    existing_readiness: object,
    filtered_readiness: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge upstream readiness with readiness recalculated for filtered rows."""
    readiness = dict(filtered_readiness)
    if not isinstance(existing_readiness, dict):
        return readiness

    selected_story_ids = {
        story_id
        for story_id in (
            normalize_positive_int(candidate.get("story_id"))
            for candidate in candidates
        )
        if story_id is not None
    }
    upstream_readiness = cast("dict[str, Any]", existing_readiness)
    upstream_codes: list[str] = []
    for code in upstream_readiness.get("blocking_codes") or []:
        normalized_code = str(code).strip()
        if (
            normalized_code
            and normalized_code not in STANDARD_SPRINT_CANDIDATE_BLOCKING_CODES
            and normalized_code not in upstream_codes
        ):
            upstream_codes.append(normalized_code)

    upstream_blocking_story_ids = {
        story_id
        for story_id in (
            normalize_positive_int(value)
            for value in (upstream_readiness.get("blocking_story_ids") or [])
        )
        if story_id is not None
    }
    retained_upstream_story_ids = upstream_blocking_story_ids & selected_story_ids
    preserve_upstream_codes = bool(retained_upstream_story_ids) or (
        bool(upstream_codes) and not upstream_blocking_story_ids
    )

    blocking_codes: list[str] = []
    if preserve_upstream_codes:
        blocking_codes.extend(upstream_codes)
    for code in filtered_readiness.get("blocking_codes") or []:
        normalized_code = str(code).strip()
        if normalized_code and normalized_code not in blocking_codes:
            blocking_codes.append(normalized_code)

    filtered_blocking_story_ids = {
        story_id
        for story_id in (
            normalize_positive_int(value)
            for value in (filtered_readiness.get("blocking_story_ids") or [])
        )
        if story_id is not None
    }
    blocking_story_ids = retained_upstream_story_ids | filtered_blocking_story_ids

    readiness["blocking_codes"] = blocking_codes
    readiness["blocking_story_ids"] = sorted(blocking_story_ids)
    readiness["status"] = (
        "blocked"
        if blocking_codes
        or blocking_story_ids
        or filtered_readiness.get("status") == "blocked"
        else "ready"
    )
    return readiness


def _augment_readiness_with_scope_external_dependencies(
    readiness: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Block scoped planning when selected candidates depend on excluded stories."""
    selected_story_ids: set[int] = set()
    for candidate in candidates:
        story_id = normalize_positive_int(candidate.get("story_id"))
        if story_id is not None:
            selected_story_ids.add(story_id)

    blocking_story_ids: set[int] = set()
    external_dependency_story_ids: set[int] = set()
    for candidate in candidates:
        story_id = normalize_positive_int(candidate.get("story_id"))
        if story_id is None:
            continue
        dependency_ids = [
            *(candidate.get("prerequisite_story_ids") or []),
            *(candidate.get("blocked_by_story_ids") or []),
        ]
        for dependency_id in dependency_ids:
            normalized_dependency_id = normalize_positive_int(dependency_id)
            if (
                normalized_dependency_id is not None
                and normalized_dependency_id not in selected_story_ids
            ):
                blocking_story_ids.add(story_id)
                external_dependency_story_ids.add(normalized_dependency_id)

    if not external_dependency_story_ids:
        return readiness

    blocking_codes = list(readiness.get("blocking_codes") or [])
    if "SPRINT_SCOPE_EXTERNAL_DEPENDENCY" not in blocking_codes:
        blocking_codes.append("SPRINT_SCOPE_EXTERNAL_DEPENDENCY")

    existing_blocking_story_ids = {
        story_id
        for story_id in (
            normalize_positive_int(value)
            for value in (readiness.get("blocking_story_ids") or [])
        )
        if story_id is not None
    }
    readiness["status"] = "blocked"
    readiness["blocking_codes"] = blocking_codes
    readiness["blocking_story_ids"] = sorted(
        existing_blocking_story_ids | blocking_story_ids
    )
    readiness["external_dependency_story_ids"] = sorted(external_dependency_story_ids)
    return readiness


def _story_completion_scope_payload(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    scope_data = cast("dict[str, Any]", value)
    raw_requirements = scope_data.get("requirements")
    if not isinstance(raw_requirements, list):
        return None
    requirements = [
        str(requirement).strip()
        for requirement in raw_requirements
        if str(requirement).strip()
    ]
    if not requirements:
        return None
    return {
        "scope": str(scope_data.get("scope") or "").strip() or None,
        "scope_id": str(scope_data.get("scope_id") or "").strip() or None,
        "requirements": requirements,
    }


def _scope_requirement_keys(scope_payload: dict[str, Any]) -> set[str]:
    return {
        normalize_requirement_key(requirement)
        for requirement in scope_payload["requirements"]
        if normalize_requirement_key(requirement)
    }


def _story_completion_scope_candidate_message(
    *,
    candidate_count: int,
    scope_payload: dict[str, Any],
    excluded_counts: dict[str, Any],
) -> str:
    """Return a user-facing candidate message without internal scope ids."""
    scope = as_text(scope_payload.get("scope")).strip().lower()
    if scope == "selection":
        scope_label = "selected-story scope"
    elif scope == "milestone":
        scope_label = "milestone Story scope"
    else:
        scope_label = "Story scope"

    candidate_word = "candidate" if candidate_count == 1 else "candidates"
    message = f"Found {candidate_count} sprint {candidate_word} for {scope_label}."
    non_refined_count = normalize_positive_int(excluded_counts.get("non_refined"))
    if non_refined_count:
        requirement_word = "requirement" if non_refined_count == 1 else "requirements"
        message = (
            f"{message} Excluded: {non_refined_count} non-refined "
            f"{requirement_word}."
        )
    return message


def apply_story_completion_scope_to_candidate_result(
    candidate_result: dict[str, Any],
    story_completion_scope: object,
) -> dict[str, Any]:
    """Return a candidate result filtered to the completed Story scope."""
    scope_payload = _story_completion_scope_payload(story_completion_scope)
    if scope_payload is None or candidate_result.get("success") is not True:
        return candidate_result

    requirement_keys = _scope_requirement_keys(scope_payload)
    if not requirement_keys:
        return candidate_result

    raw_stories = candidate_result.get("stories")
    if not isinstance(raw_stories, list):
        raw_stories = []

    filtered: list[dict[str, Any]] = []
    excluded_count = 0
    for story in raw_stories:
        if not isinstance(story, dict):
            continue
        source_requirement = as_text(story.get("source_requirement")).strip()
        if normalize_requirement_key(source_requirement) in requirement_keys:
            filtered.append(story)
        else:
            excluded_count += 1

    result = dict(candidate_result)
    result["stories"] = filtered
    result["count"] = len(filtered)
    filtered_readiness = _sprint_candidate_readiness(filtered)
    result["readiness"] = _augment_readiness_with_scope_external_dependencies(
        _merge_sprint_candidate_readiness(
            candidate_result.get("readiness"),
            filtered_readiness,
            filtered,
        ),
        filtered,
    )
    excluded_counts = dict(result.get("excluded_counts") or {})
    if excluded_count:
        excluded_counts["story_completion_scope"] = excluded_count
    result["excluded_counts"] = excluded_counts
    result["message"] = _story_completion_scope_candidate_message(
        candidate_count=len(filtered),
        scope_payload=scope_payload,
        excluded_counts=excluded_counts,
    )
    result["story_completion_scope"] = scope_payload
    result.pop("source_fingerprint", None)
    return result


def normalize_selected_story_ids(value: object) -> list[int]:
    """Normalize selected story IDs while preserving positive manual repeats."""
    if not isinstance(value, list):
        return []
    normalized: list[int] = []
    for item in value:
        parsed = normalize_positive_int(item)
        if parsed is None:
            continue
        normalized.append(parsed)
    return normalized


def velocity_story_limit(velocity: object) -> int:
    """Upper bound for the story-count heuristic used in the UI."""
    normalized = normalize_velocity(velocity)
    if normalized == "Low":
        return 3
    if normalized == "High":
        return 7
    return 5


def load_sprint_candidates(
    product_id: int,
    *,
    fetch_candidates: _SprintCandidateFetcher | None = None,
    story_completion_scope: object = None,
) -> dict[str, Any]:
    """Load and normalize sprint-eligible candidate stories from the database."""
    resolver = fetch_candidates or fetch_sprint_candidates
    raw_result = resolver(product_id=product_id)
    if not raw_result.get("success"):
        return {
            "success": False,
            "error_code": "SPRINT_CANDIDATE_FETCH_FAILED",
            "message": raw_result.get("error") or "Failed to fetch sprint candidates.",
            "stories": [],
        }

    raw_stories = raw_result.get("stories")
    if not isinstance(raw_stories, list):
        raw_stories = []

    stories: list[dict[str, Any]] = []
    for idx, row in enumerate(raw_stories, start=1):
        if not isinstance(row, dict):
            continue
        story_id = normalize_positive_int(row.get("story_id"))
        if story_id is None:
            continue

        story_title = as_text(row.get("story_title") or row.get("title")).strip()
        if not story_title:
            story_title = f"Story {story_id}"

        normalized_story: dict[str, Any] = {
            "story_id": story_id,
            "story_title": story_title,
            "priority": coerce_priority(row.get("priority"), idx),
            "story_points": normalize_positive_int(row.get("story_points")),
            "story_description": as_text(row.get("story_description")).strip(),
            "acceptance_criteria_items": [
                line.lstrip("-* \t").strip()
                for line in as_text(row.get("acceptance_criteria")).splitlines()
                if line.lstrip("-* \t").strip()
            ],
            "evaluated_invariant_ids": [
                str(item).strip()
                for item in (row.get("evaluated_invariant_ids") or [])
                if str(item).strip()
            ],
            "story_compliance_boundary_summaries": [
                str(item).strip()
                for item in (row.get("story_compliance_boundary_summaries") or [])
                if str(item).strip()
            ],
            "prerequisite_story_ids": [
                int(story_id)
                for story_id in (row.get("prerequisite_story_ids") or [])
                if normalize_positive_int(story_id) is not None
            ],
            "blocked_by_story_ids": [
                int(story_id)
                for story_id in (row.get("blocked_by_story_ids") or [])
                if normalize_positive_int(story_id) is not None
            ],
            "dependency_status": as_text(
                row.get("dependency_status") or "ready"
            ).strip()
            or "ready",
        }

        persona = as_text(row.get("persona")).strip() or None
        if persona:
            normalized_story["persona"] = persona

        source_req = as_text(row.get("source_requirement")).strip() or None
        if source_req:
            normalized_story["source_requirement"] = source_req

        stories.append(normalized_story)

    candidate_result = {
        "success": True,
        "count": len(stories),
        "stories": stories,
        "readiness": (
            raw_result.get("readiness")
            if isinstance(raw_result.get("readiness"), dict)
            else _sprint_candidate_readiness(stories)
        ),
        "excluded_counts": raw_result.get("excluded_counts") or {},
        "message": raw_result.get("message")
        or f"Found {len(stories)} sprint candidates.",
    }
    candidate_result = apply_story_completion_scope_to_candidate_result(
        candidate_result,
        story_completion_scope=story_completion_scope,
    )
    stories = list(candidate_result["stories"])
    readiness = candidate_result["readiness"]
    excluded_counts = candidate_result["excluded_counts"]
    message = candidate_result["message"]
    source_fingerprint = canonical_hash(
        {
            "command": "agileforge sprint candidates",
            "product_id": product_id,
            "stories": stories,
            "readiness": readiness,
            "excluded_counts": excluded_counts,
            "message": message,
            "story_completion_scope": candidate_result.get("story_completion_scope"),
        }
    )

    return {
        "success": True,
        "count": len(stories),
        "stories": stories,
        "readiness": readiness,
        "excluded_counts": excluded_counts,
        "message": message,
        "source_fingerprint": source_fingerprint,
        "story_completion_scope": candidate_result.get("story_completion_scope"),
    }


def prepare_sprint_input_context(
    *,
    product_id: int,
    **options: Unpack[_PrepareSprintInputOptions],
) -> dict[str, Any]:
    """Build normalized SprintPlannerInput-compatible context from DB candidates."""
    candidate_result = load_sprint_candidates(
        product_id,
        fetch_candidates=options.get("fetch_candidates"),
        story_completion_scope=options.get("story_completion_scope"),
    )
    if not candidate_result.get("success"):
        return {
            "success": False,
            "error_code": candidate_result.get(
                "error_code", "SPRINT_CANDIDATE_FETCH_FAILED"
            ),
            "message": candidate_result.get(
                "message", "Failed to fetch sprint candidates."
            ),
            "candidate_result": candidate_result,
            "input_context": {},
        }

    candidate_rows = candidate_result.get("stories") or []
    if not candidate_rows:
        return {
            "success": False,
            "error_code": "SPRINT_CANDIDATES_MISSING",
            "message": (
                "Only refined TO_DO stories are sprint-eligible. Refine stories first."
            ),
            "candidate_result": candidate_result,
            "input_context": {},
        }

    normalized_selected_ids = normalize_selected_story_ids(
        options.get("selected_story_ids")
    )
    if normalized_selected_ids:
        by_id = {
            int(row["story_id"]): row
            for row in candidate_rows
            if isinstance(row, dict)
            and normalize_positive_int(row.get("story_id")) is not None
        }
        invalid_ids = [
            story_id for story_id in normalized_selected_ids if story_id not in by_id
        ]
        if invalid_ids:
            return {
                "success": False,
                "error_code": "SPRINT_SELECTION_INVALID",
                "message": (
                    "Some selected_story_ids are not refined TO_DO candidates: "
                    + ", ".join(str(item) for item in invalid_ids)
                ),
                "invalid_selected_ids": invalid_ids,
                "candidate_result": candidate_result,
                "input_context": {},
            }

    team_velocity_assumption = normalize_velocity(options["team_velocity_assumption"])
    max_story_points = normalize_positive_int(options["max_story_points"])
    try:
        selection = select_sprint_story_rows(
            candidate_rows,
            team_velocity_assumption=team_velocity_assumption,
            max_story_points=max_story_points,
            selected_story_ids=normalized_selected_ids,
        )
    except SprintSelectionError as exc:
        return {
            "success": False,
            "error_code": exc.code,
            "message": str(exc),
            "selection_details": exc.details,
            "candidate_result": candidate_result,
            "input_context": {},
        }

    input_context: dict[str, Any] = {
        "available_stories": [
            {
                "story_id": int(row["story_id"]),
                "story_title": row["story_title"],
                "priority": int(row["priority"]),
                "parent_group": derive_parent_group(int(row["priority"])),
                "group_slot": derive_group_slot(int(row["priority"])),
                "story_points": row.get("story_points"),
                "story_description": row.get("story_description", ""),
                "acceptance_criteria_items": list(
                    row.get("acceptance_criteria_items") or []
                ),
                "persona": row.get("persona"),
                "source_requirement": row.get("source_requirement"),
                "evaluated_invariant_ids": list(
                    row.get("evaluated_invariant_ids") or []
                ),
                "story_compliance_boundary_summaries": list(
                    row.get("story_compliance_boundary_summaries") or []
                ),
                "prerequisite_story_ids": list(row.get("prerequisite_story_ids") or []),
                "blocked_by_story_ids": list(row.get("blocked_by_story_ids") or []),
                "dependency_status": row.get("dependency_status", "ready"),
            }
            for row in selection.selected_rows
            if isinstance(row, dict)
        ],
        "team_velocity_assumption": team_velocity_assumption,
        "sprint_duration_days": normalize_duration_days(
            options["sprint_duration_days"]
        ),
        "max_story_points": max_story_points,
        "include_task_decomposition": bool(options["include_task_decomposition"]),
    }

    normalized_user_context = as_text(options["user_context"]).strip()
    if normalized_user_context:
        input_context["user_context"] = normalized_user_context

    return {
        "success": True,
        "input_context": input_context,
        "candidate_result": candidate_result,
        "source_fingerprint": candidate_result.get("source_fingerprint"),
        "selected_story_ids": selection.selected_story_ids,
        "selection_policy": {
            "mode": selection.mode,
            "source_fingerprint": candidate_result.get("source_fingerprint"),
            "selected_story_ids": selection.selected_story_ids,
            "excluded_story_ids": selection.excluded_story_ids,
            "story_points_used": selection.story_points_used,
            "max_story_points": selection.max_story_points,
            "team_velocity_assumption": selection.team_velocity_assumption,
            "story_limit": selection.story_limit,
            "dependency_closed": selection.dependency_closed,
            "dependency_edges": selection.dependency_edges,
            "dependency_promoted_story_ids": selection.dependency_promoted_story_ids,
            "warnings": selection.warnings,
        },
    }
