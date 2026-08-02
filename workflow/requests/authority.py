"""Typed requests for the shared authority workflow graph."""

from typing import ClassVar, Literal

from workflow.contracts import JsonObject
from workflow.requests.base import PositionedRequest


class CompileAuthority(PositionedRequest):
    """Compile the exact current registered specification."""

    kind: Literal["compile_authority"] = "compile_authority"
    node_id: ClassVar[str] = "authority.compile"
    spec_version_id: int
    expected_spec_hash: str
    compiler_model: str = "openrouter/openai/gpt-5.6-luna"


class DecideAuthority(PositionedRequest):
    """Record a terminal decision for one exact reviewed authority."""

    kind: Literal["decide_authority"] = "decide_authority"
    node_id: ClassVar[str] = "authority.review"
    pending_authority_id: int
    authority_fingerprint: str
    review_fingerprint: str
    decision: Literal["accepted", "rejected"]
    rationale: str


class RecordAuthorityFeedback(PositionedRequest):
    """Record immutable feedback for one rejected authority."""

    kind: Literal["record_authority_feedback"] = "record_authority_feedback"
    node_id: ClassVar[str] = "authority.feedback"
    pending_authority_id: int
    authority_fingerprint: str
    feedback: JsonObject


class RepairAuthority(PositionedRequest):
    """Compile a replacement for one exact rejected authority."""

    kind: Literal["repair_authority"] = "repair_authority"
    node_id: ClassVar[str] = "authority.repair"
    source_authority_id: int
    source_authority_fingerprint: str


__all__ = [
    "CompileAuthority",
    "DecideAuthority",
    "RecordAuthorityFeedback",
    "RepairAuthority",
]
