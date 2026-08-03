"""This module defines the input and output schemas for the Product Vision Agent."""

from typing import Annotated

from pydantic import BaseModel, Field


class SaveVisionInput(BaseModel):
    """Schema for the temporary legacy Vision write entry point."""

    product_id: Annotated[
        int | None,
        Field(
            description="ID of the project to update. If None, creates a NEW project."
        ),
    ] = None
    project_name: Annotated[str, Field(description="Unique name of the project.")]
    product_vision_statement: Annotated[
        str,
        Field(description="Finalized vision text."),
    ]


class VisionComponents(BaseModel):
    """The granular components of the vision.

    This is the object we serialize/deserialize to DB.
    """

    # NOTE: We use Optional[str] and instruct the LLM to use 'null'
    # so we don't have to parse strings like "UNKNOWN" or "N/A".

    project_name: Annotated[
        str | None,
        Field(description="Name of project. Return null if not yet defined."),
    ]
    target_user: Annotated[
        str | None,
        Field(description="Who is the customer? Return null if ambiguous or unknown."),
    ]
    problem: Annotated[
        str | None,
        Field(description="What is the pain point? Return null if unknown."),
    ]
    product_category: Annotated[
        str | None,
        Field(
            description="What is it? (App, Service, Device). Return null if unknown."
        ),
    ]
    key_benefit: Annotated[
        str | None,
        Field(description="Primary value proposition. Return null if unknown."),
    ]
    competitors: Annotated[
        str | None,
        Field(description="Existing alternatives. Return null if unknown."),
    ]
    differentiator: Annotated[
        str | None,
        Field(description="Why us? (USP). Return null if unknown."),
    ]

    def is_fully_defined(self) -> bool:
        """Return whether all seven fields contain non-empty values."""
        # We check strictly for None or empty whitespace
        missing_fields = [
            k
            for k, v in self.model_dump().items()
            if v is None or (isinstance(v, str) and not v.strip()) or v == "/UNKNOWN"
        ]
        return len(missing_fields) == 0


class InputSchema(BaseModel):
    """
    Schema for the input arguments the Orchestrator MUST provide to the tool.

    CRITICAL: All fields must be REQUIRED (no defaults) so the Google ADK
    knows to force the Orchestrator to generate/provide them.
    """

    user_raw_text: Annotated[
        str,
        Field(
            description="The latest instruction or feedback text provided by the user."
        ),
    ]
    specification_content: Annotated[
        str,
        Field(
            description=(
                "The full text of a specification file to analyze. If no spec file "
                "is available, pass an empty string."
            )
        ),
    ]
    prior_vision_state: Annotated[
        str,
        Field(
            description=(
                "The raw JSON string representing the previous 'VisionComponents' "
                "state. "
                "If this is the first turn, pass the string 'NO_HISTORY'. "
                "Do not attempt to parse or summarize this; pass it exactly as "
                "received."
            ),
        ),
    ]
    compiled_authority: Annotated[
        str,
        Field(
            description=(
                "Compiled authority JSON (invariants/constraints) from the "
                "orchestrator. "
                "If no authority is available yet, pass an empty string."
            ),
        ),
    ]


class OutputSchema(BaseModel):
    """The structured response returned by the Product Vision Agent."""

    # A. The State (To be saved to DB)
    updated_components: Annotated[
        VisionComponents,
        Field(
            description="The updated state object containing the 7 vision components."
        ),
    ]

    # B. The Artifact (To be shown to User)
    product_vision_statement: Annotated[
        str,
        Field(
            description=(
                "A natural language vision statement generated from the components. "
                "If components are missing, draft what you have with placeholders."
            )
        ),
    ]

    # C. Metadata (For Orchestrator logic)
    is_complete: Annotated[
        bool,
        Field(
            description=(
                "True ONLY if all 7 components are fully defined in updated_components."
            )
        ),
    ]

    clarifying_questions: Annotated[
        list[str],
        Field(
            description=(
                "A list of specific questions to ask the user to fill missing "
                "components."
            )
        ),
    ]
