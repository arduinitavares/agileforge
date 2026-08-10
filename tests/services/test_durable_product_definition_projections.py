"""Durable product-definition read projections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import TypeAdapter
from sqlmodel import Session

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
)
from models.specs import SpecRegistry
from models.workflow import WorkflowNodeAttempt
from repositories.workflow import WorkflowFactRepository
from services.read_projections import DurableReadProjectionService
from workflow.contracts import GRAPH_VERSION, JsonObject, JsonValue
from workflow.definitions.product_discovery import select_product_definition_state
from workflow.fingerprints import (
    canonical_hash,
    canonical_json,
    product_goal_artifact_fingerprint,
    product_goal_interview_output_fingerprint,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


NOW = datetime(2026, 8, 5, 14, tzinfo=UTC)
_JSON_OBJECT = TypeAdapter(JsonObject)


def test_public_product_definition_selection_retains_projection_state(
    engine: Engine,
) -> None:
    """Read projections share one stable selection interface with graph rules."""
    seeded = _seed_lineage(engine)
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)

    selection = select_product_definition_state(snapshot)

    assert selection.discovery is not None
    assert selection.specification_candidate is not None
    assert selection.accepted_spec is None
    assert not selection.has_conflict


def _seed_lineage(
    engine: Engine,
    *,
    goal_number: int = 1,
) -> dict[str, object]:
    """Seed one valid durable Vision, Goal, discovery, and candidate chain."""
    with Session(engine) as session:
        project = Project(
            name="Projection contract",
            vision="mutable cache must not be read",
            spec_file_path="/must/not/be/read.json",
        )
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
            model_id="fake/product-definition",
            execution_settings_json="{}",
            idempotency_key=f"attempt-{goal_number}",
            actor="operator",
            correlation_id=None,
            started_at=NOW,
            lease_expires_at=NOW + timedelta(minutes=1),
            attempt_fingerprint=f"sha256:attempt-{goal_number}",
        )
        session.add(attempt)
        session.flush()
        assert attempt.workflow_node_attempt_id is not None

        vision_components = {"purpose": "durable reads"}
        vision_turn = VisionInterviewTurn(
            project_id=project.project_id,
            mode="initial",
            turn_number=1,
            revision_intent_id=None,
            prior_turn_id=None,
            user_text="Define vision",
            components_json=canonical_json(vision_components),
            vision_statement="A durable Vision.",
            is_complete=True,
            clarifying_questions_json="[]",
            output_fingerprint=canonical_hash(
                {
                    "components_json": vision_components,
                    "vision_statement": "A durable Vision.",
                    "is_complete": True,
                    "clarifying_questions_json": [],
                }
            ),
            workflow_node_attempt_id=attempt.workflow_node_attempt_id,
            attempt_fingerprint=attempt.attempt_fingerprint,
            recorded_at=NOW,
        )
        session.add(vision_turn)
        session.flush()
        assert vision_turn.vision_interview_turn_id is not None
        vision = VisionArtifact(
            project_id=project.project_id,
            version_number=1,
            components_json=canonical_json(vision_components),
            statement="A durable Vision.",
            content_fingerprint=canonical_hash(
                {"components": vision_components, "statement": "A durable Vision."}
            ),
            supersedes_vision_artifact_id=None,
            source_interview_turn_id=vision_turn.vision_interview_turn_id,
            created_by="operator",
            created_at=NOW + timedelta(seconds=1),
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
                rationale="Reviewed.",
                reviewer="operator",
                idempotency_key=f"vision-{goal_number}",
                decided_at=NOW + timedelta(seconds=2),
            )
        )

        goal_components = {
            "valuable_future_state": "Reliable decisions",
            "beneficiary": "Operators",
            "value": "Confidence",
            "success_signals": ["Measured outcomes"],
            "boundaries": ["No implementation"],
        }
        goal_statement = f"Goal {goal_number}: reliable decisions."
        goal_turn = ProductGoalInterviewTurn(
            project_id=project.project_id,
            vision_artifact_id=vision.vision_artifact_id,
            vision_fingerprint=vision.content_fingerprint,
            goal_number=goal_number,
            revision_number=1,
            prior_turn_id=None,
            user_text="Define goal",
            components_json=canonical_json(goal_components),
            goal_statement=goal_statement,
            is_complete=True,
            clarifying_questions_json="[]",
            output_fingerprint=product_goal_interview_output_fingerprint(
                goal_components, goal_statement, True, ()
            ),
            workflow_node_attempt_id=attempt.workflow_node_attempt_id,
            attempt_fingerprint=attempt.attempt_fingerprint,
            recorded_at=NOW + timedelta(seconds=3),
        )
        session.add(goal_turn)
        session.flush()
        assert goal_turn.product_goal_interview_turn_id is not None
        goal = ProductGoalArtifact(
            project_id=project.project_id,
            vision_artifact_id=vision.vision_artifact_id,
            vision_fingerprint=vision.content_fingerprint,
            goal_number=goal_number,
            revision_number=1,
            statement=goal_statement,
            content_fingerprint=product_goal_artifact_fingerprint(
                goal_components, goal_statement
            ),
            supersedes_product_goal_artifact_id=None,
            source_interview_turn_id=goal_turn.product_goal_interview_turn_id,
            created_by="operator",
            created_at=NOW + timedelta(seconds=4),
        )
        session.add(goal)
        session.flush()
        assert goal.product_goal_artifact_id is not None
        session.add(
            ProductGoalArtifactDecision(
                project_id=project.project_id,
                product_goal_artifact_id=goal.product_goal_artifact_id,
                artifact_fingerprint=goal.content_fingerprint,
                decision="accepted",
                rationale="Reviewed.",
                reviewer="operator",
                idempotency_key=f"goal-{goal_number}",
                decided_at=NOW + timedelta(seconds=5),
            )
        )

        discovery_content = {"evidence": "repository facts"}
        discovery = DiscoveryArtifact(
            project_id=project.project_id,
            vision_artifact_id=vision.vision_artifact_id,
            vision_fingerprint=vision.content_fingerprint,
            product_goal_artifact_id=goal.product_goal_artifact_id,
            product_goal_fingerprint=goal.content_fingerprint,
            canonical_content_json=canonical_json(discovery_content),
            content_fingerprint=canonical_hash(discovery_content),
            content_ref="evidence/discovery.json",
            producer="grill-me-with-docs",
            supersedes_discovery_artifact_id=None,
            recorded_by="operator",
            recorded_at=NOW + timedelta(seconds=6),
        )
        session.add(discovery)
        session.flush()
        assert discovery.discovery_artifact_id is not None
        specification_content = {"title": "Durable specification"}
        candidate = SpecificationCandidate(
            project_id=project.project_id,
            vision_artifact_id=vision.vision_artifact_id,
            vision_fingerprint=vision.content_fingerprint,
            product_goal_artifact_id=goal.product_goal_artifact_id,
            product_goal_fingerprint=goal.content_fingerprint,
            discovery_artifact_id=discovery.discovery_artifact_id,
            discovery_fingerprint=discovery.content_fingerprint,
            base_spec_version_id=None,
            base_spec_hash=None,
            canonical_content_json=canonical_json(specification_content),
            content_fingerprint=canonical_hash(specification_content),
            content_ref="specs/durable.json",
            supersedes_specification_candidate_id=None,
            recorded_by="operator",
            recorded_at=NOW + timedelta(seconds=7),
        )
        session.add(candidate)
        session.flush()
        assert candidate.specification_candidate_id is not None
        result = {
            "project_id": project.project_id,
            "vision_id": vision.vision_artifact_id,
            "vision_fingerprint": vision.content_fingerprint,
            "goal_id": goal.product_goal_artifact_id,
            "goal_fingerprint": goal.content_fingerprint,
            "goal_statement": goal.statement,
            "discovery_id": discovery.discovery_artifact_id,
            "discovery_fingerprint": discovery.content_fingerprint,
            "candidate_id": candidate.specification_candidate_id,
            "candidate_fingerprint": candidate.content_fingerprint,
            "candidate_content_ref": candidate.content_ref,
            "candidate_content_json": candidate.canonical_content_json,
            "discovery_content": discovery_content,
            "specification_content": specification_content,
            "attempt_id": attempt.workflow_node_attempt_id,
            "attempt_fingerprint": attempt.attempt_fingerprint,
        }
        session.commit()
        return result


def _json_object(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return _JSON_OBJECT.validate_python(value)


def _data(result: JsonObject) -> JsonObject:
    assert result["ok"] is True
    return _json_object(result["data"])


def _error_code(result: JsonObject) -> str:
    assert result["ok"] is False
    errors = result["errors"]
    assert isinstance(errors, list)
    assert errors
    code = _json_object(errors[0])["code"]
    assert isinstance(code, str)
    return code


def _stored_iso(value: datetime) -> str:
    """Match SQLite's durable naive-datetime representation."""
    return value.replace(tzinfo=None).isoformat()


