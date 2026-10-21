# services/contracts/generation_policy.py
"""Safe evidence for a published provider capacity, distinct from prompt accounting."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic resolves annotations
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class GenerationAllowance(BaseModel):
    """One immutable capability snapshot used to construct a physical request."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["agileforge.generation-allowance.v1"] = (
        "agileforge.generation-allowance.v1"
    )
    model_id: Annotated[
        str, Field(pattern=r"^openrouter/[A-Za-z0-9_./:-]+$", max_length=256)
    ]
    completion_tokens: Annotated[int, Field(gt=0)]
    maximum_published_context: Annotated[int, Field(gt=0)] | None = None
    known_capacity_endpoints: Annotated[int, Field(gt=0)]
    unknown_capacity_endpoints: Annotated[int, Field(ge=0)]
    request_parameter: Literal["max_tokens", "max_completion_tokens"]
    selection: Literal["maximum_published_endpoint_capacity"] = (
        "maximum_published_endpoint_capacity"
    )
    actual_prompt_allowance: Literal["unverified"] = "unverified"
    route_eligibility: Literal["provider_enforced_unverified"] = (
        "provider_enforced_unverified"
    )
    source_url: Annotated[
        str,
        Field(
            pattern=r"^https://openrouter\.ai/api/v1/models/[A-Za-z0-9_./:-]+/endpoints$"
        ),
    ]
    snapshot_sha256: Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
    retrieved_at: datetime
