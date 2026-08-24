"""Core SQLModel classes for AgileForge projects and delivery work."""

from datetime import UTC, date, datetime
from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING, ClassVar

from sqlalchemy import func
from sqlalchemy.schema import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.types import Date, Text
from sqlmodel import Field, Relationship, SQLModel

from models.enums import (
    SprintStatus,
    StoryResolution,
    StoryStatus,
    TaskStatus,
    TeamRole,
    TimeFrame,
)

if TYPE_CHECKING:
    from models.specs import SpecRegistry


class TeamMembership(SQLModel, table=True):
    """Link table for Team <-> TeamMember."""

    __tablename__ = "team_memberships"  # type: ignore[assignment]
    team_id: int = Field(foreign_key="teams.team_id", primary_key=True)
    member_id: int = Field(foreign_key="team_members.member_id", primary_key=True)
    role: TeamRole = Field(default=TeamRole.DEVELOPER, nullable=False)


class ProjectTeam(SQLModel, table=True):
    """Link table for Project <-> Team."""

    __tablename__: ClassVar[str] = "project_teams"
    project_id: int = Field(foreign_key="projects.project_id", primary_key=True)
    team_id: int = Field(foreign_key="teams.team_id", primary_key=True)


class Project(SQLModel, table=True):
    """A top-level AgileForge project."""

    __tablename__: ClassVar[str] = "projects"
    __table_args__ = (
        ForeignKeyConstraint(
            ["active_repository_binding_id"],
            ["repository_bindings.repository_binding_id"],
            name="fk_project_active_repository_binding",
            ondelete="SET NULL",
            use_alter=True,
        ),
    )
    project_id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    description: str | None = Field(default=None, sa_type=Text)
    active_repository_binding_id: int | None = Field(
        default=None,
        index=True,
    )

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={"server_default": func.now()},
        nullable=False,
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={
            "server_default": func.now(),
            "onupdate": func.now(),
        },
        nullable=False,
    )

    teams: list["Team"] = Relationship(
        back_populates="projects", link_model=ProjectTeam
    )
    themes: list["Theme"] = Relationship(back_populates="project")
    stories: list["UserStory"] = Relationship(back_populates="project")
    sprints: list["Sprint"] = Relationship(back_populates="project")
    personas: list["ProjectPersona"] = Relationship(back_populates="project")
    spec_versions: list["SpecRegistry"] = Relationship(back_populates="project")


class Team(SQLModel, table=True):
    """A stable group of members."""

    __tablename__ = "teams"  # type: ignore[assignment]
    team_id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={"server_default": func.now()},
        nullable=False,
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={
            "server_default": func.now(),
            "onupdate": func.now(),
        },
        nullable=False,
    )

    projects: list["Project"] = Relationship(
        back_populates="teams", link_model=ProjectTeam
    )
    members: list["TeamMember"] = Relationship(
        back_populates="teams", link_model=TeamMembership
    )
    sprints: list["Sprint"] = Relationship(back_populates="team")


class TeamMember(SQLModel, table=True):
    """An individual member of a team."""

    __tablename__ = "team_members"  # type: ignore[assignment]
    member_id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str = Field(unique=True, index=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={"server_default": func.now()},
        nullable=False,
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={
            "server_default": func.now(),
            "onupdate": func.now(),
        },
        nullable=False,
    )

    teams: list["Team"] = Relationship(
        back_populates="members", link_model=TeamMembership
    )
    tasks: list["Task"] = Relationship(back_populates="assignee")


class SprintStory(SQLModel, table=True):
    """Link table for Sprint <-> UserStory."""

    __tablename__ = "sprint_stories"  # type: ignore[assignment]
    sprint_id: int = Field(foreign_key="sprints.sprint_id", primary_key=True)
    story_id: int = Field(foreign_key="user_stories.story_id", primary_key=True)
    added_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={"server_default": func.now()},
        nullable=False,
    )


