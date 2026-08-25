"""Pure Roadmap, Story, dependency, readiness, and Sprint planning rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from services.planning_lineage import (
    ArtifactLineageNode,
    PlanningLineageCode,
    PlanningLineageError,
    SprintStreamState,
    select_current_accepted_artifact,
    select_current_sprint_stream,
    validate_artifact_lineage,
)
from services.story_rank import story_rank_is_valid
from workflow.contracts import (
    GRAPH_VERSION,
    Blocker,
    FactReference,
    InputField,
    RecommendationKind,
)
from workflow.definitions.backlog import current_backlog_lineage
from workflow.definitions.product_goal import lifecycle_is_quiescent
from workflow.fingerprints import canonical_hash
from workflow.graph import (
    AgenticExecutionSpec,
    ChildGraphSpec,
    NodeSpec,
    RuleCategory,
    RuleEvaluation,
    WorkflowGraph,
)
from workflow.planning_integrity import (
    active_dependency_review_edges,
    current_task_content_fingerprint,
    dependency_review_fingerprint,
)

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.facts import (
        BacklogItemFact,
        PhaseArtifactFact,
        PlanningArtifactFact,
        ProductGoalArtifactFact,
        SpecVersionFact,
        SprintStartFact,
        StoryDependencyFact,
        StoryFact,
        WorkflowFactSnapshot,
    )


@dataclass(frozen=True)
class _ArtifactState:
    latest: PlanningArtifactFact | None
    accepted: PlanningArtifactFact | None
    conflict: bool


@dataclass(frozen=True)
class _BacklogLineage:
    specification: SpecVersionFact | None
    goal: ProductGoalArtifactFact | None
    backlog: PhaseArtifactFact | None
    conflict: bool


def candidate_set_fingerprint(
    stories: tuple[StoryFact, ...],
    dependencies: tuple[StoryDependencyFact, ...],
) -> str:
    """Hash canonical current Story, dependency, and readiness facts."""
    return canonical_hash(
        {
            "selected_scope_fingerprint": (
                story_dependency_source_fingerprint(stories) if stories else None
            ),
            "stories": [
                item.model_dump(mode="json", exclude={"sprint_ids"})
                for item in sorted(stories, key=lambda story: story.story_id)
            ],
            "dependencies": [
                item.model_dump(mode="json")
                for item in sorted(
                    dependencies,
                    key=lambda edge: (
                        edge.dependent_story_id,
                        edge.prerequisite_story_id,
                        edge.dependency_id,
                    ),
                )
            ],
        }
    )


def story_dependency_source_fingerprint(stories: tuple[StoryFact, ...]) -> str:
    """Return the canonical fingerprint already derived from durable authorities."""
    selected = tuple(
        item
        for item in stories
        if item.structurally_eligible and item.sprint_selection_state == "selected"
    )
    fingerprints = {item.selected_scope_fingerprint for item in selected}
    if not selected or None in fingerprints or len(fingerprints) != 1:
        message = "Current selected Story scope fingerprint is missing or conflicting."
        raise ValueError(message)
    return next(item for item in fingerprints if item is not None)


def readiness_fingerprint(stories: tuple[StoryFact, ...]) -> str:
    """Hash exact mutable Story planning metadata before repair."""
    return canonical_hash(
        [
            {
                "story_id": item.story_id,
                "story_points": item.story_points,
                "rank": item.rank,
                "sprint_candidate": item.sprint_candidate,
                "readiness_blockers": item.readiness_blockers,
            }
            for item in sorted(stories, key=lambda story: story.story_id)
        ]
    )


def _blocked(reason: str, message: str) -> tuple[RuleEvaluation, ...]:
    return (
        RuleEvaluation(
            RuleCategory.BLOCKED,
            reason,
            blockers=(Blocker(code=reason, message=message),),
        ),
    )


def _accepted_backlog(snapshot: WorkflowFactSnapshot) -> _BacklogLineage:
    current = current_backlog_lineage(snapshot)
    return _BacklogLineage(
        specification=current.specification,
        goal=current.goal,
        backlog=current.backlog,
        conflict=current.conflict,
    )


def _artifact_matches_lineage(
    artifact: PlanningArtifactFact,
    lineage: _BacklogLineage,
    *,
    roadmap: PlanningArtifactFact | None = None,
) -> bool:
    if not _artifact_matches_backlog_lineage(artifact, lineage):
        return False
    backlog = lineage.backlog
    if backlog is None:
        return False
    if roadmap is None:
        return (
            artifact.source_artifact_id == backlog.artifact_id
            and artifact.source_fingerprint == backlog.artifact_fingerprint
        )
    return (
        artifact.source_artifact_id == roadmap.artifact_id
        and artifact.source_fingerprint == roadmap.artifact_fingerprint
        and artifact.roadmap_artifact_id == roadmap.artifact_id
        and artifact.roadmap_artifact_fingerprint == roadmap.artifact_fingerprint
    )


def _artifact_matches_backlog_lineage(
    artifact: PlanningArtifactFact,
    lineage: _BacklogLineage,
) -> bool:
    """Match the exact Backlog root while allowing a Story's Roadmap to change."""
    backlog = lineage.backlog
    if lineage.specification is None or backlog is None:
        return False
    return not (
        artifact.backlog_artifact_id != backlog.artifact_id
        or artifact.backlog_artifact_fingerprint != backlog.artifact_fingerprint
    )


def _roadmap_replacement_story_successor(
    latest: PlanningArtifactFact,
    lineage: _BacklogLineage,
    roadmap: PlanningArtifactFact,
    backlog_item_reference: FactReference,
    instance_key: str,
) -> RuleEvaluation | None:
    if latest.status != "accepted" or not _artifact_matches_backlog_lineage(
        latest, lineage
    ):
        return None
    if (
        latest.roadmap_artifact_id is None
        or latest.roadmap_artifact_id == roadmap.artifact_id
        or latest.source_artifact_id != latest.roadmap_artifact_id
        or latest.source_fingerprint != latest.roadmap_artifact_fingerprint
    ):
        return None
    return RuleEvaluation(
        RuleCategory.AVAILABLE,
        "STORY_GENERATION_REQUIRED",
        instance_key=instance_key,
        fact_references=(
            *_lineage_references(lineage),
            _artifact_reference(roadmap),
            backlog_item_reference,
            _artifact_reference(latest),
        ),
    )


