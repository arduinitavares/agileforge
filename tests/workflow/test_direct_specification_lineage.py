"""Direct-Specification lineage and stale-parent workflow regressions."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from typing import Any, Literal, cast

import pytest

import workflow.handlers.planning as planning_handlers
import workflow.handlers.product_definition as product_handlers
from repositories.workflow import WorkflowFactRepository
from services.contracts.sprint import SprintPlannerOutput
from services.planning_lineage import ArtifactLineageNode
from workflow.contracts import FactReference, NodeCategory, WorkflowErrorCode
from workflow.definitions.backlog import current_backlog_lineage
from workflow.definitions.execution import _active_sprint_lineage_is_proven
from workflow.definitions.planning import _artifact_state, planning_graph
from workflow.definitions.root import project_graph
from workflow.facts import (
    BacklogItemFact,
    PhaseArtifactFact,
    PlanningArtifactFact,
    ProductGoalArtifactDecisionFact,
    ProductGoalArtifactFact,
    ProjectFact,
    SpecificationCandidateFact,
    SpecVersionFact,
    SprintFact,
    SprintStartFact,
    StoryFact,
    TaskFact,
    VisionArtifactDecisionFact,
    VisionArtifactFact,
    WorkflowFactSnapshot,
)
from workflow.handlers.planning import execute_record_story_draft
from workflow.requests.planning import (
    RecordRoadmapDraft,
    RecordSprintPlan,
    RecordStoryDraft,
    StartSprint,
)
from workflow.requests.product_definition import RecordBacklogDraft

EVALUATED_AT = datetime(2026, 8, 21, 12, tzinfo=UTC)
PROJECT_ID = 9
VISION_ID = 11
VISION_FINGERPRINT = "sha256:vision"
GOAL_ID = 21
GOAL_FINGERPRINT = "sha256:goal"
CANDIDATE_ID = 31
CANDIDATE_FINGERPRINT = "sha256:candidate"
SPEC_VERSION_ID = 41
SPEC_HASH = "sha256:specification"

GUARD_PROJECT_ID = 7
GUARD_SPEC_VERSION_ID = 41
GUARD_GOAL_ID = 31
BACKLOG_ARTIFACT_ID = 51


def _accepted_snapshot() -> WorkflowFactSnapshot:
    vision = VisionArtifactFact(
        vision_artifact_id=VISION_ID,
        version_number=1,
        components={},
        statement="Ship the accepted contract.",
        content_fingerprint=VISION_FINGERPRINT,
        vision_evidence_snapshot_id=1,
        supersedes_vision_artifact_id=None,
        source_interview_turn_id=1,
        created_by="operator@example.com",
        created_at=EVALUATED_AT,
    )
    goal = ProductGoalArtifactFact(
        product_goal_artifact_id=GOAL_ID,
        vision_artifact_id=VISION_ID,
        vision_fingerprint=VISION_FINGERPRINT,
        goal_number=1,
        revision_number=1,
        statement="Deliver from the accepted Specification.",
        content_fingerprint=GOAL_FINGERPRINT,
        supersedes_product_goal_artifact_id=None,
        source_interview_turn_id=1,
        created_by="operator@example.com",
        created_at=EVALUATED_AT,
    )
    candidate = SpecificationCandidateFact(
        specification_candidate_id=CANDIDATE_ID,
        candidate_kind="initial",
        specification_source_id=1,
        specification_source_fingerprint="sha256:source",
        vision_artifact_id=VISION_ID,
        vision_fingerprint=VISION_FINGERPRINT,
        product_goal_artifact_id=GOAL_ID,
        product_goal_fingerprint=GOAL_FINGERPRINT,
        base_spec_version_id=None,
        base_spec_hash=None,
        canonical_envelope={},
        payload_fingerprint=SPEC_HASH,
        source_manifest_fingerprint="sha256:manifest",
        producer_input_fingerprint="sha256:producer-input",
        rendered_view_fingerprint="sha256:rendered",
        candidate_fingerprint=CANDIDATE_FINGERPRINT,
        workflow_node_attempt_id=1,
        attempt_fingerprint="sha256:attempt",
        supersedes_specification_candidate_id=None,
        supersedes_candidate_fingerprint=None,
        recorded_by="operator@example.com",
        recorded_at=EVALUATED_AT,
    )
    specification = SpecVersionFact(
        spec_version_id=SPEC_VERSION_ID,
        spec_hash=SPEC_HASH,
        status="approved",
        source_specification_decision_id=1,
        accepted_at=EVALUATED_AT,
        accepted_by="operator@example.com",
        acceptance_notes="Accepted.",
        source_specification_candidate_id=CANDIDATE_ID,
        source_specification_candidate_fingerprint=CANDIDATE_FINGERPRINT,
        source_vision_artifact_id=VISION_ID,
        source_vision_fingerprint=VISION_FINGERPRINT,
        source_product_goal_artifact_id=GOAL_ID,
        source_product_goal_fingerprint=GOAL_FINGERPRINT,
    )
    return WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=PROJECT_ID,
            name="Direct Specification graph",
            created_at=EVALUATED_AT,
        ),
        vision_artifacts=(vision,),
        vision_artifact_decisions=(
            VisionArtifactDecisionFact(
                vision_artifact_decision_id=1,
                vision_artifact_id=VISION_ID,
                artifact_fingerprint=VISION_FINGERPRINT,
                decision="accepted",
                rationale="Accepted.",
                reviewer="operator@example.com",
                idempotency_key="vision-accepted",
                decided_at=EVALUATED_AT,
            ),
        ),
        product_goal_artifacts=(goal,),
        product_goal_artifact_decisions=(
            ProductGoalArtifactDecisionFact(
                product_goal_artifact_decision_id=1,
                product_goal_artifact_id=GOAL_ID,
                artifact_fingerprint=GOAL_FINGERPRINT,
                decision="accepted",
                rationale="Accepted.",
                reviewer="operator@example.com",
                idempotency_key="goal-accepted",
                decided_at=EVALUATED_AT,
            ),
        ),
        specification_candidates=(candidate,),
        spec_versions=(specification,),
    )


def test_specification_facts_normalize_naive_sqlite_timestamps_to_utc() -> None:
    """Compare persisted Specification chronology with UTC-normalized Sprint facts."""
    snapshot = _accepted_snapshot()
    naive_timestamp = EVALUATED_AT.replace(tzinfo=None)
    candidate = SpecificationCandidateFact.model_validate(
        snapshot.specification_candidates[0].model_dump()
        | {"recorded_at": naive_timestamp}
    )
    specification = SpecVersionFact.model_validate(
        snapshot.spec_versions[0].model_dump() | {"accepted_at": naive_timestamp}
    )

    assert candidate.recorded_at == EVALUATED_AT
    assert specification.accepted_at == EVALUATED_AT


def test_accepted_specification_and_goal_expose_backlog_directly() -> None:
    """The exact current Specification and Goal are Backlog's only root parents."""
    position = project_graph().evaluate(_accepted_snapshot(), EVALUATED_AT)
    decision = next(
        item for item in position.decisions if item.node_id == "backlog.generate"
    )

    assert decision.category is NodeCategory.AVAILABLE
    assert {
        (item.fact_type, item.fact_id, item.fingerprint)
        for item in decision.fact_references
    } == {
        ("specification", str(SPEC_VERSION_ID), SPEC_HASH),
        ("product_goal", str(GOAL_ID), GOAL_FINGERPRINT),
    }


