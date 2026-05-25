"""Core SQLModel classes extracted from the legacy shim."""

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func
from sqlalchemy.schema import CheckConstraint, UniqueConstraint
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
from utils.task_metadata import canonical_task_metadata_json

if TYPE_CHECKING:
    from models.specs import SpecRegistry


class TeamMembership(SQLModel, table=True):
    """Link table for Team <-> TeamMember."""

    __tablename__ = "team_memberships"  # type: ignore[assignment]
    team_id: int = Field(foreign_key="teams.team_id", primary_key=True)
    member_id: int = Field(foreign_key="team_members.member_id", primary_key=True)
    role: TeamRole = Field(default=TeamRole.DEVELOPER, nullable=False)


class ProductTeam(SQLModel, table=True):
    """Link table for Product <-> Team."""

    __tablename__ = "product_teams"  # type: ignore[assignment]
    product_id: int = Field(foreign_key="products.product_id", primary_key=True)
    team_id: int = Field(foreign_key="teams.team_id", primary_key=True)


class Product(SQLModel, table=True):
    """A top-level product container."""

    __tablename__ = "products"  # type: ignore[assignment]
    product_id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    description: str | None = Field(default=None, sa_type=Text)
    vision: str | None = Field(default=None, sa_type=Text)
    roadmap: str | None = Field(default=None, sa_type=Text)

    technical_spec: str | None = Field(
        default=None,
        sa_type=Text,
    )
    compiled_authority_json: str | None = Field(
        default=None,
        sa_type=Text,
        description="Latest compiled spec authority JSON artifact (cached)",
    )
    spec_file_path: str | None = Field(
        default=None,
        description="Path to original spec file or generated backup file",
    )
    spec_loaded_at: datetime | None = Field(
        default=None,
        description="When the specification was saved to this product",
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
        back_populates="products", link_model=ProductTeam
    )
    themes: list["Theme"] = Relationship(back_populates="product")
    stories: list["UserStory"] = Relationship(back_populates="product")
    sprints: list["Sprint"] = Relationship(back_populates="product")
    personas: list["ProductPersona"] = Relationship(back_populates="product")
    spec_versions: list["SpecRegistry"] = Relationship(back_populates="product")


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

    products: list["Product"] = Relationship(
        back_populates="teams", link_model=ProductTeam
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
    start_date: date = Field(sa_type=Date)
    end_date: date = Field(sa_type=Date)
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

    product_id: int = Field(foreign_key="products.product_id")
    team_id: int = Field(foreign_key="teams.team_id")

    product: "Product" = Relationship(back_populates="sprints")
    team: "Team" = Relationship(back_populates="sprints")
    stories: list["UserStory"] = Relationship(
        back_populates="sprints", link_model=SprintStory
    )


class Theme(SQLModel, table=True):
    """A high-level strategic goal."""

    __tablename__ = "themes"  # type: ignore[assignment]
    __table_args__ = (UniqueConstraint("product_id", "title"),)

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

    product_id: int = Field(foreign_key="products.product_id")

    product: "Product" = Relationship(back_populates="themes")
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
    stories: list["UserStory"] = Relationship(back_populates="feature")


class UserStory(SQLModel, table=True):
    """A single item in the product backlog."""

    __tablename__ = "user_stories"  # type: ignore[assignment]
    story_id: int | None = Field(default=None, primary_key=True)
    title: str
    story_description: str | None = Field(default=None, sa_type=Text)
    acceptance_criteria: str | None = Field(default=None, sa_type=Text)
    status: StoryStatus = Field(default=StoryStatus.TO_DO, nullable=False)
    story_points: int | None = Field(default=None)
    rank: str | None = Field(default=None, index=True)  # For ordering
    source_requirement: str | None = Field(
        default=None,
        index=True,
        description="Normalized parent requirement key for refinement linkage",
    )
    refinement_slot: int | None = Field(
        default=None,
        index=True,
        description="1-based deterministic slot inside requirement decomposition",
    )
    story_origin: str | None = Field(
        default=None,
        description="Origin marker: backlog_seed or refined",
    )
    is_refined: bool = Field(
        default=False,
        nullable=False,
        description="True once story has been refined with final AC content",
    )
    is_superseded: bool = Field(
        default=False,
        nullable=False,
        description="Soft supersede marker for duplicate legacy rows",
    )
    superseded_by_story_id: int | None = Field(
        default=None,
        foreign_key="user_stories.story_id",
        description="Canonical replacement story when this row is superseded",
    )

    persona: str | None = Field(
        default=None,
        max_length=100,
        index=True,
        description="Extracted from 'As a [persona], I want...' format",
    )

    resolution: StoryResolution | None = Field(default=None)
    completion_notes: str | None = Field(default=None, sa_type=Text)
    evidence_links: str | None = Field(default=None, sa_type=Text)
    completed_at: datetime | None = Field(default=None)
    original_acceptance_criteria: str | None = Field(default=None, sa_type=Text)
    ac_updated_at: datetime | None = Field(default=None)
    ac_update_reason: str | None = Field(default=None, sa_type=Text)

    accepted_spec_version_id: int | None = Field(
        default=None,
        foreign_key="spec_registry.spec_version_id",
        description="Spec version this story was validated/accepted against",
    )
    validation_evidence: str | None = Field(
        default=None,
        sa_type=Text,
        description="JSON: validation results, rules checked, invariants applied",
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

    product_id: int = Field(foreign_key="products.product_id", index=True)
    feature_id: int | None = Field(default=None, foreign_key="features.feature_id")

    product: "Product" = Relationship(back_populates="stories")
    feature: Feature | None = Relationship(back_populates="stories")
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
            "product_id",
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
    product_id: int = Field(foreign_key="products.product_id", index=True)
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
    metadata_json: str | None = Field(
        default_factory=canonical_task_metadata_json,
        sa_type=Text,
    )
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


class ProductPersona(SQLModel, table=True):
    """Approved personas for a product."""

    __tablename__ = "product_personas"  # type: ignore[assignment]

    persona_id: int | None = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="products.product_id")
    persona_name: str = Field(max_length=100, nullable=False)
    is_default: bool = Field(default=False)
    category: str = Field(max_length=50, default="primary_user")
    description: str | None = Field(default=None, sa_type=Text)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={"server_default": func.now()},
        nullable=False,
    )

    product: "Product" = Relationship(back_populates="personas")

    __table_args__ = (
        UniqueConstraint("product_id", "persona_name", name="unique_product_persona"),
    )


# Import the compatibility shim after defining core models so direct
# `import models.core` remains safe and legacy re-exports stay wired up.
import agile_sqlmodel  # noqa: E402,F401  pylint: disable=wrong-import-position,unused-import