def _lineage_references(lineage: _BacklogLineage) -> tuple[FactReference, ...]:
    if lineage.specification is None or lineage.goal is None or lineage.backlog is None:
        return ()
    return (
        FactReference(
            fact_type="backlog",
            fact_id=str(lineage.backlog.artifact_id),
            fingerprint=lineage.backlog.artifact_fingerprint,
        ),
        FactReference(
            fact_type="product_goal",
            fact_id=str(lineage.goal.product_goal_artifact_id),
            fingerprint=lineage.goal.content_fingerprint,
        ),
        FactReference(
            fact_type="specification",
            fact_id=str(lineage.specification.spec_version_id),
            fingerprint=lineage.specification.spec_hash,
        ),
    )


def _sprint_stream_nodes(
    artifacts: tuple[PlanningArtifactFact, ...],
) -> tuple[ArtifactLineageNode, ...]:
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


def _sprint_stream_starts(
    snapshot: WorkflowFactSnapshot,
    artifacts: tuple[PlanningArtifactFact, ...],
    accepted: PlanningArtifactFact | None,
) -> tuple[SprintStartFact, ...]:
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
        or not _plan_has_matching_sprint_start(snapshot, accepted)
    ):
        raise PlanningLineageError(PlanningLineageCode.SPRINT_STREAM_AMBIGUOUS)
    return starts


def _sprint_stream_lifecycle(
    snapshot: WorkflowFactSnapshot,
    artifacts: tuple[PlanningArtifactFact, ...],
    accepted: PlanningArtifactFact | None,
) -> tuple[bool, bool, tuple[datetime, ...]]:
    starts = _sprint_stream_starts(snapshot, artifacts, accepted)
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


def _current_sprint_stream_artifacts(
    snapshot: WorkflowFactSnapshot,
    artifacts: tuple[PlanningArtifactFact, ...],
    *,
    spec_identity: tuple[int, str],
) -> tuple[PlanningArtifactFact, ...]:
    streams: dict[str, tuple[PlanningArtifactFact, ...]] = {}
    for artifact in artifacts:
        stream_id = artifact.sprint_plan_stream_id
        if stream_id is None:
            raise PlanningLineageError(PlanningLineageCode.SPRINT_STREAM_AMBIGUOUS)
        streams[stream_id] = (*streams.get(stream_id, ()), artifact)

    lifecycle_markers: dict[str, datetime] = {}
    state_parts: list[tuple[str, bool, bool]] = []
    for stream_id, stream_artifacts in streams.items():
        nodes = _sprint_stream_nodes(stream_artifacts)
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

        sprint_started, sprint_terminal, markers = _sprint_stream_lifecycle(
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


def _artifact_state(  # noqa: PLR0911
    snapshot: WorkflowFactSnapshot,
    artifact_type: Literal["roadmap", "story", "sprint_plan"],
    *,
    backlog_item_id: str | None = None,
) -> _ArtifactState:
    lineage = _accepted_backlog(snapshot)
    backlog = lineage.backlog
    specification = lineage.specification
    artifacts = tuple(
        item
        for item in snapshot.planning_artifacts
        if item.artifact_type == artifact_type
        and (backlog_item_id is None or item.backlog_item_id == backlog_item_id)
        and (
            (
                artifact_type == "sprint_plan"
                and specification is not None
                and item.spec_version_id == specification.spec_version_id
                and item.spec_hash == specification.spec_hash
            )
            or (
                artifact_type != "sprint_plan"
                and backlog is not None
                and item.backlog_artifact_id == backlog.artifact_id
                and item.backlog_artifact_fingerprint == backlog.artifact_fingerprint
            )
        )
    )
    if not artifacts:
        return _ArtifactState(latest=None, accepted=None, conflict=False)
    if artifact_type == "sprint_plan":
        if specification is None:
            return _ArtifactState(latest=None, accepted=None, conflict=True)
        try:
            artifacts = _current_sprint_stream_artifacts(
                snapshot,
                artifacts,
                spec_identity=(
                    specification.spec_version_id,
                    specification.spec_hash,
                ),
            )
        except PlanningLineageError:
            return _ArtifactState(latest=None, accepted=None, conflict=True)
    chain_keys = {
        (
            item.backlog_artifact_id,
            item.backlog_artifact_fingerprint,
        )
        if artifact_type == "roadmap"
        else (
            item.backlog_artifact_id,
            item.backlog_item_id,
        )
        if artifact_type == "story"
        else (
            item.spec_version_id,
            item.spec_hash,
            item.sprint_plan_stream_id,
        )
        for item in artifacts
    }
    if len(chain_keys) != 1:
        return _ArtifactState(latest=None, accepted=None, conflict=True)
    chain_key = next(iter(chain_keys))
    nodes = tuple(
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
    try:
        validate_artifact_lineage(nodes)
    except PlanningLineageError:
        return _ArtifactState(latest=None, accepted=None, conflict=True)
    parent_ids = {
        item.supersedes_artifact_id
        for item in artifacts
        if item.supersedes_artifact_id is not None
    }
    latest_items = tuple(
        item for item in artifacts if item.artifact_id not in parent_ids
    )
    if len(latest_items) != 1:
        return _ArtifactState(latest=None, accepted=None, conflict=True)
    accepted: PlanningArtifactFact | None = None
    try:
        accepted_id = select_current_accepted_artifact(
            nodes,
            chain_key=chain_key,
        ).artifact_id
        accepted = next(item for item in artifacts if item.artifact_id == accepted_id)
    except PlanningLineageError as error:
        if error.code is not PlanningLineageCode.ACCEPTED_LEAF_MISSING:
            return _ArtifactState(latest=None, accepted=None, conflict=True)
    return _ArtifactState(
        latest=latest_items[0],
        accepted=accepted,
        conflict=False,
    )


def _artifact_reference(artifact: PlanningArtifactFact) -> FactReference:
    return FactReference(
        fact_type=artifact.artifact_type,
        fact_id=str(artifact.artifact_id),
        fingerprint=artifact.artifact_fingerprint,
    )


def _roadmap_current_evaluation(
    latest: PlanningArtifactFact | None,
    lineage: _BacklogLineage,
) -> tuple[RuleEvaluation, ...]:
    references = _lineage_references(lineage)
    if latest is None:
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "ROADMAP_GENERATION_REQUIRED",
                fact_references=references,
            ),
        )
    if not _artifact_matches_lineage(latest, lineage):
        return (RuleEvaluation(RuleCategory.INVALID, "ROADMAP_ARTIFACT_STALE"),)
    if latest.status == "pending_review":
        return (RuleEvaluation(RuleCategory.SATISFIED, "ROADMAP_REVIEW_PENDING"),)
    if latest.status in {"rejected", "feedback"}:
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "ROADMAP_REVISION_REQUIRED",
                fact_references=(*references, _artifact_reference(latest)),
                recommendation_kind=RecommendationKind.RECOVERY,
            ),
        )
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "ROADMAP_CORRECTION_AVAILABLE",
            fact_references=(*references, _artifact_reference(latest)),
            recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
        ),
    )