def _chain_backlog(
    artifact_id: int,
    status: Literal[
        "draft",
        "pending_review",
        "accepted",
        "rejected",
        "feedback",
        "superseded",
    ],
    parent: int | None,
) -> PhaseArtifactFact:
    return PhaseArtifactFact(
        artifact_type="backlog",
        artifact_id=artifact_id,
        artifact_fingerprint=f"sha256:backlog-{artifact_id}",
        version_number=artifact_id - 100,
        spec_version_id=SPEC_VERSION_ID,
        spec_hash=SPEC_HASH,
        product_goal_artifact_id=GOAL_ID,
        product_goal_fingerprint=GOAL_FINGERPRINT,
        supersedes_artifact_id=parent,
        status=status,
    )


def _chain_planning(
    artifact_type: Literal["roadmap", "story", "sprint_plan"],
    artifact_id: int,
    status: Literal[
        "pending_review",
        "accepted",
        "rejected",
        "feedback",
        "superseded",
    ],
    parent: int | None,
) -> PlanningArtifactFact:
    values: dict[str, object] = {
        "artifact_type": artifact_type,
        "artifact_id": artifact_id,
        "artifact_fingerprint": f"sha256:{artifact_type}-{artifact_id}",
        "version_number": artifact_id % 10,
        "source_fingerprint": "sha256:source",
        "supersedes_artifact_id": parent,
        "status": status,
    }
    if artifact_type == "sprint_plan":
        values.update(
            spec_version_id=SPEC_VERSION_ID,
            spec_hash=SPEC_HASH,
            sprint_plan_stream_id="SPS-0123456789abcdef0123456789abcdef",
            activated_sprint_id=601,
        )
    else:
        values.update(
            backlog_artifact_id=101,
            backlog_artifact_fingerprint="sha256:backlog-101",
        )
    if artifact_type == "story":
        values["backlog_item_id"] = "PBI-000001"
    return PlanningArtifactFact.model_validate(values)


