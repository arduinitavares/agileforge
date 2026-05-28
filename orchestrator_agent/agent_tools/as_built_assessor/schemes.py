"""Input and output schemas for the As-Built Assessment agent."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ASSESSMENT_SCHEMA_VERSION: str = "agileforge.as_built_assessment.v1"
EVIDENCE_PACK_SCHEMA_VERSION: str = "agileforge.as_built_evidence_pack.v1"
AGENT_VERSION: str = "agileforge.as_built_assessor.v1"
EVIDENCE_PACK_BUILDER_VERSION: str = "agileforge.as_built_pack_builder.v1"

SpecMode = Literal["current_state", "desired_state", "proposed_change", "unknown"]
AssessmentStatus = Literal[
    "observed",
    "observed_with_missing_evidence",
    "contradicted",
    "not_observed",
    "unclear",
]
AssessmentConfidence = Literal["high", "medium", "low"]
BacklogTreatment = Literal[
    "skip_new_implementation",
    "create_verification_item",
    "create_hardening_item",
    "create_authority_conflict_item",
    "create_discovery_item",
    "create_product_item",
    "po_review_required",
]
EvidenceKind = Literal["source", "test", "doc", "config", "cli", "search"]


class _StrictModel(BaseModel):
    """Base model for deterministic contracts."""

    model_config = ConfigDict(extra="forbid")


class RepoSnapshot(_StrictModel):
    """Repository identity captured for an assessment."""

    path: Annotated[str, Field(min_length=1)]
    git_commit: str | None
    dirty: bool


class EvidenceWarning(_StrictModel):
    """A bounded evidence collection warning."""

    code: Annotated[str, Field(min_length=1)]
    message: Annotated[str, Field(min_length=1)]
    details: dict[str, Any] = Field(default_factory=dict)


class AuthorityTarget(_StrictModel):
    """One accepted authority target to assess against repo evidence."""

    authority_ref: Annotated[str, Field(min_length=1)]
    invariant_refs: list[str] = Field(default_factory=list)
    title: Annotated[str, Field(min_length=1)]
    invariant_type: str | None = None
    source_requirement_id: str | None = None
    terms: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("terms")
    @classmethod
    def _dedupe_terms(cls, value: list[str]) -> list[str]:
        """Normalize search terms while preserving first-seen order."""
        seen: set[str] = set()
        normalized: list[str] = []
        for term in value:
            stripped = term.strip()
            if not stripped or stripped in seen:
                continue
            seen.add(stripped)
            normalized.append(stripped)
        return normalized


class EvidenceSnippet(_StrictModel):
    """Bounded source, test, documentation, config, or search evidence."""

    kind: EvidenceKind
    path: Annotated[str, Field(min_length=1)]
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    matched_terms: list[str] = Field(default_factory=list)
    text: str = ""
    summary: str = ""


class CliObservation(_StrictModel):
    """A bounded CLI observation included in an evidence pack."""

    command: Annotated[str, Field(min_length=1)]
    exit_code: int
    output_excerpt: str


class SearchObservation(_StrictModel):
    """A deterministic search result included in an evidence pack."""

    query: Annotated[str, Field(min_length=1)]
    match_count: int = Field(ge=0)
    paths: list[str] = Field(default_factory=list)


class OriginalSpecContext(_StrictModel):
    """Original spec context and its intended interpretation mode."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    spec_mode: SpecMode = "unknown"
    json_text: str = Field(default="", alias="json")
    markdown: str = ""


class OpenSpecContext(_StrictModel):
    """Optional OpenSpec context, advisory only."""

    present: bool = False
    spec_summaries: list[str] = Field(default_factory=list)
    change_summaries: list[str] = Field(default_factory=list)


