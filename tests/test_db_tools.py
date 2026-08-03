# tests/test_db_tools.py
"""
Test suite for db_tools module using TDD approach.

Run with: pytest tests/test_db_tools.py -v.
"""

# Monkey-patch the engine for tests
import sys
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from agile_sqlmodel import Project, Task, UserStory
from models.core import Epic, Feature, Theme
from tools.db_tools import (
    CreateOrGetProjectInput,
    CreateTaskInput,
    CreateUserStoryInput,
    create_or_get_project,
    create_task,
    create_user_story,
    persist_roadmap,
)

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_create_project_new(engine: Engine) -> None:
    """Test creating a new Project."""
    del engine

    result = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Test Project",
            vision="To revolutionize testing",
            description=None,
        )
    )

    assert result["success"] is True
    assert result["action"] == "created"
    assert result["project_id"] == 1
    assert "Test Project" in result["message"]


def test_create_project_existing(engine: Engine) -> None:
    """Test getting an existing Project without duplication."""
    # Create first Project
    result1 = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Existing Project", vision=None, description=None
        )
    )
    assert result1["action"] == "created"

    # Get same Project again
    result2 = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Existing Project", vision=None, description=None
        )
    )
    assert result2["action"] == "updated"
    assert result2["project_id"] == result1["project_id"]

    # Verify only one Project exists
    with Session(engine) as session:
        projects = session.exec(select(Project)).all()
        assert len(projects) == 1


def test_persist_roadmap(engine: Engine) -> None:
    """Test persisting a roadmap hierarchy."""
    # Create Project first
    project_result = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Roadmap Project", vision=None, description=None
        )
    )
    project_id = project_result["project_id"]

    # Define roadmap
    roadmap = [
        {
            "quarter": "Q1",
            "theme_title": "Authentication",
            "theme_description": "User identity and access",
            "epics": [
                {
                    "epic_title": "Login System",
                    "epic_summary": "Email and OAuth login",
                    "features": [
                        {"title": "Email Login", "description": "Basic email/password"},
                        {"title": "OAuth 2.0", "description": "Third-party login"},
                    ],
                }
            ],
        }
    ]

    # Persist roadmap
    result = persist_roadmap(project_id, roadmap)

    assert result["success"] is True
    created = result.get("created")
    assert created is not None
    assert created["themes"][0]["id"] == 1
    assert len(created["epics"]) == 1
    expected_feature_count = 2
    assert len(created["features"]) == expected_feature_count

    # Verify hierarchy in database
    with Session(engine) as session:
        themes = session.exec(select(Theme)).all()
        assert len(themes) == 1
        assert themes[0].project_id == project_id

        epics = session.exec(select(Epic)).all()
        assert len(epics) == 1
        assert epics[0].theme_id == themes[0].theme_id

        features = session.exec(select(Feature)).all()
        assert len(features) == 2  # noqa: PLR2004


def test_create_user_story(engine: Engine) -> None:
    """Test creating a user story under a feature."""
    # Setup hierarchy
    project_result = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Story Project", vision=None, description=None
        )
    )
    project_id = project_result["project_id"]

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

    roadmap_result = persist_roadmap(project_id, roadmap)
    created = roadmap_result.get("created")
    assert created is not None
    feature_id = created["features"][0]["id"]

    # Create user story
    story_result = create_user_story(
        CreateUserStoryInput(
            project_id=project_id,
            feature_id=feature_id,
            title="Login with email",
            description="As a user, I want to log in with email and password.",
            acceptance_criteria="User can enter email/password and be authenticated.",
            story_points=5,
        )
    )

    assert story_result["success"] is True
    assert story_result.get("story_id") == 1
    assert story_result.get("feature_id") == feature_id

    # Verify in database
    with Session(engine) as session:
        stories = session.exec(select(UserStory)).all()
        assert len(stories) == 1
        assert stories[0].story_points == 5  # noqa: PLR2004


def test_create_task(engine: Engine) -> None:
    """Test creating a task under a story."""
    # Setup full hierarchy
    project_result = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Task Project", vision=None, description=None
        )
    )
    project_id = project_result["project_id"]

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

    roadmap_result = persist_roadmap(project_id, roadmap)
    created = roadmap_result.get("created")
    assert created is not None
    feature_id = created["features"][0]["id"]

    story_result = create_user_story(
        CreateUserStoryInput(
            project_id=project_id,
            feature_id=feature_id,
            title="Login with email",
            description="As a user, I want to log in.",
            acceptance_criteria=None,
            story_points=None,
        )
    )
    story_id = story_result.get("story_id")
    assert story_id is not None

    # Create task
    task_result = create_task(
        CreateTaskInput(
            story_id=story_id,
            title="Set up email validation",
            description="Implement email regex validation",
        )
    )

    assert task_result["success"] is True
    assert task_result.get("task_id") == 1

    # Verify in database
    with Session(engine) as session:
        tasks = session.exec(select(Task)).all()
        assert len(tasks) == 1


