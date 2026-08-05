"""Provider-free Product Goal persistence regression tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from sqlmodel import Session, select

from models.core import Project, Sprint, Team
from models.enums import SprintStatus
from models.product_definition import (
    DiscoveryArtifact,
    ProductGoalArtifact,
    ProductGoalInterviewTurn,
    ProductGoalOutcome,
    SpecificationCandidate,
    SpecificationDecision,
    VisionArtifact,
    VisionArtifactDecision,
    VisionInterviewTurn,
)
from models.specs import SpecRegistry
from models.workflow import WorkflowNodeAttempt
from repositories.workflow import WorkflowFactRepository
from workflow.clock import FixedClock
from workflow.contracts import GRAPH_VERSION, FactReference, WorkflowPosition
from workflow.definitions.product_discovery import PRODUCT_DISCOVERY_NODES
from workflow.definitions.product_goal import PRODUCT_GOAL_NODES
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.graph import ChildGraphSpec, WorkflowGraph
from workflow.requests import (
    AbandonProductGoal,
    DecideProductGoalReview,
    DecideSpecification,
    FulfillProductGoal,
    RecordDiscoveryArtifact,
    RecordProductGoalInterviewTurn,
    RecordSpecificationCandidate,
    StartNodeAttempt,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

NOW = datetime(2026, 8, 5, 12, tzinfo=UTC)
_TURN_COUNT = 2
_ARTIFACT_COUNT = 1
_SECOND_GOAL_NUMBER = 2


class _Registry:
    def require(self, node_id: str) -> object:
        assert node_id == "goal.interview"
        return object()


def _domain(engine: Engine, *, at: datetime = NOW) -> WorkflowDomain:
    return WorkflowDomain(
        engine=engine,
        graph=WorkflowGraph(
            graph_version=GRAPH_VERSION,
            root=ChildGraphSpec(
                child_graph_id="goal",
                nodes=(*PRODUCT_GOAL_NODES, *PRODUCT_DISCOVERY_NODES),
            ),
        ),
        clock=FixedClock(now_value=at),
        adk_recipe_registry=_Registry(),
    )


def _seed_accepted_vision(engine: Engine, *, name: str = "Goal reload") -> int:
    with Session(engine) as session:
        project = Project(name=name)
        session.add(project)
        session.flush()
        assert project.project_id is not None
        attempt = WorkflowNodeAttempt(
            project_id=project.project_id,
            node_id="vision.interview",
            instance_key=None,
            graph_version=GRAPH_VERSION,
            fact_fingerprint="sha256:facts",
            business_fact_fingerprint="sha256:business",
            decision_fingerprint="sha256:decision",
            normalized_input_json="{}",
            input_fingerprint="sha256:input",
            model_id="fake/vision",
            execution_settings_json="{}",
            idempotency_key="vision-attempt",
            actor="operator",
            correlation_id=None,
            started_at=NOW,
            lease_expires_at=NOW + timedelta(minutes=1),
            attempt_fingerprint="sha256:vision-attempt",
        )
        session.add(attempt)
        session.flush()
        assert attempt.workflow_node_attempt_id is not None
        components = {"purpose": "durable workflow"}
        turn = VisionInterviewTurn(
            project_id=project.project_id,
            mode="initial",
            turn_number=1,
            revision_intent_id=None,
            prior_turn_id=None,
            user_text="Define Vision",
            components_json=canonical_json(components),
            vision_statement="A durable Vision.",
            is_complete=True,
            clarifying_questions_json="[]",
            output_fingerprint=canonical_hash(
                {
                    "components_json": components,
                    "vision_statement": "A durable Vision.",
                    "is_complete": True,
                    "clarifying_questions_json": [],
                }
            ),
            workflow_node_attempt_id=attempt.workflow_node_attempt_id,
            attempt_fingerprint=attempt.attempt_fingerprint,
            recorded_at=NOW,
        )
        session.add(turn)
        session.flush()
        assert turn.vision_interview_turn_id is not None
        vision = VisionArtifact(
            project_id=project.project_id,
            version_number=1,
            components_json=canonical_json(components),
            statement="A durable Vision.",
            content_fingerprint=canonical_hash(
                {"components": components, "statement": "A durable Vision."}
            ),
            supersedes_vision_artifact_id=None,
            source_interview_turn_id=turn.vision_interview_turn_id,
            created_by="operator",
            created_at=NOW,
        )
        session.add(vision)
        session.flush()
        assert vision.vision_artifact_id is not None
        session.add(
            VisionArtifactDecision(
                project_id=project.project_id,
                vision_artifact_id=vision.vision_artifact_id,
                artifact_fingerprint=vision.content_fingerprint,
                decision="accepted",
                rationale="",
                reviewer="operator",
                idempotency_key="vision-accepted",
                decided_at=NOW + timedelta(seconds=1),
            )
        )
        session.commit()
        return project.project_id


def _record_turn(
    domain: WorkflowDomain,
    project_id: int,
    *,
    complete: bool,
    key: str,
) -> None:
    position = domain.position(project_id)
    decision = next(
        item for item in position.decisions if item.node_id == "goal.interview"
    )
    start = domain.transition(
        StartNodeAttempt(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=decision.decision_fingerprint,
            idempotency_key=f"{key}-start",
            actor="operator",
            target_node_id="goal.interview",
            target_instance_key=None,
            normalized_input={"user_response": "Define goal"},
            model_id="fake/goal",
            execution_settings={"timeout_seconds": 1.0, "max_attempts": 1},
            lease_seconds=60,
        )
    )
    assert start.ok
    attempt_id = start.output["attempt_id"]
    attempt_fingerprint = start.output["attempt_fingerprint"]
    assert isinstance(attempt_id, int)
    assert isinstance(attempt_fingerprint, str)
    result = domain.transition(
        RecordProductGoalInterviewTurn(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=decision.decision_fingerprint,
            idempotency_key=key,
            actor="operator",
            user_text="Define goal",
            updated_components={
                "valuable_future_state": "Reliable decisions",
                "beneficiary": "Operators",
                "value": "Confidence",
                "success_signals": ["Measured outcomes"],
                "boundaries": ["No implementation"],
            },
            product_goal_statement="Operators make reliable decisions.",
            is_complete=complete,
            clarifying_questions=() if complete else ("Who benefits first?",),
            attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
        )
    )
    assert result.ok


def _review(
    domain: WorkflowDomain,
    project_id: int,
    *,
    decision: Literal["accepted", "rejected", "feedback"],
    rationale: str,
    key: str,
) -> tuple[WorkflowPosition, FactReference]:
    """Submit the graph-selected review for the one pending Goal candidate."""
    position = domain.position(project_id)
    review = next(item for item in position.decisions if item.node_id == "goal.review")
    goal = next(
        item for item in review.fact_references if item.fact_type == "product_goal"
    )
    result = domain.transition(
        DecideProductGoalReview(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=review.decision_fingerprint,
            idempotency_key=key,
            actor="operator",
            product_goal_artifact_id=int(goal.fact_id),
            product_goal_fingerprint=goal.fingerprint,
            decision=decision,
            rationale=rationale,
        )
    )
    assert result.ok
    return position, goal


def _fulfill_request(
    domain: WorkflowDomain,
    project_id: int,
    *,
    key: str,
    rationale: str,
) -> FulfillProductGoal:
    """Build an exact positioned fulfillment request from the current snapshot."""
    position = domain.position(project_id)
    outcome = next(
        item for item in position.decisions if item.node_id == "goal.fulfill"
    )
    goal = next(
        item for item in outcome.fact_references if item.fact_type == "product_goal"
    )
    return FulfillProductGoal(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=outcome.decision_fingerprint,
        idempotency_key=key,
        actor="operator",
        product_goal_artifact_id=int(goal.fact_id),
        product_goal_fingerprint=goal.fingerprint,
        rationale=rationale,
    )


def _activate_goal(
    engine: Engine,
    *,
    project_id: int,
    key: str,
) -> tuple[WorkflowDomain, FulfillProductGoal]:
    """Create the exact active Goal needed by outcome transaction tests."""
    interview_domain = _domain(engine)
    _record_turn(interview_domain, project_id, complete=True, key=f"{key}-goal")
    review_domain = _domain(engine, at=NOW + timedelta(seconds=1))
    _review(
        review_domain,
        project_id,
        decision="accepted",
        rationale="",
        key=f"{key}-accept",
    )
    outcome_domain = _domain(engine, at=NOW + timedelta(seconds=2))
    return outcome_domain, _fulfill_request(
        outcome_domain,
        project_id,
        key=f"{key}-fulfill",
        rationale="Delivered.",
    )


def test_goal_turns_reload_after_incomplete_and_complete_transitions(
    engine: Engine,
) -> None:
    """Both persisted output fingerprints survive immediate domain snapshot reload."""
    project_id = _seed_accepted_vision(engine)
    domain = _domain(engine)

    _record_turn(domain, project_id, complete=False, key="incomplete")
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    assert len(snapshot.product_goal_interview_turns) == 1
    after_incomplete = domain.position(project_id)
    assert "goal.interview" in after_incomplete.available_nodes, [
        (item.node_id, item.category.value, item.reason_code)
        for item in after_incomplete.decisions
    ]
    _record_turn(domain, project_id, complete=True, key="complete")

    position = domain.position(project_id)
    assert "goal.review" in position.waiting_nodes
    with Session(engine) as session:
        assert len(session.exec(select(ProductGoalInterviewTurn)).all()) == _TURN_COUNT
        assert len(session.exec(select(ProductGoalArtifact)).all()) == _ARTIFACT_COUNT


def test_goal_feedback_revision_and_outcome_are_exact_and_durable(
    engine: Engine,
) -> None:
    """Feedback stays in one Goal number and creates a valid next revision."""
    project_id = _seed_accepted_vision(engine)
    domain = _domain(engine)
    _record_turn(domain, project_id, complete=True, key="first")
    position = domain.position(project_id)
    review = next(item for item in position.decisions if item.node_id == "goal.review")
    goal_ref = next(
        item for item in review.fact_references if item.fact_type == "product_goal"
    )
    review_domain = _domain(engine, at=NOW + timedelta(seconds=1))
    feedback = review_domain.transition(
        DecideProductGoalReview(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=review.decision_fingerprint,
            idempotency_key="feedback",
            actor="operator",
            product_goal_artifact_id=int(goal_ref.fact_id),
            product_goal_fingerprint=goal_ref.fingerprint,
            decision="feedback",
            rationale="Clarify outcome.",
        )
    )
    assert feedback.ok
    _record_turn(review_domain, project_id, complete=True, key="revision")
    with Session(engine) as session:
        goals = session.exec(select(ProductGoalArtifact)).all()
        assert [(goal.goal_number, goal.revision_number) for goal in goals] == [
            (1, 1),
            (1, 2),
        ]
        assert (
            goals[1].supersedes_product_goal_artifact_id
            == goals[0].product_goal_artifact_id
        )
    assert "goal.review" in review_domain.position(project_id).waiting_nodes


def test_accepted_goal_outcome_replays_and_opposite_writes_nothing(
    engine: Engine,
) -> None:
    """An active Goal has one immutable outcome and then opens Goal number two."""
    project_id = _seed_accepted_vision(engine)
    interview_domain = _domain(engine)
    _record_turn(interview_domain, project_id, complete=True, key="goal")

    review_domain = _domain(engine, at=NOW + timedelta(seconds=1))
    review_position, _ = _review(
        review_domain,
        project_id,
        decision="accepted",
        rationale="",
        key="accept",
    )
    review = next(
        item for item in review_position.decisions if item.node_id == "goal.review"
    )
    assert {item.fact_type for item in review.fact_references} == {
        "vision",
        "product_goal",
    }
    vision = next(item for item in review.fact_references if item.fact_type == "vision")
    activated = review_domain.position(project_id)
    assert "discovery.record" in activated.available_nodes
    assert "goal.interview" not in activated.available_nodes

    outcome_domain = _domain(engine, at=NOW + timedelta(seconds=2))
    fulfill = _fulfill_request(
        outcome_domain,
        project_id,
        key="fulfill",
        rationale="The goal was delivered.",
    )
    result = outcome_domain.transition(fulfill)
    assert result.ok
    assert outcome_domain.transition(fulfill).model_dump() == result.model_copy(
        update={"replayed": True}
    ).model_dump()

    opposite = AbandonProductGoal(
        project_id=fulfill.project_id,
        graph_version=fulfill.graph_version,
        fact_fingerprint=fulfill.fact_fingerprint,
        decision_fingerprint=fulfill.decision_fingerprint,
        idempotency_key="abandon-after-fulfill",
        actor=fulfill.actor,
        product_goal_artifact_id=fulfill.product_goal_artifact_id,
        product_goal_fingerprint=fulfill.product_goal_fingerprint,
        rationale="The goal was abandoned.",
    )
    before = 1
    rejected = outcome_domain.transition(opposite)
    assert not rejected.ok
    with Session(engine) as session:
        outcomes = session.exec(select(ProductGoalOutcome)).all()
        assert len(outcomes) == before
        assert outcomes[0].outcome == "fulfilled"

    next_position = outcome_domain.position(project_id)
    assert "goal.interview" in next_position.available_nodes
    _record_turn(outcome_domain, project_id, complete=True, key="goal-two")
    with Session(engine) as session:
        goal_two = session.exec(
            select(ProductGoalArtifact).where(
                ProductGoalArtifact.goal_number == _SECOND_GOAL_NUMBER
            )
        ).one()
        assert goal_two.vision_fingerprint == vision.fingerprint


def test_goal_outcome_requires_exact_active_goal_and_nonblank_rationale(
    engine: Engine,
) -> None:
    """Rejected exactness and semantic rationale guards leave no outcome row."""
    project_id = _seed_accepted_vision(engine)
    interview_domain = _domain(engine)
    _record_turn(interview_domain, project_id, complete=True, key="goal")
    review_domain = _domain(engine, at=NOW + timedelta(seconds=1))
    _review(
        review_domain,
        project_id,
        decision="accepted",
        rationale="",
        key="accept",
    )
    outcome_domain = _domain(engine, at=NOW + timedelta(seconds=2))
    exact = _fulfill_request(
        outcome_domain,
        project_id,
        key="empty-rationale",
        rationale="valid",
    )
    empty = exact.model_construct(
        None,
        **{**exact.model_dump(), "rationale": " "},
    )
    abandon = AbandonProductGoal(
        project_id=exact.project_id,
        graph_version=exact.graph_version,
        fact_fingerprint=exact.fact_fingerprint,
        decision_fingerprint=exact.decision_fingerprint,
        idempotency_key="empty-abandon-rationale",
        actor=exact.actor,
        product_goal_artifact_id=exact.product_goal_artifact_id,
        product_goal_fingerprint=exact.product_goal_fingerprint,
        rationale="valid",
    )
    empty_abandon = abandon.model_construct(
        None,
        **{**abandon.model_dump(), "rationale": " "},
    )
    wrong = exact.model_copy(
        update={"idempotency_key": "wrong-goal", "product_goal_fingerprint": "wrong"}
    )
    wrong_abandon = abandon.model_copy(
        update={
            "idempotency_key": "wrong-abandon-goal",
            "product_goal_fingerprint": "wrong",
        }
    )

    assert not outcome_domain.transition(empty).ok
    assert not outcome_domain.transition(empty_abandon).ok
    assert not outcome_domain.transition(wrong).ok
    assert not outcome_domain.transition(wrong_abandon).ok
    with Session(engine) as session:
        assert session.exec(select(ProductGoalOutcome)).all() == []


def test_sprint_facts_block_goal_outcomes_without_writes(engine: Engine) -> None:
    """Active and untriaged completed Sprints make an exact outcome stale."""
    for status in (SprintStatus.ACTIVE, SprintStatus.COMPLETED):
        project_id = _seed_accepted_vision(engine, name=f"{status.value} Goal reload")
        outcome_domain, fulfill = _activate_goal(
            engine,
            project_id=project_id,
            key=status.value,
        )
        with Session(engine) as session:
            team = Team(name=f"{status.value} outcome Team")
            session.add(team)
            session.flush()
            assert team.team_id is not None
            session.add(
                Sprint(
                    project_id=project_id,
                    team_id=team.team_id,
                    status=status,
                    started_at=NOW if status == SprintStatus.ACTIVE else None,
                    completed_at=NOW if status == SprintStatus.COMPLETED else None,
                )
            )
            session.commit()

        blocked_position = outcome_domain.position(project_id)
        assert "goal.fulfill" not in blocked_position.available_nodes
        assert "goal.abandon" not in blocked_position.available_nodes
        assert not outcome_domain.transition(fulfill).ok
        with Session(engine) as session:
            assert session.exec(select(ProductGoalOutcome)).all() == []


def test_discovery_specification_acceptance_is_atomic_and_exact(
    engine: Engine,
) -> None:
    """One current Goal writes exact discovery/spec lineage in one transaction."""
    project_id = _seed_accepted_vision(engine)
    domain = _domain(engine)
    _record_turn(domain, project_id, complete=True, key="goal")
    review_position = domain.position(project_id)
    review = next(
        item for item in review_position.decisions if item.node_id == "goal.review"
    )
    goal = next(
        item for item in review.fact_references if item.fact_type == "product_goal"
    )
    accepted_domain = _domain(engine, at=NOW + timedelta(seconds=1))
    assert accepted_domain.transition(
        DecideProductGoalReview(
            project_id=project_id,
            graph_version=review_position.graph_version,
            fact_fingerprint=review_position.fact_fingerprint,
            decision_fingerprint=review.decision_fingerprint,
            idempotency_key="goal-accepted",
            actor="operator",
            product_goal_artifact_id=int(goal.fact_id),
            product_goal_fingerprint=goal.fingerprint,
            decision="accepted",
            rationale="",
        )
    ).ok
    discovery_position = accepted_domain.position(project_id)
    discovery = next(
        item
        for item in discovery_position.decisions
        if item.node_id == "discovery.record"
    )
    assert {item.fact_type for item in discovery.fact_references} == {
        "vision",
        "product_goal",
    }
    assert accepted_domain.transition(
        RecordDiscoveryArtifact(
            project_id=project_id,
            graph_version=discovery_position.graph_version,
            fact_fingerprint=discovery_position.fact_fingerprint,
            decision_fingerprint=discovery.decision_fingerprint,
            idempotency_key="discovery",
            actor="operator",
            canonical_content={"discovery": "current"},
            content_ref=None,
        )
    ).ok
    specification_position = accepted_domain.position(project_id)
    specification = next(
        item
        for item in specification_position.decisions
        if item.node_id == "specification.record"
    )
    assert specification.fact_references[0].fact_type == "discovery"
    assert accepted_domain.transition(
        RecordSpecificationCandidate(
            project_id=project_id,
            graph_version=specification_position.graph_version,
            fact_fingerprint=specification_position.fact_fingerprint,
            decision_fingerprint=specification.decision_fingerprint,
            idempotency_key="specification",
            actor="operator",
            canonical_content={"specification": "current"},
            content_ref=None,
            supersedes_specification_candidate_id=None,
        )
    ).ok
    pending = accepted_domain.position(project_id)
    assert "specification.record" not in pending.available_nodes
    spec_review = next(
        item for item in pending.decisions if item.node_id == "specification.review"
    )
    candidate = next(
        item
        for item in spec_review.fact_references
        if item.fact_type == "specification_candidate"
    )
    review_domain = _domain(engine, at=NOW + timedelta(seconds=2))
    assert review_domain.transition(
        DecideSpecification(
            project_id=project_id,
            graph_version=pending.graph_version,
            fact_fingerprint=pending.fact_fingerprint,
            decision_fingerprint=spec_review.decision_fingerprint,
            idempotency_key="specification-accepted",
            actor="operator",
            specification_candidate_id=int(candidate.fact_id),
            specification_fingerprint=candidate.fingerprint,
            decision="accepted",
            rationale="",
        )
    ).ok
    with Session(engine) as session:
        assert len(session.exec(select(DiscoveryArtifact)).all()) == 1
        assert len(session.exec(select(SpecificationCandidate)).all()) == 1
        assert len(session.exec(select(SpecificationDecision)).all()) == 1
        specs = session.exec(select(SpecRegistry)).all()
        assert len(specs) == 1
        assert specs[0].status == "approved"
        assert specs[0].source_specification_candidate_id == int(candidate.fact_id)
