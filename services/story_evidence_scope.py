# services/story_evidence_scope.py
"""Stable disclosure boundary for provider-free Story structural evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from workflow.contracts import JsonObject

STRUCTURAL_EVIDENCE_PROVES: tuple[str, ...] = (
    "exact Story identity",
    "immutable accepted Story artifact/item binding",
    "accepted Backlog and Specification lineage",
    "parent-bounded Specification references",
    "required Story shape",
    "non-empty acceptance criteria",
    "current evidence and input fingerprints",
)
STRUCTURAL_EVIDENCE_DOES_NOT_PROVE: tuple[str, ...] = (
    "semantic/model quality",
    "product value",
    "human Sprint selection",
    "dependency safety",
    "Sprint candidacy",
    "Sprint-generation readiness",
)


def structural_evidence_scope_payload() -> JsonObject:
    """Return fresh JSON-safe proof and non-proof lists for every read surface."""
    return cast(
        "JsonObject",
        {
            "proves": list(STRUCTURAL_EVIDENCE_PROVES),
            "does_not_prove": list(STRUCTURAL_EVIDENCE_DOES_NOT_PROVE),
        },
    )


__all__ = [
    "STRUCTURAL_EVIDENCE_DOES_NOT_PROVE",
    "STRUCTURAL_EVIDENCE_PROVES",
    "structural_evidence_scope_payload",
]
