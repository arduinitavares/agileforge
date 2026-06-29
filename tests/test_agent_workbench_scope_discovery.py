"""Tests for Scope Discovery artifact persistence."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from models.agent_workbench import DiscoveryChallengeArtifact, DiscoveryPrd
from models.core import Product
from services.agent_workbench.error_codes import ErrorCode
from services.agent_workbench.scope_discovery import (
    ChallengeArtifactRecordRequest,
    PrdDraftRecordRequest,
    PrdReviewRequest,
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


def _prd_request(
    prd_file: str,
    *,
    challenge_artifact_id: int,
    idempotency_key: str = "prd-draft-record-001",
    supersedes_prd_id: int | None = None,
) -> PrdDraftRecordRequest:
    """Build a PRD draft record request."""
    return PrdDraftRecordRequest(
        project_id=PROJECT_ID,
        challenge_artifact_id=challenge_artifact_id,
        prd_file=prd_file,
        idempotency_key=idempotency_key,
        supersedes_prd_id=supersedes_prd_id,
        changed_by="test-agent",
    )


def _prd_review_request(
    *,
    prd_id: int,
    idempotency_key: str = "prd-review-001",
    reviewer: str = "product-owner",
    notes: str = "Approved for spec amendment drafting.",
) -> PrdReviewRequest:
    """Build a PRD review request."""
    return PrdReviewRequest(
        project_id=PROJECT_ID,
        prd_id=prd_id,
        reviewer=reviewer,
        notes=notes,
        idempotency_key=idempotency_key,
        changed_by="test-agent",
    )


def _write_prd_draft(
    tmp_path: Path,
    *,
    challenge_artifact_id: int,
    producer: str = "to-prd",
    title: str = "Product Reporting PRD",
    prd_id: int | None = None,
) -> str:
    """Write a PRD draft payload and return its path."""
    path = tmp_path / f"prd-{challenge_artifact_id}-{abs(hash(title))}.json"
    payload: dict[str, object] = {
        "producer": producer,
        "source_challenge_artifact_id": challenge_artifact_id,
        "title": title,
        "content": {
            "problem_statement": "Users need product reporting.",
            "solution": "Add reporting generated from accepted scope.",
            "user_stories": ["As a user, I can view product reporting."],
        },
        "markdown_export": {
            "path": "docs/prds/product-reporting.md",
            "authoritative": False,
        },
    }
    if prd_id is not None:
        payload["prd_id"] = prd_id
    path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return str(path)


def _record_challenge_artifact(
    session: Session,
    tmp_path: Path,
    *,
    readiness: str = "ready_for_prd",
) -> int:
    """Record a challenge artifact and return its ID."""
    session.add(Product(product_id=PROJECT_ID, name="Scope Discovery"))
    session.commit()
    runner = ScopeDiscoveryRunner(session=session)
    artifact_file = _write_challenge_artifact(
        tmp_path,
        readiness=readiness,
        content=(
            _rich_content()
            if readiness == "ready_for_prd"
            else {
                "blocking_reasons": ["More answers needed."],
                "remediation": ["Continue grilling."],
            }
        ),
    )
    result = runner.record_challenge_artifact(_request(artifact_file))
    assert result["ok"] is True
    return int(result["data"]["challenge_artifact_id"])


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


def test_record_prd_draft_persists_to_prd_output_from_ready_challenge(
    session: Session,
    tmp_path: Path,
) -> None:
    """Persist a draft PRD linked to a ready Challenge Artifact."""
    challenge_artifact_id = _record_challenge_artifact(session, tmp_path)
    prd_file = _write_prd_draft(
        tmp_path,
        challenge_artifact_id=challenge_artifact_id,
    )
    runner = ScopeDiscoveryRunner(session=session)

    result = runner.record_prd_draft(
        _prd_request(prd_file, challenge_artifact_id=challenge_artifact_id)
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["project_id"] == PROJECT_ID
    assert data["challenge_artifact_id"] == challenge_artifact_id
    assert data["producer"] == "to-prd"
    assert data["status"] == "draft"
    assert data["version"] == "1"
    assert data["next_action"] == "accept_prd"
    prd = session.get(DiscoveryPrd, data["prd_id"])
    assert prd is not None
    assert prd.project_id == PROJECT_ID
    assert prd.challenge_artifact_id == challenge_artifact_id
    assert prd.producer == "to-prd"
    assert prd.status == "draft"
    assert prd.version == "1"
    assert prd.changed_by == "test-agent"
    saved = json.loads(prd.content_json)
    assert saved["source_challenge_artifact_id"] == challenge_artifact_id
    assert saved["markdown_export"]["authoritative"] is False


def test_record_prd_draft_rejects_missing_challenge_artifact(
    session: Session,
    tmp_path: Path,
) -> None:
    """PRD drafts require an existing source Challenge Artifact."""
    session.add(Product(product_id=PROJECT_ID, name="Scope Discovery"))
    session.commit()
    missing_challenge_id = 404
    prd_file = _write_prd_draft(
        tmp_path,
        challenge_artifact_id=missing_challenge_id,
    )
    runner = ScopeDiscoveryRunner(session=session)

    result = runner.record_prd_draft(
        _prd_request(prd_file, challenge_artifact_id=missing_challenge_id)
    )

    assert result["ok"] is False
    assert ErrorCode.PRD_SOURCE_CHALLENGE_NOT_FOUND.value in _error_codes(result)


def test_record_prd_draft_rejects_non_ready_challenge_artifact(
    session: Session,
    tmp_path: Path,
) -> None:
    """PRD drafts require a ready_for_prd source Challenge Artifact."""
    challenge_artifact_id = _record_challenge_artifact(
        session,
        tmp_path,
        readiness="needs_answers",
    )
    prd_file = _write_prd_draft(
        tmp_path,
        challenge_artifact_id=challenge_artifact_id,
    )
    runner = ScopeDiscoveryRunner(session=session)

    result = runner.record_prd_draft(
        _prd_request(prd_file, challenge_artifact_id=challenge_artifact_id)
    )

    assert result["ok"] is False
    assert ErrorCode.PRD_SOURCE_CHALLENGE_NOT_READY.value in _error_codes(result)


def test_record_prd_draft_requires_to_prd_producer(
    session: Session,
    tmp_path: Path,
) -> None:
    """PRD drafts must declare to-prd producer provenance."""
    challenge_artifact_id = _record_challenge_artifact(session, tmp_path)
    prd_file = _write_prd_draft(
        tmp_path,
        challenge_artifact_id=challenge_artifact_id,
        producer="manual",
    )
    runner = ScopeDiscoveryRunner(session=session)

    result = runner.record_prd_draft(
        _prd_request(prd_file, challenge_artifact_id=challenge_artifact_id)
    )

    assert result["ok"] is False
    assert ErrorCode.PRD_PRODUCER_INVALID.value in _error_codes(result)


def test_record_prd_draft_replays_same_idempotency_request(
    session: Session,
    tmp_path: Path,
) -> None:
    """Retrying the same PRD draft request returns the first PRD."""
    challenge_artifact_id = _record_challenge_artifact(session, tmp_path)
    prd_file = _write_prd_draft(
        tmp_path,
        challenge_artifact_id=challenge_artifact_id,
    )
    runner = ScopeDiscoveryRunner(session=session)
    request = _prd_request(prd_file, challenge_artifact_id=challenge_artifact_id)

    first = runner.record_prd_draft(request)
    second = runner.record_prd_draft(request)

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["data"]["prd_id"] == first["data"]["prd_id"]


def test_record_prd_draft_rejects_idempotency_key_reuse(
    session: Session,
    tmp_path: Path,
) -> None:
    """The same idempotency key cannot record different PRD drafts."""
    challenge_artifact_id = _record_challenge_artifact(session, tmp_path)
    first_file = _write_prd_draft(
        tmp_path,
        challenge_artifact_id=challenge_artifact_id,
    )
    second_file = _write_prd_draft(
        tmp_path,
        challenge_artifact_id=challenge_artifact_id,
        title="Different PRD",
    )
    runner = ScopeDiscoveryRunner(session=session)

    assert runner.record_prd_draft(
        _prd_request(first_file, challenge_artifact_id=challenge_artifact_id)
    )["ok"] is True
    result = runner.record_prd_draft(
        _prd_request(second_file, challenge_artifact_id=challenge_artifact_id)
    )

    assert result["ok"] is False
    assert ErrorCode.IDEMPOTENCY_KEY_REUSED.value in _error_codes(result)


def test_accept_prd_draft_records_reviewer_notes(
    session: Session,
    tmp_path: Path,
) -> None:
    """A draft PRD can be accepted with reviewer identity and notes."""
    challenge_artifact_id = _record_challenge_artifact(session, tmp_path)
    prd_file = _write_prd_draft(
        tmp_path,
        challenge_artifact_id=challenge_artifact_id,
    )
    runner = ScopeDiscoveryRunner(session=session)
    draft = runner.record_prd_draft(
        _prd_request(prd_file, challenge_artifact_id=challenge_artifact_id)
    )
    prd_id = int(draft["data"]["prd_id"])

    result = runner.accept_prd(
        _prd_review_request(
            prd_id=prd_id,
            reviewer="Ada",
            notes="Ready to become a Spec Amendment Draft.",
        )
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["prd_id"] == prd_id
    assert data["status"] == "accepted"
    assert data["version"] == "1"
    assert data["supersedes_prd_id"] is None
    assert data["superseded_by_prd_id"] is None
    assert data["next_action"] == "record_spec_amendment_draft"
    assert "spec_amendment_draft_id" not in data
    prd = session.get(DiscoveryPrd, prd_id)
    assert prd is not None
    assert prd.status == "accepted"
    assert prd.reviewed_by == "Ada"
    assert prd.review_notes == "Ready to become a Spec Amendment Draft."
    assert prd.reviewed_at is not None


def test_reject_prd_draft_records_reviewer_notes(
    session: Session,
    tmp_path: Path,
) -> None:
    """A draft PRD can be rejected with reviewer identity and notes."""
    challenge_artifact_id = _record_challenge_artifact(session, tmp_path)
    prd_file = _write_prd_draft(
        tmp_path,
        challenge_artifact_id=challenge_artifact_id,
    )
    runner = ScopeDiscoveryRunner(session=session)
    draft = runner.record_prd_draft(
        _prd_request(prd_file, challenge_artifact_id=challenge_artifact_id)
    )
    prd_id = int(draft["data"]["prd_id"])

    result = runner.reject_prd(
        _prd_review_request(
            prd_id=prd_id,
            reviewer="Ada",
            notes="Needs clearer out-of-scope decisions.",
        )
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["prd_id"] == prd_id
    assert data["status"] == "rejected"
    assert data["version"] == "1"
    assert data["next_action"] == "revise_prd"
    prd = session.get(DiscoveryPrd, prd_id)
    assert prd is not None
    assert prd.status == "rejected"
    assert prd.reviewed_by == "Ada"
    assert prd.review_notes == "Needs clearer out-of-scope decisions."


def test_accept_prd_replays_same_idempotency_request(
    session: Session,
    tmp_path: Path,
) -> None:
    """Retrying the same PRD accept request returns the accepted PRD."""
    challenge_artifact_id = _record_challenge_artifact(session, tmp_path)
    prd_file = _write_prd_draft(
        tmp_path,
        challenge_artifact_id=challenge_artifact_id,
    )
    runner = ScopeDiscoveryRunner(session=session)
    draft = runner.record_prd_draft(
        _prd_request(prd_file, challenge_artifact_id=challenge_artifact_id)
    )
    request = _prd_review_request(prd_id=int(draft["data"]["prd_id"]))

    first = runner.accept_prd(request)
    second = runner.accept_prd(request)

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["data"]["prd_id"] == first["data"]["prd_id"]
    assert second["data"]["status"] == "accepted"


def test_prd_review_rejects_conflicting_repeated_decision(
    session: Session,
    tmp_path: Path,
) -> None:
    """Review idempotency keys cannot be reused for a different decision."""
    challenge_artifact_id = _record_challenge_artifact(session, tmp_path)
    prd_file = _write_prd_draft(
        tmp_path,
        challenge_artifact_id=challenge_artifact_id,
    )
    runner = ScopeDiscoveryRunner(session=session)
    draft = runner.record_prd_draft(
        _prd_request(prd_file, challenge_artifact_id=challenge_artifact_id)
    )
    prd_id = int(draft["data"]["prd_id"])

    assert runner.accept_prd(_prd_review_request(prd_id=prd_id))["ok"] is True
    result = runner.reject_prd(
        _prd_review_request(
            prd_id=prd_id,
            idempotency_key="prd-review-001",
            notes="Reject with reused key.",
        )
    )

    assert result["ok"] is False
    assert ErrorCode.IDEMPOTENCY_KEY_REUSED.value in _error_codes(result)


def test_record_prd_draft_rejects_in_place_edit_of_accepted_prd(
    session: Session,
    tmp_path: Path,
) -> None:
    """Accepted PRDs cannot be edited in place by recording over their PRD ID."""
    challenge_artifact_id = _record_challenge_artifact(session, tmp_path)
    first_file = _write_prd_draft(
        tmp_path,
        challenge_artifact_id=challenge_artifact_id,
    )
    runner = ScopeDiscoveryRunner(session=session)
    draft = runner.record_prd_draft(
        _prd_request(first_file, challenge_artifact_id=challenge_artifact_id)
    )
    prd_id = int(draft["data"]["prd_id"])
    assert runner.accept_prd(_prd_review_request(prd_id=prd_id))["ok"] is True
    attempted_edit_file = _write_prd_draft(
        tmp_path,
        challenge_artifact_id=challenge_artifact_id,
        title="Edited Accepted PRD",
        prd_id=prd_id,
    )
    before = session.get(DiscoveryPrd, prd_id)
    assert before is not None
    before_content_json = before.content_json

    result = runner.record_prd_draft(
        _prd_request(
            attempted_edit_file,
            challenge_artifact_id=challenge_artifact_id,
            idempotency_key="prd-edit-accepted-001",
        )
    )

    after = session.get(DiscoveryPrd, prd_id)
    assert result["ok"] is False
    assert ErrorCode.PRD_ACCEPTED_IMMUTABLE.value in _error_codes(result)
    assert after is not None
    assert after.status == "accepted"
    assert after.content_json == before_content_json


def test_record_prd_draft_creates_superseding_version_without_mutating_accepted_prd(
    session: Session,
    tmp_path: Path,
) -> None:
    """Changes after acceptance create a linked draft version."""
    challenge_artifact_id = _record_challenge_artifact(session, tmp_path)
    first_file = _write_prd_draft(
        tmp_path,
        challenge_artifact_id=challenge_artifact_id,
    )
    runner = ScopeDiscoveryRunner(session=session)
    draft = runner.record_prd_draft(
        _prd_request(first_file, challenge_artifact_id=challenge_artifact_id)
    )
    accepted_prd_id = int(draft["data"]["prd_id"])
    assert runner.accept_prd(
        _prd_review_request(prd_id=accepted_prd_id)
    )["ok"] is True
    accepted_before = session.get(DiscoveryPrd, accepted_prd_id)
    assert accepted_before is not None
    accepted_content_json = accepted_before.content_json
    second_file = _write_prd_draft(
        tmp_path,
        challenge_artifact_id=challenge_artifact_id,
        title="Product Reporting PRD v2",
    )

    result = runner.record_prd_draft(
        _prd_request(
            second_file,
            challenge_artifact_id=challenge_artifact_id,
            idempotency_key="prd-draft-record-v2-001",
            supersedes_prd_id=accepted_prd_id,
        )
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "draft"
    assert data["version"] == "2"
    assert data["supersedes_prd_id"] == accepted_prd_id
    assert data["superseded_by_prd_id"] is None
    assert data["next_action"] == "accept_prd"
    assert data["prd_id"] != accepted_prd_id
    accepted_after = session.get(DiscoveryPrd, accepted_prd_id)
    assert accepted_after is not None
    assert accepted_after.status == "accepted"
    assert accepted_after.content_json == accepted_content_json