class Sprint(SQLModel, table=True):
    """A time-boxed iteration of work for a team."""

    __tablename__ = "sprints"  # type: ignore[assignment]
    sprint_id: int | None = Field(default=None, primary_key=True)
    goal: str | None = Field(default=None, sa_type=Text)
    start_date: date | None = Field(default=None, sa_type=Date, nullable=True)
    end_date: date | None = Field(default=None, sa_type=Date, nullable=True)
    status: SprintStatus = Field(default=SprintStatus.PLANNED, nullable=False)
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)
    close_snapshot_json: str | None = Field(default=None, sa_type=Text)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={"server_default": func.now()},
        nullable=False,
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={
            "server_default": func.now(),
            "onupdate": func.now(),
        },
        nullable=False,
    )

    project_id: int = Field(foreign_key="projects.project_id")
    team_id: int = Field(foreign_key="teams.team_id")

    project: "Project" = Relationship(back_populates="sprints")
    team: "Team" = Relationship(back_populates="sprints")
    stories: list["UserStory"] = Relationship(
        back_populates="sprints", link_model=SprintStory
    )


class Theme(SQLModel, table=True):
    """A high-level strategic goal."""

    __tablename__ = "themes"  # type: ignore[assignment]
    __table_args__ = (UniqueConstraint("project_id", "title"),)

    theme_id: int | None = Field(default=None, primary_key=True)
    title: str
    description: str | None = Field(default=None, sa_type=Text)
    time_frame: TimeFrame | None = Field(default=None)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={"server_default": func.now()},
        nullable=False,
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={
            "server_default": func.now(),
            "onupdate": func.now(),
        },
        nullable=False,
    )

    project_id: int = Field(foreign_key="projects.project_id")

    project: "Project" = Relationship(back_populates="themes")
    epics: list["Epic"] = Relationship(back_populates="theme")


class Epic(SQLModel, table=True):
    """A large body of work contributing to a theme."""

    __tablename__ = "epics"  # type: ignore[assignment]
    epic_id: int | None = Field(default=None, primary_key=True)
    title: str
    summary: str | None = Field(default=None, sa_type=Text)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={"server_default": func.now()},
        nullable=False,
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={
            "server_default": func.now(),
            "onupdate": func.now(),
        },
        nullable=False,
    )

    theme_id: int = Field(foreign_key="themes.theme_id")

    theme: "Theme" = Relationship(back_populates="epics")
    features: list["Feature"] = Relationship(back_populates="epic")


class Feature(SQLModel, table=True):
    """A component or part of an epic."""

    __tablename__ = "features"  # type: ignore[assignment]
    feature_id: int | None = Field(default=None, primary_key=True)
    title: str
    description: str | None = Field(default=None, sa_type=Text)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={"server_default": func.now()},
        nullable=False,
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={
            "server_default": func.now(),
            "onupdate": func.now(),
        },
        nullable=False,
    )

    epic_id: int = Field(foreign_key="epics.epic_id")

    epic: "Epic" = Relationship(back_populates="features")


class UserStory(SQLModel, table=True):
    """Operational projection of one accepted immutable Story item."""

    __tablename__ = "user_stories"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "source_story_artifact_id",
            "source_story_item_id",
            name="uq_user_story_artifact_item",
        ),
        ForeignKeyConstraint(
            ["project_id", "accepted_spec_version_id", "accepted_spec_hash"],
            [
                "spec_registry.project_id",
                "spec_registry.spec_version_id",
                "spec_registry.spec_hash",
            ],
            name="fk_user_story_specification",
        ),
        ForeignKeyConstraint(
            [
                "project_id",
                "source_story_artifact_id",
                "source_story_artifact_fingerprint",
            ],
            [
                "story_artifacts.project_id",
                "story_artifacts.story_artifact_id",
                "story_artifacts.content_fingerprint",
            ],
            name="fk_user_story_artifact",
        ),
    )

    story_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    source_story_artifact_id: int = Field(index=True)
    source_story_artifact_fingerprint: str = Field(index=True)
    source_story_item_id: str = Field(index=True)
    source_story_item_fingerprint: str = Field(index=True)
    accepted_spec_version_id: int = Field(index=True)
    accepted_spec_hash: str = Field(index=True)
    spec_item_ids_json: str = Field(sa_type=Text)
    title: str
    story_description: str = Field(sa_type=Text)
    acceptance_criteria_json: str = Field(sa_type=Text)
    persona: str = Field(max_length=100, index=True)
    status: StoryStatus = Field(default=StoryStatus.TO_DO, nullable=False)
    story_points: int | None = Field(default=None)
    rank: str | None = Field(default=None, index=True)
    is_superseded: bool = Field(
        default=False,
        nullable=False,
        description="Accepted replacement exists in the same Story chain.",
    )
    resolution: StoryResolution | None = Field(default=None)
    completion_notes: str | None = Field(default=None, sa_type=Text)
    evidence_links: str | None = Field(default=None, sa_type=Text)
    completed_at: datetime | None = Field(default=None)
    validation_evidence: str | None = Field(
        default=None,
        sa_type=Text,
        description="Canonical validation evidence for explicit readiness checks.",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={"server_default": func.now()},
        nullable=False,
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={
            "server_default": func.now(),
            "onupdate": func.now(),
        },
        nullable=False,
    )

    project: "Project" = Relationship(back_populates="stories")
    sprints: list["Sprint"] = Relationship(
        back_populates="stories", link_model=SprintStory
    )
    tasks: list["Task"] = Relationship(
        back_populates="story",
        sa_relationship_kwargs={"cascade": "all, delete"},
    )