def test_backlog_accepted_a_survives_feedback_b_until_accepted_c() -> None:
    """Keep accepted A current until transitive accepted descendant C exists."""
    base = _accepted_snapshot()
    accepted_a = _chain_backlog(101, "accepted", None)
    feedback_b = _chain_backlog(102, "feedback", 101)
    accepted_c = _chain_backlog(103, "accepted", 102)
    after_feedback = current_backlog_lineage(
        base.model_copy(update={"phase_artifacts": (accepted_a, feedback_b)})
    )
    after_acceptance = current_backlog_lineage(
        base.model_copy(
            update={
                "phase_artifacts": (
                    accepted_a.model_copy(update={"status": "superseded"}),
                    feedback_b,
                    accepted_c,
                )
            }
        )
    )
    assert after_feedback.backlog == accepted_a
    assert after_feedback.latest == feedback_b
    assert after_acceptance.backlog == accepted_c


@pytest.mark.parametrize(
    ("artifact_type", "ids", "backlog_item_id"),
    [
        ("roadmap", (201, 202, 203), None),
        ("story", (301, 302, 303), "PBI-000001"),
        ("sprint_plan", (401, 402, 403), None),
    ],
)
def test_planning_accepted_a_survives_feedback_b_until_accepted_c(
    artifact_type: Literal["roadmap", "story", "sprint_plan"],
    ids: tuple[int, int, int],
    backlog_item_id: str | None,
) -> None:
    """Apply the accepted-leaf ancestry matrix to every planning chain."""
    base = _accepted_snapshot().model_copy(
        update={
            "phase_artifacts": (_chain_backlog(101, "accepted", None),),
            "sprints": (
                (SprintFact(sprint_id=601, status="planned", completed_at=None),)
                if artifact_type == "sprint_plan"
                else ()
            ),
        }
    )
    accepted_a = _chain_planning(artifact_type, ids[0], "accepted", None)
    feedback_b = _chain_planning(artifact_type, ids[1], "feedback", ids[0])
    accepted_c = _chain_planning(artifact_type, ids[2], "accepted", ids[1])
    after_feedback = _artifact_state(
        base.model_copy(update={"planning_artifacts": (accepted_a, feedback_b)}),
        artifact_type,
        backlog_item_id=backlog_item_id,
    )
    after_acceptance = _artifact_state(
        base.model_copy(
            update={
                "planning_artifacts": (
                    accepted_a.model_copy(update={"status": "superseded"}),
                    feedback_b,
                    accepted_c,
                )
            }
        ),
        artifact_type,
        backlog_item_id=backlog_item_id,
    )
    assert after_feedback.accepted == accepted_a
    assert after_feedback.latest == feedback_b
    assert after_acceptance.accepted == accepted_c


def test_story_generation_uses_accepted_roadmap_behind_feedback_leaf() -> None:
    """A feedback child must not make its accepted parent unusable downstream."""
    backlog = _chain_backlog(101, "accepted", None)
    accepted_roadmap = _chain_planning("roadmap", 201, "accepted", None).model_copy(
        update={
            "source_artifact_id": backlog.artifact_id,
            "source_fingerprint": backlog.artifact_fingerprint,
        }
    )
    feedback_roadmap = _chain_planning("roadmap", 202, "feedback", 201)
    snapshot = _accepted_snapshot().model_copy(
        update={
            "phase_artifacts": (backlog,),
            "backlog_items": (
                BacklogItemFact(
                    backlog_item_id="PBI-000001",
                    backlog_artifact_id=backlog.artifact_id,
                    backlog_artifact_fingerprint=backlog.artifact_fingerprint,
                    item_fingerprint="sha256:backlog-item",
                    spec_item_ids=("REQ-001",),
                    priority=1,
                ),
            ),
            "planning_artifacts": (accepted_roadmap, feedback_roadmap),
        }
    )

    decision = next(
        item
        for item in planning_graph().evaluate(snapshot, EVALUATED_AT).decisions
        if item.node_id == "planning.story.generate"
        and item.instance_key == "backlog_item:PBI-000001"
    )

    assert decision.category is NodeCategory.AVAILABLE
    assert decision.reason_code == "STORY_GENERATION_REQUIRED"
    assert ("roadmap", "201", "sha256:roadmap-201") in {
        (item.fact_type, item.fact_id, item.fingerprint)
        for item in decision.fact_references
    }


