"""Durable transition tests for the isolated Project Vision lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from sqlmodel import Session, select

from models.core import Project
from models.product_definition import (
    VisionArtifact,
    VisionArtifactDecision,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from services.node_attempt_replay import (
    DurableNodeAttemptReplayService,
    NodeAttemptReplayQuery,
)
from workflow.clock import FixedClock
from workflow.definitions.product_definition import product_definition_graph
from workflow.domain import WorkflowDomain
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
        )
    )

    assert replay == second_attempt.model_copy(update={"replayed": True})


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
    initial_position = domain.position(project_id)
    initial_review = _decision(domain, project_id, "vision.review")
    accepted = domain.transition(
        DecideVisionReview(
            project_id=project_id,
            graph_version=initial_position.graph_version,
            fact_fingerprint=initial_position.fact_fingerprint,
            decision_fingerprint=initial_review.decision_fingerprint,
            idempotency_key="revision-initial-accept",
            actor="operator@example.com",
            vision_artifact_id=artifact_id,
            vision_fingerprint=fingerprint,
            decision="accepted",
            rationale="Initial Vision accepted.",
        )
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
    pending_position = domain.position(project_id)
    pending_review = _decision(domain, project_id, "vision.review")
    accepted_revision = domain.transition(
        DecideVisionReview(
            project_id=project_id,
            graph_version=pending_position.graph_version,
            fact_fingerprint=pending_position.fact_fingerprint,
            decision_fingerprint=pending_review.decision_fingerprint,
            idempotency_key="revision-accept",
            actor="operator@example.com",
            vision_artifact_id=revised_id,
            vision_fingerprint=revised_fingerprint,
            decision="accepted",
            rationale="Revised Vision accepted.",
        )
    )

    assert accepted_revision.ok
    with Session(engine) as session:
        artifacts = session.exec(select(VisionArtifact)).all()
        assert len(artifacts) == EXPECTED_VISION_ARTIFACT_COUNT
        assert artifacts[-1].supersedes_vision_artifact_id == artifact_id
        assert (
            len(session.exec(select(VisionArtifactDecision)).all())
            == EXPECTED_VISION_ARTIFACT_COUNT
        )
        revision_turns = session.exec(
            select(VisionInterviewTurn).where(VisionInterviewTurn.mode == "revision")
        ).all()
        for turn in revision_turns:
            turn.revision_intent_id = None
            session.add(turn)
        for intent in session.exec(select(VisionRevisionIntent)).all():
            session.delete(intent)
        session.commit()
