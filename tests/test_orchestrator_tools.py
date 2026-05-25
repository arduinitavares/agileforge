"""
Test suite for orchestrator decision-making tools.

Tests the query tools used by the orchestrator agent.
"""

from sqlmodel import Session

from agile_sqlmodel import Product, StoryStatus, UserStory, UserStoryDependency
from tests.typing_helpers import make_tool_context
from tools.orchestrator_tools import (
    count_projects,
    fetch_product_backlog,
    fetch_sprint_candidates,
    get_project_by_name,
    get_project_details,
    list_projects,
)


def test_count_projects_empty() -> None:
    """Test counting projects when database is empty."""
    context = make_tool_context()
    result = count_projects({}, context)

    assert result["success"] is True
    assert result["count"] == 0
    assert "0 project" in result["message"]


def test_count_projects_with_data() -> None:
    """Test counting projects when products exist."""
    from tools.db_tools import (  # noqa: PLC0415
        CreateOrGetProductInput,
        create_or_get_product,
    )

    # Create 3 products
    create_or_get_product(
        CreateOrGetProductInput(product_name="Project A", vision=None, description=None)
    )
    create_or_get_product(
        CreateOrGetProductInput(product_name="Project B", vision=None, description=None)
    )
    create_or_get_product(
        CreateOrGetProductInput(product_name="Project C", vision=None, description=None)
    )

    context = make_tool_context()
    result = count_projects({}, context)

    assert result["success"] is True
    assert result["count"] == 3  # noqa: PLR2004
    assert "3 project" in result["message"]


def test_list_projects_empty() -> None:
    """Test listing projects when database is empty."""
    context = make_tool_context()
    result = list_projects({}, context)

    assert result["success"] is True
    assert result["count"] == 0
    assert len(result["projects"]) == 0


def test_list_projects_with_data() -> None:
    """Test listing projects with summary data."""
    from tools.db_tools import (  # noqa: PLC0415
        CreateOrGetProductInput,
        CreateUserStoryInput,
        create_or_get_product,
        create_user_story,
        persist_roadmap,
    )

    # Create a product with structure
    prod_result = create_or_get_product(
        CreateOrGetProductInput(
            product_name="Test Project", vision="Test vision", description=None
        )
    )
    product_id = prod_result["product_id"]

    # Add roadmap
    roadmap = [
        {
            "quarter": "Q1",
            "theme_title": "Auth",
            "theme_description": "",
            "epics": [
                {
                    "epic_title": "Login",
                    "epic_summary": "",
                    "features": [
                        {"title": "Email Login", "description": ""},
                    ],
                }
            ],
        }
    ]
    roadmap_result = persist_roadmap(product_id, roadmap)
    feature_id = roadmap_result["created"]["features"][0]["id"]

    # Add a user story
    create_user_story(
        CreateUserStoryInput(
            product_id=product_id,
            feature_id=feature_id,
            title="Login as user",
            description="As a user...",
            acceptance_criteria=None,
            story_points=None,
        )
    )

    # Now list projects
    context = make_tool_context()
    result = list_projects({}, context)

    assert result["success"] is True
    assert result["count"] == 1
    assert len(result["projects"]) == 1

    project = result["projects"][0]
    assert project["product_id"] == product_id
    assert project["name"] == "Test Project"
    assert project["vision"] == "Test vision"
    assert project["user_stories_count"] == 1


def test_get_project_details_not_found() -> None:
    """Test getting details for non-existent project."""
    result = get_project_details(999)

    assert result["success"] is False
    assert "not found" in result["error"].lower()


