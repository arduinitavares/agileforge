"""Service-level tests for orchestrator read/query extraction."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from agile_sqlmodel import (
    Product,
    Sprint,
    SprintStatus,
    SprintStory,
    StoryStatus,
    UserStory,
)
from models.core import Team
from services.orchestrator_query_service import (
    query_requirement_stories_and_eligibility,
)
from tests.typing_helpers import require_id

if TYPE_CHECKING:
    from sqlmodel import Session


def test_services_use_orchestrator_query_service_boundary() -> None:
    """Workflow and sprint-input services should no longer import tool-layer queries."""
    from services import sprint_input, workflow  # noqa: PLC0415

    assert (
        sprint_input.fetch_sprint_candidates.__module__
        == "services.orchestrator_query_service"
    )
    assert (
        workflow.get_real_business_state.__module__
        == "services.orchestrator_query_service"
    )


def test_query_service_fetch_sprint_candidates_filters_open_sprints(
    session: Session,
) -> None:
    """Query service should expose only refined TODO stories not already in open sprints."""  # noqa: E501
    from services.orchestrator_query_service import (  # noqa: PLC0415
        fetch_sprint_candidates,
    )

    product = Product(name="Query Product", vision="Vision", description="Desc")
    team = Team(name="Query Team")
    session.add(product)
    session.add(team)
    session.commit()
    session.refresh(product)
    session.refresh(team)
    product_id = require_id(product.product_id, "product_id")
    team_id = require_id(team.team_id, "team_id")

    stories = {
        "planned": UserStory(
            product_id=product_id,
            title="Planned story",
            story_description="Assigned to a planned sprint",
            acceptance_criteria="- AC",
            status=StoryStatus.TO_DO,
            is_refined=True,
            rank="1",
        ),
        "active": UserStory(
            product_id=product_id,
            title="Active story",
            story_description="Assigned to an active sprint",
            acceptance_criteria="- AC",
            status=StoryStatus.TO_DO,
            is_refined=True,
            rank="2",
        ),
        "completed_only": UserStory(
            product_id=product_id,
            title="Completed-only story",
            story_description="Assigned only to a completed sprint",
            acceptance_criteria="- AC",
            status=StoryStatus.TO_DO,
            is_refined=True,
            rank="3",
        ),
        "eligible": UserStory(
            product_id=product_id,
            title="Eligible story",
            story_description="Not assigned to any open sprint",
            acceptance_criteria="- AC",
            status=StoryStatus.TO_DO,
            is_refined=True,
            rank="4",
        ),
        "non_refined": UserStory(
            product_id=product_id,
            title="Needs refinement",
            story_description="Still rough",
            acceptance_criteria="- AC",
            status=StoryStatus.TO_DO,
            is_refined=False,
            rank="5",
        ),
        "superseded": UserStory(
            product_id=product_id,
            title="Superseded story",
            story_description="Replaced by another story",
            acceptance_criteria="- AC",
            status=StoryStatus.TO_DO,
            is_refined=True,
            is_superseded=True,
            rank="6",
        ),
    }
    session.add_all(stories.values())
    session.flush()

    planned_sprint = Sprint(
        goal="Planned",
        start_date=date(2026, 2, 1),
        end_date=date(2026, 2, 14),
        status=SprintStatus.PLANNED,
        product_id=product_id,
        team_id=team_id,
    )
    active_sprint = Sprint(
        goal="Active",
        start_date=date(2026, 2, 15),
        end_date=date(2026, 2, 28),
        status=SprintStatus.ACTIVE,
        product_id=product_id,
        team_id=team_id,
    )
    completed_sprint = Sprint(
        goal="Completed",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 14),
        status=SprintStatus.COMPLETED,
        product_id=product_id,
        team_id=team_id,
    )
    session.add_all([planned_sprint, active_sprint, completed_sprint])
    session.flush()

    session.add_all(
        [
            SprintStory(
                sprint_id=require_id(planned_sprint.sprint_id, "planned_sprint_id"),
                story_id=require_id(stories["planned"].story_id, "planned_story_id"),
            ),
            SprintStory(
                sprint_id=require_id(active_sprint.sprint_id, "active_sprint_id"),
                story_id=require_id(stories["active"].story_id, "active_story_id"),
            ),
            SprintStory(
                sprint_id=require_id(
                    completed_sprint.sprint_id,
                    "completed_sprint_id",
                ),
                story_id=require_id(
                    stories["completed_only"].story_id,
                    "completed_story_id",
                ),
            ),
        ]
    )
    session.commit()

    result = fetch_sprint_candidates(product_id)

    assert result["success"] is True
    assert [story["story_title"] for story in result["stories"]] == [
        "Completed-only story",
        "Eligible story",
    ]
    assert result["excluded_counts"] == {
        "non_refined": 1,
        "superseded": 1,
        "open_sprint": 2,
    }


def test_query_service_session_helper_matches_fetch_sprint_candidates(
    session: Session,
) -> None:
    """Session-level helper should be the canonical sprint candidate query."""
    from services.orchestrator_query_service import (  # noqa: PLC0415
        fetch_sprint_candidates,
        fetch_sprint_candidates_from_session,
    )

    product = Product(name="Session Helper Product", vision="Vision")
    team = Team(name="Session Helper Team")
    session.add(product)
    session.add(team)
    session.commit()
    session.refresh(product)
    session.refresh(team)
    product_id = require_id(product.product_id, "product_id")
    team_id = require_id(team.team_id, "team_id")

    planned_story = UserStory(
        product_id=product_id,
        title="Planned story",
        status=StoryStatus.TO_DO,
        is_refined=True,
        rank="1",
    )
    eligible_story = UserStory(
        product_id=product_id,
        title="Eligible story",
        status=StoryStatus.TO_DO,
        is_refined=True,
        rank="2",
    )
    session.add_all([planned_story, eligible_story])
    session.flush()
    sprint = Sprint(
        goal="Planned",
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 14),
        status=SprintStatus.PLANNED,
        product_id=product_id,
        team_id=team_id,
    )
    session.add(sprint)
    session.flush()
    session.add(
        SprintStory(
            sprint_id=require_id(sprint.sprint_id, "sprint_id"),
            story_id=require_id(planned_story.story_id, "planned_story_id"),
        )
    )
    session.commit()

    from_session = fetch_sprint_candidates_from_session(session, product_id)
    from_engine = fetch_sprint_candidates(product_id)

    assert from_session == from_engine


def test_query_service_get_real_business_state_returns_idle_snapshot(
    session: Session,
) -> None:
    """Query service should build the initial workflow state from project summaries."""
    from services.orchestrator_query_service import (  # noqa: PLC0415
        get_real_business_state,
    )

    team = Team(name="State Team")
    alpha = Product(name="Alpha", vision="Alpha vision", description="Desc")
    beta = Product(name="Beta", vision=None, description="Beta desc")
    session.add_all([team, alpha, beta])
    session.commit()
    session.refresh(team)
    session.refresh(alpha)
    session.refresh(beta)
    team_id = require_id(team.team_id, "team_id")
    alpha_id = require_id(alpha.product_id, "alpha_id")

    session.add(
        UserStory(
            product_id=alpha_id,
            title="Alpha story",
            status=StoryStatus.TO_DO,
            rank="1",
        )
    )
    session.add(
        Sprint(
            goal="Alpha sprint",
            start_date=date(2026, 3, 1),
            end_date=date(2026, 3, 14),
            status=SprintStatus.PLANNED,
            product_id=alpha_id,
            team_id=team_id,
        )
    )
    session.commit()

    result = get_real_business_state()

    assert result["projects_summary"] == 2  # noqa: PLR2004
    assert result["current_context"] == "idle"
    assert result["active_project"] is None
    assert "projects_last_refreshed_utc" in result

    by_name = {project["name"]: project for project in result["projects_list"]}
    assert by_name["Alpha"]["user_stories_count"] == 1
    assert by_name["Alpha"]["sprint_count"] == 1
    assert by_name["Alpha"]["vision"] == "Alpha vision"
    assert by_name["Beta"]["user_stories_count"] == 0
    assert by_name["Beta"]["sprint_count"] == 0
    assert by_name["Beta"]["vision"] == "(No vision set)"


def test_query_requirement_stories_and_eligibility(
    session: Session,
) -> None:
    """Requirement stories should be grouped with sprint eligibility facts."""
    expected_requirement_a_story_count = 2
    expected_story_points = 3
    expected_refinement_slot = 1

    product = Product(name="Stories Eligibility Product", vision="Vision")
    session.add(product)
    session.commit()
    session.refresh(product)
    product_id = require_id(product.product_id, "product_id")

    story_eligible = UserStory(
        product_id=product_id,
        title="Story Eligible",
        story_description="Eligible story details",
        acceptance_criteria="- First acceptance criterion",
        status=StoryStatus.TO_DO,
        story_points=expected_story_points,
        is_refined=True,
        is_superseded=False,
        source_requirement="Requirement A",
        refinement_slot=expected_refinement_slot,
        rank="1",
    )
    story_superseded = UserStory(
        product_id=product_id,
        title="Story Superseded",
        status=StoryStatus.TO_DO,
        is_refined=True,
        is_superseded=True,
        source_requirement="Requirement A",
        rank="2",
    )
    story_completed = UserStory(
        product_id=product_id,
        title="Story Completed",
        status=StoryStatus.DONE,
        is_refined=True,
        is_superseded=False,
        source_requirement="Requirement B",
        rank="3",
    )
    session.add_all([story_eligible, story_superseded, story_completed])
    session.commit()

    meta = query_requirement_stories_and_eligibility(session, product_id)

    assert "requirement a" in meta
    req_a = meta["requirement a"]
    assert req_a["has_candidates"] is True
    assert req_a["all_completed"] is False
    assert req_a["all_superseded"] is False
    assert len(req_a["stories"]) == expected_requirement_a_story_count
    eligible_story = req_a["stories"][0]
    assert eligible_story["story_description"] == "Eligible story details"
    assert eligible_story["acceptance_criteria"] == "- First acceptance criterion"
    assert eligible_story["story_points"] == expected_story_points
    assert eligible_story["refinement_slot"] == expected_refinement_slot
    assert eligible_story["rank"] == "1"

    assert "requirement b" in meta
    req_b = meta["requirement b"]
    assert req_b["has_candidates"] is False
    assert req_b["all_completed"] is True
    assert req_b["all_superseded"] is False
    assert len(req_b["stories"]) == 1
