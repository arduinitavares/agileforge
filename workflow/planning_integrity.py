"""Canonical integrity helpers shared by planning writes, facts, and rules."""

from __future__ import annotations

from typing import TYPE_CHECKING

from models.enums import TaskStatus
from utils.task_metadata import metadata_from_structured_task, serialize_task_metadata
from workflow.facts import (
    StoryDependencyReviewEdgeFact,
)
from workflow.fingerprints import canonical_hash

if TYPE_CHECKING:
    from collections.abc import Iterable

    from orchestrator_agent.agent_tools.sprint_planner_tool.schemes import (
        SprintPlannerOutput,
    )
    from workflow.contracts import JsonObject
    from workflow.facts import StoryDependencyFact, TaskFact


def dependency_edge_payload(edge: StoryDependencyReviewEdgeFact) -> JsonObject:
    """Return one canonical dependency edge payload."""
    return {
        "dependent_story_id": edge.dependent_story_id,
        "prerequisite_story_id": edge.prerequisite_story_id,
        "reason": edge.reason,
    }


def dependency_edges_payload(
    edges: Iterable[StoryDependencyReviewEdgeFact],
) -> list[JsonObject]:
    """Return canonical payloads without changing caller-provided order."""
    return [dependency_edge_payload(edge) for edge in edges]


def dependency_review_fingerprint(
    edges: Iterable[StoryDependencyReviewEdgeFact],
) -> str:
    """Hash reviewed dependency semantics with the write-time function."""
    return canonical_hash(dependency_edges_payload(edges))


def canonical_dependency_edges(
    edges: Iterable[StoryDependencyReviewEdgeFact],
) -> tuple[StoryDependencyReviewEdgeFact, ...]:
    """Return edges in stable endpoint order."""
    return tuple(
        sorted(
            edges,
            key=lambda edge: (
                edge.dependent_story_id,
                edge.prerequisite_story_id,
            ),
        )
    )


def dependency_edges_are_canonical(
    edges: tuple[StoryDependencyReviewEdgeFact, ...],
) -> bool:
    """Require stable order and unique directed endpoints."""
    pairs = tuple(
        (edge.dependent_story_id, edge.prerequisite_story_id) for edge in edges
    )
    return pairs == tuple(sorted(set(pairs)))


def dependency_edges_have_cycle(
    edges: Iterable[StoryDependencyReviewEdgeFact],
) -> bool:
    """Return whether directed dependency semantics contain a cycle."""
    graph: dict[int, set[int]] = {}
    nodes: set[int] = set()
    for edge in edges:
        graph.setdefault(edge.dependent_story_id, set()).add(
            edge.prerequisite_story_id
        )
        nodes.update((edge.dependent_story_id, edge.prerequisite_story_id))
    active: set[int] = set()
    visited: set[int] = set()

    def visit(story_id: int) -> bool:
        if story_id in active:
            return True
        if story_id in visited:
            return False
        active.add(story_id)
        found = any(visit(parent) for parent in sorted(graph.get(story_id, set())))
        active.remove(story_id)
        visited.add(story_id)
        return found

    return any(visit(story_id) for story_id in sorted(nodes))


def active_dependency_review_edges(
    dependencies: Iterable[StoryDependencyFact],
) -> tuple[StoryDependencyReviewEdgeFact, ...]:
    """Project current active dependency facts into reviewed semantics."""
    return canonical_dependency_edges(
        StoryDependencyReviewEdgeFact(
            dependent_story_id=edge.dependent_story_id,
            prerequisite_story_id=edge.prerequisite_story_id,
            reason=edge.reason or "",
        )
        for edge in dependencies
        if edge.status == "active"
    )


def planned_task_content_fingerprint(plan: SprintPlannerOutput) -> str:
    """Hash every persisted task semantic field represented by a plan."""
    payload: list[JsonObject] = []
    for selected in sorted(plan.selected_stories, key=lambda item: item.story_id):
        for ordinal, task in enumerate(selected.tasks, start=1):
            payload.append(
                {
                    "story_id": selected.story_id,
                    "task_ordinal": ordinal,
                    "description": task.description,
                    "metadata_json": serialize_task_metadata(
                        metadata_from_structured_task(task)
                    ),
                    "status": TaskStatus.TO_DO.value,
                }
            )
    return canonical_hash(payload)


def current_task_content_fingerprint(
    tasks: Iterable[TaskFact],
    *,
    sprint_id: int,
    story_ids: tuple[int, ...],
) -> str:
    """Hash persisted task semantics in stable Story and task identity order."""
    selected = set(story_ids)
    relevant = sorted(
        (
            task
            for task in tasks
            if task.sprint_id == sprint_id and task.story_id in selected
        ),
        key=lambda task: (task.story_id, task.task_id),
    )
    ordinals: dict[int, int] = {}
    payload: list[JsonObject] = []
    for task in relevant:
        ordinal = ordinals.get(task.story_id, 0) + 1
        ordinals[task.story_id] = ordinal
        payload.append(
            {
                "story_id": task.story_id,
                "task_ordinal": ordinal,
                "description": task.description,
                "metadata_json": task.metadata_json,
                "status": task.status,
            }
        )
    return canonical_hash(payload)


__all__ = [
    "active_dependency_review_edges",
    "canonical_dependency_edges",
    "current_task_content_fingerprint",
    "dependency_edges_are_canonical",
    "dependency_edges_have_cycle",
    "dependency_edges_payload",
    "dependency_review_fingerprint",
    "planned_task_content_fingerprint",
]
