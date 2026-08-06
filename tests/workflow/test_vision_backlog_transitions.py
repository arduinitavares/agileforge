"""Durable Backlog Goal/Authority lineage tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from sqlmodel import Session

from models.core import Project
from models.specs import CompiledSpecAuthority, SpecRegistry
from models.workflow import BacklogArtifact
from services.agent_workbench.backlog_phase import record_backlog_draft_in_session
from workflow.fingerprints import canonical_hash
from workflow.requests.product_definition import RecordBacklogDraft

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from workflow.contracts import JsonObject

EVALUATED_AT = datetime(2026, 8, 5, 12, tzinfo=UTC)
GOAL_ID = 31
GOAL_FINGERPRINT = "sha256:goal-current"
AUTHORITY_FINGERPRINT = "sha256:authority-current"


def _backlog_content() -> JsonObject:
    return {
        "backlog_items": [
            {
                "priority": 1,
                "requirement": "Persist exact delivery lineage",
                "authority_ref": "REQ.lineage",
                "capability_hint": None,
                "value_driver": "Strategic",
                "justification": "Keeps delivery decisions restart-safe.",
                "estimated_effort": "M",
                "technical_note": None,
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }


def _vision_content(statement: str = "Build reliable product decisions.") -> JsonObject:
    return {
        "updated_components": {
            "project_name": "Task 10 Project",
            "target_user": "Project operators",
            "problem": "Workflow state can drift",
            "product_category": "Developer tool",
            "key_benefit": "Durable decisions",
            "competitors": "Manual process",
            "differentiator": "Fact-derived routing",
        },
        "product_vision_statement": statement,
        "is_complete": True,
        "clarifying_questions": [],
    }


def _seed_project_authority(session: Session) -> tuple[int, int]:
    project = Project(name="Backlog lineage", origin="greenfield")
    session.add(project)
    session.flush()
    assert project.project_id is not None
    spec = SpecRegistry(
        project_id=project.project_id,
        spec_hash="sha256:spec-current",
        content="{}",
        status="approved",
        approved_at=EVALUATED_AT,
        approved_by="operator@example.com",
        source_specification_candidate_id=1,
        source_vision_artifact_id=1,
        source_vision_fingerprint="sha256:vision-current",
        source_product_goal_artifact_id=GOAL_ID,
        source_product_goal_fingerprint=GOAL_FINGERPRINT,
        source_discovery_artifact_id=1,
        source_discovery_fingerprint="sha256:discovery-current",
    )
    session.add(spec)
    session.flush()
    assert spec.spec_version_id is not None
    authority = CompiledSpecAuthority(
        spec_version_id=spec.spec_version_id,
        compiler_version="test",
        prompt_hash="a" * 64,
        compiled_at=EVALUATED_AT,
        compiled_artifact_json="{}",
        scope_themes="[]",
        invariants="[]",
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps="[]",
    )
    session.add(authority)
    session.flush()
    assert authority.authority_id is not None
    return project.project_id, authority.authority_id


def test_backlog_row_persists_exact_goal_and_authority_lineage(engine: Engine) -> None:
    """A stored Backlog carries both durable upstream identities."""
    content = _backlog_content()
    with Session(engine) as session:
        project_id, authority_id = _seed_project_authority(session)
        row = record_backlog_draft_in_session(
            session,
            project_id=project_id,
            authority_id=authority_id,
            authority_fingerprint=AUTHORITY_FINGERPRINT,
            product_goal_artifact_id=GOAL_ID,
            product_goal_fingerprint=GOAL_FINGERPRINT,
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
            supersedes_backlog_artifact_id=None,
            artifact_id=101,
            actor="operator@example.com",
            recorded_at=EVALUATED_AT,
        )
        session.commit()

        stored = session.get(BacklogArtifact, row.backlog_artifact_id)

    assert stored is not None
    assert stored.authority_id == authority_id
    assert stored.authority_fingerprint == AUTHORITY_FINGERPRINT
    assert stored.product_goal_artifact_id == GOAL_ID
    assert stored.product_goal_fingerprint == GOAL_FINGERPRINT


def test_backlog_replacement_rejects_cross_goal_supersession(engine: Engine) -> None:
    """A later Goal must create a replacement chain instead of mutating old work."""
    content = _backlog_content()
    with Session(engine) as session:
        project_id, authority_id = _seed_project_authority(session)
        parent = record_backlog_draft_in_session(
            session,
            project_id=project_id,
            authority_id=authority_id,
            authority_fingerprint=AUTHORITY_FINGERPRINT,
            product_goal_artifact_id=GOAL_ID,
            product_goal_fingerprint=GOAL_FINGERPRINT,
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
            supersedes_backlog_artifact_id=None,
            artifact_id=101,
            actor="operator@example.com",
            recorded_at=EVALUATED_AT,
        )
        assert parent.backlog_artifact_id is not None

        with pytest.raises(ValueError, match="different delivery lineage"):
            record_backlog_draft_in_session(
                session,
                project_id=project_id,
                authority_id=authority_id,
                authority_fingerprint=AUTHORITY_FINGERPRINT,
                product_goal_artifact_id=GOAL_ID + 1,
                product_goal_fingerprint="sha256:goal-replacement",
                canonical_content=content,
                content_fingerprint=canonical_hash(content),
                supersedes_backlog_artifact_id=parent.backlog_artifact_id,
                artifact_id=102,
                actor="operator@example.com",
                recorded_at=EVALUATED_AT,
            )


def test_record_request_requires_exact_goal_identity_and_fingerprint() -> None:
    """Callers cannot create a Goal-less Backlog transition request."""
    with pytest.raises(ValidationError):
        RecordBacklogDraft.model_validate(
            {
                "project_id": 1,
                "graph_version": "agileforge.workflow.v1",
                "fact_fingerprint": "sha256:facts",
                "decision_fingerprint": "sha256:decision",
                "idempotency_key": "request",
                "actor": "operator@example.com",
                "authority_id": 2,
                "authority_fingerprint": AUTHORITY_FINGERPRINT,
                "canonical_content": _backlog_content(),
                "content_fingerprint": canonical_hash(_backlog_content()),
            }
        )
