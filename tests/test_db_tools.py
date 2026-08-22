# tests/test_db_tools.py
"""Provider-free tests for the retained Project hierarchy tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlmodel import Session, select

from agile_sqlmodel import Project
from models.core import Epic, Feature, Theme
from models.product_definition import VisionInterviewTurn
from tests.vision_lineage_fixtures import seed_accepted_vision
from tools.db_tools import (
    CreateOrGetProjectInput,
    QueryProjectStructureFailure,
    QueryProjectStructureSuccess,
    create_or_get_project,
    persist_roadmap,
    query_project_structure,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


EXPECTED_FEATURE_COUNT = 2


def _created_roadmap_count(result: object, kind: str) -> int:
    """Read one successful roadmap collection without exporting a test-only type."""
    assert isinstance(result, dict)
    result_mapping = cast("dict[str, object]", result)
    assert result_mapping["success"] is True
    created = result_mapping["created"]
    assert isinstance(created, dict)
    created_mapping = cast("dict[str, object]", created)
    entries = created_mapping[kind]
    assert isinstance(entries, list)
    return len(entries)


def _roadmap() -> list[dict[str, object]]:
    return [
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


def test_create_project_new(engine: Engine) -> None:
    """Create one Project through the retained project tool."""
    del engine

    result = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Test Project",
            vision="To revolutionize testing",
            description=None,
        )
    )

    assert result == {
        "success": True,
        "project_id": 1,
        "action": "created",
        "message": "Created project 'Test Project' with ID 1",
    }


def test_create_project_existing(engine: Engine) -> None:
    """Keep the Project tool idempotent by name."""
    first = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Existing Project", vision=None, description=None
        )
    )
    second = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Existing Project", vision=None, description=None
        )
    )

    assert first["action"] == "created"
    assert second["action"] == "updated"
    assert second["project_id"] == first["project_id"]
    with Session(engine) as session:
        assert len(session.exec(select(Project)).all()) == 1


def test_persist_roadmap_retains_only_theme_epic_feature_hierarchy(
    engine: Engine,
) -> None:
    """Persist the honest hierarchy without a false Feature-to-Story edge."""
    project_id = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Roadmap Project", vision=None, description=None
        )
    )["project_id"]

    result = persist_roadmap(project_id, _roadmap())

    assert result["success"] is True
    assert _created_roadmap_count(result, "themes") == 1
    assert _created_roadmap_count(result, "epics") == 1
    assert _created_roadmap_count(result, "features") == EXPECTED_FEATURE_COUNT
    with Session(engine) as session:
        assert len(session.exec(select(Theme)).all()) == 1
        assert len(session.exec(select(Epic)).all()) == 1
        assert len(session.exec(select(Feature)).all()) == EXPECTED_FEATURE_COUNT


def test_query_project_structure_returns_only_current_hierarchy(engine: Engine) -> None:
    """Expose exactly Theme, Epic, and Feature records for a Project."""
    project_id = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Query Project",
            vision="Test vision statement",
            description=None,
        )
    )["project_id"]
    with Session(engine) as session:
        seed_accepted_vision(
            session,
            project_id=project_id,
            statement="Test vision statement",
        )
    persist_roadmap(project_id, _roadmap())

    result = query_project_structure(project_id)

    assert result["success"] is True
    success = cast("QueryProjectStructureSuccess", result)
    structure = success["structure"]
    assert structure["project"]["name"] == "Query Project"
    assert structure["project"]["vision"] == "Test vision statement"
    feature = structure["themes"][0]["epics"][0]["features"][0]
    assert feature == {"id": 1, "title": "Email Login"}


def test_query_project_structure_fails_closed_on_ambiguous_vision(
    engine: Engine,
) -> None:
    """Reject two accepted Vision roots instead of selecting one arbitrarily."""
    project_id = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Ambiguous Vision Project", vision=None, description=None
        )
    )["project_id"]
    with Session(engine) as session:
        seed_accepted_vision(session, project_id=project_id, statement="First root.")
        seed_accepted_vision(
            session,
            project_id=project_id,
            statement="Second root.",
            version_number=2,
        )

    result = query_project_structure(project_id)

    assert result["success"] is False
    failure = cast("QueryProjectStructureFailure", result)
    assert "Vision lineage is invalid" in failure["error"]


def test_query_project_structure_fails_closed_on_corrupt_vision_source_turn(
    engine: Engine,
) -> None:
    """Expose corrupt durable Vision source evidence through the tool envelope."""
    project_id = create_or_get_project(
        CreateOrGetProjectInput(
            project_name="Corrupt Vision Source Project", vision=None, description=None
        )
    )["project_id"]
    with Session(engine) as session:
        artifact = seed_accepted_vision(
            session,
            project_id=project_id,
            statement="Trust immutable Vision evidence.",
        )
        turn = session.get(VisionInterviewTurn, artifact.source_interview_turn_id)
        assert turn is not None
        turn.output_fingerprint = "corrupt"
        session.add(turn)
        session.commit()

    result = query_project_structure(project_id)

    assert result["success"] is False
    failure = cast("QueryProjectStructureFailure", result)
    assert "Vision lineage is invalid" in failure["error"]