def _seeded_int(seeded: dict[str, object], key: str) -> int:
    value = seeded[key]
    assert isinstance(value, int)
    return value


def _seed_interview_project(engine: Engine) -> dict[str, object]:
    """Seed one Project and durable attempt used by direct fact fixtures."""
    with Session(engine) as session:
        project = Project(
            name="Interview read contract",
            description="Durable human review state",
        )
        session.add(project)
        session.flush()
        assert project.project_id is not None
        attempt = WorkflowNodeAttempt(
            project_id=project.project_id,
            node_id="vision.interview",
            instance_key=None,
            graph_version=GRAPH_VERSION,
            fact_fingerprint="sha256:interview-facts",
            business_fact_fingerprint="sha256:interview-business",
            decision_fingerprint="sha256:interview-decision",
            normalized_input_json="{}",
            input_fingerprint="sha256:interview-input",
            model_id="fake/interview-read-contract",
            execution_settings_json="{}",
            idempotency_key="interview-read-attempt",
            actor="operator",
            correlation_id=None,
            started_at=NOW,
            lease_expires_at=NOW + timedelta(minutes=1),
            attempt_fingerprint="sha256:interview-attempt",
        )
        session.add(attempt)
        session.flush()
        assert attempt.workflow_node_attempt_id is not None
        result = {
            "project_id": project.project_id,
            "attempt_id": attempt.workflow_node_attempt_id,
            "attempt_fingerprint": attempt.attempt_fingerprint,
        }
        session.commit()
        return result


def _vision_components(*, complete: bool) -> JsonObject:
    return {
        "project_name": "AgileForge",
        "target_user": "Product teams",
        "problem": "Workflow state is hard to review",
        "product_category": "Product delivery system",
        "key_benefit": "Durable review context",
        "competitors": "Mutable chat history" if complete else None,
        "differentiator": "Immutable workflow facts" if complete else None,
    }


def _goal_components(*, complete: bool) -> JsonObject:
    return {
        "valuable_future_state": "Every review uses durable facts",
        "beneficiary": "Product operators",
        "value": "Reliable decisions",
        "success_signals": ["Exact candidates are reviewable"],
        "boundaries": ["No mutable cache reads"] if complete else [],
    }


