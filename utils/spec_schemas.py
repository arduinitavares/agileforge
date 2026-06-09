"""Spec/compiler/story-validation shared schemas."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from utils.spec_authority_ir import (
    AuthorityTargetKind,
    CoverageStatus,
    IrProvenance,
    MappingProvenance,
    SourceUnitDisposition,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_DATETIME_TYPE = datetime
MIN_STORY_POINTS = 1
MAX_STORY_POINTS = 8
MAX_COMPACT_IR_EXCERPT_BYTES = 2_000
_COMPACT_IR_GAP_FINDING_CODES: Final[tuple[str, ...]] = (
    "AUTHORITY_CANDIDATE_UNCOVERED",
    "AUTHORITY_CANDIDATE_WEAK_MAPPING",
    "AUTHORITY_CANDIDATE_INTENTIONALLY_CLASSIFIED",
    "AUTHORITY_CANDIDATE_PARTIAL",
    "AUTHORITY_CANDIDATE_UNCERTAIN",
    "AUTHORITY_COVERAGE_INCOMPLETE",
)


class ValidationFailure(BaseModel):
    """A single validation failure record."""

    rule: Annotated[str, Field(description="Rule ID or name that failed")]
    expected: Annotated[
        str | None,
        Field(default=None, description="Expected value/condition"),
    ]
    actual: Annotated[
        str | None,
        Field(default=None, description="Actual value found"),
    ]
    message: Annotated[str, Field(description="Human-readable failure message")]


class AlignmentFinding(BaseModel):
    """Structured alignment finding for audit evidence."""

    code: Annotated[str, Field(description="Stable identifier for the finding")]
    invariant: Annotated[
        str | None,
        Field(default=None, description="Invariant text or ID used"),
    ]
    source_requirement: str | None = Field(
        default=None, description="Source requirement ID from backlog."
    )
    capability: Annotated[
        str | None,
        Field(default=None, description="Capability token (if applicable)"),
    ]
    message: Annotated[str, Field(description="Human-readable message")]
    severity: Annotated[
        Literal["warning", "failure"],
        Field(description="Severity level"),
    ]
    created_at: Annotated[
        datetime,
        Field(description="UTC timestamp for the finding"),
    ]


class ValidationEvidence(BaseModel):
    """
    Complete validation evidence for a story validation run.

    Persisted to UserStory.validation_evidence as JSON.
    Every validation (pass or fail) produces this evidence.
    """

    spec_version_id: Annotated[
        int,
        Field(description="Spec version used for validation"),
    ]
    validated_at: Annotated[
        datetime,
        Field(description="UTC timestamp of validation"),
    ]
    passed: Annotated[bool, Field(description="Overall validation result")]
    rules_checked: Annotated[
        list[str],
        Field(description="List of rule IDs/names checked"),
    ]
    invariants_checked: Annotated[
        list[str],
        Field(description="List of invariant IDs/strings checked"),
    ]
    evaluated_invariant_ids: list[str] = Field(
        default_factory=list,
        description="IDs of invariants whose validation logic actually ran",
    )
    finding_invariant_ids: list[str] = Field(
        default_factory=list,
        description="IDs of invariants referenced in alignment warnings or failures",
    )
    failures: list[ValidationFailure] = Field(
        default_factory=list,
        description="List of failures",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-blocking warnings",
    )
    alignment_warnings: list[AlignmentFinding] = Field(
        default_factory=list,
        description="Alignment warnings",
    )
    alignment_failures: list[AlignmentFinding] = Field(
        default_factory=list,
        description="Alignment failures",
    )
    validator_version: Annotated[
        str,
        Field(description="Version of validator logic used"),
    ]
    input_hash: Annotated[
        str,
        Field(description="SHA-256 hash of story content at validation time"),
    ]


class _SpecAuthoritySourceSelectionError(ValueError):
    """Raised when compiler input provides both or neither source fields."""

    def __init__(self) -> None:
        super().__init__("Provide exactly one of spec_source or spec_content_ref.")


class _InvariantParameterTypeError(ValueError):
    """Raised when an invariant payload uses parameters for the wrong type."""

    def __init__(self, invariant_type: InvariantType) -> None:
        super().__init__(f"Invariant parameters do not match type {invariant_type}.")


class _StoryPointRangeError(ValueError):
    """Raised when story points fall outside the accepted range."""

    def __init__(self) -> None:
        super().__init__(
            "Story points must be between 1 and 8 (INVEST principle: Small)."
        )


class _StoryDescriptionPrefixError(ValueError):
    """Raised when a story description is missing the persona clause."""

    def __init__(self) -> None:
        super().__init__("Story description must start with 'As a ...'")


class _StoryDescriptionWantError(ValueError):
    """Raised when a story description is missing the desire clause."""

    def __init__(self) -> None:
        super().__init__("Story description must contain '... I want ...'")


class _StoryDescriptionBenefitError(ValueError):
    """Raised when a story description is missing the benefit clause."""

    def __init__(self) -> None:
        super().__init__("Story description must contain '... so that ...'")


class SpecAuthorityCompilerInput(BaseModel):
    """Input schema for spec_authority_compiler_agent."""

    model_config = ConfigDict(extra="forbid")

    spec_source: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Raw specification text. Provide exactly one of "
                "spec_source or spec_content_ref."
            ),
        ),
    ]
    spec_content_ref: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Path or identifier for spec content. Provide exactly "
                "one of spec_source or spec_content_ref."
            ),
        ),
    ]
    domain_hint: Annotated[
        str | None,
        Field(default=None, description="Optional domain hint for extraction."),
    ]
    product_id: Annotated[
        int | None,
        Field(default=None, description="Optional product identifier."),
    ]
    spec_version_id: Annotated[
        int | None,
        Field(default=None, description="Optional spec version identifier."),
    ]
    spec_source_format: Annotated[
        Literal["agileforge.spec.v1"],
        Field(
            description=(
                "Input format: canonical agileforge.spec.v1 JSON."
            )
        ),
    ] = "agileforge.spec.v1"

    @model_validator(mode="after")
    def validate_exactly_one_source(self) -> SpecAuthorityCompilerInput:
        """Require exactly one of inline spec content or a content reference."""
        has_source = bool(self.spec_source and self.spec_source.strip())
        has_ref = bool(self.spec_content_ref and self.spec_content_ref.strip())
        if has_source == has_ref:
            raise _SpecAuthoritySourceSelectionError
        return self


class InvariantType(StrEnum):
    """Allowed invariant types for compiled spec authority."""

    FORBIDDEN_CAPABILITY = "FORBIDDEN_CAPABILITY"
    REQUIRED_FIELD = "REQUIRED_FIELD"
    MAX_VALUE = "MAX_VALUE"
    RELATION_CONSTRAINT = "RELATION_CONSTRAINT"
    USER_INTERACTION = "USER_INTERACTION"
    STATE_TRANSITION = "STATE_TRANSITION"
    DATA_CONTRACT = "DATA_CONTRACT"
    ROUTE_CONTRACT = "ROUTE_CONTRACT"
    VISIBILITY_RULE = "VISIBILITY_RULE"


SpecAuthoritySourceLevel = Literal["MUST", "SHOULD", "MAY", "MUST_NOT"]


class BehavioralAuthorityParams(BaseModel):
    """Strict base class for behavioral authority parameter payloads."""

    model_config = ConfigDict(extra="forbid")


class ForbiddenCapabilityParams(BaseModel):
    """Parameters for FORBIDDEN_CAPABILITY invariants."""

    model_config = ConfigDict(extra="forbid")

    capability: Annotated[
        str,
        Field(min_length=1, description="Capability or technology that is forbidden."),
    ]


class RequiredFieldParams(BaseModel):
    """Parameters for REQUIRED_FIELD invariants."""

    model_config = ConfigDict(extra="forbid")

    field_name: Annotated[
        str,
        Field(min_length=1, description="Required field or artifact name."),
    ]


class MaxValueParams(BaseModel):
    """Parameters for MAX_VALUE invariants."""

    model_config = ConfigDict(extra="forbid")

    field_name: Annotated[
        str,
        Field(min_length=1, description="Field constrained by a maximum value."),
    ]
    max_value: Annotated[
        int | float,
        Field(description="Maximum allowed numeric value."),
    ]


class RelationConstraintParams(BaseModel):
    """Parameters for dynamic relationship constraints."""

    model_config = ConfigDict(extra="forbid")

    expression: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Dynamic relationship expression, e.g. budget_used <= budget."
            ),
        ),
    ]


class UserInteractionParams(BaseModel):
    """Parameters for user-triggered interaction contracts."""

    model_config = ConfigDict(extra="forbid")

    trigger: Annotated[
        str,
        Field(
            min_length=1,
            description="User event or gesture that triggers behavior.",
        ),
    ]
    target: Annotated[
        str,
        Field(min_length=1, description="UI element or object receiving the trigger."),
    ]
    expected_response: Annotated[
        str,
        Field(min_length=1, description="Required response to the interaction."),
    ]


class StateTransitionParams(BaseModel):
    """Parameters for state transition contracts."""

    model_config = ConfigDict(extra="forbid")

    state: Annotated[
        str,
        Field(min_length=1, description="State or state machine being constrained."),
    ]
    trigger: Annotated[
        str,
        Field(min_length=1, description="Event or condition that causes transition."),
    ]
    outcome: Annotated[
        str,
        Field(min_length=1, description="Required resulting state or side effect."),
    ]


class DataContractParams(BaseModel):
    """Parameters for persisted or exchanged data contracts."""

    model_config = ConfigDict(extra="forbid")

    subject: Annotated[
        str,
        Field(min_length=1, description="Data object, record, key, or payload."),
    ]
    fields: Annotated[
        list[str],
        Field(default_factory=list, description="Required or recommended fields."),
    ]
    rule: Annotated[
        str,
        Field(min_length=1, description="Data shape, naming, or persistence rule."),
    ]


class RouteContractParams(BaseModel):
    """Parameters for routing contracts."""

    model_config = ConfigDict(extra="forbid")

    route: Annotated[
        str,
        Field(min_length=1, description="Route pattern or route family."),
    ]
    route_name: Annotated[
        str,
        Field(min_length=1, description="Human-readable route purpose."),
    ]
    behavior: Annotated[
        str,
        Field(min_length=1, description="Required behavior when the route is active."),
    ]


class VisibilityRuleParams(BaseModel):
    """Parameters for UI visibility contracts."""

    model_config = ConfigDict(extra="forbid")

    target: Annotated[
        str,
        Field(min_length=1, description="UI element whose visibility is constrained."),
    ]
    condition: Annotated[
        str,
        Field(min_length=1, description="Condition under which the rule applies."),
    ]
    visibility: Annotated[
        Literal["visible", "hidden", "shown", "removed"],
        Field(description="Required visibility state."),
    ]


InvariantParameters = (
    ForbiddenCapabilityParams
    | RequiredFieldParams
    | MaxValueParams
    | RelationConstraintParams
    | UserInteractionParams
    | StateTransitionParams
    | DataContractParams
    | RouteContractParams
    | VisibilityRuleParams
)


class Invariant(BaseModel):
    """Typed invariant with deterministic ID and structured parameters."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[
        str,
        Field(
            pattern=r"^INV-[0-9a-f]{16}$",
            description="Deterministic invariant identifier (INV- + 16 hex chars).",
        ),
    ]
    type: Annotated[InvariantType, Field(description="Invariant type enum.")]
    source_item_id: Annotated[
        str | None,
        Field(
            min_length=1,
            description="Structured spec item ID that authorizes this invariant.",
        ),
    ] = None
    source_level: Annotated[
        SpecAuthoritySourceLevel | None,
        Field(
            description="Normative level of the source item.",
        ),
    ] = None
    parameters: Annotated[
        InvariantParameters,
        Field(description="Typed parameters for the invariant."),
    ]

    @model_validator(mode="after")
    def validate_parameters_match_type(self) -> Invariant:
        """Ensure the invariant parameter model matches the declared type."""
        type_map = {
            InvariantType.FORBIDDEN_CAPABILITY: ForbiddenCapabilityParams,
            InvariantType.REQUIRED_FIELD: RequiredFieldParams,
            InvariantType.MAX_VALUE: MaxValueParams,
            InvariantType.RELATION_CONSTRAINT: RelationConstraintParams,
            InvariantType.USER_INTERACTION: UserInteractionParams,
            InvariantType.STATE_TRANSITION: StateTransitionParams,
            InvariantType.DATA_CONTRACT: DataContractParams,
            InvariantType.ROUTE_CONTRACT: RouteContractParams,
            InvariantType.VISIBILITY_RULE: VisibilityRuleParams,
        }
        expected_type = type_map.get(self.type)
        if expected_type and not isinstance(self.parameters, expected_type):
            raise _InvariantParameterTypeError(self.type)
        return self


