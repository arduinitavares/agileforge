"""Strict read projection for the current accepted immutable Project Vision."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from workflow.definitions.product_goal import accepted_current_vision

if TYPE_CHECKING:
    from sqlmodel import Session


class _VisionLineageIssue(StrEnum):
    MULTIPLE_LEAVES = "Vision artifact lineage has multiple current leaves."
    UNACCEPTED_PARENT = "Superseded Vision artifact was not accepted."


class VisionLineageError(RuntimeError):
    """Raised when durable Vision rows cannot identify one trustworthy current state."""

    def __init__(
        self,
        issue: _VisionLineageIssue | WorkflowFactLoadError,
    ) -> None:
        """Preserve a closed selector issue or canonical fact-load failure."""
        self.issue = issue
        super().__init__(str(issue))


@dataclass(frozen=True)
class AcceptedVision:
    """Stable public fields of the current accepted Vision artifact."""

    vision_artifact_id: int
    fingerprint: str
    statement: str


def load_current_accepted_vision(
    session: Session,
    *,
    project_id: int,
) -> AcceptedVision | None:
    """Validate durable Vision lineage and return its accepted leaf, if any."""
    try:
        snapshot = WorkflowFactRepository(session).load_vision_snapshot(project_id)
    except WorkflowFactLoadError as error:
        raise VisionLineageError(error) from error
    superseded_ids = {
        artifact.supersedes_vision_artifact_id
        for artifact in snapshot.vision_artifacts
        if artifact.supersedes_vision_artifact_id is not None
    }
    leaves = [
        artifact
        for artifact in snapshot.vision_artifacts
        if artifact.vision_artifact_id not in superseded_ids
    ]
    if not leaves:
        return None
    if len(leaves) != 1:
        raise VisionLineageError(_VisionLineageIssue.MULTIPLE_LEAVES)
    decisions = {
        decision.vision_artifact_id: decision
        for decision in snapshot.vision_artifact_decisions
    }
    if any(
        decisions.get(artifact_id) is None
        or decisions[artifact_id].decision != "accepted"
        for artifact_id in superseded_ids
    ):
        raise VisionLineageError(_VisionLineageIssue.UNACCEPTED_PARENT)
    vision = accepted_current_vision(snapshot)
    if vision is None:
        return None
    return AcceptedVision(
        vision_artifact_id=vision.vision_artifact_id,
        fingerprint=vision.content_fingerprint,
        statement=vision.statement,
    )


__all__ = [
    "AcceptedVision",
    "VisionLineageError",
    "load_current_accepted_vision",
]