@dataclass(frozen=True)
class _VisionTurnSeed:
    """Input values for one direct Vision turn fixture."""

    components: JsonObject
    statement: str
    is_complete: bool
    questions: tuple[str, ...]
    turn_number: int
    prior_turn_id: int | None
    recorded_at: datetime


@dataclass(frozen=True)
class _GoalTurnSeed:
    """Input values for one direct Product Goal turn fixture."""

    components: JsonObject
    statement: str
    is_complete: bool
    questions: tuple[str, ...]
    goal_number: int
    revision_number: int
    prior_turn_id: int | None
    recorded_at: datetime


def _add_vision_turn(
    engine: Engine,
    seeded: dict[str, object],
    turn_seed: _VisionTurnSeed,
) -> int:
    project_id = seeded["project_id"]
    attempt_id = seeded["attempt_id"]
    attempt_fingerprint = seeded["attempt_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(attempt_id, int)
    assert isinstance(attempt_fingerprint, str)
    with Session(engine) as session:
        turn = VisionInterviewTurn(
            project_id=project_id,
            mode="initial",
            turn_number=turn_seed.turn_number,
            revision_intent_id=None,
            prior_turn_id=turn_seed.prior_turn_id,
            user_text=f"Vision answer {turn_seed.turn_number}",
            components_json=canonical_json(turn_seed.components),
            vision_statement=turn_seed.statement,
            is_complete=turn_seed.is_complete,
            clarifying_questions_json=canonical_json(list(turn_seed.questions)),
            output_fingerprint=canonical_hash(
                {
                    "components_json": turn_seed.components,
                    "vision_statement": turn_seed.statement,
                    "is_complete": turn_seed.is_complete,
                    "clarifying_questions_json": list(turn_seed.questions),
                }
            ),
            workflow_node_attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            recorded_at=turn_seed.recorded_at,
        )
        session.add(turn)
        session.commit()
        session.refresh(turn)
        assert turn.vision_interview_turn_id is not None
        return turn.vision_interview_turn_id


def _seed_vision_candidate(
    engine: Engine,
    *,
    decision: str | None = None,
) -> dict[str, object]:
    seeded = _seed_interview_project(engine)
    components = _vision_components(complete=True)
    statement = "Product teams review exact durable workflow state."
    turn_id = _add_vision_turn(
        engine,
        seeded,
        _VisionTurnSeed(
            components=components,
            statement=statement,
            is_complete=True,
            questions=(),
            turn_number=1,
            prior_turn_id=None,
            recorded_at=NOW,
        ),
    )
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)
    with Session(engine) as session:
        artifact = VisionArtifact(
            project_id=project_id,
            version_number=1,
            components_json=canonical_json(components),
            statement=statement,
            content_fingerprint=canonical_hash(
                {"components": components, "statement": statement}
            ),
            supersedes_vision_artifact_id=None,
            source_interview_turn_id=turn_id,
            created_by="operator",
            created_at=NOW + timedelta(seconds=1),
        )
        session.add(artifact)
        session.flush()
        assert artifact.vision_artifact_id is not None
        if decision is not None:
            session.add(
                VisionArtifactDecision(
                    project_id=project_id,
                    vision_artifact_id=artifact.vision_artifact_id,
                    artifact_fingerprint=artifact.content_fingerprint,
                    decision=decision,
                    rationale=f"Vision {decision} rationale.",
                    reviewer="vision-reviewer",
                    idempotency_key=f"vision-{decision}",
                    decided_at=NOW + timedelta(seconds=2),
                )
            )
        seeded.update(
            vision_id=artifact.vision_artifact_id,
            vision_fingerprint=artifact.content_fingerprint,
            vision_statement=artifact.statement,
            vision_components=components,
            vision_turn_id=turn_id,
        )
        session.commit()
    return seeded


