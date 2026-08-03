"""Input and output schemas for the User Story Writer agent."""

from typing import Annotated, Any, Literal, Self, TypeGuard

from pydantic import BaseModel, ConfigDict, Field, model_validator

STORY_QUALITY_SCHEMA_VERSION = "agileforge.story_quality.v1"

_LOW_WARNING_PLACEHOLDER_STRINGS = {
    "only include this key if score is low",
    "only include this key if the score is low",
    "omit for high or medium",
    "null",
    "none",
    "n/a",
}


def _is_string_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    """Narrow a validator input dictionary to string keys."""
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


class SaveStoriesInput(BaseModel):
    """Input schema for the temporary legacy Story write entry point."""

    idempotency_key: Annotated[
        str,
        Field(
            description="Stable key used to safely replay the same persistence call."
        ),
    ]
    product_id: Annotated[
        int,
        Field(description="The product ID to attach stories to."),
    ]
    parent_requirement: Annotated[
        str,
        Field(description="The roadmap requirement these stories decompose."),
    ]
    parent_rank: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description=(
                "1-based Roadmap parent order used to derive deterministic child "
                "story rank."
            ),
        ),
    ] = None
    story_origin: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional persistence origin override for extension scope.",
        ),
    ] = None
    accepted_spec_version_id: Annotated[
        int | None,
        Field(
            default=None,
            description="Accepted amended spec version that produced these stories.",
        ),
    ] = None
    stories: Annotated[
        list[dict[str, Any]],
        Field(
            description=(
                "List of approved story dicts from user_story_writer_tool output. "
                "Each must have: story_title, statement, acceptance_criteria, "
                "invest_score."
            ),
        ),
    ]


class SaveStoryPatchInput(BaseModel):
    """Input schema for saving one targeted Story refinement patch."""

    idempotency_key: Annotated[
        str,
        Field(
            description="Stable key used to safely replay the same persistence call."
        ),
    ]
    product_id: Annotated[
        int,
        Field(description="The product ID that owns the target story."),
    ]
    parent_requirement: Annotated[
        str,
        Field(description="The roadmap requirement that owns the target story."),
    ]
    parent_rank: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description=(
                "1-based Roadmap parent order used to derive deterministic child "
                "story rank."
            ),
        ),
    ] = None
    target_story_id: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Existing story ID to update. Mutually exclusive with "
                "target_refinement_slot."
            ),
        ),
    ] = None
    target_refinement_slot: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description=(
                "Existing refinement slot to update. Mutually exclusive with "
                "target_story_id."
            ),
        ),
    ] = None
    story_origin: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional persistence origin override for extension scope.",
        ),
    ] = None
    accepted_spec_version_id: Annotated[
        int | None,
        Field(
            default=None,
            description="Accepted amended spec version that produced the story.",
        ),
    ] = None
    story: Annotated[
        dict[str, Any],
        Field(
            description=(
                "Single approved story dict from user_story_writer_tool output. "
                "Must have: story_title, statement, acceptance_criteria, invest_score."
            ),
        ),
    ]

    @model_validator(mode="after")
    def _validate_exactly_one_target(self) -> Self:
        has_story_id = self.target_story_id is not None
        has_slot = self.target_refinement_slot is not None
        if has_story_id == has_slot:
            msg = (
                "Exactly one of target_story_id or target_refinement_slot is required."
            )
            raise ValueError(msg)
        return self


class StoryQualityFinding(BaseModel):
    """Machine-readable Story draft quality finding."""

    model_config = ConfigDict(extra="forbid")

    code: Annotated[
        str,
        Field(min_length=3, description="Stable quality finding code."),
    ]
    severity: Annotated[
        Literal["blocking", "warning"],
        Field(description="Whether this finding blocks save/review eligibility."),
    ]
    message: Annotated[
        str,
        Field(min_length=3, description="Reader-facing finding message."),
    ]
    affected_story_indexes: list[int] = Field(
        default_factory=list,
        description="1-based indexes of draft stories affected by the finding.",
    )
    affected_story_titles: list[str] = Field(
        default_factory=list,
        description="Draft story titles affected by the finding.",
    )