def _roadmap_generate_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    lineage = _accepted_backlog(snapshot)
    if lineage.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if lineage.specification is None:
        return _blocked(
            "ACCEPTED_CURRENT_SPECIFICATION_REQUIRED",
            "Roadmap generation requires the accepted current Specification.",
        )
    backlog = lineage.backlog
    if backlog is None:
        return _blocked(
            "ACCEPTED_CURRENT_BACKLOG_REQUIRED",
            "Roadmap generation requires the accepted current Backlog.",
        )
    state = _artifact_state(snapshot, "roadmap")
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    return _roadmap_current_evaluation(state.latest, lineage)


def _roadmap_review_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = _artifact_state(snapshot, "roadmap")
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if state.latest is None or state.latest.status != "pending_review":
        return (RuleEvaluation(RuleCategory.SATISFIED, "ROADMAP_REVIEW_NOT_PENDING"),)
    lineage = _accepted_backlog(snapshot)
    if lineage.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if not _artifact_matches_lineage(state.latest, lineage):
        return (RuleEvaluation(RuleCategory.INVALID, "ROADMAP_REVIEW_SOURCE_STALE"),)
    return (
        RuleEvaluation(
            RuleCategory.WAITING,
            "ROADMAP_REVIEW_REQUIRED",
            fact_references=(
                *_lineage_references(lineage),
                _artifact_reference(state.latest),
            ),
        ),
    )


def _accepted_current_roadmap(
    snapshot: WorkflowFactSnapshot,
) -> tuple[PlanningArtifactFact | None, bool]:
    lineage = _accepted_backlog(snapshot)
    state = _artifact_state(snapshot, "roadmap")
    if lineage.conflict or state.conflict:
        return None, True
    roadmap = state.accepted
    if (
        lineage.backlog is None
        or roadmap is None
        or not _artifact_matches_lineage(roadmap, lineage)
    ):
        return None, False
    return roadmap, False


def _current_backlog_items(
    snapshot: WorkflowFactSnapshot,
) -> tuple[BacklogItemFact, ...]:
    lineage = _accepted_backlog(snapshot)
    backlog = lineage.backlog
    if lineage.conflict or backlog is None:
        return ()
    return tuple(
        sorted(
            (
                item
                for item in snapshot.backlog_items
                if item.backlog_artifact_id == backlog.artifact_id
                and item.backlog_artifact_fingerprint == backlog.artifact_fingerprint
            ),
            key=lambda item: item.backlog_item_id,
        )
    )


def _story_generate_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    roadmap, conflict = _accepted_current_roadmap(snapshot)
    if conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if roadmap is None:
        return _blocked(
            "ACCEPTED_ROADMAP_REQUIRED",
            "Story generation requires the accepted current Roadmap.",
        )
    lineage = _accepted_backlog(snapshot)
    backlog_items = _current_backlog_items(snapshot)
    if not backlog_items:
        return (RuleEvaluation(RuleCategory.INVALID, "BACKLOG_ITEMS_MISSING"),)
    evaluations: list[RuleEvaluation] = []
    for backlog_item in backlog_items:
        backlog_item_id = backlog_item.backlog_item_id
        instance_key = f"backlog_item:{backlog_item_id}"
        state = _artifact_state(
            snapshot,
            "story",
            backlog_item_id=backlog_item_id,
        )
        backlog_item_reference = FactReference(
            fact_type="backlog_item",
            fact_id=backlog_item_id,
            fingerprint=backlog_item.item_fingerprint,
        )
        if state.conflict:
            evaluations.append(
                RuleEvaluation(
                    RuleCategory.INVALID,
                    "WORKFLOW_FACT_CONFLICT",
                    instance_key=instance_key,
                )
            )
            continue
        latest = state.latest
        if latest is not None:
            if not _artifact_matches_lineage(latest, lineage, roadmap=roadmap):
                replacement_successor = _roadmap_replacement_story_successor(
                    latest,
                    lineage,
                    roadmap,
                    backlog_item_reference,
                    instance_key,
                )
                evaluations.append(
                    replacement_successor
                    or RuleEvaluation(
                        RuleCategory.INVALID,
                        "STORY_ARTIFACT_STALE",
                        instance_key=instance_key,
                    )
                )
            elif latest.status in {"rejected", "feedback"}:
                evaluations.append(
                    RuleEvaluation(
                        RuleCategory.AVAILABLE,
                        "STORY_REVISION_REQUIRED",
                        instance_key=instance_key,
                        fact_references=(
                            *_lineage_references(lineage),
                            _artifact_reference(roadmap),
                            backlog_item_reference,
                            _artifact_reference(latest),
                        ),
                        recommendation_kind=RecommendationKind.RECOVERY,
                    )
                )
            elif latest.status == "accepted":
                evaluations.append(
                    RuleEvaluation(
                        RuleCategory.AVAILABLE,
                        "STORY_CORRECTION_AVAILABLE",
                        instance_key=instance_key,
                        fact_references=(
                            *_lineage_references(lineage),
                            _artifact_reference(roadmap),
                            backlog_item_reference,
                            _artifact_reference(latest),
                        ),
                        recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
                    )
                )
            continue
        evaluations.append(
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "STORY_GENERATION_REQUIRED",
                instance_key=instance_key,
                fact_references=(
                    *_lineage_references(lineage),
                    _artifact_reference(roadmap),
                    backlog_item_reference,
                ),
            )
        )
    return tuple(evaluations)