class SourceMapEntry(BaseModel):
    """Mapping of invariants to source excerpts."""

    model_config = ConfigDict(extra="forbid")

    invariant_id: Annotated[
        str,
        Field(description="Invariant ID referenced in this mapping."),
    ]
    excerpt: Annotated[
        str,
        Field(description="Exact excerpt from spec supporting the invariant."),
    ]
    location: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional location reference (e.g., line or section).",
        ),
    ]


class EligibleFeatureRule(BaseModel):
    """Closed schema for optional feature eligibility notes."""

    model_config = ConfigDict(extra="forbid")

    rule: Annotated[
        str,
        Field(
            description="Short eligibility rule or note tied to a candidate feature."
        ),
    ]


class _CompactIrExcerptTooLargeError(ValueError):
    """Raised when compact IR stores more than a bounded excerpt."""

    def __init__(self, field_name: str) -> None:
        super().__init__(
            f"{field_name} must be <= {MAX_COMPACT_IR_EXCERPT_BYTES} UTF-8 bytes."
        )


class _CompactIrLineRangeError(ValueError):
    """Raised when a compact IR line range is invalid."""

    def __init__(self) -> None:
        super().__init__("line_end must be greater than or equal to line_start.")


class _CompactIrSourceReferenceError(ValueError):
    """Raised when a candidate references a missing source unit."""

    def __init__(self, candidate_id: str, source_unit_id: str) -> None:
        super().__init__(
            f"Requirement candidate {candidate_id} references missing "
            f"source unit {source_unit_id}."
        )


