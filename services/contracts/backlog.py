"""Input and output schemas for the Backlog Primer agent."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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
            description=(
                "Action-oriented Project Backlog Item title describing remaining work."
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
    capability_hint: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional model-authored capability hint. This is advisory only; "
                "the host derives authoritative annotation."
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
        Field(
            description=(
                "Relative effort as an exact T-shirt size token: S, M, L, or XL. "
                "No qualifiers or suffixes (e.g. 'L (Bounded)' is invalid). "
                "Put sizing caveats in technical_note instead."
            ),
        ),
    ]
    technical_note: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional: sizing context, scope caveats, bounded-exploration notes, "
                "or effort rationale derived from technical_context."
            ),
        ),
    ]


class SaveBacklogInput(BaseModel):
    """Input schema for the temporary legacy Backlog write entry point."""

    model_config = ConfigDict(extra="forbid")

    project_id: Annotated[int, Field(description="The Project ID.")]
    idempotency_key: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional save idempotency key supplied by guarded callers.",
        ),
    ] = None
    backlog_items: Annotated[
        list[dict[str, Any]],
        Field(
            default_factory=list,
            description=(
                "List of approved backlog items from backlog_primer_tool output. "
                "Each must have: priority, requirement, value_driver, justification, "
                "estimated_effort."
            ),
        ),
    ]


class InputSchema(BaseModel):
    """Schema for the input to the backlog primer agent."""

    model_config = ConfigDict(extra="forbid")

    product_vision_statement: Annotated[
        str,
        Field(description="Final approved product vision statement."),
    ]
    product_goal_statement: Annotated[
        str,
        Field(description="The active accepted Product Goal statement."),
    ]
    technical_spec: Annotated[
        str,
        Field(
            description=(
                "Raw technical specification content (markdown or plain text)."
            ),
        ),
    ]
    compiled_authority: Annotated[
        str,
        Field(
            description=(
                "Compiled authority JSON artifact for constraints and invariants."
            ),
        ),
    ]
    prior_backlog_state: Annotated[
        str,
        Field(
            description=(
                "JSON string of the previous backlog state or 'NO_HISTORY' "
                "if starting fresh."
            ),
        ),
    ]
    user_input: Annotated[
        str | None,
        Field(
            description="User-provided notes, requirements, or answers to questions.",
        ),
    ]


class OutputSchema(BaseModel):
    """Schema for the backlog draft output."""

    model_config = ConfigDict(extra="forbid")

    backlog_items: Annotated[
        list[BacklogItem],
        Field(description="Prioritized high-level backlog requirements."),
    ]
    is_complete: Annotated[
        bool,
        Field(
            description=(
                "True if backlog has at least 10 well-formed items with "
                "priority, value justification, and estimates."
            ),
        ),
    ]
    clarifying_questions: Annotated[
        list[str],
        Field(
            description="Questions to resolve missing or ambiguous backlog details.",
        ),
    ]
