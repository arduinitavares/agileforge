"""Base request contracts for guarded workflow graph transitions."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, model_validator


class GuardedRequest(BaseModel):
    """Transition request guarded by a previously evaluated graph position."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    idempotency_key: str
    actor: str
    correlation_id: str | None = None


class PositionedRequest(GuardedRequest):
    """Guarded request for one graph node and optional durable attempt."""

    instance_key: str | None = None
    attempt_id: int | None = None
    attempt_fingerprint: str | None = None
    node_id: ClassVar[str]

    @model_validator(mode="after")
    def validate_attempt_guard_pair(self) -> PositionedRequest:
        """Require durable attempt identity and fingerprint together."""
        if (self.attempt_id is None) != (self.attempt_fingerprint is None):
            msg = "attempt_id and attempt_fingerprint must be provided together."
            raise ValueError(msg)
        return self

    def decision_node_id(self) -> str:
        """Return the node identifier associated with this request."""
        return self.node_id

    def decision_instance_key(self) -> str | None:
        """Return the node instance key associated with this request."""
        return self.instance_key
