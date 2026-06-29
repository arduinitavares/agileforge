"""Read/query service helpers previously embedded in tools.orchestrator_tools."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy import func
from sqlmodel import Session, select

from models.core import Product, Sprint, SprintStory, UserStory
from models.db import get_engine
from models.enums import SprintStatus, StoryStatus
from orchestrator_agent.agent_tools.story_linkage import normalize_requirement_key
from services.story_dependencies import (
    DependencyGraphIssue,
    load_story_dependency_graph,
)
from utils.spec_schemas import ValidationEvidence

CACHE_TTL_MINUTES: int = 5
DEFAULT_PRIORITY: int = 999
logger: logging.Logger = logging.getLogger(name=__name__)


def utc_now_iso() -> str:
    """Return current UTC time in RFC3339/ISO format with 'Z' suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def is_projects_cache_fresh(
    state: dict[str, Any],
    ttl_minutes: int = CACHE_TTL_MINUTES,
) -> bool:
    """Return True if the cached projects snapshot is within the TTL window."""
    ts = state.get("projects_last_refreshed_utc")
    if not ts:
        return False
    last = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return datetime.now(UTC) - last <= timedelta(minutes=ttl_minutes)


def _query_products(session: Session) -> list[Product]:
    """Fetch all products."""
    return list(session.exec(select(Product)).all())


def _story_evaluated_invariant_ids(story: UserStory) -> list[str]:
    """Return the evaluated invariant IDs already validated for a story."""
    if not story.validation_evidence:
        return []
    try:
        evidence = ValidationEvidence.model_validate_json(story.validation_evidence)
    except (TypeError, ValueError, ValidationError):
        return []
    return list(evidence.evaluated_invariant_ids or [])


def _story_compliance_boundary_summaries(story: UserStory) -> list[str]:
    """Return the evaluated compliance boundaries for a story."""
    if not story.validation_evidence:
        return []
    try:
        evidence = ValidationEvidence.model_validate_json(story.validation_evidence)
    except (TypeError, ValueError, ValidationError):
        return []

    findings = evidence.alignment_failures + evidence.alignment_warnings
    return [finding.message for finding in findings if finding.message]


def _priority_to_int(rank: str | None) -> int:
    """Convert legacy string rank to a comparable integer."""
    if rank is None:
        return DEFAULT_PRIORITY
    try:
        return int(rank)
    except (TypeError, ValueError):
        return DEFAULT_PRIORITY


def _story_order_key(story: UserStory) -> tuple[int, int]:
    """Stable ordering for story query results."""
    story_id = cast("int", story.story_id or 0)
    return (_priority_to_int(story.rank), story_id)


