"""Durable transition tests for the isolated Project Vision lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlmodel import Session, select

from models.core import Project
from models.product_definition import VisionArtifact, VisionArtifactDecision
from workflow.clock import FixedClock
from workflow.definitions.product_definition import product_definition_graph
from workflow.domain import WorkflowDomain
from workflow.requests import (
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


class _Registry:
    def require(self, node_id: str) -> object:
        assert node_id == "vision.interview"
        return object()


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
    domain: WorkflowDomain, project_id: int, key: str
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
        normalized_input={"mode": "initial", "user_response": "Build a tool."},
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
    complete: bool,
    key: str,
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
            idempotency_key=key,
            actor=start.actor,
            mode="initial",
            user_text="Build a tool.",
            updated_components=COMPONENTS,
            project_vision_statement="A trusted workflow tool.",
            is_complete=complete,
            clarifying_questions=() if complete else ("Who is the user?",),
            attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
        )
    )


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
        domain, first_start, first_attempt, complete=False, key="record-incomplete"
    )

    assert incomplete.ok
    assert "vision.interview" in domain.position(project_id).available_nodes
    second_start, second_attempt = _start(domain, project_id, "vision-complete")
    complete = _record(
        domain, second_start, second_attempt, complete=True, key="record-complete"
    )

    assert complete.ok
    assert "vision.review" in domain.position(project_id).waiting_nodes
    with Session(engine) as session:
        assert len(session.exec(select(VisionArtifact)).all()) == 1


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
    recorded = _record(domain, start, attempt, complete=True, key="vision-record")
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
