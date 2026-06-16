"""Tests for authority feedback curation mutations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from sqlmodel import Session, select

import services.agent_workbench.authority_curation as curation_mod
from db.migrations import ensure_schema_current
from models.agent_workbench import CliMutationLedger
from models.authority_curation import AuthorityCurationAttempt, AuthorityFeedbackAttempt
from models.core import Product
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from services.agent_workbench.authority_curation import (
    AuthorityCurationRequest,
    AuthorityCurationRunner,
    AuthorityFeedbackFile,
    AuthorityFeedbackItem,
    AuthorityFeedbackRecordRequest,
)
from services.agent_workbench.authority_projection import pending_authority_fingerprint
from services.specs.authority_curation_diff import (
    AuthorityDiffValidationError,
    build_authority_diff,
)
from tests.typing_helpers import require_id

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine


@dataclass(frozen=True)
class RejectedAuthorityFixture:
    """Rejected authority with blocking feedback ready for curation."""

    project_id: int
    spec_version_id: int
    authority_id: int
    authority_fingerprint: str
    feedback_attempt_id: str


class FakeWorkflowPort:
    """In-memory workflow port for curation runner tests."""

    def __init__(self) -> None:
        """Initialize empty workflow state."""
        self.state: dict[str, dict[str, object]] = {}

    def get_session_status(self, session_id: str) -> dict[str, object]:
        """Return workflow state for a session."""
        return dict(self.state.get(session_id, {}))

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, object],
    ) -> None:
        """Merge partial workflow state for a session."""
        current = dict(self.state.get(session_id, {}))
        current.update(partial_update)
        self.state[session_id] = current


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
                },
                {
                    "id": "INV-curation-untargeted",
                    "source_item_id": "SRC-curation-untargeted",
                    "text": "Unrelated review packets remain stable.",
                }
            ],
            "eligible_feature_rules": [],
            "rejected_features": [],
            "gaps": [{"gap_id": "GAP-curation-1"}],
            "assumptions": [{"assumption_id": "ASM-curation-1"}],
            "quality_groups": [{"group_id": "QG-curation-1"}],
            "source_map": [
                {"id": "SRC-curation-1"},
                {"id": "SRC-curation-untargeted"},
            ],
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
                        "source_item_id": "SRC-curation-1",
                        "text": "Review packets include guard evidence.",
                    },
                    {
                        "id": "INV-curation-untargeted",
                        "source_item_id": "SRC-curation-untargeted",
                        "text": "Unrelated review packets remain stable.",
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


def _insert_rejected_authority_with_feedback(
    engine: Engine,
) -> RejectedAuthorityFixture:
    """Seed a rejected authority and matching feedback attempt."""
    project_id, authority_id, fingerprint = _seed_pending_authority(engine)
    now = datetime(2026, 6, 16, 14, tzinfo=UTC)
    with Session(engine) as session:
        authority = session.get(CompiledSpecAuthority, authority_id)
        assert authority is not None
        spec_version_id = authority.spec_version_id
        spec = session.get(SpecRegistry, spec_version_id)
        assert spec is not None
        session.add(
            SpecAuthorityAcceptance(
                product_id=project_id,
                spec_version_id=spec_version_id,
                status="rejected",
                policy="manual",
                decided_by="reviewer",
                decided_at=now,
                rationale="Needs structured feedback repair.",
                compiler_version=authority.compiler_version,
                prompt_hash=authority.prompt_hash,
                spec_hash=spec.spec_hash,
                pending_authority_id=authority_id,
                actor_mode="human_review_token",
                review_completeness="complete",
                terminal_decision_key=f"{project_id}:{spec_version_id}:{authority_id}",
                provenance_source="test",
            )
        )
        feedback = AuthorityFeedbackAttempt(
            project_id=project_id,
            feedback_attempt_id="feedback-curation-1",
            source_authority_id=authority_id,
            source_authority_fingerprint=fingerprint,
            feedback_fingerprint="sha256:" + ("c" * 64),
            has_blocking_feedback=True,
            feedback_json=json.dumps(
                {
                    "feedback_items": [
                        {
                            "feedback_id": "AFB-curation-1",
                            "target_kind": "invariant",
                            "target_id": "INV-curation-1",
                            "issue_type": "overstrong_invariant",
                            "severity": "blocking",
                            "instruction": "Repair the targeted invariant.",
                        }
                    ],
                },
                sort_keys=True,
            ),
            request_hash="sha256:" + ("d" * 64),
            idempotency_key="feedback-curation-1",
            created_at=now,
            updated_at=now,
        )
        session.add(feedback)
        session.commit()

    return RejectedAuthorityFixture(
        project_id=project_id,
        spec_version_id=spec_version_id,
        authority_id=authority_id,
        authority_fingerprint=fingerprint,
        feedback_attempt_id="feedback-curation-1",
    )


def _successful_curation_result(
    fixture: RejectedAuthorityFixture,
) -> dict[str, object]:
    """Return a deterministic placeholder successful workflow result."""
    return {
        "ok": True,
        "curation_attempt_id": "curation-fake-result",
        "project_id": fixture.project_id,
        "candidate_authority_json": json.loads(_compiled_artifact_json()),
        "quality_report": {"status": "passed"},
    }


def _targeted_repair_curation_result(
    fixture: RejectedAuthorityFixture,
) -> dict[str, object]:
    """Return a candidate with one targeted invariant replacement."""
    candidate = json.loads(_compiled_artifact_json())
    candidate["invariants"] = [
        {
            "id": "INV-curation-1-repaired",
            "source_item_id": "SRC-curation-1",
            "text": "Review packets include concrete guard evidence.",
        },
        {
            "id": "INV-curation-untargeted",
            "source_item_id": "SRC-curation-untargeted",
            "text": "Unrelated review packets remain stable.",
        },
    ]
    return {
        "ok": True,
        "curation_attempt_id": "curation-fake-result",
        "project_id": fixture.project_id,
        "candidate_authority_json": candidate,
        "candidate_lineage_json": {"source": "workflow"},
        "quality_report": {"status": "passed"},
    }


def _untargeted_change_curation_result(
    fixture: RejectedAuthorityFixture,
) -> dict[str, object]:
    """Return a candidate that changes an untargeted invariant."""
    candidate = json.loads(_compiled_artifact_json())
    for invariant in candidate["invariants"]:
        if invariant["id"] == "INV-curation-untargeted":
            invariant["text"] = "Unrelated review packets changed."
    return {
        "ok": True,
        "curation_attempt_id": "curation-fake-result",
        "project_id": fixture.project_id,
        "candidate_authority_json": candidate,
        "quality_report": {"status": "passed"},
    }


def _candidate_with_missing_invariant_id_result(
    fixture: RejectedAuthorityFixture,
) -> dict[str, object]:
    """Return a candidate with a malformed invariant id."""
    candidate = json.loads(_compiled_artifact_json())
    candidate["invariants"].append(
        {
            "source_item_id": "SRC-curation-1",
            "text": "Malformed invariant without id.",
        }
    )
    return {
        "ok": True,
        "curation_attempt_id": "curation-fake-result",
        "project_id": fixture.project_id,
        "candidate_authority_json": candidate,
        "quality_report": {"status": "passed"},
    }


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


def test_authority_diff_maps_targeted_replacement_lineage() -> None:
    """Diff helper maps targeted replacement by source item id."""
    source = {
        "invariants": [
            {
                "id": "INV-oldoldoldoldold1",
                "type": "relation_constraint",
                "parameters": {"expression": "learned_model_score >= max_baseline"},
                "source_item_id": "REQ.delayed-outcome-predictor",
                "source_level": "MUST",
            },
            {
                "id": "INV-keepkeepkeepkeep",
                "type": "required_field",
                "parameters": {"field_name": "report_id"},
                "source_item_id": "DATA.operational-learning-report",
                "source_level": "MUST",
            },
        ]
    }
    candidate = {
        "invariants": [
            {
                "id": "INV-newnewnewnewnew1",
                "type": "required_field",
                "parameters": {"field_name": "baseline_comparison_summary"},
                "source_item_id": "REQ.delayed-outcome-predictor",
                "source_level": "MUST",
            },
            {
                "id": "INV-keepkeepkeepkeep",
                "type": "required_field",
                "parameters": {"field_name": "report_id"},
                "source_item_id": "DATA.operational-learning-report",
                "source_level": "MUST",
            },
        ]
    }

    diff = build_authority_diff(
        source_authority_json=source,
        candidate_authority_json=candidate,
        targeted_source_item_ids={"REQ.delayed-outcome-predictor"},
    )

    assert diff["lineage_json"]["INV-oldoldoldoldold1"]["new_id"] == (
        "INV-newnewnewnewnew1"
    )
    assert diff["summary"]["unchanged_count"] == 1
    assert diff["summary"]["changed_count"] == 1
    assert diff["summary"]["untargeted_change_count"] == 0


def test_authority_diff_treats_same_id_payload_change_as_changed() -> None:
    """Same id is not unchanged when canonical payload changes."""
    source = {
        "invariants": [
            {
                "id": "INV-stable-id",
                "type": "required_field",
                "parameters": {"field_name": "report_id"},
                "source_item_id": "SRC-untargeted",
            }
        ]
    }
    candidate = {
        "invariants": [
            {
                "id": "INV-stable-id",
                "type": "required_field",
                "parameters": {"field_name": "changed_report_id"},
                "source_item_id": "SRC-untargeted",
            }
        ]
    }

    diff = build_authority_diff(
        source_authority_json=source,
        candidate_authority_json=candidate,
        targeted_source_item_ids={"SRC-targeted"},
    )

    assert diff["unchanged_ids"] == []
    assert diff["changed_ids"] == ["INV-stable-id"]
    assert diff["summary"]["untargeted_change_count"] == 1


def test_authority_diff_rejects_missing_invariant_id() -> None:
    """Malformed invariant ids fail before diff calculations."""
    source = {
        "invariants": [
            {
                "id": "INV-source-1",
                "source_item_id": "SRC-source-1",
                "text": "Stable source invariant.",
            }
        ]
    }
    candidate = {
        "invariants": [
            {
                "source_item_id": "SRC-source-1",
                "text": "Malformed candidate invariant.",
            }
        ]
    }

    with pytest.raises(AuthorityDiffValidationError) as exc_info:
        build_authority_diff(
            source_authority_json=source,
            candidate_authority_json=candidate,
            targeted_source_item_ids={"SRC-source-1"},
        )

    assert exc_info.value.validation_errors == [
        {
            "authority": "candidate",
            "index": 0,
            "reason": "missing_or_invalid_id",
        }
    ]


def test_authority_diff_rejects_duplicate_invariant_id() -> None:
    """Duplicate invariant ids fail before one item can overwrite another."""
    source = {
        "invariants": [
            {
                "id": "INV-source-1",
                "source_item_id": "SRC-source-1",
                "text": "Stable source invariant.",
            }
        ]
    }
    candidate = {
        "invariants": [
            {
                "id": "INV-duplicate",
                "source_item_id": "SRC-source-1",
                "text": "First candidate invariant.",
            },
            {
                "id": "INV-duplicate",
                "source_item_id": "SRC-source-1",
                "text": "Second candidate invariant.",
            },
        ]
    }

    with pytest.raises(AuthorityDiffValidationError) as exc_info:
        build_authority_diff(
            source_authority_json=source,
            candidate_authority_json=candidate,
            targeted_source_item_ids={"SRC-source-1"},
        )

    assert exc_info.value.validation_errors == [
        {
            "authority": "candidate",
            "duplicate_id": "INV-duplicate",
            "first_index": 0,
            "index": 1,
            "reason": "duplicate_id",
        }
    ]


def test_authority_curate_sets_curating_before_workflow(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Long curation work must be fenced by authority_curating status."""
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    fake_workflow = FakeWorkflowPort()
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": "authority_rejected",
        },
    )
    captured_state: dict[str, object] = {}

    def fake_run_curation(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        captured_state.update(fake_workflow.get_session_status(str(fixture.project_id)))
        return _successful_curation_result(fixture)

    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        fake_run_curation,
    )
    runner = AuthorityCurationRunner(engine=engine, workflow=fake_workflow)

    result = runner.curate(
        AuthorityCurationRequest(
            project_id=fixture.project_id,
            spec_version_id=fixture.spec_version_id,
            source_authority_id=fixture.authority_id,
            expected_source_authority_fingerprint=fixture.authority_fingerprint,
            feedback_attempt_id=fixture.feedback_attempt_id,
            idempotency_key="curate-001",
        )
    )

    assert result["ok"] is True
    assert captured_state["setup_status"] == "authority_curating"
    with Session(engine) as session:
        rows = session.exec(select(AuthorityCurationAttempt)).all()
    assert len(rows) == 1
    assert rows[0].status == "succeeded"


