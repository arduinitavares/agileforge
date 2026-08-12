"""Typed direct Specification authoring and review requests."""

from typing import ClassVar, Literal

from pydantic import Field

from services.specs.candidate_contract import StableIdReplacement
from utils.agileforge_spec_profile_v2 import SpecificationPayload
from workflow.requests.base import PositionedRequest


class CompleteSpecificationAuthoring(PositionedRequest):
    """Continue one exact authoring attempt with provider semantic output only."""

    kind: Literal["complete_specification_authoring"] = (
        "complete_specification_authoring"
    )
    node_id: ClassVar[str] = "specification.author"
    attempt_id: int = Field(gt=0)
    attempt_fingerprint: str = Field(min_length=1)
    payload: SpecificationPayload
    removal_justifications: dict[str, str] = Field(default_factory=dict)
    stable_id_replacements: tuple[StableIdReplacement, ...] = ()


class DecideSpecification(PositionedRequest):
    """Record one human decision for an exact immutable candidate."""

    kind: Literal["decide_specification"] = "decide_specification"
    node_id: ClassVar[str] = "specification.review"
    specification_candidate_id: int = Field(gt=0)
    candidate_fingerprint: str = Field(min_length=1)
    repository_source_fingerprint: str | None = None
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: str = ""


__all__ = ["CompleteSpecificationAuthoring", "DecideSpecification"]