class EvidencePack(_StrictModel):
    """Bounded, host-prepared evidence input for the assessment agent."""

    schema_version: Literal["agileforge.as_built_evidence_pack.v1"]
    builder_version: Literal["agileforge.as_built_pack_builder.v1"]
    authority_fingerprint: Annotated[str, Field(min_length=1)]
    evidence_pack_fingerprint: Annotated[str, Field(min_length=1)]
    generated_at: Annotated[str, Field(min_length=1)]
    repo_snapshot: RepoSnapshot
    warnings: list[EvidenceWarning] = Field(default_factory=list)
    file_manifest_summary: dict[str, Any] = Field(default_factory=dict)
    authority_targets: list[AuthorityTarget] = Field(default_factory=list)
    source_snippets: list[EvidenceSnippet] = Field(default_factory=list)
    test_snippets: list[EvidenceSnippet] = Field(default_factory=list)
    doc_snippets: list[EvidenceSnippet] = Field(default_factory=list)
    cli_observations: list[CliObservation] = Field(default_factory=list)
    search_observations: list[SearchObservation] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    def has_no_targets_limitation(self) -> bool:
        """Return whether the pack explicitly explains missing targets."""
        return any(
            "no authority targets" in limitation.lower()
            for limitation in self.limitations
        )


class AsBuiltAssessorInput(BaseModel):
    """Input for the As-Built Assessment agent."""

    model_config = ConfigDict(extra="forbid")

    project_id: int
    assessment_id: Annotated[str, Field(min_length=1)]
    compiled_authority: Annotated[str, Field(min_length=1)]
    original_spec: OriginalSpecContext
    repo_evidence_pack: EvidencePack
    openspec_context: OpenSpecContext
    prior_as_built_assessment: str = "NO_HISTORY"
    user_input: str = ""


class CapabilityEvidence(_StrictModel):
    """Evidence cited by one capability assessment."""

    kind: EvidenceKind
    path: str | None = None
    summary: Annotated[str, Field(min_length=1)]
    supports: Annotated[str, Field(min_length=1)]


class CapabilityAssessment(_StrictModel):
    """Assessment of one authority-backed capability."""

    authority_ref: Annotated[str, Field(min_length=1)]
    invariant_refs: list[str] = Field(default_factory=list)
    capability_title: Annotated[str, Field(min_length=1)]
    status: AssessmentStatus
    confidence: AssessmentConfidence
    evidence: list[CapabilityEvidence] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    recommended_backlog_treatment: BacklogTreatment
    reasoning: Annotated[str, Field(min_length=1)]


class AsBuiltAssessment(_StrictModel):
    """Structured output from the As-Built Assessment agent."""

    schema_version: Literal["agileforge.as_built_assessment.v1"]
    project_id: int
    assessment_id: Annotated[str, Field(min_length=1)]
    agent_version: Literal["agileforge.as_built_assessor.v1"]
    evidence_pack_builder_version: Literal["agileforge.as_built_pack_builder.v1"]
    authority_fingerprint: Annotated[str, Field(min_length=1)]
    evidence_pack_fingerprint: Annotated[str, Field(min_length=1)]
    generated_at: Annotated[str, Field(min_length=1)]
    assessment_summary: Annotated[str, Field(min_length=1)]
    repo_snapshot: RepoSnapshot
    capability_assessments: list[CapabilityAssessment] = Field(default_factory=list)
    cross_cutting_findings: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    is_complete: bool
    clarifying_questions: list[str] = Field(default_factory=list)


class AsBuiltAssessmentCacheMeta(_StrictModel):
    """Workflow-state freshness metadata for cached assessments."""

    schema_version: Literal["agileforge.as_built_assessment.v1"]
    agent_version: Literal["agileforge.as_built_assessor.v1"]
    evidence_pack_builder_version: Literal["agileforge.as_built_pack_builder.v1"]
    authority_fingerprint: Annotated[str, Field(min_length=1)]
    repo_git_commit: str | None
    repo_dirty: bool
    evidence_pack_fingerprint: Annotated[str, Field(min_length=1)]
    assessment_fingerprint: Annotated[str, Field(min_length=1)]
    generated_at: Annotated[str, Field(min_length=1)]
