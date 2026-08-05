"""Fresh-schema contract tests for durable product-definition records."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CheckConstraint, UniqueConstraint
from sqlmodel import Session, SQLModel

from models import product_definition
from models.db import _CURRENT_MODEL_MODULES
from models.product_definition import (
    DiscoveryArtifact,
    ProductGoalArtifact,
    ProductGoalArtifactDecision,
    ProductGoalInterviewTurn,
    ProductGoalOutcome,
    SpecificationCandidate,
    SpecificationDecision,
    VisionInterviewTurn,
    VisionRevisionIntent,
)

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
    VisionInterviewTurn: {
        "vision_interview_turn_id",
        "project_id",
        "mode",
        "turn_number",
        "revision_intent_id",
        "prior_turn_id",
        "user_text",
        "components_json",
        "vision_statement",
        "is_complete",
        "clarifying_questions_json",
        "output_fingerprint",
        "workflow_node_attempt_id",
        "attempt_fingerprint",
        "recorded_at",
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
    DiscoveryArtifact: {
        "discovery_artifact_id",
        "project_id",
        "vision_artifact_id",
        "vision_fingerprint",
        "product_goal_artifact_id",
        "product_goal_fingerprint",
        "canonical_content_json",
        "content_fingerprint",
        "content_ref",
        "producer",
        "supersedes_discovery_artifact_id",
        "recorded_by",
        "recorded_at",
    },
    SpecificationCandidate: {
        "specification_candidate_id",
        "project_id",
        "vision_artifact_id",
        "vision_fingerprint",
        "product_goal_artifact_id",
        "product_goal_fingerprint",
        "discovery_artifact_id",
        "discovery_fingerprint",
        "base_spec_version_id",
        "base_spec_hash",
        "canonical_content_json",
        "content_fingerprint",
        "content_ref",
        "supersedes_specification_candidate_id",
        "recorded_by",
        "recorded_at",
    },
    SpecificationDecision: {
        "specification_decision_id",
        "project_id",
        "specification_candidate_id",
        "artifact_fingerprint",
        "decision",
        "rationale",
        "reviewer",
        "idempotency_key",
        "decided_at",
    },
}

EXPECTED_TABLE_NAMES: dict[type[SQLModel], str] = {
    VisionRevisionIntent: "vision_revision_intents",
    VisionInterviewTurn: "vision_interview_turns",
    ProductGoalInterviewTurn: "product_goal_interview_turns",
    ProductGoalArtifact: "product_goal_artifacts",
    ProductGoalArtifactDecision: "product_goal_artifact_decisions",
    ProductGoalOutcome: "product_goal_outcomes",
    DiscoveryArtifact: "discovery_artifacts",
    SpecificationCandidate: "specification_candidates",
    SpecificationDecision: "specification_decisions",
}


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
        "vision_interview_turns",
        "product_goal_interview_turns",
        "product_goal_artifacts",
        "product_goal_artifact_decisions",
        "product_goal_outcomes",
        "discovery_artifacts",
        "specification_candidates",
        "specification_decisions",
    } <= names
    assert product_definition in _CURRENT_MODEL_MODULES


def test_product_definition_records_expose_exact_immutable_columns() -> None:
    """Keep every durable record's persisted shape explicit and additive."""
    for model, expected_columns in EXPECTED_COLUMNS.items():
        table = SQLModel.metadata.tables[EXPECTED_TABLE_NAMES[model]]
        assert set(table.columns.keys()) == expected_columns

    candidate = SQLModel.metadata.tables["specification_candidates"]
    assert candidate.c.canonical_content_json.type.__class__.__name__ == "Text"
    assert candidate.c.content_ref.nullable