def _add_goal_turn(
    engine: Engine,
    seeded: dict[str, object],
    turn_seed: _GoalTurnSeed,
) -> int:
    project_id = seeded["project_id"]
    vision_id = seeded["vision_id"]
    vision_fingerprint = seeded["vision_fingerprint"]
    attempt_id = seeded["attempt_id"]
    attempt_fingerprint = seeded["attempt_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(vision_id, int)
    assert isinstance(vision_fingerprint, str)
    assert isinstance(attempt_id, int)
    assert isinstance(attempt_fingerprint, str)
    with Session(engine) as session:
        turn = ProductGoalInterviewTurn(
            project_id=project_id,
            vision_artifact_id=vision_id,
            vision_fingerprint=vision_fingerprint,
            goal_number=turn_seed.goal_number,
            revision_number=turn_seed.revision_number,
            prior_turn_id=turn_seed.prior_turn_id,
            user_text=(
                f"Goal answer {turn_seed.goal_number}.{turn_seed.revision_number}"
            ),
            components_json=canonical_json(turn_seed.components),
            goal_statement=turn_seed.statement,
            is_complete=turn_seed.is_complete,
            clarifying_questions_json=canonical_json(list(turn_seed.questions)),
            output_fingerprint=product_goal_interview_output_fingerprint(
                turn_seed.components,
                turn_seed.statement,
                turn_seed.is_complete,
                turn_seed.questions,
            ),
            workflow_node_attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            recorded_at=turn_seed.recorded_at,
        )
        session.add(turn)
        session.commit()
        session.refresh(turn)
        assert turn.product_goal_interview_turn_id is not None
        return turn.product_goal_interview_turn_id


def _seed_goal_candidate(
    engine: Engine,
    *,
    decision: str | None = None,
) -> dict[str, object]:
    seeded = _seed_vision_candidate(engine, decision="accepted")
    components = _goal_components(complete=True)
    statement = "Make every product-definition review durable."
    turn_id = _add_goal_turn(
        engine,
        seeded,
        _GoalTurnSeed(
            components=components,
            statement=statement,
            is_complete=True,
            questions=(),
            goal_number=1,
            revision_number=1,
            prior_turn_id=None,
            recorded_at=NOW + timedelta(seconds=3),
        ),
    )
    project_id = seeded["project_id"]
    vision_id = seeded["vision_id"]
    vision_fingerprint = seeded["vision_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(vision_id, int)
    assert isinstance(vision_fingerprint, str)
    with Session(engine) as session:
        artifact = ProductGoalArtifact(
            project_id=project_id,
            vision_artifact_id=vision_id,
            vision_fingerprint=vision_fingerprint,
            goal_number=1,
            revision_number=1,
            statement=statement,
            content_fingerprint=product_goal_artifact_fingerprint(
                components, statement
            ),
            supersedes_product_goal_artifact_id=None,
            source_interview_turn_id=turn_id,
            created_by="operator",
            created_at=NOW + timedelta(seconds=4),
        )
        session.add(artifact)
        session.flush()
        assert artifact.product_goal_artifact_id is not None
        if decision is not None:
            session.add(
                ProductGoalArtifactDecision(
                    project_id=project_id,
                    product_goal_artifact_id=artifact.product_goal_artifact_id,
                    artifact_fingerprint=artifact.content_fingerprint,
                    decision=decision,
                    rationale=f"Goal {decision} rationale.",
                    reviewer="goal-reviewer",
                    idempotency_key=f"goal-{decision}",
                    decided_at=NOW + timedelta(seconds=5),
                )
            )
        seeded.update(
            goal_id=artifact.product_goal_artifact_id,
            goal_fingerprint=artifact.content_fingerprint,
            goal_statement=artifact.statement,
            goal_components=components,
            goal_turn_id=turn_id,
        )
        session.commit()
    return seeded


def test_new_project_has_empty_durable_interview_reads(engine: Engine) -> None:
    """A Project with no interview facts exposes an empty, stable contract."""
    with Session(engine) as session:
        project = Project(name="Empty interview state")
        session.add(project)
        session.commit()
        session.refresh(project)
        assert project.project_id is not None
        project_id = project.project_id

    reads = DurableReadProjectionService(engine=engine)

    assert _data(reads.vision_status(project_id=project_id)) == {
        "current": None,
        "transcript": [],
        "latest_questions": [],
        "candidate": None,
        "review": None,
        "stale_reason": "VISION_NOT_ACCEPTED",
    }
    assert _data(reads.product_goal_status(project_id=project_id)) == {
        "accepted_vision": None,
        "active": None,
        "transcript": [],
        "latest_questions": [],
        "candidate": None,
        "review": None,
        "outcome": None,
        "stale_reason": "GOAL_NOT_ACTIVE",
    }


def test_incomplete_vision_turn_exposes_exact_transcript_and_questions(
    engine: Engine,
) -> None:
    """The read contract preserves one incomplete Vision turn verbatim."""
    seeded = _seed_interview_project(engine)
    components = _vision_components(complete=False)
    questions = ("What alternatives do product teams use today?",)
    statement = "Product teams need durable workflow review."
    turn_id = _add_vision_turn(
        engine,
        seeded,
        _VisionTurnSeed(
            components=components,
            statement=statement,
            is_complete=False,
            questions=questions,
            turn_number=1,
            prior_turn_id=None,
            recorded_at=NOW,
        ),
    )
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)

    data = _data(
        DurableReadProjectionService(engine=engine).vision_status(project_id=project_id)
    )

    assert data["transcript"] == [
        {
            "vision_interview_turn_id": turn_id,
            "mode": "initial",
            "turn_number": 1,
            "revision_intent_id": None,
            "prior_turn_id": None,
            "user_text": "Vision answer 1",
            "statement": statement,
            "components": components,
            "is_complete": False,
            "clarifying_questions": list(questions),
            "output_fingerprint": canonical_hash(
                {
                    "components_json": components,
                    "vision_statement": statement,
                    "is_complete": False,
                    "clarifying_questions_json": list(questions),
                }
            ),
            "recorded_at": _stored_iso(NOW),
        }
    ]
    assert data["latest_questions"] == list(questions)
    assert data["candidate"] is None
    assert data["review"] is None


def test_pending_vision_exposes_exact_candidate_and_pending_review(
    engine: Engine,
) -> None:
    """A complete Vision turn projects the immutable candidate it created."""
    seeded = _seed_vision_candidate(engine)
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)

    data = _data(
        DurableReadProjectionService(engine=engine).vision_status(project_id=project_id)
    )

    assert data["candidate"] == {
        "vision_artifact_id": seeded["vision_id"],
        "version_number": 1,
        "fingerprint": seeded["vision_fingerprint"],
        "statement": seeded["vision_statement"],
        "components": seeded["vision_components"],
        "supersedes_vision_artifact_id": None,
        "source_interview_turn_id": seeded["vision_turn_id"],
        "created_by": "operator",
        "created_at": _stored_iso(NOW + timedelta(seconds=1)),
    }
    assert data["review"] == {"state": "pending"}
    transcript = data["transcript"]
    assert isinstance(transcript, list)
    assert len(transcript) == 1
    assert data["latest_questions"] == []