def test_get_project_details_with_structure() -> None:
    """Test getting detailed structure of a project."""
    from tools.db_tools import (  # noqa: PLC0415
        CreateOrGetProductInput,
        CreateUserStoryInput,
        create_or_get_product,
        create_user_story,
        persist_roadmap,
    )

    # Create a complete project structure
    prod_result = create_or_get_product(
        CreateOrGetProductInput(
            product_name="Full Project", vision="Complete vision", description=None
        )
    )
    product_id = prod_result["product_id"]

    roadmap = [
        {
            "quarter": "Q1",
            "theme_title": "Auth",
            "theme_description": "Authentication",
            "epics": [
                {
                    "epic_title": "Login",
                    "epic_summary": "Login system",
                    "features": [
                        {"title": "Email Login", "description": "Email auth"},
                        {"title": "OAuth", "description": "OAuth login"},
                    ],
                },
                {
                    "epic_title": "Registration",
                    "epic_summary": "Registration system",
                    "features": [
                        {"title": "Sign Up", "description": "User signup"},
                    ],
                },
            ],
        }
    ]
    roadmap_result = persist_roadmap(product_id, roadmap)

    # Add user stories
    for feature in roadmap_result["created"]["features"]:
        create_user_story(
            CreateUserStoryInput(
                product_id=product_id,
                feature_id=feature["id"],
                title=f"Story for {feature['title']}",
                description="As a user...",
                acceptance_criteria=None,
                story_points=None,
            )
        )

    # Get details
    result = get_project_details(product_id)

    assert result["success"] is True
    assert result["product"]["name"] == "Full Project"
    assert result["structure"]["themes"] == 1
    assert result["structure"]["epics"] == 2  # noqa: PLR2004
    assert result["structure"]["features"] == 3  # noqa: PLR2004
    assert result["structure"]["user_stories"] == 3  # noqa: PLR2004


def test_get_project_by_name_not_found() -> None:
    """Test finding project by name when it doesn't exist."""
    result = get_project_by_name("Non Existent Project")

    assert result["success"] is False
    assert "not found" in result["error"].lower()


def test_get_project_by_name_found() -> None:
    """Test finding project by name successfully."""
    from tools.db_tools import (  # noqa: PLC0415
        CreateOrGetProductInput,
        create_or_get_product,
    )

    # Create product
    prod_result = create_or_get_product(
        CreateOrGetProductInput(
            product_name="Findable Project", vision=None, description=None
        )
    )
    product_id = prod_result["product_id"]

    # Find it by name
    result = get_project_by_name("Findable Project")

    assert result["success"] is True
    assert result["product_id"] == product_id
    assert result["product_name"] == "Findable Project"


def test_fetch_sprint_candidates_only_refined_todo(session: Session) -> None:
    """Sprint candidates must include only refined, non-superseded TO_DO stories."""
    session.add(Product(product_id=21, name="Sprint Filter Project"))
    session.commit()

    refined_story = UserStory(
        product_id=21,
        title="Refined Story",
        status=StoryStatus.TO_DO,
        rank="2",
        story_points=3,
        is_refined=True,
        is_superseded=False,
        story_origin="refined",
        persona="Document Reviewer",
    )
    non_refined_story = UserStory(
        product_id=21,
        title="Backlog Seed",
        status=StoryStatus.TO_DO,
        rank="1",
        story_points=5,
        is_refined=False,
        is_superseded=False,
        story_origin="backlog_seed",
    )
    superseded_story = UserStory(
        product_id=21,
        title="Superseded Story",
        status=StoryStatus.TO_DO,
        rank="3",
        story_points=3,
        is_refined=True,
        is_superseded=True,
        story_origin="refined",
    )
    done_story = UserStory(
        product_id=21,
        title="Done Story",
        status=StoryStatus.DONE,
        rank="4",
        story_points=2,
        is_refined=True,
        is_superseded=False,
        story_origin="refined",
    )

    session.add(refined_story)
    session.add(non_refined_story)
    session.add(superseded_story)
    session.add(done_story)
    session.commit()

    result = fetch_sprint_candidates(21)

    assert result["success"] is True
    assert result["count"] == 1
    assert result["excluded_counts"] == {
        "non_refined": 1,
        "superseded": 1,
        "open_sprint": 0,
    }
    assert result["stories"][0]["story_title"] == "Refined Story"
    assert result["stories"][0]["priority"] == 2  # noqa: PLR2004
    assert result["stories"][0]["persona"] == "Document Reviewer"


