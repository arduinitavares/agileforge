"""Story dependency graph loading and diagnostics."""

from dataclasses import dataclass
from datetime import datetime

from sqlmodel import Session, col, select

from models.core import UserStory, UserStoryDependency
from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from models.workflow import StoryDependencyReview
from services.agent_workbench.story_phase import (
    load_story_correction_target_in_session,
)
from workflow.facts import StoryDependencyReviewEdgeFact
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.planning_integrity import (
    canonical_dependency_edges,
    dependency_edges_have_duplicate_endpoints,
    dependency_edges_payload,
    dependency_review_fingerprint,
)


@dataclass(frozen=True)
class DependencyGraphIssue:
    """A dependency graph issue safe to expose in CLI JSON."""

    code: str
    message: str
    story_ids: list[int]
    edge_status: str | None = None
    dependency_id: int | None = None
    dependent_story_id: int | None = None
    prerequisite_story_id: int | None = None


@dataclass(frozen=True)
class SelectedScopeStory:
    """Exact Story, evidence, and latest human-selection lineage in one scope."""

    story_id: int
    source_story_artifact_id: int
    source_story_artifact_fingerprint: str
    source_story_item_id: str
    source_story_item_fingerprint: str
    accepted_spec_version_id: int
    accepted_spec_hash: str
    validation_evidence_fingerprint: str
    selection_state_fingerprint: str
    selection_event_id: int
    selection_event_fingerprint: str


def selected_scope_fingerprint(
    *,
    project_id: int,
    stories: tuple[SelectedScopeStory, ...],
) -> str:
    """Bind exact selected Story lineages to current evidence and intent facts."""
    ordered = tuple(sorted(stories, key=lambda item: item.story_id))
    if len({item.story_id for item in ordered}) != len(ordered):
        message = "Selected Story scope contains duplicate Story identities."
        raise ValueError(message)
    return canonical_hash(
        {
            "schema_version": "agileforge.story-selected-scope.v1",
            "project_id": project_id,
            "stories": [
                {
                    "story_id": item.story_id,
                    "source_story_artifact_id": item.source_story_artifact_id,
                    "source_story_artifact_fingerprint": (
                        item.source_story_artifact_fingerprint
                    ),
                    "source_story_item_id": item.source_story_item_id,
                    "source_story_item_fingerprint": (
                        item.source_story_item_fingerprint
                    ),
                    "accepted_spec_version_id": item.accepted_spec_version_id,
                    "accepted_spec_hash": item.accepted_spec_hash,
                    "validation_evidence_fingerprint": (
                        item.validation_evidence_fingerprint
                    ),
                    "selection_state": "selected",
                    "selection_state_fingerprint": (
                        item.selection_state_fingerprint
                    ),
                    "selection_event_id": item.selection_event_id,
                    "selection_event_fingerprint": (
                        item.selection_event_fingerprint
                    ),
                }
                for item in ordered
            ],
        }
    )


class StoryDependencyGraphError(RuntimeError):
    """Raised when active dependencies are not safe for sprint planning."""

    def __init__(self, issues: list[DependencyGraphIssue]) -> None:
        """Initialize with graph issues to expose through CLI diagnostics."""
        self.issues = issues
        codes = ", ".join(sorted({issue.code for issue in issues}))
        super().__init__(f"Story dependency graph invalid for sprint planning: {codes}")


def detect_dependency_cycles(edges: dict[int, set[int]]) -> list[list[int]]:
    """Return deterministic directed cycle paths from dependency edges."""
    visited: set[int] = set()
    active: set[int] = set()
    path: list[int] = []
    cycles: list[list[int]] = []
    seen_cycle_keys: set[tuple[int, ...]] = set()
    nodes = set(edges)
    for prerequisites in edges.values():
        nodes.update(prerequisites)

    def visit(node_id: int) -> None:
        visited.add(node_id)
        active.add(node_id)
        path.append(node_id)

        for prerequisite_id in sorted(edges.get(node_id, set())):
            if prerequisite_id not in visited:
                visit(prerequisite_id)
            elif prerequisite_id in active:
                cycle_start = path.index(prerequisite_id)
                cycle_path = [*path[cycle_start:], prerequisite_id]
                cycle_key = _canonical_cycle_key(cycle_path)
                if cycle_key not in seen_cycle_keys:
                    seen_cycle_keys.add(cycle_key)
                    cycles.append(cycle_path)

        path.pop()
        active.remove(node_id)

    for node_id in sorted(nodes):
        if node_id not in visited:
            visit(node_id)

    return cycles


@dataclass(frozen=True)
class ApplyStoryDependenciesInput:
    """Exact caller-owned mutation inputs for dependency review."""

    project_id: int
    selected_story_ids: tuple[int, ...]
    reviewed_edges: tuple[StoryDependencyReviewEdgeFact, ...]
    source_fingerprint: str
    reviewer: str
    reviewed_at: datetime


