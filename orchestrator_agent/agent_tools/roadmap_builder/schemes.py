"""Input and output schemas for the Roadmap Builder agent."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestrator_agent.agent_tools.as_built_assessor.schemes import (
    AssessmentStatus,
    BacklogTreatment,
)


class BacklogItem(BaseModel):
    """A single high-level backlog requirement with priority and estimate."""

    model_config = ConfigDict(extra="forbid")

    priority: Annotated[
        int,
        Field(
            ge=1,
            description="Priority rank (1 is highest). Must be a positive integer.",
        ),
    ]
    requirement: Annotated[
        str,
        Field(
            min_length=3,
            description="Action-oriented backlog work item title.",
        ),
    ]
    capability_name: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional As-Built capability name this backlog item relates to."
            ),
        ),
    ]
    authority_ref: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional authority target reference associated with this item."
            ),
        ),
    ]
    as_built_status: Annotated[
        AssessmentStatus | None,
        Field(
            default=None,
            description=(
                "Optional As-Built assessment status for the related capability."
            ),
        ),
    ]
    recommended_backlog_treatment: Annotated[
        BacklogTreatment | None,
        Field(
            default=None,
            description=(
                "Optional As-Built recommended backlog treatment for this item."
            ),
        ),
    ]
    value_driver: Annotated[
        Literal["Revenue", "Customer Satisfaction", "Strategic"],
        Field(description="Primary value driver for prioritization."),
    ]
    justification: Annotated[
        str,
        Field(
            min_length=3,
            description="Why this priority (linked to vision and value driver).",
        ),
    ]
    estimated_effort: Annotated[
        Literal["S", "M", "L", "XL"],
        Field(description="Relative effort using T-shirt size: S, M, L, XL."),
    ]
    technical_note: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional technical rationale for sizing.",
        ),
    ]


class RoadmapBuilderInput(BaseModel):
    """Input for the Roadmap Builder agent."""

    # We allow extra fields in input because context might add more than we need
    model_config = ConfigDict(extra="ignore")

    backlog_items: Annotated[
        list[BacklogItem],
        Field(description="List of prioritized backlog items from Stage 1."),
    ]
    product_vision: Annotated[
        str,
        Field(description="The full product vision text."),
    ]
    technical_spec: Annotated[
        str,
        Field(description="The technical specification text."),
    ]
    compiled_authority: Annotated[
        str,
        Field(description="The compiled authority text/JSON."),
    ]
    time_increment: Annotated[
        str,
        Field(
            default="Milestone-based",
            description="Start date or time increment strategy.",
        ),
    ]
    prior_roadmap_state: Annotated[
        str,
        Field(
            default="NO_HISTORY",
            description="Previous roadmap JSON for refinement, or 'NO_HISTORY' for first call.",
        ),
    ]
    user_input: Annotated[
        str,
        Field(
            default="",
            description="User's specific requests, feedback, or constraints.",
        ),
    ]


class RoadmapRelease(BaseModel):
    """A single release/milestone in the roadmap."""

    model_config = ConfigDict(extra="forbid")

    release_name: Annotated[
        str,
        Field(description="Name of the release (e.g., 'Milestone 1')."),
    ]
    theme: Annotated[
        str,
        Field(description="Short goal description or theme derived from Vision."),
    ]
    focus_area: Annotated[
        Literal["Technical Foundation", "User Value", "Scale", "Other"],
        Field(description="Primary focus area of this release."),
    ]
    items: Annotated[
        list[str],
        Field(description="List of Requirement Names included in this release."),
    ]
    reasoning: Annotated[
        str,
        Field(description="Reasoning for item selection (dependencies, value)."),
    ]


class RoadmapBuilderOutput(BaseModel):
    """Output schema for the Roadmap."""

    model_config = ConfigDict(extra="forbid")

    roadmap_releases: Annotated[
        list[RoadmapRelease],
        Field(description="Ordered list of roadmap releases/milestones."),
    ]
    roadmap_summary: Annotated[
        str,
        Field(description="Narrative summary of the roadmap strategy."),
    ]
    is_complete: Annotated[
        bool,
        Field(
            description="True if roadmap is complete and ready for review. False if clarification needed."
        ),
    ]
    clarifying_questions: Annotated[
        list[str],
        Field(
            default_factory=list,
            description="Questions to ask user if is_complete=False.",
        ),
    ]