def test_vision_feedback_keeps_reviewed_candidate_separate_from_revision_chain(
    engine: Engine,
) -> None:
    """Feedback context remains exact while only new turns are current."""
    seeded = _seed_vision_candidate(engine, decision="feedback")
    components = _vision_components(complete=False)
    questions = ("Which differentiator should the revision emphasize?",)
    revision_turn_id = _add_vision_turn(
        engine,
        seeded,
        _VisionTurnSeed(
            components=components,
            statement="Product teams need a sharper durable workflow Vision.",
            is_complete=False,
            questions=questions,
            turn_number=2,
            prior_turn_id=_seeded_int(seeded, "vision_turn_id"),
            recorded_at=NOW + timedelta(seconds=3),
        ),
    )
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)

    data = _data(
        DurableReadProjectionService(engine=engine).vision_status(project_id=project_id)
    )

    candidate = _json_object(data["candidate"])
    assert candidate["vision_artifact_id"] == seeded["vision_id"]
    assert data["review"] == {
        "state": "feedback",
        "vision_artifact_decision_id": 1,
        "decision": "feedback",
        "rationale": "Vision feedback rationale.",
        "reviewer": "vision-reviewer",
        "decided_at": _stored_iso(NOW + timedelta(seconds=2)),
    }
    transcript = data["transcript"]
    assert isinstance(transcript, list)
    assert [_json_object(item)["vision_interview_turn_id"] for item in transcript] == [
        revision_turn_id
    ]
    assert data["latest_questions"] == list(questions)


def test_accepted_vision_has_current_artifact_and_no_pending_candidate(
    engine: Engine,
) -> None:
    """Acceptance promotes current Vision while preserving terminal review."""
    seeded = _seed_vision_candidate(engine, decision="accepted")
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)

    data = _data(
        DurableReadProjectionService(engine=engine).vision_status(project_id=project_id)
    )

    assert data["current"] == {
        "vision_artifact_id": seeded["vision_id"],
        "fingerprint": seeded["vision_fingerprint"],
        "statement": seeded["vision_statement"],
    }
    assert data["candidate"] is None
    assert data["transcript"] == []
    assert data["latest_questions"] == []
    assert data["review"] == {
        "state": "accepted",
        "vision_artifact_decision_id": 1,
        "decision": "accepted",
        "rationale": "Vision accepted rationale.",
        "reviewer": "vision-reviewer",
        "decided_at": _stored_iso(NOW + timedelta(seconds=2)),
    }
    assert data["stale_reason"] is None


def test_incomplete_goal_exposes_accepted_vision_transcript_and_questions(
    engine: Engine,
) -> None:
    """Goal interview reads include immutable accepted Vision context."""
    seeded = _seed_vision_candidate(engine, decision="accepted")
    components = _goal_components(complete=False)
    questions = ("What is outside this Product Goal?",)
    statement = "Make product-definition reviews durable."
    turn_id = _add_goal_turn(
        engine,
        seeded,
        _GoalTurnSeed(
            components=components,
            statement=statement,
            is_complete=False,
            questions=questions,
            goal_number=1,
            revision_number=1,
            prior_turn_id=None,
            recorded_at=NOW + timedelta(seconds=3),
        ),
    )
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)

    data = _data(
        DurableReadProjectionService(engine=engine).product_goal_status(
            project_id=project_id
        )
    )

    assert data["accepted_vision"] == {
        "vision_artifact_id": seeded["vision_id"],
        "fingerprint": seeded["vision_fingerprint"],
        "statement": seeded["vision_statement"],
    }
    assert data["transcript"] == [
        {
            "product_goal_interview_turn_id": turn_id,
            "vision_artifact_id": seeded["vision_id"],
            "vision_fingerprint": seeded["vision_fingerprint"],
            "goal_number": 1,
            "revision_number": 1,
            "prior_turn_id": None,
            "user_text": "Goal answer 1.1",
            "statement": statement,
            "components": components,
            "is_complete": False,
            "clarifying_questions": list(questions),
            "output_fingerprint": product_goal_interview_output_fingerprint(
                components, statement, False, questions
            ),
            "recorded_at": _stored_iso(NOW + timedelta(seconds=3)),
        }
    ]
    assert data["latest_questions"] == list(questions)
    assert data["candidate"] is None
    assert data["review"] is None


def test_pending_goal_exposes_exact_candidate_and_pending_review(
    engine: Engine,
) -> None:
    """A complete Goal turn projects its exact immutable candidate."""
    seeded = _seed_goal_candidate(engine)
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)

    data = _data(
        DurableReadProjectionService(engine=engine).product_goal_status(
            project_id=project_id
        )
    )

    assert data["candidate"] == {
        "product_goal_artifact_id": seeded["goal_id"],
        "vision_artifact_id": seeded["vision_id"],
        "vision_fingerprint": seeded["vision_fingerprint"],
        "goal_number": 1,
        "revision_number": 1,
        "fingerprint": seeded["goal_fingerprint"],
        "statement": seeded["goal_statement"],
        "components": seeded["goal_components"],
        "supersedes_product_goal_artifact_id": None,
        "source_interview_turn_id": seeded["goal_turn_id"],
        "created_by": "operator",
        "created_at": _stored_iso(NOW + timedelta(seconds=4)),
    }
    assert data["review"] == {"state": "pending"}
    transcript = data["transcript"]
    assert isinstance(transcript, list)
    assert len(transcript) == 1
    assert data["latest_questions"] == []


