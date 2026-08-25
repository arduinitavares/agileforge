"""Canonical integrity shared by execution graph reads and writes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from models.enums import TaskStatus
from workflow.fingerprints import canonical_hash
from workflow.planning_integrity import (
    active_dependency_review_edges,
    dependency_review_fingerprint,
    selected_dependency_active_closure,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import NoReturn

    from workflow.contracts import JsonObject
    from workflow.facts import (
        PlanningArtifactFact,
        ReviewDecisionFact,
        SprintStartFact,
        StoryDependencyFact,
        StoryDependencyReviewEdgeFact,
        StoryDependencyReviewFact,
        StoryFact,
        TaskFact,
        WorkflowFactSnapshot,
    )


class ExecutionIntegrityError(ValueError):
    """Normalized execution facts cannot prove one coherent contract."""


@dataclass(frozen=True)
class ExecutionContract:
    """Exact immutable execution lineage for one started Sprint."""

    sprint_id: int
    start: SprintStartFact
    plan: PlanningArtifactFact
    decision: ReviewDecisionFact
    dependency_review: StoryDependencyReviewFact
    stories: tuple[StoryFact, ...]
    tasks: tuple[TaskFact, ...]
    dependencies: tuple[StoryDependencyFact, ...]
    fingerprint: str


@dataclass(frozen=True)
class SprintStartAudit:
    """Canonical metadata values persisted for one StartSprint transition."""

    sprint_id: int
    team_id: int
    sprint_plan_artifact_id: int
    sprint_plan_artifact_decision_id: int
    story_dependency_review_id: int
    plan_fingerprint: str
    candidate_set_fingerprint: str
    selected_story_ids: tuple[int, ...]
    task_content_fingerprint: str
    dependency_source_fingerprint: str
    dependency_fingerprint: str
    dependency_rows_fingerprint: str
    decision_fingerprint: str
    started_by: str


@dataclass(frozen=True)
class SelectedStoryDependencySnapshot:
    """Canonical dependency facts incident to one exact selected Story set."""

    story_ids: tuple[int, ...]
    stories: tuple[StoryFact, ...]
    dependencies: tuple[StoryDependencyFact, ...]
    reviewed_edges: tuple[StoryDependencyReviewEdgeFact, ...]
    source_fingerprint: str
    dependency_fingerprint: str
    rows_fingerprint: str


@dataclass(frozen=True)
class TaskEvidencePayload:
    """Canonical user-supplied evidence for one Task completion."""

    outcome_summary: str
    artifact_refs: tuple[str, ...]
    acceptance_result: Literal["partially_met", "fully_met"]
    checklist_result: JsonObject


@dataclass(frozen=True)
class StoryClosurePayload:
    """Canonical user-supplied evidence for one Story closure."""

    resolution: str
    delivered: str
    evidence: str
    known_gaps: str


@dataclass(frozen=True)
class _ExecutionContractFacts:
    start: SprintStartFact
    plan: PlanningArtifactFact
    decision: ReviewDecisionFact
    dependency_review: StoryDependencyReviewFact
    stories: tuple[StoryFact, ...]
    tasks: tuple[TaskFact, ...]
    dependencies: tuple[StoryDependencyFact, ...]


def _fail(message: str, *, cause: Exception | None = None) -> NoReturn:
    if cause is not None:
        raise ExecutionIntegrityError(message) from cause
    raise ExecutionIntegrityError(message)


def _accepted_story_payload(story: StoryFact) -> dict[str, object]:
    return {
        "story_id": story.story_id,
        "source_story_item_id": story.source_story_item_id,
        "content_fingerprint": story.content_fingerprint,
        "content_accepted": story.content_accepted,
        "story_artifact_id": story.story_artifact_id,
        "accepted_spec_version_id": story.accepted_spec_version_id,
        "accepted_spec_hash": story.accepted_spec_hash,
        "backlog_artifact_id": story.backlog_artifact_id,
        "backlog_artifact_fingerprint": story.backlog_artifact_fingerprint,
        "roadmap_artifact_id": story.roadmap_artifact_id,
        "roadmap_artifact_fingerprint": story.roadmap_artifact_fingerprint,
    }


def accepted_story_source_fingerprint(stories: tuple[StoryFact, ...]) -> str:
    """Hash exact accepted Story content without mutable lifecycle fields."""
    return canonical_hash(
        [
            _accepted_story_payload(story)
            for story in sorted(stories, key=lambda item: item.story_id)
        ]
    )


def dependency_rows_fingerprint(
    dependencies: tuple[StoryDependencyFact, ...],
) -> str:
    """Hash exact dependency identities and all persisted edge fields."""
    return canonical_hash(
        [
            item.model_dump(mode="json")
            for item in sorted(
                dependencies,
                key=lambda edge: (
                    edge.dependent_story_id,
                    edge.prerequisite_story_id,
                    edge.dependency_id,
                ),
            )
        ]
    )


def selected_story_dependency_snapshot(
    snapshot: WorkflowFactSnapshot,
    selected_story_ids: tuple[int, ...],
) -> SelectedStoryDependencySnapshot:
    """Scope dependency identity, edges, and Story source to selected Stories."""
    canonical_story_ids = selected_story_ids
    if not canonical_story_ids or len(canonical_story_ids) != len(
        set(canonical_story_ids)
    ):
        _fail("Selected Story dependency scope is not canonical.")
    stories_by_id = {item.story_id: item for item in snapshot.stories}
    if len(stories_by_id) != len(snapshot.stories):
        _fail("Story facts contain duplicate identities.")
    try:
        stories = tuple(stories_by_id[story_id] for story_id in canonical_story_ids)
    except KeyError as error:
        _fail(
            "Selected Story dependency scope references a missing Story.",
            cause=error,
        )
    if any(
        not item.content_accepted
        or item.content_fingerprint is None
        or item.story_artifact_id is None
        for item in stories
    ):
        _fail("Selected dependency scope requires accepted Story content.")
    selected = set(canonical_story_ids)
    try:
        direct_dependencies = tuple(
            item
            for item in snapshot.story_dependencies
            if item.dependent_story_id in selected
        )
        dependencies = selected_dependency_active_closure(
            snapshot.story_dependencies,
            canonical_story_ids,
            project_story_ids=frozenset(stories_by_id),
        )
        reviewed_edges = active_dependency_review_edges(direct_dependencies)
    except (ValueError, ValidationError) as error:
        _fail("Selected dependency edges are not reviewable.", cause=error)
    if any(edge.prerequisite_story_id not in stories_by_id for edge in reviewed_edges):
        _fail("Selected dependency scope references a missing prerequisite Story.")
    source_fingerprints = {story.selected_scope_fingerprint for story in stories}
    if None in source_fingerprints or len(source_fingerprints) != 1:
        _fail("Selected Story scope fingerprint is missing or conflicting.")
    source_fingerprint = next(
        item for item in source_fingerprints if item is not None
    )
    return SelectedStoryDependencySnapshot(
        story_ids=canonical_story_ids,
        stories=stories,
        dependencies=dependencies,
        reviewed_edges=reviewed_edges,
        source_fingerprint=source_fingerprint,
        dependency_fingerprint=dependency_review_fingerprint(reviewed_edges),
        rows_fingerprint=dependency_rows_fingerprint(dependencies),
    )


def execution_task_content_fingerprint(
    tasks: tuple[TaskFact, ...],
    *,
    sprint_id: int,
    story_ids: tuple[int, ...],
) -> str:
    """Project current Tasks back to the canonical accepted plan shape."""
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
    payload: list[dict[str, object]] = []
    for task in relevant:
        ordinal = ordinals.get(task.story_id, 0) + 1
        ordinals[task.story_id] = ordinal
        payload.append(
            {
                "story_id": task.story_id,
                "task_ordinal": ordinal,
                "description": task.description,
                "metadata_json": task.metadata_json,
                "status": TaskStatus.TO_DO.value,
            }
        )
    return canonical_hash(payload)


def current_dependency_review(
    snapshot: WorkflowFactSnapshot,
) -> StoryDependencyReviewFact:
    """Return the one review matching current candidate Story dependency facts."""
    stories = tuple(
        sorted(
            (item for item in snapshot.stories if item.sprint_candidate),
            key=lambda item: item.story_id,
        )
    )
    selected_story_ids = tuple(item.story_id for item in stories)
    source_fingerprints = {item.selected_scope_fingerprint for item in stories}
    if None in source_fingerprints or len(source_fingerprints) != 1:
        _fail("Current selected Story scope fingerprint is missing or conflicting.")
    source_fingerprint = next(
        item for item in source_fingerprints if item is not None
    )
    selected_id_set = set(selected_story_ids)
    try:
        edges = active_dependency_review_edges(
            item
            for item in snapshot.story_dependencies
            if item.dependent_story_id in selected_id_set
        )
    except ValueError as error:
        _fail("Current dependency edges are not reviewable.", cause=error)
    fingerprint = dependency_review_fingerprint(edges)
    matching = tuple(
        item
        for item in snapshot.story_dependency_reviews
        if item.selected_story_ids == selected_story_ids
        and item.source_fingerprint == source_fingerprint
        and item.reviewed_edges == edges
        and item.dependency_fingerprint == fingerprint
    )
    if len(matching) != 1:
        _fail("Current Story dependency review is missing or conflicting.")
    return matching[0]


def sprint_start_audit_metadata(audit: SprintStartAudit) -> JsonObject:
    """Return canonical StartSprint event metadata for exact lineage checks."""
    return {
        "action": "sprint_started",
        "sprint_id": audit.sprint_id,
        "team_id": audit.team_id,
        "sprint_plan_artifact_id": audit.sprint_plan_artifact_id,
        "sprint_plan_artifact_decision_id": (audit.sprint_plan_artifact_decision_id),
        "story_dependency_review_id": audit.story_dependency_review_id,
        "plan_fingerprint": audit.plan_fingerprint,
        "candidate_set_fingerprint": audit.candidate_set_fingerprint,
        "selected_story_ids": list(audit.selected_story_ids),
        "task_content_fingerprint": audit.task_content_fingerprint,
        "dependency_source_fingerprint": audit.dependency_source_fingerprint,
        "dependency_fingerprint": audit.dependency_fingerprint,
        "dependency_rows_fingerprint": audit.dependency_rows_fingerprint,
        "decision_fingerprint": audit.decision_fingerprint,
        "started_by": audit.started_by,
    }


def _one_by_id[T](
    rows: tuple[T, ...],
    *,
    identity: int,
    identity_of: Callable[[T], int],
    label: str,
) -> T:
    matching = tuple(row for row in rows if identity_of(row) == identity)
    if len(matching) != 1:
        _fail(f"{label} identity is missing or conflicting.")
    return matching[0]


def _contract_fingerprint(facts: _ExecutionContractFacts) -> str:
    return canonical_hash(
        {
            "start": facts.start.model_dump(mode="json"),
            "plan": facts.plan.model_copy(update={"status": "accepted"}).model_dump(
                mode="json"
            ),
            "decision": facts.decision.model_dump(mode="json"),
            "dependency_review": facts.dependency_review.model_dump(mode="json"),
            "stories": [_accepted_story_payload(item) for item in facts.stories],
            "tasks": [
                {
                    "task_id": item.task_id,
                    "sprint_id": item.sprint_id,
                    "story_id": item.story_id,
                    "description": item.description,
                    "metadata_json": item.metadata_json,
                }
                for item in facts.tasks
            ],
            "dependencies": [
                item.model_dump(mode="json") for item in facts.dependencies
            ],
        }
    )


def _start_contract_facts(
    snapshot: WorkflowFactSnapshot,
    sprint_id: int,
) -> tuple[
    SprintStartFact,
    PlanningArtifactFact,
    ReviewDecisionFact,
    StoryDependencyReviewFact,
]:
    starts = tuple(
        item for item in snapshot.sprint_starts if item.sprint_id == sprint_id
    )
    if len(starts) != 1:
        _fail("Sprint start lineage is missing or conflicting.")
    start = starts[0]
    plan = _one_by_id(
        tuple(
            item
            for item in snapshot.planning_artifacts
            if item.artifact_type == "sprint_plan"
        ),
        identity=start.sprint_plan_artifact_id,
        identity_of=lambda item: item.artifact_id,
        label="Sprint plan",
    )
    decision = _one_by_id(
        tuple(
            item for item in snapshot.review_decisions if item.artifact_type == "sprint"
        ),
        identity=start.sprint_plan_artifact_decision_id,
        identity_of=lambda item: item.decision_id,
        label="Sprint plan decision",
    )
    dependency_review = _one_by_id(
        snapshot.story_dependency_reviews,
        identity=start.story_dependency_review_id,
        identity_of=lambda item: item.review_id,
        label="Story dependency review",
    )
    if (
        plan.artifact_type != "sprint_plan"
        or plan.status not in {"accepted", "superseded"}
        or plan.activated_sprint_id != sprint_id
        or plan.spec_version_id != start.spec_version_id
        or plan.spec_hash != start.spec_hash
        or plan.source_fingerprint != plan.candidate_set_fingerprint
        or decision.artifact_type != "sprint"
        or decision.artifact_id != plan.artifact_id
        or decision.artifact_fingerprint != plan.artifact_fingerprint
        or decision.decision != "accepted"
        or start.plan_fingerprint != plan.artifact_fingerprint
        or start.candidate_set_fingerprint != plan.candidate_set_fingerprint
        or start.selected_story_ids != plan.selected_story_ids
        or start.task_content_fingerprint != plan.task_content_fingerprint
        or not start.decision_fingerprint
        or start.audit_event_id <= 0
        or not start.audit_event_fingerprint
    ):
        _fail("Sprint start does not match the accepted Sprint plan.")
    return start, plan, decision, dependency_review


def _contract_stories(
    snapshot: WorkflowFactSnapshot,
    start: SprintStartFact,
    dependency_review: StoryDependencyReviewFact,
) -> tuple[StoryFact, ...]:
    selected_story_ids = start.selected_story_ids
    if not selected_story_ids or len(selected_story_ids) != len(
        set(selected_story_ids)
    ):
        _fail("Sprint start selected Story IDs are not canonical.")
    attached_by_id = {
        item.story_id: item
        for item in snapshot.stories
        if start.sprint_id in item.sprint_ids
    }
    if set(attached_by_id) != set(selected_story_ids):
        _fail("Sprint Story membership changed after plan acceptance.")
    attached = tuple(attached_by_id[story_id] for story_id in selected_story_ids)
    if dependency_review.selected_story_ids != selected_story_ids:
        _fail("Sprint dependency review Story set is stale.")
    if any(
        not item.content_accepted
        or item.content_fingerprint is None
        or item.story_artifact_id is None
        for item in attached
    ):
        _fail("Sprint execution requires accepted Story content.")
    return attached


def _contract_dependencies(
    snapshot: WorkflowFactSnapshot,
    start: SprintStartFact,
    dependency_review: StoryDependencyReviewFact,
) -> tuple[StoryDependencyFact, ...]:
    selected = selected_story_dependency_snapshot(
        snapshot,
        start.selected_story_ids,
    )
    if (
        dependency_review.selected_story_ids != selected.story_ids
        or dependency_review.source_fingerprint != selected.source_fingerprint
        or dependency_review.reviewed_edges != selected.reviewed_edges
        or dependency_review.dependency_fingerprint != selected.dependency_fingerprint
        or start.dependency_source_fingerprint != selected.source_fingerprint
        or start.dependency_fingerprint != selected.dependency_fingerprint
        or start.dependency_rows_fingerprint != selected.rows_fingerprint
    ):
        _fail("Sprint dependency review or dependency rows changed.")
    return selected.dependencies


def _contract_tasks(
    snapshot: WorkflowFactSnapshot,
    start: SprintStartFact,
) -> tuple[TaskFact, ...]:
    tasks = tuple(
        sorted(
            (item for item in snapshot.tasks if item.sprint_id == start.sprint_id),
            key=lambda item: (item.story_id, item.task_id),
        )
    )
    if (
        len({item.task_id for item in tasks}) != len(tasks)
        or any(item.story_id not in set(start.selected_story_ids) for item in tasks)
        or execution_task_content_fingerprint(
            tasks,
            sprint_id=start.sprint_id,
            story_ids=start.selected_story_ids,
        )
        != start.task_content_fingerprint
    ):
        _fail("Sprint Tasks no longer match the canonical accepted plan.")
    return tasks


def execution_contract(
    snapshot: WorkflowFactSnapshot,
    sprint_id: int,
) -> ExecutionContract:
    """Require one exact accepted-plan, dependency, Task, and start lineage."""
    start, plan, decision, dependency_review = _start_contract_facts(
        snapshot,
        sprint_id,
    )
    stories = _contract_stories(snapshot, start, dependency_review)
    dependencies = _contract_dependencies(
        snapshot,
        start,
        dependency_review,
    )
    tasks = _contract_tasks(snapshot, start)
    facts = _ExecutionContractFacts(
        start=start,
        plan=plan,
        decision=decision,
        dependency_review=dependency_review,
        stories=stories,
        tasks=tasks,
        dependencies=dependencies,
    )
    return ExecutionContract(
        sprint_id=sprint_id,
        start=start,
        plan=plan,
        decision=decision,
        dependency_review=dependency_review,
        stories=stories,
        tasks=tasks,
        dependencies=dependencies,
        fingerprint=_contract_fingerprint(facts),
    )


def task_evidence_fingerprint(
    snapshot: WorkflowFactSnapshot,
    task: TaskFact,
    *,
    evidence: TaskEvidencePayload,
) -> str:
    """Bind completion evidence to the complete normalized execution contract."""
    contract = execution_contract(snapshot, task.sprint_id)
    current = next(
        (item for item in contract.tasks if item.task_id == task.task_id),
        None,
    )
    if (
        current is None
        or current.sprint_id != task.sprint_id
        or current.story_id != task.story_id
        or current.description != task.description
        or current.metadata_json != task.metadata_json
        or task.status != TaskStatus.DONE.value
        or not task.dependencies_satisfied
    ):
        _fail("Task completion does not match an eligible contract Task.")
    return canonical_hash(
        {
            "execution_contract_fingerprint": contract.fingerprint,
            "task": task.model_dump(mode="json"),
            "outcome_summary": evidence.outcome_summary,
            "artifact_refs": evidence.artifact_refs,
            "acceptance_result": evidence.acceptance_result,
            "checklist_result": evidence.checklist_result,
        }
    )


def story_completion_eligibility_fingerprint(
    snapshot: WorkflowFactSnapshot,
    *,
    sprint_id: int,
    story_id: int,
) -> str:
    """Bind Story close eligibility to accepted content, Tasks, and dependencies."""
    contract = execution_contract(snapshot, sprint_id)
    story = next((item for item in contract.stories if item.story_id == story_id), None)
    if story is None or sprint_id not in story.sprint_ids:
        _fail("Story closure does not target an attached contract Story.")
    tasks = tuple(item for item in contract.tasks if item.story_id == story_id)
    task_ids = {item.task_id for item in tasks}
    completions = tuple(
        sorted(
            (
                item
                for item in snapshot.task_completions
                if item.sprint_id == sprint_id and item.task_id in task_ids
            ),
            key=lambda item: item.task_id,
        )
    )
    return canonical_hash(
        {
            "execution_contract_fingerprint": contract.fingerprint,
            "story": _accepted_story_payload(story),
            "tasks": [item.model_dump(mode="json") for item in tasks],
            "task_completions": [item.model_dump(mode="json") for item in completions],
        }
    )


def story_completion_fingerprint(
    snapshot: WorkflowFactSnapshot,
    *,
    sprint_id: int,
    story_id: int,
    closure: StoryClosurePayload,
) -> str:
    """Bind one persisted Story closure to eligibility and exact close evidence."""
    return canonical_hash(
        {
            "eligibility_fingerprint": story_completion_eligibility_fingerprint(
                snapshot,
                sprint_id=sprint_id,
                story_id=story_id,
            ),
            "resolution": closure.resolution,
            "delivered": closure.delivered,
            "evidence": closure.evidence,
            "known_gaps": closure.known_gaps,
        }
    )


def sprint_review_fingerprint(
    snapshot: WorkflowFactSnapshot,
    sprint_id: int,
) -> str:
    """Bind Sprint review to current terminal Story, Task, and closure facts."""
    contract = execution_contract(snapshot, sprint_id)
    story_ids = {item.story_id for item in contract.stories}
    return canonical_hash(
        {
            "execution_contract_fingerprint": contract.fingerprint,
            "sprint_id": sprint_id,
            "stories": [
                item.model_dump(mode="json")
                for item in sorted(
                    (item for item in snapshot.stories if item.story_id in story_ids),
                    key=lambda item: item.story_id,
                )
            ],
            "tasks": [item.model_dump(mode="json") for item in contract.tasks],
            "task_completions": [
                item.model_dump(mode="json")
                for item in sorted(
                    (
                        item
                        for item in snapshot.task_completions
                        if item.sprint_id == sprint_id
                    ),
                    key=lambda item: item.task_id,
                )
            ],
            "story_completions": [
                item.model_dump(mode="json")
                for item in sorted(
                    (
                        item
                        for item in snapshot.story_completions
                        if item.sprint_id == sprint_id
                    ),
                    key=lambda item: item.story_id,
                )
            ],
        }
    )


def sprint_close_fingerprint(
    snapshot: WorkflowFactSnapshot,
    sprint_id: int,
    review_fingerprint: str,
) -> str:
    """Bind Sprint closure to its current review and exact terminal Story set."""
    contract = execution_contract(snapshot, sprint_id)
    closures = tuple(
        sorted(
            (
                item
                for item in snapshot.story_completions
                if item.sprint_id == sprint_id
            ),
            key=lambda item: item.story_id,
        )
    )
    return canonical_hash(
        {
            "execution_contract_fingerprint": contract.fingerprint,
            "sprint_id": sprint_id,
            "review_fingerprint": review_fingerprint,
            "terminal_stories": [
                item.model_dump(mode="json") for item in contract.stories
            ],
            "story_completions": [
                {
                    "completion_id": item.completion_id,
                    "story_id": item.story_id,
                    "completion_fingerprint": item.completion_fingerprint,
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
    "ExecutionContract",
    "ExecutionIntegrityError",
    "SelectedStoryDependencySnapshot",
    "SprintStartAudit",
    "StoryClosurePayload",
    "TaskEvidencePayload",
    "accepted_story_source_fingerprint",
    "current_dependency_review",
    "dependency_rows_fingerprint",
    "execution_contract",
    "execution_task_content_fingerprint",
    "selected_story_dependency_snapshot",
    "sprint_close_fingerprint",
    "sprint_review_fingerprint",
    "sprint_start_audit_metadata",
    "story_completion_eligibility_fingerprint",
    "story_completion_fingerprint",
    "task_evidence_fingerprint",
    "triage_payload_fingerprint",
]
