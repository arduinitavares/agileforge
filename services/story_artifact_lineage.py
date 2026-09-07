"""Cycle-neutral Story artifact lineage row projection."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from services.planning_lineage import ArtifactLineageNode

if TYPE_CHECKING:
    from collections.abc import Iterable

    from models.workflow import StoryArtifact, StoryArtifactDecision
    from services.planning_lineage import Decision


def build_story_artifact_lineage_nodes(
    artifacts: Iterable[StoryArtifact],
    decisions: Iterable[StoryArtifactDecision],
) -> tuple[ArtifactLineageNode, ...]:
    """Map complete Story artifact and decision rows into lineage nodes."""
    artifacts_by_id: dict[int, StoryArtifact] = {}
    for artifact in artifacts:
        artifact_id = artifact.story_artifact_id
        if artifact_id is None:
            message = "Story artifact has no durable identity."
            raise ValueError(message)
        if artifact_id in artifacts_by_id:
            message = "Stored Story artifact identity is duplicated."
            raise ValueError(message)
        artifacts_by_id[artifact_id] = artifact

    decisions_by_artifact: dict[int, Decision] = {}
    for decision in decisions:
        artifact = artifacts_by_id.get(decision.story_artifact_id)
        if (
            artifact is None
            or artifact.content_fingerprint != decision.artifact_fingerprint
            or decision.story_artifact_id in decisions_by_artifact
            or decision.decision not in {"accepted", "feedback", "rejected"}
        ):
            message = "Stored Story decision lineage is invalid."
            raise ValueError(message)
        decisions_by_artifact[decision.story_artifact_id] = cast(
            "Decision", decision.decision
        )

    return tuple(
        ArtifactLineageNode(
            artifact_id=artifact_id,
            chain_key=(
                artifact.project_id,
                artifact.source_backlog_artifact_id,
                artifact.backlog_item_id,
            ),
            version_number=artifact.version_number,
            supersedes_artifact_id=artifact.supersedes_story_artifact_id,
            decision=decisions_by_artifact.get(artifact_id),
        )
        for artifact_id, artifact in artifacts_by_id.items()
    )
