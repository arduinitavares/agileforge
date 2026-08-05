"""Host-prepared input tests for the isolated Vision interview."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event
from sqlmodel import Session, delete

import services.vision_interview_input as vision_input_module
from models.core import Project
from models.product_definition import (
    VisionArtifact,
    VisionArtifactDecision,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from models.workflow import WorkflowNodeAttempt
from services.vision_interview_input import VisionInterviewInputService
from workflow.contracts import NodeCategory, NodeDecision, RecommendationKind
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine


@pytest.fixture(autouse=True)
def _clear_seeded_vision_rows(engine: Engine) -> Iterator[None]:
    """Remove explicit durable fixtures before the in-memory schema is dropped."""
    yield
    with Session(engine) as session:
        session.exec(delete(VisionArtifactDecision))
        session.exec(delete(VisionRevisionIntent))
        session.exec(delete(VisionArtifact))
        session.exec(delete(VisionInterviewTurn))
        session.exec(delete(WorkflowNodeAttempt))
        session.exec(delete(Project))
        session.commit()


def _decision() -> NodeDecision:
    return NodeDecision(
        node_id="vision.interview",
        child_graph_id="vision",
        request_kind="record_vision_interview_turn",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="VISION_INTERVIEW_REQUIRED",
        decision_fingerprint="sha256:decision",
    )


def test_builds_initial_input_from_project_and_human_text_only(engine: Engine) -> None:
    """The first Vision turn does not read authority, specs, or repository state."""
    with Session(engine) as session:
        project = Project(
            name="Vision input",
            description="Human intent only.",
            origin="greenfield",
        )
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id

    payload = VisionInterviewInputService(engine=engine).build(
        project_id=project_id,
        decision=_decision(),
        user_text="We need durable workflow decisions.",
    )

    assert payload == {
        "project_name": "Vision input",
        "project_description": "Human intent only.",
        "mode": "initial",
        "user_response": "We need durable workflow decisions.",
        "prior_components": None,
        "accepted_vision_statement": None,
    }


def _seed_open_revision(session: Session, project: Project) -> None:
    """Persist a complete accepted Vision with one open revision intent."""
    assert project.project_id is not None
    recorded_at = datetime(2026, 8, 5, 12, tzinfo=UTC)
    attempt = WorkflowNodeAttempt(
        project_id=project.project_id,
        node_id="vision.interview",
        instance_key=None,
        graph_version="agileforge.workflow.v1",
        fact_fingerprint="sha256:facts",
        business_fact_fingerprint="sha256:business",
        decision_fingerprint="sha256:decision",
        normalized_input_json="{}",
        input_fingerprint="sha256:input",
        model_id="test-model",
        execution_settings_json="{}",
        idempotency_key="vision-input-attempt",
        actor="operator@example.com",
        correlation_id=None,
        started_at=recorded_at,
        lease_expires_at=recorded_at + timedelta(minutes=5),
        attempt_fingerprint="sha256:attempt:vision-input",
    )
    session.add(attempt)
    session.flush()
    assert attempt.workflow_node_attempt_id is not None
    components = {
        "project_name": project.name,
        "target_user": "Operators",
        "problem": "State drift",
        "product_category": "Tool",
        "key_benefit": "Trust",
        "competitors": "Spreadsheets",
        "differentiator": "Typed facts",
    }
    statement = "A durable workflow tool."
    turn = VisionInterviewTurn(
        project_id=project.project_id,
        mode="initial",
        turn_number=1,
        revision_intent_id=None,
        prior_turn_id=None,
        user_text="Build durable workflow facts.",
        components_json=canonical_json(components),
        vision_statement=statement,
        is_complete=True,
        clarifying_questions_json=canonical_json([]),
        output_fingerprint=canonical_hash(
            {
                "components_json": components,
                "vision_statement": statement,
                "is_complete": True,
                "clarifying_questions_json": [],
            }
        ),
        workflow_node_attempt_id=attempt.workflow_node_attempt_id,
        attempt_fingerprint=attempt.attempt_fingerprint,
        recorded_at=recorded_at,
    )
    session.add(turn)
    session.flush()
    assert turn.vision_interview_turn_id is not None
    fingerprint = canonical_hash({"components": components, "statement": statement})
    artifact = VisionArtifact(
        project_id=project.project_id,
        version_number=1,
        components_json=canonical_json(components),
        statement=statement,
        content_fingerprint=fingerprint,
        supersedes_vision_artifact_id=None,
        source_interview_turn_id=turn.vision_interview_turn_id,
        created_by="operator@example.com",
        created_at=recorded_at,
    )
    session.add(artifact)
    session.flush()
    assert artifact.vision_artifact_id is not None
    session.add(
        VisionArtifactDecision(
            project_id=project.project_id,
            vision_artifact_id=artifact.vision_artifact_id,
            artifact_fingerprint=fingerprint,
            decision="accepted",
            rationale="Accepted.",
            reviewer="operator@example.com",
            idempotency_key="vision-input-accepted",
            decided_at=recorded_at + timedelta(seconds=1),
        )
    )
    session.add(
        VisionRevisionIntent(
            project_id=project.project_id,
            source_vision_artifact_id=artifact.vision_artifact_id,
            source_vision_fingerprint=fingerprint,
            reason="Clarify the target user.",
            initiated_by="operator@example.com",
            initiated_at=recorded_at + timedelta(seconds=2),
        )
    )


def _observed_query_tables(
    engine: Engine,
    *,
    project_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> set[str]:
    """Return every business table reached by input construction."""
    statements: list[str] = []

    def record_query(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement.lower())

    def forbidden_access(*_args: object, **_kwargs: object) -> object:
        pytest.fail("Vision input must not access replay or ADK trace services.")

    monkeypatch.setattr(
        vision_input_module,
        "DurableNodeAttemptReplayService",
        forbidden_access,
    )
    monkeypatch.setattr(
        vision_input_module,
        "DurableTransitionReplayService",
        forbidden_access,
    )
    monkeypatch.setattr(
        "google.adk.sessions.DatabaseSessionService",
        forbidden_access,
    )
    event.listen(engine, "before_cursor_execute", record_query)
    try:
        VisionInterviewInputService(engine=engine).build(
            project_id=project_id,
            decision=_decision(),
            user_text="Keep provider prompts focused on human intent.",
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_query)
    return {
        match.group(1)
        for statement in statements
        for match in re.finditer(r"\b(?:from|join)\s+\"?([a-z_]+)", statement)
    }


def test_build_initial_queries_only_vision_input_tables(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initial Vision input reads no Goal, workflow, or ADK state."""
    with Session(engine) as session:
        project = Project(
            name="Vision input isolation",
            description="Only Vision facts may be read.",
            origin="greenfield",
        )
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id

    assert _observed_query_tables(
        engine,
        project_id=project_id,
        monkeypatch=monkeypatch,
    ) == {
        "projects",
        "vision_artifact_decisions",
        "vision_artifacts",
        "vision_interview_turns",
        "vision_revision_intents",
    }


def test_build_revision_adds_only_validated_goal_guard_tables(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revision input adds only the durable Goal rows needed by its guard."""
    with Session(engine) as session:
        project = Project(name="Vision revision input", origin="greenfield")
        session.add(project)
        session.flush()
        _seed_open_revision(session, project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id

    assert _observed_query_tables(
        engine,
        project_id=project_id,
        monkeypatch=monkeypatch,
    ) == {
        "product_goal_artifact_decisions",
        "product_goal_artifacts",
        "product_goal_interview_turns",
        "product_goal_outcomes",
        "projects",
        "vision_artifact_decisions",
        "vision_artifacts",
        "vision_interview_turns",
        "vision_revision_intents",
    }
