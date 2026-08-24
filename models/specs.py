"""Spec-related SQLModel classes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.schema import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
)
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from models.core import Project


class SpecRegistry(SQLModel, table=True):
    """Versioned technical specification registry with approval workflow."""

    __tablename__ = "spec_registry"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "spec_version_id",
            name="uq_spec_registry_project_id",
        ),
        CheckConstraint(
            "status IN ('approved', 'superseded')",
            name="ck_spec_registry_status",
        ),
        UniqueConstraint(
            "project_id",
            "spec_version_id",
            "spec_hash",
            name="uq_spec_registry_project_id_hash",
        ),
        UniqueConstraint(
            "project_id",
            "source_specification_candidate_id",
            name="uq_spec_registry_candidate",
        ),
        UniqueConstraint(
            "project_id",
            "source_specification_candidate_id",
            "source_specification_candidate_fingerprint",
            "spec_hash",
            name="uq_spec_registry_source_candidate",
        ),
        ForeignKeyConstraint(
            [
                "project_id",
                "source_specification_candidate_id",
                "source_specification_candidate_fingerprint",
                "spec_hash",
            ],
            [
                "specification_candidates.project_id",
                "specification_candidates.specification_candidate_id",
                "specification_candidates.candidate_fingerprint",
                "specification_candidates.payload_fingerprint",
            ],
            name="fk_spec_registry_source_candidate",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            [
                "project_id",
                "source_specification_decision_id",
                "source_specification_candidate_id",
                "source_specification_candidate_fingerprint",
            ],
            [
                "specification_decisions.project_id",
                "specification_decisions.specification_decision_id",
                "specification_decisions.specification_candidate_id",
                "specification_decisions.candidate_fingerprint",
            ],
            name="fk_spec_registry_accepted_decision",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["project_id", "source_vision_artifact_id", "source_vision_fingerprint"],
            [
                "vision_artifacts.project_id",
                "vision_artifacts.vision_artifact_id",
                "vision_artifacts.content_fingerprint",
            ],
            name="fk_spec_registry_source_vision",
        ),
        ForeignKeyConstraint(
            [
                "project_id",
                "source_product_goal_artifact_id",
                "source_product_goal_fingerprint",
            ],
            [
                "product_goal_artifacts.project_id",
                "product_goal_artifacts.product_goal_artifact_id",
                "product_goal_artifacts.content_fingerprint",
            ],
            name="fk_spec_registry_source_goal",
        ),
        Index(
            "uq_spec_registry_current_approved",
            "project_id",
            unique=True,
            sqlite_where=text("status = 'approved'"),
            postgresql_where=text("status = 'approved'"),
        ),
    )

    spec_version_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    spec_hash: str = Field(
        description="SHA-256 hash of spec content for change detection"
    )
    status: str = Field(
        description="Lifecycle status: approved | superseded",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
    )
    source_specification_decision_id: int = Field(index=True)
    source_specification_candidate_id: int = Field(index=True)
    source_specification_candidate_fingerprint: str = Field(index=True)
    source_vision_artifact_id: int = Field(index=True)
    source_vision_fingerprint: str = Field(index=True)
    source_product_goal_artifact_id: int = Field(index=True)
    source_product_goal_fingerprint: str = Field(index=True)
    supersedes_spec_version_id: int | None = Field(
        default=None,
        foreign_key="spec_registry.spec_version_id",
        index=True,
    )

    project: Project = Relationship(back_populates="spec_versions")
