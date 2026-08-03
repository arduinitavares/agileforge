"""Exact model roles used by retained production ADK recipes."""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from workflow.definitions.root import ROOT_GRAPH

AGENTIC_MODEL_ROLES: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        "onboarding.brownfield.curation": "brownfield_curator",
        "authority.compile": "spec_authority_compiler",
        "authority.repair": "spec_authority_compiler",
        "vision.generate": "product_vision",
        "backlog.generate": "backlog_primer",
        "planning.roadmap.generate": "roadmap_builder",
        "planning.story.generate": "user_story_writer",
        "planning.sprint.plan": "sprint_planner",
    }
)
RETAINED_MODEL_ROLES: Final[frozenset[str]] = frozenset(
    {*AGENTIC_MODEL_ROLES.values(), "spec_validator"}
)

if tuple(AGENTIC_MODEL_ROLES) != ROOT_GRAPH.agentic_node_ids:
    message = "Model roles must exactly match the live agentic recipe catalog."
    raise RuntimeError(message)

__all__ = ["AGENTIC_MODEL_ROLES", "RETAINED_MODEL_ROLES"]