def test_repository_supersedes_only_transitive_accepted_ancestors() -> None:
    """Repository display status follows accepted ancestry, not physical leaves."""
    accepted_a_id = 101
    feedback_b_id = 102
    accepted_c_id = 103
    key = (PROJECT_ID, SPEC_VERSION_ID, SPEC_HASH)
    accepted_a = ArtifactLineageNode(
        artifact_id=accepted_a_id,
        chain_key=key,
        version_number=1,
        decision="accepted",
    )
    feedback_b = ArtifactLineageNode(
        artifact_id=feedback_b_id,
        chain_key=key,
        version_number=2,
        supersedes_artifact_id=accepted_a_id,
        decision="feedback",
    )
    accepted_c = ArtifactLineageNode(
        artifact_id=accepted_c_id,
        chain_key=key,
        version_number=3,
        supersedes_artifact_id=feedback_b_id,
        decision="accepted",
    )

    after_feedback = WorkflowFactRepository._superseded_accepted_ids(
        (accepted_a, feedback_b)
    )
    after_acceptance = WorkflowFactRepository._superseded_accepted_ids(
        (accepted_a, feedback_b, accepted_c)
    )

    assert after_feedback == frozenset()
    assert (
        WorkflowFactRepository._phase_status(
            "accepted", superseded=accepted_a_id in after_feedback
        )
        == "accepted"
    )
    assert after_acceptance == frozenset({accepted_a_id})
    assert (
        WorkflowFactRepository._phase_status(
            "accepted", superseded=accepted_a_id in after_acceptance
        )
        == "superseded"
    )
    assert (
        WorkflowFactRepository._phase_status(
            "accepted", superseded=accepted_c_id in after_acceptance
        )
        == "accepted"
    )
    assert (
        WorkflowFactRepository._phase_status(
            "feedback", superseded=feedback_b_id in after_acceptance
        )
        == "feedback"
    )


def _active_old_lineage_snapshot(
    *, selected_story_ids: tuple[int, ...] = (71,)
) -> WorkflowFactSnapshot:
    now = datetime(2026, 8, 21, 12, tzinfo=UTC)
    return WorkflowFactSnapshot(
        project=ProjectFact(project_id=7, name="Project", created_at=now),
        planning_artifacts=(
            PlanningArtifactFact(
                artifact_type="sprint_plan",
                artifact_id=81,
                artifact_fingerprint="sha256:plan",
                version_number=1,
                source_fingerprint="sha256:candidates",
                spec_version_id=41,
                spec_hash="sha256:old-spec",
                sprint_plan_stream_id="SPS-0123456789abcdef0123456789abcdef",
                selected_story_ids=(71,),
                activated_sprint_id=91,
                candidate_set_fingerprint="sha256:candidates",
                task_content_fingerprint="sha256:tasks",
                status="accepted",
            ),
        ),
        sprints=(SprintFact(sprint_id=91, status="active", completed_at=None),),
        sprint_starts=(
            SprintStartFact(
                start_id=1,
                sprint_id=91,
                spec_version_id=41,
                spec_hash="sha256:old-spec",
                sprint_plan_artifact_id=81,
                sprint_plan_artifact_decision_id=82,
                story_dependency_review_id=83,
                plan_fingerprint="sha256:plan",
                candidate_set_fingerprint="sha256:candidates",
                selected_story_ids=selected_story_ids,
                task_content_fingerprint="sha256:tasks",
                dependency_source_fingerprint="sha256:dependency-source",
                dependency_fingerprint="sha256:dependencies",
                dependency_rows_fingerprint="sha256:dependency-rows",
                dependency_rows_snapshot=(),
                decision_fingerprint="sha256:decision",
                audit_event_id=84,
                audit_event_fingerprint="sha256:audit",
                started_by="operator",
                started_at=now,
            ),
        ),
        stories=(
            StoryFact(
                story_id=71,
                is_superseded=False,
                source_story_artifact_id=61,
                source_story_artifact_fingerprint="sha256:story-artifact",
                source_story_item_id="US-000001",
                source_story_item_fingerprint="sha256:story-item",
                accepted_spec_version_id=41,
                accepted_spec_hash="sha256:old-spec",
                spec_item_ids=("REQ.001",),
                content_fingerprint="sha256:story",
                content_accepted=True,
                story_artifact_id=61,
                status="To Do",
                sprint_ids=(91,),
                structurally_eligible=True,
                structural_eligibility_status="eligible",
                sprint_selection_state="unselected",
                sprint_selection_state_fingerprint="sha256:selection-state",
                sprint_candidate=False,
                readiness_blockers=(),
            ),
        ),
        tasks=(
            TaskFact(
                task_id=101,
                sprint_id=91,
                story_id=71,
                description="Finish pinned work",
                metadata_json="{}",
                status="To Do",
                dependencies_satisfied=True,
            ),
        ),
    )


