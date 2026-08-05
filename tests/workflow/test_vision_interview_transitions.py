"""Durable transition tests for the isolated Project Vision lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from sqlmodel import Session, select

from models.core import Project
from models.product_definition import (
    DiscoveryArtifact,
    ProductGoalArtifact,
    ProductGoalArtifactDecision,
    ProductGoalInterviewTurn,
    ProductGoalOutcome,
    SpecificationCandidate,
    SpecificationDecision,
    VisionArtifact,
    VisionArtifactDecision,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from models.specs import SpecRegistry
from repositories.workflow import WorkflowFactRepository
from services.application import (
    AgileForgeApplication,
    VisionInterviewRequest,
)
from services.node_attempt_replay import (
    DurableNodeAttemptReplayService,
    NodeAttemptReplayQuery,
)
from services.vision_interview_input import VisionInterviewInputService
from workflow.clock import FixedClock
from workflow.contracts import WorkflowErrorCode
from workflow.definitions.product_definition import product_definition_graph
from workflow.domain import WorkflowDomain
from workflow.fingerprints import (
    canonical_hash,
    canonical_json,
    product_goal_artifact_fingerprint,
    product_goal_interview_output_fingerprint,
)
from workflow.requests import (
    BeginVisionRevision,
    DecideVisionReview,
    RecordVisionInterviewTurn,
    StartNodeAttempt,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from workflow.contracts import JsonObject, NodeDecision, TransitionResult

NOW = datetime(2026, 8, 5, 12, tzinfo=UTC)
COMPONENTS: JsonObject = {
    "project_name": "Vision transitions",
    "target_user": "Operators",
    "problem": "State drift",
    "product_category": "Tool",
    "key_benefit": "Trust",
    "competitors": "Spreadsheets",
    "differentiator": "Typed facts",
}
EXPECTED_VISION_ARTIFACT_COUNT = 2


@dataclass(frozen=True)
class _RecordRequest:
    complete: bool
    key: str
    mode: Literal["initial", "revision"] = "initial"
    components: JsonObject = field(default_factory=lambda: dict(COMPONENTS))
    statement: str = "A trusted workflow tool."


@dataclass(frozen=True)
class _VisionReview:
    artifact_id: int
    fingerprint: str
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: str
    idempotency_key: str


@dataclass(frozen=True)
class _GoalLineage:
    project_id: int
    vision_artifact_id: int
    vision_fingerprint: str
    attempt_id: int
    attempt_fingerprint: str
    revised_vision_artifact_id: int
    revised_vision_fingerprint: str


@dataclass(frozen=True)
class _ResolvedGoalSpecificationLineage:
    product_goal_artifact_id: int
    product_goal_fingerprint: str
    spec_version_id: int


class _Registry:
    def require(self, node_id: str) -> object:
        assert node_id == "vision.interview"
        return object()


def _seed_accepted_goal(
    session: Session,
    lineage: _GoalLineage,
) -> tuple[int, str]:
    """Persist one accepted Goal anchored to the prior accepted Vision."""
    components: JsonObject = {"outcome": "Deliver durable workflow facts."}
    statement = "Deliver durable workflow facts."
    turn = ProductGoalInterviewTurn(
        project_id=lineage.project_id,
        vision_artifact_id=lineage.vision_artifact_id,
        vision_fingerprint=lineage.vision_fingerprint,
        goal_number=1,
        revision_number=1,
        prior_turn_id=None,
        user_text="Define the original product goal.",
        components_json=canonical_json(components),
        goal_statement=statement,
        is_complete=True,
        clarifying_questions_json=canonical_json([]),
        output_fingerprint=product_goal_interview_output_fingerprint(
            components, statement, True, []
        ),
        workflow_node_attempt_id=lineage.attempt_id,
        attempt_fingerprint=lineage.attempt_fingerprint,
        recorded_at=NOW,
    )
    session.add(turn)
    session.flush()
    assert turn.product_goal_interview_turn_id is not None
    fingerprint = product_goal_artifact_fingerprint(components, statement)
    goal = ProductGoalArtifact(
        project_id=lineage.project_id,
        vision_artifact_id=lineage.vision_artifact_id,
        vision_fingerprint=lineage.vision_fingerprint,
        goal_number=1,
        revision_number=1,
        statement=statement,
        content_fingerprint=fingerprint,
        supersedes_product_goal_artifact_id=None,
        source_interview_turn_id=turn.product_goal_interview_turn_id,
        created_by="operator@example.com",
        created_at=NOW,
    )
    session.add(goal)
    session.flush()
    assert goal.product_goal_artifact_id is not None
    session.add(
        ProductGoalArtifactDecision(
            project_id=lineage.project_id,
            product_goal_artifact_id=goal.product_goal_artifact_id,
            artifact_fingerprint=fingerprint,
            decision="accepted",
            rationale="Accepted original Goal.",
            reviewer="operator@example.com",
            idempotency_key="original-goal-accepted",
            decided_at=NOW + timedelta(seconds=1),
        )
    )
    return goal.product_goal_artifact_id, fingerprint


def _seed_accepted_goal_specification_lineage(
    session: Session,
    lineage: _GoalLineage,
) -> _ResolvedGoalSpecificationLineage:
    """Persist the durable Task 2 Goal through registered-specification lineage."""
    goal_id, goal_fingerprint = _seed_accepted_goal(session, lineage)
    discovery_content: JsonObject = {"discovery": "Original Goal discovery."}
    discovery = DiscoveryArtifact(
        project_id=lineage.project_id,
        vision_artifact_id=lineage.vision_artifact_id,
        vision_fingerprint=lineage.vision_fingerprint,
        product_goal_artifact_id=goal_id,
        product_goal_fingerprint=goal_fingerprint,
        canonical_content_json=canonical_json(discovery_content),
        content_fingerprint=canonical_hash(discovery_content),
        content_ref="discovery.md",
        producer="test",
        supersedes_discovery_artifact_id=None,
        recorded_by="operator@example.com",
        recorded_at=NOW + timedelta(seconds=1, microseconds=1),
    )
    session.add(discovery)
    session.flush()
    assert discovery.discovery_artifact_id is not None
    candidate_content: JsonObject = {"specification": "Original Goal scope."}
    candidate = SpecificationCandidate(
        project_id=lineage.project_id,
        vision_artifact_id=lineage.vision_artifact_id,
        vision_fingerprint=lineage.vision_fingerprint,
        product_goal_artifact_id=goal_id,
        product_goal_fingerprint=goal_fingerprint,
        discovery_artifact_id=discovery.discovery_artifact_id,
        discovery_fingerprint=discovery.content_fingerprint,
        base_spec_version_id=None,
        base_spec_hash=None,
        canonical_content_json=canonical_json(candidate_content),
        content_fingerprint=canonical_hash(candidate_content),
        content_ref="specification.json",
        supersedes_specification_candidate_id=None,
        recorded_by="operator@example.com",
        recorded_at=NOW + timedelta(seconds=1, microseconds=2),
    )
    session.add(candidate)
    session.flush()
    assert candidate.specification_candidate_id is not None
    session.add(
        SpecificationDecision(
            project_id=lineage.project_id,
            specification_candidate_id=candidate.specification_candidate_id,
            artifact_fingerprint=candidate.content_fingerprint,
            decision="accepted",
            rationale="Original Goal specification accepted.",
            reviewer="operator@example.com",
            idempotency_key="original-goal-specification-accepted",
            decided_at=NOW + timedelta(seconds=1, microseconds=3),
        )
    )
    spec_content = canonical_json(candidate_content)
    registered_spec = SpecRegistry(
        project_id=lineage.project_id,
        spec_hash=canonical_hash(candidate_content),
        content=spec_content,
        status="approved",
        approved_at=NOW + timedelta(seconds=1, microseconds=4),
        approved_by="operator@example.com",
        source_specification_candidate_id=candidate.specification_candidate_id,
        source_vision_artifact_id=lineage.vision_artifact_id,
        source_vision_fingerprint=lineage.vision_fingerprint,
        source_product_goal_artifact_id=goal_id,
        source_product_goal_fingerprint=goal_fingerprint,
        source_discovery_artifact_id=discovery.discovery_artifact_id,
        source_discovery_fingerprint=discovery.content_fingerprint,
        supersedes_spec_version_id=None,
    )
    session.add(registered_spec)
    session.flush()
    assert registered_spec.spec_version_id is not None
    return _ResolvedGoalSpecificationLineage(
        product_goal_artifact_id=goal_id,
        product_goal_fingerprint=goal_fingerprint,
        spec_version_id=registered_spec.spec_version_id,
    )


class _PositionMustNotRunDomain:
    """Fail when a durable replay attempts to derive current graph state."""

    def position(self, project_id: int) -> object:
        del project_id
        message = "receipt replay must happen before position reads"
        raise AssertionError(message)

    def transition(self, request: object) -> TransitionResult:
        del request
        message = "receipt replay must happen before transitions"
        raise AssertionError(message)


def _domain(engine: Engine) -> WorkflowDomain:
    return WorkflowDomain(
        engine=engine,
        graph=product_definition_graph(),
        clock=FixedClock(now_value=NOW),
        adk_recipe_registry=_Registry(),
    )


def _decision(domain: WorkflowDomain, project_id: int, node_id: str) -> NodeDecision:
    return next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == node_id
    )


def _start(
    domain: WorkflowDomain,
    project_id: int,
    key: str,
    *,
    mode: Literal["initial", "revision"] = "initial",
) -> tuple[StartNodeAttempt, TransitionResult]:
    position = domain.position(project_id)
    decision = _decision(domain, project_id, "vision.interview")
    request = StartNodeAttempt(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=decision.decision_fingerprint,
        idempotency_key=key,
        actor="operator@example.com",
        target_node_id="vision.interview",
        target_instance_key=decision.instance_key,
        normalized_input={"mode": mode, "user_response": "Build a tool."},
        model_id="fake/vision",
        execution_settings={"timeout_seconds": 5.0, "max_attempts": 1},
        lease_seconds=60,
    )
    result = domain.transition(request)
    assert result.ok
    return request, result


def _record(
    domain: WorkflowDomain,
    start: StartNodeAttempt,
    result: TransitionResult,
    *,
    request: _RecordRequest,
) -> TransitionResult:
    attempt_id = result.output["attempt_id"]
    attempt_fingerprint = result.output["attempt_fingerprint"]
    assert isinstance(attempt_id, int)
    assert isinstance(attempt_fingerprint, str)
    return domain.transition(
        RecordVisionInterviewTurn(
            project_id=start.project_id,
            graph_version=start.graph_version,
            fact_fingerprint=start.fact_fingerprint,
            decision_fingerprint=start.decision_fingerprint,
            instance_key=start.target_instance_key,
            idempotency_key=request.key,
            actor=start.actor,
            mode=request.mode,
            user_text="Build a tool.",
            updated_components=request.components,
            project_vision_statement=request.statement,
            is_complete=request.complete,
            clarifying_questions=(
                () if request.complete else ("Who is the user?",)
            ),
            attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
        )
    )


def _review_vision(
    domain: WorkflowDomain,
    project_id: int,
    review_request: _VisionReview,
) -> TransitionResult:
    """Submit one review with the current durable Vision guards."""
    position = domain.position(project_id)
    review = _decision(domain, project_id, "vision.review")
    return domain.transition(
        DecideVisionReview(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=review.decision_fingerprint,
            idempotency_key=review_request.idempotency_key,
            actor="operator@example.com",
            vision_artifact_id=review_request.artifact_id,
            vision_fingerprint=review_request.fingerprint,
            decision=review_request.decision,
            rationale=review_request.rationale,
        )
    )


def _assert_active_goal_blocks_revision_acceptance(
    engine: Engine,
    domain: WorkflowDomain,
    lineage: _GoalLineage,
) -> _ResolvedGoalSpecificationLineage:
    """Keep a pending revision unaccepted until its prior Goal is resolved."""
    with Session(engine) as session:
        resolved_lineage = _seed_accepted_goal_specification_lineage(session, lineage)
        session.commit()
    blocked = _review_vision(
        domain,
        lineage.project_id,
        _VisionReview(
            artifact_id=lineage.revised_vision_artifact_id,
            fingerprint=lineage.revised_vision_fingerprint,
            decision="accepted",
            rationale="Revised Vision accepted.",
            idempotency_key="revision-accept-blocked",
        ),
    )

    assert blocked.ok is False
    assert blocked.error is not None
    assert blocked.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    with Session(engine) as session:
        assert len(session.exec(select(VisionArtifactDecision)).all()) == 1
        session.add(
            ProductGoalOutcome(
                project_id=lineage.project_id,
                product_goal_artifact_id=resolved_lineage.product_goal_artifact_id,
                artifact_fingerprint=resolved_lineage.product_goal_fingerprint,
                outcome="fulfilled",
                rationale="Original Goal fulfilled.",
                decided_by="operator@example.com",
                idempotency_key="original-goal-fulfilled",
                decided_at=NOW + timedelta(seconds=2),
            )
        )
        session.commit()
    accepted = _review_vision(
        domain,
        lineage.project_id,
        _VisionReview(
            artifact_id=lineage.revised_vision_artifact_id,
            fingerprint=lineage.revised_vision_fingerprint,
            decision="accepted",
            rationale="Revised Vision accepted.",
            idempotency_key="revision-accept",
        ),
    )

    assert accepted.ok
    current_goal = _decision(domain, lineage.project_id, "goal.interview")
    assert current_goal.fact_references[0].fact_id == str(
        lineage.revised_vision_artifact_id
    )
    available_nodes = domain.position(lineage.project_id).available_nodes
    assert all("discovery" not in node_id for node_id in available_nodes)
    return resolved_lineage


def test_incomplete_then_complete_turn_creates_one_pending_vision(
    engine: Engine,
) -> None:
    """Incomplete turns persist without an artifact; completion creates one review."""
    with Session(engine) as session:
        project = Project(name="Vision transitions", origin="greenfield")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id
    domain = _domain(engine)
    first_start, first_attempt = _start(domain, project_id, "vision-incomplete")

    incomplete = _record(
        domain,
        first_start,
        first_attempt,
        request=_RecordRequest(complete=False, key="record-incomplete"),
    )

    assert incomplete.ok
    assert "vision.interview" in domain.position(project_id).available_nodes
    second_start, second_attempt = _start(domain, project_id, "vision-complete")
    complete = _record(
        domain,
        second_start,
        second_attempt,
        request=_RecordRequest(complete=True, key="record-complete"),
    )

    assert complete.ok
    assert "vision.review" in domain.position(project_id).waiting_nodes
    with Session(engine) as session:
        assert len(session.exec(select(VisionArtifact)).all()) == 1


def test_replay_uses_persisted_after_turn_instance_key(engine: Engine) -> None:
    """A lost later interview start replays without caller-held instance metadata."""
    with Session(engine) as session:
        project = Project(name="Vision replay", origin="greenfield")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id
    domain = _domain(engine)
    first_start, first_attempt = _start(domain, project_id, "vision-first-turn")
    incomplete = _record(
        domain,
        first_start,
        first_attempt,
        request=_RecordRequest(complete=False, key="vision-first-complete"),
    )
    assert incomplete.ok
    second_start, second_attempt = _start(domain, project_id, "vision-later-turn")
    assert second_start.target_instance_key is not None
    assert second_start.target_instance_key.startswith("after-turn:")

    replay = DurableNodeAttemptReplayService(engine=engine).replay(
        NodeAttemptReplayQuery(
            project_id=project_id,
            graph_version=second_start.graph_version,
            fact_fingerprint=second_start.fact_fingerprint,
            decision_fingerprint=second_start.decision_fingerprint,
            node_id="vision.interview",
            idempotency_key=second_start.idempotency_key,
            actor=second_start.actor,
            user_text="Build a tool.",
        )
    )

    assert replay == second_attempt.model_copy(update={"replayed": True})

    changed_input = DurableNodeAttemptReplayService(engine=engine).replay(
        NodeAttemptReplayQuery(
            project_id=project_id,
            graph_version=second_start.graph_version,
            fact_fingerprint=second_start.fact_fingerprint,
            decision_fingerprint=second_start.decision_fingerprint,
            node_id="vision.interview",
            idempotency_key=second_start.idempotency_key,
            actor=second_start.actor,
            user_text="Changed answer.",
        )
    )

    assert changed_input is not None
    assert changed_input.error is not None
    assert changed_input.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT


def test_application_replay_normalizes_retry_user_text_before_position_read(
    engine: Engine,
) -> None:
    """Padded retries use the same Vision input boundary as persisted starts."""
    with Session(engine) as session:
        project = Project(name="Vision replay normalization", origin="greenfield")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id
    domain = _domain(engine)
    decision = _decision(domain, project_id, "vision.interview")
    input_service = VisionInterviewInputService(engine=engine)
    persisted_input = input_service.build(
        project_id=project_id,
        decision=decision,
        user_text="  Same answer.  ",
    )
    start = StartNodeAttempt(
        project_id=project_id,
        graph_version=domain.position(project_id).graph_version,
        fact_fingerprint=domain.position(project_id).fact_fingerprint,
        decision_fingerprint=decision.decision_fingerprint,
        idempotency_key="vision-padded-replay",
        actor="operator@example.com",
        target_node_id="vision.interview",
        target_instance_key=decision.instance_key,
        normalized_input=persisted_input,
        model_id="fake/vision",
        execution_settings={"timeout_seconds": 5.0, "max_attempts": 1},
        lease_seconds=60,
    )
    started = domain.transition(start)
    assert started.ok
    app = object.__new__(AgileForgeApplication)
    app._workflow_domain = _PositionMustNotRunDomain()
    app._vision_interview_input = input_service
    app._prepared_agentic_inputs = type(
        "PreparedVisionInputServices",
        (),
        {"vision_interview": input_service},
    )()
    same_request = VisionInterviewRequest(
        project_id=project_id,
        graph_version=start.graph_version,
        fact_fingerprint=start.fact_fingerprint,
        decision_fingerprint=start.decision_fingerprint,
        user_text="  Same answer.  ",
        idempotency_key=start.idempotency_key,
        actor=start.actor,
    )

    replay = app.run_vision_interview(same_request)
    changed = app.run_vision_interview(
        same_request.model_copy(update={"user_text": "Changed answer."})
    )

    assert replay == started.model_copy(update={"replayed": True})
    assert changed.error is not None
    assert changed.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT


def test_review_accepts_one_vision_exactly_once(engine: Engine) -> None:
    """A review decision targets the graph-selected artifact and is idempotent."""
    with Session(engine) as session:
        project = Project(name="Vision review", origin="greenfield")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id
    domain = _domain(engine)
    start, attempt = _start(domain, project_id, "vision-review-start")
    recorded = _record(
        domain,
        start,
        attempt,
        request=_RecordRequest(complete=True, key="vision-record"),
    )
    artifact_id = recorded.output["vision_artifact_id"]
    fingerprint = recorded.output["vision_fingerprint"]
    assert isinstance(artifact_id, int)
    assert isinstance(fingerprint, str)
    position = domain.position(project_id)
    review = _decision(domain, project_id, "vision.review")
    request = DecideVisionReview(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=review.decision_fingerprint,
        idempotency_key="accept-vision",
        actor="operator@example.com",
        vision_artifact_id=artifact_id,
        vision_fingerprint=fingerprint,
        decision="accepted",
        rationale="Ready for Product Goal.",
    )

    accepted = domain.transition(request)
    replay = domain.transition(request)

    assert accepted.ok
    assert replay.replayed
    assert accepted.position is not None
    assert "goal.interview" in accepted.position.available_nodes
    with Session(engine) as session:
        assert len(session.exec(select(VisionArtifactDecision)).all()) == 1


def test_feedback_reopens_the_same_vision_interview(engine: Engine) -> None:
    """Feedback records one decision and returns the human to Vision interviewing."""
    with Session(engine) as session:
        project = Project(name="Vision feedback", origin="greenfield")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id
    domain = _domain(engine)
    start, attempt = _start(domain, project_id, "feedback-start")
    recorded = _record(
        domain,
        start,
        attempt,
        request=_RecordRequest(complete=True, key="feedback-record"),
    )
    artifact_id = recorded.output["vision_artifact_id"]
    fingerprint = recorded.output["vision_fingerprint"]
    assert isinstance(artifact_id, int)
    assert isinstance(fingerprint, str)
    position = domain.position(project_id)
    review = _decision(domain, project_id, "vision.review")

    feedback = domain.transition(
        DecideVisionReview(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=review.decision_fingerprint,
            idempotency_key="vision-feedback",
            actor="operator@example.com",
            vision_artifact_id=artifact_id,
            vision_fingerprint=fingerprint,
            decision="feedback",
            rationale="Clarify the target audience.",
        )
    )

    assert feedback.ok
    assert "vision.interview" in domain.position(project_id).available_nodes
    with Session(engine) as session:
        assert len(session.exec(select(VisionArtifactDecision)).all()) == 1


def test_accepted_revision_creates_only_a_new_vision(engine: Engine) -> None:
    """Revision completion and acceptance create Vision without a Product Goal."""
    with Session(engine) as session:
        project = Project(name="Vision revision", origin="greenfield")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id
    domain = _domain(engine)
    initial_start, initial_attempt = _start(domain, project_id, "revision-initial")
    initial = _record(
        domain,
        initial_start,
        initial_attempt,
        request=_RecordRequest(complete=True, key="revision-initial-record"),
    )
    artifact_id = initial.output["vision_artifact_id"]
    fingerprint = initial.output["vision_fingerprint"]
    assert isinstance(artifact_id, int)
    assert isinstance(fingerprint, str)
    accepted = _review_vision(
        domain,
        project_id,
        _VisionReview(
            artifact_id=artifact_id,
            fingerprint=fingerprint,
            decision="accepted",
            rationale="Initial Vision accepted.",
            idempotency_key="revision-initial-accept",
        ),
    )
    assert accepted.ok
    revision_position = domain.position(project_id)
    revision = _decision(domain, project_id, "vision.revision.start")
    opened = domain.transition(
        BeginVisionRevision(
            project_id=project_id,
            graph_version=revision_position.graph_version,
            fact_fingerprint=revision_position.fact_fingerprint,
            decision_fingerprint=revision.decision_fingerprint,
            idempotency_key="revision-open",
            actor="operator@example.com",
            source_vision_artifact_id=artifact_id,
            source_vision_fingerprint=fingerprint,
            reason="The market changed.",
        )
    )
    assert opened.ok
    decision = _decision(domain, project_id, "vision.interview")
    assert decision.category.value == "available"
    revision_start, revision_attempt = _start(
        domain,
        project_id,
        "revision-turn",
        mode="revision",
    )
    revised = _record(
        domain,
        revision_start,
        revision_attempt,
        request=_RecordRequest(
            complete=True,
            key="revision-record",
            mode="revision",
            components=COMPONENTS | {"differentiator": "Changed durable facts"},
            statement="A revised trusted workflow tool.",
        ),
    )
    revised_id = revised.output["vision_artifact_id"]
    revised_fingerprint = revised.output["vision_fingerprint"]
    assert isinstance(revised_id, int)
    assert isinstance(revised_fingerprint, str)
    initial_attempt_id = initial_attempt.output["attempt_id"]
    initial_attempt_fingerprint = initial_attempt.output["attempt_fingerprint"]
    assert isinstance(initial_attempt_id, int)
    assert isinstance(initial_attempt_fingerprint, str)
    resolved_lineage = _assert_active_goal_blocks_revision_acceptance(
        engine,
        domain,
        _GoalLineage(
            project_id=project_id,
            vision_artifact_id=artifact_id,
            vision_fingerprint=fingerprint,
            attempt_id=initial_attempt_id,
            attempt_fingerprint=initial_attempt_fingerprint,
            revised_vision_artifact_id=revised_id,
            revised_vision_fingerprint=revised_fingerprint,
        ),
    )
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
        old_goal_ids = {
            item.product_goal_artifact_id
            for item in snapshot.product_goal_artifact_decisions
            if item.decision == "accepted"
        } - {
            item.product_goal_artifact_id for item in snapshot.product_goal_outcomes
        }
        current_specs = tuple(
            item for item in snapshot.spec_versions if item.status == "approved"
        )
        assert resolved_lineage.product_goal_artifact_id not in old_goal_ids
        assert resolved_lineage.spec_version_id not in {
            item.spec_version_id for item in current_specs
        }
        artifacts = session.exec(select(VisionArtifact)).all()
        assert len(artifacts) == EXPECTED_VISION_ARTIFACT_COUNT
        assert artifacts[-1].supersedes_vision_artifact_id == artifact_id
        assert (
            len(session.exec(select(VisionArtifactDecision)).all())
            == EXPECTED_VISION_ARTIFACT_COUNT
        )
        for row_type in (
            SpecificationDecision,
            SpecRegistry,
            SpecificationCandidate,
            DiscoveryArtifact,
            ProductGoalOutcome,
            ProductGoalArtifactDecision,
            ProductGoalArtifact,
            ProductGoalInterviewTurn,
        ):
            for row in session.exec(select(row_type)).all():
                session.delete(row)
        session.flush()
        revision_turns = session.exec(
            select(VisionInterviewTurn).where(VisionInterviewTurn.mode == "revision")
        ).all()
        for turn in revision_turns:
            turn.revision_intent_id = None
            session.add(turn)
        for intent in session.exec(select(VisionRevisionIntent)).all():
            session.delete(intent)
        session.commit()
