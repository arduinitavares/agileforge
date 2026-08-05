"""Provider-free Product Goal persistence regression tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlmodel import Session, select

from models.core import Project
from models.product_definition import (
    ProductGoalArtifact,
    ProductGoalInterviewTurn,
    VisionArtifact,
    VisionArtifactDecision,
    VisionInterviewTurn,
)
from models.workflow import WorkflowNodeAttempt
from repositories.workflow import WorkflowFactRepository
from workflow.clock import FixedClock
from workflow.contracts import GRAPH_VERSION
from workflow.definitions.product_goal import PRODUCT_GOAL_NODES
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.graph import ChildGraphSpec, WorkflowGraph
from workflow.requests import RecordProductGoalInterviewTurn, StartNodeAttempt

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

NOW = datetime(2026, 8, 5, 12, tzinfo=UTC)
_TURN_COUNT = 2
_ARTIFACT_COUNT = 1


class _Registry:
    def require(self, node_id: str) -> object:
        assert node_id == "goal.interview"
        return object()


def _domain(engine: Engine) -> WorkflowDomain:
    return WorkflowDomain(
        engine=engine,
        graph=WorkflowGraph(
            graph_version=GRAPH_VERSION,
            root=ChildGraphSpec(child_graph_id="goal", nodes=PRODUCT_GOAL_NODES),
        ),
        clock=FixedClock(now_value=NOW),
        adk_recipe_registry=_Registry(),
    )


def _seed_accepted_vision(engine: Engine) -> int:
    with Session(engine) as session:
        project = Project(name="Goal reload")
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