class UserStoryDependency(SQLModel, table=True):
    """Reviewable dependency edge between two active user stories."""

    __tablename__ = "user_story_dependencies"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "dependent_story_id",
            "prerequisite_story_id",
            name="unique_user_story_dependency_edge",
        ),
        CheckConstraint(
            "dependent_story_id <> prerequisite_story_id",
            name="ck_user_story_dependencies_not_self",
        ),
        CheckConstraint(
            "status IN ('proposed', 'active', 'rejected')",
            name="ck_user_story_dependencies_status",
        ),
        CheckConstraint(
            "source IN ('story_writer', 'dependency_repair', 'manual_review')",
            name="ck_user_story_dependencies_source",
        ),
        CheckConstraint(
            "confidence IN ('explicit', 'inferred', 'reviewed')",
            name="ck_user_story_dependencies_confidence",
        ),
    )

    dependency_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    dependent_story_id: int = Field(foreign_key="user_stories.story_id", index=True)
    prerequisite_story_id: int = Field(foreign_key="user_stories.story_id", index=True)
    status: str = Field(default="proposed", index=True, nullable=False)
    source: str = Field(default="story_writer", index=True, nullable=False)
    confidence: str = Field(default="inferred", index=True, nullable=False)
    reason: str | None = Field(default=None, sa_type=Text)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={"server_default": func.now()},
        nullable=False,
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={
            "server_default": func.now(),
            "onupdate": func.now(),
        },
        nullable=False,
    )


class Task(SQLModel, table=True):
    """A granular sub-task for a user story."""

    __tablename__ = "tasks"  # type: ignore[assignment]
    task_id: int | None = Field(default=None, primary_key=True)
    description: str = Field(sa_type=Text)
    metadata_json: str = Field(sa_type=Text)
    status: TaskStatus = Field(default=TaskStatus.TO_DO, nullable=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={"server_default": func.now()},
        nullable=False,
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={
            "server_default": func.now(),
            "onupdate": func.now(),
        },
        nullable=False,
    )

    story_id: int = Field(foreign_key="user_stories.story_id")
    assigned_to_member_id: int | None = Field(
        default=None, foreign_key="team_members.member_id"
    )

    story: "UserStory" = Relationship(back_populates="tasks")
    assignee: TeamMember | None = Relationship(back_populates="tasks")


class ProjectPersona(SQLModel, table=True):
    """Approved personas for a Project."""

    __tablename__: ClassVar[str] = "project_personas"

    persona_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id")
    persona_name: str = Field(max_length=100, nullable=False)
    is_default: bool = Field(default=False)
    category: str = Field(max_length=50, default="primary_user")
    description: str | None = Field(default=None, sa_type=Text)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={"server_default": func.now()},
        nullable=False,
    )

    project: "Project" = Relationship(back_populates="personas")

    __table_args__ = (
        UniqueConstraint("project_id", "persona_name", name="unique_project_persona"),
    )


_PRODUCT_DEFINITION_MODELS: ModuleType = import_module("models.product_definition")
_SPEC_MODELS: ModuleType = import_module("models.specs")
