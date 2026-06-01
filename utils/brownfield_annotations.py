"""Host-derived brownfield annotation schemas."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestrator_agent.agent_tools.as_built_assessor.schemes import (
    AssessmentConfidence,  # noqa: TC001 - Pydantic resolves these at runtime.
    AssessmentStatus,  # noqa: TC001 - Pydantic resolves these at runtime.
    BacklogTreatment,  # noqa: TC001 - Pydantic resolves these at runtime.
)

BrownfieldMatchTier = Literal["exact", "fuzzy", "none"]
BrownfieldAnnotationSource = Literal["host_derived", "model_asserted"]
BrownfieldWarningCode = Literal[
    "metadata_filled_by_host",
    "possible_mapping",
    "looks_mapped_but_unmatched",
    "conflicting_invariants",
    "status_disagreement",
    "treatment_disagreement",
    "capability_disagreement",
    "asserted_authority_ref_unmatched",
]
BrownfieldDisagreementCode = Literal[
    "status_disagreement",
    "treatment_disagreement",
    "capability_disagreement",
]
BrownfieldWarningSeverity = Literal["info", "review", "block_on_save"]


class BrownfieldSelectedCapability(BaseModel):
    """A host-selected As-Built capability contract."""

    model_config = ConfigDict(extra="forbid")

    authority_ref: Annotated[str, Field(min_length=1)]
    capability_title: Annotated[str, Field(min_length=1)]
    invariant_refs: list[str] = Field(default_factory=list)
    as_built_status: AssessmentStatus
    recommended_backlog_treatment: BacklogTreatment
    confidence: AssessmentConfidence


class BrownfieldModelAssertion(BaseModel):
    """Raw model-provided brownfield hints preserved for provenance."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["model_asserted"] = "model_asserted"
    authority_ref: str | None = None
    capability_hint: str | None = None
    as_built_status: AssessmentStatus | None = None
    recommended_backlog_treatment: BacklogTreatment | None = None


class BrownfieldDisagreement(BaseModel):
    """A structured disagreement between model assertion and host annotation."""

    model_config = ConfigDict(extra="forbid")

    field: Annotated[str, Field(min_length=1)]
    model_value: str | None
    host_value: str | None
    code: BrownfieldDisagreementCode


class BrownfieldAnnotation(BaseModel):
    """Host-derived annotation attached to one backlog item."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agileforge.brownfield_annotation.v1"]
    source: Literal["host_derived"] = "host_derived"
    match_tier: BrownfieldMatchTier
    match_basis: list[str] = Field(default_factory=list)
    conflict: bool = False
    selected: BrownfieldSelectedCapability | None = None
    candidates: list[BrownfieldSelectedCapability] = Field(default_factory=list)
    model_assertion: BrownfieldModelAssertion = Field(
        default_factory=BrownfieldModelAssertion
    )
    disagreements: list[BrownfieldDisagreement] = Field(default_factory=list)
    warning_codes: list[BrownfieldWarningCode] = Field(default_factory=list)


class BrownfieldWarning(BaseModel):
    """Structured warning emitted during host annotation."""

    model_config = ConfigDict(extra="forbid")

    code: BrownfieldWarningCode
    item_index: Annotated[int, Field(ge=0)] | None = None
    severity: BrownfieldWarningSeverity = "review"
    match_tier: BrownfieldMatchTier
    authority_ref: str | None = None
    invariant_refs: list[str] = Field(default_factory=list)
    message: Annotated[str, Field(min_length=1)]
    details: dict[str, object] = Field(default_factory=dict)