class _CompactIrMappingCandidateError(ValueError):
    """Raised when a mapping references a missing requirement candidate."""

    def __init__(self, candidate_id: str) -> None:
        super().__init__(
            f"Authority mapping references missing candidate {candidate_id}."
        )


class _CompactIrMappingAuthorityError(ValueError):
    """Raised when a mapping references a missing authority item."""

    def __init__(
        self,
        authority_item_id: str,
        target_kind: AuthorityTargetKind,
    ) -> None:
        super().__init__(
            f"Authority mapping references missing {target_kind.value} "
            f"item {authority_item_id}."
        )


class _CompactIrMappingTargetKindError(ValueError):
    """Raised when a mapping target kind does not match the target collection."""

    def __init__(
        self,
        authority_item_id: str,
        target_kind: AuthorityTargetKind,
        actual_kind: AuthorityTargetKind,
    ) -> None:
        super().__init__(
            f"Authority mapping target kind {target_kind.value} is incompatible "
            f"with {authority_item_id}, which belongs to {actual_kind.value}."
        )


class _CompactIrProvenanceRequiredError(ValueError):
    """Raised when compact IR data is present without artifact provenance."""

    def __init__(self) -> None:
        super().__init__("ir_provenance is required when compact IR fields are set.")


class SpecAuthorityIrPacketLimits(BaseModel):
    """Limits that shaped the compact authority IR packet."""

    model_config = ConfigDict(extra="forbid")

    max_candidates: Annotated[
        int,
        Field(ge=0, description="Maximum requirement candidates included."),
    ]
    max_findings: Annotated[
        int,
        Field(ge=0, description="Maximum review findings included."),
    ]
    max_excerpt_bytes: Annotated[
        int,
        Field(
            ge=1,
            le=MAX_COMPACT_IR_EXCERPT_BYTES,
            description="Maximum UTF-8 bytes allowed for compact excerpts.",
        ),
    ] = MAX_COMPACT_IR_EXCERPT_BYTES
    truncated: Annotated[
        bool,
        Field(description="Whether packet lists were truncated."),
    ] = False


