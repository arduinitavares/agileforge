"""Typed Specification source, structuring, and review requests."""

from typing import ClassVar, Literal

from pydantic import Field

from services.contracts.specification_source import SpecificationSourceBundle
from services.specs.candidate_contract import StableIdReplacement
from utils.agileforge_spec_profile_v2 import SpecificationPayload
from workflow.requests.base import PositionedRequest


class RegisterSpecificationSource(PositionedRequest):
    """Persist one host-captured exact external to-spec source bundle."""

    kind: Literal["register_specification_source"] = "register_specification_source"
    node_id: ClassVar[str] = "specification.source.register"
    accepted_vision_artifact_id: int = Field(gt=0)
    accepted_product_goal_artifact_id: int = Field(gt=0)
    repository_binding_id: int = Field(gt=0)
    repository_binding_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    capture_request_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    bundle: SpecificationSourceBundle


class CompleteSpecificationStructuring(PositionedRequest):
    """Continue one exact structuring attempt with provider semantic output only."""

    kind: Literal["complete_specification_structuring"] = (
        "complete_specification_structuring"
    )
    node_id: ClassVar[str] = "specification.structure"
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


__all__ = [
    "CompleteSpecificationStructuring",
    "DecideSpecification",
    "RegisterSpecificationSource",
]
