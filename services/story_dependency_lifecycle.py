# services/story_dependency_lifecycle.py
"""Append-only dependency freshness for exact superseded Story identities."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import ConfigDict, Field, ValidationError, model_validator
from sqlmodel import Session, col, select

from models.core import Sprint, SprintStory, UserStory, UserStoryDependency
from models.enums import SprintStatus, WorkflowEventType
from models.events import WorkflowEvent
from models.workflow import StoryArtifact, StoryArtifactDecision
from services.planning_lineage import (
    ArtifactLineageNode,
    PlanningLineageError,
    validate_artifact_lineage,
)
from services.story_artifact_lineage import build_story_artifact_lineage_nodes
from workflow.contracts import FrozenModel
from workflow.facts import StoryDependencyFact, StoryFact
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.planning_integrity import current_dependency_closure, is_terminal_external

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.facts import StoryDependencyReviewEdgeFact

type _Identity = Annotated[int, Field(gt=0, strict=True)]
type _Fingerprint = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$", strict=True)]


class StoryDependencyLifecycleIntegrityError(RuntimeError):
    """Stored lifecycle evidence contradicts exact Project/Story lineage."""


class StoryDependencyInvalidationAnchor(FrozenModel):
    """Stable freshness identity with optional accepted-successor diagnostics."""

    dependent_story_id: _Identity
    superseded_story_id: _Identity
    superseded_story_artifact_id: _Identity
    superseded_story_artifact_fingerprint: _Fingerprint
    replacement_story_artifact_id: _Identity | None = None
    replacement_story_artifact_fingerprint: _Fingerprint | None = None

    @model_validator(mode="after")
    def require_paired_successor(self) -> Self:
        """Keep absent proof distinct from malformed partial proof."""
        if (self.replacement_story_artifact_id is None) != (
            self.replacement_story_artifact_fingerprint is None
        ):
            message = "Replacement Story artifact diagnostics must be paired."
            raise ValueError(message)
        return self


def _identity(anchor: StoryDependencyInvalidationAnchor) -> tuple[int, int, int, str]:
    return (
        anchor.dependent_story_id,
        anchor.superseded_story_id,
        anchor.superseded_story_artifact_id,
        anchor.superseded_story_artifact_fingerprint,
    )


def _canonical_anchors(
    anchors: tuple[StoryDependencyInvalidationAnchor, ...],
) -> tuple[StoryDependencyInvalidationAnchor, ...]:
    normalized: dict[tuple[int, int, int, str], StoryDependencyInvalidationAnchor] = {}
    for anchor in anchors:
        identity = _identity(anchor)
        previous = normalized.get(identity)
        if previous is not None and previous.replacement_story_artifact_id is not None:
            if anchor.replacement_story_artifact_id is not None and (
                previous.replacement_story_artifact_id
                != anchor.replacement_story_artifact_id
                or previous.replacement_story_artifact_fingerprint
                != anchor.replacement_story_artifact_fingerprint
            ):
                message = (
                    "One dependency invalidation has contradictory successor proof."
                )
                raise StoryDependencyLifecycleIntegrityError(message)
            continue
        normalized[identity] = anchor
    return tuple(normalized[key] for key in sorted(normalized))


class StoryDependencyInvalidationPath(FrozenModel):
    """One historical simple current-authority path to an obsolete endpoint."""

    dependent_story_id: _Identity
    superseded_story_id: _Identity
    dependency_rows: tuple[StoryDependencyFact, ...]


class StoryDependencyInvalidationMetadata(FrozenModel):
    """Canonical append-only causal evidence persisted in WorkflowEvent."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["agileforge.story-dependency-invalidation.v1"] = (
        "agileforge.story-dependency-invalidation.v1"
    )
    action: Literal["story_dependency_invalidated"] = "story_dependency_invalidated"
    mode: Literal["accepted_replacement", "review_adoption"]
    anchors: tuple[StoryDependencyInvalidationAnchor, ...]
    causal_paths: tuple[StoryDependencyInvalidationPath, ...]

    @model_validator(mode="after")
    def require_canonical_evidence(self) -> Self:
        """Bind one deterministic path to each unique, ordered identity."""
        anchor_pairs = tuple(
            (item.dependent_story_id, item.superseded_story_id) for item in self.anchors
        )
        path_pairs = tuple(
            (item.dependent_story_id, item.superseded_story_id)
            for item in self.causal_paths
        )
        if (
            not self.anchors
            or self.anchors != _canonical_anchors(self.anchors)
            or len(anchor_pairs) != len(set(anchor_pairs))
            or anchor_pairs != path_pairs
            or any(not path.dependency_rows for path in self.causal_paths)
        ):
            message = "Dependency invalidation anchors and paths are not canonical."
            raise ValueError(message)
        if self.mode == "accepted_replacement" and (
            any(anchor.replacement_story_artifact_id is None for anchor in self.anchors)
            or len({anchor.replacement_story_artifact_id for anchor in self.anchors})
            != 1
        ):
            message = "Accepted replacement invalidation requires one exact successor."
            raise ValueError(message)
        return self


