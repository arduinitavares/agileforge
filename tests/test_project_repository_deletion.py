"""Fresh-schema Project deletion tests."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event
from sqlmodel import Session, col, select

from models.core import (
    Project,
    ProjectTeam,
    Sprint,
    SprintStory,
    Task,
    Team,
    UserStory,
    UserStoryDependency,
)
from models.enums import (
    SprintStatus,
    StoryResolution,
    StoryStatus,
    TaskAcceptanceResult,
    TaskStatus,
    WorkflowEventType,
)
from models.events import StoryCompletionLog, TaskExecutionLog, WorkflowEvent
from models.product_definition import (
    ProductGoalArtifact,
    ProductGoalArtifactDecision,
    ProductGoalInterviewTurn,
    ProductGoalOutcome,
    SpecificationCandidate,
    SpecificationDecision,
    SpecificationSource,
    VisionArtifact,
    VisionArtifactDecision,
    VisionEvidenceSnapshot,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from models.repository import RepositoryBinding
from models.specs import SpecRegistry
from models.workflow import (
    BacklogArtifact,
    BacklogArtifactDecision,
    PostSprintTriage,
    RoadmapArtifact,
    RoadmapArtifactDecision,
    SprintClosure,
    SprintPlanArtifact,
    SprintPlanArtifactDecision,
    SprintReview,
    SprintStart,
    StoryArtifact,
    StoryArtifactDecision,
    StoryClosure,
    StoryDependencyReview,
    TaskCompletionEvidence,
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
)
from services.contracts.vision_evidence import VisionEvidenceBundle, VisionEvidenceItem
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from workflow.contracts import GRAPH_VERSION, JsonObject
from workflow.fingerprints import (
    canonical_hash,
    canonical_json,
    vision_interview_output_fingerprint,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_PROJECT_REPOSITORY_SPEC = importlib.util.spec_from_file_location(
    "task3_project_repository",
    Path(__file__).parents[1] / "repositories" / "project.py",
)
assert _PROJECT_REPOSITORY_SPEC is not None
assert _PROJECT_REPOSITORY_SPEC.loader is not None
_PROJECT_REPOSITORY_MODULE = importlib.util.module_from_spec(_PROJECT_REPOSITORY_SPEC)
_PROJECT_REPOSITORY_SPEC.loader.exec_module(_PROJECT_REPOSITORY_MODULE)
ProjectDeletionConflictError = _PROJECT_REPOSITORY_MODULE.ProjectDeletionConflictError
ProjectRepository = _PROJECT_REPOSITORY_MODULE.ProjectRepository

_REPOSITORY_PATH = "repository"
_COMMIT_FAILURE = "injected commit failure"

_PRODUCT_MODELS = (
    VisionRevisionIntent,
    VisionInterviewTurn,
    VisionArtifactDecision,
    VisionArtifact,
    VisionEvidenceSnapshot,
    ProductGoalOutcome,
    ProductGoalArtifactDecision,
    ProductGoalArtifact,
    ProductGoalInterviewTurn,
    SpecificationDecision,
    SpecificationCandidate,
    SpecificationSource,
    SpecRegistry,
    RepositoryBinding,
    WorkflowNodeAttemptOutcome,
    WorkflowNodeAttempt,
)

_DELIVERY_MODELS = (
    BacklogArtifactDecision,
    BacklogArtifact,
    RoadmapArtifactDecision,
    RoadmapArtifact,
    StoryArtifactDecision,
    StoryArtifact,
    SprintStart,
    StoryDependencyReview,
    SprintPlanArtifactDecision,
    SprintPlanArtifact,
    TaskCompletionEvidence,
    StoryClosure,
    SprintReview,
    SprintClosure,
    PostSprintTriage,
    TaskExecutionLog,
    StoryCompletionLog,
    SprintStory,
    Task,
    UserStory,
    Sprint,
    WorkflowEvent,
    ProjectTeam,
)


def _repository_binding(project_id: int) -> RepositoryBinding:
    return RepositoryBinding(
        project_id=project_id,
        worktree_path=_REPOSITORY_PATH,
        common_git_dir=f"{_REPOSITORY_PATH}/.git",
        head_sha="a" * 40,
        branch_name="main",
        detached_head=False,
        dirty=False,
        status_fingerprint=canonical_hash({"status": "clean"}),
        remotes_json="[]",
        warnings_json="[]",
        probe_version="agileforge.repository-probe.v1",
        inspected_at=datetime(2026, 8, 9, 12, tzinfo=UTC),
        recorded_by="operator@example.com",
    )


def _vision_evidence_snapshot(
    project_id: int,
    attempt: WorkflowNodeAttempt,
    repository_binding_id: int,
) -> VisionEvidenceSnapshot:
    """Create one valid immutable snapshot bound to the deletion fixture."""
    content: JsonObject = {"project_name": "Populated lineage"}
    item = VisionEvidenceItem(
        evidence_id="project:metadata",
        kind="project_metadata",
        relative_path=None,
        content_fingerprint=canonical_hash(content),
        trust="operator_provided",
        content=content,
        truncated=False,
    )
    payload = {
        "schema_version": "agileforge.vision-evidence.v1",
        "items": [item.model_dump(mode="json")],
        "warnings": [],
    }
    evidence = VisionEvidenceBundle(
        schema_version="agileforge.vision-evidence.v1",
        items=(item,),
        warnings=(),
        evidence_fingerprint=canonical_hash(payload),
    )
    assert attempt.workflow_node_attempt_id is not None
    return VisionEvidenceSnapshot(
        project_id=project_id,
        repository_binding_id=repository_binding_id,
        workflow_node_attempt_id=attempt.workflow_node_attempt_id,
        evidence_json=canonical_json(evidence.model_dump(mode="json")),
        evidence_fingerprint=evidence.evidence_fingerprint,
        warnings_json="[]",
        created_at=datetime(2026, 8, 9, 13, 3, 15, tzinfo=UTC),
    )


def _seed_populated_product_lineage(session: Session) -> int:
    """Persist every current product-lineage family, including a Vision revision."""
    project = Project(name="Populated lineage")
    session.add(project)
    session.flush()
    assert project.project_id is not None
    project_id = project.project_id
    binding = _repository_binding(project_id)
    session.add(binding)
    session.flush()
    assert binding.repository_binding_id is not None
    project.active_repository_binding_id = binding.repository_binding_id
    session.add(project)
    session.commit()

    def bind_current_specification_decision(
        flush_session: Session,
        *_args: object,
    ) -> None:
        decisions = [
            row for row in flush_session.new if isinstance(row, SpecificationDecision)
        ]
        registries = [row for row in flush_session.new if isinstance(row, SpecRegistry)]
        if len(decisions) != 1 or len(registries) != 1:
            return
        decision = decisions[0]
        registry = registries[0]
        if decision.specification_decision_id is None:
            decision.specification_decision_id = 1_000_000 + project_id
        registry.source_specification_decision_id = decision.specification_decision_id

    event.listen(session, "before_flush", bind_current_specification_decision)
    try:
        lineage = seed_accepted_specification(
            session,
            project_id=project_id,
            content='{"title":"Accepted specification"}',
            recorded_at=datetime(2026, 8, 9, 13, tzinfo=UTC),
        )
    finally:
        event.remove(session, "before_flush", bind_current_specification_decision)
    session.add(
        ProductGoalOutcome(
            project_id=project_id,
            product_goal_artifact_id=lineage.product_goal_artifact_id,
            artifact_fingerprint=lineage.product_goal_fingerprint,
            outcome="fulfilled",
            rationale="Completed before deletion.",
            decided_by="operator@example.com",
            idempotency_key="goal-outcome-delete",
            decided_at=datetime(2026, 8, 9, 13, 1, tzinfo=UTC),
        )
    )
    intent = VisionRevisionIntent(
        project_id=project_id,
        source_vision_artifact_id=lineage.vision_artifact_id,
        source_vision_fingerprint=lineage.vision_fingerprint,
        reason="Exercise revision deletion order.",
        initiated_by="operator@example.com",
        initiated_at=datetime(2026, 8, 9, 13, 2, tzinfo=UTC),
    )
    session.add(intent)
    session.flush()
    assert intent.vision_revision_intent_id is not None
    attempt = WorkflowNodeAttempt(
        project_id=project_id,
        node_id="vision.interview",
        instance_key=None,
        graph_version=GRAPH_VERSION,
        fact_fingerprint=canonical_hash({"facts": "revision"}),
        business_fact_fingerprint=canonical_hash({"business": "revision"}),
        decision_fingerprint=canonical_hash({"decision": "revision"}),
        normalized_input_json="{}",
        input_fingerprint=canonical_hash({"input": "revision"}),
        model_id="fake/revision",
        execution_settings_json="{}",
        idempotency_key="vision-revision-attempt-delete",
        actor="operator@example.com",
        correlation_id=None,
        started_at=datetime(2026, 8, 9, 13, 3, tzinfo=UTC),
        lease_expires_at=datetime(2026, 8, 9, 13, 4, tzinfo=UTC),
        attempt_fingerprint=canonical_hash({"attempt": "revision"}),
    )
    session.add(attempt)
    session.flush()
    assert attempt.workflow_node_attempt_id is not None
    snapshot = _vision_evidence_snapshot(
        project_id,
        attempt,
        binding.repository_binding_id,
    )
    session.add(snapshot)
    session.flush()
    assert snapshot.vision_evidence_snapshot_id is not None
    turn = VisionInterviewTurn(
        project_id=project_id,
        operation="revision",
        turn_number=1,
        revision_intent_id=intent.vision_revision_intent_id,
        vision_evidence_snapshot_id=snapshot.vision_evidence_snapshot_id,
        prior_turn_id=None,
        user_text="Revise the Vision.",
        components_json='{"purpose":"revised"}',
        vision_statement="Deliver the revised Vision.",
        is_complete=True,
        clarifying_questions_json="[]",
        component_basis_json="[]",
        assumptions_json="[]",
        conflicts_json="[]",
        output_fingerprint=vision_interview_output_fingerprint(
            {"purpose": "revised"},
            "Deliver the revised Vision.",
            True,
            (),
            {"component_basis": (), "assumptions": (), "conflicts": ()},
        ),
        workflow_node_attempt_id=attempt.workflow_node_attempt_id,
        attempt_fingerprint=attempt.attempt_fingerprint,
        recorded_at=datetime(2026, 8, 9, 13, 3, 30, tzinfo=UTC),
    )
    session.add(turn)
    session.flush()
    assert turn.vision_interview_turn_id is not None
    session.add(
        VisionArtifact(
            project_id=project_id,
            version_number=2,
            components_json='{"purpose":"revised"}',
            statement="Deliver the revised Vision.",
            content_fingerprint=canonical_hash({"vision": "revised"}),
            vision_evidence_snapshot_id=snapshot.vision_evidence_snapshot_id,
            component_basis_json="[]",
            assumptions_json="[]",
            conflicts_json="[]",
            supersedes_vision_artifact_id=lineage.vision_artifact_id,
            source_interview_turn_id=turn.vision_interview_turn_id,
            created_by="operator@example.com",
            created_at=datetime(2026, 8, 9, 13, 4, tzinfo=UTC),
        )
    )
    session.commit()
    return project_id


def _required_identity(value: int | None, label: str) -> int:
    if value is None:
        message = f"{label} has no durable identity."
        raise AssertionError(message)
    return value


def _seed_started_delivery_lifecycle(  # noqa: PLR0915
    session: Session,
) -> tuple[int, int, int]:
    """Persist one complete started-then-closed Sprint planning lifecycle."""
    project_id = _seed_populated_product_lineage(session)
    now = datetime(2026, 8, 9, 14, tzinfo=UTC)
    spec = session.exec(
        select(SpecRegistry).where(
            col(SpecRegistry.project_id) == project_id,
            col(SpecRegistry.status) == "approved",
        )
    ).one()
    goal = session.exec(
        select(ProductGoalArtifact).where(
            col(ProductGoalArtifact.project_id) == project_id
        )
    ).one()
    spec_id = _required_identity(spec.spec_version_id, "Specification")
    goal_id = _required_identity(goal.product_goal_artifact_id, "Product Goal")

    backlog_fingerprint = canonical_hash({"artifact": "backlog", "project": project_id})
    backlog = BacklogArtifact(
        project_id=project_id,
        spec_version_id=spec_id,
        spec_hash=spec.spec_hash,
        product_goal_artifact_id=goal_id,
        product_goal_fingerprint=goal.content_fingerprint,
        version_number=1,
        canonical_content_json='{"items":["BACKLOG-1"]}',
        content_fingerprint=backlog_fingerprint,
        created_by="operator@example.com",
        created_at=now,
    )
    session.add(backlog)
    session.flush()
    backlog_id = _required_identity(backlog.backlog_artifact_id, "Backlog")
    session.add(
        BacklogArtifactDecision(
            project_id=project_id,
            backlog_artifact_id=backlog_id,
            artifact_fingerprint=backlog_fingerprint,
            decision="accepted",
            rationale="Accepted for deletion regression.",
            reviewer="operator@example.com",
            idempotency_key="delete-backlog-accepted",
            decided_at=now,
        )
    )

    roadmap_fingerprint = canonical_hash({"artifact": "roadmap", "project": project_id})
    roadmap = RoadmapArtifact(
        project_id=project_id,
        backlog_artifact_id=backlog_id,
        backlog_artifact_fingerprint=backlog_fingerprint,
        version_number=1,
        canonical_content_json='{"items":["ROADMAP-1"]}',
        content_fingerprint=roadmap_fingerprint,
        created_by="operator@example.com",
        created_at=now,
    )
    session.add(roadmap)
    session.flush()
    roadmap_id = _required_identity(roadmap.roadmap_artifact_id, "Roadmap")
    session.add(
        RoadmapArtifactDecision(
            project_id=project_id,
            roadmap_artifact_id=roadmap_id,
            artifact_fingerprint=roadmap_fingerprint,
            decision="accepted",
            rationale="Accepted for deletion regression.",
            reviewer="operator@example.com",
            idempotency_key="delete-roadmap-accepted",
            decided_at=now,
        )
    )

    story_artifact_fingerprint = canonical_hash(
        {"artifact": "story", "project": project_id}
    )
    story_artifact = StoryArtifact(
        project_id=project_id,
        source_backlog_artifact_id=backlog_id,
        source_backlog_artifact_fingerprint=backlog_fingerprint,
        backlog_item_id="BACKLOG-1",
        roadmap_artifact_id=roadmap_id,
        roadmap_artifact_fingerprint=roadmap_fingerprint,
        version_number=1,
        canonical_content_json='{"items":["STORY-1","STORY-2"]}',
        content_fingerprint=story_artifact_fingerprint,
        story_item_ids_json='["STORY-1","STORY-2"]',
        created_by="operator@example.com",
        created_at=now,
    )
    session.add(story_artifact)
    session.flush()
    story_artifact_id = _required_identity(
        story_artifact.story_artifact_id,
        "Story artifact",
    )
    session.add(
        StoryArtifactDecision(
            project_id=project_id,
            story_artifact_id=story_artifact_id,
            artifact_fingerprint=story_artifact_fingerprint,
            decision="accepted",
            rationale="Accepted for deletion regression.",
            reviewer="operator@example.com",
            idempotency_key="delete-story-accepted",
            decided_at=now,
        )
    )

    story = UserStory(
        project_id=project_id,
        source_story_artifact_id=story_artifact_id,
        source_story_artifact_fingerprint=story_artifact_fingerprint,
        source_story_item_id="STORY-1",
        source_story_item_fingerprint=canonical_hash({"story": "STORY-1"}),
        accepted_spec_version_id=spec_id,
        accepted_spec_hash=spec.spec_hash,
        spec_item_ids_json='["GOAL.fixture.accepted-specification"]',
        title="Delete the complete delivery lifecycle",
        story_description="As an operator, I want complete child-first deletion.",
        acceptance_criteria_json='["Every dependent row is removed first."]',
        persona="operator",
        status=StoryStatus.DONE,
        resolution=StoryResolution.COMPLETED,
    )
    session.add(story)
    session.flush()
    story_id = _required_identity(story.story_id, "User Story")

    team = Team(name=f"Deletion team {project_id}")
    session.add(team)
    session.flush()
    team_id = _required_identity(team.team_id, "Team")
    session.add(ProjectTeam(project_id=project_id, team_id=team_id))
    sprint = Sprint(
        project_id=project_id,
        team_id=team_id,
        goal="Prove complete deletion.",
        status=SprintStatus.COMPLETED,
        started_at=now,
        completed_at=now,
        close_snapshot_json="{}",
    )
    session.add(sprint)
    session.flush()
    sprint_id = _required_identity(sprint.sprint_id, "Sprint")
    session.add(SprintStory(sprint_id=sprint_id, story_id=story_id, added_at=now))

    task = Task(
        story_id=story_id,
        description="Delete delivery dependents in FK order.",
        metadata_json='{"version":"task_metadata.v2"}',
        status=TaskStatus.DONE,
    )
    session.add(task)
    session.flush()
    task_id = _required_identity(task.task_id, "Task")

    plan_fingerprint = canonical_hash(
        {"artifact": "sprint-plan", "project": project_id}
    )
    candidate_set_fingerprint = canonical_hash({"stories": [story_id]})
    plan = SprintPlanArtifact(
        project_id=project_id,
        spec_version_id=spec_id,
        spec_hash=spec.spec_hash,
        sprint_plan_stream_id="SPS-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        version_number=1,
        selected_story_ids_json=f"[{story_id}]",
        canonical_task_plan_json=f'{{"task_ids":[{task_id}]}}',
        plan_fingerprint=plan_fingerprint,
        candidate_set_fingerprint=candidate_set_fingerprint,
        created_by="operator@example.com",
        created_at=now,
    )
    session.add(plan)
    session.flush()
    plan_id = _required_identity(plan.sprint_plan_artifact_id, "Sprint plan")
    decision = SprintPlanArtifactDecision(
        project_id=project_id,
        sprint_plan_artifact_id=plan_id,
        plan_fingerprint=plan_fingerprint,
        decision="accepted",
        activated_sprint_id=sprint_id,
        rationale="Accepted and activated.",
        reviewer="operator@example.com",
        idempotency_key="delete-sprint-plan-accepted",
        decided_at=now,
    )
    session.add(decision)
    session.flush()
    decision_id = _required_identity(
        decision.sprint_plan_artifact_decision_id,
        "Sprint-plan decision",
    )

    dependency_review = StoryDependencyReview(
        project_id=project_id,
        selected_story_ids_json=f"[{story_id}]",
        reviewed_edges_json="[]",
        source_fingerprint=canonical_hash({"selected": [story_id]}),
        dependency_fingerprint=canonical_hash({"edges": []}),
        reviewed_by="operator@example.com",
        reviewed_at=now,
    )
    event_row = WorkflowEvent(
        event_type=WorkflowEventType.SPRINT_STARTED,
        project_id=project_id,
        sprint_id=sprint_id,
        timestamp=now,
    )
    session.add(dependency_review)
    session.add(event_row)
    session.flush()
    dependency_review_id = _required_identity(
        dependency_review.story_dependency_review_id,
        "Story dependency review",
    )
    event_id = _required_identity(event_row.event_id, "Workflow event")
    session.add(
        SprintStart(
            project_id=project_id,
            sprint_id=sprint_id,
            sprint_plan_artifact_id=plan_id,
            sprint_plan_artifact_decision_id=decision_id,
            story_dependency_review_id=dependency_review_id,
            plan_fingerprint=plan_fingerprint,
            candidate_set_fingerprint=candidate_set_fingerprint,
            selected_story_ids_json=f"[{story_id}]",
            task_content_fingerprint=canonical_hash({"tasks": [task_id]}),
            dependency_source_fingerprint=dependency_review.source_fingerprint,
            dependency_fingerprint=dependency_review.dependency_fingerprint,
            dependency_rows_fingerprint=canonical_hash({"rows": []}),
            decision_fingerprint=canonical_hash({"decision": decision_id}),
            audit_event_id=event_id,
            started_by="operator@example.com",
            started_at=now,
        )
    )
    session.add(
        TaskExecutionLog(
            task_id=task_id,
            sprint_id=sprint_id,
            old_status=TaskStatus.IN_PROGRESS,
            new_status=TaskStatus.DONE,
            acceptance_result=TaskAcceptanceResult.FULLY_MET,
            changed_by="operator@example.com",
            changed_at=now,
        )
    )
    session.add(
        StoryCompletionLog(
            story_id=story_id,
            old_status=StoryStatus.IN_PROGRESS,
            new_status=StoryStatus.DONE,
            resolution=StoryResolution.COMPLETED,
            changed_by="operator@example.com",
            changed_at=now,
        )
    )
    session.add(
        TaskCompletionEvidence(
            project_id=project_id,
            sprint_id=sprint_id,
            task_id=task_id,
            outcome_summary="Completed.",
            artifact_refs_json="[]",
            acceptance_result="fully_met",
            checklist_result_json="[]",
            evidence_fingerprint=canonical_hash({"task": task_id}),
            completed_by="operator@example.com",
            completed_at=now,
        )
    )
    session.add(
        StoryClosure(
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
            completion_fingerprint=canonical_hash({"story": story_id}),
            resolution="Completed",
            delivered="Complete deletion coverage.",
            evidence="Provider-free regression.",
            known_gaps="None.",
            closed_by="operator@example.com",
            closed_at=now,
        )
    )
    review_fingerprint = canonical_hash({"sprint-review": sprint_id})
    session.add(
        SprintReview(
            project_id=project_id,
            sprint_id=sprint_id,
            review_fingerprint=review_fingerprint,
            reviewed_by="operator@example.com",
            reviewed_at=now,
        )
    )
    session.add(
        SprintClosure(
            project_id=project_id,
            sprint_id=sprint_id,
            review_fingerprint=review_fingerprint,
            close_fingerprint=canonical_hash({"sprint-close": sprint_id}),
            closed_by="operator@example.com",
            closed_at=now,
        )
    )
    session.add(
        PostSprintTriage(
            project_id=project_id,
            sprint_id=sprint_id,
            impact="none",
            canonical_payload_json='{"impact":"none"}',
            payload_fingerprint=canonical_hash({"impact": "none"}),
            recorded_by="operator@example.com",
            recorded_at=now,
        )
    )
    session.commit()
    return project_id, story_id, story_artifact_id


def _record_counts(session: Session) -> dict[type[object], int]:
    return {model: len(session.exec(select(model)).all()) for model in _PRODUCT_MODELS}


def _delivery_counts(session: Session) -> dict[type[object], int]:
    return {model: len(session.exec(select(model)).all()) for model in _DELIVERY_MODELS}


def test_delete_project_removes_active_repository_binding(engine: Engine) -> None:
    """Remove the Project pointer and immutable repository observations together."""
    with Session(engine) as session:
        project = Project(name="Repository deletion")
        session.add(project)
        session.flush()
        assert project.project_id is not None
        project_id = project.project_id
        binding = _repository_binding(project_id)
        session.add(binding)
        session.flush()
        assert binding.repository_binding_id is not None
        binding_id = binding.repository_binding_id
        project.active_repository_binding_id = binding_id
        session.add(project)
        session.commit()

        assert ProjectRepository(session).delete_project(project_id) is True
        assert session.get(Project, project_id) is None
        assert session.get(RepositoryBinding, binding_id) is None


def test_delete_project_rolls_back_repository_rows_when_commit_fails(
    engine: Engine,
) -> None:
    """Leave both Project and active binding intact when the write cannot commit."""
    with Session(engine) as session:
        project = Project(name="Repository rollback")
        session.add(project)
        session.flush()
        assert project.project_id is not None
        project_id = project.project_id
        binding = _repository_binding(project_id)
        session.add(binding)
        session.flush()
        assert binding.repository_binding_id is not None
        binding_id = binding.repository_binding_id
        project.active_repository_binding_id = binding_id
        session.add(project)
        session.commit()

        def fail_commit(_session: Session) -> None:
            raise RuntimeError(_COMMIT_FAILURE)

        event.listen(session, "before_commit", fail_commit)
        try:
            with pytest.raises(RuntimeError, match=_COMMIT_FAILURE):
                ProjectRepository(session).delete_project(project_id)
        finally:
            event.remove(session, "before_commit", fail_commit)

        assert session.get(Project, project_id) is not None
        assert session.get(RepositoryBinding, binding_id) is not None


def test_delete_project_removes_complete_product_lineage(engine: Engine) -> None:
    """Delete revision Vision, Goal, discovery, specification, and repository rows."""
    with Session(engine) as session:
        project_id = _seed_populated_product_lineage(session)

        assert all(count > 0 for count in _record_counts(session).values())
        assert ProjectRepository(session).delete_project(project_id) is True

        assert session.get(Project, project_id) is None
        assert all(count == 0 for count in _record_counts(session).values())


def test_populated_product_lineage_deletion_rolls_back_on_failure(
    engine: Engine,
) -> None:
    """Restore every populated lineage row when the deletion transaction fails."""
    with Session(engine) as session:
        project_id = _seed_populated_product_lineage(session)
        before = _record_counts(session)

        def fail_commit(_session: Session) -> None:
            raise RuntimeError(_COMMIT_FAILURE)

        event.listen(session, "before_commit", fail_commit)
        try:
            with pytest.raises(RuntimeError, match=_COMMIT_FAILURE):
                ProjectRepository(session).delete_project(project_id)
        finally:
            event.remove(session, "before_commit", fail_commit)

        assert session.get(Project, project_id) is not None
        assert _record_counts(session) == before
        assert ProjectRepository(session).delete_project(project_id) is True


def test_delete_project_removes_complete_started_delivery_lifecycle(
    engine: Engine,
) -> None:
    """Delete every started Sprint planning/execution child before its parent."""
    with Session(engine) as session:
        project_id, _story_id, _story_artifact_id = _seed_started_delivery_lifecycle(
            session
        )

        assert all(count > 0 for count in _delivery_counts(session).values())
        assert ProjectRepository(session).delete_project(project_id) is True

        assert session.get(Project, project_id) is None
        assert all(count == 0 for count in _delivery_counts(session).values())
        assert all(count == 0 for count in _record_counts(session).values())


def test_delete_project_rejects_cross_project_story_dependency_inbound_reference(
    engine: Engine,
) -> None:
    """Do not erase another Project's edge to conceal an inbound FK blocker."""
    with Session(engine) as session:
        project_id, first_story_id, story_artifact_id = (
            _seed_started_delivery_lifecycle(session)
        )
        first_story = session.get(UserStory, first_story_id)
        assert first_story is not None
        second_story = UserStory(
            project_id=project_id,
            source_story_artifact_id=story_artifact_id,
            source_story_artifact_fingerprint=(
                first_story.source_story_artifact_fingerprint
            ),
            source_story_item_id="STORY-2",
            source_story_item_fingerprint=canonical_hash({"story": "STORY-2"}),
            accepted_spec_version_id=first_story.accepted_spec_version_id,
            accepted_spec_hash=first_story.accepted_spec_hash,
            spec_item_ids_json=first_story.spec_item_ids_json,
            title="Preserve an external inbound dependency",
            story_description="An external Project owns the dependency edge.",
            acceptance_criteria_json='["Deletion fails closed."]',
            persona="operator",
        )
        external_project = Project(name="External dependency owner")
        session.add(second_story)
        session.add(external_project)
        session.flush()
        second_story_id = _required_identity(second_story.story_id, "second Story")
        external_project_id = _required_identity(
            external_project.project_id,
            "external Project",
        )
        dependency = UserStoryDependency(
            project_id=external_project_id,
            dependent_story_id=first_story_id,
            prerequisite_story_id=second_story_id,
            status="active",
            source="manual_review",
            confidence="reviewed",
            reason="Cross-Project inbound deletion guard.",
        )
        session.add(dependency)
        session.commit()
        dependency_id = _required_identity(dependency.dependency_id, "dependency")

        with pytest.raises(ProjectDeletionConflictError) as raised:
            ProjectRepository(session).delete_project(project_id)

        assert raised.value.references == ("user_story_dependencies.project_id",)
        assert session.get(Project, project_id) is not None
        assert session.get(Project, external_project_id) is not None
        assert session.get(UserStoryDependency, dependency_id) is not None
