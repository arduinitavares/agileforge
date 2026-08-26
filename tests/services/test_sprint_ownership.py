"""Provider-free Sprint ownership resolution and durable evidence tests."""

from collections.abc import Iterable

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from models.core import Project, ProjectTeam, Team
from services.sprint_ownership import (
    SprintOwnerResolutionError,
    resolve_sprint_owner,
)
from workflow.contracts import WorkflowErrorCode


def _add_project(session: Session, name: str) -> Project:
    project = Project(name=name)
    session.add(project)
    session.flush()
    assert project.project_id is not None
    return project


def _solo_label(project_id: int, project_name: str) -> str:
    return (
        "[agileforge:sprint-owner:solo-project:v1:project:"
        f"{project_id}] Solo operator for {project_name}"
    )


def _project_id(project: Project) -> int:
    project_id = project.project_id
    assert project_id is not None
    return project_id


def _ids(projects: Iterable[Project]) -> tuple[int, ...]:
    return tuple(_project_id(item) for item in projects)


def test_omission_resolves_canonical_project_scoped_solo_owner(
    engine: Engine,
) -> None:
    """Omission resolves one stable Project role without external identity."""
    with Session(engine) as session:
        project = _add_project(session, "AgileForge")
        session.commit()
        project_id = project.project_id
        assert project_id is not None

        owner = resolve_sprint_owner(
            session,
            project_id=project_id,
            team_name=None,
        )

    assert owner.kind == "solo_project"
    assert owner.key == (
        f"agileforge:sprint-owner:solo-project:v1:project:{project_id}"
    )
    assert owner.label == (
        "[agileforge:sprint-owner:solo-project:v1:project:"
        f"{project_id}] Solo operator for AgileForge"
    )


@pytest.mark.parametrize("team_name", [None, "Platform"])
def test_missing_project_fails_before_default_or_named_owner_resolution(
    engine: Engine,
    team_name: str | None,
) -> None:
    """Every ownership path starts from the exact durable Project."""
    with Session(engine) as session, pytest.raises(
        SprintOwnerResolutionError
    ) as captured:
        resolve_sprint_owner(
            session,
            project_id=999_999,
            team_name=team_name,
        )

    assert captured.value.code is WorkflowErrorCode.PROJECT_NOT_FOUND


def test_explicit_named_team_override_is_trimmed_and_preserved(
    engine: Engine,
) -> None:
    """A real named Team remains an exact explicit per-plan override."""
    with Session(engine) as session:
        project = _add_project(session, "Named ownership")

        owner = resolve_sprint_owner(
            session,
            project_id=_project_id(project),
            team_name="  Platform  ",
        )

    assert owner.kind == "named_team"
    assert owner.key == (
        "agileforge:sprint-owner:named-team:v1:sha256:"
        "c78ffe19571018fbc93a78873969046fe1a80c3d21e5d20fbb2ca17c7c53a144"
    )
    assert owner.label == "Platform"


@pytest.mark.parametrize(
    "team_name",
    [
        "[agileforge:sprint-owner:spoof] Team",
        " [AGILEFORGE:SPRINT-OWNER:future:v9] Team ",
    ],
)
def test_reserved_namespace_override_fails_before_attempt(
    engine: Engine,
    team_name: str,
) -> None:
    """Explicit callers cannot spoof any current or future owner namespace."""
    with Session(engine) as session:
        project = _add_project(session, "Reserved override")

        with pytest.raises(SprintOwnerResolutionError) as captured:
            resolve_sprint_owner(
                session,
                project_id=_project_id(project),
                team_name=team_name,
            )

    assert captured.value.code is WorkflowErrorCode.SPRINT_OWNER_CONFLICT


@pytest.mark.parametrize(
    "project_name",
    ["", "   ", "Line\nbreak", "Control\x00character", "x" * 201],
)
def test_malformed_default_project_identity_fails_closed(
    engine: Engine,
    project_name: str,
) -> None:
    """Malformed durable Project display state never invents solo ownership."""
    with Session(engine) as session:
        project = _add_project(session, project_name)

        with pytest.raises(SprintOwnerResolutionError) as captured:
            resolve_sprint_owner(
                session,
                project_id=_project_id(project),
                team_name=None,
            )

    assert captured.value.code is WorkflowErrorCode.SPRINT_OWNER_UNAVAILABLE


def test_format_characters_in_meaningful_project_name_remain_valid(
    engine: Engine,
) -> None:
    """Unicode formatting used by a visible name is not a control character."""
    with Session(engine) as session:
        project = _add_project(session, "Project 👩‍💻")

        owner = resolve_sprint_owner(
            session,
            project_id=_project_id(project),
            team_name=None,
        )

    assert owner.label.endswith("Solo operator for Project 👩‍💻")


@pytest.mark.parametrize(
    ("link_targets", "succeeds"),
    [
        ((), False),
        (("current",), True),
        (("other",), False),
        (("other", "another_other"), False),
        (("current", "other"), False),
    ],
)
def test_reserved_solo_team_must_be_exclusive_to_encoded_project(
    engine: Engine,
    link_targets: tuple[str, ...],
    succeeds: bool,
) -> None:
    """Preflight rejects orphaned, foreign, and shared reserved Team rows."""
    with Session(engine) as session:
        current = _add_project(session, "Current")
        other = _add_project(session, "Other")
        another_other = _add_project(session, "Another other")
        current_id, other_id, another_other_id = _ids(
            (current, other, another_other)
        )
        team = Team(name=_solo_label(current_id, current.name))
        session.add(team)
        session.flush()
        assert team.team_id is not None
        target_ids = {
            "current": current_id,
            "other": other_id,
            "another_other": another_other_id,
        }
        for target in link_targets:
            session.add(
                ProjectTeam(project_id=target_ids[target], team_id=team.team_id)
            )
        session.commit()
        before_teams = tuple(session.exec(select(Team)).all())
        before_links = tuple(session.exec(select(ProjectTeam)).all())

        if succeeds:
            owner = resolve_sprint_owner(
                session,
                project_id=current_id,
                team_name=None,
            )
            assert owner.label == team.name
        else:
            with pytest.raises(SprintOwnerResolutionError) as captured:
                resolve_sprint_owner(
                    session,
                    project_id=current_id,
                    team_name=None,
                )
            assert captured.value.code is WorkflowErrorCode.SPRINT_OWNER_CONFLICT

        assert tuple(session.exec(select(Team)).all()) == before_teams
        assert tuple(session.exec(select(ProjectTeam)).all()) == before_links
