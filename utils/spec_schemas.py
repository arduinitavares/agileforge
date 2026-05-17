"""Spec/compiler/story-validation shared schemas."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

_DATETIME_TYPE = datetime
MIN_STORY_POINTS = 1
MAX_STORY_POINTS = 8


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


InvariantParameters = (
    ForbiddenCapabilityParams
    | RequiredFieldParams
    | MaxValueParams
    | RelationConstraintParams
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


class SpecAuthorityCompilationSuccess(BaseModel):
    """Successful spec authority compilation output."""

    model_config = ConfigDict(extra="forbid")

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


class SpecAuthorityCompilationFailure(BaseModel):
    """Structured failure response from compiler agent."""

    model_config = ConfigDict(extra="forbid")

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
