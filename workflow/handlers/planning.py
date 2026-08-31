"""Caller-transaction handlers for durable planning workflow facts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError
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
from services.story_dependencies import (
    ApplyStoryDependenciesInput,
    StoryDependencyGraphError,
    apply_story_dependencies_in_session,
)
from services.story_rank import story_rank_is_valid
from workflow.contracts import (
    NodeDecision,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
)
from workflow.definitions.planning import (
    candidate_set_fingerprint,
    dependency_review_lifecycle_locked,
    readiness_fingerprint,
    selected_scope_stories,
    story_dependency_source_fingerprint,
)
from workflow.definitions.product_discovery import accepted_current_spec
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

    from workflow.facts import (
        PlanningArtifactFact,
        StoryFact,
        WorkflowFactSnapshot,
    )


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


def _story_needs_readiness_repair(story: StoryFact) -> bool:
    return story.story_points is None or not story_rank_is_valid(story.rank)


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


def _workflow_error(code: WorkflowErrorCode, message: str) -> TransitionResult:
    return TransitionResult(ok=False, error=WorkflowError(code=code, message=message))


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


def _sprint_plan_fact(
    snapshot: WorkflowFactSnapshot,
    artifact_id: int,
) -> PlanningArtifactFact | None:
    return next(
        (
            item
            for item in snapshot.planning_artifacts
            if item.artifact_type == "sprint_plan" and item.artifact_id == artifact_id
        ),
        None,
    )


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
            and persisted.backlog_item_id == request.backlog_item_id
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
    from services.agent_workbench.roadmap_phase import (  # noqa: PLC0415
        RecordRoadmapDraftInput,
        record_roadmap_draft_in_session,
    )

    try:
        row = record_roadmap_draft_in_session(
            session,
            inputs=RecordRoadmapDraftInput(
                project_id=request.project_id,
                backlog_artifact_id=request.backlog_artifact_id,
                backlog_artifact_fingerprint=request.backlog_artifact_fingerprint,
                canonical_content=request.canonical_content,
                content_fingerprint=request.content_fingerprint,
                supersedes_roadmap_artifact_id=(request.supersedes_roadmap_artifact_id),
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
    from services.agent_workbench.roadmap_phase import (  # noqa: PLC0415
        RecordRoadmapDecisionInput,
        record_roadmap_decision_in_session,
    )

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
    backlog_item = next(
        (
            item
            for item in snapshot.backlog_items
            if item.backlog_item_id == request.backlog_item_id
            and item.backlog_artifact_id == request.source_backlog_artifact_id
            and item.backlog_artifact_fingerprint
            == request.source_backlog_artifact_fingerprint
            and _matches_reference(
                decision,
                fact_type="backlog_item",
                fact_id=item.backlog_item_id,
                fingerprint=item.item_fingerprint,
            )
        ),
        None,
    )
    roadmap = session.get(RoadmapArtifact, request.roadmap_artifact_id)
    if (
        backlog_item is None
        or roadmap is None
        or roadmap.project_id != request.project_id
        or roadmap.content_fingerprint != request.roadmap_artifact_fingerprint
        or roadmap.backlog_artifact_id != request.source_backlog_artifact_id
        or roadmap.backlog_artifact_fingerprint
        != request.source_backlog_artifact_fingerprint
        or not _matches_reference(
            decision,
            fact_type="roadmap",
            fact_id=request.roadmap_artifact_id,
            fingerprint=request.roadmap_artifact_fingerprint,
        )
        or request.supersedes_story_artifact_id != _expected_parent(decision, "story")
        or (
            request.identical_successor_authorized
            and decision.reason_code != "STORY_CORRECTION_AVAILABLE"
        )
    ):
        return _conflict("RecordStoryDraft does not target exact graph facts.")
    from services.agent_workbench.story_phase import (  # noqa: PLC0415
        RecordStoryDraftInput,
        record_story_draft_in_session,
    )

    try:
        row = record_story_draft_in_session(
            session,
            inputs=RecordStoryDraftInput(
                project_id=request.project_id,
                source_backlog_artifact_id=request.source_backlog_artifact_id,
                source_backlog_artifact_fingerprint=(
                    request.source_backlog_artifact_fingerprint
                ),
                backlog_item_id=request.backlog_item_id,
                roadmap_artifact_id=request.roadmap_artifact_id,
                roadmap_artifact_fingerprint=(request.roadmap_artifact_fingerprint),
                canonical_content=request.canonical_content,
                content_fingerprint=request.content_fingerprint,
                supersedes_story_artifact_id=request.supersedes_story_artifact_id,
                identical_successor_authorized=(
                    request.identical_successor_authorized
                ),
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
            "story_item_ids": tuple(json.loads(row.story_item_ids_json)),
            "content_fingerprint": row.content_fingerprint,
            "backlog_item_id": row.backlog_item_id,
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
        or artifact.backlog_item_id != request.backlog_item_id
        or artifact.content_fingerprint != request.artifact_fingerprint
        or not _matches_reference(
            decision,
            fact_type="story",
            fact_id=request.story_artifact_id,
            fingerprint=request.artifact_fingerprint,
        )
    ):
        return _conflict("DecideStory does not target the waiting artifact.")
    from services.agent_workbench.story_phase import (  # noqa: PLC0415
        RecordStoryDecisionInput,
        StoryAcceptanceValidationError,
        prove_story_decision_winner_in_session,
        record_story_decision_in_session,
    )

    try:
        result = record_story_decision_in_session(
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
    except IntegrityError as error:
        if not _is_story_decision_uniqueness_race(error):
            raise
        session.rollback()
        if not prove_story_decision_winner_in_session(
            session,
            project_id=request.project_id,
            story_artifact_id=request.story_artifact_id,
            artifact_fingerprint=request.artifact_fingerprint,
        ):
            raise
        return _conflict(
            "The Story artifact was decided by another workflow transition."
        )
    except StoryAcceptanceValidationError:
        raise
    except ValueError as error:
        return _conflict(str(error))
    return _success(
        decision,
        {
            "story_artifact_decision_id": (result.decision.story_artifact_decision_id),
            "story_artifact_id": request.story_artifact_id,
            "decision": request.decision,
            "activated_story_ids": result.activated_story_ids,
        },
    )


def _is_story_decision_uniqueness_race(error: IntegrityError) -> bool:
    """Classify only the two named Story decision/materialization constraints."""
    constraint_names = {
        "uq_story_artifact_decision",
        "uq_user_story_artifact_item",
    }
    original = error.orig
    diagnostic = getattr(original, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    if constraint_name is not None:
        return constraint_name in constraint_names
    return str(original) in {
        "UNIQUE constraint failed: story_artifact_decisions.project_id, "
        "story_artifact_decisions.story_artifact_id",
        "UNIQUE constraint failed: user_stories.project_id, "
        "user_stories.source_story_artifact_id, "
        "user_stories.source_story_item_id",
    }


def execute_apply_story_dependencies(
    session: Session,
    request: ApplyStoryDependencies,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    snapshot = WorkflowFactRepository(session).load(request.project_id)
    if dependency_review_lifecycle_locked(snapshot):
        return _conflict(
            "ApplyStoryDependencies is locked until prior Sprint lifecycle "
            "triage finishes."
        )
    stories = selected_scope_stories(snapshot)
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
        if not item.is_superseded
        and item.sprint_selection_state == "selected"
        and _story_needs_readiness_repair(item)
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
    from services.agent_workbench.story_phase import (  # noqa: PLC0415
        repair_story_readiness_in_session,
    )

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


def execute_record_sprint_plan(  # noqa: PLR0911
    session: Session,
    request: RecordSprintPlan,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    snapshot = WorkflowFactRepository(session).load(request.project_id)
    specification = accepted_current_spec(snapshot)
    if (
        specification is None
        or request.spec_version_id != specification.spec_version_id
        or request.spec_hash != specification.spec_hash
        or not _matches_reference(
            decision,
            fact_type="specification",
            fact_id=request.spec_version_id,
            fingerprint=request.spec_hash,
        )
    ):
        return _workflow_error(
            WorkflowErrorCode.STALE_SPECIFICATION,
            "Sprint planning requires the current accepted Specification.",
        )
    from services.agent_workbench.sprint_phase import (  # noqa: PLC0415
        RecordSprintPlanInput,
        record_sprint_plan_in_session,
    )

    try:
        row = record_sprint_plan_in_session(
            session,
            inputs=RecordSprintPlanInput(
                project_id=request.project_id,
                spec_version_id=request.spec_version_id,
                spec_hash=request.spec_hash,
                team_name=request.team_name,
                planner_output=request.planner_output,
                actor=request.actor,
                recorded_at=evaluated_at,
            ),
        )
    except StoryDependencyGraphError as error:
        return _conflict(str(error))
    except ValueError as error:
        from services.agent_workbench.sprint_phase import (  # noqa: PLC0415
            SprintPlanStreamCollisionError,
            StaleSpecificationError,
        )

        if isinstance(error, StaleSpecificationError):
            return _workflow_error(WorkflowErrorCode.STALE_SPECIFICATION, str(error))
        if isinstance(error, SprintPlanStreamCollisionError):
            return _workflow_error(
                WorkflowErrorCode.SPRINT_PLAN_STREAM_ID_COLLISION,
                str(error),
            )
        return _conflict(str(error))
    if row.sprint_plan_artifact_id is None:
        return _conflict("Sprint plan artifact did not receive a durable identity.")
    return _success(
        decision,
        {
            "sprint_plan_artifact_id": row.sprint_plan_artifact_id,
            "sprint_plan_stream_id": row.sprint_plan_stream_id,
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
    current_plan = _sprint_plan_fact(snapshot, request.sprint_plan_artifact_id)
    specification = accepted_current_spec(snapshot)
    stories = tuple(item for item in snapshot.stories if item.sprint_candidate)
    current_candidate = candidate_set_fingerprint(stories, snapshot.story_dependencies)
    if (
        artifact is None
        or current_plan is None
        or current_plan.status != "pending_review"
        or artifact.project_id != request.project_id
        or artifact.plan_fingerprint != request.plan_fingerprint
        or specification is None
        or artifact.spec_version_id != specification.spec_version_id
        or artifact.spec_hash != specification.spec_hash
        or artifact.candidate_set_fingerprint != current_candidate
        or not _matches_reference(
            decision,
            fact_type="sprint_plan",
            fact_id=request.sprint_plan_artifact_id,
            fingerprint=request.plan_fingerprint,
        )
    ):
        return _conflict("DecideSprintPlan does not target the waiting artifact.")
    from services.agent_workbench.sprint_phase import (  # noqa: PLC0415
        RecordSprintPlanDecisionInput,
        record_sprint_plan_decision_in_session,
    )

    try:
        with session.begin_nested():
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
        from services.sprint_ownership import (  # noqa: PLC0415
            SprintOwnerResolutionError,
        )

        if isinstance(error, SprintOwnerResolutionError):
            return _workflow_error(error.code, str(error))
        return _conflict(str(error))
    return _success(
        decision,
        {
            "sprint_plan_artifact_decision_id": (row.sprint_plan_artifact_decision_id),
            "sprint_plan_artifact_id": request.sprint_plan_artifact_id,
            "decision": request.decision,
            "activated_sprint_id": row.activated_sprint_id,
        },
    )


def execute_start_sprint(
    session: Session,
    request: StartSprint,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    task_references = tuple(
        item
        for item in decision.fact_references
        if item.fact_type == "sprint_plan_tasks"
    )
    if len(task_references) != 1 or not task_references[0].fact_id.isdigit():
        return _conflict("StartSprint does not target exact current plan facts.")
    from services.agent_workbench.sprint_phase import (  # noqa: PLC0415
        SprintStartInput,
        start_sprint_in_session,
    )

    try:
        with session.begin_nested():
            sprint = start_sprint_in_session(
                session,
                SprintStartInput(
                    project_id=request.project_id,
                    expected_sprint_id=int(task_references[0].fact_id),
                    expected_task_content_fingerprint=(task_references[0].fingerprint),
                    decision_fingerprint=decision.decision_fingerprint,
                    started_by=request.actor,
                    started_at=evaluated_at,
                ),
            )
    except ValueError as error:
        from services.agent_workbench.sprint_phase import (  # noqa: PLC0415
            ActiveSprintExistsError,
            StaleSpecificationError,
        )

        if isinstance(error, StaleSpecificationError):
            return _workflow_error(WorkflowErrorCode.STALE_SPECIFICATION, str(error))
        if isinstance(error, ActiveSprintExistsError):
            return _workflow_error(WorkflowErrorCode.ACTIVE_SPRINT_EXISTS, str(error))
        return _conflict(str(error))
    return _success(
        decision,
        {"sprint_id": sprint.sprint_id, "status": sprint.status.value},
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
    "validate_planning_review",
]