@dataclass(frozen=True)
class _CurrentGraph:
    stories: tuple[StoryFact, ...]
    dependencies: tuple[StoryDependencyFact, ...]
    rows: dict[int, UserStory]
    completed_story_ids: frozenset[int]


@dataclass(frozen=True)
class _LineageProof:
    artifacts: dict[int, StoryArtifact]
    nodes: dict[int, ArtifactLineageNode]


def _current_graph(session: Session, *, project_id: int) -> _CurrentGraph:
    rows = session.exec(
        select(UserStory).where(col(UserStory.project_id) == project_id)
    ).all()
    dependencies = session.exec(
        select(UserStoryDependency).where(
            col(UserStoryDependency.project_id) == project_id
        )
    ).all()
    by_id: dict[int, UserStory] = {}
    facts: list[StoryFact] = []
    for row in rows:
        if row.story_id is None or row.story_id <= 0:
            message = "Dependency lifecycle Story has no valid durable identity."
            raise StoryDependencyLifecycleIntegrityError(message)
        by_id[row.story_id] = row
        # Only immutable identity/completion fields feed current authority. The
        # eligibility/selection placeholders are never returned as product facts.
        facts.append(
            StoryFact(
                story_id=row.story_id,
                is_superseded=row.is_superseded,
                source_story_artifact_id=row.source_story_artifact_id,
                source_story_artifact_fingerprint=row.source_story_artifact_fingerprint,
                source_story_item_id=row.source_story_item_id,
                source_story_item_fingerprint=row.source_story_item_fingerprint,
                accepted_spec_version_id=row.accepted_spec_version_id,
                accepted_spec_hash=row.accepted_spec_hash,
                spec_item_ids=(),
                status=row.status,
                structurally_eligible=False,
                structural_eligibility_status="stale",
                sprint_selection_state="unselected",
                sprint_selection_state_fingerprint="",
                sprint_candidate=False,
                readiness_blockers=(),
            )
        )
    edge_facts_list: list[StoryDependencyFact] = []
    try:
        # Project only semantic fields; database timestamps are not freshness inputs.
        for row in dependencies:
            dependency_id = row.dependency_id
            if dependency_id is None:
                message = "Dependency lifecycle rows contain malformed identities."
                raise StoryDependencyLifecycleIntegrityError(message)
            edge_facts_list.append(
                StoryDependencyFact.model_validate(
                    {
                        "dependency_id": dependency_id,
                        "dependent_story_id": row.dependent_story_id,
                        "prerequisite_story_id": row.prerequisite_story_id,
                        "status": row.status,
                        "source": row.source,
                        "confidence": row.confidence,
                        "reason": row.reason,
                    }
                )
            )
    except ValidationError as error:
        message = "Dependency lifecycle rows contain malformed identities."
        raise StoryDependencyLifecycleIntegrityError(message) from error
    edge_facts = tuple(edge_facts_list)
    if any(
        edge.dependency_id <= 0
        or edge.dependent_story_id not in by_id
        or edge.prerequisite_story_id not in by_id
        for edge in edge_facts
    ):
        message = "Dependency lifecycle edge references a foreign or missing Story."
        raise StoryDependencyLifecycleIntegrityError(message)
    completed_ids = frozenset(
        session.exec(
            select(SprintStory.story_id)
            .join(Sprint, col(Sprint.sprint_id) == col(SprintStory.sprint_id))
            .where(
                col(Sprint.project_id) == project_id,
                col(Sprint.status) == SprintStatus.COMPLETED,
            )
        ).all()
    )
    return _CurrentGraph(tuple(facts), edge_facts, by_id, completed_ids)