class SpecAuthoritySourceUnit(BaseModel):
    """Compact source-unit metadata for authority IR."""

    model_config = ConfigDict(extra="forbid")

    unit_id: Annotated[str, Field(min_length=1)]
    section_id: Annotated[str, Field(min_length=1)]
    heading_path: list[str] = Field(default_factory=list)
    kind: Annotated[str, Field(min_length=1)]
    line_start: Annotated[int, Field(ge=1)]
    line_end: Annotated[int, Field(ge=1)]
    text_hash: Annotated[str, Field(min_length=1)]
    text_excerpt: Annotated[str, Field(min_length=1)]
    disposition: SourceUnitDisposition
    disposition_reason: str | None = None

    @model_validator(mode="after")
    def validate_compact_excerpt(self) -> Self:
        """Ensure only bounded display excerpts are persisted."""
        if len(self.text_excerpt.encode("utf-8")) > MAX_COMPACT_IR_EXCERPT_BYTES:
            field_name = "text_excerpt"
            raise _CompactIrExcerptTooLargeError(field_name)
        if self.line_end < self.line_start:
            raise _CompactIrLineRangeError
        return self


class SpecAuthorityRequirementCandidate(BaseModel):
    """Compact requirement-candidate metadata for authority IR."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: Annotated[str, Field(min_length=1)]
    source_unit_id: Annotated[str, Field(min_length=1)]
    statement: Annotated[str, Field(min_length=1)]
    source_quote: Annotated[str, Field(min_length=1)]
    quote_hash: Annotated[str, Field(min_length=1)]
    line_start: Annotated[int, Field(ge=1)]
    line_end: Annotated[int, Field(ge=1)]
    classification: Annotated[str, Field(min_length=1)]
    provenance: IrProvenance

    @model_validator(mode="after")
    def validate_compact_quote(self) -> Self:
        """Ensure source quote is a bounded compact excerpt."""
        if len(self.source_quote.encode("utf-8")) > MAX_COMPACT_IR_EXCERPT_BYTES:
            field_name = "source_quote"
            raise _CompactIrExcerptTooLargeError(field_name)
        if self.line_end < self.line_start:
            raise _CompactIrLineRangeError
        return self


class SpecAuthorityMapping(BaseModel):
    """Compact requirement-candidate to authority-item mapping."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: Annotated[str, Field(min_length=1)]
    authority_item_id: Annotated[str, Field(min_length=1)]
    authority_target_kind: AuthorityTargetKind
    mapping_status: CoverageStatus
    mapping_rationale: Annotated[str, Field(min_length=1)]
    source_quote_hash: str | None = None
    mapping_provenance: MappingProvenance


