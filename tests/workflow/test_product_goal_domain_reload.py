"""Provider-free Product Goal persistence regression tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

import pytest
from sqlalchemy import event
from sqlmodel import Session, col, select

from models.core import Project, Sprint, Team
from models.enums import SprintStatus
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
    VisionEvidenceSnapshot,
    VisionInterviewTurn,
)
from models.specs import SpecRegistry
from models.workflow import WorkflowNodeAttempt, WorkflowTransitionReceipt
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.product_goal_interview_input import ProductGoalInterviewInputService
from workflow.clock import FixedClock
from workflow.contracts import GRAPH_VERSION, FactReference, WorkflowPosition
from workflow.definitions.product_discovery import (
    PRODUCT_DISCOVERY_NODES,
    accepted_current_spec,
    current_discovery,
    current_specification_candidate,
)
from workflow.definitions.product_goal import PRODUCT_GOAL_NODES
from workflow.domain import WorkflowDomain
from workflow.fingerprints import (
    canonical_hash,
    canonical_json,
    canonical_stored_json_hash,
    product_goal_artifact_fingerprint,
    product_goal_interview_output_fingerprint,
    vision_interview_output_fingerprint,
)
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
_STAGED_SPEC_REGISTRY_COUNT = 2
_FORCED_SPECIFICATION_ACCEPTANCE_FLUSH_FAILURE = (
    "forced specification acceptance flush failure"
)


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
            node_id="vision.bootstrap",
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
        evidence_item = {
            "evidence_id": "project:metadata",
            "kind": "project_metadata",
            "relative_path": None,
            "content_fingerprint": canonical_hash(
                {"name": name, "description": None}
            ),
            "trust": "operator_provided",
            "content": {"name": name, "description": None},
            "truncated": False,
        }
        evidence = {
            "schema_version": "agileforge.vision-evidence.v1",
            "items": [evidence_item],
            "warnings": [],
        }
        snapshot = VisionEvidenceSnapshot(
            project_id=project.project_id,
            repository_binding_id=None,
            workflow_node_attempt_id=attempt.workflow_node_attempt_id,
            evidence_json=canonical_json(
                {
                    **evidence,
                    "evidence_fingerprint": canonical_hash(evidence),
                }
            ),
            evidence_fingerprint=canonical_hash(evidence),
            warnings_json="[]",
            created_at=NOW,
        )
        session.add(snapshot)
        session.flush()
        assert snapshot.vision_evidence_snapshot_id is not None
        turn = VisionInterviewTurn(
            project_id=project.project_id,
            operation="bootstrap",
            turn_number=1,
            revision_intent_id=None,
            prior_turn_id=None,
            vision_evidence_snapshot_id=snapshot.vision_evidence_snapshot_id,
            user_text=None,
            components_json=canonical_json(components),
            vision_statement="A durable Vision.",
            is_complete=True,
            clarifying_questions_json="[]",
            component_basis_json="[]",
            assumptions_json="[]",
            conflicts_json="[]",
            output_fingerprint=vision_interview_output_fingerprint(
                components,
                "A durable Vision.",
                True,
                (),
                {"component_basis": (), "assumptions": (), "conflicts": ()},
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
            vision_evidence_snapshot_id=snapshot.vision_evidence_snapshot_id,
            component_basis_json="[]",
            assumptions_json="[]",
            conflicts_json="[]",
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


def _add_goal_chain(
    session: Session,
    project_id: int,
    vision: VisionArtifact,
    *,
    goal_number: int,
    accepted: bool = True,
) -> None:
    """Persist one complete Goal chain with an optional accepted review."""
    recorded_at = NOW + timedelta(minutes=goal_number)
    attempt = WorkflowNodeAttempt(
        project_id=project_id,
        node_id="goal.interview",
        instance_key=None,
        graph_version=GRAPH_VERSION,
        fact_fingerprint=f"sha256:facts-{goal_number}",
        business_fact_fingerprint=f"sha256:business-{goal_number}",
        decision_fingerprint=f"sha256:decision-{goal_number}",
        normalized_input_json="{}",
        input_fingerprint=f"sha256:input-{goal_number}",
        model_id="fake/goal",
        execution_settings_json="{}",
        idempotency_key=f"ambiguous-goal-{goal_number}-attempt",
        actor="operator",
        correlation_id=None,
        started_at=recorded_at,
        lease_expires_at=recorded_at + timedelta(minutes=1),
        attempt_fingerprint=f"sha256:goal-attempt-{goal_number}",
    )
    session.add(attempt)
    session.flush()
    assert attempt.workflow_node_attempt_id is not None

    components = {
        "valuable_future_state": f"Reliable decisions {goal_number}",
        "beneficiary": "Operators",
        "value": "Confidence",
        "success_signals": ["Measured outcomes"],
        "boundaries": ["No implementation"],
    }
    statement = f"Operators complete accepted Goal {goal_number}."
    turn = ProductGoalInterviewTurn(
        project_id=project_id,
        vision_artifact_id=vision.vision_artifact_id,
        vision_fingerprint=vision.content_fingerprint,
        goal_number=goal_number,
        revision_number=1,
        prior_turn_id=None,
        user_text=f"Define Goal {goal_number}",
        components_json=canonical_json(components),
        goal_statement=statement,
        is_complete=True,
        clarifying_questions_json="[]",
        output_fingerprint=product_goal_interview_output_fingerprint(
            components, statement, True, ()
        ),
        workflow_node_attempt_id=attempt.workflow_node_attempt_id,
        attempt_fingerprint=attempt.attempt_fingerprint,
        recorded_at=recorded_at + timedelta(seconds=1),
    )
    session.add(turn)
    session.flush()
    assert turn.product_goal_interview_turn_id is not None
    goal = ProductGoalArtifact(
        project_id=project_id,
        vision_artifact_id=vision.vision_artifact_id,
        vision_fingerprint=vision.content_fingerprint,
        goal_number=goal_number,
        revision_number=1,
        statement=statement,
        content_fingerprint=product_goal_artifact_fingerprint(components, statement),
        supersedes_product_goal_artifact_id=None,
        source_interview_turn_id=turn.product_goal_interview_turn_id,
        created_by="operator",
        created_at=recorded_at + timedelta(seconds=2),
    )
    session.add(goal)
    session.flush()
    assert goal.product_goal_artifact_id is not None
    if accepted:
        session.add(
            ProductGoalArtifactDecision(
                project_id=project_id,
                product_goal_artifact_id=goal.product_goal_artifact_id,
                artifact_fingerprint=goal.content_fingerprint,
                decision="accepted",
                rationale="Accepted without an outcome.",
                reviewer="operator",
                idempotency_key=f"ambiguous-goal-{goal_number}-accepted",
                decided_at=recorded_at + timedelta(seconds=3),
            )
        )


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


def _accept_active_goal(
    engine: Engine,
    project_id: int,
    *,
    key: str,
    at: datetime = NOW,
) -> WorkflowDomain:
    """Create and accept one complete Goal, returning its current domain."""
    interview_domain = _domain(engine, at=at)
    _record_turn(interview_domain, project_id, complete=True, key=f"{key}-goal")
    review_domain = _domain(engine, at=at + timedelta(seconds=1))
    _review(
        review_domain,
        project_id,
        decision="accepted",
        rationale="",
        key=f"{key}-accepted",
    )
    return _domain(engine, at=at + timedelta(seconds=2))


def _record_discovery(
    domain: WorkflowDomain,
    project_id: int,
    *,
    key: str,
) -> FactReference:
    """Record the graph-selected discovery and return its exact reference."""
    position = domain.position(project_id)
    decision = next(
        item for item in position.decisions if item.node_id == "discovery.record"
    )
    assert domain.transition(
        RecordDiscoveryArtifact(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=decision.decision_fingerprint,
            idempotency_key=key,
            actor="operator",
            canonical_content={"discovery": key},
            content_ref=None,
        )
    ).ok
    recorded = domain.position(project_id)
    specification = next(
        item for item in recorded.decisions if item.node_id == "specification.record"
    )
    reference = specification.fact_references[0]
    assert reference.fact_type == "discovery"
    return reference


def _record_specification(
    domain: WorkflowDomain,
    project_id: int,
    *,
    key: str,
    supersedes: int | None = None,
) -> FactReference:
    """Record one graph-selected candidate and return its pending review reference."""
    position = domain.position(project_id)
    decision = next(
        item for item in position.decisions if item.node_id == "specification.record"
    )
    result = domain.transition(
        RecordSpecificationCandidate(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=decision.decision_fingerprint,
            idempotency_key=key,
            actor="operator",
            canonical_content={"specification": key},
            content_ref=None,
            supersedes_specification_candidate_id=supersedes,
        )
    )
    assert result.ok
    pending = domain.position(project_id)
    review = next(
        item for item in pending.decisions if item.node_id == "specification.review"
    )
    return next(
        item
        for item in review.fact_references
        if item.fact_type == "specification_candidate"
    )


def _decide_specification(
    domain: WorkflowDomain,
    project_id: int,
    *,
    key: str,
    decision: Literal["accepted", "rejected", "feedback"],
    rationale: str = "",
) -> FactReference:
    """Apply the exact graph-selected review and return its candidate reference."""
    position = domain.position(project_id)
    review = next(
        item for item in position.decisions if item.node_id == "specification.review"
    )
    candidate = next(
        item
        for item in review.fact_references
        if item.fact_type == "specification_candidate"
    )
    assert domain.transition(
        DecideSpecification(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=review.decision_fingerprint,
            idempotency_key=key,
            actor="operator",
            specification_candidate_id=int(candidate.fact_id),
            specification_fingerprint=candidate.fingerprint,
            decision=decision,
            rationale=rationale,
        )
    ).ok
    return candidate


def _product_definition_counts(engine: Engine) -> tuple[int, int, int, int]:
    """Read append-only discovery/specification record counts for rollback checks."""
    with Session(engine) as session:
        return (
            len(session.exec(select(DiscoveryArtifact)).all()),
            len(session.exec(select(SpecificationCandidate)).all()),
            len(session.exec(select(SpecificationDecision)).all()),
            len(session.exec(select(SpecRegistry)).all()),
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


def test_goal_input_ignores_unrelated_facts_but_rejects_goal_lineage(
    engine: Engine,
) -> None:
    """Goal preparation has a narrow durable boundary and validates its lineage."""
    project_id = _seed_accepted_vision(engine)
    domain = _domain(engine)
    position = domain.position(project_id)
    decision = next(
        item for item in position.decisions if item.node_id == "goal.interview"
    )
    with Session(engine) as session:
        session.add(
            SpecRegistry(
                project_id=project_id,
                spec_hash="not-a-canonical-hash",
                content="{",
                content_ref=None,
                status="approved",
                source_specification_candidate_id=999,
                source_vision_artifact_id=999,
                source_vision_fingerprint="wrong",
                source_product_goal_artifact_id=999,
                source_product_goal_fingerprint="wrong",
                source_discovery_artifact_id=999,
                source_discovery_fingerprint="wrong",
            )
        )
        session.commit()
    with Session(engine) as session, pytest.raises(WorkflowFactLoadError):
        WorkflowFactRepository(session).load(project_id)

    payload = ProductGoalInterviewInputService(engine=engine).build(
        project_id, decision, "Prepare the Goal interview"
    )
    assert payload["accepted_vision_statement"] == "A durable Vision."

    with Session(engine) as session:
        attempt = session.exec(
            select(WorkflowNodeAttempt).where(
                WorkflowNodeAttempt.project_id == project_id
            )
        ).one()
        vision = session.exec(
            select(VisionArtifact).where(VisionArtifact.project_id == project_id)
        ).one()
        assert attempt.workflow_node_attempt_id is not None
        assert vision.vision_artifact_id is not None
        session.add(
            ProductGoalInterviewTurn(
                project_id=project_id,
                vision_artifact_id=vision.vision_artifact_id,
                vision_fingerprint=vision.content_fingerprint,
                goal_number=1,
                revision_number=1,
                prior_turn_id=None,
                user_text="Malformed",
                components_json="{",
                goal_statement="Malformed",
                is_complete=False,
                clarifying_questions_json="[]",
                output_fingerprint="wrong",
                workflow_node_attempt_id=attempt.workflow_node_attempt_id,
                attempt_fingerprint=attempt.attempt_fingerprint,
                recorded_at=NOW + timedelta(seconds=1),
            )
        )
        session.commit()

    with pytest.raises(WorkflowFactLoadError):
        ProductGoalInterviewInputService(engine=engine).build(
            project_id, decision, "Reject malformed Goal lineage"
        )


def test_goal_input_rejects_multiple_unresolved_accepted_goals(
    engine: Engine,
) -> None:
    """Contradictory accepted Goal chains cannot authorize host input."""
    project_id = _seed_accepted_vision(engine)
    position = _domain(engine).position(project_id)
    decision = next(
        item for item in position.decisions if item.node_id == "goal.interview"
    )
    with Session(engine) as session:
        vision = session.exec(
            select(VisionArtifact).where(VisionArtifact.project_id == project_id)
        ).one()
        _add_goal_chain(session, project_id, vision, goal_number=1)
        _add_goal_chain(session, project_id, vision, goal_number=2)
        session.commit()

    with pytest.raises(
        WorkflowFactLoadError,
        match="more than one unresolved Product Goal selection",
    ):
        ProductGoalInterviewInputService(engine=engine).build(
            project_id, decision, "Do not prepare from ambiguous Goal state"
        )

    with pytest.raises(
        WorkflowFactLoadError,
        match="more than one unresolved Product Goal selection",
    ):
        _domain(engine).position(project_id)


def test_goal_input_rejects_mixed_unresolved_accepted_and_pending_goals(
    engine: Engine,
) -> None:
    """An accepted Goal and pending Goal cannot form a domain position."""
    project_id = _seed_accepted_vision(engine)
    position = _domain(engine).position(project_id)
    decision = next(
        item for item in position.decisions if item.node_id == "goal.interview"
    )
    with Session(engine) as session:
        vision = session.exec(
            select(VisionArtifact).where(VisionArtifact.project_id == project_id)
        ).one()
        _add_goal_chain(session, project_id, vision, goal_number=1)
        _add_goal_chain(session, project_id, vision, goal_number=2, accepted=False)
        session.commit()

    with pytest.raises(
        WorkflowFactLoadError,
        match="more than one unresolved Product Goal selection",
    ):
        ProductGoalInterviewInputService(engine=engine).build(
            project_id, decision, "Do not prepare from mixed Goal state"
        )

    with pytest.raises(
        WorkflowFactLoadError,
        match="more than one unresolved Product Goal selection",
    ):
        _domain(engine).position(project_id)


def test_goal_feedback_revision_and_outcome_are_exact_and_durable(
    engine: Engine,
) -> None:
    """A resolved accepted feedback replacement opens the next Goal number."""
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
    acceptance_domain = _domain(engine, at=NOW + timedelta(seconds=2))
    replacement_position, _ = _review(
        acceptance_domain,
        project_id,
        decision="accepted",
        rationale="",
        key="revision-accepted",
    )
    assert "discovery.record" in acceptance_domain.position(project_id).available_nodes

    outcome_domain = _domain(engine, at=NOW + timedelta(seconds=3))
    assert outcome_domain.transition(
        _fulfill_request(
            outcome_domain,
            project_id,
            key="replacement-fulfilled",
            rationale="The replacement Goal was delivered.",
        )
    ).ok
    assert "goal.interview" in outcome_domain.position(project_id).available_nodes
    _record_turn(outcome_domain, project_id, complete=True, key="next-goal")

    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    assert [
        (goal.goal_number, goal.revision_number)
        for goal in snapshot.product_goal_artifacts
    ] == [(1, 1), (1, 2), (2, 1)]
    assert len(snapshot.product_goal_outcomes) == 1
    assert "goal.review" in replacement_position.waiting_nodes


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
    assert (
        outcome_domain.transition(fulfill).model_dump()
        == result.model_copy(update={"replayed": True}).model_dump()
    )

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


def test_specification_acceptance_rolls_back_all_staged_rows_on_flush_failure(
    engine: Engine,
) -> None:
    """A database fault after staging acceptance leaves no partial durable state."""
    project_id = _seed_accepted_vision(engine)
    domain = _accept_active_goal(engine, project_id, key="first-goal")
    domain = _domain(engine, at=NOW + timedelta(seconds=3))
    _record_discovery(domain, project_id, key="first-discovery")
    domain = _domain(engine, at=NOW + timedelta(seconds=4))
    _record_specification(domain, project_id, key="first-specification")
    domain = _domain(engine, at=NOW + timedelta(seconds=5))
    _decide_specification(
        domain,
        project_id,
        key="first-specification-accepted",
        decision="accepted",
    )
    outcome_domain = _domain(engine, at=NOW + timedelta(seconds=6))
    assert outcome_domain.transition(
        _fulfill_request(
            outcome_domain,
            project_id,
            key="first-goal-fulfilled",
            rationale="Delivered.",
        )
    ).ok
    _accept_active_goal(
        engine,
        project_id,
        key="second-goal",
        at=NOW + timedelta(seconds=7),
    )
    domain = _domain(engine, at=NOW + timedelta(seconds=10))
    _record_discovery(domain, project_id, key="second-discovery")
    domain = _domain(engine, at=NOW + timedelta(seconds=11))
    _record_specification(domain, project_id, key="second-specification")
    position = domain.position(project_id)
    review = next(
        item for item in position.decisions if item.node_id == "specification.review"
    )
    candidate = next(
        item
        for item in review.fact_references
        if item.fact_type == "specification_candidate"
    )
    request = DecideSpecification(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=review.decision_fingerprint,
        idempotency_key="second-specification-accepted-fails",
        actor="operator",
        specification_candidate_id=int(candidate.fact_id),
        specification_fingerprint=candidate.fingerprint,
        decision="accepted",
        rationale="",
    )

    def fail_after_acceptance_stage(session: Session, _context: object) -> None:
        rows = (*session.identity_map.values(), *session.new)
        staged_review = any(
            isinstance(row, SpecificationDecision)
            and row.idempotency_key == request.idempotency_key
            for row in rows
        )
        staged_specs = [row for row in rows if isinstance(row, SpecRegistry)]
        if staged_review and len(staged_specs) == _STAGED_SPEC_REGISTRY_COUNT:
            raise RuntimeError(_FORCED_SPECIFICATION_ACCEPTANCE_FLUSH_FAILURE)

    event.listen(Session, "after_flush", fail_after_acceptance_stage)
    try:
        failing_domain = _domain(engine, at=NOW + timedelta(seconds=12))
        with pytest.raises(
            RuntimeError, match=_FORCED_SPECIFICATION_ACCEPTANCE_FLUSH_FAILURE
        ):
            failing_domain.transition(request)
    finally:
        event.remove(Session, "after_flush", fail_after_acceptance_stage)

    with Session(engine) as session:
        decisions = session.exec(select(SpecificationDecision)).all()
        specs = session.exec(select(SpecRegistry)).all()
        receipts = session.exec(
            select(WorkflowTransitionReceipt).where(
                WorkflowTransitionReceipt.idempotency_key == request.idempotency_key
            )
        ).all()
    assert len(decisions) == 1
    assert [(spec.status, spec.supersedes_spec_version_id) for spec in specs] == [
        ("approved", None)
    ]
    assert receipts == []


def test_discovery_and_pending_specification_follow_exact_active_lineage(
    engine: Engine,
) -> None:
    """Discovery and its pending candidate bind only the accepted Vision and Goal."""
    project_id = _seed_accepted_vision(engine)
    domain = _accept_active_goal(engine, project_id, key="lineage")

    discovery_position = domain.position(project_id)
    discovery_decision = next(
        item
        for item in discovery_position.decisions
        if item.node_id == "discovery.record"
    )
    assert {item.fact_type for item in discovery_decision.fact_references} == {
        "vision",
        "product_goal",
    }
    discovery = _record_discovery(domain, project_id, key="lineage-discovery")
    candidate = _record_specification(domain, project_id, key="lineage-specification")

    pending = domain.position(project_id)
    required = {
        item.node_id
        for item in pending.decisions
        if item.recommendation_kind.value == "required"
        and item.category.value in {"available", "waiting"}
    }
    assert required == {"specification.review"}
    assert all("prd" not in item.node_id for item in pending.decisions)
    with Session(engine) as session:
        stored_discovery = session.get(DiscoveryArtifact, int(discovery.fact_id))
        stored_candidate = session.get(SpecificationCandidate, int(candidate.fact_id))
        assert stored_discovery is not None
        assert stored_candidate is not None
        assert (
            stored_candidate.discovery_artifact_id,
            stored_candidate.discovery_fingerprint,
        ) == (int(discovery.fact_id), discovery.fingerprint)
        assert stored_candidate.base_spec_version_id is None
        assert stored_candidate.base_spec_hash is None


@pytest.mark.parametrize("terminal_decision", ["rejected", "feedback"])
def test_specification_replacement_requires_exact_terminal_supersedes(
    engine: Engine,
    terminal_decision: Literal["rejected", "feedback"],
) -> None:
    """A reviewed candidate cannot be bypassed by a new unsuperseded candidate."""
    project_id = _seed_accepted_vision(engine)
    domain = _accept_active_goal(engine, project_id, key=terminal_decision)
    domain = _domain(engine, at=NOW + timedelta(seconds=3))
    _record_discovery(domain, project_id, key=f"{terminal_decision}-discovery")
    domain = _domain(engine, at=NOW + timedelta(seconds=4))
    first = _record_specification(domain, project_id, key=f"{terminal_decision}-first")
    domain = _domain(engine, at=NOW + timedelta(seconds=5))
    _decide_specification(
        domain,
        project_id,
        key=f"{terminal_decision}-decision",
        decision=terminal_decision,
        rationale="Revise this candidate.",
    )
    replacement_position = domain.position(project_id)
    replacement = next(
        item
        for item in replacement_position.decisions
        if item.node_id == "specification.record"
    )
    invalid = RecordSpecificationCandidate(
        project_id=project_id,
        graph_version=replacement_position.graph_version,
        fact_fingerprint=replacement_position.fact_fingerprint,
        decision_fingerprint=replacement.decision_fingerprint,
        idempotency_key=f"{terminal_decision}-missing-supersedes",
        actor="operator",
        canonical_content={"specification": "missing supersedes"},
        content_ref=None,
        supersedes_specification_candidate_id=None,
    )
    before = _product_definition_counts(engine)
    assert not domain.transition(invalid).ok
    assert _product_definition_counts(engine) == before

    wrong = invalid.model_copy(
        update={
            "idempotency_key": f"{terminal_decision}-wrong-supersedes",
            "supersedes_specification_candidate_id": int(first.fact_id) + 100,
        }
    )
    assert not domain.transition(wrong).ok
    assert _product_definition_counts(engine) == before

    domain = _domain(engine, at=NOW + timedelta(seconds=6))
    replacement_candidate = _record_specification(
        domain,
        project_id,
        key=f"{terminal_decision}-replacement",
        supersedes=int(first.fact_id),
    )
    with Session(engine) as session:
        stored = session.get(SpecificationCandidate, int(replacement_candidate.fact_id))
        assert stored is not None
        assert stored.supersedes_specification_candidate_id == int(first.fact_id)


def test_stale_discovery_and_candidate_requests_write_nothing(engine: Engine) -> None:
    """Wrong positioned discovery or candidate fingerprints cannot append records."""
    project_id = _seed_accepted_vision(engine)
    domain = _accept_active_goal(engine, project_id, key="stale")
    discovery_position = domain.position(project_id)
    discovery = next(
        item
        for item in discovery_position.decisions
        if item.node_id == "discovery.record"
    )
    stale_discovery = RecordDiscoveryArtifact(
        project_id=project_id,
        graph_version=discovery_position.graph_version,
        fact_fingerprint="sha256:wrong-vision-or-goal",
        decision_fingerprint=discovery.decision_fingerprint,
        idempotency_key="stale-discovery",
        actor="operator",
        canonical_content={"discovery": "stale"},
        content_ref=None,
    )
    before = _product_definition_counts(engine)
    assert not domain.transition(stale_discovery).ok
    assert _product_definition_counts(engine) == before

    _record_discovery(domain, project_id, key="current-discovery")
    specification_position = domain.position(project_id)
    specification = next(
        item
        for item in specification_position.decisions
        if item.node_id == "specification.record"
    )
    before_candidate = _product_definition_counts(engine)
    stale_candidate = RecordSpecificationCandidate(
        project_id=project_id,
        graph_version=specification_position.graph_version,
        fact_fingerprint="sha256:wrong-discovery",
        decision_fingerprint=specification.decision_fingerprint,
        idempotency_key="stale-candidate",
        actor="operator",
        canonical_content={"specification": "stale"},
        content_ref=None,
        supersedes_specification_candidate_id=None,
    )
    assert not domain.transition(stale_candidate).ok
    assert _product_definition_counts(engine) == before_candidate


def test_later_goal_replaces_the_registered_specification_atomically(
    engine: Engine,
) -> None:
    """A second Goal starts with the exact current spec base and retires prior facts."""
    project_id = _seed_accepted_vision(engine)
    domain = _accept_active_goal(engine, project_id, key="first-goal")
    domain = _domain(engine, at=NOW + timedelta(seconds=3))
    _record_discovery(domain, project_id, key="first-discovery")
    domain = _domain(engine, at=NOW + timedelta(seconds=4))
    first_candidate = _record_specification(
        domain, project_id, key="first-specification"
    )
    domain = _domain(engine, at=NOW + timedelta(seconds=5))
    _decide_specification(
        domain,
        project_id,
        key="first-specification-accepted",
        decision="accepted",
    )
    with Session(engine) as session:
        first_spec = session.exec(select(SpecRegistry)).one()
        assert first_spec.status == "approved"
        assert first_spec.source_specification_candidate_id == int(
            first_candidate.fact_id
        )
        assert first_spec.spec_hash == canonical_stored_json_hash(first_spec.content)
        first_spec_id = first_spec.spec_version_id
        first_spec_hash = first_spec.spec_hash
    assert first_spec_id is not None

    outcome_domain = _domain(engine, at=NOW + timedelta(seconds=6))
    assert outcome_domain.transition(
        _fulfill_request(
            outcome_domain,
            project_id,
            key="first-goal-fulfilled",
            rationale="Delivered.",
        )
    ).ok
    _accept_active_goal(
        engine,
        project_id,
        key="second-goal",
        at=NOW + timedelta(seconds=7),
    )
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    assert current_discovery(snapshot) is None
    assert current_specification_candidate(snapshot) is None
    assert accepted_current_spec(snapshot) is None

    domain = _domain(engine, at=NOW + timedelta(seconds=10))
    _record_discovery(domain, project_id, key="second-discovery")
    domain = _domain(engine, at=NOW + timedelta(seconds=11))
    second_candidate = _record_specification(
        domain, project_id, key="second-specification"
    )
    with Session(engine) as session:
        stored_candidate = session.get(
            SpecificationCandidate, int(second_candidate.fact_id)
        )
        assert stored_candidate is not None
        assert (
            stored_candidate.base_spec_version_id,
            stored_candidate.base_spec_hash,
        ) == (first_spec_id, first_spec_hash)

    domain = _domain(engine, at=NOW + timedelta(seconds=12))
    _decide_specification(
        domain,
        project_id,
        key="second-specification-accepted",
        decision="accepted",
    )
    with Session(engine) as session:
        specs = session.exec(
            select(SpecRegistry).order_by(col(SpecRegistry.spec_version_id))
        ).all()
    assert [(spec.status, spec.supersedes_spec_version_id) for spec in specs] == [
        ("superseded", None),
        ("approved", first_spec_id),
    ]
    assert specs[1].source_specification_candidate_id == int(second_candidate.fact_id)
    assert specs[1].spec_hash == canonical_stored_json_hash(specs[1].content)