def _story_review_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    roadmap, roadmap_conflict = _accepted_current_roadmap(snapshot)
    lineage = _accepted_backlog(snapshot)
    evaluations: list[RuleEvaluation] = []
    backlog_item_ids = sorted(
        {
            item.backlog_item_id
            for item in snapshot.planning_artifacts
            if item.artifact_type == "story" and item.backlog_item_id is not None
        }
    )
    for backlog_item_id in backlog_item_ids:
        state = _artifact_state(snapshot, "story", backlog_item_id=backlog_item_id)
        latest = state.latest
        if state.conflict or roadmap_conflict or lineage.conflict:
            evaluations.append(
                RuleEvaluation(
                    RuleCategory.INVALID,
                    "WORKFLOW_FACT_CONFLICT",
                    instance_key=f"backlog_item:{backlog_item_id}",
                )
            )
        elif (
            latest is not None
            and latest.status == "pending_review"
            and (
                roadmap is None
                or not _artifact_matches_lineage(latest, lineage, roadmap=roadmap)
                or backlog_item_id
                not in {
                    item.backlog_item_id for item in _current_backlog_items(snapshot)
                }
            )
        ):
            evaluations.append(
                RuleEvaluation(
                    RuleCategory.INVALID,
                    "STORY_REVIEW_SOURCE_STALE",
                    instance_key=f"backlog_item:{backlog_item_id}",
                )
            )
        elif latest is not None and latest.status == "pending_review":
            if roadmap is None:
                evaluations.append(
                    RuleEvaluation(
                        RuleCategory.INVALID,
                        "STORY_REVIEW_SOURCE_STALE",
                        instance_key=f"backlog_item:{backlog_item_id}",
                    )
                )
                continue
            evaluations.append(
                RuleEvaluation(
                    RuleCategory.WAITING,
                    "STORY_REVIEW_REQUIRED",
                    instance_key=f"backlog_item:{backlog_item_id}",
                    fact_references=(
                        *_lineage_references(lineage),
                        _artifact_reference(roadmap),
                        _artifact_reference(latest),
                    ),
                )
            )
    return tuple(evaluations) or (
        RuleEvaluation(RuleCategory.SATISFIED, "STORY_REVIEW_NOT_PENDING"),
    )


def _candidate_stories(snapshot: WorkflowFactSnapshot) -> tuple[StoryFact, ...]:
    return tuple(
        sorted(
            (item for item in snapshot.stories if item.sprint_candidate),
            key=lambda item: item.story_id,
        )
    )


def _selected_scope_stories(snapshot: WorkflowFactSnapshot) -> tuple[StoryFact, ...]:
    return tuple(
        sorted(
            (
                item
                for item in snapshot.stories
                if item.structurally_eligible
                and item.sprint_selection_state == "selected"
            ),
            key=lambda item: item.story_id,
        )
    )


def _story_lineage_problem(
    snapshot: WorkflowFactSnapshot,
    stories: tuple[StoryFact, ...],
) -> RuleEvaluation | None:
    roadmap, conflict = _accepted_current_roadmap(snapshot)
    lineage = _accepted_backlog(snapshot)
    if conflict or lineage.conflict:
        return RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT")
    specification = lineage.specification
    backlog = lineage.backlog
    if roadmap is None or specification is None or backlog is None:
        return RuleEvaluation(RuleCategory.INVALID, "STORY_PLANNING_LINEAGE_STALE")
    backlog_item_ids = {
        item.backlog_item_id for item in _current_backlog_items(snapshot)
    }
    artifacts = {
        item.artifact_id: item
        for item in snapshot.planning_artifacts
        if item.artifact_type == "story"
    }
    for story in stories:
        artifact = (
            None
            if story.story_artifact_id is None
            else artifacts.get(story.story_artifact_id)
        )
        if not story.content_accepted:
            continue
        if (
            artifact is None
            or story.source_story_item_id not in artifact.story_item_ids
            or artifact.backlog_item_id not in backlog_item_ids
            or story.accepted_spec_version_id != specification.spec_version_id
            or story.accepted_spec_hash != specification.spec_hash
            or story.source_story_artifact_id != artifact.artifact_id
            or story.source_story_artifact_fingerprint != artifact.artifact_fingerprint
            or story.backlog_artifact_id != backlog.artifact_id
            or story.backlog_artifact_fingerprint != backlog.artifact_fingerprint
            or story.roadmap_artifact_id != roadmap.artifact_id
            or story.roadmap_artifact_fingerprint != roadmap.artifact_fingerprint
            or _artifact_state(
                snapshot,
                "story",
                backlog_item_id=artifact.backlog_item_id,
            ).accepted
            != artifact
            or not _artifact_matches_lineage(artifact, lineage, roadmap=roadmap)
        ):
            return RuleEvaluation(
                RuleCategory.INVALID,
                "STORY_PLANNING_LINEAGE_STALE",
            )
    return None


