"""Spec-related SQLModel classes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy.types import Text
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from models.core import Product


class SpecRegistry(SQLModel, table=True):
    """Versioned technical specification registry with approval workflow."""

    __tablename__ = "spec_registry"  # type: ignore[assignment]
    spec_version_id: int | None = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="products.product_id", index=True)
    spec_hash: str = Field(
        description="SHA-256 hash of spec content for change detection"
    )
    content: str = Field(
        sa_type=Text,
        description="Full specification content (markdown or plain text)",
    )
    content_ref: str | None = Field(
        default=None,
        description="Original file path or reference for provenance",
    )
    status: str = Field(
        default="draft",
        description="Lifecycle status: draft | approved | superseded",
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

    product: Product = Relationship(back_populates="spec_versions")
    compiled_authority: CompiledSpecAuthority = Relationship(
        back_populates="spec_version",
        sa_relationship_kwargs={"uselist": False},
    )


class CompiledSpecAuthority(SQLModel, table=True):
    """Cached compilation output for an approved spec version."""

    __tablename__ = "compiled_spec_authority"  # type: ignore[assignment]
    authority_id: int | None = Field(default=None, primary_key=True)
    spec_version_id: int = Field(
        foreign_key="spec_registry.spec_version_id",
        unique=True,
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

    spec_version: SpecRegistry = Relationship(back_populates="compiled_authority")


class SpecAuthorityAcceptance(SQLModel, table=True):
    """Append-only acceptance decisions for compiled spec authority."""

    __tablename__ = "spec_authority_acceptance"  # type: ignore[assignment]
    id: int | None = Field(default=None, primary_key=True)
    product_id: int = Field(
        foreign_key="products.product_id",
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