def _causal_paths(
    graph: _CurrentGraph, *, root_story_ids: tuple[int, ...]
) -> tuple[StoryDependencyInvalidationPath, ...]:
    facts = {story.story_id: story for story in graph.stories}
    if not set(root_story_ids).issubset(facts):
        message = "Dependency invalidation root references a foreign or missing Story."
        raise StoryDependencyLifecycleIntegrityError(message)
    roots = tuple(
        root
        for root in sorted(set(root_story_ids))
        if (
            not facts[root].is_superseded
            and not is_terminal_external(facts[root])
            and root not in graph.completed_story_ids
        )
    )
    try:
        closure = current_dependency_closure(
            stories=graph.stories,
            dependencies=graph.dependencies,
            root_story_ids=roots,
            include_proposed=True,
            completed_story_ids=graph.completed_story_ids,
        )
    except (ValueError, PlanningLineageError) as error:
        raise StoryDependencyLifecycleIntegrityError(str(error)) from error
    by_dependent: dict[int, list[StoryDependencyFact]] = {}
    for edge in closure:
        by_dependent.setdefault(edge.dependent_story_id, []).append(edge)
    paths: dict[tuple[int, int], StoryDependencyInvalidationPath] = {}
    for root in roots:
        pending: deque[tuple[int, tuple[StoryDependencyFact, ...]]] = deque(
            [(root, ())]
        )
        visited = {root}
        while pending:
            dependent, prior_path = pending.popleft()
            for edge in by_dependent.get(dependent, ()):
                prerequisite = facts[edge.prerequisite_story_id]
                path = (*prior_path, edge)
                if prerequisite.is_superseded:
                    paths.setdefault(
                        (root, prerequisite.story_id),
                        StoryDependencyInvalidationPath(
                            dependent_story_id=root,
                            superseded_story_id=prerequisite.story_id,
                            dependency_rows=path,
                        ),
                    )
                elif (
                    not is_terminal_external(prerequisite)
                    and prerequisite.story_id not in graph.completed_story_ids
                    and prerequisite.story_id not in visited
                ):
                    visited.add(prerequisite.story_id)
                    pending.append((prerequisite.story_id, path))
    return tuple(paths[key] for key in sorted(paths))


def _load_lineage_proof(session: Session, *, project_id: int) -> _LineageProof:
    artifacts = tuple(
        session.exec(
            select(StoryArtifact).where(col(StoryArtifact.project_id) == project_id)
        ).all()
    )
    decisions = tuple(
        session.exec(
            select(StoryArtifactDecision).where(
                col(StoryArtifactDecision.project_id) == project_id
            )
        ).all()
    )
    try:
        nodes = build_story_artifact_lineage_nodes(artifacts, decisions)
        validate_artifact_lineage(nodes)
    except (ValueError, PlanningLineageError) as error:
        raise StoryDependencyLifecycleIntegrityError(str(error)) from error
    return _LineageProof(
        {
            artifact.story_artifact_id: artifact
            for artifact in artifacts
            if artifact.story_artifact_id is not None
        },
        {node.artifact_id: node for node in nodes},
    )


def _first_accepted_successor(
    proof: _LineageProof, *, source_id: int
) -> StoryArtifact | None:
    children = {
        node.supersedes_artifact_id: node
        for node in proof.nodes.values()
        if node.supersedes_artifact_id is not None
    }
    child = children.get(source_id)
    while child is not None:
        if child.decision == "accepted":
            return proof.artifacts[child.artifact_id]
        child = children.get(child.artifact_id)
    return None


def _anchor_for_path(
    path: StoryDependencyInvalidationPath, *, graph: _CurrentGraph, proof: _LineageProof
) -> StoryDependencyInvalidationAnchor:
    source = graph.rows[path.superseded_story_id]
    artifact = proof.artifacts.get(source.source_story_artifact_id)
    node = proof.nodes.get(source.source_story_artifact_id)
    if (
        not source.is_superseded
        or artifact is None
        or node is None
        or node.decision != "accepted"
        or artifact.content_fingerprint != source.source_story_artifact_fingerprint
    ):
        message = (
            "Dependency invalidation source is not one exact accepted "
            "superseded artifact."
        )
        raise StoryDependencyLifecycleIntegrityError(message)
    successor = _first_accepted_successor(
        proof, source_id=source.source_story_artifact_id
    )
    try:
        return StoryDependencyInvalidationAnchor(
            dependent_story_id=path.dependent_story_id,
            superseded_story_id=path.superseded_story_id,
            superseded_story_artifact_id=source.source_story_artifact_id,
            superseded_story_artifact_fingerprint=(
                source.source_story_artifact_fingerprint
            ),
            replacement_story_artifact_id=successor.story_artifact_id
            if successor
            else None,
            replacement_story_artifact_fingerprint=successor.content_fingerprint
            if successor
            else None,
        )
    except ValidationError as error:
        message = "Dependency invalidation anchor contains malformed exact identity."
        raise StoryDependencyLifecycleIntegrityError(message) from error


