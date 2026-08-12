"""Fresh-schema contract tests for durable product-definition records."""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CheckConstraint, UniqueConstraint
from sqlmodel import Session, SQLModel

from models import product_definition
from models.db import _CURRENT_MODEL_MODULES
from models.product_definition import (
    ProductGoalArtifact,
    ProductGoalArtifactDecision,
    ProductGoalInterviewTurn,
    ProductGoalOutcome,
    SpecificationCandidate,
    SpecificationDecision,
    SpecificationSource,
    VisionArtifact,
    VisionEvidenceSnapshot,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from services.contracts.specification_source import (
    SpecificationContextCapture,
    SpecificationRepositoryRevision,
    SpecificationSourceBundle,
    SpecificationSourceDocument,
    source_bundle_fingerprint,
)
from workflow.fingerprints import canonical_json

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

EXPECTED_COLUMNS: dict[type[SQLModel], set[str]] = {
    VisionRevisionIntent: {
        "vision_revision_intent_id",
        "project_id",
        "source_vision_artifact_id",
        "source_vision_fingerprint",
        "reason",
        "initiated_by",
        "initiated_at",
    },
    VisionEvidenceSnapshot: {
        "vision_evidence_snapshot_id",
        "project_id",
        "repository_binding_id",
        "supersedes_vision_evidence_snapshot_id",
        "workflow_node_attempt_id",
        "evidence_json",
        "evidence_fingerprint",
        "warnings_json",
        "created_at",
    },
    VisionInterviewTurn: {
        "vision_interview_turn_id",
        "project_id",
        "operation",
        "turn_number",
        "revision_intent_id",
        "vision_evidence_snapshot_id",
        "prior_turn_id",
        "user_text",
        "components_json",
        "vision_statement",
        "is_complete",
        "clarifying_questions_json",
        "component_basis_json",
        "assumptions_json",
        "conflicts_json",
        "output_fingerprint",
        "workflow_node_attempt_id",
        "attempt_fingerprint",
        "recorded_at",
    },
    VisionArtifact: {
        "vision_artifact_id",
        "project_id",
        "version_number",
        "components_json",
        "statement",
        "content_fingerprint",
        "vision_evidence_snapshot_id",
        "component_basis_json",
        "assumptions_json",
        "conflicts_json",
        "supersedes_vision_artifact_id",
        "source_interview_turn_id",
        "created_by",
        "created_at",
    },
    ProductGoalInterviewTurn: {
        "product_goal_interview_turn_id",
        "project_id",
        "vision_artifact_id",
        "vision_fingerprint",
        "goal_number",
        "revision_number",
        "prior_turn_id",
        "user_text",
        "components_json",
        "goal_statement",
        "is_complete",
        "clarifying_questions_json",
        "output_fingerprint",
        "workflow_node_attempt_id",
        "attempt_fingerprint",
        "recorded_at",
    },
    ProductGoalArtifact: {
        "product_goal_artifact_id",
        "project_id",
        "vision_artifact_id",
        "vision_fingerprint",
        "goal_number",
        "revision_number",
        "statement",
        "content_fingerprint",
        "supersedes_product_goal_artifact_id",
        "source_interview_turn_id",
        "created_by",
        "created_at",
    },
    ProductGoalArtifactDecision: {
        "product_goal_artifact_decision_id",
        "project_id",
        "product_goal_artifact_id",
        "artifact_fingerprint",
        "decision",
        "rationale",
        "reviewer",
        "idempotency_key",
        "decided_at",
    },
    ProductGoalOutcome: {
        "product_goal_outcome_id",
        "project_id",
        "product_goal_artifact_id",
        "artifact_fingerprint",
        "outcome",
        "rationale",
        "decided_by",
        "idempotency_key",
        "decided_at",
    },
    SpecificationSource: {
        "specification_source_id",
        "project_id",
        "source_bundle_json",
        "source_fingerprint",
        "repository_binding_id",
        "repository_head_sha",
        "repository_dirty",
        "repository_status_fingerprint",
        "vision_artifact_id",
        "vision_fingerprint",
        "product_goal_artifact_id",
        "product_goal_fingerprint",
        "supersedes_specification_source_id",
        "supersedes_source_fingerprint",
        "registered_by",
        "registered_at",
    },
    SpecificationCandidate: {
        "specification_candidate_id",
        "project_id",
        "candidate_kind",
        "specification_source_id",
        "specification_source_fingerprint",
        "vision_artifact_id",
        "vision_fingerprint",
        "product_goal_artifact_id",
        "product_goal_fingerprint",
        "base_spec_version_id",
        "base_spec_hash",
        "canonical_envelope_json",
        "payload_fingerprint",
        "source_manifest_fingerprint",
        "producer_input_fingerprint",
        "rendered_view_fingerprint",
        "candidate_fingerprint",
        "workflow_node_attempt_id",
        "attempt_fingerprint",
        "supersedes_specification_candidate_id",
        "supersedes_candidate_fingerprint",
        "recorded_by",
        "recorded_at",
    },
    SpecificationDecision: {
        "specification_decision_id",
        "project_id",
        "specification_candidate_id",
        "candidate_fingerprint",
        "decision",
        "rationale",
        "reviewer",
        "idempotency_key",
        "decided_at",
    },
}

EXPECTED_TABLE_NAMES: dict[type[SQLModel], str] = {
    VisionRevisionIntent: "vision_revision_intents",
    VisionEvidenceSnapshot: "vision_evidence_snapshots",
    VisionInterviewTurn: "vision_interview_turns",
    VisionArtifact: "vision_artifacts",
    ProductGoalInterviewTurn: "product_goal_interview_turns",
    ProductGoalArtifact: "product_goal_artifacts",
    ProductGoalArtifactDecision: "product_goal_artifact_decisions",
    ProductGoalOutcome: "product_goal_outcomes",
    SpecificationSource: "specification_sources",
    SpecificationCandidate: "specification_candidates",
    SpecificationDecision: "specification_decisions",
}


def _source_document(
    *,
    source_id: str,
    relative_path: str,
    content: bytes,
) -> SpecificationSourceDocument:
    """Build one byte-exact source document for contract tests."""
    return SpecificationSourceDocument(
        source_id=source_id,
        relative_path=relative_path,
        content_base64=base64.b64encode(content).decode("ascii"),
        byte_length=len(content),
        content_fingerprint=("sha256:" + hashlib.sha256(content).hexdigest()),
    )


def _source_bundle(
    *,
    context: SpecificationContextCapture | None = None,
    adrs: tuple[SpecificationSourceDocument, ...] = (),
) -> SpecificationSourceBundle:
    """Build one valid portable registered-source bundle."""
    return SpecificationSourceBundle(
        source=_source_document(
            source_id="SRC.specification-source.primary",
            relative_path="SPECIFICATION.md",
            content=b"# Exact source\r\n\xef\xbb\xbfbytes\n",
        ),
        context=(
            SpecificationContextCapture(state="absent") if context is None else context
        ),
        adrs=adrs,
        repository_revision=SpecificationRepositoryRevision(
            head_sha="a" * 40,
            dirty=True,
            status_fingerprint="sha256:" + "b" * 64,
        ),
        accepted_vision_fingerprint="sha256:" + "c" * 64,
        accepted_product_goal_fingerprint="sha256:" + "d" * 64,
    )


def test_source_bundle_is_closed_byte_exact_and_canonical() -> None:
    """Canonical identity preserves bytes and ignores ADR input order."""
    first = _source_document(
        source_id=(
            "SRC.specification-source.adr."
            + hashlib.sha256(b"docs/adr/0001-a.md").hexdigest()
        ),
        relative_path="docs/adr/0001-a.md",
        content=b"# A\r\n",
    )
    second = _source_document(
        source_id=(
            "SRC.specification-source.adr."
            + hashlib.sha256(b"docs/adr/0002-b.md").hexdigest()
        ),
        relative_path="docs/adr/0002-b.md",
        content=b"# B\n",
    )
    baseline = _source_bundle(adrs=(second, first))
    permuted = _source_bundle(adrs=(first, second))

    assert [item.relative_path for item in baseline.adrs] == [
        "docs/adr/0001-a.md",
        "docs/adr/0002-b.md",
    ]
    assert source_bundle_fingerprint(baseline) == source_bundle_fingerprint(permuted)
    assert base64.b64decode(baseline.source.content_base64) == (
        b"# Exact source\r\n\xef\xbb\xbfbytes\n"
    )
    assert canonical_json(baseline.model_dump(mode="json")) == canonical_json(
        permuted.model_dump(mode="json")
    )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SpecificationSourceBundle.model_validate(
            {**baseline.model_dump(mode="json"), "lifecycle_status": "accepted"}
        )


def test_source_bundle_distinguishes_absent_and_present_context() -> None:
    """An explicitly present empty Context is not equivalent to absence."""
    present = SpecificationContextCapture(
        state="present",
        document=_source_document(
            source_id="SRC.specification-source.context",
            relative_path="CONTEXT.md",
            content=b"",
        ),
    )

    assert source_bundle_fingerprint(_source_bundle()) != source_bundle_fingerprint(
        _source_bundle(context=present)
    )
    with pytest.raises(ValidationError, match="present context requires a document"):
        SpecificationContextCapture(state="present")
    with pytest.raises(
        ValidationError,
        match="absent context cannot include a document",
    ):
        SpecificationContextCapture(state="absent", document=present.document)


@pytest.mark.parametrize(
    ("relative_path", "content"),
    [
        ("../SPECIFICATION.md", b"valid"),
        ("/SPECIFICATION.md", b"valid"),
        ("docs\\SPECIFICATION.md", b"valid"),
        ("SPECIFICATION.md", b"\xff"),
    ],
)
def test_source_document_rejects_unsafe_paths_and_invalid_utf8(
    relative_path: str,
    content: bytes,
) -> None:
    """Registered documents are safe repository-relative UTF-8 bytes."""
    with pytest.raises(ValidationError):
        _source_document(
            source_id="SRC.specification-source.primary",
            relative_path=relative_path,
            content=content,
        )


def _foreign_keys(table_name: str) -> set[tuple[tuple[str, ...], tuple[str, ...]]]:
    """Return the same-Project foreign keys declared by one product table."""
    table = SQLModel.metadata.tables[table_name]
    return {
        (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in table.foreign_key_constraints
    }


def _checks(table_name: str) -> set[str]:
    """Return explicit check-constraint SQL for one product table."""
    table = SQLModel.metadata.tables[table_name]
    return {
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_fresh_schema_has_versioned_product_definition_tables(engine: Engine) -> None:
    """Register the exact additive product-definition tables in a fresh schema."""
    names = set(inspect(engine).get_table_names())

    assert {
        "vision_revision_intents",
        "vision_evidence_snapshots",
        "vision_interview_turns",
        "vision_artifacts",
        "product_goal_interview_turns",
        "product_goal_artifacts",
        "product_goal_artifact_decisions",
        "product_goal_outcomes",
        "specification_sources",
        "specification_candidates",
        "specification_decisions",
    } <= names
    assert "discovery_artifacts" not in names
    assert product_definition in _CURRENT_MODEL_MODULES


def test_product_definition_records_expose_exact_immutable_columns() -> None:
    """Keep every durable record's persisted shape explicit and additive."""
    for model, expected_columns in EXPECTED_COLUMNS.items():
        table = SQLModel.metadata.tables[EXPECTED_TABLE_NAMES[model]]
        assert set(table.columns.keys()) == expected_columns

    candidate = SQLModel.metadata.tables["specification_candidates"]
    assert candidate.c.canonical_envelope_json.type.__class__.__name__ == "Text"


def test_product_definition_records_enforce_scoped_lineage_and_values() -> None:
    """Reject cross-Project parents and unsupported operation or decision values."""
    assert (
        ("project_id", "repository_binding_id"),
        (
            "repository_bindings.project_id",
            "repository_bindings.repository_binding_id",
        ),
    ) in _foreign_keys("vision_evidence_snapshots")
    assert (
        ("project_id", "supersedes_vision_evidence_snapshot_id"),
        (
            "vision_evidence_snapshots.project_id",
            "vision_evidence_snapshots.vision_evidence_snapshot_id",
        ),
    ) in _foreign_keys("vision_evidence_snapshots")
    assert (
        ("project_id", "source_vision_artifact_id", "source_vision_fingerprint"),
        (
            "vision_artifacts.project_id",
            "vision_artifacts.vision_artifact_id",
            "vision_artifacts.content_fingerprint",
        ),
    ) in _foreign_keys("vision_revision_intents")
    assert (
        ("project_id", "revision_intent_id"),
        (
            "vision_revision_intents.project_id",
            "vision_revision_intents.vision_revision_intent_id",
        ),
    ) in _foreign_keys("vision_interview_turns")
    assert (
        ("project_id", "vision_evidence_snapshot_id"),
        (
            "vision_evidence_snapshots.project_id",
            "vision_evidence_snapshots.vision_evidence_snapshot_id",
        ),
    ) in _foreign_keys("vision_interview_turns")
    assert (
        ("project_id", "prior_turn_id"),
        (
            "vision_interview_turns.project_id",
            "vision_interview_turns.vision_interview_turn_id",
        ),
    ) in _foreign_keys("vision_interview_turns")
    assert "operation IN ('bootstrap', 'clarification', 'revision')" in _checks(
        "vision_interview_turns"
    )
    assert (
        "((operation = 'bootstrap' AND user_text IS NULL) "
        "OR (operation IN ('clarification', 'revision') "
        "AND user_text IS NOT NULL))"
    ) in _checks("vision_interview_turns")
    assert "outcome IN ('fulfilled', 'abandoned')" in _checks("product_goal_outcomes")
    assert "decision IN ('accepted', 'rejected', 'feedback')" in _checks(
        "product_goal_artifact_decisions"
    )
    assert "decision IN ('accepted', 'rejected', 'feedback')" in _checks(
        "specification_decisions"
    )

    source_fingerprints = _foreign_keys("specification_sources")
    assert (
        ("project_id", "repository_binding_id"),
        (
            "repository_bindings.project_id",
            "repository_bindings.repository_binding_id",
        ),
    ) in source_fingerprints
    assert (
        ("project_id", "vision_artifact_id", "vision_fingerprint"),
        (
            "vision_artifacts.project_id",
            "vision_artifacts.vision_artifact_id",
            "vision_artifacts.content_fingerprint",
        ),
    ) in source_fingerprints
    assert (
        ("project_id", "product_goal_artifact_id", "product_goal_fingerprint"),
        (
            "product_goal_artifacts.project_id",
            "product_goal_artifacts.product_goal_artifact_id",
            "product_goal_artifacts.content_fingerprint",
        ),
    ) in source_fingerprints
    assert (
        (
            "project_id",
            "supersedes_specification_source_id",
            "supersedes_source_fingerprint",
        ),
        (
            "specification_sources.project_id",
            "specification_sources.specification_source_id",
            "specification_sources.source_fingerprint",
        ),
    ) in source_fingerprints
    assert (
        "(supersedes_specification_source_id IS NULL "
        "AND supersedes_source_fingerprint IS NULL) OR "
        "(supersedes_specification_source_id IS NOT NULL "
        "AND supersedes_source_fingerprint IS NOT NULL)"
    ) in _checks("specification_sources")

    vision_constraints = {
        constraint.name: tuple(constraint.columns.keys())
        for constraint in SQLModel.metadata.tables["vision_interview_turns"].constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert vision_constraints["uq_vision_interview_snapshot_turn_number"] == (
        "project_id",
        "vision_evidence_snapshot_id",
        "turn_number",
    )

    vision_artifact_keys = _foreign_keys("vision_artifacts")
    assert (
        ("project_id", "vision_evidence_snapshot_id"),
        (
            "vision_evidence_snapshots.project_id",
            "vision_evidence_snapshots.vision_evidence_snapshot_id",
        ),
    ) in vision_artifact_keys

    goal_fingerprints = _foreign_keys("product_goal_artifacts")
    assert (
        ("project_id", "vision_artifact_id", "vision_fingerprint"),
        (
            "vision_artifacts.project_id",
            "vision_artifacts.vision_artifact_id",
            "vision_artifacts.content_fingerprint",
        ),
    ) in goal_fingerprints
    assert (
        ("project_id", "source_interview_turn_id"),
        (
            "product_goal_interview_turns.project_id",
            "product_goal_interview_turns.product_goal_interview_turn_id",
        ),
    ) in goal_fingerprints
    assert (
        not SQLModel.metadata.tables["product_goal_artifacts"]
        .c["source_interview_turn_id"]
        .nullable
    )
    assert (
        ("project_id", "supersedes_product_goal_artifact_id"),
        (
            "product_goal_artifacts.project_id",
            "product_goal_artifacts.product_goal_artifact_id",
        ),
    ) in goal_fingerprints

    candidate_fingerprints = _foreign_keys("specification_candidates")
    assert (
        (
            "project_id",
            "specification_source_id",
            "specification_source_fingerprint",
        ),
        (
            "specification_sources.project_id",
            "specification_sources.specification_source_id",
            "specification_sources.source_fingerprint",
        ),
    ) in candidate_fingerprints
    assert (
        ("project_id", "vision_artifact_id", "vision_fingerprint"),
        (
            "vision_artifacts.project_id",
            "vision_artifacts.vision_artifact_id",
            "vision_artifacts.content_fingerprint",
        ),
    ) in candidate_fingerprints
    assert (
        ("project_id", "product_goal_artifact_id", "product_goal_fingerprint"),
        (
            "product_goal_artifacts.project_id",
            "product_goal_artifacts.product_goal_artifact_id",
            "product_goal_artifacts.content_fingerprint",
        ),
    ) in candidate_fingerprints
    assert (
        ("project_id", "base_spec_version_id", "base_spec_hash"),
        (
            "spec_registry.project_id",
            "spec_registry.spec_version_id",
            "spec_registry.spec_hash",
        ),
    ) in candidate_fingerprints
    assert (
        ("project_id", "workflow_node_attempt_id", "attempt_fingerprint"),
        (
            "workflow_node_attempts.project_id",
            "workflow_node_attempts.workflow_node_attempt_id",
            "workflow_node_attempts.attempt_fingerprint",
        ),
    ) in candidate_fingerprints
    assert (
        (
            "project_id",
            "supersedes_specification_candidate_id",
            "supersedes_candidate_fingerprint",
        ),
        (
            "specification_candidates.project_id",
            "specification_candidates.specification_candidate_id",
            "specification_candidates.candidate_fingerprint",
        ),
    ) in candidate_fingerprints
    assert (
        "(candidate_kind = 'initial' AND base_spec_version_id IS NULL "
        "AND base_spec_hash IS NULL) OR (candidate_kind = 'amendment' "
        "AND base_spec_version_id IS NOT NULL AND base_spec_hash IS NOT NULL)"
    ) in _checks("specification_candidates")
    assert (
        "(supersedes_specification_candidate_id IS NULL "
        "AND supersedes_candidate_fingerprint IS NULL) OR "
        "(supersedes_specification_candidate_id IS NOT NULL "
        "AND supersedes_candidate_fingerprint IS NOT NULL)"
    ) in _checks("specification_candidates")

    candidate_uniques = {
        tuple(constraint.columns.keys())
        for constraint in SQLModel.metadata.tables[
            "specification_candidates"
        ].constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert (
        "project_id",
        "specification_candidate_id",
        "candidate_fingerprint",
    ) in candidate_uniques
    assert (
        "project_id",
        "specification_candidate_id",
        "candidate_fingerprint",
        "payload_fingerprint",
    ) in candidate_uniques
    assert ("project_id", "workflow_node_attempt_id") in candidate_uniques
    assert ("project_id", "supersedes_specification_candidate_id") in candidate_uniques

    decision_fingerprints = _foreign_keys("specification_decisions")
    assert (
        ("project_id", "specification_candidate_id", "candidate_fingerprint"),
        (
            "specification_candidates.project_id",
            "specification_candidates.specification_candidate_id",
            "specification_candidates.candidate_fingerprint",
        ),
    ) in decision_fingerprints
    decision_uniques = {
        tuple(constraint.columns.keys())
        for constraint in SQLModel.metadata.tables[
            "specification_decisions"
        ].constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("project_id", "specification_candidate_id") in decision_uniques

    attempt_uniques = {
        tuple(constraint.columns.keys())
        for constraint in SQLModel.metadata.tables["workflow_node_attempts"].constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert (
        "project_id",
        "workflow_node_attempt_id",
        "attempt_fingerprint",
    ) in attempt_uniques


def test_spec_registry_requires_product_definition_lineage() -> None:
    """Require Task 4 provenance for every registered specification version."""
    table = SQLModel.metadata.tables["spec_registry"]

    expected_columns = {
        "source_specification_candidate_id",
        "source_specification_candidate_fingerprint",
        "source_vision_artifact_id",
        "source_vision_fingerprint",
        "source_product_goal_artifact_id",
        "source_product_goal_fingerprint",
        "supersedes_spec_version_id",
    }
    assert expected_columns <= set(table.columns.keys())
    retired_columns = {
        "content",
        "content_ref",
        "source_discovery_artifact_id",
        "source_discovery_fingerprint",
    }
    assert retired_columns.isdisjoint(table.columns.keys())
    required_columns = expected_columns - {"supersedes_spec_version_id"}
    assert all(not table.c[column].nullable for column in required_columns)
    assert table.c.supersedes_spec_version_id.nullable
    assert "status IN ('approved', 'superseded')" in _checks("spec_registry")
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert (
        "project_id",
        "source_specification_candidate_id",
    ) in unique_columns
    assert (
        "project_id",
        "source_specification_candidate_id",
        "source_specification_candidate_fingerprint",
        "spec_hash",
    ) in unique_columns
    assert (
        (
            "project_id",
            "source_specification_candidate_id",
            "source_specification_candidate_fingerprint",
            "spec_hash",
        ),
        (
            "specification_candidates.project_id",
            "specification_candidates.specification_candidate_id",
            "specification_candidates.candidate_fingerprint",
            "specification_candidates.payload_fingerprint",
        ),
    ) in _foreign_keys("spec_registry")
    assert (
        (
            "project_id",
            "source_vision_artifact_id",
            "source_vision_fingerprint",
        ),
        (
            "vision_artifacts.project_id",
            "vision_artifacts.vision_artifact_id",
            "vision_artifacts.content_fingerprint",
        ),
    ) in _foreign_keys("spec_registry")
    assert (
        (
            "project_id",
            "source_product_goal_artifact_id",
            "source_product_goal_fingerprint",
        ),
        (
            "product_goal_artifacts.project_id",
            "product_goal_artifacts.product_goal_artifact_id",
            "product_goal_artifacts.content_fingerprint",
        ),
    ) in _foreign_keys("spec_registry")


def _insert_vision_turn(
    session: Session,
    *,
    operation: str,
    revision_intent_id: int | None,
    vision_evidence_snapshot_id: int,
    turn_number: int,
) -> None:
    """Insert minimal rows to exercise snapshot-scoped turn uniqueness."""
    session.connection().exec_driver_sql(
        "INSERT INTO vision_interview_turns ("
        "project_id, operation, turn_number, revision_intent_id, "
        "vision_evidence_snapshot_id, prior_turn_id, "
        "user_text, components_json, vision_statement, is_complete, "
        "clarifying_questions_json, component_basis_json, assumptions_json, "
        "conflicts_json, output_fingerprint, workflow_node_attempt_id, "
        "attempt_fingerprint, recorded_at"
        ") VALUES (1, :operation, :turn_number, :revision_intent_id, "
        ":vision_evidence_snapshot_id, NULL, "
        ":user_text, '{}', 'statement', 1, '[]', '[]', '[]', '[]', "
        "'sha256:output', 1, "
        "'sha256:attempt', '2026-08-05 12:00:00')",
        {
            "operation": operation,
            "revision_intent_id": revision_intent_id,
            "vision_evidence_snapshot_id": vision_evidence_snapshot_id,
            "turn_number": turn_number,
            "user_text": None if operation == "bootstrap" else "user",
        },
    )


def test_vision_interview_turn_numbers_are_scoped_to_each_snapshot_lineage(
    engine: Engine,
) -> None:
    """Allow equal lineage-local numbers and reject duplicates on one snapshot."""
    with Session(engine) as session:
        session.connection().exec_driver_sql("PRAGMA foreign_keys = OFF")
        _insert_vision_turn(
            session,
            operation="bootstrap",
            revision_intent_id=None,
            vision_evidence_snapshot_id=1,
            turn_number=1,
        )
        _insert_vision_turn(
            session,
            operation="revision",
            revision_intent_id=10,
            vision_evidence_snapshot_id=2,
            turn_number=1,
        )
        _insert_vision_turn(
            session,
            operation="revision",
            revision_intent_id=11,
            vision_evidence_snapshot_id=3,
            turn_number=1,
        )
        with pytest.raises(IntegrityError):
            _insert_vision_turn(
                session,
                operation="bootstrap",
                revision_intent_id=None,
                vision_evidence_snapshot_id=1,
                turn_number=1,
            )
        session.rollback()

        _insert_vision_turn(
            session,
            operation="revision",
            revision_intent_id=10,
            vision_evidence_snapshot_id=2,
            turn_number=1,
        )
        with pytest.raises(IntegrityError):
            _insert_vision_turn(
                session,
                operation="revision",
                revision_intent_id=10,
                vision_evidence_snapshot_id=2,
                turn_number=1,
            )
        session.rollback()
        session.connection().exec_driver_sql("PRAGMA foreign_keys = ON")