def test_authority_curate_fails_closed_for_untargeted_diff(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host gate rejects candidate changes outside recorded feedback targets."""
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    fake_workflow = FakeWorkflowPort()
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": "authority_rejected",
        },
    )

    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        lambda **_: _untargeted_change_curation_result(fixture),
    )
    runner = AuthorityCurationRunner(engine=engine, workflow=fake_workflow)

    result = runner.curate(
        AuthorityCurationRequest(
            project_id=fixture.project_id,
            spec_version_id=fixture.spec_version_id,
            source_authority_id=fixture.authority_id,
            expected_source_authority_fingerprint=fixture.authority_fingerprint,
            feedback_attempt_id=fixture.feedback_attempt_id,
            idempotency_key="curate-untargeted-diff",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_CURATED_DIFF_UNBOUNDED"
    assert (
        fake_workflow.get_session_status(str(fixture.project_id))["setup_status"]
        == "authority_rejected"
    )
    with Session(engine) as session:
        attempt = session.exec(select(AuthorityCurationAttempt)).one()
        ledger = session.exec(select(CliMutationLedger)).one()
    assert attempt.status == "failed"
    assert ledger.status != "pending"


def test_authority_curate_fails_closed_without_candidate_json(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host gate rejects ok workflow results without candidate authority JSON."""
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    fake_workflow = FakeWorkflowPort()
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": "authority_rejected",
        },
    )

    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        lambda **_: {"ok": True, "project_id": fixture.project_id},
    )
    runner = AuthorityCurationRunner(engine=engine, workflow=fake_workflow)

    result = runner.curate(
        AuthorityCurationRequest(
            project_id=fixture.project_id,
            spec_version_id=fixture.spec_version_id,
            source_authority_id=fixture.authority_id,
            expected_source_authority_fingerprint=fixture.authority_fingerprint,
            feedback_attempt_id=fixture.feedback_attempt_id,
            idempotency_key="curate-missing-candidate-json",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "MUTATION_FAILED"
    assert result["errors"][0]["details"]["reason"] == (
        "missing_or_invalid_candidate_authority_json"
    )
    assert (
        fake_workflow.get_session_status(str(fixture.project_id))["setup_status"]
        == "authority_rejected"
    )
    with Session(engine) as session:
        attempt = session.exec(select(AuthorityCurationAttempt)).one()
    assert attempt.status == "failed"


def test_authority_curate_fails_closed_for_malformed_candidate_invariant(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host gate rejects malformed candidate invariant ids."""
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    fake_workflow = FakeWorkflowPort()
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": "authority_rejected",
        },
    )

    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        lambda **_: _candidate_with_missing_invariant_id_result(fixture),
    )
    runner = AuthorityCurationRunner(engine=engine, workflow=fake_workflow)

    result = runner.curate(
        AuthorityCurationRequest(
            project_id=fixture.project_id,
            spec_version_id=fixture.spec_version_id,
            source_authority_id=fixture.authority_id,
            expected_source_authority_fingerprint=fixture.authority_fingerprint,
            feedback_attempt_id=fixture.feedback_attempt_id,
            idempotency_key="curate-malformed-candidate-id",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_CURATED_DIFF_UNBOUNDED"
    details = result["errors"][0]["details"]
    assert details["validation_error_count"] == 1
    assert details["validation_errors"] == [
        {
            "authority": "candidate",
            "index": 2,
            "reason": "missing_or_invalid_id",
        }
    ]
    with Session(engine) as session:
        attempt = session.exec(select(AuthorityCurationAttempt)).one()
    assert attempt.status == "failed"


def test_authority_curate_gap_target_id_collision_does_not_authorize_invariant(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only invariant feedback target ids map to invariant source item ids."""
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    with Session(engine) as session:
        feedback = session.exec(select(AuthorityFeedbackAttempt)).one()
        feedback.feedback_json = json.dumps(
            {
                "feedback_items": [
                    {
                        "feedback_id": "AFB-gap-collision",
                        "target_kind": "gap",
                        "target_id": "INV-curation-1",
                        "issue_type": "invalid_gap",
                        "severity": "blocking",
                        "instruction": "This gap target id collides with invariant id.",
                    }
                ],
            },
            sort_keys=True,
        )
        session.add(feedback)
        session.commit()
    fake_workflow = FakeWorkflowPort()
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": "authority_rejected",
        },
    )

    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        lambda **_: _targeted_repair_curation_result(fixture),
    )
    runner = AuthorityCurationRunner(engine=engine, workflow=fake_workflow)

    result = runner.curate(
        AuthorityCurationRequest(
            project_id=fixture.project_id,
            spec_version_id=fixture.spec_version_id,
            source_authority_id=fixture.authority_id,
            expected_source_authority_fingerprint=fixture.authority_fingerprint,
            feedback_attempt_id=fixture.feedback_attempt_id,
            idempotency_key="curate-gap-id-collision",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_CURATED_DIFF_UNBOUNDED"
    with Session(engine) as session:
        attempt = session.exec(select(AuthorityCurationAttempt)).one()
    assert attempt.status == "failed"


def test_authority_curate_persists_diff_summary_and_lineage(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Targeted repair stores bounded diff and lineage for audit."""
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    fake_workflow = FakeWorkflowPort()
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": "authority_rejected",
        },
    )

    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        lambda **_: _targeted_repair_curation_result(fixture),
    )
    runner = AuthorityCurationRunner(engine=engine, workflow=fake_workflow)

    result = runner.curate(
        AuthorityCurationRequest(
            project_id=fixture.project_id,
            spec_version_id=fixture.spec_version_id,
            source_authority_id=fixture.authority_id,
            expected_source_authority_fingerprint=fixture.authority_fingerprint,
            feedback_attempt_id=fixture.feedback_attempt_id,
            idempotency_key="curate-targeted-lineage",
        )
    )

    assert result["ok"] is True
    assert result["data"]["diff_summary"]["changed_count"] == 1
    assert result["data"]["lineage"]["INV-curation-1"]["new_id"] == (
        "INV-curation-1-repaired"
    )
    assert "candidate_authority_json" not in json.dumps(result, sort_keys=True)
    with Session(engine) as session:
        attempt = session.exec(select(AuthorityCurationAttempt)).one()

    assert attempt.status == "succeeded"
    assert json.loads(attempt.diff_summary_json)["changed_count"] == 1
    assert json.loads(attempt.lineage_json)["INV-curation-1"]["new_id"] == (
        "INV-curation-1-repaired"
    )
    assert json.loads(attempt.candidate_lineage_json) == {"source": "workflow"}
    assert json.loads(attempt.quality_report_json) == {"status": "passed"}


def test_authority_curate_rejects_when_already_curating(
    engine: Engine,
) -> None:
    """Concurrent curation must be blocked by setup status."""
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    fake_workflow = FakeWorkflowPort()
    active_mutation_event_id = 777
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": "authority_curating",
            "setup_curation_mutation_event_id": active_mutation_event_id,
        },
    )
    runner = AuthorityCurationRunner(engine=engine, workflow=fake_workflow)

    result = runner.curate(
        AuthorityCurationRequest(
            project_id=fixture.project_id,
            spec_version_id=fixture.spec_version_id,
            source_authority_id=fixture.authority_id,
            expected_source_authority_fingerprint=fixture.authority_fingerprint,
            feedback_attempt_id=fixture.feedback_attempt_id,
            idempotency_key="curate-concurrent-001",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] in {"STALE_SETUP_STATUS", "MUTATION_IN_PROGRESS"}
    assert (
        result["errors"][0]["details"]["setup_curation_mutation_event_id"]
        == active_mutation_event_id
    )


def test_authority_curate_rejects_existing_running_attempt(
    engine: Engine,
) -> None:
    """A running curation row is the durable mutex for an authority."""
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    fake_workflow = FakeWorkflowPort()
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": "authority_rejected",
        },
    )
    with Session(engine) as session:
        session.add(
            AuthorityCurationAttempt(
                project_id=fixture.project_id,
                curation_attempt_id="curation-existing-running",
                source_authority_id=fixture.authority_id,
                source_authority_fingerprint=fixture.authority_fingerprint,
                spec_version_id=fixture.spec_version_id,
                feedback_attempt_id=fixture.feedback_attempt_id,
                status="running",
                request_hash="sha256:" + ("e" * 64),
                idempotency_key="curate-existing-running",
            )
        )
        session.commit()
    runner = AuthorityCurationRunner(engine=engine, workflow=fake_workflow)

    result = runner.curate(
        AuthorityCurationRequest(
            project_id=fixture.project_id,
            spec_version_id=fixture.spec_version_id,
            source_authority_id=fixture.authority_id,
            expected_source_authority_fingerprint=fixture.authority_fingerprint,
            feedback_attempt_id=fixture.feedback_attempt_id,
            idempotency_key="curate-concurrent-002",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "MUTATION_IN_PROGRESS"
    assert (
        result["errors"][0]["details"]["curation_attempt_id"]
        == "curation-existing-running"
    )
    assert (
        fake_workflow.get_session_status(str(fixture.project_id))["setup_status"]
        == "authority_rejected"
    )
    with Session(engine) as session:
        running_rows = session.exec(
            select(AuthorityCurationAttempt)
            .where(AuthorityCurationAttempt.project_id == fixture.project_id)
            .where(AuthorityCurationAttempt.source_authority_id == fixture.authority_id)
            .where(AuthorityCurationAttempt.status == "running")
        ).all()
    assert len(running_rows) == 1


def test_authority_curate_default_failure_restores_rejected_workflow(
    engine: Engine,
) -> None:
    """Default unimplemented workflow must fail closed without a stuck mutex."""
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    fake_workflow = FakeWorkflowPort()
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": "authority_rejected",
        },
    )
    runner = AuthorityCurationRunner(engine=engine, workflow=fake_workflow)

    result = runner.curate(
        AuthorityCurationRequest(
            project_id=fixture.project_id,
            spec_version_id=fixture.spec_version_id,
            source_authority_id=fixture.authority_id,
            expected_source_authority_fingerprint=fixture.authority_fingerprint,
            feedback_attempt_id=fixture.feedback_attempt_id,
            idempotency_key="curate-default-failure",
        )
    )

    assert result["ok"] is False
    workflow_state = fake_workflow.get_session_status(str(fixture.project_id))
    assert workflow_state["setup_status"] == "authority_rejected"
    assert workflow_state["setup_curation_mutation_event_id"] is None
    with Session(engine) as session:
        attempt = session.exec(select(AuthorityCurationAttempt)).one()
        ledger = session.exec(select(CliMutationLedger)).one()
    assert attempt.status == "failed"
    assert ledger.status != "pending"


def test_authority_curate_exception_sanitizes_and_restores_state(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workflow exceptions must not leak raw provider content or leave locks."""
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    fake_workflow = FakeWorkflowPort()
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": "authority_rejected",
        },
    )

    def raise_secret(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        message = "raw-provider-secret-token"
        raise RuntimeError(message)

    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        raise_secret,
    )
    runner = AuthorityCurationRunner(engine=engine, workflow=fake_workflow)

    result = runner.curate(
        AuthorityCurationRequest(
            project_id=fixture.project_id,
            spec_version_id=fixture.spec_version_id,
            source_authority_id=fixture.authority_id,
            expected_source_authority_fingerprint=fixture.authority_fingerprint,
            feedback_attempt_id=fixture.feedback_attempt_id,
            idempotency_key="curate-exception-failure",
        )
    )

    serialized = json.dumps(result, sort_keys=True)
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "MUTATION_FAILED"
    assert "raw-provider-secret-token" not in serialized
    assert result["errors"][0]["details"]["exception_type"] == "RuntimeError"
    workflow_state = fake_workflow.get_session_status(str(fixture.project_id))
    assert workflow_state["setup_status"] == "authority_rejected"
    assert workflow_state["setup_curation_mutation_event_id"] is None
    with Session(engine) as session:
        attempt = session.exec(select(AuthorityCurationAttempt)).one()
        ledger = session.exec(select(CliMutationLedger)).one()
    assert attempt.status == "failed"
    assert ledger.status != "pending"


def test_authority_curate_curating_status_write_failure_is_sanitized(
    engine: Engine,
) -> None:
    """Workflow write failures after mutex acquisition must not leave locks."""
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)

    class FailingWorkflowPort(FakeWorkflowPort):
        def update_session_status(
            self,
            session_id: str,
            partial_update: dict[str, object],
        ) -> None:
            """Fail all workflow writes."""
            del session_id, partial_update
            message = "workflow-write-secret"
            raise RuntimeError(message)

    fake_workflow = FailingWorkflowPort()
    fake_workflow.state[str(fixture.project_id)] = {
        "fsm_state": "SETUP_REQUIRED",
        "setup_status": "authority_rejected",
    }
    runner = AuthorityCurationRunner(engine=engine, workflow=fake_workflow)

    result = runner.curate(
        AuthorityCurationRequest(
            project_id=fixture.project_id,
            spec_version_id=fixture.spec_version_id,
            source_authority_id=fixture.authority_id,
            expected_source_authority_fingerprint=fixture.authority_fingerprint,
            feedback_attempt_id=fixture.feedback_attempt_id,
            idempotency_key="curate-workflow-write-failure",
        )
    )

    serialized = json.dumps(result, sort_keys=True)
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "MUTATION_FAILED"
    assert "workflow-write-secret" not in serialized
    assert result["errors"][0]["details"]["exception_type"] == "RuntimeError"
    with Session(engine) as session:
        attempt = session.exec(select(AuthorityCurationAttempt)).one()
        ledger = session.exec(select(CliMutationLedger)).one()
    assert attempt.status == "failed"
    assert ledger.status != "pending"


