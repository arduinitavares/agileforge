"""Agent workbench Sprint phase command runner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from models.core import Sprint, SprintStory, Task, Team, UserStory
from models.enums import (
    SprintStatus,
    WorkflowEventType,
)
from models.events import WorkflowEvent
from models.workflow import (
    SprintClosure,
    SprintPlanArtifact,
    SprintPlanArtifactDecision,
    SprintReview,
    SprintStart,
    StoryDependencyReview,
)
from repositories.workflow import WorkflowFactRepository
from services.agent_workbench.fingerprints import canonical_hash
from services.contracts.sprint import (
    SprintPlannerOutput,
    validate_task_decomposition_quality,
    validate_task_invariant_bindings,
)
from services.story_dependencies import (
    assert_dependency_graph_valid_for_sprint,
)
from utils.spec_schemas import ValidationEvidence
from utils.task_metadata import (
    metadata_from_structured_task,
    serialize_task_metadata,
)
from workflow.execution_integrity import (
    SelectedStoryDependencySnapshot,
    SprintStartAudit,
    sprint_close_fingerprint,
    sprint_review_fingerprint,
    sprint_start_audit_metadata,
)
from workflow.fingerprints import canonical_json

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.contracts import JsonObject


def _planning_story_invariant_ids(story: UserStory) -> list[str]:
    if not story.validation_evidence:
        return []
    try:
        evidence = ValidationEvidence.model_validate_json(story.validation_evidence)
    except (ValueError, TypeError):
        return []
    return list(evidence.evaluated_invariant_ids or [])


@dataclass(frozen=True)
class RecordSprintPlanInput:
    """Exact immutable values used to record one Sprint plan."""

    project_id: int
    team_name: str
    selected_story_ids: tuple[int, ...]
    canonical_task_plan: JsonObject
    plan_fingerprint: str
    candidate_set_fingerprint: str
    supersedes_sprint_plan_artifact_id: int | None
    actor: str
    recorded_at: datetime


@dataclass(frozen=True)
class RecordSprintPlanDecisionInput:
    """Exact append-only values used to decide one Sprint plan."""

    artifact: SprintPlanArtifact
    decision: str
    rationale: str
    reviewer: str
    idempotency_key: str
    decided_at: datetime


def _validated_sprint_plan(inputs: RecordSprintPlanInput) -> SprintPlannerOutput:
    plan = SprintPlannerOutput.model_validate(inputs.canonical_task_plan)
    if canonical_hash(inputs.canonical_task_plan) != inputs.plan_fingerprint:
        message = "Sprint plan fingerprint does not match canonical content."
        raise ValueError(message)
    plan_story_ids = tuple(sorted(item.story_id for item in plan.selected_stories))
    if plan_story_ids != inputs.selected_story_ids:
        message = "Sprint plan selected Story IDs do not match the request."
        raise ValueError(message)
    return plan


def _sprint_plan_artifact_history(
    session: Session,
    inputs: RecordSprintPlanInput,
) -> tuple[SprintPlanArtifact, ...]:
    artifacts = tuple(
        session.exec(
            select(SprintPlanArtifact)
            .where(SprintPlanArtifact.project_id == inputs.project_id)
            .order_by(col(SprintPlanArtifact.version_number))
        ).all()
    )
    expected_parent = artifacts[-1].sprint_plan_artifact_id if artifacts else None
    if inputs.supersedes_sprint_plan_artifact_id != expected_parent:
        message = "Sprint plan supersession does not match the current artifact."
        raise ValueError(message)
    return artifacts


def _selected_sprint_stories(
    session: Session,
    inputs: RecordSprintPlanInput,
) -> dict[int, UserStory]:
    stories = session.exec(
        select(UserStory).where(
            col(UserStory.story_id).in_(inputs.selected_story_ids)
        )
    ).all()
    stories_by_id = {
        story.story_id: story for story in stories if story.story_id is not None
    }
    if set(stories_by_id) != set(inputs.selected_story_ids) or any(
        story.project_id != inputs.project_id
        or story.is_superseded
        or not story.is_refined
        for story in stories_by_id.values()
    ):
        message = "Sprint plan does not target exact active Project stories."
        raise ValueError(message)
    assert_dependency_graph_valid_for_sprint(session, project_id=inputs.project_id)
    return stories_by_id


def _ensure_sprint_team(session: Session, inputs: RecordSprintPlanInput) -> int:
    team = session.exec(select(Team).where(Team.name == inputs.team_name)).first()
    if team is None:
        team = Team(
            name=inputs.team_name,
            created_at=inputs.recorded_at,
            updated_at=inputs.recorded_at,
        )
        session.add(team)
        session.flush()
    team_id = team.team_id
    if team_id is None:
        message = "Sprint Team did not receive a durable identity."
        raise ValueError(message)
    return team_id


def _superseded_sprint(
    session: Session,
    inputs: RecordSprintPlanInput,
) -> tuple[Sprint | None, int | None]:
    parent_id = inputs.supersedes_sprint_plan_artifact_id
    if parent_id is None:
        return None, None
    parent = session.get(SprintPlanArtifact, parent_id)
    if parent is None or parent.project_id != inputs.project_id:
        message = "Superseded Sprint plan does not exist."
        raise ValueError(message)
    return session.get(Sprint, parent.sprint_id), parent.sprint_id


def _assert_no_open_story_conflicts(
    session: Session,
    selected_story_ids: tuple[int, ...],
    ignored_sprint_id: int | None,
) -> None:
    conflicts = session.exec(
        select(SprintStory.story_id)
        .join(Sprint, col(Sprint.sprint_id) == col(SprintStory.sprint_id))
        .where(
            col(SprintStory.story_id).in_(selected_story_ids),
            col(Sprint.status).in_([SprintStatus.PLANNED, SprintStatus.ACTIVE]),
            *(
                (Sprint.sprint_id != ignored_sprint_id,)
                if ignored_sprint_id is not None
                else ()
            ),
        )
    ).all()
    if conflicts:
        message = f"Stories already assigned to open Sprints: {sorted(set(conflicts))}."
        raise ValueError(message)


def _ensure_planned_sprint(
    session: Session,
    inputs: RecordSprintPlanInput,
    plan: SprintPlannerOutput,
    team_id: int,
    sprint: Sprint | None,
) -> Sprint:
    if sprint is None:
        sprint = Sprint(
            goal=plan.sprint_goal,
            status=SprintStatus.PLANNED,
            project_id=inputs.project_id,
            team_id=team_id,
            created_at=inputs.recorded_at,
            updated_at=inputs.recorded_at,
        )
        session.add(sprint)
        session.flush()
    else:
        if sprint.status is not SprintStatus.PLANNED:
            message = "Only a planned Sprint can receive a superseding plan."
            raise ValueError(message)
        sprint.goal = plan.sprint_goal
        sprint.team_id = team_id
        sprint.updated_at = inputs.recorded_at
        session.add(sprint)
        for link in session.exec(
            select(SprintStory).where(SprintStory.sprint_id == sprint.sprint_id)
        ).all():
            session.delete(link)
        session.flush()
    if sprint.sprint_id is None:
        message = "Sprint did not receive a durable identity."
        raise ValueError(message)
    return sprint


def _validate_sprint_task_plan(
    plan: SprintPlannerOutput,
    stories_by_id: dict[int, UserStory],
) -> None:
    allowed_invariants = {
        story_id: _planning_story_invariant_ids(story)
        for story_id, story in stories_by_id.items()
    }
    binding_errors = validate_task_invariant_bindings(
        plan,
        allowed_invariant_ids_by_story=allowed_invariants,
    )
    if binding_errors:
        raise ValueError(
            "Sprint plan invariant binding failed: " + "; ".join(binding_errors)
        )
    acceptance_items: dict[int, list[str]] = {
        story_id: [
            line.lstrip("-* \t").strip()
            for line in (story.acceptance_criteria or "").splitlines()
            if line.lstrip("-* \t").strip()
        ]
        for story_id, story in stories_by_id.items()
    }
    decomposition_errors = validate_task_decomposition_quality(
        plan,
        include_task_decomposition=True,
        has_acceptance_criteria_by_story={
            story_id: bool(items) for story_id, items in acceptance_items.items()
        },
        acceptance_criteria_items_by_story=acceptance_items,
    )
    if decomposition_errors:
        raise ValueError(
            "Sprint task decomposition failed: " + "; ".join(decomposition_errors)
        )


def _replace_sprint_story_tasks(
    session: Session,
    sprint_id: int,
    plan: SprintPlannerOutput,
    recorded_at: datetime,
) -> None:
    for selected in plan.selected_stories:
        session.add(
            SprintStory(
                sprint_id=sprint_id,
                story_id=selected.story_id,
                added_at=recorded_at,
            )
        )
        for task in session.exec(
            select(Task).where(Task.story_id == selected.story_id)
        ).all():
            session.delete(task)
        for task_spec in selected.tasks:
            session.add(
                Task(
                    story_id=selected.story_id,
                    description=task_spec.description,
                    metadata_json=serialize_task_metadata(
                        metadata_from_structured_task(task_spec)
                    ),
                    created_at=recorded_at,
                    updated_at=recorded_at,
                )
            )


def _add_sprint_plan_artifact(
    session: Session,
    inputs: RecordSprintPlanInput,
    sprint_id: int,
    artifact_count: int,
) -> SprintPlanArtifact:
    row = SprintPlanArtifact(
        project_id=inputs.project_id,
        sprint_id=sprint_id,
        version_number=artifact_count + 1,
        selected_story_ids_json=canonical_json(list(inputs.selected_story_ids)),
        canonical_task_plan_json=canonical_json(inputs.canonical_task_plan),
        plan_fingerprint=inputs.plan_fingerprint,
        candidate_set_fingerprint=inputs.candidate_set_fingerprint,
        supersedes_sprint_plan_artifact_id=(
            inputs.supersedes_sprint_plan_artifact_id
        ),
        created_by=inputs.actor,
        created_at=inputs.recorded_at,
    )
    session.add(row)
    session.add(
        WorkflowEvent(
            event_type=WorkflowEventType.SPRINT_PLAN_SAVED,
            timestamp=inputs.recorded_at,
            project_id=inputs.project_id,
            sprint_id=sprint_id,
            event_metadata=canonical_json(
                {
                    "action": "sprint_plan_recorded",
                    "candidate_set_fingerprint": inputs.candidate_set_fingerprint,
                    "plan_fingerprint": inputs.plan_fingerprint,
                    "selected_story_ids": list(inputs.selected_story_ids),
                }
            ),
            duration_seconds=0.0,
        )
    )
    session.flush()
    return row


def record_sprint_plan_in_session(
    session: Session,
    *,
    inputs: RecordSprintPlanInput,
) -> SprintPlanArtifact:
    """Persist exact Sprint, task, and immutable plan facts in one transaction."""
    plan = _validated_sprint_plan(inputs)
    artifacts = _sprint_plan_artifact_history(session, inputs)
    stories_by_id = _selected_sprint_stories(session, inputs)
    team_id = _ensure_sprint_team(session, inputs)
    sprint, ignored_sprint_id = _superseded_sprint(session, inputs)
    _assert_no_open_story_conflicts(
        session,
        inputs.selected_story_ids,
        ignored_sprint_id,
    )
    sprint = _ensure_planned_sprint(session, inputs, plan, team_id, sprint)
    sprint_id = sprint.sprint_id
    if sprint_id is None:
        message = "Sprint did not receive a durable identity."
        raise ValueError(message)
    _validate_sprint_task_plan(plan, stories_by_id)
    _replace_sprint_story_tasks(
        session,
        sprint_id,
        plan,
        inputs.recorded_at,
    )
    return _add_sprint_plan_artifact(
        session,
        inputs,
        sprint_id,
        len(artifacts),
    )


def record_sprint_plan_decision_in_session(
    session: Session,
    *,
    inputs: RecordSprintPlanDecisionInput,
) -> SprintPlanArtifactDecision:
    """Append one terminal decision for an exact immutable Sprint plan."""
    artifact = inputs.artifact
    if inputs.decision not in {"accepted", "rejected", "feedback"}:
        message = "Sprint plan decision is invalid."
        raise ValueError(message)
    artifact_id = artifact.sprint_plan_artifact_id
    if artifact_id is None:
        message = "Sprint plan artifact has no durable identity."
        raise ValueError(message)
    existing = session.exec(
        select(SprintPlanArtifactDecision).where(
            SprintPlanArtifactDecision.project_id == artifact.project_id,
            SprintPlanArtifactDecision.sprint_plan_artifact_id == artifact_id,
        )
    ).first()
    if existing is not None:
        message = "Sprint plan already has a terminal decision."
        raise ValueError(message)
    row = SprintPlanArtifactDecision(
        project_id=artifact.project_id,
        sprint_plan_artifact_id=artifact_id,
        plan_fingerprint=artifact.plan_fingerprint,
        decision=inputs.decision,
        rationale=inputs.rationale,
        reviewer=inputs.reviewer,
        idempotency_key=inputs.idempotency_key,
        decided_at=inputs.decided_at,
    )
    session.add(row)
    session.flush()
    return row


@dataclass(frozen=True)
class SprintStartInput:
    """Exact accepted planning and audit facts for one Sprint start."""

    project_id: int
    sprint_id: int
    sprint_plan_artifact_id: int
    sprint_plan_artifact_decision_id: int
    plan_fingerprint: str
    candidate_set_fingerprint: str
    selected_story_ids: tuple[int, ...]
    task_content_fingerprint: str
    dependency_snapshot: SelectedStoryDependencySnapshot
    decision_fingerprint: str
    started_by: str
    started_at: datetime


def _selected_dependency_review_id(
    session: Session,
    command: SprintStartInput,
) -> int:
    dependency = command.dependency_snapshot
    if dependency.story_ids != command.selected_story_ids:
        message = "Sprint dependency scope does not match selected Stories."
        raise ValueError(message)
    selected_json = canonical_json(list(dependency.story_ids))
    edges_json = canonical_json(
        [item.model_dump(mode="json") for item in dependency.reviewed_edges]
    )
    existing = session.exec(
        select(StoryDependencyReview).where(
            StoryDependencyReview.project_id == command.project_id,
            StoryDependencyReview.source_fingerprint
            == dependency.source_fingerprint,
        )
    ).one_or_none()
    if existing is None:
        existing = StoryDependencyReview(
            project_id=command.project_id,
            selected_story_ids_json=selected_json,
            reviewed_edges_json=edges_json,
            source_fingerprint=dependency.source_fingerprint,
            dependency_fingerprint=dependency.dependency_fingerprint,
            reviewed_by=command.started_by,
            reviewed_at=command.started_at,
        )
        session.add(existing)
        session.flush()
    elif (
        existing.selected_story_ids_json != selected_json
        or existing.reviewed_edges_json != edges_json
        or existing.dependency_fingerprint != dependency.dependency_fingerprint
    ):
        message = "Sprint dependency review conflicts with selected facts."
        raise ValueError(message)
    review_id = existing.story_dependency_review_id
    if review_id is None:
        message = "Sprint dependency review has no durable identity."
        raise ValueError(message)
    return review_id


def start_sprint_in_session(
    session: Session,
    command: SprintStartInput,
) -> Sprint:
    """Start one planned Sprint without consulting routing state."""
    sprint = session.get(Sprint, command.sprint_id)
    plan = session.get(SprintPlanArtifact, command.sprint_plan_artifact_id)
    plan_decision = session.get(
        SprintPlanArtifactDecision,
        command.sprint_plan_artifact_decision_id,
    )
    if sprint is None or sprint.project_id != command.project_id:
        message = "Sprint start does not target an exact Project Sprint."
        raise ValueError(message)
    if (
        plan is None
        or plan.project_id != command.project_id
        or plan.sprint_id != command.sprint_id
        or plan.plan_fingerprint != command.plan_fingerprint
        or plan.candidate_set_fingerprint != command.candidate_set_fingerprint
        or plan_decision is None
        or plan_decision.project_id != command.project_id
        or plan_decision.sprint_plan_artifact_id
        != command.sprint_plan_artifact_id
        or plan_decision.plan_fingerprint != command.plan_fingerprint
        or plan_decision.decision != "accepted"
    ):
        message = "Sprint start does not match an exact accepted Sprint plan."
        raise ValueError(message)
    if sprint.status is not SprintStatus.PLANNED or sprint.started_at is not None:
        message = "Only an unstarted planned Sprint can start."
        raise ValueError(message)
    other_active = session.exec(
        select(Sprint).where(
            Sprint.project_id == command.project_id,
            Sprint.status == SprintStatus.ACTIVE,
            Sprint.sprint_id != command.sprint_id,
        )
    ).first()
    if other_active is not None:
        message = "Another Sprint is already active for this Project."
        raise ValueError(message)
    existing = session.exec(
        select(SprintStart).where(SprintStart.sprint_id == command.sprint_id)
    ).one_or_none()
    if existing is not None:
        message = "Sprint start lineage is immutable."
        raise ValueError(message)
    dependency_review_id = _selected_dependency_review_id(session, command)
    dependency = command.dependency_snapshot
    metadata = sprint_start_audit_metadata(
        SprintStartAudit(
            sprint_id=command.sprint_id,
            team_id=sprint.team_id,
            sprint_plan_artifact_id=command.sprint_plan_artifact_id,
            sprint_plan_artifact_decision_id=(
                command.sprint_plan_artifact_decision_id
            ),
            story_dependency_review_id=dependency_review_id,
            plan_fingerprint=command.plan_fingerprint,
            candidate_set_fingerprint=command.candidate_set_fingerprint,
            selected_story_ids=command.selected_story_ids,
            task_content_fingerprint=command.task_content_fingerprint,
            dependency_source_fingerprint=dependency.source_fingerprint,
            dependency_fingerprint=dependency.dependency_fingerprint,
            dependency_rows_fingerprint=dependency.rows_fingerprint,
            decision_fingerprint=command.decision_fingerprint,
            started_by=command.started_by,
        )
    )
    event = WorkflowEvent(
        event_type=WorkflowEventType.SPRINT_STARTED,
        timestamp=command.started_at,
        project_id=command.project_id,
        sprint_id=command.sprint_id,
        event_metadata=canonical_json(metadata),
        duration_seconds=0.0,
    )
    session.add(event)
    session.flush()
    if event.event_id is None:
        message = "Sprint start audit event has no durable identity."
        raise ValueError(message)
    sprint.status = SprintStatus.ACTIVE
    sprint.started_at = command.started_at
    sprint.updated_at = command.started_at
    session.add(sprint)
    session.add(
        SprintStart(
            project_id=command.project_id,
            sprint_id=command.sprint_id,
            sprint_plan_artifact_id=command.sprint_plan_artifact_id,
            sprint_plan_artifact_decision_id=(
                command.sprint_plan_artifact_decision_id
            ),
            story_dependency_review_id=dependency_review_id,
            plan_fingerprint=command.plan_fingerprint,
            candidate_set_fingerprint=command.candidate_set_fingerprint,
            selected_story_ids_json=canonical_json(list(command.selected_story_ids)),
            task_content_fingerprint=command.task_content_fingerprint,
            dependency_source_fingerprint=dependency.source_fingerprint,
            dependency_fingerprint=dependency.dependency_fingerprint,
            dependency_rows_fingerprint=dependency.rows_fingerprint,
            decision_fingerprint=command.decision_fingerprint,
            audit_event_id=event.event_id,
            started_by=command.started_by,
            started_at=command.started_at,
        )
    )
    session.flush()
    return sprint


@dataclass(frozen=True)
class SprintReviewInput:
    """Caller-owned inputs for a persisted Sprint review."""

    project_id: int
    sprint_id: int
    review_fingerprint: str
    reviewed_by: str
    reviewed_at: datetime


@dataclass(frozen=True)
class SprintCloseInput:
    """Caller-owned inputs for a persisted Sprint closure."""

    project_id: int
    sprint_id: int
    review_fingerprint: str
    close_fingerprint: str
    closed_by: str
    closed_at: datetime


def review_sprint_in_session(
    session: Session,
    command: SprintReviewInput,
) -> SprintReview:
    """Persist one exact Sprint review in the caller's transaction."""
    snapshot = WorkflowFactRepository(session).load(command.project_id)
    sprint = next(
        (item for item in snapshot.sprints if item.sprint_id == command.sprint_id),
        None,
    )
    if sprint is None or sprint.status != "active":
        message = "Sprint review requires the exact active Project Sprint."
        raise ValueError(message)
    attached = tuple(
        item for item in snapshot.stories if command.sprint_id in item.sprint_ids
    )
    closure_ids = {
        item.story_id
        for item in snapshot.story_completions
        if item.sprint_id == command.sprint_id
    }
    if (
        not attached
        or any(item.status not in {"Done", "Accepted"} for item in attached)
        or closure_ids != {item.story_id for item in attached}
    ):
        message = "Sprint review requires every attached Story terminal."
        raise ValueError(message)
    expected = sprint_review_fingerprint(snapshot, command.sprint_id)
    if command.review_fingerprint != expected:
        message = "Sprint review fingerprint is stale."
        raise ValueError(message)
    existing = session.exec(
        select(SprintReview).where(col(SprintReview.sprint_id) == command.sprint_id)
    ).one_or_none()
    if existing is not None:
        message = "Sprint review is immutable."
        raise ValueError(message)
    row = SprintReview(
        project_id=command.project_id,
        sprint_id=command.sprint_id,
        review_fingerprint=command.review_fingerprint,
        reviewed_by=command.reviewed_by,
        reviewed_at=command.reviewed_at,
    )
    session.add(row)
    session.flush()
    return row