AuthorityQualityGroupType = Literal[
    "near_duplicate_invariants",
    "over_split_invariants",
    "related_source_variants",
    "noisy_assumptions",
]
AuthorityQualitySeverity = Literal["info", "warning"]
AuthorityQualityItemKind = Literal["invariant", "assumption"]


class AuthorityQualitySummary(BaseModel):
    """Compact counts produced by the authority quality gate."""

    model_config = ConfigDict(extra="forbid")

    original_invariant_count: Annotated[int, Field(ge=0)]
    final_invariant_count: Annotated[int, Field(ge=0)]
    merged_invariant_count: Annotated[int, Field(ge=0)]
    merged_assumption_count: Annotated[int, Field(ge=0)]
    review_group_count: Annotated[int, Field(ge=0)]
    near_duplicate_group_count: Annotated[int, Field(ge=0)]
    over_split_group_count: Annotated[int, Field(ge=0)]
    noisy_assumption_group_count: Annotated[int, Field(ge=0)]


class AuthorityQualityMergedItem(BaseModel):
    """One auto-merge decision made by the quality gate."""

    model_config = ConfigDict(extra="forbid")

    merge_id: Annotated[str, Field(min_length=1)]
    item_kind: AuthorityQualityItemKind
    kept_id: Annotated[str, Field(min_length=1)]
    removed_ids: Annotated[list[str], Field(min_length=1)]
    reason: Annotated[str, Field(min_length=1)]
    source_evidence_count: Annotated[int, Field(ge=0)] = 0


class AuthorityQualityReviewGroup(BaseModel):
    """Related authority items that need human review."""

    model_config = ConfigDict(extra="forbid")

    group_id: Annotated[str, Field(min_length=1)]
    group_type: AuthorityQualityGroupType
    severity: AuthorityQualitySeverity = "warning"
    member_ids: Annotated[list[str], Field(min_length=1)]
    reason: Annotated[str, Field(min_length=1)]
    merge_allowed: bool = False
    truncated: bool = False


