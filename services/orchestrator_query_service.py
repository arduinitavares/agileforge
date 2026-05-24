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
    open_sprint_story_ids = {
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

    candidate_list: list[dict[str, Any]] = [
        {
            "story_id": story.story_id,
            "story_title": story.title,
            "priority": _priority_to_int(story.rank),
            "story_points": story.story_points,
            "persona": story.persona,
            "source_requirement": story.source_requirement,
            "story_origin": story.story_origin,
            "story_description": story.story_description,
            "acceptance_criteria": story.acceptance_criteria,
            "evaluated_invariant_ids": _story_evaluated_invariant_ids(story),
            "story_compliance_boundary_summaries": (
                _story_compliance_boundary_summaries(story)
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
        "readiness": _sprint_candidate_readiness(candidate_list),
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
