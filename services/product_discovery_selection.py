"""Canonical durable selection for Product Discovery replacement lineage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlmodel import Session

from repositories.workflow import WorkflowFactRepository
from workflow.definitions.product_discovery import current_specification_candidate

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


@dataclass(frozen=True)
class ProductDiscoverySelectionService:
    """Resolve exact candidate lineage without accepting operator-supplied IDs."""

    engine: Engine

    def resolve_specification_supersedes(self, project_id: int) -> int | None:
        """Return the current rejected or feedback candidate, if one exists."""
        with Session(self.engine) as session:
            snapshot = WorkflowFactRepository(session).load(project_id)
        candidate = current_specification_candidate(snapshot)
        if candidate is None:
            return None
        decisions = tuple(
            item
            for item in snapshot.specification_decisions
            if (
                item.specification_candidate_id
                == candidate.specification_candidate_id
                and item.artifact_fingerprint == candidate.content_fingerprint
            )
        )
        if len(decisions) != 1 or decisions[0].decision not in {
            "rejected",
            "feedback",
        }:
            return None
        return candidate.specification_candidate_id


__all__ = ["ProductDiscoverySelectionService"]
