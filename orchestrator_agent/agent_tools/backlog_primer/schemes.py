"""Input and output schemas for the Backlog Primer agent."""

from typing import Annotated, Literal

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
                "action-oriented Product Backlog Item title describing remaining work. "
                "In brownfield contexts this is not the capability name."
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
                "Optional model-authored brownfield hint. This is advisory only; "
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


class InputSchema(BaseModel):
    """Schema for the input to the backlog primer agent."""

    product_vision_statement: Annotated[
        str,
        Field(description="Final approved product vision statement."),
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
    as_built_assessment: Annotated[
        str,
        Field(
            description=(
                "Raw agileforge.as_built_assessment.v1 JSON from "
                "as_built_assessment_cached or 'NO_AS_BUILT_ASSESSMENT' when no "
                "fresh assessment cache is available."
            ),
        ),
    ]
    implementation_evidence: Annotated[
        str,
        Field(
            description=(
                "Raw ReconciliationReport JSON from implementation_evidence_cached "
                "or 'NO_EVIDENCE' when no evidence cache is available."
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
