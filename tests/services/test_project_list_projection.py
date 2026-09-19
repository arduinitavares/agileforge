"""Project-list count correctness and bounded database work."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event
from sqlmodel import Session, col, select

from models.core import Project, Sprint, Team, UserStory
from models.enums import SprintStatus, StoryStatus
from services.read_projections import DurableReadProjectionService
from tests.workflow.planning_fixtures import (
    _record_and_accept_roadmap,
    _record_and_accept_story,
    _seed_accepted_backlog,
)
from tests.workflow.test_planning_transitions import _domain

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_UPDATED_AT = datetime(2026, 9, 19, 12, tzinfo=UTC)
_MAX_LIST_QUERIES = 3


def _seed_story_project(
    engine: Engine,
    *,
    name: str,
    superseded: tuple[bool, ...],
) -> int:
    requirements = tuple(
        f"{name} requirement {index}" for index in range(1, len(superseded) + 1)
    )
    project_id = _seed_accepted_backlog(engine, requirements=requirements)
    domain = _domain(engine)
    _record_and_accept_roadmap(
        domain,
        project_id,
        requirements=requirements,
        idempotency_suffix=f"-{project_id}",
    )
    for index, requirement in enumerate(requirements, start=1):
        _record_and_accept_story(
            engine,
            domain,
            project_id,
            requirement=requirement,
            spec_item_id=f"REQ.planning-{index}",
            backlog_item_id=f"PBI-{index:06d}",
            idempotency_suffix=f"-{project_id}-{index}",
        )
    with Session(engine) as session:
        project = session.get(Project, project_id)
        assert project is not None
        project.name = name
        project.description = None
        project.updated_at = _UPDATED_AT
        stories = session.exec(
            select(UserStory)
            .where(col(UserStory.project_id) == project_id)
            .order_by(col(UserStory.story_id))
        ).all()
        for story, is_superseded in zip(stories, superseded, strict=True):
            story.is_superseded = is_superseded
            story.status = StoryStatus.DONE
        session.commit()
    return project_id


def test_project_list_empty_database(engine: Engine) -> None:
    """An empty installation still returns the complete successful envelope."""
    assert DurableReadProjectionService(engine=engine).project_list() == {
        "ok": True,
        "data": {"items": [], "count": 0},
        "warnings": [],
        "errors": [],
    }


def test_project_list_preserves_counts_order_and_metadata(engine: Engine) -> None:
    """Count current Stories and every Sprint without dropping empty Projects."""
    first_id = _seed_story_project(engine, name="Zulu", superseded=(False, True))
    second_id = _seed_story_project(engine, name="Alpha", superseded=(True,))
    with Session(engine) as session:
        empty = Project(
            name="Empty", description="No child rows", updated_at=_UPDATED_AT
        )
        team = Team(name="Count projection team")
        session.add_all([empty, team])
        session.flush()
        empty_id = empty.project_id
        assert empty_id is not None
        assert team.team_id is not None
        session.add_all(
            [
                Sprint(
                    project_id=first_id,
                    team_id=team.team_id,
                    status=SprintStatus.ACTIVE,
                ),
                Sprint(
                    project_id=first_id,
                    team_id=team.team_id,
                    status=SprintStatus.COMPLETED,
                ),
                Sprint(
                    project_id=second_id,
                    team_id=team.team_id,
                    status=SprintStatus.PLANNED,
                ),
            ]
        )
        session.commit()

    assert DurableReadProjectionService(engine=engine).project_list() == {
        "ok": True,
        "data": {
            "items": [
                {
                    "id": first_id,
                    "project_id": first_id,
                    "name": "Zulu",
                    "description": None,
                    "user_stories_count": 1,
                    "sprint_count": 2,
                    "updated_at": "2026-09-19T12:00:00",
                },
                {
                    "id": second_id,
                    "project_id": second_id,
                    "name": "Alpha",
                    "description": None,
                    "user_stories_count": 0,
                    "sprint_count": 1,
                    "updated_at": "2026-09-19T12:00:00",
                },
                {
                    "id": empty_id,
                    "project_id": empty_id,
                    "name": "Empty",
                    "description": "No child rows",
                    "user_stories_count": 0,
                    "sprint_count": 0,
                    "updated_at": "2026-09-19T12:00:00",
                },
            ],
            "count": 3,
        },
        "warnings": [],
        "errors": [],
    }


@pytest.mark.parametrize("extra_project_count", [0, 10])
def test_project_list_does_not_materialize_children_or_query_per_project(
    engine: Engine, extra_project_count: int
) -> None:
    """Keep count-only reads free of child ORM hydration and N+1 queries."""
    project_id = _seed_story_project(engine, name="Counted", superseded=(False,))
    with Session(engine) as session:
        team = Team(name="Query budget team")
        session.add(team)
        session.flush()
        assert team.team_id is not None
        session.add(Sprint(project_id=project_id, team_id=team.team_id))
        session.add_all(
            Project(name=f"Extra Project {index}")
            for index in range(extra_project_count)
        )
        session.commit()

    child_loads: list[type[object]] = []
    query_count = 0

    def record_load(_session: Session, instance: object) -> None:
        if isinstance(instance, UserStory | Sprint):
            child_loads.append(type(instance))

    def record_query(*_args: object) -> None:
        nonlocal query_count
        query_count += 1

    event.listen(Session, "loaded_as_persistent", record_load)
    event.listen(engine, "before_cursor_execute", record_query)
    try:
        result = DurableReadProjectionService(engine=engine).project_list()
    finally:
        event.remove(engine, "before_cursor_execute", record_query)
        event.remove(Session, "loaded_as_persistent", record_load)

    assert result["ok"] is True
    assert query_count <= _MAX_LIST_QUERIES
    assert child_loads == []