class AuthorityQualityReport(BaseModel):
    """Persisted authority quality gate report."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agileforge.authority_quality.v1"] = (
        "agileforge.authority_quality.v1"
    )
    summary: AuthorityQualitySummary
    merged_items: list[AuthorityQualityMergedItem] = Field(default_factory=list)
    review_groups: list[AuthorityQualityReviewGroup] = Field(default_factory=list)


class SpecAuthorityCompilationSuccess(BaseModel):
    """Successful spec authority compilation output."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Annotated[
        Literal["agileforge.compiled_authority.v2"],
        Field(description="Stored compiled-authority schema version."),
    ] = "agileforge.compiled_authority.v2"
    scope_themes: Annotated[
        list[str],
        Field(description="Top-level scope themes extracted from the spec."),
    ]
    domain: str | None = Field(
        default=None,
        description="Optional primary domain for spec (e.g., training, review).",
    )
    invariants: Annotated[
        list[Invariant],
        Field(description="Structured invariants extracted from the spec."),
    ]
    eligible_feature_rules: Annotated[
        list[EligibleFeatureRule],
        Field(description="Optional feature eligibility rules (may be empty)."),
    ]
    rejected_features: list[str] = Field(
        default_factory=list,
        description="Optional rejected feature/scope exclusions (may be empty).",
    )
    gaps: Annotated[
        list[str],
        Field(description="Missing or ambiguous spec items."),
    ]
    assumptions: Annotated[
        list[str],
        Field(description="Explicit assumptions made during compilation."),
    ]
    source_map: Annotated[
        list[SourceMapEntry],
        Field(description="Mapping of invariants to source excerpts."),
    ]
    authority_quality: AuthorityQualityReport | None = Field(
        default=None,
        description="Optional host-derived quality report for review.",
    )
    compiler_version: Annotated[
        str,
        Field(description="Compiler version identifier."),
    ]
    prompt_hash: Annotated[
        str,
        Field(
            pattern=r"^[0-9a-f]{64}$",
            description="SHA-256 hash of the compiler prompt/instructions.",
        ),
    ]
    ir_schema_version: str | None = Field(
        default=None,
        description="Optional compact authority IR schema version.",
    )
    ir_provenance: IrProvenance | None = Field(
        default=None,
        description="Provenance for compact authority IR fields.",
    )
    source_units: list[SpecAuthoritySourceUnit] = Field(
        default_factory=list,
        description="Compact parsed source-unit metadata.",
    )
    requirement_candidates: list[SpecAuthorityRequirementCandidate] = Field(
        default_factory=list,
        description="Compact atomic requirement-candidate metadata.",
    )
    authority_mappings: list[SpecAuthorityMapping] = Field(
        default_factory=list,
        description="Compact candidate-to-authority mappings.",
    )
    ir_packet_limits: SpecAuthorityIrPacketLimits | None = Field(
        default=None,
        description="Limits used when building the compact IR packet.",
    )

    @model_validator(mode="after")
    def validate_compact_ir_references(self) -> Self:
        """Validate compact IR provenance and internal references."""
        has_ir_payload = bool(
            self.ir_schema_version
            or self.source_units
            or self.requirement_candidates
            or self.authority_mappings
            or self.ir_packet_limits
        )
        if has_ir_payload and self.ir_provenance is None:
            raise _CompactIrProvenanceRequiredError

        source_unit_ids = {unit.unit_id for unit in self.source_units}
        allow_model_candidate_hints_without_units = (
            self.ir_provenance == IrProvenance.MODEL_EMITTED
            and not self.source_units
        )
        if not allow_model_candidate_hints_without_units:
            for candidate in self.requirement_candidates:
                if candidate.source_unit_id not in source_unit_ids:
                    raise _CompactIrSourceReferenceError(
                        candidate.candidate_id,
                        candidate.source_unit_id,
                    )

        candidate_ids = {
            candidate.candidate_id for candidate in self.requirement_candidates
        }
        allow_external_manifest_candidates = allow_model_candidate_hints_without_units
        authority_item_kinds = self._authority_item_kinds_by_id()
        for mapping in self.authority_mappings:
            if (
                mapping.candidate_id not in candidate_ids
                and not allow_external_manifest_candidates
            ):
                raise _CompactIrMappingCandidateError(mapping.candidate_id)
            actual_kind = authority_item_kinds.get(mapping.authority_item_id)
            if actual_kind is None:
                raise _CompactIrMappingAuthorityError(
                    mapping.authority_item_id,
                    mapping.authority_target_kind,
                )
        return self

    def _authority_item_kinds_by_id(self) -> dict[str, AuthorityTargetKind]:
        """Return authority target IDs available to compact IR mappings."""
        item_kinds = {
            invariant.id: AuthorityTargetKind.INVARIANT for invariant in self.invariants
        }
        candidate_ids = {
            candidate.candidate_id for candidate in self.requirement_candidates
        }
        for index, _rule in enumerate(self.eligible_feature_rules, start=1):
            item_kinds[f"ELIG-{index}"] = AuthorityTargetKind.ELIGIBLE_FEATURE_RULE
            item_kinds[f"EFR-{index}"] = AuthorityTargetKind.ELIGIBLE_FEATURE_RULE
            for candidate_id in candidate_ids:
                item_kinds[
                    _generated_compact_target_id(
                        "EFR",
                        candidate_id,
                        AuthorityTargetKind.ELIGIBLE_FEATURE_RULE,
                        _rule.rule,
                    )
                ] = AuthorityTargetKind.ELIGIBLE_FEATURE_RULE
        for index, _feature in enumerate(self.rejected_features, start=1):
            item_kinds[f"REJ-{index}"] = AuthorityTargetKind.REJECTED_FEATURE
            item_kinds[f"RF-{index}"] = AuthorityTargetKind.REJECTED_FEATURE
            for candidate_id in candidate_ids:
                item_kinds[
                    _generated_compact_target_id(
                        "RF",
                        candidate_id,
                        AuthorityTargetKind.REJECTED_FEATURE,
                        _feature,
                    )
                ] = AuthorityTargetKind.REJECTED_FEATURE
        for index, _gap in enumerate(self.gaps, start=1):
            item_kinds[f"GAP-{index}"] = AuthorityTargetKind.GAP
            for candidate_id in candidate_ids:
                for finding_code in _COMPACT_IR_GAP_FINDING_CODES:
                    item_kinds[
                        _generated_compact_gap_id(
                            candidate_id,
                            finding_code,
                            _gap,
                        )
                    ] = AuthorityTargetKind.GAP
        for index, _assumption in enumerate(self.assumptions, start=1):
            item_kinds[f"ASM-{index}"] = AuthorityTargetKind.ASSUMPTION
            for candidate_id in candidate_ids:
                item_kinds[
                    _generated_compact_assumption_id(
                        candidate_id,
                        AuthorityTargetKind.ASSUMPTION,
                        _assumption,
                    )
                ] = AuthorityTargetKind.ASSUMPTION
        return item_kinds


