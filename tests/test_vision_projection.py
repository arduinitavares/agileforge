"""Durable Vision projection regressions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from models.core import Project
from models.db import set_sqlite_pragma
from models.product_definition import VisionInterviewTurn
from services.read_projections import DurableReadProjectionService
from services.vision_projection import VisionLineageError, load_current_accepted_vision
from tests.vision_lineage_fixtures import (
    seed_accepted_vision,
    seed_accepted_vision_revision,
)
from workflow.facts import (
    ProjectFact,
    VisionArtifactDecisionFact,
    VisionArtifactFact,
    VisionEvidenceSnapshotFact,
    VisionInterviewTurnFact,
    VisionRevisionIntentFact,
    WorkflowFactSnapshot,
)
from workflow.fingerprints import vision_interview_output_fingerprint

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from workflow.contracts import JsonObject, JsonValue


NOW = datetime(2026, 8, 10, 16, tzinfo=UTC)


def _vision_components(*, complete: bool) -> JsonObject:
    return {
        "project_name": "AgileForge <script>draft()</script>",
        "target_user": "Product teams",
        "problem": "Workflow context is fragmented",
        "product_category": "Product delivery system",
        "key_benefit": "Durable review context",
        "competitors": "Mutable chat history" if complete else None,
        "differentiator": "Evidence-grounded workflow" if complete else None,
    }


def _component_basis(*, complete: bool) -> tuple[JsonObject, ...]:
    names = tuple(
        name for name, value in _vision_components(complete=complete).items() if value
    )
    return tuple(
        {
            "component": name,
            "source_kinds": ["evidence", "inference"],
            "evidence_ids": ["evidence-secret-id"],
            "assumption_ids": ["assumption-secret-id"],
        }
        for name in names
    )


def _vision_snapshot(*, complete: bool) -> WorkflowFactSnapshot:
    components = _vision_components(complete=complete)
    basis = _component_basis(complete=complete)
    assumptions: tuple[JsonObject, ...] = (
        {
            "assumption_id": "assumption-secret-id",
            "text": "Teams can adopt a durable review flow.",
            "affected_components": ["key_benefit"],
        },
    )
    conflicts: tuple[JsonObject, ...] = (
        {
            "conflict_id": "conflict-secret-id",
            "text": "The primary alternative is still disputed.",
            "status": "resolved" if complete else "unresolved",
            "affected_components": ["competitors"],
            "evidence_ids": ["evidence-secret-id"],
            "assumption_ids": ["assumption-secret-id"],
            "resolution": "Use mutable chat history." if complete else None,
        },
    )
    questions: tuple[JsonObject, ...]
    if complete:
        questions = ()
    else:
        questions = (
            {
                "question_id": "question-render-id",
                "text": "Which alternative do teams use today?",
                "affected_components": ["competitors"],
                "conflict_ids": ["conflict-secret-id"],
            },
        )
    turn = VisionInterviewTurnFact(
        vision_interview_turn_id=71,
        operation="bootstrap",
        turn_number=1,
        revision_intent_id=None,
        vision_evidence_snapshot_id=41,
        prior_turn_id=None,
        user_text=None,
        components=components,
        vision_statement="A durable Vision grounded in available context.",
        is_complete=complete,
        clarifying_questions=questions,
        component_basis=basis,
        assumptions=assumptions,
        conflicts=conflicts,
        output_fingerprint="sha256:output-secret",
        workflow_node_attempt_id=31,
        attempt_fingerprint="sha256:attempt-secret",
        recorded_at=NOW,
    )
    artifact = VisionArtifactFact(
        vision_artifact_id=81,
        version_number=1,
        components=components,
        statement=turn.vision_statement,
        content_fingerprint="sha256:review-concurrency-only",
        vision_evidence_snapshot_id=41,
        component_basis=basis,
        assumptions=assumptions,
        conflicts=conflicts,
        supersedes_vision_artifact_id=None,
        source_interview_turn_id=turn.vision_interview_turn_id,
        created_by="model-secret",
        created_at=NOW,
    )
    return WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=1,
            name="Projection safety",
            created_at=NOW,
        ),
        vision_evidence_snapshots=(
            VisionEvidenceSnapshotFact(
                vision_evidence_snapshot_id=41,
                repository_binding_id=21,
                supersedes_vision_evidence_snapshot_id=None,
                workflow_node_attempt_id=31,
                evidence={
                    "raw_path": "/Users/private/worktree",
                    "evidence_id": "evidence-secret-id",
                },
                evidence_fingerprint="sha256:evidence-secret",
                warnings=(),
                created_at=NOW,
            ),
        ),
        vision_interview_turns=(turn,),
        vision_artifacts=(artifact,) if complete else (),
    )


def _vision_status_from_snapshot(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: WorkflowFactSnapshot,
) -> JsonObject:
    reads = DurableReadProjectionService(engine=engine)
    monkeypatch.setattr(reads, "_snapshot", lambda _project_id: snapshot)
    result = reads.vision_status(project_id=1)
    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, dict)
    return data


def _all_keys(value: JsonValue) -> set[str]:
    if isinstance(value, dict):
        return set(value.keys()) | {
            key for item in value.values() for key in _all_keys(item)
        }
    if isinstance(value, list):
        return {key for item in value for key in _all_keys(item)}
    return set()


def _json_object(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value


def _project_id(project: Project) -> int:
    assert project.project_id is not None
    return project.project_id


def test_projection_rejects_incomplete_source_turn(engine: Engine) -> None:
    """Fail closed when an accepted Vision no longer has a complete source turn."""
    with Session(engine) as session:
        project = Project(name="Incomplete Vision Source")
        session.add(project)
        session.commit()
        artifact = seed_accepted_vision(
            session,
            project_id=_project_id(project),
            statement="Use durable source facts.",
        )
        turn = session.get(VisionInterviewTurn, artifact.source_interview_turn_id)
        assert turn is not None
        turn.is_complete = False
        turn.output_fingerprint = vision_interview_output_fingerprint(
            json.loads(turn.components_json),
            turn.vision_statement,
            False,
            json.loads(turn.clarifying_questions_json),
            {
                "component_basis": json.loads(turn.component_basis_json),
                "assumptions": json.loads(turn.assumptions_json),
                "conflicts": json.loads(turn.conflicts_json),
            },
        )
        session.add(turn)
        session.commit()

        with pytest.raises(VisionLineageError, match="complete source interview turn"):
            load_current_accepted_vision(session, project_id=_project_id(project))


def test_projection_returns_the_accepted_superseding_leaf(tmp_path: Path) -> None:
    """Return the sole accepted leaf after a valid durable Vision revision."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'vision-projection.db'}",
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", set_sqlite_pragma)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="Revised Vision")
        session.add(project)
        session.commit()
        initial = seed_accepted_vision(
            session,
            project_id=_project_id(project),
            statement="Initial durable Vision.",
        )
        revised = seed_accepted_vision_revision(
            session,
            project_id=_project_id(project),
            superseded_vision=initial,
            statement="Revised durable Vision.",
        )

        accepted = load_current_accepted_vision(
            session,
            project_id=_project_id(project),
        )
        revised_id = revised.vision_artifact_id
        revised_fingerprint = revised.content_fingerprint

    assert accepted is not None
    assert accepted.vision_artifact_id == revised_id
    assert accepted.fingerprint == revised_fingerprint
    assert accepted.statement == "Revised durable Vision."
    engine.dispose()