def test_goal_feedback_keeps_candidate_separate_from_revision_chain(
    engine: Engine,
) -> None:
    """A rejected Goal remains review context while revision turns advance."""
    seeded = _seed_goal_candidate(engine, decision="feedback")
    components = _goal_components(complete=False)
    questions = ("Which boundary should the revision add?",)
    revision_turn_id = _add_goal_turn(
        engine,
        seeded,
        _GoalTurnSeed(
            components=components,
            statement="Make durable reviews narrower and measurable.",
            is_complete=False,
            questions=questions,
            goal_number=1,
            revision_number=2,
            prior_turn_id=None,
            recorded_at=NOW + timedelta(seconds=6),
        ),
    )
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)

    data = _data(
        DurableReadProjectionService(engine=engine).product_goal_status(
            project_id=project_id
        )
    )

    candidate = _json_object(data["candidate"])
    assert candidate["product_goal_artifact_id"] == seeded["goal_id"]
    assert data["review"] == {
        "state": "feedback",
        "product_goal_artifact_decision_id": 1,
        "decision": "feedback",
        "rationale": "Goal feedback rationale.",
        "reviewer": "goal-reviewer",
        "decided_at": _stored_iso(NOW + timedelta(seconds=5)),
    }
    transcript = data["transcript"]
    assert isinstance(transcript, list)
    assert [
        _json_object(item)["product_goal_interview_turn_id"] for item in transcript
    ] == [revision_turn_id]
    assert data["latest_questions"] == list(questions)


def test_resolved_goal_followed_by_new_interview_excludes_old_candidate(
    engine: Engine,
) -> None:
    """The next Goal transcript does not revive the resolved Goal candidate."""
    seeded = _seed_goal_candidate(engine, decision="accepted")
    project_id = seeded["project_id"]
    goal_id = seeded["goal_id"]
    goal_fingerprint = seeded["goal_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(goal_id, int)
    assert isinstance(goal_fingerprint, str)
    with Session(engine) as session:
        session.add(
            ProductGoalOutcome(
                project_id=project_id,
                product_goal_artifact_id=goal_id,
                artifact_fingerprint=goal_fingerprint,
                outcome="fulfilled",
                rationale="The first durable review Goal was fulfilled.",
                decided_by="goal-owner",
                idempotency_key="goal-one-fulfilled",
                decided_at=NOW + timedelta(seconds=6),
            )
        )
        session.commit()
    components = _goal_components(complete=False)
    questions = ("What should the next measurable outcome be?",)
    new_turn_id = _add_goal_turn(
        engine,
        seeded,
        _GoalTurnSeed(
            components=components,
            statement="Define the next durable product outcome.",
            is_complete=False,
            questions=questions,
            goal_number=2,
            revision_number=1,
            prior_turn_id=None,
            recorded_at=NOW + timedelta(seconds=7),
        ),
    )

    data = _data(
        DurableReadProjectionService(engine=engine).product_goal_status(
            project_id=project_id
        )
    )

    assert data["active"] is None
    assert data["candidate"] is None
    assert data["review"] is None
    transcript = data["transcript"]
    assert isinstance(transcript, list)
    assert [
        _json_object(item)["product_goal_interview_turn_id"] for item in transcript
    ] == [new_turn_id]
    outcome = _json_object(data["outcome"])
    assert outcome["product_goal_artifact_id"] == goal_id
    assert data["stale_reason"] == "GOAL_RESOLVED"


def test_ambiguous_vision_leaf_fails_closed_with_typed_stale_reason(
    engine: Engine,
) -> None:
    """Two immutable Vision leaves never degrade to latest-row selection."""
    seeded = _seed_vision_candidate(engine, decision="accepted")
    components = _vision_components(complete=True)
    statement = "A conflicting durable Vision leaf."
    turn_id = _add_vision_turn(
        engine,
        seeded,
        _VisionTurnSeed(
            components=components,
            statement=statement,
            is_complete=True,
            questions=(),
            turn_number=2,
            prior_turn_id=_seeded_int(seeded, "vision_turn_id"),
            recorded_at=NOW + timedelta(seconds=3),
        ),
    )
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)
    with Session(engine) as session:
        session.add(
            VisionArtifact(
                project_id=project_id,
                version_number=2,
                components_json=canonical_json(components),
                statement=statement,
                content_fingerprint=canonical_hash(
                    {"components": components, "statement": statement}
                ),
                supersedes_vision_artifact_id=None,
                source_interview_turn_id=turn_id,
                created_by="operator",
                created_at=NOW + timedelta(seconds=4),
            )
        )
        session.commit()

    data = _data(
        DurableReadProjectionService(engine=engine).vision_status(project_id=project_id)
    )

    assert data == {
        "current": None,
        "transcript": [],
        "latest_questions": [],
        "candidate": None,
        "review": None,
        "stale_reason": "VISION_FACT_CONFLICT",
    }