def _normalize_compact_ir_text(text: str) -> str:
    return " ".join(text.strip().split())


def _compact_ir_canonical_hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


def _generated_compact_gap_id(
    candidate_id: str,
    finding_code: str,
    normalized_gap_text: str,
) -> str:
    payload = {
        "candidate_id": candidate_id,
        "finding_code": finding_code,
        "normalized_gap_text": _normalize_compact_ir_text(normalized_gap_text),
    }
    return f"GAP-{_compact_ir_canonical_hash(payload)}"


def _generated_compact_assumption_id(
    candidate_id: str,
    target_kind: AuthorityTargetKind,
    normalized_assumption_text: str,
) -> str:
    payload = {
        "candidate_id": candidate_id,
        "normalized_assumption_text": _normalize_compact_ir_text(
            normalized_assumption_text
        ),
        "target_kind": target_kind.value,
    }
    return f"ASM-{_compact_ir_canonical_hash(payload)}"


def _generated_compact_target_id(
    prefix: str,
    candidate_id: str,
    target_kind: AuthorityTargetKind,
    text: str,
) -> str:
    payload = {
        "candidate_id": candidate_id,
        "normalized_text": _normalize_compact_ir_text(text),
        "target_kind": target_kind.value,
    }
    return f"{prefix}-{_compact_ir_canonical_hash(payload)}"