class UserStoryItem(BaseModel):
    """A single user story produced by the User Story Writer (Page 69)."""

    model_config = ConfigDict(extra="forbid")

    story_title: Annotated[
        str,
        Field(
            min_length=3,
            description="Concise functional label for the story.",
        ),
    ]
    statement: Annotated[
        str,
        Field(
            min_length=10,
            description=(
                "The story narrative in strict format: "
                "'As a [role], I want [feature], so that [benefit].' (Page 72)"
            ),
        ),
    ]
    acceptance_criteria: Annotated[
        list[str],
        Field(
            min_length=1,
            description=(
                "Testable Conditions of Satisfaction (Page 77). "
                "Functional: 'Verify that ...', Non-functional: 'Ensure that ...'."
            ),
        ),
    ]
    invest_score: Annotated[
        Literal["High", "Medium", "Low"],
        Field(
            description=(
                "INVEST compliance quality grade for this story (Page 73). True "
                "effort is tracked in estimated_effort."
            )
        ),
    ]
    estimated_effort: Annotated[
        Literal["XS", "S", "M", "L", "XL"],
        Field(
            description=(
                "Estimated size/effort. XS = hours, S = 1 day, M = 2-3 days. "
                "Small tasks like documentation should be XS/S, never artificially "
                "split to fill larger buckets."
            )
        ),
    ]
    produced_artifacts: list[str] = Field(
        default_factory=list,
        description=(
            "List of specific artifacts, documents, or deliverables this story "
            "produces."
        ),
    )
    research_caveats: list[str] = Field(
        default_factory=list,
        description=(
            "Advisory uncertainty or research risk notes. These do not by "
            "themselves lower INVEST score."
        ),
    )
    decomposition_warning: str | None = Field(
        default=None,
        description=(
            "Reason for low INVEST score. "
            "Include ONLY when invest_score is 'Low'. "
            "Omit (null) for 'High' or 'Medium'."
        ),
    )
    dependency_candidates: list["StoryDependencyCandidate"] = Field(
        default_factory=list,
        description=(
            "Proposed prerequisite story references. These are advisory until "
            "reviewed and applied as active story dependencies."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_placeholder_warning(cls, data: object) -> object:
        """Normalize warning/score mismatches before strict validation."""
        if not _is_string_object_dict(data):
            return data

        invest_score = data.get("invest_score")
        warning = data.get("decomposition_warning")
        if invest_score not in ("High", "Medium") or warning is None:
            return data

        if not isinstance(warning, str):
            return data

        normalized_warning = warning.strip()
        normalized = normalized_warning.lower()
        if not normalized or normalized in _LOW_WARNING_PLACEHOLDER_STRINGS:
            cleaned = dict(data)
            cleaned.pop("decomposition_warning", None)
            return cleaned

        cleaned = dict(data)
        cleaned["invest_score"] = "Low"
        cleaned["decomposition_warning"] = normalized_warning
        return cleaned

    @model_validator(mode="after")
    def _validate_statement_format(self) -> "UserStoryItem":
        """Enforce 'As a ... I want ... so that ...' syntax (Page 72)."""
        stmt = self.statement or ""
        stmt_lower = stmt.lower().strip()

        # Strip common markdown bolding just in case the agent formats it
        stmt_lower = stmt_lower.replace("**", "").replace("*", "")

        if not (stmt_lower.startswith(("as a ", "as an ", "as the "))):
            message = (
                "Statement must precisely start with 'As a ...', 'As an ...', "
                "or 'As the ...'"
            )
            raise ValueError(message)

        if " i want " not in stmt_lower:
            message = "Statement must contain '... I want ...'"
            raise ValueError(message)

        if " so that " not in stmt_lower:
            message = "Statement must contain '... so that ...'"
            raise ValueError(message)

        return self

    @model_validator(mode="after")
    def _validate_warning_consistency(self) -> "UserStoryItem":
        """decomposition_warning must be present only when invest_score is Low."""
        if (
            self.invest_score in ("High", "Medium")
            and self.decomposition_warning is not None
        ):
            message = (
                "decomposition_warning must be omitted (null) when invest_score is "
                "'High' or 'Medium'."
            )
            raise ValueError(message)
        if self.invest_score == "Low" and not self.decomposition_warning:
            message = "decomposition_warning is required when invest_score is 'Low'."
            raise ValueError(message)
        return self


class StoryDependencyCandidate(BaseModel):
    """Candidate prerequisite edge proposed by story generation."""

    model_config = ConfigDict(extra="forbid")

    prerequisite_ref: Annotated[
        str,
        Field(
            min_length=1,
            description="Story id, exact title, or source_requirement#slot reference.",
        ),
    ]
    reason: Annotated[
        str,
        Field(
            min_length=3,
            description="Why the prerequisite must precede this story.",
        ),
    ]
    confidence: Annotated[
        Literal["explicit", "inferred"],
        Field(
            description=(
                "Whether the source explicitly states the dependency or the model "
                "inferred it."
            ),
        ),
    ]


class UserStoryWriterInput(BaseModel):
    """Structured input payload for the User Story Writer agent.

    NOTE: No ``extra="forbid"`` or ``min_length`` constraints here.
    ADK's automatic function-calling parser cannot handle strict Pydantic
    config on input schemas.  Validation constraints belong on the
    *output* schema and internal models only.
    """

    parent_requirement: Annotated[
        str,
        Field(
            description="Roadmap item name (copied verbatim from roadmap).",
        ),
    ]
    requirement_context: Annotated[
        str,
        Field(
            description=(
                "Business justification and technical notes for this requirement."
            ),
        ),
    ]
    technical_spec: Annotated[
        str,
        Field(
            description="Relevant technical constraints and system behaviors.",
        ),
    ]
    compiled_authority: Annotated[
        str,
        Field(
            description="Regulatory, architectural, or organizational constraints.",
        ),
    ]
    global_roadmap_context: str = Field(
        default="",
        description=(
            "All roadmap milestones to provide boundaries on what NOT to implement."
        ),
    )
    already_generated_milestone_stories: str = Field(
        default="",
        description=(
            "Details of stories already generated for other requirements to avoid "
            "overlap."
        ),
    )
    artifact_registry: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping of artifact_key -> owner_requirement.",
    )


class UserStoryWriterOutput(BaseModel):
    """Structured output payload from the User Story Writer agent."""

    model_config = ConfigDict(extra="forbid")

    parent_requirement: Annotated[
        str,
        Field(description="Copied verbatim from input for traceability."),
    ]
    user_stories: Annotated[
        list[UserStoryItem],
        Field(
            min_length=1,
            max_length=8,
            description="List of decomposed, INVEST-compliant user stories.",
        ),
    ]
    quality_schema_version: Literal["agileforge.story_quality.v1"] = Field(
        default=STORY_QUALITY_SCHEMA_VERSION,
        description="Version of the Story draft quality contract.",
    )
    coverage_status: Literal[
        "complete",
        "partial_capacity_limited",
        "needs_clarification",
    ] = Field(
        default="complete",
        description="Whether this bounded attempt fully covers the request.",
    )
    remaining_scope: list[str] = Field(
        default_factory=list,
        description=(
            "Concrete uncovered terms, slices, or requested scope items when "
            "coverage_status is not complete."
        ),
    )
    quality_findings: list[StoryQualityFinding] = Field(
        default_factory=list,
        description="Machine-readable draft quality findings.",
    )
    is_complete: Annotated[
        bool,
        Field(
            description=(
                "True if all stories pass INVEST validation and fully cover "
                "the parent requirement. False if clarification is needed."
            ),
        ),
    ]
    clarifying_questions: list[str] = Field(
        default_factory=list,
        description="Questions for the user if is_complete is False.",
    )


class UserStoryPatchOutput(BaseModel):
    """Structured output payload for one targeted Story refinement patch."""

    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["story_patch"] = Field(
        default="story_patch",
        description="Discriminator for targeted Story patch artifacts.",
    )
    parent_requirement: Annotated[
        str,
        Field(description="Copied verbatim from input for traceability."),
    ]
    target_refinement_slot: Annotated[
        int,
        Field(
            ge=1,
            description="Canonical 1-based refinement slot being patched.",
        ),
    ]
    target_story_id: int | None = Field(
        default=None,
        description="Existing story ID when host-side target resolution knows it.",
    )
    story: Annotated[
        UserStoryItem,
        Field(description="The only story item included in a targeted patch artifact."),
    ]
    quality_schema_version: Literal["agileforge.story_quality.v1"] = Field(
        default=STORY_QUALITY_SCHEMA_VERSION,
        description="Version of the Story draft quality contract.",
    )
    coverage_status: Literal["complete", "needs_clarification"] = Field(
        default="complete",
        description=(
            "Whether this targeted patch fully resolves the requested refinement."
        ),
    )
    remaining_scope: list[str] = Field(
        default_factory=list,
        description="Concrete uncovered scope when coverage_status is not complete.",
    )
    quality_findings: list[StoryQualityFinding] = Field(
        default_factory=list,
        description="Machine-readable draft quality findings.",
    )
    is_complete: Annotated[
        bool,
        Field(
            description=(
                "True if the targeted patch passes validation. False if clarification "
                "is needed."
            ),
        ),
    ]
    clarifying_questions: list[str] = Field(
        default_factory=list,
        description="Questions for the user if is_complete is False.",
    )