def test_fetch_sprint_candidates_includes_dependency_metadata(
    session: Session,
) -> None:
    """Sprint candidates expose active story dependency metadata."""
    session.add(Product(product_id=22, name="Sprint Dependency Project"))
    prerequisite = UserStory(
        product_id=22,
        title="Capture market data",
        status=StoryStatus.TO_DO,
        rank="101",
        story_points=2,
        is_refined=True,
        is_superseded=False,
        story_origin="refined",
    )
    dependent = UserStory(
        product_id=22,
        title="Generate recommendation",
        status=StoryStatus.TO_DO,
        rank="102",
        story_points=3,
        is_refined=True,
        is_superseded=False,
        story_origin="refined",
    )
    session.add(prerequisite)
    session.add(dependent)
    session.commit()
    session.refresh(prerequisite)
    session.refresh(dependent)
    assert prerequisite.story_id is not None
    assert dependent.story_id is not None
    session.add(
        UserStoryDependency(
            product_id=22,
            dependent_story_id=dependent.story_id,
            prerequisite_story_id=prerequisite.story_id,
            status="active",
        )
    )
    session.commit()

    result = fetch_sprint_candidates(22)

    assert result["success"] is True
    by_title = {story["story_title"]: story for story in result["stories"]}
    assert by_title["Generate recommendation"]["prerequisite_story_ids"] == [
        prerequisite.story_id
    ]
    assert by_title["Generate recommendation"]["blocked_by_story_ids"] == [
        prerequisite.story_id
    ]
    assert by_title["Generate recommendation"]["dependency_status"] == "blocked"
    assert by_title["Capture market data"]["dependency_status"] == "ready"


def test_fetch_sprint_candidates_blocks_active_dependency_cycle(
    session: Session,
) -> None:
    """Sprint candidates fail readiness when active dependency graph cycles."""
    session.add(Product(product_id=23, name="Sprint Cycle Project"))
    first = UserStory(
        product_id=23,
        title="First",
        status=StoryStatus.TO_DO,
        rank="101",
        story_points=2,
        is_refined=True,
        is_superseded=False,
        story_origin="refined",
    )
    second = UserStory(
        product_id=23,
        title="Second",
        status=StoryStatus.TO_DO,
        rank="102",
        story_points=3,
        is_refined=True,
        is_superseded=False,
        story_origin="refined",
    )
    session.add(first)
    session.add(second)
    session.commit()
    session.refresh(first)
    session.refresh(second)
    assert first.story_id is not None
    assert second.story_id is not None
    session.add(
        UserStoryDependency(
            product_id=23,
            dependent_story_id=first.story_id,
            prerequisite_story_id=second.story_id,
            status="active",
        )
    )
    session.add(
        UserStoryDependency(
            product_id=23,
            dependent_story_id=second.story_id,
            prerequisite_story_id=first.story_id,
            status="active",
        )
    )
    session.commit()

    result = fetch_sprint_candidates(23)

    assert result["success"] is True
    assert result["readiness"]["status"] == "blocked"
    assert "STORY_DEPENDENCY_CYCLE" in result["readiness"]["blocking_codes"]
    assert result["readiness"]["dependency_cycle_paths"] == [
        [first.story_id, second.story_id, first.story_id]
    ]


def test_fetch_product_backlog_exposes_refinement_flags(session: Session) -> None:
    """Backlog fetch should expose refinement metadata for diagnostics."""
    session.add(Product(product_id=31, name="Backlog Metadata Project"))
    session.commit()

    session.add(
        UserStory(
            product_id=31,
            title="Seed Story",
            status=StoryStatus.TO_DO,
            rank="1",
            is_refined=False,
            is_superseded=False,
            story_origin="backlog_seed",
            persona=None,
        )
    )
    session.add(
        UserStory(
            product_id=31,
            title="Refined Story",
            status=StoryStatus.TO_DO,
            rank="2",
            is_refined=True,
            is_superseded=False,
            story_origin="refined",
            persona="Data Steward",
        )
    )
    session.commit()

    result = fetch_product_backlog(31)
    assert result["success"] is True
    assert result["count"] == 2  # noqa: PLR2004

    by_title = {story["title"]: story for story in result["stories"]}
    assert by_title["Seed Story"]["is_refined"] is False
    assert by_title["Seed Story"]["story_origin"] == "backlog_seed"
    assert by_title["Refined Story"]["is_refined"] is True
    assert by_title["Refined Story"]["story_origin"] == "refined"
