"""Caller-transaction handlers for durable planning workflow facts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from models.workflow import (
    BacklogArtifact,
    BacklogArtifactDecision,
    RoadmapArtifact,
    RoadmapArtifactDecision,
    SprintPlanArtifact,
    SprintPlanArtifactDecision,
    StoryArtifact,
    StoryArtifactDecision,
    WorkflowTransitionReceipt,
)
from repositories.workflow import WorkflowFactRepository
from services.agent_workbench.roadmap_phase import (
    RecordRoadmapDecisionInput,
    RecordRoadmapDraftInput,
    record_roadmap_decision_in_session,
    record_roadmap_draft_in_session,
)
from services.agent_workbench.sprint_phase import (
    RecordSprintPlanDecisionInput,
    RecordSprintPlanInput,
    record_sprint_plan_decision_in_session,
    record_sprint_plan_in_session,
    start_sprint_in_session,
)
from services.agent_workbench.story_phase import (
    RecordStoryDecisionInput,
    RecordStoryDraftInput,
    record_story_decision_in_session,
    record_story_draft_in_session,
    repair_story_readiness_in_session,
)
from services.sprint_input import candidate_set_in_session
from services.story_dependencies import (
    ApplyStoryDependenciesInput,
    StoryDependencyGraphError,
    apply_story_dependencies_in_session,
)
from workflow.contracts import (
    NodeDecision,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
)
from workflow.definitions.planning import (
    candidate_set_fingerprint,
    readiness_fingerprint,
    story_dependency_source_fingerprint,
)
from workflow.planning_integrity import current_task_content_fingerprint
from workflow.requests.planning import (
    ApplyStoryDependencies,
    DecideRoadmap,
    DecideSprintPlan,
    DecideStory,
    RecordRoadmapDraft,
    RecordSprintPlan,
    RecordStoryDraft,
    RepairStoryReadiness,
    StartSprint,
)

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.facts import PlanningArtifactFact, WorkflowFactSnapshot


type PlanningRequest = (
    RecordRoadmapDraft
    | DecideRoadmap
    | RecordStoryDraft
    | DecideStory
    | ApplyStoryDependencies
    | RepairStoryReadiness
    | RecordSprintPlan
    | DecideSprintPlan
    | StartSprint
)
type PlanningReviewRequest = DecideRoadmap | DecideStory | DecideSprintPlan
type RoadmapPlanningRequest = RecordRoadmapDraft | DecideRoadmap
type StoryPlanningRequest = (
    RecordStoryDraft | DecideStory | ApplyStoryDependencies | RepairStoryReadiness
)
type SprintPlanningRequest = RecordSprintPlan | DecideSprintPlan | StartSprint


def _success(decision: NodeDecision, output: dict[str, object]) -> TransitionResult:
    return TransitionResult(ok=True, applied_node_id=decision.node_id, output=output)


def _conflict(message: str) -> TransitionResult:
    return TransitionResult(
        ok=False,
        error=WorkflowError(
            code=WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
            message=message,
        ),
    )


def _matches_reference(
    decision: NodeDecision,
    *,
    fact_type: str,
    fact_id: int | str,
    fingerprint: str,
) -> bool:
    return any(
        item.fact_type == fact_type
        and item.fact_id == str(fact_id)
        and item.fingerprint == fingerprint
        for item in decision.fact_references
    )


def _expected_parent(decision: NodeDecision, fact_type: str) -> int | None:
    ids = [
        int(item.fact_id)
        for item in decision.fact_references
        if item.fact_type == fact_type and item.fact_id.isdigit()
    ]
    return max(ids) if ids else None


def _current_sprint_plan(
    snapshot: WorkflowFactSnapshot,
    artifact_id: int,
) -> tuple[PlanningArtifactFact, str, str] | None:
    plan = next(
        (
            item
            for item in snapshot.planning_artifacts
            if item.artifact_type == "sprint_plan"
            and item.artifact_id == artifact_id
        ),
        None,
    )
    if plan is None or plan.sprint_id is None:
        return None
    stories = tuple(
        sorted(
            (item for item in snapshot.stories if item.sprint_candidate),
            key=lambda item: item.story_id,
        )
    )
    candidates = candidate_set_fingerprint(stories, snapshot.story_dependencies)
    tasks = current_task_content_fingerprint(
        snapshot.tasks,
        sprint_id=plan.sprint_id,
        story_ids=plan.story_ids,
    )
    if (
        plan.candidate_set_fingerprint != candidates
        or plan.task_content_fingerprint != tasks
    ):
        return None
    return plan, candidates, tasks


def _review_request_matches(
    persisted: PlanningReviewRequest,
    request: PlanningReviewRequest,
) -> bool:
    common = (
        persisted.project_id == request.project_id
        and persisted.graph_version == request.graph_version
        and persisted.fact_fingerprint == request.fact_fingerprint
        and persisted.decision_fingerprint == request.decision_fingerprint
        and persisted.instance_key == request.instance_key
        and persisted.attempt_id == request.attempt_id
        and persisted.attempt_fingerprint == request.attempt_fingerprint
    )
    if isinstance(persisted, DecideRoadmap) and isinstance(request, DecideRoadmap):
        return (
            common
            and persisted.roadmap_artifact_id == request.roadmap_artifact_id
            and persisted.artifact_fingerprint == request.artifact_fingerprint
        )
    if isinstance(persisted, DecideStory) and isinstance(request, DecideStory):
        return (
            common
            and persisted.requirement_id == request.requirement_id
            and persisted.story_artifact_id == request.story_artifact_id
            and persisted.artifact_fingerprint == request.artifact_fingerprint
        )
    if isinstance(persisted, DecideSprintPlan) and isinstance(
        request,
        DecideSprintPlan,
    ):
        return (
            common
            and persisted.sprint_plan_artifact_id == request.sprint_plan_artifact_id
            and persisted.plan_fingerprint == request.plan_fingerprint
        )
    return False


def validate_planning_review(
    session: Session,
    request: PlanningReviewRequest,
) -> TransitionResult | None:
    """Fail exact stale review guards after any terminal planning decision."""
    if isinstance(request, DecideRoadmap):
        existing = session.exec(
            select(RoadmapArtifactDecision).where(
                col(RoadmapArtifactDecision.project_id) == request.project_id,
                col(RoadmapArtifactDecision.roadmap_artifact_id)
                == request.roadmap_artifact_id,
            )
        ).one_or_none()
    elif isinstance(request, DecideStory):
        existing = session.exec(
            select(StoryArtifactDecision).where(
                col(StoryArtifactDecision.project_id) == request.project_id,
                col(StoryArtifactDecision.story_artifact_id)
                == request.story_artifact_id,
            )
        ).one_or_none()
    else:
        existing = session.exec(
            select(SprintPlanArtifactDecision).where(
                col(SprintPlanArtifactDecision.project_id) == request.project_id,
                col(SprintPlanArtifactDecision.sprint_plan_artifact_id)
                == request.sprint_plan_artifact_id,
            )
        ).one_or_none()
    if existing is None:
        return None
    receipts = session.exec(
        select(WorkflowTransitionReceipt).where(
            col(WorkflowTransitionReceipt.request_kind) == request.kind
        )
    ).all()
    request_type = type(request)
    for receipt in receipts:
        persisted = request_type.model_validate_json(receipt.request_json)
        if _review_request_matches(persisted, request):
            return _conflict("Planning artifact already has a terminal decision.")
    return None


def execute_record_roadmap_draft(
    session: Session,
    request: RecordRoadmapDraft,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    backlog = session.get(BacklogArtifact, request.backlog_artifact_id)
    accepted = session.exec(
        select(BacklogArtifactDecision).where(
            col(BacklogArtifactDecision.project_id) == request.project_id,
            col(BacklogArtifactDecision.backlog_artifact_id)
            == request.backlog_artifact_id,
            col(BacklogArtifactDecision.decision) == "accepted",
        )
    ).one_or_none()
    if (
        backlog is None
        or backlog.project_id != request.project_id
        or accepted is None
        or backlog.content_fingerprint != request.backlog_artifact_fingerprint
        or not _matches_reference(
            decision,
            fact_type="backlog",
            fact_id=request.backlog_artifact_id,
            fingerprint=request.backlog_artifact_fingerprint,
        )
        or request.supersedes_roadmap_artifact_id
        != _expected_parent(decision, "roadmap")
    ):
        return _conflict("RecordRoadmapDraft does not target exact graph facts.")
    try:
        row = record_roadmap_draft_in_session(
            session,
            inputs=RecordRoadmapDraftInput(
                project_id=request.project_id,
                backlog_artifact_id=request.backlog_artifact_id,
                backlog_artifact_fingerprint=request.backlog_artifact_fingerprint,
                canonical_content=request.canonical_content,
                content_fingerprint=request.content_fingerprint,
                supersedes_roadmap_artifact_id=(
                    request.supersedes_roadmap_artifact_id
                ),
                actor=request.actor,
                recorded_at=evaluated_at,
            ),
        )
    except ValueError as error:
        return _conflict(str(error))
    if row.roadmap_artifact_id is None:
        return _conflict("Roadmap artifact did not receive a durable identity.")
    return _success(
        decision,
        {
            "roadmap_artifact_id": row.roadmap_artifact_id,
            "content_fingerprint": row.content_fingerprint,
        },
    )


def execute_decide_roadmap(
    session: Session,
    request: DecideRoadmap,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    artifact = session.get(RoadmapArtifact, request.roadmap_artifact_id)
    if (
        artifact is None
        or artifact.project_id != request.project_id
        or artifact.content_fingerprint != request.artifact_fingerprint
        or not _matches_reference(
            decision,
            fact_type="roadmap",
            fact_id=request.roadmap_artifact_id,
            fingerprint=request.artifact_fingerprint,
        )
    ):
        return _conflict("DecideRoadmap does not target the waiting artifact.")
    try:
        row = record_roadmap_decision_in_session(
            session,
            inputs=RecordRoadmapDecisionInput(
                artifact=artifact,
                decision=request.decision,
                rationale=request.rationale,
                reviewer=request.actor,
                idempotency_key=request.idempotency_key,
                decided_at=evaluated_at,
            ),
        )
    except ValueError as error:
        return _conflict(str(error))
    return _success(
        decision,
        {
            "roadmap_artifact_decision_id": row.roadmap_artifact_decision_id,
            "roadmap_artifact_id": request.roadmap_artifact_id,
            "decision": request.decision,
        },
    )


def execute_record_story_draft(
    session: Session,
    request: RecordStoryDraft,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    snapshot = WorkflowFactRepository(session).load(request.project_id)
    requirement = next(
        (
            item
            for item in snapshot.backlog_requirements
            if item.requirement_id == request.requirement_id
            and _matches_reference(
                decision,
                fact_type="backlog_requirement",
                fact_id=item.requirement_id,
                fingerprint=item.backlog_artifact_fingerprint,
            )
        ),
        None,
    )
    roadmap = session.get(RoadmapArtifact, request.roadmap_artifact_id)
    if (
        requirement is None
        or roadmap is None
        or roadmap.project_id != request.project_id
        or roadmap.content_fingerprint != request.roadmap_artifact_fingerprint
        or not _matches_reference(
            decision,
            fact_type="roadmap",
            fact_id=request.roadmap_artifact_id,
            fingerprint=request.roadmap_artifact_fingerprint,
        )
        or request.supersedes_story_artifact_id != _expected_parent(decision, "story")
    ):
        return _conflict("RecordStoryDraft does not target exact graph facts.")
    try:
        row = record_story_draft_in_session(
            session,
            inputs=RecordStoryDraftInput(
                project_id=request.project_id,
                requirement_id=request.requirement_id,
                requirement_text=requirement.requirement,
                requirement_rank=requirement.rank,
                roadmap_artifact_id=request.roadmap_artifact_id,
                roadmap_artifact_fingerprint=(
                    request.roadmap_artifact_fingerprint
                ),
                canonical_content=request.canonical_content,
                content_fingerprint=request.content_fingerprint,
                supersedes_story_artifact_id=request.supersedes_story_artifact_id,
                actor=request.actor,
                recorded_at=evaluated_at,
            ),
        )
    except ValueError as error:
        return _conflict(str(error))
    if row.story_artifact_id is None:
        return _conflict("Story artifact did not receive a durable identity.")
    return _success(
        decision,
        {
            "story_artifact_id": row.story_artifact_id,
            "story_ids": tuple(json.loads(row.story_ids_json)),
            "content_fingerprint": row.content_fingerprint,
            "requirement_id": row.requirement_id,
        },
    )


def execute_decide_story(
    session: Session,
    request: DecideStory,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    artifact = session.get(StoryArtifact, request.story_artifact_id)
    if (
        artifact is None
        or artifact.project_id != request.project_id
        or artifact.requirement_id != request.requirement_id
        or artifact.content_fingerprint != request.artifact_fingerprint
        or not _matches_reference(
            decision,
            fact_type="story",
            fact_id=request.story_artifact_id,
            fingerprint=request.artifact_fingerprint,
        )
    ):
        return _conflict("DecideStory does not target the waiting artifact.")
    try:
        row = record_story_decision_in_session(
            session,
            inputs=RecordStoryDecisionInput(
                artifact=artifact,
                decision=request.decision,
                rationale=request.rationale,
                reviewer=request.actor,
                idempotency_key=request.idempotency_key,
                decided_at=evaluated_at,
            ),
        )
    except ValueError as error:
        return _conflict(str(error))
    return _success(
        decision,
        {
            "story_artifact_decision_id": row.story_artifact_decision_id,
            "story_artifact_id": request.story_artifact_id,
            "decision": request.decision,
        },
    )


def execute_apply_story_dependencies(
    session: Session,
    request: ApplyStoryDependencies,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    stories, _dependencies = candidate_set_in_session(
        session,
        project_id=request.project_id,
    )
    expected_ids = tuple(item.story_id for item in stories)
    expected_source = story_dependency_source_fingerprint(stories)
    if (
        request.selected_story_ids != expected_ids
        or request.source_fingerprint != expected_source
        or not _matches_reference(
            decision,
            fact_type="story_dependency_source",
            fact_id=request.project_id,
            fingerprint=request.source_fingerprint,
        )
    ):
        return _conflict("ApplyStoryDependencies does not target exact Story facts.")
    try:
        row = apply_story_dependencies_in_session(
            session,
            inputs=ApplyStoryDependenciesInput(
                project_id=request.project_id,
                selected_story_ids=request.selected_story_ids,
                reviewed_edges=request.reviewed_edges,
                source_fingerprint=request.source_fingerprint,
                reviewer=request.actor,
                reviewed_at=evaluated_at,
            ),
        )
    except StoryDependencyGraphError as error:
        return _conflict(str(error))
    return _success(
        decision,
        {
            "story_dependency_review_id": row.story_dependency_review_id,
            "selected_story_ids": request.selected_story_ids,
            "dependency_fingerprint": row.dependency_fingerprint,
        },
    )


def execute_repair_story_readiness(
    session: Session,
    request: RepairStoryReadiness,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    snapshot = WorkflowFactRepository(session).load(request.project_id)
    missing_ids = tuple(
        item.story_id
        for item in snapshot.stories
        if item.sprint_candidate and (item.story_points is None or item.rank is None)
    )
    expected = readiness_fingerprint(snapshot.stories)
    if (
        request.story_ids != missing_ids
        or request.expected_readiness_fingerprint != expected
        or not _matches_reference(
            decision,
            fact_type="story_readiness",
            fact_id=request.project_id,
            fingerprint=expected,
        )
    ):
        return _conflict("RepairStoryReadiness does not target exact Story facts.")
    try:
        repaired = repair_story_readiness_in_session(
            session,
            project_id=request.project_id,
            repairs=tuple(
                (item.story_id, item.story_points, item.rank)
                for item in request.repairs
            ),
            repaired_at=evaluated_at,
        )
    except (ValueError, RuntimeError) as error:
        return _conflict(str(error))
    return _success(decision, {"story_ids": repaired})


def execute_record_sprint_plan(
    session: Session,
    request: RecordSprintPlan,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    stories, dependencies = candidate_set_in_session(
        session,
        project_id=request.project_id,
    )
    current = candidate_set_fingerprint(stories, dependencies)
    candidate_ids = {item.story_id for item in stories}
    if (
        request.candidate_set_fingerprint != current
        or any(item not in candidate_ids for item in request.selected_story_ids)
        or not _matches_reference(
            decision,
            fact_type="candidate_set",
            fact_id=request.project_id,
            fingerprint=current,
        )
        or request.supersedes_sprint_plan_artifact_id
        != _expected_parent(decision, "sprint_plan")
    ):
        return _conflict("RecordSprintPlan does not target exact candidate facts.")
    try:
        row = record_sprint_plan_in_session(
            session,
            inputs=RecordSprintPlanInput(
                project_id=request.project_id,
                team_name=request.team_name,
                selected_story_ids=request.selected_story_ids,
                canonical_task_plan=request.canonical_task_plan,
                plan_fingerprint=request.plan_fingerprint,
                candidate_set_fingerprint=request.candidate_set_fingerprint,
                supersedes_sprint_plan_artifact_id=(
                    request.supersedes_sprint_plan_artifact_id
                ),
                actor=request.actor,
                recorded_at=evaluated_at,
            ),
        )
    except (ValueError, StoryDependencyGraphError) as error:
        return _conflict(str(error))
    if row.sprint_plan_artifact_id is None:
        return _conflict("Sprint plan artifact did not receive a durable identity.")
    return _success(
        decision,
        {
            "sprint_plan_artifact_id": row.sprint_plan_artifact_id,
            "sprint_id": row.sprint_id,
            "plan_fingerprint": row.plan_fingerprint,
            "candidate_set_fingerprint": row.candidate_set_fingerprint,
        },
    )


def execute_decide_sprint_plan(
    session: Session,
    request: DecideSprintPlan,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    artifact = session.get(SprintPlanArtifact, request.sprint_plan_artifact_id)
    snapshot = WorkflowFactRepository(session).load(request.project_id)
    current_plan = _current_sprint_plan(snapshot, request.sprint_plan_artifact_id)
    if (
        artifact is None
        or current_plan is None
        or artifact.project_id != request.project_id
        or artifact.plan_fingerprint != request.plan_fingerprint
        or not _matches_reference(
            decision,
            fact_type="sprint_plan",
            fact_id=request.sprint_plan_artifact_id,
            fingerprint=request.plan_fingerprint,
        )
        or not _matches_reference(
            decision,
            fact_type="candidate_set",
            fact_id=request.project_id,
            fingerprint=current_plan[1],
        )
        or not _matches_reference(
            decision,
            fact_type="sprint_plan_tasks",
            fact_id=artifact.sprint_id,
            fingerprint=current_plan[2],
        )
    ):
        return _conflict("DecideSprintPlan does not target the waiting artifact.")
    try:
        row = record_sprint_plan_decision_in_session(
            session,
            inputs=RecordSprintPlanDecisionInput(
                artifact=artifact,
                decision=request.decision,
                rationale=request.rationale,
                reviewer=request.actor,
                idempotency_key=request.idempotency_key,
                decided_at=evaluated_at,
            ),
        )
    except ValueError as error:
        return _conflict(str(error))
    return _success(
        decision,
        {
            "sprint_plan_artifact_decision_id": (row.sprint_plan_artifact_decision_id),
            "sprint_plan_artifact_id": request.sprint_plan_artifact_id,
            "decision": request.decision,
        },
    )


def execute_start_sprint(
    session: Session,
    request: StartSprint,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    artifact = session.get(SprintPlanArtifact, request.sprint_plan_artifact_id)
    accepted = session.exec(
        select(SprintPlanArtifactDecision).where(
            col(SprintPlanArtifactDecision.project_id) == request.project_id,
            col(SprintPlanArtifactDecision.sprint_plan_artifact_id)
            == request.sprint_plan_artifact_id,
            col(SprintPlanArtifactDecision.decision) == "accepted",
        )
    ).one_or_none()
    stories, dependencies = candidate_set_in_session(
        session,
        project_id=request.project_id,
    )
    current = candidate_set_fingerprint(stories, dependencies)
    snapshot = WorkflowFactRepository(session).load(request.project_id)
    current_plan = _current_sprint_plan(snapshot, request.sprint_plan_artifact_id)
    if (
        artifact is None
        or accepted is None
        or current_plan is None
        or artifact.sprint_id != request.sprint_id
        or artifact.plan_fingerprint != request.plan_fingerprint
        or artifact.candidate_set_fingerprint != request.candidate_set_fingerprint
        or current != request.candidate_set_fingerprint
        or not _matches_reference(
            decision,
            fact_type="sprint_plan",
            fact_id=request.sprint_plan_artifact_id,
            fingerprint=request.plan_fingerprint,
        )
        or not _matches_reference(
            decision,
            fact_type="candidate_set",
            fact_id=request.project_id,
            fingerprint=request.candidate_set_fingerprint,
        )
        or not _matches_reference(
            decision,
            fact_type="sprint_plan_tasks",
            fact_id=request.sprint_id,
            fingerprint=current_plan[2],
        )
    ):
        return _conflict("StartSprint does not target exact current plan facts.")
    try:
        sprint = start_sprint_in_session(
            session,
            project_id=request.project_id,
            sprint_id=request.sprint_id,
            started_at=evaluated_at,
        )
    except ValueError as error:
        return _conflict(str(error))
    return _success(
        decision,
        {"sprint_id": request.sprint_id, "status": sprint.status.value},
    )


def _execute_roadmap_request(
    session: Session,
    request: RoadmapPlanningRequest,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    if isinstance(request, RecordRoadmapDraft):
        return execute_record_roadmap_draft(session, request, decision, evaluated_at)
    return execute_decide_roadmap(session, request, decision, evaluated_at)


def _execute_story_request(
    session: Session,
    request: StoryPlanningRequest,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    if isinstance(request, RecordStoryDraft):
        return execute_record_story_draft(session, request, decision, evaluated_at)
    if isinstance(request, DecideStory):
        return execute_decide_story(session, request, decision, evaluated_at)
    if isinstance(request, ApplyStoryDependencies):
        return execute_apply_story_dependencies(
            session,
            request,
            decision,
            evaluated_at,
        )
    return execute_repair_story_readiness(session, request, decision, evaluated_at)


def _execute_sprint_request(
    session: Session,
    request: SprintPlanningRequest,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    if isinstance(request, RecordSprintPlan):
        return execute_record_sprint_plan(session, request, decision, evaluated_at)
    if isinstance(request, DecideSprintPlan):
        return execute_decide_sprint_plan(session, request, decision, evaluated_at)
    return execute_start_sprint(session, request, decision, evaluated_at)


def execute_planning_request(
    session: Session,
    request: PlanningRequest,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Dispatch the closed nine-request planning family."""
    if isinstance(request, (RecordRoadmapDraft, DecideRoadmap)):
        return _execute_roadmap_request(session, request, decision, evaluated_at)
    if isinstance(
        request,
        (RecordStoryDraft, DecideStory, ApplyStoryDependencies, RepairStoryReadiness),
    ):
        return _execute_story_request(session, request, decision, evaluated_at)
    return _execute_sprint_request(session, request, decision, evaluated_at)


__all__ = [
    "PlanningRequest",
    "execute_planning_request",
    "record_roadmap_draft_in_session",
    "validate_planning_review",
]
