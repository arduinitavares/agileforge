"""Requests for durable agentic node execution attempts."""

from typing import Literal

from pydantic import Field

from workflow.contracts import FrozenModel, JsonObject, WorkflowErrorCode
from workflow.requests.base import GuardedRequest


class StartNodeAttempt(GuardedRequest):
    """Start or recover one currently available registry-backed node."""

    kind: Literal["start_node_attempt"] = "start_node_attempt"
    target_node_id: str = Field(min_length=1)
    target_instance_key: str | None = None
    normalized_input: JsonObject
    model_id: str = Field(min_length=1)
    execution_settings: JsonObject
    lease_seconds: int = Field(ge=30, le=3_600)


class FailNodeAttempt(FrozenModel):
    """Record an external execution failure for one live attempt."""

    kind: Literal["fail_node_attempt"] = "fail_node_attempt"
    project_id: int
    attempt_id: int
    attempt_fingerprint: str = Field(min_length=1)
    failure_code: str = Field(min_length=1)
    failure_message: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class RevalidateNodeAttempt(FrozenModel):
    """Recheck one live Specification attempt before its provider call."""

    kind: Literal["revalidate_node_attempt"] = "revalidate_node_attempt"
    project_id: int
    attempt_id: int
    attempt_fingerprint: str = Field(min_length=1)
    target_node_id: Literal["specification.structure"] = "specification.structure"
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class ObsoleteNodeAttempt(FrozenModel):
    """Close one exact Specification attempt after a host source re-probe."""

    kind: Literal["obsolete_node_attempt"] = "obsolete_node_attempt"
    project_id: int
    attempt_id: int
    attempt_fingerprint: str = Field(min_length=1)
    error_code: Literal[WorkflowErrorCode.STALE_SPECIFICATION_INPUT] = (
        WorkflowErrorCode.STALE_SPECIFICATION_INPUT
    )
    error_message: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


__all__ = [
    "FailNodeAttempt",
    "ObsoleteNodeAttempt",
    "RevalidateNodeAttempt",
    "StartNodeAttempt",
]