def test_initial_vision_status_offers_bootstrap_without_an_active_draft(
    engine: Engine,
) -> None:
    """An empty Vision lineage advertises explicit bootstrap only."""
    with Session(engine) as session:
        project = Project(name="Initial Vision bootstrap")
        session.add(project)
        session.commit()
        project_id = _project_id(project)

    result = DurableReadProjectionService(engine=engine).vision_status(
        project_id=project_id
    )

    assert result["ok"] is True
    assert result["data"] == {
        "bootstrap_available": True,
        "current": None,
        "draft": None,
        "transcript": [],
        "candidate": None,
        "review": None,
        "stale_reason": "VISION_NOT_ACCEPTED",
    }


def test_incomplete_vision_status_projects_display_safe_review_material(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An incomplete draft exposes review prose without raw lineage facts."""
    data = _vision_status_from_snapshot(
        engine,
        monkeypatch,
        _vision_snapshot(complete=False),
    )

    assert data["bootstrap_available"] is False
    assert data["candidate"] is None
    assert data["review"] is None
    assert data["transcript"] == []
    draft = _json_object(data["draft"])
    assert draft["statement"] == "A durable Vision grounded in available context."
    draft_components = draft["components"]
    assert isinstance(draft_components, list)
    assert {
        "name": "key_benefit",
        "value": "Durable review context",
        "source_kinds": ["evidence", "inference"],
    } in draft_components
    assert draft["assumptions"] == [
        {
            "text": "Teams can adopt a durable review flow.",
            "affected_components": ["key_benefit"],
        }
    ]
    assert draft["conflicts"] == [
        {
            "text": "The primary alternative is still disputed.",
            "status": "unresolved",
            "affected_components": ["competitors"],
            "resolution": None,
        }
    ]
    assert draft["questions"] == [
        {
            "question_id": "question-render-id",
            "text": "Which alternative do teams use today?",
            "affected_components": ["competitors"],
        }
    ]


def test_complete_vision_status_projects_review_material_and_only_review_binding(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending candidate retains only its required review fingerprint."""
    data = _vision_status_from_snapshot(
        engine,
        monkeypatch,
        _vision_snapshot(complete=True),
    )

    assert data["bootstrap_available"] is False
    assert data["draft"] is None
    assert data["review"] == {"state": "pending", "rationale": None}
    candidate = _json_object(data["candidate"])
    assert candidate["review_fingerprint"] == "sha256:review-concurrency-only"
    assert candidate["statement"] == "A durable Vision grounded in available context."
    assert candidate["questions"] == []
    assert candidate["assumptions"]
    assert candidate["conflicts"]


def test_open_revision_offers_bootstrap_while_retaining_accepted_vision(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit revision intent offers generation without leaking its identity."""
    base = _vision_snapshot(complete=True)
    artifact = base.vision_artifacts[0]
    snapshot = base.model_copy(
        update={
            "vision_artifact_decisions": (
                VisionArtifactDecisionFact(
                    vision_artifact_decision_id=91,
                    vision_artifact_id=artifact.vision_artifact_id,
                    artifact_fingerprint=artifact.content_fingerprint,
                    decision="accepted",
                    rationale="Accepted before revision.",
                    reviewer="reviewer-secret",
                    idempotency_key="decision-secret",
                    decided_at=NOW,
                ),
            ),
            "vision_revision_intents": (
                VisionRevisionIntentFact(
                    vision_revision_intent_id=101,
                    source_vision_artifact_id=artifact.vision_artifact_id,
                    source_vision_fingerprint=artifact.content_fingerprint,
                    reason="Revisit the target audience.",
                    initiated_by="operator-secret",
                    initiated_at=NOW,
                ),
            ),
        }
    )

    data = _vision_status_from_snapshot(engine, monkeypatch, snapshot)

    assert data["bootstrap_available"] is True
    assert data["current"] == {"statement": artifact.statement}
    assert data["draft"] is None
    assert data["candidate"] is None
    assert "101" not in json.dumps(data)


def test_vision_status_omits_raw_provenance_and_persistence_identities(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vision status never passes persistence or model provenance through."""
    incomplete = _vision_status_from_snapshot(
        engine,
        monkeypatch,
        _vision_snapshot(complete=False),
    )
    complete = _vision_status_from_snapshot(
        engine,
        monkeypatch,
        _vision_snapshot(complete=True),
    )
    forbidden_keys = {
        "attempt_fingerprint",
        "created_by",
        "evidence_fingerprint",
        "evidence_ids",
        "output_fingerprint",
        "prior_turn_id",
        "repository_binding_id",
        "revision_intent_id",
        "source_interview_turn_id",
        "supersedes_vision_artifact_id",
        "supersedes_vision_evidence_snapshot_id",
        "vision_artifact_id",
        "vision_evidence_snapshot_id",
        "vision_interview_turn_id",
        "workflow_node_attempt_id",
    }

    for data in (incomplete, complete):
        assert _all_keys(data).isdisjoint(forbidden_keys)
        serialized = json.dumps(data)
        assert "/Users/private/worktree" not in serialized
        assert "evidence-secret-id" not in serialized
        assert "assumption-secret-id" not in serialized
        assert "conflict-secret-id" not in serialized
        assert "sha256:attempt-secret" not in serialized
        assert "sha256:evidence-secret" not in serialized
        assert "sha256:output-secret" not in serialized