class SpecAuthorityCompilationFailure(BaseModel):
    """Structured failure response from compiler agent."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Annotated[
        Literal["agileforge.compiled_authority.v2"],
        Field(description="Stored compiled-authority schema version."),
    ] = "agileforge.compiled_authority.v2"
    error: Annotated[
        str,
        Field(description="Error code for compilation failure."),
    ]
    reason: Annotated[
        str,
        Field(description="Short reason for failure."),
    ]
    blocking_gaps: Annotated[
        list[str],
        Field(description="Blocking gaps that prevented compilation."),
    ]


class SpecAuthorityCompilerOutput(
    RootModel[SpecAuthorityCompilationSuccess | SpecAuthorityCompilationFailure]
):
    """Root output schema for spec authority compilation."""


class SpecAuthorityCompilerEnvelope(BaseModel):
    """Envelope schema for spec authority compilation output."""

    model_config = ConfigDict(extra="forbid")

    result: Annotated[
        SpecAuthorityCompilationSuccess | SpecAuthorityCompilationFailure,
        Field(description="Compiler output payload."),
    ]


class StoryDraftMetadata(BaseModel):
    """Traceability metadata for drafted stories."""

    model_config = ConfigDict(extra="forbid")

    spec_version_id: Annotated[
        int,
        Field(description="Pinned compiled spec version ID used for this story."),
    ]


class StoryDraft(BaseModel):
    """
    Schema for a User Story draft.

    NOTE: feature_id and feature_title are NOT part of this schema.
    They are preserved from input state to prevent LLM override causing data corruption.
    """

    model_config = ConfigDict(extra="forbid")

    title: Annotated[
        str,
        Field(description="Short, action-oriented title for the story."),
    ]
    description: Annotated[
        str,
        Field(
            description=(
                "The story narrative in the format: "
                "'As a <persona>, I want <action> so that <benefit>.'"
            )
        ),
    ]
    acceptance_criteria: Annotated[
        str,
        Field(
            description=(
                "A list of 3-5 specific, testable criteria, each starting with '- '."
            )
        ),
    ]
    story_points: Annotated[
        int | None,
        Field(
            description=(
                "Estimated effort (1-8 points). Null if not estimable "
                "or if story points are disabled."
            )
        ),
    ]
    metadata: Annotated[
        StoryDraftMetadata,
        Field(description="Traceability metadata (must include spec_version_id)."),
    ]

    @model_validator(mode="after")
    def _validate_story_points(self) -> Self:
        if self.story_points is not None and (
            self.story_points < MIN_STORY_POINTS or self.story_points > MAX_STORY_POINTS
        ):
            raise _StoryPointRangeError
        return self

    @model_validator(mode="after")
    def _validate_description_format(self) -> Self:
        desc = self.description or ""
        desc_lower = desc.lower()
        if not desc_lower.startswith("as a"):
            raise _StoryDescriptionPrefixError
        if " i want " not in desc_lower:
            raise _StoryDescriptionWantError
        if " so that " not in desc_lower:
            raise _StoryDescriptionBenefitError
        return self


class StoryDraftInput(BaseModel):
    """Structured input payload for StoryDraftAgent."""

    model_config = ConfigDict(extra="forbid")

    current_feature: Annotated[
        dict[str, Any],
        Field(
            description=(
                "Feature context (id, title, theme, epic, time_frame, "
                "justification, siblings)."
            )
        ),
    ]
    product_context: Annotated[
        dict[str, Any],
        Field(description="Product context (id, name, vision, time_frame)."),
    ]
    spec_version_id: Annotated[
        int,
        Field(description="Pinned compiled spec version ID."),
    ]
    authority_context: Annotated[
        dict[str, Any],
        Field(
            description=(
                "Compiled authority context (scope themes, invariants, "
                "gaps, assumptions, hashes)."
            )
        ),
    ]
    user_persona: Annotated[
        str | None,
        Field(description="Optional persona hint. Use if provided; otherwise infer."),
    ] = None
    story_preferences: Annotated[
        dict[str, Any],
        Field(description="Story preferences (e.g., include_story_points)."),
    ]
    refinement_feedback: Annotated[
        str,
        Field(description="Validator feedback from previous attempt, or empty string."),
    ]
    raw_spec_text: Annotated[
        str | None,
        Field(description="Optional raw spec text for phrasing only."),
    ] = None


class NegationCheckInput(BaseModel):
    """Structured input payload for NegationCheckerAgent."""

    model_config = ConfigDict(extra="forbid")

    text: Annotated[
        str,
        Field(min_length=1, description="Full text where the forbidden term appears."),
    ]
    forbidden_term: Annotated[
        str,
        Field(min_length=1, description="Forbidden capability term detected in text."),
    ]
    context_label: Annotated[
        str,
        Field(min_length=1, description="Context label (e.g., story, feature)."),
    ]


class NegationCheckOutput(BaseModel):
    """Structured output payload for NegationCheckerAgent."""

    model_config = ConfigDict(extra="forbid")

    is_negated: Annotated[
        bool,
        Field(
            description=(
                "True if the forbidden term is only mentioned as a "
                "prohibition or negation."
            )
        ),
    ]
    confidence: Annotated[
        int,
        Field(ge=0, le=100, description="Confidence score from 0 to 100."),
    ]
    rationale: Annotated[
        str,
        Field(min_length=1, description="Short rationale for the decision."),
    ]


class StoryRefinerInput(BaseModel):
    """Structured input payload for StoryRefinerAgent."""

    model_config = ConfigDict(extra="allow")

    story_draft: Annotated[
        Any | None,
        Field(description="Original story draft payload from state."),
    ]
    spec_validation_result: Annotated[
        Any | None,
        Field(description="Spec validation feedback from state."),
    ]
    authority_context: Annotated[
        dict[str, Any],
        Field(
            description=(
                "Compiled authority context (scope themes, invariants, "
                "gaps, assumptions)."
            )
        ),
    ]
    spec_version_id: Annotated[
        int,
        Field(description="Pinned compiled spec version ID."),
    ]
    current_feature: Annotated[
        dict[str, Any],
        Field(
            description=(
                "Feature context (id, title, theme, epic, time_frame, "
                "justification, siblings)."
            )
        ),
    ]
    story_preferences: Annotated[
        dict[str, Any],
        Field(description="Story preferences (e.g., include_story_points)."),
    ]
    raw_spec_text: Annotated[
        str | None,
        Field(description="Optional raw spec text for phrasing only."),
    ] = None