def _require_anchor(
    anchor: StoryDependencyInvalidationAnchor,
    *,
    graph: _CurrentGraph,
    proof: _LineageProof,
) -> None:
    dependent = graph.rows.get(anchor.dependent_story_id)
    source = graph.rows.get(anchor.superseded_story_id)
    if dependent is None or source is None or dependent.story_id == source.story_id:
        message = (
            "Dependency invalidation anchor references foreign or malformed "
            "Story identities."
        )
        raise StoryDependencyLifecycleIntegrityError(message)
    source_artifact = proof.artifacts.get(anchor.superseded_story_artifact_id)
    source_node = proof.nodes.get(anchor.superseded_story_artifact_id)
    if (
        not source.is_superseded
        or source_artifact is None
        or source_node is None
        or source_node.decision != "accepted"
        or source.source_story_artifact_id != anchor.superseded_story_artifact_id
        or source.source_story_artifact_fingerprint
        != anchor.superseded_story_artifact_fingerprint
        or source_artifact.content_fingerprint
        != anchor.superseded_story_artifact_fingerprint
    ):
        message = (
            "Dependency invalidation anchor contradicts exact superseded "
            "source evidence."
        )
        raise StoryDependencyLifecycleIntegrityError(message)
    if anchor.replacement_story_artifact_id is not None:
        successor = _first_accepted_successor(
            proof, source_id=anchor.superseded_story_artifact_id
        )
        if (
            successor is None
            or successor.story_artifact_id != anchor.replacement_story_artifact_id
            or successor.content_fingerprint
            != anchor.replacement_story_artifact_fingerprint
        ):
            message = (
                "Dependency invalidation successor lacks exact accepted ancestry proof."
            )
            raise StoryDependencyLifecycleIntegrityError(message)


def _require_saved_path(
    path: StoryDependencyInvalidationPath, *, graph: _CurrentGraph
) -> None:
    rows = {row.dependency_id: row for row in graph.dependencies}
    expected = path.dependent_story_id
    visited = {expected}
    for edge in path.dependency_rows:
        actual = rows.get(edge.dependency_id)
        if (
            actual is None
            or edge.status not in {"active", "proposed"}
            or edge.dependent_story_id != expected
            or actual.dependent_story_id != edge.dependent_story_id
            or actual.prerequisite_story_id != edge.prerequisite_story_id
            or edge.prerequisite_story_id not in graph.rows
            or edge.prerequisite_story_id in visited
        ):
            message = (
                "Dependency invalidation causal path contradicts saved "
                "endpoint evidence."
            )
            raise StoryDependencyLifecycleIntegrityError(message)
        expected = edge.prerequisite_story_id
        visited.add(expected)
    if expected != path.superseded_story_id:
        message = (
            "Dependency invalidation causal path does not reach its exact "
            "obsolete endpoint."
        )
        raise StoryDependencyLifecycleIntegrityError(message)


def invalidate_story_dependents_for_replacement_in_session(
    session: Session,
    *,
    artifact: StoryArtifact,
    superseded_stories: tuple[UserStory, ...],
    accepted_at: datetime,
) -> None:
    """Append targeted acceptance evidence in the caller-owned transaction."""
    if not superseded_stories:
        return
    graph = _current_graph(session, project_id=artifact.project_id)
    superseded_ids = {story.story_id for story in superseded_stories}
    if any(story.project_id != artifact.project_id for story in superseded_stories):
        message = "Accepted replacement contains a foreign superseded Story."
        raise StoryDependencyLifecycleIntegrityError(message)
    paths = tuple(
        path
        for path in _causal_paths(graph, root_story_ids=tuple(graph.rows))
        if path.superseded_story_id in superseded_ids
    )
    if not paths:
        return
    proof = _load_lineage_proof(session, project_id=artifact.project_id)
    anchors = tuple(_anchor_for_path(path, graph=graph, proof=proof) for path in paths)
    if any(
        anchor.replacement_story_artifact_id != artifact.story_artifact_id
        or anchor.replacement_story_artifact_fingerprint != artifact.content_fingerprint
        for anchor in anchors
    ):
        message = "Accepted replacement is not the exact first accepted successor."
        raise StoryDependencyLifecycleIntegrityError(message)
    _append_missing_event(
        session,
        project_id=artifact.project_id,
        anchors=anchors,
        paths=paths,
        mode="accepted_replacement",
        recorded_at=accepted_at,
    )


