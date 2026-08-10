"""Framework-neutral workflow graph domain package."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from workflow.contracts import (
    GRAPH_VERSION,
    NodeDecision,
    TransitionResult,
    WorkflowError,
    WorkflowPosition,
)
from workflow.facts import WorkflowFactSnapshot
from workflow.fingerprints import decision_fingerprint, fact_fingerprint
from workflow.requests import (
    ApplyStoryDependencies,
    CompileAuthority,
    DecideAuthority,
    DecideBacklog,
    DecideRoadmap,
    DecideSprintPlan,
    DecideStory,
    RecordAuthorityFeedback,
    RecordBacklogDraft,
    RecordRoadmapDraft,
    RecordSprintPlan,
    RecordStoryDraft,
    RepairAuthority,
    RepairStoryReadiness,
    StartSprint,
    TransitionRequest,
)

if TYPE_CHECKING:
    from workflow.domain import WorkflowDomain

_LAZY_EXPORTS: dict[str, str] = {"WorkflowDomain": "workflow.domain"}

__all__ = [
    "GRAPH_VERSION",
    "ApplyStoryDependencies",
    "CompileAuthority",
    "DecideAuthority",
    "DecideBacklog",
    "DecideRoadmap",
    "DecideSprintPlan",
    "DecideStory",
    "NodeDecision",
    "RecordAuthorityFeedback",
    "RecordBacklogDraft",
    "RecordRoadmapDraft",
    "RecordSprintPlan",
    "RecordStoryDraft",
    "RepairAuthority",
    "RepairStoryReadiness",
    "StartSprint",
    "TransitionRequest",
    "TransitionResult",
    "WorkflowDomain",
    "WorkflowError",
    "WorkflowFactSnapshot",
    "WorkflowPosition",
    "decision_fingerprint",
    "fact_fingerprint",
]


def __getattr__(name: str) -> object:
    """Load the domain service without creating package import cycles."""
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