def _dependency_review_evaluation(
    snapshot: WorkflowFactSnapshot,
    stories: tuple[StoryFact, ...],
) -> RuleEvaluation:
    source = story_dependency_source_fingerprint(stories)
    selected_story_ids = tuple(item.story_id for item in stories)
    matching = tuple(
        item
        for item in snapshot.story_dependency_reviews
        if item.source_fingerprint == source
        and item.selected_story_ids == selected_story_ids
    )
    if len(matching) > 1:
        return RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT")
    if matching:
        review = matching[0]
        try:
            selected_ids = {story.story_id for story in stories}
            current_edges = active_dependency_review_edges(
                edge
                for edge in snapshot.story_dependencies
                if edge.dependent_story_id in selected_ids
            )
        except ValueError:
            return RuleEvaluation(
                RuleCategory.INVALID,
                "STORY_DEPENDENCY_REVIEW_STALE",
            )
        if (
            review.reviewed_edges != current_edges
            or review.dependency_fingerprint
            != dependency_review_fingerprint(current_edges)
        ):
            return RuleEvaluation(
                RuleCategory.INVALID,
                "STORY_DEPENDENCY_REVIEW_STALE",
            )
        incomplete = tuple(
            blocker
            for story in stories
            for blocker in story.readiness_blockers
            if blocker.startswith("PREREQUISITE_STORY_")
            and blocker.endswith("_INCOMPLETE")
        )
        if incomplete:
            return RuleEvaluation(
                RuleCategory.BLOCKED,
                "STORY_DEPENDENCY_EXTERNAL_INCOMPLETE",
                blockers=tuple(
                    Blocker(
                        code=code,
                        message=(
                            "Selected Story scope has an incomplete external "
                            "prerequisite."
                        ),
                    )
                    for code in incomplete
                ),
            )
        return RuleEvaluation(
            RuleCategory.SATISFIED,
            "STORY_DEPENDENCIES_REVIEWED",
        )
    lineage = _accepted_backlog(snapshot)
    roadmap, _conflict = _accepted_current_roadmap(snapshot)
    references = [*_lineage_references(lineage)]
    if roadmap is not None:
        references.append(_artifact_reference(roadmap))
    references.append(
        FactReference(
            fact_type="story_dependency_source",
            fact_id=str(snapshot.project.project_id),
            fingerprint=source,
        )
    )
    return RuleEvaluation(
        RuleCategory.AVAILABLE,
        "STORY_DEPENDENCY_REVIEW_REQUIRED",
        fact_references=tuple(references),
    )


def _dependency_problem(
    stories: tuple[StoryFact, ...],
    dependencies: tuple[StoryDependencyFact, ...],
) -> tuple[RuleCategory, str] | None:
    story_ids = {item.story_id for item in stories}
    relevant = tuple(
        item for item in dependencies if item.status in {"active", "proposed"}
    )
    if any(
        item.dependent_story_id not in story_ids
        or item.prerequisite_story_id not in story_ids
        or item.dependent_story_id == item.prerequisite_story_id
        for item in relevant
    ):
        return RuleCategory.INVALID, "STORY_DEPENDENCY_INVALID"
    if any(item.status == "proposed" for item in relevant):
        return RuleCategory.BLOCKED, "STORY_DEPENDENCIES_UNREVIEWED"
    edges: dict[int, set[int]] = {}
    for item in relevant:
        edges.setdefault(item.dependent_story_id, set()).add(item.prerequisite_story_id)
    active: set[int] = set()
    visited: set[int] = set()

    def visit(story_id: int) -> bool:
        if story_id in active:
            return True
        if story_id in visited:
            return False
        visited.add(story_id)
        active.add(story_id)
        found = any(visit(parent) for parent in sorted(edges.get(story_id, set())))
        active.remove(story_id)
        return found

    if any(visit(story_id) for story_id in sorted(story_ids)):
        return RuleCategory.INVALID, "STORY_DEPENDENCY_CYCLE"
    return None


def _story_dependencies_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    stories = _selected_scope_stories(snapshot)
    if not stories:
        return _blocked(
            "STORY_DEPENDENCY_CANDIDATES_MISSING",
            "Dependency review requires at least one candidate Story.",
        )
    if any(not story.content_accepted for story in stories):
        return _blocked(
            "STORY_CONTENT_NOT_ACCEPTED",
            "Dependency review requires accepted current Story content.",
        )
    lineage_problem = _story_lineage_problem(snapshot, stories)
    if lineage_problem is not None:
        return (lineage_problem,)
    return (_dependency_review_evaluation(snapshot, stories),)


def _story_readiness_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    stories = _candidate_stories(snapshot)
    if any(not story.content_accepted for story in stories):
        return _blocked(
            "STORY_CONTENT_NOT_ACCEPTED",
            "Readiness repair requires accepted current Story content.",
        )
    if stories:
        lineage_problem = _story_lineage_problem(snapshot, stories)
        if lineage_problem is not None:
            return (lineage_problem,)
    missing = tuple(
        item
        for item in stories
        if item.story_points is None or not story_rank_is_valid(item.rank)
    )
    if not missing:
        return (RuleEvaluation(RuleCategory.SATISFIED, "STORY_READINESS_COMPLETE"),)
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "STORY_READINESS_REPAIR_REQUIRED",
            fact_references=(
                *_lineage_references(_accepted_backlog(snapshot)),
                *(
                    (_artifact_reference(roadmap),)
                    if (roadmap := _accepted_current_roadmap(snapshot)[0]) is not None
                    else ()
                ),
                FactReference(
                    fact_type="story_readiness",
                    fact_id=str(snapshot.project.project_id),
                    fingerprint=readiness_fingerprint(snapshot.stories),
                ),
            ),
        ),
    )