def test_active_superseded_lineage_requires_matching_sprint_start_membership() -> None:
    """Allow old-lineage execution only for exact persisted start membership."""
    assert _active_sprint_lineage_is_proven(
        _active_old_lineage_snapshot(), 91, story_id=71
    )
    assert not _active_sprint_lineage_is_proven(
        _active_old_lineage_snapshot(selected_story_ids=(999,)), 91, story_id=71
    )


def test_loose_old_story_cannot_borrow_active_sprint_exception() -> None:
    """Reject an unattached old-lineage Story from the active Sprint exception."""
    snapshot = _active_old_lineage_snapshot()
    loose = snapshot.stories[0].model_copy(
        update={"story_id": 72, "sprint_ids": (), "source_story_item_id": "US-000002"}
    )
    assert not _active_sprint_lineage_is_proven(
        snapshot.model_copy(update={"stories": (*snapshot.stories, loose)}),
        91,
        story_id=72,
    )


def _guards(*, instance_key: str | None = None) -> dict[str, Any]:
    return {
        "project_id": GUARD_PROJECT_ID,
        "graph_version": "workflow.v1",
        "fact_fingerprint": "sha256:facts",
        "decision_fingerprint": "sha256:decision",
        "instance_key": instance_key,
        "actor": "operator",
        "correlation_id": "task-5",
        "idempotency_key": "task-5-request",
    }


def _fail_if_deferred_module_is_imported(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
) -> None:
    module = ModuleType(module_name)

    def fail_on_attribute(name: str) -> object:
        pytest.fail(
            f"{module_name}.{name} imported before Task 5 guards failed"  # ty: ignore[invalid-argument-type]
        )

    module.__dict__["__getattr__"] = fail_on_attribute
    monkeypatch.setitem(sys.modules, module_name, module)


def _sprint_planner_output(story_id: int) -> SprintPlannerOutput:
    """Build the smallest current Task 10 planner contract for one Story."""
    return SprintPlannerOutput.model_validate(
        {
            "sprint_goal": "Preserve direct-Specification lineage.",
            "selected_stories": [
                {
                    "story_id": story_id,
                    "story_item_id": "US-0001",
                    "tasks": [
                        {
                            "description": "Implement the selected Story.",
                            "relevant_spec_item_ids": ["REQ-001"],
                            "task_kind": "implementation",
                            "artifact_targets": ["workflow"],
                            "workstream_tags": ["planning"],
                            "checklist_items": ["Run focused tests."],
                        }
                    ],
                    "reason_for_selection": "The Story is ready for planning.",
                }
            ],
        }
    )


def test_direct_specification_requests_have_no_authority_compatibility_fields() -> None:
    """Expose only direct Specification and immutable planning identities."""
    backlog = RecordBacklogDraft(
        **_guards(),
        spec_version_id=41,
        spec_hash="sha256:spec",
        product_goal_artifact_id=31,
        product_goal_fingerprint="sha256:goal",
        canonical_content={},
        content_fingerprint="sha256:backlog",
    )
    story = RecordStoryDraft(
        **_guards(instance_key="backlog_item:PBI-000001"),
        backlog_item_id="PBI-000001",
        source_backlog_artifact_id=51,
        source_backlog_artifact_fingerprint="sha256:backlog",
        roadmap_artifact_id=61,
        roadmap_artifact_fingerprint="sha256:roadmap",
        canonical_content={},
        content_fingerprint="sha256:story",
    )
    sprint = RecordSprintPlan(
        **_guards(),
        spec_version_id=41,
        spec_hash="sha256:spec",
        team_name="Team",
        planner_output=_sprint_planner_output(71),
    )
    start = StartSprint(**_guards())

    for request in (backlog, story, sprint, start):
        assert "authority_id" not in type(request).model_fields
        assert "authority_fingerprint" not in type(request).model_fields
    assert story.decision_instance_key() == "backlog_item:PBI-000001"
    assert "sprint_id" not in type(start).model_fields