def load_story_dependency_invalidations_in_session(
    session: Session,
    *,
    project_id: int,
) -> tuple[StoryDependencyInvalidationAnchor, ...]:
    """Verify persisted evidence, then normalize its stable identities."""
    events = tuple(
        session.exec(
            select(WorkflowEvent)
            .where(
                col(WorkflowEvent.project_id) == project_id,
                col(WorkflowEvent.event_type)
                == WorkflowEventType.STORY_DEPENDENCY_STALE,
            )
            .order_by(col(WorkflowEvent.event_id))
        ).all()
    )
    if not events:
        return ()
    graph = _current_graph(session, project_id=project_id)
    proof = _load_lineage_proof(session, project_id=project_id)
    anchors: list[StoryDependencyInvalidationAnchor] = []
    for event in events:
        if (
            event.event_id is None
            or event.project_id != project_id
            or event.sprint_id is not None
            or event.duration_seconds is not None
            or event.turn_count is not None
            or event.event_metadata is None
        ):
            message = (
                "Dependency invalidation event has malformed Project audit identity."
            )
            raise StoryDependencyLifecycleIntegrityError(message)
        try:
            metadata = StoryDependencyInvalidationMetadata.model_validate_json(
                event.event_metadata, strict=True
            )
        except ValidationError as error:
            message = "Dependency invalidation event metadata is malformed."
            raise StoryDependencyLifecycleIntegrityError(message) from error
        if canonical_json(metadata.model_dump(mode="json")) != event.event_metadata:
            message = "Dependency invalidation event metadata is not canonical."
            raise StoryDependencyLifecycleIntegrityError(message)
        for anchor, path in zip(metadata.anchors, metadata.causal_paths, strict=True):
            _require_anchor(anchor, graph=graph, proof=proof)
            _require_saved_path(path, graph=graph)
        anchors.extend(metadata.anchors)
    return _canonical_anchors(tuple(anchors))


def _build_inference_lineage(session: Session, *, project_id: int) -> _LineageProof:
    """Build complete artifact proof only after reaching an obsolete endpoint."""
    return _load_lineage_proof(session, project_id=project_id)


def infer_unrecorded_story_dependency_invalidations_in_session(
    session: Session,
    *,
    project_id: int,
    root_story_ids: tuple[int, ...],
) -> tuple[StoryDependencyInvalidationAnchor, ...]:
    """Recover verified legacy identities without mutating stored evidence."""
    graph = _current_graph(session, project_id=project_id)
    paths = _causal_paths(graph, root_story_ids=root_story_ids)
    if not paths:
        return ()
    proof = _build_inference_lineage(session, project_id=project_id)
    return _canonical_anchors(
        tuple(_anchor_for_path(path, graph=graph, proof=proof) for path in paths)
    )


def _append_missing_event(  # noqa: PLR0913
    session: Session,
    *,
    project_id: int,
    anchors: tuple[StoryDependencyInvalidationAnchor, ...],
    paths: tuple[StoryDependencyInvalidationPath, ...],
    mode: Literal["accepted_replacement", "review_adoption"],
    recorded_at: datetime,
) -> None:
    existing = load_story_dependency_invalidations_in_session(
        session, project_id=project_id
    )
    identities = {_identity(anchor) for anchor in existing}
    missing = tuple(
        anchor
        for anchor in _canonical_anchors(anchors)
        if _identity(anchor) not in identities
    )
    if not missing:
        return
    by_pair = {
        (path.dependent_story_id, path.superseded_story_id): path for path in paths
    }
    metadata = StoryDependencyInvalidationMetadata(
        mode=mode,
        anchors=missing,
        causal_paths=tuple(
            by_pair[(anchor.dependent_story_id, anchor.superseded_story_id)]
            for anchor in missing
        ),
    )
    session.add(
        WorkflowEvent(
            event_type=WorkflowEventType.STORY_DEPENDENCY_STALE,
            project_id=project_id,
            timestamp=recorded_at,
            event_metadata=canonical_json(metadata.model_dump(mode="json")),
        )
    )
    session.flush()