def _sprint_candidate_problem(
    snapshot: WorkflowFactSnapshot,
    stories: tuple[StoryFact, ...],
) -> RuleEvaluation | None:
    if any(not item.content_accepted for item in stories):
        return RuleEvaluation(
            RuleCategory.BLOCKED,
            "STORY_CONTENT_NOT_ACCEPTED",
            blockers=(
                Blocker(
                    code="STORY_CONTENT_NOT_ACCEPTED",
                    message="Every candidate Story requires accepted content.",
                ),
            ),
        )
    lineage_problem = _story_lineage_problem(snapshot, stories)
    if lineage_problem is not None:
        return lineage_problem
    blockers: list[Blocker] = []
    if any(item.story_points is None for item in stories):
        blockers.append(
            Blocker(code="STORY_POINTS_MISSING", message="Story points are required.")
        )
    if any(item.rank is None for item in stories):
        blockers.append(
            Blocker(code="STORY_RANK_MISSING", message="Story rank is required.")
        )
    if any(
        item.rank is not None and not story_rank_is_valid(item.rank) for item in stories
    ):
        blockers.append(
            Blocker(
                code="STORY_RANK_INVALID",
                message=(
                    "Story rank must be a canonical positive base-10 integer string."
                ),
            )
        )
    if blockers:
        return RuleEvaluation(
            RuleCategory.BLOCKED,
            "STORY_READINESS_INCOMPLETE",
            blockers=tuple(blockers),
        )
    return None


def _sprint_join(  # noqa: PLR0911
    snapshot: WorkflowFactSnapshot,
) -> tuple[StoryFact, ...] | RuleEvaluation:
    selected = _selected_scope_stories(snapshot)
    if not selected:
        return RuleEvaluation(
            RuleCategory.BLOCKED,
            "SPRINT_CANDIDATES_MISSING",
            blockers=(
                Blocker(
                    code="SPRINT_CANDIDATES_MISSING",
                    message="Sprint planning requires at least one candidate Story.",
                ),
            ),
        )
    dependency_review = _dependency_review_evaluation(snapshot, selected)
    if dependency_review.category is RuleCategory.INVALID:
        return dependency_review
    if dependency_review.category is not RuleCategory.SATISFIED:
        if dependency_review.category is RuleCategory.BLOCKED:
            return dependency_review
        return RuleEvaluation(
            RuleCategory.BLOCKED,
            "STORY_DEPENDENCIES_UNREVIEWED",
            blockers=(
                Blocker(
                    code="STORY_DEPENDENCIES_UNREVIEWED",
                    message="Current Story dependency semantics require review.",
                ),
            ),
        )
    stories = _candidate_stories(snapshot)
    if tuple(story.story_id for story in stories) != tuple(
        story.story_id for story in selected
    ):
        return RuleEvaluation(
            RuleCategory.INVALID,
            "STORY_CANDIDACY_PROJECTION_INVALID",
        )
    candidate_problem = _sprint_candidate_problem(snapshot, stories)
    if candidate_problem is not None:
        return candidate_problem
    return stories


def _sprint_plan_freshness_reason(
    snapshot: WorkflowFactSnapshot,
    plan: PlanningArtifactFact,
    stories: tuple[StoryFact, ...],
    *,
    review: bool,
) -> str | None:
    candidate_ids = {item.story_id for item in stories}
    if not plan.selected_story_ids or any(
        item not in candidate_ids for item in plan.selected_story_ids
    ):
        return (
            "SPRINT_PLAN_REVIEW_SOURCE_STALE"
            if review
            else "SPRINT_PLAN_SELECTED_STORY_INVALID"
        )
    current_candidates = candidate_set_fingerprint(
        stories,
        snapshot.story_dependencies,
    )
    if plan.candidate_set_fingerprint != current_candidates:
        return "SPRINT_PLAN_REVIEW_SOURCE_STALE" if review else "SPRINT_PLAN_STALE"
    if plan.activated_sprint_id is not None and (
        plan.task_content_fingerprint is None
        or plan.task_content_fingerprint
        != current_task_content_fingerprint(
            snapshot.tasks,
            sprint_id=plan.activated_sprint_id,
            story_ids=plan.selected_story_ids,
        )
    ):
        return (
            "SPRINT_PLAN_REVIEW_TASK_CONTENT_STALE"
            if review
            else "SPRINT_PLAN_TASK_CONTENT_STALE"
        )
    return None


def _sprint_plan_references(
    snapshot: WorkflowFactSnapshot,
    stories: tuple[StoryFact, ...],
    *,
    plan: PlanningArtifactFact | None = None,
) -> tuple[FactReference, ...]:
    lineage = _accepted_backlog(snapshot)
    roadmap, _conflict = _accepted_current_roadmap(snapshot)
    return (
        *((_artifact_reference(plan),) if plan is not None else ()),
        *_lineage_references(lineage),
        *((_artifact_reference(roadmap),) if roadmap is not None else ()),
        FactReference(
            fact_type="candidate_set",
            fact_id=str(snapshot.project.project_id),
            fingerprint=candidate_set_fingerprint(
                stories,
                snapshot.story_dependencies,
            ),
        ),
    )


def _plan_has_matching_sprint_start(
    snapshot: WorkflowFactSnapshot,
    plan: PlanningArtifactFact,
) -> bool:
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


def _sprint_plan_cycle_head(
    snapshot: WorkflowFactSnapshot,
    state: _ArtifactState,
) -> PlanningArtifactFact | None:
    """Freeze one started stream on its semantic accepted plan."""
    accepted = state.accepted
    if accepted is not None and _plan_has_matching_sprint_start(snapshot, accepted):
        return accepted
    return state.latest


def _existing_sprint_plan_evaluation(
    snapshot: WorkflowFactSnapshot,
    latest: PlanningArtifactFact,
    stories: tuple[StoryFact, ...],
) -> tuple[RuleEvaluation, ...]:
    completed = any(
        sprint.sprint_id == latest.activated_sprint_id and sprint.status == "completed"
        for sprint in snapshot.sprints
    )
    if (
        latest.status == "accepted"
        and completed
        and not lifecycle_is_quiescent(snapshot)
    ):
        return (
            RuleEvaluation(
                RuleCategory.SATISFIED,
                "NEXT_SPRINT_AWAITS_QUIESCENT_LIFECYCLE",
            ),
        )
    if latest.status == "accepted" and (
        completed or _plan_has_matching_sprint_start(snapshot, latest)
    ):
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "NEXT_SPRINT_PLANNING_REQUIRED",
                fact_references=_sprint_plan_references(
                    snapshot,
                    stories,
                    plan=latest,
                ),
            ),
        )
    stale_reason = _sprint_plan_freshness_reason(
        snapshot,
        latest,
        stories,
        review=False,
    )
    if stale_reason is not None:
        return (RuleEvaluation(RuleCategory.INVALID, stale_reason),)
    if latest.status == "pending_review":
        return (RuleEvaluation(RuleCategory.SATISFIED, "SPRINT_PLAN_REVIEW_PENDING"),)
    references = _sprint_plan_references(snapshot, stories, plan=latest)
    if latest.status in {"rejected", "feedback"}:
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "SPRINT_PLAN_REVISION_REQUIRED",
                fact_references=references,
                recommendation_kind=RecommendationKind.RECOVERY,
            ),
        )
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "SPRINT_PLAN_CORRECTION_AVAILABLE",
            fact_references=references,
            recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
        ),
    )