def _sprint_candidate_readiness(
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return planning-readiness diagnostics for sprint candidate rows."""
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


def _augment_readiness_with_dependency_issues(
    readiness: dict[str, Any],
    *,
    dependency_issues: list[Any],
    cycle_paths: list[list[int]],
) -> dict[str, Any]:
    """Add active dependency graph blockers to sprint readiness."""
    blocking_codes = list(readiness.get("blocking_codes") or [])
    blocking_story_ids = set(readiness.get("blocking_story_ids") or [])
    for issue in dependency_issues:
        code = str(getattr(issue, "code", "STORY_DEPENDENCY_INVALID"))
        if code not in blocking_codes:
            blocking_codes.append(code)
        dependent_story_id = getattr(issue, "dependent_story_id", None)
        if isinstance(dependent_story_id, int):
            blocking_story_ids.add(dependent_story_id)
        else:
            blocking_story_ids.update(int(story_id) for story_id in issue.story_ids)
    readiness["blocking_codes"] = blocking_codes
    readiness["blocking_story_ids"] = sorted(blocking_story_ids)
    readiness["dependency_issue_count"] = len(dependency_issues)
    readiness["dependency_cycle_paths"] = cycle_paths
    if blocking_codes:
        readiness["status"] = "blocked"
    return readiness


def _dependency_issue_blocks_candidate(
    issue: DependencyGraphIssue,
    *,
    candidate_story_ids: set[int],
) -> bool:
    """Return whether a dependency graph issue affects current Sprint candidates."""
    if issue.code == "STORY_DEPENDENCY_CYCLE":
        return any(story_id in candidate_story_ids for story_id in issue.story_ids)
    if issue.edge_status != "active":
        return False
    dependent_story_id = issue.dependent_story_id
    if dependent_story_id is None:
        return any(story_id in candidate_story_ids for story_id in issue.story_ids)
    return dependent_story_id in candidate_story_ids


def _build_projects_payload(
    session: Session,
    products: list[Product],
) -> tuple[int, list[dict[str, Any]]]:
    """Build (count, projects_list) from DB rows."""
    projects: list[dict[str, Any]] = []
    product_ids = [product.product_id for product in products]

    if not product_ids:
        return 0, []

    story_counts_query = (
        select(UserStory.product_id, func.count(cast("Any", UserStory.story_id)))
        .where(cast("Any", UserStory.product_id).in_(product_ids))
        .where(UserStory.is_superseded == False)  # noqa: E712
        .group_by(cast("Any", UserStory.product_id))
    )
    sprint_counts_query = (
        select(Sprint.product_id, func.count(cast("Any", Sprint.sprint_id)))
        .where(cast("Any", Sprint.product_id).in_(product_ids))
        .group_by(cast("Any", Sprint.product_id))
    )

    story_counts: dict[int, int] = dict(session.exec(story_counts_query).all())
    sprint_counts: dict[int, int] = dict(session.exec(sprint_counts_query).all())

    for product in products:
        product_id = cast("int", product.product_id)
        projects.append(
            {
                "product_id": product_id,
                "name": product.name,
                "vision": product.vision or "(No vision set)",
                "roadmap": product.roadmap or "(No roadmap set)",
                "user_stories_count": story_counts.get(product_id, 0),
                "sprint_count": sprint_counts.get(product_id, 0),
            }
        )
    return len(projects), projects


def refresh_projects_cache(
    state: dict[str, Any],
) -> tuple[int, list[dict[str, Any]]]:
    """Hit the DB and update the persistent projects cache in state."""
    logger.debug("Projects cache miss or expired; querying database.")
    with Session(get_engine()) as session:
        products = _query_products(session)
        count, projects = _build_projects_payload(session, products)

    state["projects_summary"] = count
    state["projects_list"] = projects
    state["projects_last_refreshed_utc"] = utc_now_iso()
    return count, projects


def get_open_sprint_story_ids(session: Session, product_id: int) -> set[int]:
    """Get the set of story IDs that are in open (PLANNED or ACTIVE) sprints."""
    return {
        int(story_id)
        for story_id in session.exec(
            select(SprintStory.story_id)
            .join(
                Sprint,
                cast("Any", Sprint.sprint_id) == cast("Any", SprintStory.sprint_id),
            )
            .where(
                Sprint.product_id == product_id,
                cast("Any", Sprint.status).in_(
                    [SprintStatus.PLANNED, SprintStatus.ACTIVE]
                ),
            )
        ).all()
        if story_id is not None
    }


def is_sprint_candidate_story(
    story: UserStory,
    open_sprint_story_ids: set[int],
) -> bool:
    """
    Check if a user story is a valid candidate for sprint planning.

    Eligibility rules:
    - Status is TO_DO
    - Story is refined (is_refined == True)
    - Story is not superseded (is_superseded == False)
    - Story is not archived (archived_reason is None)
    - Story is not linked to any open/active sprints
    """
    story_id_val = story.story_id
    if story_id_val is None:
        return False
    return (
        story.status == StoryStatus.TO_DO
        and bool(story.is_refined)
        and not bool(story.is_superseded)
        and story.archived_reason is None
        and int(story_id_val) not in open_sprint_story_ids
    )


def query_requirement_stories_and_eligibility(
    session: Session,
    project_id: int,
) -> dict[str, Any]:
    """Fetch per-requirement story metadata and sprint eligibility facts."""
    stories = list(
        session.exec(
            select(UserStory)
            .where(UserStory.product_id == project_id)
            .where(cast("Any", UserStory.archived_reason).is_(None))
            .order_by(cast("Any", UserStory.story_id))
        ).all()
    )
    open_sprint_story_ids = get_open_sprint_story_ids(session, project_id)

    stories_by_req: dict[str, list[UserStory]] = {}
    for story in stories:
        if story.source_requirement:
            norm_key = normalize_requirement_key(story.source_requirement)
            stories_by_req.setdefault(norm_key, []).append(story)

    result: dict[str, Any] = {}
    completed_statuses = {StoryStatus.DONE, StoryStatus.ACCEPTED}
    for norm_key, req_stories in stories_by_req.items():
        story_list = [
            {
                "story_id": story.story_id,
                "title": story.title,
                "story_description": story.story_description,
                "acceptance_criteria": story.acceptance_criteria,
                "status": (
                    story.status.value
                    if hasattr(story.status, "value")
                    else str(story.status)
                ),
                "story_points": story.story_points,
                "rank": story.rank,
                "refinement_slot": story.refinement_slot,
                "is_refined": bool(story.is_refined),
                "is_superseded": bool(story.is_superseded),
                "in_open_sprint": story.story_id in open_sprint_story_ids,
            }
            for story in req_stories
        ]
        active_stories = [story for story in req_stories if not story.is_superseded]
        candidates = [
            story
            for story in req_stories
            if is_sprint_candidate_story(story, open_sprint_story_ids)
        ]

        result[norm_key] = {
            "stories": story_list,
            "story_ids": [
                story.story_id for story in req_stories if story.story_id is not None
            ],
            "has_candidates": bool(candidates),
            "all_completed": bool(active_stories)
            and all(story.status in completed_statuses for story in active_stories),
            "all_superseded": bool(req_stories)
            and all(bool(story.is_superseded) for story in req_stories),
            "all_in_active_sprint": bool(active_stories)
            and all(
                story.story_id in open_sprint_story_ids for story in active_stories
            ),
        }
    return result


def fetch_sprint_candidates_from_session(
    session: Session,
    product_id: int,
) -> dict[str, Any]:
    """
    Fetch sprint-eligible stories for a product using an existing session.

    Eligibility rule:
    - status == TO_DO
    - is_refined == True
    - is_superseded == False
    """
    logger.debug(
        "Fetching refined sprint candidates for product_id=%s",
        product_id,
    )
    open_sprint_story_ids = get_open_sprint_story_ids(session, product_id)
    stories = list(
        session.exec(
            select(UserStory)
            .where(UserStory.product_id == product_id)
            .where(UserStory.status == StoryStatus.TO_DO)
            .order_by(
                cast("Any", UserStory.rank),
                cast("Any", UserStory.story_id),
            )
        ).all()
    )

    if not stories:
        logger.debug("No sprint candidate stories found for product_id=%s", product_id)
        return {
            "success": True,
            "count": 0,
            "stories": [],
            "readiness": _sprint_candidate_readiness([]),
            "excluded_counts": {
                "non_refined": 0,
                "superseded": 0,
                "open_sprint": 0,
            },
            "message": "No stories found in backlog.",
        }

    refined: list[UserStory] = []
    excluded_non_refined = 0
    excluded_superseded = 0
    excluded_open_sprint = 0

    for story in stories:
        if story.archived_reason is not None:
            continue
        if bool(story.is_superseded):
            excluded_superseded += 1
            continue
        if not bool(story.is_refined):
            excluded_non_refined += 1
            continue
        if int(story.story_id or 0) in open_sprint_story_ids:
            excluded_open_sprint += 1
            continue
        refined.append(story)

    refined.sort(key=_story_order_key)
    dependency_graph = load_story_dependency_graph(session, project_id=product_id)
    refined_story_ids = {
        int(story.story_id) for story in refined if story.story_id is not None
    }
    active_dependency_issues = [
        issue
        for issue in dependency_graph.issues
        if _dependency_issue_blocks_candidate(
            issue,
            candidate_story_ids=refined_story_ids,
        )
    ]

    candidate_list: list[dict[str, Any]] = [
        {
            "story_id": story.story_id,
            "story_title": story.title,
            "priority": _priority_to_int(story.rank),
            "story_points": story.story_points,
            "persona": story.persona,
            "source_requirement": story.source_requirement,
            "story_origin": story.story_origin,
            "accepted_spec_version_id": story.accepted_spec_version_id,
            "story_description": story.story_description,
            "acceptance_criteria": story.acceptance_criteria,
            "evaluated_invariant_ids": _story_evaluated_invariant_ids(story),
            "story_compliance_boundary_summaries": (
                _story_compliance_boundary_summaries(story)
            ),
            "prerequisite_story_ids": sorted(
                dependency_graph.active_edges.get(int(story.story_id or 0), set())
            ),
            "blocked_by_story_ids": sorted(
                prerequisite_id
                for prerequisite_id in dependency_graph.active_edges.get(
                    int(story.story_id or 0),
                    set(),
                )
                if prerequisite_id in refined_story_ids
            ),
            "dependency_status": (
                "blocked"
                if any(
                    prerequisite_id in refined_story_ids
                    for prerequisite_id in dependency_graph.active_edges.get(
                        int(story.story_id or 0),
                        set(),
                    )
                )
                else "ready"
            ),
        }
        for story in refined
    ]

    logger.debug(
        (
            "Found %s sprint candidates "
            "(excluded: non_refined=%s, superseded=%s, open_sprint=%s)."
        ),
        len(candidate_list),
        excluded_non_refined,
        excluded_superseded,
        excluded_open_sprint,
    )

    return {
        "success": True,
        "count": len(candidate_list),
        "stories": candidate_list,
        "readiness": _augment_readiness_with_dependency_issues(
            _sprint_candidate_readiness(candidate_list),
            dependency_issues=active_dependency_issues,
            cycle_paths=dependency_graph.cycle_paths,
        ),
        "excluded_counts": {
            "non_refined": excluded_non_refined,
            "superseded": excluded_superseded,
            "open_sprint": excluded_open_sprint,
        },
        "message": (
            f"Found {len(candidate_list)} refined sprint candidate(s) in backlog "
            f"(excluded non-refined={excluded_non_refined}, "
            f"superseded={excluded_superseded}, "
            f"open_sprint={excluded_open_sprint})."
        ),
    }


def fetch_sprint_candidates(product_id: int) -> dict[str, Any]:
    """Open a session and fetch sprint-eligible stories for a product."""
    with Session(get_engine()) as session:
        return fetch_sprint_candidates_from_session(session, product_id)


def get_real_business_state() -> dict[str, Any]:
    """Hydrate the initial session state by querying the business database."""
    logger.debug("Hydrating session state from business database.")
    with Session(get_engine()) as session:
        products = _query_products(session)
        count, projects = _build_projects_payload(session, products)

    logger.debug("Found %s existing projects while hydrating session state.", count)
    return {
        "projects_summary": count,
        "projects_list": projects,
        "projects_last_refreshed_utc": utc_now_iso(),
        "current_context": "idle",
        "active_project": None,
    }
