"""Tests for authority decision storage migrations."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from db.migrations import (
    migrate_agent_workbench_contract_tables,
    migrate_spec_authority_tables,
)
from services.agent_workbench import schema_readiness

LEGACY_ACCEPTANCE_SQL = """
CREATE TABLE spec_authority_acceptance (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL,
  spec_version_id INTEGER NOT NULL,
  status VARCHAR NOT NULL,
  policy VARCHAR NOT NULL,
  decided_by VARCHAR NOT NULL,
  decided_at DATETIME NOT NULL,
  rationale TEXT,
  compiler_version VARCHAR NOT NULL,
  prompt_hash VARCHAR NOT NULL,
  spec_hash VARCHAR NOT NULL
)
"""

MINIMAL_COMPILED_AUTHORITY_SQL = """
CREATE TABLE compiled_spec_authority (
  authority_id INTEGER PRIMARY KEY,
  spec_version_id INTEGER NOT NULL,
  compiler_version VARCHAR NOT NULL,
  prompt_hash VARCHAR NOT NULL,
  compiled_at DATETIME NOT NULL,
  scope_themes TEXT NOT NULL,
  invariants TEXT NOT NULL,
  eligible_feature_ids TEXT NOT NULL
)
"""

NEW_AUTHORITY_DECISION_COLUMNS = {
    "pending_authority_id",
    "authority_fingerprint",
    "review_token",
    "review_fingerprint",
    "disk_spec_hash",
    "resolved_spec_path",
    "actor_mode",
    "review_completeness",
    "incomplete_review_override",
    "incomplete_review_rationale",
    "terminal_decision_key",
    "provenance_source",
}
LEGACY_PRODUCT_ID = 7
LEGACY_SPEC_VERSION_ID = 11
LEGACY_AUTHORITY_ID = 13


def _engine(tmp_path: Path) -> Engine:
    db_path = tmp_path / "authority_decision.sqlite3"
    return create_engine(f"sqlite:///{db_path.as_posix()}")


def _create_legacy_acceptance_table(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(LEGACY_ACCEPTANCE_SQL))


def _create_minimal_compiled_authority_table(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(MINIMAL_COMPILED_AUTHORITY_SQL))


def _insert_legacy_acceptance(
    engine: Engine,
    *,
    product_id: int = 7,
    spec_version_id: int = 11,
    status: str = "accepted",
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO spec_authority_acceptance (
                    product_id,
                    spec_version_id,
                    status,
                    policy,
                    decided_by,
                    decided_at,
                    rationale,
                    compiler_version,
                    prompt_hash,
                    spec_hash
                )
                VALUES (
                    :product_id,
                    :spec_version_id,
                    :status,
                    'manual',
                    'tester',
                    '2026-05-17 10:00:00',
                    'legacy decision',
                    'compiler-v1',
                    'prompt-hash',
                    'spec-hash'
                )
                """
            ),
            {
                "product_id": product_id,
                "spec_version_id": spec_version_id,
                "status": status,
            },
        )


def _insert_compiled_authority(
    engine: Engine,
    *,
    authority_id: int,
    spec_version_id: int = 11,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO compiled_spec_authority (
                    authority_id,
                    spec_version_id,
                    compiler_version,
                    prompt_hash,
                    compiled_at,
                    scope_themes,
                    invariants,
                    eligible_feature_ids
                )
                VALUES (
                    :authority_id,
                    :spec_version_id,
                    'compiler-v1',
                    'prompt-hash',
                    '2026-05-17 09:00:00',
                    '[]',
                    '[]',
                    '[]'
                )
                """
            ),
            {"authority_id": authority_id, "spec_version_id": spec_version_id},
        )


def _add_terminal_decision_key_column(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                ALTER TABLE spec_authority_acceptance
                ADD COLUMN terminal_decision_key VARCHAR
                """
            )
        )