def adopt_story_dependency_invalidations_in_session(
    session: Session,
    *,
    project_id: int,
    anchors: tuple[StoryDependencyInvalidationAnchor, ...],
    reviewed_at: datetime,
) -> None:
    """Persist missing implicit identities before caller reconciliation writes."""
    if not anchors:
        return
    graph = _current_graph(session, project_id=project_id)
    proof = _load_lineage_proof(session, project_id=project_id)
    for anchor in anchors:
        _require_anchor(anchor, graph=graph, proof=proof)
    obsolete_ids = {anchor.superseded_story_id for anchor in anchors}
    paths = tuple(
        path
        for path in _causal_paths(graph, root_story_ids=tuple(graph.rows))
        if path.superseded_story_id in obsolete_ids
    )
    inferred = tuple(_anchor_for_path(path, graph=graph, proof=proof) for path in paths)
    current_identities = {_identity(anchor) for anchor in inferred}
    recorded_identities = {
        _identity(anchor)
        for anchor in load_story_dependency_invalidations_in_session(
            session, project_id=project_id
        )
    }
    if any(
        _identity(anchor) not in current_identities | recorded_identities
        for anchor in anchors
    ):
        message = (
            "Dependency invalidation adoption lacks current causal endpoint evidence."
        )
        raise StoryDependencyLifecycleIntegrityError(message)
    _append_missing_event(
        session,
        project_id=project_id,
        anchors=inferred,
        paths=paths,
        mode="review_adoption",
        recorded_at=reviewed_at,
    )


def validate_reviewed_dependency_lifecycle_in_session(
    session: Session,
    *,
    project_id: int,
    selected_story_ids: tuple[int, ...],
    reviewed_edges: tuple[StoryDependencyReviewEdgeFact, ...],
) -> None:
    """Reject unresolved prospective authority with ValueError.

    Malformed stored evidence raises StoryDependencyLifecycleIntegrityError.
    """
    graph = _current_graph(session, project_id=project_id)
    if any(story_id not in graph.rows for story_id in selected_story_ids):
        message = "Reviewed dependency scope references a missing Story."
        raise ValueError(message)
    selected_ids = set(selected_story_ids)
    prospective = [
        edge
        for edge in graph.dependencies
        if edge.dependent_story_id not in selected_ids
    ]
    next_id = max((edge.dependency_id for edge in graph.dependencies), default=0) + 1
    for offset, edge in enumerate(reviewed_edges):
        if (
            edge.dependent_story_id not in selected_ids
            or edge.prerequisite_story_id not in graph.rows
        ):
            message = (
                "Reviewed dependency endpoints do not belong to the requested "
                "Project scope."
            )
            raise ValueError(message)
        prospective.append(
            StoryDependencyFact(
                dependency_id=next_id + offset,
                dependent_story_id=edge.dependent_story_id,
                prerequisite_story_id=edge.prerequisite_story_id,
                status="active",
                source="manual_review",
                confidence="reviewed",
                reason=edge.reason,
            )
        )
    try:
        closure = current_dependency_closure(
            stories=graph.stories,
            dependencies=tuple(prospective),
            root_story_ids=selected_story_ids,
            include_proposed=True,
            completed_story_ids=graph.completed_story_ids,
        )
    except ValueError as error:
        raise StoryDependencyLifecycleIntegrityError(str(error)) from error
    if any(graph.rows[edge.prerequisite_story_id].is_superseded for edge in closure):
        message = (
            "Reviewed dependencies retain a superseded prerequisite requiring "
            "human reconciliation."
        )
        raise ValueError(message)


def selected_scope_dependency_lifecycle_fingerprint(
    *,
    selected_scope_fingerprint: str,
    selected_story_ids: tuple[int, ...],
    invalidation_anchors: tuple[StoryDependencyInvalidationAnchor, ...],
) -> str:
    """Revise only selected identities; diagnostics never participate."""
    selected_ids = set(selected_story_ids)
    identities = sorted(
        {
            _identity(anchor)
            for anchor in invalidation_anchors
            if anchor.dependent_story_id in selected_ids
        }
    )
    if not identities:
        return selected_scope_fingerprint
    return canonical_hash(
        {
            "schema_version": "agileforge.story-selected-scope.v2",
            "selected_story_scope_fingerprint": selected_scope_fingerprint,
            "invalidation_anchors": [
                {
                    "dependent_story_id": dependent,
                    "superseded_story_id": source,
                    "superseded_story_artifact_id": artifact,
                    "superseded_story_artifact_fingerprint": fingerprint,
                }
                for dependent, source, artifact, fingerprint in identities
            ],
        }
    )