def test_query_project_structure(engine: Engine) -> None:
    """Test querying full Project structure."""
    del engine

    from tools.db_tools import (  # noqa: PLC0415
        CreateOrGetProjectInput,
        CreateUserStoryInput,
        create_or_get_project,
        create_user_story,
        persist_roadmap,
        query_project_structure,
    )

    # Setup hierarchy
    project_result = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Query Project",
            vision="Test vision statement",
            description=None,
        )
    )
    project_id = project_result["project_id"]

    roadmap = [
        {
            "quarter": "Q1",
            "theme_title": "Auth",
            "theme_description": "Authentication features",
            "epics": [
                {
                    "epic_title": "Login",
                    "epic_summary": "Login implementation",
                    "features": [
                        {"title": "Email Login", "description": "Email auth"},
                    ],
                }
            ],
        }
    ]

    roadmap_result = persist_roadmap(project_id, roadmap)
    created = roadmap_result.get("created")
    assert created is not None
    feature_id = created["features"][0]["id"]

    create_user_story(
        CreateUserStoryInput(
            project_id=project_id,
            feature_id=feature_id,
            title="User can login",
            description="As a user...",
            story_points=5,
            acceptance_criteria=None,
        )
    )

    # Query structure
    result = query_project_structure(project_id)

    assert result["success"] is True
    structure = result.get("structure")
    assert structure is not None
    assert structure["project"]["name"] == "Query Project"
    assert structure["project"]["vision"] == "Test vision statement"
    assert len(structure["themes"]) == 1
    assert len(structure["themes"][0]["epics"]) == 1
    assert len(structure["themes"][0]["epics"][0]["features"]) == 1
    assert (
        len(structure["themes"][0]["epics"][0]["features"][0]["stories"]) == 1
    )


def test_get_story_details(engine: Engine) -> None:
    """Test fetching details for a specific story by ID."""
    del engine

    # Arrange: Create a Project and story
    project_result = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Story Details Test Project",
            vision="Test vision for story details",
            description=None,
        )
    )
    project_id = project_result["project_id"]

    # Create roadmap structure
    roadmap = [
        {
            "theme_title": "Feature Theme",
            "theme_description": "Theme for testing story details",
            "epics": [
                {
                    "epic_title": "Test Epic",
                    "epic_summary": "Epic for testing",
                    "features": [
                        {
                            "title": "Test Feature",
                            "description": "Feature for testing story details",
                        },
                    ],
                }
            ],
        }
    ]

    roadmap_result = persist_roadmap(project_id, roadmap)
    created = roadmap_result.get("created")
    assert created is not None
    feature_id = created["features"][0]["id"]

    # Create a test story
    expected_story_points = 3
    story_result = create_user_story(
        CreateUserStoryInput(
            project_id=project_id,
            feature_id=feature_id,
            title="Test Story for Details",
            description="As a tester, I want to retrieve story details so that I can verify the functionality.",  # noqa: E501
            story_points=expected_story_points,
            acceptance_criteria="- Story details can be fetched\n- All fields are returned correctly",  # noqa: E501
        )
    )
    story_id = story_result.get("story_id")
    assert story_id is not None

    # Act: Call the get_story_details function
    from tools.db_tools import get_story_details  # noqa: PLC0415

    result = get_story_details(story_id)

    # Assert: Verify the returned details
    assert result["success"] is True
    assert result["story_id"] == story_id
    assert result.get("title") == "Test Story for Details"
    assert (
        result.get("description")
        == "As a tester, I want to retrieve story details so that I can verify the functionality."  # noqa: E501
    )
    assert (
        result.get("acceptance_criteria")
        == "- Story details can be fetched\n- All fields are returned correctly"
    )
    assert result.get("status") == "To Do"  # StoryStatus enum value
    assert result.get("story_points") == expected_story_points
    assert result.get("feature_id") == feature_id
    assert result.get("project_id") == project_id
    assert "created_at" in result
    assert "updated_at" in result


def test_get_story_details_not_found(engine: Engine) -> None:
    """Test fetching details for a non-existent story."""
    del engine

    # Act: Try to fetch a story that doesn't exist
    from tools.db_tools import get_story_details  # noqa: PLC0415

    result = get_story_details(999999)

    # Assert: Verify the error message
    assert result["success"] is False
    message = result.get("message")
    assert message is not None
    assert "not found" in message.lower()
    assert result["story_id"] == 999999  # noqa: PLR2004
