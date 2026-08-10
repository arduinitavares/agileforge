"""Base request contracts for guarded workflow graph transitions."""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal, Self, get_args, get_origin

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

type ReviewRationale = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class GuardedRequest(BaseModel):
    """Transition request guarded by a previously evaluated graph position."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)
    _is_request_scaffold: ClassVar[bool] = True

    kind: str
    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    idempotency_key: str
    actor: str
    correlation_id: str | None = None

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: object) -> None:
        """Reject concrete request classes with open discriminator fields."""
        super().__pydantic_init_subclass__(**kwargs)
        if cls.__dict__.get("_is_request_scaffold", False):
            return

        kind_field = cls.model_fields.get("kind")
        kind_annotation = None if kind_field is None else kind_field.annotation
        kind_values = get_args(kind_annotation)
        if (
            get_origin(kind_annotation) is not Literal
            or len(kind_values) != 1
            or not isinstance(kind_values[0], str)
            or not kind_values[0]
        ):
            msg = f"{cls.__name__} must declare kind as one non-empty string Literal."
            raise TypeError(msg)

        if issubclass(cls, PositionedRequest):
            node_id = cls.__dict__.get("node_id")
            if not isinstance(node_id, str) or not node_id.strip():
                msg = (
                    f"{cls.__name__} must declare a non-empty stable node_id ClassVar."
                )
                raise TypeError(msg)

    @model_validator(mode="after")
    def reject_request_scaffolding(self) -> Self:
        """Prevent the open base models from validating as transition actions."""
        if type(self).__dict__.get("_is_request_scaffold", False):
            msg = "GuardedRequest and PositionedRequest are request scaffolding only."
            raise ValueError(msg)
        return self


class PositionedRequest(GuardedRequest):
    """Guarded request for one graph node and optional durable attempt."""

    _is_request_scaffold: ClassVar[bool] = True
    instance_key: str | None = None
    attempt_id: int | None = None
    attempt_fingerprint: str | None = None
    node_id: ClassVar[str]

    @model_validator(mode="after")
    def validate_attempt_guard_pair(self) -> Self:
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