def test_product_definition_records_enforce_scoped_lineage_and_values() -> None:
    """Reject cross-Project parents and unsupported mode or decision values."""
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
        ("project_id", "prior_turn_id"),
        (
            "vision_interview_turns.project_id",
            "vision_interview_turns.vision_interview_turn_id",
        ),
    ) in _foreign_keys("vision_interview_turns")
    assert "mode IN ('initial', 'revision')" in _checks("vision_interview_turns")
    assert "outcome IN ('fulfilled', 'abandoned')" in _checks("product_goal_outcomes")
    assert "decision IN ('accepted', 'rejected', 'feedback')" in _checks(
        "product_goal_artifact_decisions"
    )
    assert "decision IN ('accepted', 'rejected')" in _checks("specification_decisions")

    vision_table = SQLModel.metadata.tables["vision_interview_turns"]
    initial_index = next(
        index
        for index in vision_table.indexes
        if index.name == "uq_vision_interview_initial_turn_number"
    )
    assert initial_index.unique
    assert tuple(initial_index.columns.keys()) == ("project_id", "turn_number")
    assert str(initial_index.dialect_options["sqlite"]["where"]) == "mode = 'initial'"
    revision_index = next(
        index
        for index in vision_table.indexes
        if index.name == "uq_vision_interview_revision_turn_number"
    )
    assert revision_index.unique
    assert tuple(revision_index.columns.keys()) == (
        "project_id",
        "revision_intent_id",
        "turn_number",
    )
    assert str(revision_index.dialect_options["sqlite"]["where"]) == "mode = 'revision'"

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
        ("project_id", "discovery_artifact_id", "discovery_fingerprint"),
        (
            "discovery_artifacts.project_id",
            "discovery_artifacts.discovery_artifact_id",
            "discovery_artifacts.content_fingerprint",
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


def test_spec_registry_stages_nullable_product_definition_lineage() -> None:
    """Keep legacy specification rows valid before Task 4 owns all writes."""
    table = SQLModel.metadata.tables["spec_registry"]

    expected_columns = {
        "source_specification_candidate_id",
        "source_vision_artifact_id",
        "source_vision_fingerprint",
        "source_product_goal_artifact_id",
        "source_product_goal_fingerprint",
        "source_discovery_artifact_id",
        "source_discovery_fingerprint",
        "supersedes_spec_version_id",
    }
    assert expected_columns <= set(table.columns.keys())
    assert all(table.c[column].nullable for column in expected_columns)
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("source_specification_candidate_id",) in unique_columns


def _insert_vision_turn(
    session: Session,
    *,
    mode: str,
    revision_intent_id: int | None,
    turn_number: int,
) -> None:
    """Insert minimal rows to exercise SQLite's partial unique indexes."""
    session.connection().exec_driver_sql(
        "INSERT INTO vision_interview_turns ("
        "project_id, mode, turn_number, revision_intent_id, prior_turn_id, "
        "user_text, components_json, vision_statement, is_complete, "
        "clarifying_questions_json, output_fingerprint, workflow_node_attempt_id, "
        "attempt_fingerprint, recorded_at"
        ") VALUES (1, :mode, :turn_number, :revision_intent_id, NULL, "
        "'user', '{}', 'statement', 1, '[]', 'sha256:output', 1, "
        "'sha256:attempt', '2026-08-05 12:00:00')",
        {
            "mode": mode,
            "revision_intent_id": revision_intent_id,
            "turn_number": turn_number,
        },
    )


def test_vision_interview_turn_number_indexes_are_scoped_to_each_chain(
    engine: Engine,
) -> None:
    """Allow equal chain-local numbers and reject duplicates in one chain."""
    with Session(engine) as session:
        session.connection().exec_driver_sql("PRAGMA foreign_keys = OFF")
        _insert_vision_turn(
            session,
            mode="initial",
            revision_intent_id=None,
            turn_number=1,
        )
        _insert_vision_turn(
            session,
            mode="revision",
            revision_intent_id=10,
            turn_number=1,
        )
        _insert_vision_turn(
            session,
            mode="revision",
            revision_intent_id=11,
            turn_number=1,
        )
        with pytest.raises(IntegrityError):
            _insert_vision_turn(
                session,
                mode="initial",
                revision_intent_id=None,
                turn_number=1,
            )
        session.rollback()

        _insert_vision_turn(
            session,
            mode="revision",
            revision_intent_id=10,
            turn_number=1,
        )
        with pytest.raises(IntegrityError):
            _insert_vision_turn(
                session,
                mode="revision",
                revision_intent_id=10,
                turn_number=1,
            )
        session.rollback()
        session.connection().exec_driver_sql("PRAGMA foreign_keys = ON")
