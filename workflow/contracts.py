"""Framework-neutral immutable workflow graph contracts."""

from __future__ import annotations

import datetime as _datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

GRAPH_VERSION: str = "agileforge.workflow.v1"
_DATETIME = _datetime.datetime
type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


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
    output: JsonObject = Field(default_factory=dict)
    position: WorkflowPosition | None = None
    error: WorkflowError | None = None