def test_authority_curate_replays_same_idempotency_key(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same curation request replays the finalized mutation response."""
    ensure_schema_current(engine)
    fixture = _insert_rejected_authority_with_feedback(engine)
    fake_workflow = FakeWorkflowPort()
    fake_workflow.update_session_status(
        str(fixture.project_id),
        {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": "authority_rejected",
        },
    )
    calls = 0

    def fake_run_curation(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        nonlocal calls
        calls += 1
        return _successful_curation_result(fixture)

    monkeypatch.setattr(
        "services.agent_workbench.authority_curation.run_authority_curation_workflow",
        fake_run_curation,
    )
    runner = AuthorityCurationRunner(engine=engine, workflow=fake_workflow)
    request = AuthorityCurationRequest(
        project_id=fixture.project_id,
        spec_version_id=fixture.spec_version_id,
        source_authority_id=fixture.authority_id,
        expected_source_authority_fingerprint=fixture.authority_fingerprint,
        feedback_attempt_id=fixture.feedback_attempt_id,
        idempotency_key="curate-replay-001",
    )

    first = runner.curate(request)
    second = runner.curate(request)

    assert first["ok"] is True
    assert second == first
    assert calls == 1
    with Session(engine) as session:
        rows = session.exec(select(AuthorityCurationAttempt)).all()
    assert len(rows) == 1