def close_sprint_in_session(
    session: Session,
    command: SprintCloseInput,
) -> SprintClosure:
    """Close one reviewed Sprint in the caller's transaction."""
    sprint = session.get(Sprint, command.sprint_id)
    if (
        sprint is None
        or sprint.project_id != command.project_id
        or sprint.status is not SprintStatus.ACTIVE
    ):
        message = "Sprint close requires the exact active Project Sprint."
        raise ValueError(message)
    review = session.exec(
        select(SprintReview).where(
            col(SprintReview.project_id) == command.project_id,
            col(SprintReview.sprint_id) == command.sprint_id,
        )
    ).one_or_none()
    if review is None or review.review_fingerprint != command.review_fingerprint:
        message = "Sprint close review fingerprint is stale or missing."
        raise ValueError(message)
    snapshot = WorkflowFactRepository(session).load(command.project_id)
    if (
        sprint_review_fingerprint(snapshot, command.sprint_id)
        != command.review_fingerprint
    ):
        message = "Sprint facts changed after review."
        raise ValueError(message)
    expected_close = sprint_close_fingerprint(
        snapshot,
        command.sprint_id,
        command.review_fingerprint,
    )
    if command.close_fingerprint != expected_close:
        message = "Sprint close fingerprint is stale."
        raise ValueError(message)
    existing = session.exec(
        select(SprintClosure).where(col(SprintClosure.sprint_id) == command.sprint_id)
    ).one_or_none()
    if existing is not None:
        message = "Sprint closure is immutable."
        raise ValueError(message)
    sprint.status = SprintStatus.COMPLETED
    sprint.completed_at = command.closed_at
    sprint.updated_at = command.closed_at
    sprint.close_snapshot_json = None
    closure = SprintClosure(
        project_id=command.project_id,
        sprint_id=command.sprint_id,
        review_fingerprint=command.review_fingerprint,
        close_fingerprint=command.close_fingerprint,
        closed_by=command.closed_by,
        closed_at=command.closed_at,
    )
    session.add(sprint)
    session.add(closure)
    session.add(
        WorkflowEvent(
            event_type=WorkflowEventType.SPRINT_COMPLETED,
            timestamp=command.closed_at,
            project_id=command.project_id,
            sprint_id=command.sprint_id,
            event_metadata=canonical_json(
                {
                    "action": "sprint_closed",
                    "review_fingerprint": command.review_fingerprint,
                    "close_fingerprint": command.close_fingerprint,
                }
            ),
            duration_seconds=0.0,
        )
    )
    session.flush()
    return closure
