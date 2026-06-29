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
    content: dict[str, object] | None = None,
) -> str:
    """Write a challenge artifact and return its path."""
    path = tmp_path / f"challenge-{abs(hash(original_idea))}.json"
    path.write_text(
        json.dumps(
            {
                "producer": producer,
                "readiness": readiness,
                "original_idea": original_idea,
                "content": content if content is not None else _rich_content(),
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def _rich_content(**overrides: object) -> dict[str, object]:
    """Return a rich challenge artifact content fixture."""
    content: dict[str, object] = {
        "questions": [
            {
                "question": "What new scope is being introduced?",
                "answer": (
                    "Add product reporting after the accepted scope is exhausted."
                ),
            }
        ],
        "reviewed_evidence": [
            {
                "source": "CONTEXT.md",
                "summary": "Project language defines Challenge Artifact and PRD.",
            }
        ],
        "evidence_conflicts": [],
        "assumptions": ["Existing authority remains the execution source."],
        "non_goals": ["Do not bypass PRD acceptance."],
        "risks": [
            {
                "risk": "Agents could treat chat as workflow state.",
                "mitigation": "Persist the Challenge Artifact in AgileForge state.",
            }
        ],
        "open_questions": [],
        "glossary_changes": [
            {
                "term": "Challenge Artifact",
                "change": "Settled as a first-class scope discovery artifact.",
                "committed_to_project_glossary": True,
                "evidence": "CONTEXT.md",
            }
        ],
    }
    content.update(overrides)
    return content


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


def _first_error(result: dict[str, Any]) -> dict[str, Any]:
    return dict(result["errors"][0])


def _blocker_fields(result: dict[str, Any]) -> set[str]:
    details = _first_error(result)["details"]
    assert isinstance(details, dict)
    blockers = details["blockers"]
    assert isinstance(blockers, list)
    return {str(blocker["field"]) for blocker in blockers}


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


def test_record_ready_challenge_artifact_persists_rich_evidence(
    session: Session,
    tmp_path: Path,
) -> None:
    """A ready Challenge Artifact records rich challenge evidence."""
    session.add(Product(product_id=PROJECT_ID, name="Scope Discovery"))
    session.commit()
    artifact_file = _write_challenge_artifact(tmp_path)
    runner = ScopeDiscoveryRunner(session=session)

    result = runner.record_challenge_artifact(_request(artifact_file))

    assert result["ok"] is True
    artifact = session.get(
        DiscoveryChallengeArtifact,
        result["data"]["challenge_artifact_id"],
    )
    assert artifact is not None
    saved = json.loads(artifact.content_json)
    content = saved["content"]
    assert content["questions"][0]["answer"].startswith("Add product reporting")
    assert content["reviewed_evidence"][0]["source"] == "CONTEXT.md"
    assert content["assumptions"] == [
        "Existing authority remains the execution source."
    ]
    assert content["non_goals"] == ["Do not bypass PRD acceptance."]
    assert content["risks"][0]["mitigation"].startswith("Persist")
    assert content["glossary_changes"][0]["committed_to_project_glossary"] is True


def test_record_ready_challenge_artifact_rejects_missing_rich_evidence(
    session: Session,
    tmp_path: Path,
) -> None:
    """Ready artifacts require the full rich evidence field set."""
    session.add(Product(product_id=PROJECT_ID, name="Scope Discovery"))
    session.commit()
    artifact_file = _write_challenge_artifact(
        tmp_path,
        content={"questions": []},
    )
    runner = ScopeDiscoveryRunner(session=session)

    result = runner.record_challenge_artifact(_request(artifact_file))

    assert result["ok"] is False
    assert ErrorCode.CHALLENGE_ARTIFACT_INVALID.value in _error_codes(result)
    assert {
        "content.reviewed_evidence",
        "content.evidence_conflicts",
        "content.assumptions",
        "content.non_goals",
        "content.risks",
        "content.open_questions",
        "content.glossary_changes",
    }.issubset(_blocker_fields(result))


def test_record_ready_challenge_artifact_rejects_open_questions(
    session: Session,
    tmp_path: Path,
) -> None:
    """Ready artifacts cannot carry unresolved open questions."""
    session.add(Product(product_id=PROJECT_ID, name="Scope Discovery"))
    session.commit()
    artifact_file = _write_challenge_artifact(
        tmp_path,
        content=_rich_content(
            open_questions=[
                {
                    "question": "Who accepts the PRD?",
                    "blocking": True,
                }
            ]
        ),
    )
    runner = ScopeDiscoveryRunner(session=session)

    result = runner.record_challenge_artifact(_request(artifact_file))

    assert result["ok"] is False
    assert "content.open_questions" in _blocker_fields(result)


def test_record_ready_challenge_artifact_rejects_unresolved_evidence_conflicts(
    session: Session,
    tmp_path: Path,
) -> None:
    """Ready artifacts cannot carry unresolved evidence conflicts."""
    session.add(Product(product_id=PROJECT_ID, name="Scope Discovery"))
    session.commit()
    artifact_file = _write_challenge_artifact(
        tmp_path,
        content=_rich_content(
            evidence_conflicts=[
                {
                    "description": "CONTEXT.md and PRD disagree on producer.",
                    "resolved": False,
                }
            ]
        ),
    )
    runner = ScopeDiscoveryRunner(session=session)

    result = runner.record_challenge_artifact(_request(artifact_file))

    assert result["ok"] is False
    assert "content.evidence_conflicts" in _blocker_fields(result)


def test_record_ready_challenge_artifact_rejects_uncommitted_glossary_changes(
    session: Session,
    tmp_path: Path,
) -> None:
    """Ready artifacts require glossary change evidence in the Project Glossary."""
    session.add(Product(product_id=PROJECT_ID, name="Scope Discovery"))
    session.commit()
    artifact_file = _write_challenge_artifact(
        tmp_path,
        content=_rich_content(
            glossary_changes=[
                {
                    "term": "Scope Discovery Gate",
                    "change": "New settled term.",
                    "committed_to_project_glossary": False,
                }
            ]
        ),
    )
    runner = ScopeDiscoveryRunner(session=session)

    result = runner.record_challenge_artifact(_request(artifact_file))

    assert result["ok"] is False
    assert "content.glossary_changes" in _blocker_fields(result)


def test_record_non_ready_challenge_artifact_saves_blocking_reasons(
    session: Session,
    tmp_path: Path,
) -> None:
    """Non-ready artifacts can record blockers and remediation for continuation."""
    session.add(Product(product_id=PROJECT_ID, name="Scope Discovery"))
    session.commit()
    artifact_file = _write_challenge_artifact(
        tmp_path,
        readiness="needs_answers",
        content={
            "blocking_reasons": ["Open question about PRD acceptance remains."],
            "remediation": ["Answer the PRD acceptance question."],
        },
    )
    runner = ScopeDiscoveryRunner(session=session)

    result = runner.record_challenge_artifact(_request(artifact_file))

    assert result["ok"] is True
    assert result["data"]["next_action"] == "continue_challenge"
    artifact = session.get(
        DiscoveryChallengeArtifact,
        result["data"]["challenge_artifact_id"],
    )
    assert artifact is not None
    saved = json.loads(artifact.content_json)
    assert saved["content"]["blocking_reasons"] == [
        "Open question about PRD acceptance remains."
    ]


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