def _replace_terminal_index_with_malformed_index(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX uq_spec_authority_terminal_decision_key"))
        conn.execute(
            text(
                """
                CREATE INDEX uq_spec_authority_terminal_decision_key
                ON spec_authority_acceptance (terminal_decision_key)
                """
            )
        )


def _replace_terminal_index_with_narrower_unique_partial_index(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX uq_spec_authority_terminal_decision_key"))
        conn.execute(
            text(
                """
                CREATE UNIQUE INDEX uq_spec_authority_terminal_decision_key
                ON spec_authority_acceptance (terminal_decision_key)
                WHERE terminal_decision_key IS NOT NULL AND status = 'accepted'
                """
            )
        )


def test_authority_decision_migration_adds_provenance_columns(
    tmp_path: Path,
) -> None:
    """Add authority review provenance columns and the terminal unique index."""
    engine = _engine(tmp_path)
    _create_legacy_acceptance_table(engine)
    _create_minimal_compiled_authority_table(engine)

    migrate_spec_authority_tables(engine)

    columns = {
        column["name"]
        for column in inspect(engine).get_columns("spec_authority_acceptance")
    }
    with engine.connect() as conn:
        indexes = {
            row._mapping["name"]
            for row in conn.execute(
                text("PRAGMA index_list('spec_authority_acceptance')")
            )
        }

    assert columns >= NEW_AUTHORITY_DECISION_COLUMNS
    assert "uq_spec_authority_terminal_decision_key" in indexes


def test_authority_decision_migration_backfills_unambiguous_legacy_acceptance(
    tmp_path: Path,
) -> None:
    """Attach legacy terminal decisions to the one matching compiled authority."""
    engine = _engine(tmp_path)
    _create_legacy_acceptance_table(engine)
    _create_minimal_compiled_authority_table(engine)
    _insert_legacy_acceptance(
        engine,
        product_id=LEGACY_PRODUCT_ID,
        spec_version_id=LEGACY_SPEC_VERSION_ID,
    )
    _insert_compiled_authority(
        engine,
        authority_id=LEGACY_AUTHORITY_ID,
        spec_version_id=LEGACY_SPEC_VERSION_ID,
    )

    migrate_spec_authority_tables(engine)

    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT pending_authority_id,
                       terminal_decision_key,
                       provenance_source
                FROM spec_authority_acceptance
                """
            )
        ).one()

    assert row.pending_authority_id == LEGACY_AUTHORITY_ID
    assert row.terminal_decision_key == "7:11:13"
    assert row.provenance_source == "legacy_backfill"


def test_authority_decision_migration_blocks_ambiguous_legacy_acceptance(
    tmp_path: Path,
) -> None:
    """Reject legacy terminal decisions that cannot map to one authority row."""
    engine = _engine(tmp_path)
    _create_legacy_acceptance_table(engine)
    _create_minimal_compiled_authority_table(engine)
    _insert_legacy_acceptance(
        engine,
        product_id=LEGACY_PRODUCT_ID,
        spec_version_id=LEGACY_SPEC_VERSION_ID,
    )
    _insert_compiled_authority(
        engine,
        authority_id=LEGACY_AUTHORITY_ID,
        spec_version_id=LEGACY_SPEC_VERSION_ID,
    )
    _insert_compiled_authority(
        engine,
        authority_id=17,
        spec_version_id=LEGACY_SPEC_VERSION_ID,
    )

    with pytest.raises(RuntimeError, match="Ambiguous legacy authority decision"):
        migrate_spec_authority_tables(engine)


def test_authority_decision_migration_blocks_unmatched_legacy_acceptance(
    tmp_path: Path,
) -> None:
    """Reject legacy terminal decisions with no matching authority row."""
    engine = _engine(tmp_path)
    _create_legacy_acceptance_table(engine)
    _create_minimal_compiled_authority_table(engine)
    _insert_legacy_acceptance(
        engine,
        product_id=LEGACY_PRODUCT_ID,
        spec_version_id=LEGACY_SPEC_VERSION_ID,
    )

    with pytest.raises(RuntimeError, match="Ambiguous legacy authority decision"):
        migrate_spec_authority_tables(engine)


def test_authority_decision_migration_blocks_duplicate_legacy_terminal_keys(
    tmp_path: Path,
) -> None:
    """Reject duplicate generated terminal keys before any legacy backfill writes."""
    engine = _engine(tmp_path)
    _create_legacy_acceptance_table(engine)
    _create_minimal_compiled_authority_table(engine)
    _insert_legacy_acceptance(
        engine,
        product_id=LEGACY_PRODUCT_ID,
        spec_version_id=LEGACY_SPEC_VERSION_ID,
        status="accepted",
    )
    _insert_legacy_acceptance(
        engine,
        product_id=LEGACY_PRODUCT_ID,
        spec_version_id=LEGACY_SPEC_VERSION_ID,
        status="rejected",
    )
    _insert_compiled_authority(
        engine,
        authority_id=LEGACY_AUTHORITY_ID,
        spec_version_id=LEGACY_SPEC_VERSION_ID,
    )

    with pytest.raises(
        RuntimeError,
        match="Duplicate legacy authority terminal decision",
    ):
        migrate_spec_authority_tables(engine)

    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                SELECT pending_authority_id,
                       terminal_decision_key,
                       provenance_source
                FROM spec_authority_acceptance
                ORDER BY id
                """
                )
            )
            .mappings()
            .all()
        )

    assert rows
    assert all(row["pending_authority_id"] is None for row in rows)
    assert all(row["terminal_decision_key"] is None for row in rows)
    assert all(row["provenance_source"] != "legacy_backfill" for row in rows)


