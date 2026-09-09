"""Pure Sprint-plan stream selection shared outside graph definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.planning_lineage import (
    ArtifactLineageNode,
    PlanningLineageCode,
    PlanningLineageError,
    SprintStreamState,
    select_current_accepted_artifact,
    select_current_sprint_stream,
    validate_artifact_lineage,
)

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.facts import (
        PlanningArtifactFact,
        SprintStartFact,
        WorkflowFactSnapshot,
    )


def sprint_stream_nodes(
    artifacts: tuple[PlanningArtifactFact, ...],
) -> tuple[ArtifactLineageNode, ...]:
    """Project one exact Sprint-plan stream into validated lineage nodes."""
    stream_id = artifacts[0].sprint_plan_stream_id
    chain_key = (
        artifacts[0].spec_version_id,
        artifacts[0].spec_hash,
        stream_id,
    )
    return tuple(
        ArtifactLineageNode(
            artifact_id=item.artifact_id,
            chain_key=chain_key,
            version_number=item.version_number,
            supersedes_artifact_id=item.supersedes_artifact_id,
            decision=(
                "accepted"
                if item.status in {"accepted", "superseded"}
                else item.status
                if item.status in {"feedback", "rejected"}
                else None
            ),
        )
        for item in artifacts
    )


def sprint_stream_starts(
    snapshot: WorkflowFactSnapshot,
    artifacts: tuple[PlanningArtifactFact, ...],
    accepted: PlanningArtifactFact | None,
) -> tuple[SprintStartFact, ...]:
    """Return the exact start for a stream, or reject ambiguous history."""
    artifact_ids = {item.artifact_id for item in artifacts}
    activated_sprint_id = None if accepted is None else accepted.activated_sprint_id
    starts = tuple(
        item
        for item in snapshot.sprint_starts
        if item.sprint_plan_artifact_id in artifact_ids
        or (activated_sprint_id is not None and item.sprint_id == activated_sprint_id)
    )
    if starts and (
        len(starts) != 1
        or accepted is None
        or not plan_has_matching_sprint_start(snapshot, accepted)
    ):
        raise PlanningLineageError(PlanningLineageCode.SPRINT_STREAM_AMBIGUOUS)
    return starts


def sprint_stream_lifecycle(
    snapshot: WorkflowFactSnapshot,
    artifacts: tuple[PlanningArtifactFact, ...],
    accepted: PlanningArtifactFact | None,
) -> tuple[bool, bool, tuple[datetime, ...]]:
    """Return stream start/terminal state and stable lifecycle ordering markers."""
    starts = sprint_stream_starts(snapshot, artifacts, accepted)
    if accepted is None:
        return False, False, ()
    activated_sprint_id = accepted.activated_sprint_id
    matching_sprints = tuple(
        item for item in snapshot.sprints if item.sprint_id == activated_sprint_id
    )
    if len(matching_sprints) != 1:
        raise PlanningLineageError(PlanningLineageCode.SPRINT_STREAM_AMBIGUOUS)
    sprint = matching_sprints[0]
    if sprint.status in {"planned", "active"} and sprint.completed_at is not None:
        raise PlanningLineageError(PlanningLineageCode.SPRINT_STREAM_AMBIGUOUS)
    if sprint.status == "planned" and not starts:
        return False, False, ()
    if sprint.status not in {"active", "completed"} or not starts:
        raise PlanningLineageError(PlanningLineageCode.SPRINT_STREAM_AMBIGUOUS)
    if sprint.status == "completed" and (
        sprint.completed_at is None or sprint.completed_at < starts[0].started_at
    ):
        raise PlanningLineageError(PlanningLineageCode.SPRINT_STREAM_AMBIGUOUS)
    markers = (
        starts[0].started_at,
        *((sprint.completed_at,) if sprint.completed_at is not None else ()),
    )
    return True, sprint.status == "completed", markers


def current_sprint_stream_artifacts(
    snapshot: WorkflowFactSnapshot,
    artifacts: tuple[PlanningArtifactFact, ...],
    *,
    spec_identity: tuple[int, str],
) -> tuple[PlanningArtifactFact, ...]:
    """Select the sole current stream without depending on graph definitions."""
    streams: dict[str, tuple[PlanningArtifactFact, ...]] = {}
    for artifact in artifacts:
        stream_id = artifact.sprint_plan_stream_id
        if stream_id is None:
            raise PlanningLineageError(PlanningLineageCode.SPRINT_STREAM_AMBIGUOUS)
        streams[stream_id] = (*streams.get(stream_id, ()), artifact)

    lifecycle_markers: dict[str, datetime] = {}
    state_parts: list[tuple[str, bool, bool]] = []
    for stream_id, stream_artifacts in streams.items():
        nodes = sprint_stream_nodes(stream_artifacts)
        validate_artifact_lineage(nodes)
        accepted: PlanningArtifactFact | None = None
        try:
            accepted_id = select_current_accepted_artifact(
                nodes,
                chain_key=nodes[0].chain_key,
            ).artifact_id
            accepted = next(
                item for item in stream_artifacts if item.artifact_id == accepted_id
            )
        except PlanningLineageError as error:
            if error.code is not PlanningLineageCode.ACCEPTED_LEAF_MISSING:
                raise

        sprint_started, sprint_terminal, markers = sprint_stream_lifecycle(
            snapshot,
            stream_artifacts,
            accepted,
        )
        if markers:
            lifecycle_markers[stream_id] = max(markers)
        state_parts.append((stream_id, sprint_started, sprint_terminal))

    if len(set(lifecycle_markers.values())) != len(lifecycle_markers):
        raise PlanningLineageError(PlanningLineageCode.SPRINT_STREAM_AMBIGUOUS)
    lifecycle_order = {
        stream_id: order
        for order, (stream_id, _marker) in enumerate(
            sorted(lifecycle_markers.items(), key=lambda item: item[1]),
            start=1,
        )
    }
    open_order = len(lifecycle_order) + 1
    states = tuple(
        SprintStreamState(
            spec_identity=spec_identity,
            stream_id=stream_id,
            created_order=lifecycle_order.get(stream_id, open_order),
            sprint_started=sprint_started,
            sprint_terminal=sprint_terminal,
        )
        for stream_id, sprint_started, sprint_terminal in state_parts
    )
    selected_stream_id = select_current_sprint_stream(
        states,
        spec_identity=spec_identity,
    )
    if selected_stream_id is None:
        raise PlanningLineageError(PlanningLineageCode.SPRINT_STREAM_AMBIGUOUS)
    return streams[selected_stream_id]


def plan_has_matching_sprint_start(
    snapshot: WorkflowFactSnapshot,
    plan: PlanningArtifactFact,
) -> bool:
    """Require the exact immutable start to bind one accepted Sprint plan."""
    sprint_id = plan.activated_sprint_id
    if sprint_id is None:
        return False
    starts = tuple(
        item for item in snapshot.sprint_starts if item.sprint_id == sprint_id
    )
    return len(starts) == 1 and (
        starts[0].sprint_plan_artifact_id == plan.artifact_id
        and starts[0].plan_fingerprint == plan.artifact_fingerprint
        and starts[0].spec_version_id == plan.spec_version_id
        and starts[0].spec_hash == plan.spec_hash
        and starts[0].candidate_set_fingerprint == plan.candidate_set_fingerprint
        and starts[0].selected_story_ids == plan.selected_story_ids
        and starts[0].task_content_fingerprint == plan.task_content_fingerprint
    )


__all__ = [
    "current_sprint_stream_artifacts",
    "plan_has_matching_sprint_start",
    "sprint_stream_lifecycle",
    "sprint_stream_nodes",
    "sprint_stream_starts",
]
