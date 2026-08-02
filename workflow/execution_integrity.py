"""Canonical fingerprints shared by execution graph reads and writes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from workflow.fingerprints import canonical_hash

if TYPE_CHECKING:
    from workflow.contracts import JsonObject
    from workflow.facts import (
        StoryFact,
        TaskCompletionFact,
        TaskFact,
        WorkflowFactSnapshot,
    )


def task_evidence_fingerprint(
    task: TaskFact,
    *,
    outcome_summary: str,
    artifact_refs: tuple[str, ...],
    acceptance_result: Literal["partially_met", "fully_met"],
    checklist_result: JsonObject,
) -> str:
    """Bind completion evidence to one exact immutable Task contract."""
    return canonical_hash(
        {
            "task_id": task.task_id,
            "sprint_id": task.sprint_id,
            "story_id": task.story_id,
            "description": task.description,
            "metadata_json": task.metadata_json,
            "outcome_summary": outcome_summary,
            "artifact_refs": artifact_refs,
            "acceptance_result": acceptance_result,
            "checklist_result": checklist_result,
        }
    )


def story_completion_fingerprint(
    story: StoryFact,
    tasks: tuple[TaskFact, ...],
    completions: tuple[TaskCompletionFact, ...],
) -> str:
    """Bind Story closure to its exact terminal Task and evidence set."""
    relevant_tasks = tuple(
        sorted(
            (item for item in tasks if item.story_id == story.story_id),
            key=lambda item: (item.sprint_id, item.task_id),
        )
    )
    completion_by_key = {
        (item.sprint_id, item.task_id): item for item in completions
    }
    return canonical_hash(
        {
            "story_id": story.story_id,
            "sprint_ids": tuple(sorted(story.sprint_ids)),
            "tasks": [
                {
                    "task_id": item.task_id,
                    "sprint_id": item.sprint_id,
                    "status": item.status,
                    "evidence_fingerprint": (
                        completion_by_key[
                            (item.sprint_id, item.task_id)
                        ].evidence_fingerprint
                        if (item.sprint_id, item.task_id) in completion_by_key
                        else None
                    ),
                }
                for item in relevant_tasks
            ],
        }
    )


def sprint_review_fingerprint(
    snapshot: WorkflowFactSnapshot,
    sprint_id: int,
) -> str:
    """Bind Sprint review to every attached terminal Story and Task fact."""
    story_ids = tuple(
        sorted(
            item.story_id
            for item in snapshot.stories
            if sprint_id in item.sprint_ids
        )
    )
    closures = tuple(
        sorted(
            (
                item
                for item in snapshot.story_completions
                if item.sprint_id == sprint_id and item.story_id in story_ids
            ),
            key=lambda item: item.story_id,
        )
    )
    return canonical_hash(
        {
            "sprint_id": sprint_id,
            "story_ids": story_ids,
            "story_completions": [
                {
                    "story_id": item.story_id,
                    "completion_fingerprint": item.completion_fingerprint,
                    "resolution": item.resolution,
                    "delivered": item.delivered,
                    "evidence": item.evidence,
                    "known_gaps": item.known_gaps,
                }
                for item in closures
            ],
        }
    )


def triage_payload_fingerprint(
    impact: Literal["none", "backlog", "specification"] | str,
    canonical_payload: JsonObject,
) -> str:
    """Hash one canonical triage impact and payload."""
    return canonical_hash({"impact": impact, "canonical_payload": canonical_payload})


__all__ = [
    "sprint_review_fingerprint",
    "story_completion_fingerprint",
    "task_evidence_fingerprint",
    "triage_payload_fingerprint",
]
