"""Durable Backlog Goal/Authority lineage tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from sqlmodel import Session

from models.core import Project
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance
from models.workflow import BacklogArtifact
from services.agent_workbench.backlog_phase import record_backlog_draft_in_session
from services.specs.authority_selection import pending_authority_fingerprint
from tests.workflow.lifecycle_fixtures import (
    PersistedSpecificationLineage,
    seed_accepted_specification,
)
from utils.spec_schemas import SpecAuthorityCompilationSuccess
from workflow.fingerprints import canonical_hash
from workflow.requests.product_definition import RecordBacklogDraft

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from workflow.contracts import JsonObject

EVALUATED_AT = datetime(2026, 8, 5, 12, tzinfo=UTC)
AUTHORITY_FINGERPRINT = "sha256:authority-current"


@dataclass(frozen=True)
class _DeliveryLineage:
    project_id: int
    product_goal_artifact_id: int
    product_goal_fingerprint: str
    authority_id: int
    authority_fingerprint: str


def _backlog_content(
    requirement: str = "Persist exact delivery lineage",
) -> JsonObject:
    return {
        "backlog_items": [
            {
                "priority": 1,
                "requirement": requirement,
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


def _accept_authority(
    session: Session,
    *,
    project_id: int,
    specification: PersistedSpecificationLineage,
    ordinal: int,
) -> _DeliveryLineage:
    artifact = SpecAuthorityCompilationSuccess(
        scope_themes=[f"Backlog lineage {ordinal}"],
        invariants=[],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[],
        compiler_version="3.0.0",
        prompt_hash=f"{ordinal}" * 64,
    )
    spec_version_id = specification.spec.spec_version_id
    assert spec_version_id is not None
    approved_at = specification.spec.approved_at
    assert approved_at is not None
    authority_at = approved_at + timedelta(seconds=1)
    authority = CompiledSpecAuthority(
        spec_version_id=spec_version_id,
        compiler_version=artifact.compiler_version,
        prompt_hash=artifact.prompt_hash,
        compiled_at=authority_at,
        compiled_artifact_json=artifact.model_dump_json(),
        scope_themes="[]",
        invariants="[]",
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps="[]",
    )
    session.add(authority)
    session.flush()
    assert authority.authority_id is not None
    authority_fingerprint = pending_authority_fingerprint(authority)
    assert authority_fingerprint is not None
    session.add(
        SpecAuthorityAcceptance(
            project_id=project_id,
            spec_version_id=spec_version_id,
            status="accepted",
            policy="manual",
            decided_by="operator@example.com",
            decided_at=authority_at + timedelta(seconds=1),
            rationale="Accepted for Backlog lineage tests.",
            compiler_version=authority.compiler_version,
            prompt_hash=authority.prompt_hash,
            spec_hash=specification.spec.spec_hash,
            pending_authority_id=authority.authority_id,
            authority_fingerprint=authority_fingerprint,
            review_fingerprint=f"sha256:review-{ordinal}",
            terminal_decision_key=f"backlog-authority-{ordinal}",
        )
    )
    session.commit()
    return _DeliveryLineage(
        project_id=project_id,
        product_goal_artifact_id=specification.product_goal_artifact_id,
        product_goal_fingerprint=specification.product_goal_fingerprint,
        authority_id=authority.authority_id,
        authority_fingerprint=authority_fingerprint,
    )


def _seed_project_authority(session: Session) -> _DeliveryLineage:
    project = Project(name="Backlog lineage")
    session.add(project)
    session.commit()
    assert project.project_id is not None
    specification = seed_accepted_specification(
        session,
        project_id=project.project_id,
        content='{"increment":1}',
        recorded_at=EVALUATED_AT - timedelta(minutes=1),
    )
    return _accept_authority(
        session,
        project_id=project.project_id,
        specification=specification,
        ordinal=1,
    )


def test_backlog_row_persists_exact_goal_and_authority_lineage(engine: Engine) -> None:
    """A stored Backlog carries both durable upstream identities."""
    content = _backlog_content()
    with Session(engine) as session:
        lineage = _seed_project_authority(session)
        row = record_backlog_draft_in_session(
            session,
            project_id=lineage.project_id,
            authority_id=lineage.authority_id,
            authority_fingerprint=lineage.authority_fingerprint,
            product_goal_artifact_id=lineage.product_goal_artifact_id,
            product_goal_fingerprint=lineage.product_goal_fingerprint,
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
    assert stored.authority_id == lineage.authority_id
    assert stored.authority_fingerprint == lineage.authority_fingerprint
    assert stored.product_goal_artifact_id == lineage.product_goal_artifact_id
    assert stored.product_goal_fingerprint == lineage.product_goal_fingerprint


def test_backlog_replacement_rejects_cross_goal_supersession(engine: Engine) -> None:
    """A later Goal must create a replacement chain instead of mutating old work."""
    content = _backlog_content()
    with Session(engine) as session:
        lineage = _seed_project_authority(session)
        parent = record_backlog_draft_in_session(
            session,
            project_id=lineage.project_id,
            authority_id=lineage.authority_id,
            authority_fingerprint=lineage.authority_fingerprint,
            product_goal_artifact_id=lineage.product_goal_artifact_id,
            product_goal_fingerprint=lineage.product_goal_fingerprint,
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
            supersedes_backlog_artifact_id=None,
            artifact_id=101,
            actor="operator@example.com",
            recorded_at=EVALUATED_AT,
        )
        assert parent.backlog_artifact_id is not None

        with pytest.raises(ValueError, match="different Product Goal lineage"):
            record_backlog_draft_in_session(
                session,
                project_id=lineage.project_id,
                authority_id=lineage.authority_id,
                authority_fingerprint=lineage.authority_fingerprint,
                product_goal_artifact_id=lineage.product_goal_artifact_id + 1,
                product_goal_fingerprint="sha256:goal-replacement",
                canonical_content=content,
                content_fingerprint=canonical_hash(content),
                supersedes_backlog_artifact_id=parent.backlog_artifact_id,
                artifact_id=102,
                actor="operator@example.com",
                recorded_at=EVALUATED_AT,
            )


def test_backlog_replacement_carries_same_goal_across_authority_versions(
    engine: Engine,
) -> None:
    """A later Authority preserves the immutable same-Goal Backlog chain."""
    first_content = _backlog_content()
    replacement_content = _backlog_content("Deliver the next discovered increment")
    with Session(engine) as session:
        first = _seed_project_authority(session)
        parent = record_backlog_draft_in_session(
            session,
            project_id=first.project_id,
            authority_id=first.authority_id,
            authority_fingerprint=first.authority_fingerprint,
            product_goal_artifact_id=first.product_goal_artifact_id,
            product_goal_fingerprint=first.product_goal_fingerprint,
            canonical_content=first_content,
            content_fingerprint=canonical_hash(first_content),
            supersedes_backlog_artifact_id=None,
            artifact_id=101,
            actor="operator@example.com",
            recorded_at=EVALUATED_AT,
        )
        session.commit()
        assert parent.backlog_artifact_id is not None
        specification = seed_accepted_specification(
            session,
            project_id=first.project_id,
            content='{"increment":2}',
            recorded_at=EVALUATED_AT + timedelta(minutes=1),
        )
        replacement = _accept_authority(
            session,
            project_id=first.project_id,
            specification=specification,
            ordinal=2,
        )

        child = record_backlog_draft_in_session(
            session,
            project_id=replacement.project_id,
            authority_id=replacement.authority_id,
            authority_fingerprint=replacement.authority_fingerprint,
            product_goal_artifact_id=replacement.product_goal_artifact_id,
            product_goal_fingerprint=replacement.product_goal_fingerprint,
            canonical_content=replacement_content,
            content_fingerprint=canonical_hash(replacement_content),
            supersedes_backlog_artifact_id=parent.backlog_artifact_id,
            artifact_id=102,
            actor="operator@example.com",
            recorded_at=EVALUATED_AT + timedelta(minutes=2),
        )
        session.commit()
        assert child.supersedes_backlog_artifact_id == parent.backlog_artifact_id
        assert child.authority_id == replacement.authority_id
        assert child.authority_id != parent.authority_id
        assert child.product_goal_artifact_id == parent.product_goal_artifact_id
        assert child.product_goal_fingerprint == parent.product_goal_fingerprint


def test_record_request_requires_exact_goal_identity_and_fingerprint() -> None:
    """Callers cannot create a Goal-less Backlog transition request."""
    with pytest.raises(ValidationError):
        RecordBacklogDraft.model_validate(
            {
                "project_id": 1,
                "graph_version": "agileforge.workflow.v2",
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