def apply_story_dependencies_in_session(  # noqa: C901, PLR0915
    session: Session,
    *,
    inputs: ApplyStoryDependenciesInput,
) -> StoryDependencyReview:
    """Apply an exact acyclic reviewed edge set in the caller transaction."""
    project_id = inputs.project_id
    selected_story_ids = inputs.selected_story_ids
    reviewed_edges = canonical_dependency_edges(inputs.reviewed_edges)
    reviewed_at = inputs.reviewed_at
    selected = set(selected_story_ids)
    if dependency_edges_have_duplicate_endpoints(reviewed_edges):
        message = "Dependency review contains duplicate directed endpoints."
        raise StoryDependencyGraphError(
            [
                DependencyGraphIssue(
                    code="STORY_DEPENDENCY_DUPLICATE_EDGE",
                    message=message,
                    story_ids=sorted(selected),
                )
            ]
        )
    endpoint_ids = selected | {
        edge.prerequisite_story_id for edge in reviewed_edges
    }
    stories = session.exec(
        select(UserStory).where(col(UserStory.story_id).in_(endpoint_ids))
    ).all()
    stories_by_id = {
        story.story_id: story for story in stories if story.story_id is not None
    }
    if set(stories_by_id) != endpoint_ids or any(
        story.project_id != project_id or story.is_superseded
        for story in stories_by_id.values()
    ):
        message = "Dependency review does not target exact active Project stories."
        raise StoryDependencyGraphError(
            [
                DependencyGraphIssue(
                    code="STORY_DEPENDENCY_STORY_SET_INVALID",
                    message=message,
                    story_ids=sorted(endpoint_ids),
                )
            ]
        )
    specification_roots = {
        (story.accepted_spec_version_id, story.accepted_spec_hash)
        for story in stories_by_id.values()
    }
    if len(specification_roots) != 1:
        message = "Dependency review cannot cross accepted Specification roots."
        raise StoryDependencyGraphError(
            [
                DependencyGraphIssue(
                    code="STORY_DEPENDENCY_CROSS_SPECIFICATION",
                    message=message,
                    story_ids=sorted(endpoint_ids),
                )
            ]
        )
    try:
        for story_id in sorted(endpoint_ids):
            load_story_correction_target_in_session(
                session,
                project_id=project_id,
                story_id=story_id,
            )
    except ValueError as error:
        raise StoryDependencyGraphError(
            [
                DependencyGraphIssue(
                    code="STORY_DEPENDENCY_STORY_SET_INVALID",
                    message=str(error),
                    story_ids=sorted(endpoint_ids),
                )
            ]
        ) from error
    pairs = tuple(
        (edge.dependent_story_id, edge.prerequisite_story_id) for edge in reviewed_edges
    )
    if any(left not in selected for left, _right in pairs):
        message = "Dependency review edge dependent leaves the selected Story set."
        raise StoryDependencyGraphError(
            [
                DependencyGraphIssue(
                    code="STORY_DEPENDENCY_CROSS_SELECTION",
                    message=message,
                    story_ids=sorted(selected),
                )
            ]
        )
    graph: dict[int, set[int]] = {}
    for dependent_story_id, prerequisite_story_id in pairs:
        graph.setdefault(dependent_story_id, set()).add(prerequisite_story_id)
    cycles = detect_dependency_cycles(graph)
    if cycles:
        raise StoryDependencyGraphError(
            [
                DependencyGraphIssue(
                    code="STORY_DEPENDENCY_CYCLE",
                    message="Reviewed Story dependency graph contains a cycle.",
                    story_ids=cycle,
                    edge_status="active",
                )
                for cycle in cycles
            ]
        )
    existing_rows = session.exec(
        select(UserStoryDependency).where(UserStoryDependency.project_id == project_id)
    ).all()
    existing_by_pair = {
        (row.dependent_story_id, row.prerequisite_story_id): row
        for row in existing_rows
    }
    reviewed_by_pair = {
        (edge.dependent_story_id, edge.prerequisite_story_id): edge.reason
        for edge in reviewed_edges
    }
    for pair, row in existing_by_pair.items():
        if row.dependent_story_id not in selected:
            continue
        row.status = "active" if pair in reviewed_by_pair else "rejected"
        row.source = "manual_review"
        row.confidence = "reviewed"
        row.reason = reviewed_by_pair.get(pair, "Rejected by dependency review.")
        row.updated_at = reviewed_at
        session.add(row)
    for pair, reason in reviewed_by_pair.items():
        if pair in existing_by_pair:
            continue
        dependent_story_id, prerequisite_story_id = pair
        session.add(
            UserStoryDependency(
                project_id=project_id,
                dependent_story_id=dependent_story_id,
                prerequisite_story_id=prerequisite_story_id,
                status="active",
                source="manual_review",
                confidence="reviewed",
                reason=reason,
                created_at=reviewed_at,
                updated_at=reviewed_at,
            )
        )
    edge_payload = dependency_edges_payload(reviewed_edges)
    review = StoryDependencyReview(
        project_id=project_id,
        selected_story_ids_json=canonical_json(list(selected_story_ids)),
        reviewed_edges_json=canonical_json(edge_payload),
        source_fingerprint=inputs.source_fingerprint,
        dependency_fingerprint=dependency_review_fingerprint(reviewed_edges),
        reviewed_by=inputs.reviewer,
        reviewed_at=reviewed_at,
    )
    session.add(review)
    session.add(
        WorkflowEvent(
            event_type=WorkflowEventType.STORY_DEPENDENCIES_APPLIED,
            timestamp=reviewed_at,
            project_id=project_id,
            duration_seconds=0.0,
            event_metadata=canonical_json(
                {
                    "action": "story_dependencies_reviewed",
                    "dependency_fingerprint": review.dependency_fingerprint,
                    "selected_story_ids": list(selected_story_ids),
                    "source_fingerprint": inputs.source_fingerprint,
                }
            ),
        )
    )
    session.flush()
    return review


def _canonical_cycle_key(cycle_path: list[int]) -> tuple[int, ...]:
    cycle_body = cycle_path[:-1]
    if not cycle_body:
        return ()
    rotations = [
        tuple(cycle_body[index:] + cycle_body[:index])
        for index in range(len(cycle_body))
    ]
    return min(rotations)