@pytest.mark.parametrize(
    ("backlog_item_id", "source_fingerprint", "roadmap_fingerprint"),
    [
        ("PBI-FOREIGN", "sha256:backlog", "sha256:roadmap"),
        ("PBI-000001", "sha256:stale-backlog", "sha256:roadmap"),
        ("PBI-000001", "sha256:backlog", "sha256:stale-roadmap"),
    ],
)
def test_story_handler_rejects_foreign_or_stale_parent_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
    backlog_item_id: str,
    source_fingerprint: str,
    roadmap_fingerprint: str,
) -> None:
    """Exact parent guards fail before the Task 8 persistence seam imports."""
    snapshot = SimpleNamespace(
        backlog_items=(
            BacklogItemFact(
                backlog_item_id="PBI-000001",
                backlog_artifact_id=51,
                backlog_artifact_fingerprint="sha256:backlog",
                item_fingerprint="sha256:item",
                spec_item_ids=("SPEC-001",),
                priority=1,
            ),
        )
    )
    monkeypatch.setattr(
        "workflow.handlers.planning.WorkflowFactRepository.load",
        lambda _repository, _project_id: snapshot,
    )

    class _Session:
        def get(self, _model: object, _artifact_id: int) -> object:
            return SimpleNamespace(
                project_id=7,
                content_fingerprint="sha256:roadmap",
                backlog_artifact_id=51,
                backlog_artifact_fingerprint="sha256:backlog",
            )

    request = RecordStoryDraft(
        **_guards(instance_key=f"backlog_item:{backlog_item_id}"),
        backlog_item_id=backlog_item_id,
        source_backlog_artifact_id=51,
        source_backlog_artifact_fingerprint=source_fingerprint,
        roadmap_artifact_id=61,
        roadmap_artifact_fingerprint=roadmap_fingerprint,
        canonical_content={},
        content_fingerprint="sha256:story",
    )
    decision = SimpleNamespace(
        fact_references=(
            FactReference(
                fact_type="backlog_item",
                fact_id="PBI-000001",
                fingerprint="sha256:item",
            ),
            FactReference(
                fact_type="roadmap",
                fact_id="61",
                fingerprint="sha256:roadmap",
            ),
        )
    )

    result = execute_record_story_draft(
        cast("Any", _Session()),
        request,
        cast("Any", decision),
        datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT


@pytest.mark.parametrize(
    "case",
    [
        (52, "sha256:backlog", 52, "sha256:backlog", 61),
        (51, "sha256:stale-backlog", 51, "sha256:backlog", 61),
        (51, "sha256:backlog", 52, "sha256:backlog", 61),
        (51, "sha256:backlog", 51, "sha256:stale-backlog", 61),
        (51, "sha256:backlog", 51, "sha256:backlog", 62),
    ],
)
def test_roadmap_handler_rejects_stale_parent_before_deferred_import(
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[int, str, int, str, int],
) -> None:
    """Reject foreign, stale, or wrong-parent Roadmaps before Task 7 imports."""
    (
        backlog_artifact_id,
        request_fingerprint,
        reference_id,
        reference_fingerprint,
        supersedes_id,
    ) = case
    _fail_if_deferred_module_is_imported(
        monkeypatch,
        "services.agent_workbench.roadmap_phase",
    )

    class _Result:
        def one_or_none(self) -> object:
            return SimpleNamespace()

    class _Session:
        def __init__(self) -> None:
            self.writes: list[object] = []

        def get(self, _model: object, artifact_id: int) -> object | None:
            if artifact_id != BACKLOG_ARTIFACT_ID:
                return None
            return SimpleNamespace(
                project_id=GUARD_PROJECT_ID,
                content_fingerprint="sha256:backlog",
            )

        def exec(self, _statement: object) -> _Result:
            return _Result()

    request = RecordRoadmapDraft(
        **_guards(),
        backlog_artifact_id=backlog_artifact_id,
        backlog_artifact_fingerprint=request_fingerprint,
        canonical_content={},
        content_fingerprint="sha256:roadmap",
        supersedes_roadmap_artifact_id=supersedes_id,
    )
    decision = SimpleNamespace(
        fact_references=(
            FactReference(
                fact_type="backlog",
                fact_id=str(reference_id),
                fingerprint=reference_fingerprint,
            ),
            FactReference(
                fact_type="roadmap",
                fact_id="61",
                fingerprint="sha256:prior-roadmap",
            ),
        )
    )
    session = _Session()

    result = planning_handlers.execute_record_roadmap_draft(
        cast("Any", session),
        request,
        cast("Any", decision),
        datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert session.writes == []


@pytest.mark.parametrize(
    ("case", "service_owned", "expected_code"),
    [
        (
            (42, "sha256:spec", 71, 42, "sha256:spec", "sha256:candidates", 81),
            False,
            WorkflowErrorCode.STALE_SPECIFICATION,
        ),
        (
            (
                41,
                "sha256:stale-spec",
                71,
                41,
                "sha256:stale-spec",
                "sha256:candidates",
                81,
            ),
            False,
            WorkflowErrorCode.STALE_SPECIFICATION,
        ),
        (
            (41, "sha256:spec", 72, 41, "sha256:spec", "sha256:candidates", 81),
            True,
            WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
        ),
        (
            (41, "sha256:spec", 71, 42, "sha256:spec", "sha256:candidates", 81),
            False,
            WorkflowErrorCode.STALE_SPECIFICATION,
        ),
        (
            (
                41,
                "sha256:spec",
                71,
                41,
                "sha256:stale-spec",
                "sha256:candidates",
                81,
            ),
            False,
            WorkflowErrorCode.STALE_SPECIFICATION,
        ),
        (
            (
                41,
                "sha256:spec",
                71,
                41,
                "sha256:spec",
                "sha256:stale-candidates",
                81,
            ),
            True,
            WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
        ),
        (
            (41, "sha256:spec", 71, 41, "sha256:spec", "sha256:candidates", 82),
            True,
            WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
        ),
    ],
)
def test_sprint_plan_handler_rejects_stale_guards_before_deferred_import(
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[int, str, int, int, str, str, int],
    service_owned: bool,
    expected_code: WorkflowErrorCode,
) -> None:
    """Reject stale Spec locally and delegated planning lineage at Task 10."""
    (
        spec_version_id,
        spec_hash,
        selected_story_id,
        spec_reference_id,
        spec_reference_hash,
        candidate_reference,
        supersedes_id,
    ) = case
    if service_owned:
        task10_module = ModuleType("services.agent_workbench.sprint_phase")

        class _SprintPlanStreamCollisionError(ValueError):
            pass

        class _StaleSpecificationError(ValueError):
            pass

        def record_sprint_plan_in_session(
            _session: object,
            *,
            inputs: object,
        ) -> None:
            assert isinstance(inputs, SimpleNamespace)
            assert inputs.planner_output.selected_stories[0].story_id == (
                selected_story_id
            )
            message = "Task 10 rejected stale candidate or parent lineage"
            raise ValueError(message)

        task10_module.__dict__["RecordSprintPlanInput"] = SimpleNamespace
        task10_module.__dict__["SprintPlanStreamCollisionError"] = (
            _SprintPlanStreamCollisionError
        )
        task10_module.__dict__["StaleSpecificationError"] = _StaleSpecificationError
        task10_module.__dict__["record_sprint_plan_in_session"] = (
            record_sprint_plan_in_session
        )
        monkeypatch.setitem(
            sys.modules,
            "services.agent_workbench.sprint_phase",
            task10_module,
        )
    else:
        _fail_if_deferred_module_is_imported(
            monkeypatch,
            "services.agent_workbench.sprint_phase",
        )
    snapshot = SimpleNamespace(
        stories=(SimpleNamespace(story_id=71, sprint_candidate=True),),
        story_dependencies=(),
    )
    monkeypatch.setattr(
        "workflow.handlers.planning.WorkflowFactRepository.load",
        lambda _repository, _project_id: snapshot,
    )
    monkeypatch.setattr(
        planning_handlers,
        "accepted_current_spec",
        lambda _snapshot: SimpleNamespace(
            spec_version_id=GUARD_SPEC_VERSION_ID,
            spec_hash="sha256:spec",
        ),
    )
    monkeypatch.setattr(
        planning_handlers,
        "candidate_set_fingerprint",
        lambda _stories, _dependencies: "sha256:candidates",
    )
    request = RecordSprintPlan(
        **_guards(),
        spec_version_id=spec_version_id,
        spec_hash=spec_hash,
        team_name="Team",
        planner_output=_sprint_planner_output(selected_story_id),
    )
    decision = SimpleNamespace(
        fact_references=(
            FactReference(
                fact_type="specification",
                fact_id=str(spec_reference_id),
                fingerprint=spec_reference_hash,
            ),
            FactReference(
                fact_type="candidate_set",
                fact_id=str(GUARD_PROJECT_ID),
                fingerprint=candidate_reference,
            ),
            FactReference(
                fact_type="sprint_plan",
                fact_id=str(supersedes_id),
                fingerprint="sha256:prior-plan",
            ),
        )
    )
    session = SimpleNamespace(writes=[])

    result = planning_handlers.execute_record_sprint_plan(
        cast("Any", session),
        request,
        cast("Any", decision),
        datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is expected_code
    assert session.writes == []


@pytest.mark.parametrize(
    "case",
    [
        (42, "sha256:spec", 31, "sha256:goal", None),
        (41, "sha256:stale-spec", 31, "sha256:goal", None),
        (41, "sha256:spec", 32, "sha256:goal", None),
        (41, "sha256:spec", 31, "sha256:stale-goal", None),
        (41, "sha256:spec", 31, "sha256:goal", "specification"),
        (41, "sha256:spec", 31, "sha256:goal", "product_goal"),
    ],
)
def test_backlog_handler_rejects_stale_lineage_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[int, str, int, str, str | None],
) -> None:
    """Reject stale Specification, Goal, or graph references before Task 7."""
    spec_version_id, spec_hash, goal_id, goal_fingerprint, bad_ref = case

    def accepted_specification(
        _session: object,
        *,
        project_id: int,
        spec_version_id: int,
        spec_hash: str,
    ) -> bool:
        return (
            project_id == GUARD_PROJECT_ID
            and spec_version_id == GUARD_SPEC_VERSION_ID
            and spec_hash == "sha256:spec"
        )

    def accepted_goal(
        _session: object,
        *,
        project_id: int,
        product_goal_artifact_id: int,
        product_goal_fingerprint: str,
    ) -> object | None:
        if (
            project_id == GUARD_PROJECT_ID
            and product_goal_artifact_id == GUARD_GOAL_ID
            and product_goal_fingerprint == "sha256:goal"
        ):
            return SimpleNamespace()
        return None

    monkeypatch.setattr(
        product_handlers,
        "_accepted_specification",
        accepted_specification,
    )
    monkeypatch.setattr(
        product_handlers,
        "_accepted_goal",
        accepted_goal,
    )
    task7_module = ModuleType("services.agent_workbench.backlog_phase")

    def fail_if_persistence_runs(*_args: object, **_kwargs: object) -> None:
        pytest.fail(
            "Task 7 persistence was reached before Task 5 guards failed"  # ty: ignore[invalid-argument-type]
        )

    task7_module.__dict__["record_backlog_draft_in_session"] = fail_if_persistence_runs
    monkeypatch.setitem(
        sys.modules,
        "services.agent_workbench.backlog_phase",
        task7_module,
    )
    request = RecordBacklogDraft(
        **_guards(),
        spec_version_id=spec_version_id,
        spec_hash=spec_hash,
        product_goal_artifact_id=goal_id,
        product_goal_fingerprint=goal_fingerprint,
        canonical_content={},
        content_fingerprint="sha256:backlog",
    )
    decision = SimpleNamespace(
        fact_references=(
            FactReference(
                fact_type="specification",
                fact_id=str(spec_version_id),
                fingerprint=(
                    "sha256:wrong-reference"
                    if bad_ref == "specification"
                    else spec_hash
                ),
            ),
            FactReference(
                fact_type="product_goal",
                fact_id=str(goal_id),
                fingerprint=(
                    "sha256:wrong-reference"
                    if bad_ref == "product_goal"
                    else goal_fingerprint
                ),
            ),
        )
    )
    session = SimpleNamespace(writes=[])

    result = product_handlers.execute_record_backlog_draft(
        cast("Any", session),
        request,
        cast("Any", decision),
        datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert session.writes == []