def _sprint_plan_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = _artifact_state(snapshot, "sprint_plan")
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    joined = _sprint_join(snapshot)
    if isinstance(joined, RuleEvaluation):
        return (joined,)
    cycle_head = _sprint_plan_cycle_head(snapshot, state)
    if cycle_head is not None:
        return _existing_sprint_plan_evaluation(snapshot, cycle_head, joined)
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "SPRINT_PLANNING_REQUIRED",
            fact_references=_sprint_plan_references(snapshot, joined),
        ),
    )


def _sprint_review_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = _artifact_state(snapshot, "sprint_plan")
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    cycle_head = _sprint_plan_cycle_head(snapshot, state)
    if cycle_head is None or cycle_head.status != "pending_review":
        return (RuleEvaluation(RuleCategory.SATISFIED, "SPRINT_REVIEW_NOT_PENDING"),)
    joined = _sprint_join(snapshot)
    if isinstance(joined, RuleEvaluation):
        return (
            RuleEvaluation(
                RuleCategory.INVALID,
                "SPRINT_PLAN_REVIEW_SOURCE_STALE",
            ),
        )
    return _sprint_review_evaluation(snapshot, cycle_head, joined)


def _sprint_review_evaluation(
    snapshot: WorkflowFactSnapshot,
    plan: PlanningArtifactFact,
    stories: tuple[StoryFact, ...],
) -> tuple[RuleEvaluation, ...]:
    stale_reason = _sprint_plan_freshness_reason(
        snapshot,
        plan,
        stories,
        review=True,
    )
    if stale_reason is not None:
        return (RuleEvaluation(RuleCategory.INVALID, stale_reason),)
    return (
        RuleEvaluation(
            RuleCategory.WAITING,
            "SPRINT_PLAN_REVIEW_REQUIRED",
            fact_references=_sprint_plan_references(snapshot, stories, plan=plan),
        ),
    )


def _sprint_start_evaluation(
    snapshot: WorkflowFactSnapshot,
    plan: PlanningArtifactFact,
    stories: tuple[StoryFact, ...],
) -> tuple[RuleEvaluation, ...]:
    stale_reason = _sprint_plan_freshness_reason(
        snapshot,
        plan,
        stories,
        review=False,
    )
    if stale_reason is not None:
        return (RuleEvaluation(RuleCategory.INVALID, stale_reason),)
    sprint_id = plan.activated_sprint_id
    if sprint_id is None:
        return (
            RuleEvaluation(
                RuleCategory.INVALID,
                "SPRINT_PLAN_TASK_CONTENT_STALE",
            ),
        )
    if any(
        item.status == "active" and item.sprint_id != sprint_id
        for item in snapshot.sprints
    ):
        return _blocked(
            "ACTIVE_SPRINT_EXISTS",
            "Another Sprint is already active for this Project. Close it before "
            "starting this Sprint.",
        )
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "SPRINT_READY_TO_START",
            fact_references=(
                *_sprint_plan_references(snapshot, stories, plan=plan),
                FactReference(
                    fact_type="sprint_plan_tasks",
                    fact_id=str(sprint_id),
                    fingerprint=current_task_content_fingerprint(
                        snapshot.tasks,
                        sprint_id=sprint_id,
                        story_ids=plan.selected_story_ids,
                    ),
                ),
            ),
        ),
    )


def _sprint_start_artifact_state(snapshot: WorkflowFactSnapshot) -> _ArtifactState:
    """Select the current start target and validate older lifecycle roles."""
    current_state = _artifact_state(snapshot, "sprint_plan")
    current_target = current_state.accepted
    older_states = tuple(
        (item, _sprint_start_lifecycle_state(snapshot, item))
        for item in snapshot.planning_artifacts
        if item.artifact_type == "sprint_plan"
        and item.status == "accepted"
        and (current_target is None or item.artifact_id != current_target.artifact_id)
    )
    if current_state.conflict or any(
        state == "conflict" for _item, state in older_states
    ):
        return _ArtifactState(latest=None, accepted=None, conflict=True)
    older_targets = tuple(item for item, state in older_states if state != "terminal")
    if current_target is not None:
        if (
            any(state == "unstarted" for _item, state in older_states)
            or sum(state == "started" for _item, state in older_states) > 1
        ):
            return _ArtifactState(latest=None, accepted=None, conflict=True)
        return current_state
    if len(older_targets) > 1:
        return _ArtifactState(latest=None, accepted=None, conflict=True)
    if len(older_targets) == 1:
        plan = older_targets[0]
        return _ArtifactState(latest=plan, accepted=plan, conflict=False)
    return current_state


def _sprint_start_lifecycle_state(
    snapshot: WorkflowFactSnapshot,
    plan: PlanningArtifactFact,
) -> Literal["unstarted", "started", "terminal", "conflict"]:
    """Classify the exact activated Sprint and SprintStart relationship."""
    try:
        started, terminal, _markers = _sprint_stream_lifecycle(
            snapshot,
            (plan,),
            plan,
        )
    except PlanningLineageError:
        return "conflict"
    if terminal:
        return "terminal"
    if started:
        return "started"
    return "unstarted"


