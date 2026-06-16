"""Tests for authority feedback curation mutations."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from sqlmodel import Session, select

import services.agent_workbench.authority_curation as curation_mod
from db.migrations import ensure_schema_current
from models.authority_curation import AuthorityFeedbackAttempt
from models.core import Product
from models.specs import CompiledSpecAuthority, SpecRegistry
from services.agent_workbench.authority_curation import (
    AuthorityCurationRunner,
    AuthorityFeedbackFile,
    AuthorityFeedbackItem,
    AuthorityFeedbackRecordRequest,
)
from services.agent_workbench.authority_projection import pending_authority_fingerprint
from tests.typing_helpers import require_id

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine


def _compiled_artifact_json() -> str:
    return json.dumps(
        {
            "schema_version": "agileforge.compiled_authority.v2",
            "scope_themes": [],
            "domain": "operations",
            "invariants": [
                {
                    "id": "INV-curation-1",
                    "source_item_id": "SRC-curation-1",
                    "text": "Review packets include guard evidence.",
                }
            ],
            "eligible_feature_rules": [],
            "rejected_features": [],
            "gaps": [{"gap_id": "GAP-curation-1"}],
            "assumptions": [{"assumption_id": "ASM-curation-1"}],
            "quality_groups": [{"group_id": "QG-curation-1"}],
            "source_map": [{"id": "SRC-curation-1"}],
            "compiler_version": "2.0.0",
            "prompt_hash": "a" * 64,
            "ir_schema_version": None,
            "ir_provenance": None,
        },
        sort_keys=True,
    )


def _seed_pending_authority(
    engine: Engine,
    *,
    product_name: str = "Authority Feedback Product",
) -> tuple[int, int, str]:
    ensure_schema_current(engine)
    with Session(engine) as session:
        product = Product(
            name=product_name,
            description="Seeded authority feedback project",
        )
        session.add(product)
        session.commit()
        session.refresh(product)
        project_id = require_id(product.product_id, "product_id")

        spec = SpecRegistry(
            product_id=project_id,
            spec_hash="sha256:" + ("b" * 64),
            content='{"schema_version":"agileforge.spec.v1","items":[]}',
            content_ref="specs/authority-feedback.json",
            status="approved",
            approved_at=datetime(2026, 6, 16, 12, tzinfo=UTC),
            approved_by="curation-test",
            approval_notes="Approved for curation test.",
        )
        session.add(spec)
        session.commit()
        session.refresh(spec)

        authority = CompiledSpecAuthority(
            spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
            compiler_version="2.0.0",
            prompt_hash="a" * 64,
            compiled_at=datetime(2026, 6, 16, 13, tzinfo=UTC),
            compiled_artifact_json=_compiled_artifact_json(),
            scope_themes=json.dumps(["Authority feedback"]),
            invariants=json.dumps(
                [
                    {
                        "id": "INV-curation-1",
                        "text": "Review packets include guard evidence.",
                    }
                ]
            ),
            eligible_feature_ids=json.dumps([]),
            rejected_features=json.dumps([]),
            spec_gaps=json.dumps([{"id": "GAP-curation-1"}]),
        )
        session.add(authority)
        session.commit()
        session.refresh(authority)
        authority_id = require_id(authority.authority_id, "authority_id")
        fingerprint = pending_authority_fingerprint(authority)
        assert fingerprint is not None
        return project_id, authority_id, fingerprint


def _write_feedback(
    tmp_path: Path,
    *,
    authority_id: int,
    item_overrides: dict[str, object] | None = None,
    filename: str = "feedback.json",
) -> Path:
    item = {
        "feedback_id": "AFB-curation-1",
        "target_kind": "invariant",
        "target_id": "INV-curation-1",
        "issue_type": "overstrong_invariant",
        "severity": "blocking",
        "instruction": "Make the invariant less absolute.",
    }
    for key, value in (item_overrides or {}).items():
        if value is None:
            item.pop(key, None)
        else:
            item[key] = value
    feedback_file = tmp_path / filename
    feedback_file.write_text(
        json.dumps(
            {
                "schema_version": "agileforge.authority_feedback.v1",
                "authority_id": authority_id,
                "feedback_items": [item],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return feedback_file


def test_feedback_models_reject_unknown_fields() -> None:
    """Feedback payloads are strict audit artifacts."""
    with pytest.raises(ValidationError):
        AuthorityFeedbackItem.model_validate(
            {
                "feedback_id": "AFB-1",
                "target_kind": "invariant",
                "target_id": "INV-0123456789abcdef",
                "issue_type": "overstrong_invariant",
                "severity": "blocking",
                "instruction": "Replace the overstrong invariant.",
                "extra": "rejected",
            }
        )

    with pytest.raises(ValidationError):
        AuthorityFeedbackFile.model_validate(
            {
                "schema_version": "agileforge.authority_feedback.v1",
                "authority_id": 1,
                "feedback_items": [
                    {
                        "feedback_id": "AFB-1",
                        "target_kind": "invariant",
                        "target_id": "INV-0123456789abcdef",
                        "issue_type": "overstrong_invariant",
                        "severity": "blocking",
                        "instruction": "Replace the overstrong invariant.",
                    }
                ],
                "extra": "rejected",
            }
        )


def test_feedback_record_requires_idempotency_key() -> None:
    """Feedback recording is a mutation and requires idempotency."""
    with pytest.raises(ValidationError):
        AuthorityFeedbackRecordRequest(
            project_id=1,
            pending_authority_id=6,
            expected_authority_fingerprint="sha256:abc",
            feedback_file="feedback.json",
        )


def test_feedback_file_authority_mismatch_returns_schema_invalid(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Feedback files must target the same pending authority as the request."""
    project_id, authority_id, fingerprint = _seed_pending_authority(engine)
    feedback_file = _write_feedback(
        tmp_path,
        authority_id=authority_id + 1,
        item_overrides={
            "target_kind": "authority_candidate",
            "target_id": f"authority:{authority_id + 1}",
        },
    )

    result = AuthorityCurationRunner(engine=engine).feedback_record(
        AuthorityFeedbackRecordRequest(
            project_id=project_id,
            pending_authority_id=authority_id,
            expected_authority_fingerprint=fingerprint,
            feedback_file=str(feedback_file),
            idempotency_key="feedback-record-001",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_FEEDBACK_SCHEMA_INVALID"


def test_feedback_file_invalid_utf8_returns_schema_invalid(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Feedback file decode failures are schema errors."""
    project_id, authority_id, fingerprint = _seed_pending_authority(engine)
    feedback_file = tmp_path / "feedback-invalid-utf8.json"
    feedback_file.write_bytes(b"\xff")

    result = AuthorityCurationRunner(engine=engine).feedback_record(
        AuthorityFeedbackRecordRequest(
            project_id=project_id,
            pending_authority_id=authority_id,
            expected_authority_fingerprint=fingerprint,
            feedback_file=str(feedback_file),
            idempotency_key="feedback-record-invalid-utf8",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_FEEDBACK_SCHEMA_INVALID"


def test_feedback_schema_invalid_sanitizes_validation_details(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Validation errors must not echo raw feedback payload values."""
    project_id, authority_id, fingerprint = _seed_pending_authority(engine)
    feedback_file = tmp_path / "feedback-secret.json"
    feedback_file.write_text(
        json.dumps(
            {
                "schema_version": "agileforge.authority_feedback.v1",
                "authority_id": authority_id,
                "feedback_items": [
                    {
                        "feedback_id": "AFB-secret",
                        "target_kind": "invariant",
                        "target_id": "INV-curation-1",
                        "issue_type": "overstrong_invariant",
                        "severity": "blocking",
                        "instruction": "Make the invariant less absolute.",
                        "extra": "s3cr3t-value",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = AuthorityCurationRunner(engine=engine).feedback_record(
        AuthorityFeedbackRecordRequest(
            project_id=project_id,
            pending_authority_id=authority_id,
            expected_authority_fingerprint=fingerprint,
            feedback_file=str(feedback_file),
            idempotency_key="feedback-record-secret-validation",
        )
    )

    serialized = json.dumps(result, sort_keys=True)
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_FEEDBACK_SCHEMA_INVALID"
    assert "s3cr3t-value" not in serialized
    assert "input_value" not in serialized


def test_feedback_record_rejects_stale_authority_fingerprint(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Feedback recording must guard against stale pending authority versions."""
    project_id, authority_id, _fingerprint = _seed_pending_authority(engine)
    feedback_file = _write_feedback(tmp_path, authority_id=authority_id)

    result = AuthorityCurationRunner(engine=engine).feedback_record(
        AuthorityFeedbackRecordRequest(
            project_id=project_id,
            pending_authority_id=authority_id,
            expected_authority_fingerprint="sha256:stale",
            feedback_file=str(feedback_file),
            idempotency_key="feedback-record-002",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "STALE_AUTHORITY_VERSION"


def test_feedback_record_rejects_missing_target(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Concrete target ids must exist in the authority candidate."""
    project_id, authority_id, fingerprint = _seed_pending_authority(engine)
    feedback_file = _write_feedback(
        tmp_path,
        authority_id=authority_id,
        item_overrides={"target_id": "INV-missing-target"},
    )

    result = AuthorityCurationRunner(engine=engine).feedback_record(
        AuthorityFeedbackRecordRequest(
            project_id=project_id,
            pending_authority_id=authority_id,
            expected_authority_fingerprint=fingerprint,
            feedback_file=str(feedback_file),
            idempotency_key="feedback-record-003",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_FEEDBACK_TARGET_NOT_FOUND"


def test_feedback_record_rejects_wrong_kind_target(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """A valid id in one bucket must not validate as another target kind."""
    project_id, authority_id, fingerprint = _seed_pending_authority(engine)
    feedback_file = _write_feedback(
        tmp_path,
        authority_id=authority_id,
        item_overrides={
            "target_kind": "gap",
            "target_id": "INV-curation-1",
        },
    )

    result = AuthorityCurationRunner(engine=engine).feedback_record(
        AuthorityFeedbackRecordRequest(
            project_id=project_id,
            pending_authority_id=authority_id,
            expected_authority_fingerprint=fingerprint,
            feedback_file=str(feedback_file),
            idempotency_key="feedback-record-wrong-kind",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_FEEDBACK_TARGET_NOT_FOUND"
    assert result["errors"][0]["details"] == {
        "target_kind": "gap",
        "target_id": "INV-curation-1",
    }


def test_feedback_record_rejects_missing_source_item_id(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """source_item_id-only feedback must reference known source items."""
    project_id, authority_id, fingerprint = _seed_pending_authority(engine)
    feedback_file = _write_feedback(
        tmp_path,
        authority_id=authority_id,
        item_overrides={
            "target_id": None,
            "source_item_id": "SRC-missing",
        },
    )

    result = AuthorityCurationRunner(engine=engine).feedback_record(
        AuthorityFeedbackRecordRequest(
            project_id=project_id,
            pending_authority_id=authority_id,
            expected_authority_fingerprint=fingerprint,
            feedback_file=str(feedback_file),
            idempotency_key="feedback-record-missing-source",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_FEEDBACK_TARGET_NOT_FOUND"
    assert result["errors"][0]["details"] == {"source_item_id": "SRC-missing"}


def test_feedback_record_rejects_project_authority_mismatch(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """A project cannot record feedback against another project's authority."""
    project_a_id, _authority_a_id, _fingerprint_a = _seed_pending_authority(
        engine,
        product_name="Authority Feedback Product A",
    )
    project_b_id, authority_b_id, fingerprint_b = _seed_pending_authority(
        engine,
        product_name="Authority Feedback Product B",
    )
    feedback_file = _write_feedback(tmp_path, authority_id=authority_b_id)

    result = AuthorityCurationRunner(engine=engine).feedback_record(
        AuthorityFeedbackRecordRequest(
            project_id=project_a_id,
            pending_authority_id=authority_b_id,
            expected_authority_fingerprint=fingerprint_b,
            feedback_file=str(feedback_file),
            idempotency_key="feedback-record-project-mismatch",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_NOT_PENDING"
    assert result["errors"][0]["details"] == {
        "project_id": project_a_id,
        "authority_id": authority_b_id,
        "authority_project_id": project_b_id,
    }
    with Session(engine) as session:
        rows = session.exec(select(AuthorityFeedbackAttempt)).all()
    assert rows == []


def test_feedback_record_persists_blocking_feedback(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Valid feedback is stored as one canonical feedback attempt row."""
    project_id, authority_id, fingerprint = _seed_pending_authority(engine)
    feedback_file = _write_feedback(tmp_path, authority_id=authority_id)

    result = AuthorityCurationRunner(engine=engine).feedback_record(
        AuthorityFeedbackRecordRequest(
            project_id=project_id,
            pending_authority_id=authority_id,
            expected_authority_fingerprint=fingerprint,
            feedback_file=str(feedback_file),
            idempotency_key="feedback-record-004",
            changed_by="curation-test",
        )
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == "authority_feedback_recorded"
    assert data["source_authority_id"] == authority_id
    assert data["source_authority_fingerprint"] == fingerprint
    assert data["has_blocking_feedback"] is True
    assert data["feedback_fingerprint"].startswith("sha256:")

    with Session(engine) as session:
        rows = session.exec(select(AuthorityFeedbackAttempt)).all()

    assert len(rows) == 1
    row = rows[0]
    assert row.feedback_attempt_id == data["feedback_attempt_id"]
    assert row.project_id == project_id
    assert row.source_authority_id == authority_id
    assert row.source_authority_fingerprint == fingerprint
    assert row.feedback_fingerprint == data["feedback_fingerprint"]
    assert row.has_blocking_feedback is True
    assert row.idempotency_key == "feedback-record-004"
    assert row.changed_by == "curation-test"
    assert json.loads(row.feedback_json)["authority_id"] == authority_id


def test_feedback_record_replays_same_idempotency_key_same_request(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Same idempotency key and same request returns the existing attempt."""
    project_id, authority_id, fingerprint = _seed_pending_authority(engine)
    feedback_file = _write_feedback(tmp_path, authority_id=authority_id)
    request = AuthorityFeedbackRecordRequest(
        project_id=project_id,
        pending_authority_id=authority_id,
        expected_authority_fingerprint=fingerprint,
        feedback_file=str(feedback_file),
        idempotency_key="feedback-record-replay",
        changed_by="curation-test",
    )
    runner = AuthorityCurationRunner(engine=engine)

    first = runner.feedback_record(request)
    second = runner.feedback_record(request)

    assert first["ok"] is True
    assert second["ok"] is True
    assert (
        second["data"]["feedback_attempt_id"]
        == first["data"]["feedback_attempt_id"]
    )
    assert second["data"] == first["data"]
    with Session(engine) as session:
        rows = session.exec(select(AuthorityFeedbackAttempt)).all()
    assert len(rows) == 1


def test_feedback_record_rejects_reused_idempotency_key_different_request(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Same idempotency key with different feedback is a conflict."""
    project_id, authority_id, fingerprint = _seed_pending_authority(engine)
    first_file = _write_feedback(
        tmp_path,
        authority_id=authority_id,
        filename="feedback-first.json",
    )
    second_file = _write_feedback(
        tmp_path,
        authority_id=authority_id,
        item_overrides={"severity": "non_blocking"},
        filename="feedback-second.json",
    )
    runner = AuthorityCurationRunner(engine=engine)
    first = runner.feedback_record(
        AuthorityFeedbackRecordRequest(
            project_id=project_id,
            pending_authority_id=authority_id,
            expected_authority_fingerprint=fingerprint,
            feedback_file=str(first_file),
            idempotency_key="feedback-record-reused",
        )
    )

    second = runner.feedback_record(
        AuthorityFeedbackRecordRequest(
            project_id=project_id,
            pending_authority_id=authority_id,
            expected_authority_fingerprint=fingerprint,
            feedback_file=str(second_file),
            idempotency_key="feedback-record-reused",
        )
    )

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["errors"][0]["code"] == "IDEMPOTENCY_KEY_REUSED"
    with Session(engine) as session:
        rows = session.exec(select(AuthorityFeedbackAttempt)).all()
    assert len(rows) == 1


def test_feedback_record_replays_after_idempotency_integrity_error(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commit-time idempotency conflicts replay the existing stored response."""
    project_id, authority_id, fingerprint = _seed_pending_authority(engine)
    feedback_file = _write_feedback(tmp_path, authority_id=authority_id)
    request = AuthorityFeedbackRecordRequest(
        project_id=project_id,
        pending_authority_id=authority_id,
        expected_authority_fingerprint=fingerprint,
        feedback_file=str(feedback_file),
        idempotency_key="feedback-record-integrity-race",
    )
    runner = AuthorityCurationRunner(engine=engine)
    first = runner.feedback_record(request)
    assert first["ok"] is True

    original_replay = curation_mod._idempotency_replay
    misses_remaining = 1

    def replay_once_missed(**kwargs: object) -> dict[str, object] | None:
        nonlocal misses_remaining
        if misses_remaining:
            misses_remaining -= 1
            return None
        return original_replay(**kwargs)

    monkeypatch.setattr(curation_mod, "_idempotency_replay", replay_once_missed)

    second = runner.feedback_record(request)

    assert second["ok"] is True
    assert second["data"] == first["data"]
    with Session(engine) as session:
        rows = session.exec(select(AuthorityFeedbackAttempt)).all()
    assert len(rows) == 1