def test_terminal_decision_unique_key_blocks_duplicate_accept_reject_rows(
    tmp_path: Path,
) -> None:
    """Block duplicate terminal decisions for the same pending authority."""
    engine = _engine(tmp_path)
    _create_legacy_acceptance_table(engine)
    _create_minimal_compiled_authority_table(engine)

    migrate_spec_authority_tables(engine)

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO spec_authority_acceptance (
                    product_id,
                    spec_version_id,
                    status,
                    policy,
                    decided_by,
                    decided_at,
                    compiler_version,
                    prompt_hash,
                    spec_hash,
                    pending_authority_id,
                    terminal_decision_key
                )
                VALUES (
                    7,
                    11,
                    'accepted',
                    'manual',
                    'tester',
                    '2026-05-17 10:00:00',
                    'compiler-v1',
                    'prompt-hash',
                    'spec-hash',
                    13,
                    '7:11:13'
                )
                """
            )
        )

    with (
        pytest.raises(IntegrityError, match="UNIQUE constraint failed"),
        engine.begin() as conn,
    ):
        conn.execute(
            text(
                """
                INSERT INTO spec_authority_acceptance (
                    product_id,
                    spec_version_id,
                    status,
                    policy,
                    decided_by,
                    decided_at,
                    compiler_version,
                    prompt_hash,
                    spec_hash,
                    pending_authority_id,
                    terminal_decision_key
                )
                VALUES (
                    7,
                    11,
                    'rejected',
                    'manual',
                    'tester',
                    '2026-05-17 10:01:00',
                    'compiler-v1',
                    'prompt-hash',
                    'spec-hash',
                    13,
                    '7:11:13'
                )
                """
            )
        )


def test_authority_decision_migration_rejects_malformed_terminal_index(
    tmp_path: Path,
) -> None:
    """Fail migration when the canonical index name has the wrong contract."""
    engine = _engine(tmp_path)
    _create_legacy_acceptance_table(engine)
    _create_minimal_compiled_authority_table(engine)
    _add_terminal_decision_key_column(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE INDEX uq_spec_authority_terminal_decision_key
                ON spec_authority_acceptance (terminal_decision_key)
                """
            )
        )

    with pytest.raises(RuntimeError, match="Malformed terminal decision index"):
        migrate_spec_authority_tables(engine)


def test_authority_decision_migration_rejects_narrower_terminal_index(
    tmp_path: Path,
) -> None:
    """Fail migration when the same-name partial index has extra predicates."""
    engine = _engine(tmp_path)
    _create_legacy_acceptance_table(engine)
    _create_minimal_compiled_authority_table(engine)
    migrate_spec_authority_tables(engine)
    _replace_terminal_index_with_narrower_unique_partial_index(engine)

    with pytest.raises(RuntimeError, match="Malformed terminal decision index"):
        migrate_spec_authority_tables(engine)


def test_schema_readiness_requires_terminal_decision_invariant(
    tmp_path: Path,
) -> None:
    """Require authority decision columns, terminal index, and storage version."""
    engine = _engine(tmp_path)
    _create_legacy_acceptance_table(engine)
    _create_minimal_compiled_authority_table(engine)
    migrate_agent_workbench_contract_tables(engine)

    requirements = schema_readiness.AUTHORITY_DECISION_REQUIREMENTS
    before = schema_readiness.check_schema_readiness(engine, requirements)
    assert before.ok is False
    assert "spec_authority_acceptance" in before.missing

    migrate_spec_authority_tables(engine)
    migrate_agent_workbench_contract_tables(engine)

    after = schema_readiness.check_schema_readiness(engine, requirements)
    assert after.ok is True
    assert after.missing == {}


def test_check_authority_decision_readiness_public_helper(
    tmp_path: Path,
) -> None:
    """Expose decision storage readiness for future authority write services."""
    engine = _engine(tmp_path)
    _create_legacy_acceptance_table(engine)
    _create_minimal_compiled_authority_table(engine)
    migrate_agent_workbench_contract_tables(engine)

    before = schema_readiness.check_authority_decision_readiness(engine)
    assert before.ok is False
    assert "spec_authority_acceptance" in before.missing

    migrate_spec_authority_tables(engine)
    migrate_agent_workbench_contract_tables(engine)

    after = schema_readiness.check_authority_decision_readiness(engine)
    assert after.ok is True
    assert after.missing == {}


def test_schema_readiness_rejects_malformed_terminal_decision_index(
    tmp_path: Path,
) -> None:
    """Report not-ready when the terminal index name has the wrong contract."""
    engine = _engine(tmp_path)
    _create_legacy_acceptance_table(engine)
    _create_minimal_compiled_authority_table(engine)
    migrate_spec_authority_tables(engine)
    migrate_agent_workbench_contract_tables(engine)
    _replace_terminal_index_with_malformed_index(engine)

    result = schema_readiness.check_schema_readiness(
        engine,
        schema_readiness.AUTHORITY_DECISION_REQUIREMENTS,
    )

    assert result.ok is False
    assert result.missing == {
        "spec_authority_acceptance": ["uq_spec_authority_terminal_decision_key"]
    }


def test_schema_readiness_rejects_narrower_terminal_decision_index(
    tmp_path: Path,
) -> None:
    """Report not-ready when the same-name partial index has extra predicates."""
    engine = _engine(tmp_path)
    _create_legacy_acceptance_table(engine)
    _create_minimal_compiled_authority_table(engine)
    migrate_spec_authority_tables(engine)
    migrate_agent_workbench_contract_tables(engine)
    _replace_terminal_index_with_narrower_unique_partial_index(engine)

    result = schema_readiness.check_schema_readiness(
        engine,
        schema_readiness.AUTHORITY_DECISION_REQUIREMENTS,
    )

    assert result.ok is False
    assert result.missing == {
        "spec_authority_acceptance": ["uq_spec_authority_terminal_decision_key"]
    }
