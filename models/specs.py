"""Spec-related SQLModel classes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import relationship
from sqlalchemy.schema import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.types import Text
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
    approved_at: datetime | None = Field(
        default=None, description="Timestamp when spec was approved"
    )
    approved_by: str | None = Field(
        default=None,
        description="Identifier of approver (e.g., username, email)",
    )
    approval_notes: str | None = Field(
        default=None,
        sa_type=Text,
        description="Review notes or justification for approval",
    )
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
    compiled_authority: list[CompiledSpecAuthority] = Relationship(
        sa_relationship=relationship(
            "CompiledSpecAuthority",
            back_populates="spec_version",
            collection_class=list,
            uselist=True,
        )
    )


class CompiledSpecAuthority(SQLModel, table=True):
    """Cached compilation output for an approved spec version."""

    __tablename__ = "compiled_spec_authority"  # type: ignore[assignment]
    authority_id: int | None = Field(default=None, primary_key=True)
    spec_version_id: int = Field(
        foreign_key="spec_registry.spec_version_id",
        index=True,
    )
    compiler_version: str = Field(
        description="Version of compilation logic (e.g., '1.0.0')"
    )
    prompt_hash: str = Field(
        description="Hash of LLM prompt used for compilation (reproducibility)"
    )
    compiled_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
    )
    compiled_artifact_json: str | None = Field(
        default=None,
        sa_type=Text,
        description=(
            "Normalized SpecAuthorityCompilationSuccess JSON artifact (authoritative)"
        ),
    )
    scope_themes: str = Field(
        sa_type=Text, description="JSON array of extracted scope themes"
    )
    invariants: str = Field(
        sa_type=Text, description="JSON array of business rules and invariants"
    )
    eligible_feature_ids: str = Field(
        sa_type=Text,
        description="JSON array of feature IDs that align with spec",
    )
    rejected_features: str | None = Field(
        default=None,
        sa_type=Text,
        description="JSON array of out-of-scope features with rationale",
    )
    spec_gaps: str | None = Field(
        default=None,
        sa_type=Text,
        description="JSON array of detected spec ambiguities or gaps",
    )

    spec_version: SpecRegistry = Relationship(
        sa_relationship=relationship(
            "SpecRegistry",
            back_populates="compiled_authority",
        )
    )


class SpecAuthorityAcceptance(SQLModel, table=True):
    """Append-only acceptance decisions for compiled spec authority."""

    __tablename__ = "spec_authority_acceptance"  # type: ignore[assignment]
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(
        foreign_key="projects.project_id",
        index=True,
    )
    spec_version_id: int = Field(
        foreign_key="spec_registry.spec_version_id",
        index=True,
    )
    status: str = Field(description="Decision status: accepted | rejected")
    policy: str = Field(
        description=(
            "Decision policy: manual | agent_requested | dashboard_manual | test"
        )
    )
    decided_by: str = Field(description="Who or what made the decision")
    decided_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
    )
    rationale: str | None = Field(
        default=None,
        sa_type=Text,
        description="Optional acceptance rationale",
    )
    compiler_version: str = Field(description="Compiler version at decision time")
    prompt_hash: str = Field(description="Prompt hash at decision time")
    spec_hash: str = Field(description="Spec hash at decision time")
    pending_authority_id: int | None = Field(default=None, index=True)
    authority_fingerprint: str | None = Field(default=None, index=True)
    review_token: str | None = Field(default=None, index=True)
    review_fingerprint: str | None = Field(default=None)
    disk_spec_hash: str | None = Field(default=None)
    resolved_spec_path: str | None = Field(default=None)
    actor_mode: str | None = Field(default=None)
    review_completeness: str | None = Field(default=None)
    incomplete_review_override: bool = Field(default=False)
    incomplete_review_rationale: str | None = Field(default=None)
    incomplete_review_overrides_json: str | None = Field(default=None, sa_type=Text)
    terminal_decision_key: str | None = Field(default=None, index=True)
    provenance_source: str = Field(default="normal")