def _sprint_start_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = _sprint_start_artifact_state(snapshot)
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    plan = state.accepted
    if plan is None:
        return _blocked(
            "ACCEPTED_SPRINT_PLAN_REQUIRED",
            "Sprint start requires an accepted exact Sprint plan.",
        )
    lifecycle_state = _sprint_start_lifecycle_state(snapshot, plan)
    if lifecycle_state != "unstarted":
        return (
            RuleEvaluation(
                (
                    RuleCategory.INVALID
                    if lifecycle_state == "conflict"
                    else RuleCategory.SATISFIED
                ),
                (
                    "WORKFLOW_FACT_CONFLICT"
                    if lifecycle_state == "conflict"
                    else "SPRINT_ALREADY_STARTED"
                ),
            ),
        )
    specification = _accepted_backlog(snapshot).specification
    if (
        specification is None
        or plan.spec_version_id != specification.spec_version_id
        or plan.spec_hash != specification.spec_hash
    ):
        return (RuleEvaluation(RuleCategory.INVALID, "STALE_SPECIFICATION"),)
    joined = _sprint_join(snapshot)
    if isinstance(joined, RuleEvaluation):
        return (joined,)
    return _sprint_start_evaluation(snapshot, plan, joined)


PLANNING_NODES: tuple[NodeSpec, ...] = (
    NodeSpec(
        node_id="planning.roadmap.generate",
        child_graph_id="planning",
        request_kind="record_roadmap_draft",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="backlog_artifact_id", value_type="integer"),
            InputField(name="backlog_artifact_fingerprint", value_type="string"),
            InputField(name="canonical_content", value_type="object"),
            InputField(name="content_fingerprint", value_type="string"),
        ),
        evaluate_rule=_roadmap_generate_rule,
        agentic_execution=AgenticExecutionSpec(
            active_reason="ROADMAP_GENERATION_ACTIVE",
            failure_reason="ROADMAP_GENERATION_FAILED",
            recovery_reason="ROADMAP_GENERATION_RECOVERY_REQUIRED",
        ),
    ),
    NodeSpec(
        node_id="planning.roadmap.review",
        child_graph_id="planning",
        request_kind="decide_roadmap",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="roadmap_artifact_id", value_type="integer"),
            InputField(name="artifact_fingerprint", value_type="string"),
            InputField(name="decision", value_type="string"),
            InputField(name="rationale", value_type="string"),
        ),
        evaluate_rule=_roadmap_review_rule,
    ),
    NodeSpec(
        node_id="planning.story.generate",
        child_graph_id="planning",
        request_kind="record_story_draft",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="backlog_item_id", value_type="string"),
            InputField(name="source_backlog_artifact_id", value_type="integer"),
            InputField(
                name="source_backlog_artifact_fingerprint",
                value_type="string",
            ),
            InputField(name="roadmap_artifact_id", value_type="integer"),
            InputField(name="canonical_content", value_type="object"),
            InputField(name="content_fingerprint", value_type="string"),
        ),
        evaluate_rule=_story_generate_rule,
        agentic_execution=AgenticExecutionSpec(
            active_reason="STORY_GENERATION_ACTIVE",
            failure_reason="STORY_GENERATION_FAILED",
            recovery_reason="STORY_GENERATION_RECOVERY_REQUIRED",
        ),
    ),
    NodeSpec(
        node_id="planning.story.review",
        child_graph_id="planning",
        request_kind="decide_story",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="story_artifact_id", value_type="integer"),
            InputField(name="artifact_fingerprint", value_type="string"),
            InputField(name="decision", value_type="string"),
            InputField(name="rationale", value_type="string"),
        ),
        evaluate_rule=_story_review_rule,
    ),
    NodeSpec(
        node_id="planning.story_dependencies",
        child_graph_id="planning",
        request_kind="apply_story_dependencies",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="selected_story_ids", value_type="array"),
            InputField(name="reviewed_edges", value_type="array"),
            InputField(name="source_fingerprint", value_type="string"),
        ),
        evaluate_rule=_story_dependencies_rule,
    ),
    NodeSpec(
        node_id="planning.story_readiness",
        child_graph_id="planning",
        request_kind="repair_story_readiness",
        recommendation_kind=RecommendationKind.RECOVERY,
        required_inputs=(
            InputField(name="story_ids", value_type="array"),
            InputField(name="repairs", value_type="array"),
            InputField(
                name="expected_readiness_fingerprint",
                value_type="string",
            ),
        ),
        evaluate_rule=_story_readiness_rule,
    ),
    NodeSpec(
        node_id="planning.sprint.plan",
        child_graph_id="planning",
        request_kind="record_sprint_plan",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="team_name", value_type="string"),
            InputField(name="spec_version_id", value_type="integer"),
            InputField(name="spec_hash", value_type="string"),
            InputField(name="planner_output", value_type="object"),
        ),
        evaluate_rule=_sprint_plan_rule,
        agentic_execution=AgenticExecutionSpec(
            active_reason="SPRINT_PLANNING_ACTIVE",
            failure_reason="SPRINT_PLANNING_FAILED",
            recovery_reason="SPRINT_PLANNING_RECOVERY_REQUIRED",
        ),
    ),
    NodeSpec(
        node_id="planning.sprint.review",
        child_graph_id="planning",
        request_kind="decide_sprint_plan",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="sprint_plan_artifact_id", value_type="integer"),
            InputField(name="plan_fingerprint", value_type="string"),
            InputField(name="decision", value_type="string"),
            InputField(name="rationale", value_type="string"),
        ),
        evaluate_rule=_sprint_review_rule,
    ),
    NodeSpec(
        node_id="planning.sprint.start",
        child_graph_id="planning",
        request_kind="start_sprint",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(),
        evaluate_rule=_sprint_start_rule,
    ),
)


def planning_graph() -> WorkflowGraph:
    """Return the standalone planning graph used by focused tests."""
    return WorkflowGraph(
        graph_version=GRAPH_VERSION,
        root=ChildGraphSpec(child_graph_id="planning", nodes=PLANNING_NODES),
    )


__all__ = [
    "PLANNING_NODES",
    "candidate_set_fingerprint",
    "planning_graph",
    "readiness_fingerprint",
    "story_dependency_source_fingerprint",
]