def _resolve_goal(engine: Engine, seeded: dict[str, object]) -> None:
    """Record the exact terminal outcome for the fixture's accepted first Goal."""
    project_id = seeded["project_id"]
    goal_id = seeded["goal_id"]
    goal_fingerprint = seeded["goal_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(goal_id, int)
    assert isinstance(goal_fingerprint, str)
    with Session(engine) as session:
        session.add(
            ProductGoalOutcome(
                project_id=project_id,
                product_goal_artifact_id=goal_id,
                artifact_fingerprint=goal_fingerprint,
                outcome="fulfilled",
                rationale="Observable success signals were reached.",
                decided_by="operator",
                idempotency_key="goal-fulfilled",
                decided_at=NOW + timedelta(seconds=8),
            )
        )
        session.commit()


def _accept_next_goal(engine: Engine, seeded: dict[str, object]) -> None:
    """Add the next accepted Goal under the unchanged accepted Vision."""
    project_id = seeded["project_id"]
    vision_id = seeded["vision_id"]
    vision_fingerprint = seeded["vision_fingerprint"]
    attempt_id = seeded["attempt_id"]
    attempt_fingerprint = seeded["attempt_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(vision_id, int)
    assert isinstance(vision_fingerprint, str)
    assert isinstance(attempt_id, int)
    assert isinstance(attempt_fingerprint, str)
    components = {
        "valuable_future_state": "Transparent decisions",
        "beneficiary": "Operators",
        "value": "Trust",
        "success_signals": ["Auditable outcomes"],
        "boundaries": ["No implementation"],
    }
    statement = "Goal 2: transparent decisions."
    with Session(engine) as session:
        turn = ProductGoalInterviewTurn(
            project_id=project_id,
            vision_artifact_id=vision_id,
            vision_fingerprint=vision_fingerprint,
            goal_number=2,
            revision_number=1,
            prior_turn_id=None,
            user_text="Define the next goal",
            components_json=canonical_json(components),
            goal_statement=statement,
            is_complete=True,
            clarifying_questions_json="[]",
            output_fingerprint=product_goal_interview_output_fingerprint(
                components, statement, True, ()
            ),
            workflow_node_attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            recorded_at=NOW + timedelta(seconds=9),
        )
        session.add(turn)
        session.flush()
        assert turn.product_goal_interview_turn_id is not None
        goal = ProductGoalArtifact(
            project_id=project_id,
            vision_artifact_id=vision_id,
            vision_fingerprint=vision_fingerprint,
            goal_number=2,
            revision_number=1,
            statement=statement,
            content_fingerprint=product_goal_artifact_fingerprint(
                components, statement
            ),
            supersedes_product_goal_artifact_id=None,
            source_interview_turn_id=turn.product_goal_interview_turn_id,
            created_by="operator",
            created_at=NOW + timedelta(seconds=10),
        )
        session.add(goal)
        session.flush()
        assert goal.product_goal_artifact_id is not None
        session.add(
            ProductGoalArtifactDecision(
                project_id=project_id,
                product_goal_artifact_id=goal.product_goal_artifact_id,
                artifact_fingerprint=goal.content_fingerprint,
                decision="accepted",
                rationale="Reviewed.",
                reviewer="operator",
                idempotency_key="goal-2-accepted",
                decided_at=NOW + timedelta(seconds=11),
            )
        )
        session.commit()


def test_durable_projections_expose_current_human_content_and_pending_review(
    engine: Engine,
) -> None:
    """Every status read derives current content from the immutable fact chain."""
    seeded = _seed_lineage(engine)
    project_id = seeded["project_id"]
    assert isinstance(project_id, int)
    vision_id = seeded["vision_id"]
    vision_fingerprint = seeded["vision_fingerprint"]
    goal_id = seeded["goal_id"]
    goal_fingerprint = seeded["goal_fingerprint"]
    goal_statement = seeded["goal_statement"]
    assert isinstance(vision_id, int)
    assert isinstance(vision_fingerprint, str)
    assert isinstance(goal_id, int)
    assert isinstance(goal_fingerprint, str)
    assert isinstance(goal_statement, str)

    reads = DurableReadProjectionService(engine=engine)
    vision_data = _data(reads.vision_status(project_id=project_id))
    goal_data = _data(reads.product_goal_status(project_id=project_id))
    discovery_data = _data(reads.discovery_status(project_id=project_id))
    specification_data = _data(reads.specification_status(project_id=project_id))
    review_data = _data(reads.specification_review(project_id=project_id))

    assert vision_data["current"] == {
        "vision_artifact_id": vision_id,
        "fingerprint": vision_fingerprint,
        "statement": "A durable Vision.",
    }
    active_goal = _json_object(goal_data["active"])
    assert active_goal["product_goal_artifact_id"] == goal_id
    assert active_goal["statement"] == goal_statement
    current_discovery = _json_object(discovery_data["current"])
    assert current_discovery["canonical_content"] == seeded["discovery_content"]
    assert current_discovery["content_ref"] == "evidence/discovery.json"
    assert current_discovery["vision_fingerprint"] == vision_fingerprint
    assert current_discovery["product_goal_fingerprint"] == goal_fingerprint
    candidate_data = _json_object(specification_data["candidate"])
    assert candidate_data["canonical_content"] == seeded["specification_content"]
    assert specification_data["current"] is None
    assert review_data["review"] == {"state": "pending"}
    assert review_data["candidate"] == candidate_data


@pytest.mark.parametrize(
    ("decision", "expect_registry"),
    [("feedback", False), ("rejected", False), ("accepted", True)],
)
def test_specification_projections_expose_terminal_review_and_registry_content(
    engine: Engine,
    decision: str,
    expect_registry: bool,
) -> None:
    """Every terminal review preserves exact candidate content and terminal state."""
    seeded = _seed_lineage(engine)
    project_id = seeded["project_id"]
    candidate_id = seeded["candidate_id"]
    candidate_fingerprint = seeded["candidate_fingerprint"]
    candidate_content_json = seeded["candidate_content_json"]
    candidate_content_ref = seeded["candidate_content_ref"]
    assert isinstance(project_id, int)
    assert isinstance(candidate_id, int)
    assert isinstance(candidate_fingerprint, str)
    assert isinstance(candidate_content_json, str)
    assert isinstance(candidate_content_ref, str)
    with Session(engine) as session:
        session.add(
            SpecificationDecision(
                project_id=project_id,
                specification_candidate_id=candidate_id,
                artifact_fingerprint=candidate_fingerprint,
                decision=decision,
                rationale="Ready.",
                reviewer="operator",
                idempotency_key="spec-accepted",
                decided_at=NOW + timedelta(seconds=8),
            )
        )
        registry: SpecRegistry | None = None
        if expect_registry:
            registry = SpecRegistry(
                project_id=project_id,
                spec_hash=candidate_fingerprint,
                content=candidate_content_json,
                content_ref=candidate_content_ref,
                status="approved",
                source_specification_candidate_id=candidate_id,
                source_vision_artifact_id=seeded["vision_id"],
                source_vision_fingerprint=seeded["vision_fingerprint"],
                source_product_goal_artifact_id=seeded["goal_id"],
                source_product_goal_fingerprint=seeded["goal_fingerprint"],
                source_discovery_artifact_id=seeded["discovery_id"],
                source_discovery_fingerprint=seeded["discovery_fingerprint"],
            )
            session.add(registry)
        session.commit()
        if registry is not None:
            session.refresh(registry)

    reads = DurableReadProjectionService(engine=engine)
    status = _data(reads.specification_status(project_id=project_id))
    review = _data(reads.specification_review(project_id=project_id))

    current = status["current"]
    if registry is None:
        assert current is None
        assert status["stale_reason"] == "SPECIFICATION_NOT_APPROVED"
    else:
        current = _json_object(current)
        assert current["spec_version_id"] == registry.spec_version_id
        assert current["spec_hash"] == candidate_fingerprint
        assert current["canonical_content"] == seeded["specification_content"]
    assert review["review"] == {
        "state": decision,
        "specification_decision_id": 1,
        "decision": decision,
        "rationale": "Ready.",
        "reviewer": "operator",
    }


def test_projection_fails_closed_for_ambiguous_discovery_without_cache_fallback(
    engine: Engine,
) -> None:
    """Ambiguous durable leaves expose a typed stale reason, never a latest row."""
    seeded = _seed_lineage(engine)
    project_id = seeded["project_id"]
    vision_id = seeded["vision_id"]
    vision_fingerprint = seeded["vision_fingerprint"]
    goal_id = seeded["goal_id"]
    goal_fingerprint = seeded["goal_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(vision_id, int)
    assert isinstance(vision_fingerprint, str)
    assert isinstance(goal_id, int)
    assert isinstance(goal_fingerprint, str)
    extra_content = {"evidence": "a second leaf"}
    with Session(engine) as session:
        session.add(
            DiscoveryArtifact(
                project_id=project_id,
                vision_artifact_id=vision_id,
                vision_fingerprint=vision_fingerprint,
                product_goal_artifact_id=goal_id,
                product_goal_fingerprint=goal_fingerprint,
                canonical_content_json=canonical_json(extra_content),
                content_fingerprint=canonical_hash(extra_content),
                content_ref="evidence/second.json",
                producer="grill-me-with-docs",
                supersedes_discovery_artifact_id=None,
                recorded_by="operator",
                recorded_at=NOW + timedelta(seconds=9),
            )
        )
        project = session.get(Project, project_id)
        assert project is not None
        project.vision = "incorrect mutable cache"
        session.add(project)
        session.commit()

    result = _data(
        DurableReadProjectionService(engine=engine).discovery_status(
            project_id=project_id
        )
    )

    assert result == {
        "current": None,
        "stale_reason": "DISCOVERY_FACT_CONFLICT",
    }


def test_resolved_goal_and_next_goal_leave_old_product_definition_non_current(
    engine: Engine,
) -> None:
    """Goal outcome remains visible and a later Goal does not reuse old artifacts."""
    seeded = _seed_lineage(engine)
    project_id = seeded["project_id"]
    goal_id = seeded["goal_id"]
    goal_fingerprint = seeded["goal_fingerprint"]
    assert isinstance(project_id, int)
    assert isinstance(goal_id, int)
    assert isinstance(goal_fingerprint, str)
    _resolve_goal(engine, seeded)

    reads = DurableReadProjectionService(engine=engine)
    resolved = _data(reads.product_goal_status(project_id=project_id))
    assert resolved == {
        "accepted_vision": {
            "vision_artifact_id": seeded["vision_id"],
            "fingerprint": seeded["vision_fingerprint"],
            "statement": "A durable Vision.",
        },
        "active": None,
        "transcript": [],
        "latest_questions": [],
        "candidate": None,
        "review": None,
        "outcome": {
            "product_goal_artifact_id": goal_id,
            "fingerprint": goal_fingerprint,
            "statement": "Goal 1: reliable decisions.",
            "goal_number": 1,
            "revision_number": 1,
            "outcome": "fulfilled",
            "rationale": "Observable success signals were reached.",
            "decided_by": "operator",
        },
        "stale_reason": "GOAL_RESOLVED",
    }

    _accept_next_goal(engine, seeded)

    vision = _data(reads.vision_status(project_id=project_id))
    active = _data(reads.product_goal_status(project_id=project_id))
    discovery = _data(reads.discovery_status(project_id=project_id))
    specification = _data(reads.specification_status(project_id=project_id))
    assert vision["current"] is not None
    assert active["active"] is not None
    assert discovery == {"current": None, "stale_reason": "DISCOVERY_NOT_CURRENT"}
    assert specification == {
        "current": None,
        "candidate": None,
        "review": None,
        "stale_reason": "SPECIFICATION_NOT_CURRENT",
    }


def test_malformed_durable_projection_data_returns_typed_error(engine: Engine) -> None:
    """Loader validation failures are reported as typed reads instead of crashes."""
    seeded = _seed_lineage(engine)
    project_id = seeded["project_id"]
    candidate_id = seeded["candidate_id"]
    assert isinstance(project_id, int)
    assert isinstance(candidate_id, int)
    with Session(engine) as session:
        candidate = session.get(SpecificationCandidate, candidate_id)
        assert candidate is not None
        candidate.canonical_content_json = "not-json"
        session.add(candidate)
        session.commit()

    result = DurableReadProjectionService(engine=engine).specification_status(
        project_id=project_id
    )

    assert _error_code(result) == "PROJECT_FACTS_UNAVAILABLE"
