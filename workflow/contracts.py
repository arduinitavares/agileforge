"""Framework-neutral immutable workflow graph contracts."""

from __future__ import annotations

import datetime as _datetime
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
)
from pydantic_core import PydanticCustomError

GRAPH_VERSION: str = "agileforge.workflow.v2"
_DATETIME = _datetime.datetime
_INVALID_JSON_KEY_CODE = "invalid_json_key"
_INVALID_JSON_KEY = "Transition output mappings must use string keys."
type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


def _freeze_json_value(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise PydanticCustomError(_INVALID_JSON_KEY_CODE, _INVALID_JSON_KEY)
            frozen[key] = _freeze_json_value(item)
        return MappingProxyType(frozen)
    if isinstance(value, list | tuple):
        return tuple(_freeze_json_value(item) for item in value)
    msg = f"Transition output does not support {type(value).__name__} values."
    raise ValueError(msg)


def _freeze_json_object(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(
        {key: _freeze_json_value(item) for key, item in value.items()}
    )


def _dump_json_value(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        dumped: JsonObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                msg = "Frozen JSON mappings must use string keys."
                raise TypeError(msg)
            dumped[key] = _dump_json_value(item)
        return dumped
    if isinstance(value, tuple | list):
        return [_dump_json_value(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    msg = f"Unsupported frozen JSON value: {type(value).__name__}."
    raise TypeError(msg)


def _dump_json_object(value: object) -> JsonObject:
    dumped = _dump_json_value(value)
    if not isinstance(dumped, dict):
        msg = "Transition output must serialize to a JSON object."
        raise TypeError(msg)
    return dumped


class FrozenModel(BaseModel):
    """Base model for immutable public workflow records."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class NodeCategory(StrEnum):
    """Availability category assigned to an evaluated node."""

    AVAILABLE = "available"
    WAITING = "waiting"
    BLOCKED = "blocked"
    INVALID = "invalid"


class RecommendationKind(StrEnum):
    """Why an evaluated node is recommended."""

    REQUIRED = "required"
    OPTIONAL_REENTRY = "optional_reentry"
    RECOVERY = "recovery"


class WorkflowErrorCode(StrEnum):
    """Closed error categories for guarded workflow transitions."""

    STALE_POSITION = "STALE_POSITION"
    TRANSITION_NOT_AVAILABLE = "TRANSITION_NOT_AVAILABLE"
    WORKFLOW_FACT_CONFLICT = "WORKFLOW_FACT_CONFLICT"
    ATTEMPT_OBSOLETE = "ATTEMPT_OBSOLETE"
    EXTERNAL_EXECUTION_FAILED = "EXTERNAL_EXECUTION_FAILED"
    SPRINT_CAPACITY_REQUIRED = "SPRINT_CAPACITY_REQUIRED"
    PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
    REPOSITORY_BINDING_INVALID = "REPOSITORY_BINDING_INVALID"
    REPOSITORY_PROVENANCE_STALE = "REPOSITORY_PROVENANCE_STALE"
    REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION = (
        "REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION"
    )
    VISION_EVIDENCE_STALE = "VISION_EVIDENCE_STALE"
    STALE_SPECIFICATION_INPUT = "STALE_SPECIFICATION_INPUT"
    STALE_SPECIFICATION_BASE = "STALE_SPECIFICATION_BASE"
    STALE_SPECIFICATION = "STALE_SPECIFICATION"
    SPECIFICATION_CANDIDATE_CONFLICT = "SPECIFICATION_CANDIDATE_CONFLICT"
    SPECIFICATION_AMENDMENT_MISMATCH = "SPECIFICATION_AMENDMENT_MISMATCH"
    INVALID_SPECIFICATION_PAYLOAD = "INVALID_SPECIFICATION_PAYLOAD"
    UNSUPPORTED_SPECIFICATION_SCHEMA = "UNSUPPORTED_SPECIFICATION_SCHEMA"
    SPECIFICATION_OUTPUT_INCOMPLETE = "SPECIFICATION_OUTPUT_INCOMPLETE"
    SPECIFICATION_PRODUCER_FAILED = "SPECIFICATION_PRODUCER_FAILED"
    SPRINT_PLAN_STREAM_ID_COLLISION = "SPRINT_PLAN_STREAM_ID_COLLISION"
    ACTIVE_SPRINT_EXISTS = "ACTIVE_SPRINT_EXISTS"


class FactReference(FrozenModel):
    """Reference to an immutable fact used in an evaluation."""

    fact_type: str
    fact_id: str
    fingerprint: str


class Blocker(FrozenModel):
    """Reason an evaluated node cannot be selected."""

    code: str
    message: str
    fact_references: tuple[FactReference, ...] = ()


class InputField(FrozenModel):
    """Typed user or system input required by a node."""

    name: str
    value_type: Literal["string", "integer", "boolean", "object", "array"]
    required: bool = True


class NodeDecision(FrozenModel):
    """Immutable decision for one workflow graph node instance."""

    node_id: str
    instance_key: str | None = None
    child_graph_id: str
    request_kind: str
    category: NodeCategory
    recommendation_kind: RecommendationKind
    reason_code: str
    required_inputs: tuple[InputField, ...] = ()
    fact_references: tuple[FactReference, ...] = ()
    blockers: tuple[Blocker, ...] = ()
    valid_until: _DATETIME | None = None
    decision_fingerprint: str


class WorkflowPosition(FrozenModel):
    """Immutable result of evaluating the workflow graph for a project."""

    project_id: int
    graph_version: str
    fact_fingerprint: str
    evaluated_at: _DATETIME
    available_nodes: tuple[str, ...]
    waiting_nodes: tuple[str, ...]
    blocked_nodes: tuple[str, ...]
    invalid_nodes: tuple[str, ...]
    terminal: bool
    decisions: tuple[NodeDecision, ...]


class WorkflowError(FrozenModel):
    """Structured failure returned from a guarded workflow transition."""

    code: WorkflowErrorCode
    message: str
    blockers: tuple[Blocker, ...] = ()


class TransitionResult(FrozenModel):
    """Immutable outcome of a workflow transition request."""

    ok: bool
    replayed: bool = False
    applied_node_id: str | None = None
    output: Mapping[str, object] = Field(
        default_factory=dict,
        validate_default=True,
    )
    position: WorkflowPosition | None = None
    error: WorkflowError | None = None

    @field_validator("output", mode="after")
    @classmethod
    def freeze_output(cls, value: Mapping[str, object]) -> Mapping[str, object]:
        """Recursively freeze validated JSON output."""
        return _freeze_json_object(value)

    @field_serializer("output")
    def serialize_output(self, value: object) -> JsonObject:
        """Serialize immutable output back to ordinary JSON containers."""
        return _dump_json_object(value)
