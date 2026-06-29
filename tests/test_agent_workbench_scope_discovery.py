"""Tests for Scope Discovery artifact persistence."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from models.agent_workbench import DiscoveryChallengeArtifact
from models.core import Product
from services.agent_workbench.error_codes import ErrorCode
from services.agent_workbench.scope_discovery import (
    ChallengeArtifactRecordRequest,
    ScopeDiscoveryRunner,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlmodel import Session

PROJECT_ID = 7


def _write_challenge_artifact(
    tmp_path: Path,
    *,
    producer: str = "grill-with-docs",
    readiness: str = "ready_for_prd",
    original_idea: str = "Add product reporting.",
) -> str:
    """Write a minimal challenge artifact and return its path."""
    path = tmp_path / f"challenge-{abs(hash(original_idea))}.json"
    path.write_text(
        json.dumps(
            {
                "producer": producer,
                "readiness": readiness,
                "original_idea": original_idea,
                "content": {
                    "questions_answered": [],
                    "open_questions": [],
                },
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def _request(
    artifact_file: str,
    *,
    idempotency_key: str = "challenge-record-001",
) -> ChallengeArtifactRecordRequest:
    """Build a minimal record request."""
    return ChallengeArtifactRecordRequest(
        project_id=PROJECT_ID,
        artifact_file=artifact_file,
        idempotency_key=idempotency_key,
        changed_by="test-agent",
    )


def _error_codes(result: dict[str, Any]) -> list[str]:
    return [str(error["code"]) for error in result["errors"]]


def test_record_challenge_artifact_persists_minimal_provenance(
    session: Session,
    tmp_path: Path,
) -> None:
    """Persist a minimal grill-with-docs Challenge Artifact."""
    session.add(Product(product_id=PROJECT_ID, name="Scope Discovery"))
    session.commit()
    artifact_file = _write_challenge_artifact(tmp_path)
    runner = ScopeDiscoveryRunner(session=session)

    result = runner.record_challenge_artifact(_request(artifact_file))

    assert result["ok"] is True
    data = result["data"]
    assert data["project_id"] == PROJECT_ID
    assert data["producer"] == "grill-with-docs"
    assert data["readiness"] == "ready_for_prd"
    assert data["next_action"] == "record_prd"
    artifact = session.get(
        DiscoveryChallengeArtifact,
        data["challenge_artifact_id"],
    )
    assert artifact is not None
    assert artifact.project_id == PROJECT_ID
    assert artifact.original_idea == "Add product reporting."
    assert artifact.producer == "grill-with-docs"
    assert artifact.readiness == "ready_for_prd"
    assert artifact.idempotency_key == "challenge-record-001"
    assert artifact.changed_by == "test-agent"


def test_record_challenge_artifact_replays_same_idempotency_request(
    session: Session,
    tmp_path: Path,
) -> None:
    """Retrying the same record request returns the first artifact."""
    session.add(Product(product_id=PROJECT_ID, name="Scope Discovery"))
    session.commit()
    artifact_file = _write_challenge_artifact(tmp_path)
    runner = ScopeDiscoveryRunner(session=session)
    request = _request(artifact_file)

    first = runner.record_challenge_artifact(request)
    second = runner.record_challenge_artifact(request)

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["data"]["challenge_artifact_id"] == (
        first["data"]["challenge_artifact_id"]
    )


def test_record_challenge_artifact_rejects_idempotency_key_reuse(
    session: Session,
    tmp_path: Path,
) -> None:
    """The same idempotency key cannot record different artifacts."""
    session.add(Product(product_id=PROJECT_ID, name="Scope Discovery"))
    session.commit()
    first_file = _write_challenge_artifact(tmp_path)
    second_file = _write_challenge_artifact(
        tmp_path,
        original_idea="Add account exports.",
    )
    runner = ScopeDiscoveryRunner(session=session)

    assert runner.record_challenge_artifact(_request(first_file))["ok"] is True
    result = runner.record_challenge_artifact(_request(second_file))

    assert result["ok"] is False
    assert ErrorCode.IDEMPOTENCY_KEY_REUSED.value in _error_codes(result)


def test_record_challenge_artifact_requires_grill_with_docs_producer(
    session: Session,
    tmp_path: Path,
) -> None:
    """Challenge Artifacts must come from grill-with-docs."""
    session.add(Product(product_id=PROJECT_ID, name="Scope Discovery"))
    session.commit()
    artifact_file = _write_challenge_artifact(tmp_path, producer="manual")
    runner = ScopeDiscoveryRunner(session=session)

    result = runner.record_challenge_artifact(_request(artifact_file))

    assert result["ok"] is False
    assert ErrorCode.CHALLENGE_PRODUCER_INVALID.value in _error_codes(result)


def test_record_challenge_artifact_rejects_unknown_project(
    session: Session,
    tmp_path: Path,
) -> None:
    """Challenge Artifacts must reference an existing project."""
    artifact_file = _write_challenge_artifact(tmp_path)
    runner = ScopeDiscoveryRunner(session=session)

    result = runner.record_challenge_artifact(_request(artifact_file))

    assert result["ok"] is False
    assert ErrorCode.PROJECT_NOT_FOUND.value in _error_codes(result)
