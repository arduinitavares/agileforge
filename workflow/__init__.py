"""Framework-neutral workflow graph domain package."""

from workflow.contracts import (
    GRAPH_VERSION,
    NodeDecision,
    TransitionResult,
    WorkflowError,
    WorkflowPosition,
)
from workflow.facts import WorkflowFactSnapshot
from workflow.fingerprints import decision_fingerprint, fact_fingerprint

__all__ = [
    "GRAPH_VERSION",
    "NodeDecision",
    "TransitionResult",
    "WorkflowError",
    "WorkflowFactSnapshot",
    "WorkflowPosition",
    "decision_fingerprint",
    "fact_fingerprint",
]
