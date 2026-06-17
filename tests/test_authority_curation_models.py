"""Tests for authority curation persistence models."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel

from db.migrations import ensure_schema_current
from models.authority_curation import (
    AuthorityCurationAttempt,
    AuthorityFeedbackAttempt,
)
from models.core import Product
from services.agent_workbench.schema_readiness import (
    check_authority_curation_readiness,
)
from tests.typing_helpers import require_id

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def test_authority_curation_tables_are_created(engine: Engine) -> None:
    """Authority curation attempts must have dedicated tables."""
    ensure_schema_current(engine)

    table_names = set(inspect(engine).get_table_names())

    assert "authority_feedback_attempts" in table_names
    assert "authority_curation_attempts" in table_names


def test_authority_curation_schema_readiness_passes_after_migration(
    engine: Engine,
) -> None:
    """Readiness check must accept migrated curation storage."""
    ensure_schema_current(engine)

    result = check_authority_curation_readiness(engine)

    assert result.ok is True
    assert result.missing == {}


def test_authority_curation_create_all_defaults_match_migration(
    engine: Engine,
) -> None:
    """Metadata-created curation tables must retain migration-level defaults."""
    SQLModel.metadata.create_all(engine)
    ensure_schema_current(engine)

    feedback_defaults = _column_defaults(engine, "authority_feedback_attempts")
    assert feedback_defaults["status"] == "'recorded'"
    assert feedback_defaults["has_blocking_feedback"] == "0"
    assert feedback_defaults["changed_by"] == "'cli-agent'"

    curation_defaults = _column_defaults(engine, "authority_curation_attempts")
    assert curation_defaults["status"] == "'running'"
    assert curation_defaults["max_iterations"] == "2"
    assert curation_defaults["iteration_count"] == "0"
    assert curation_defaults["request_json"] == "'{}'"
    assert curation_defaults["candidate_lineage_json"] == "'{}'"
    assert curation_defaults["diff_summary_json"] == "'{}'"
    assert curation_defaults["lineage_json"] == "'{}'"
    assert curation_defaults["quality_report_json"] == "'{}'"
    assert curation_defaults["contract_version"] == "'authority_curation.v1'"
    assert curation_defaults["rejected_selection_json"] == "'{}'"
    assert curation_defaults["overlay_json"] == "'{}'"
    assert curation_defaults["changed_by"] == "'cli-agent'"


def test_authority_curation_attempt_v2_columns_exist(engine: Engine) -> None:
    """Curation attempts must persist v2 repair-menu metadata."""
    SQLModel.metadata.create_all(engine)

    inspector = inspect(engine)
    columns = {
        column["name"]
        for column in inspector.get_columns("authority_curation_attempts")
    }

    assert "contract_version" in columns
    assert "menu_fingerprint" in columns
    assert "selection_fingerprint" in columns
    assert "rejected_selection_json" in columns
    assert "overlay_json" in columns


def test_authority_feedback_idempotency_key_is_unique_per_project(
    engine: Engine,
) -> None:
    """Feedback attempts must durably guard idempotency replay keys."""
    ensure_schema_current(engine)
    project_id = _seed_product(engine)

    with Session(engine) as session:
        session.add(
            _feedback_attempt(
                project_id=project_id,
                feedback_attempt_id="feedback-a",
                idempotency_key="same-key",
            )
        )
        session.add(
            _feedback_attempt(
                project_id=project_id,
                feedback_attempt_id="feedback-b",
                idempotency_key="same-key",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_authority_curation_idempotency_key_is_unique_per_project(
    engine: Engine,
) -> None:
    """Curation attempts must durably guard idempotency replay keys."""
    ensure_schema_current(engine)
    project_id = _seed_product(engine)

    with Session(engine) as session:
        session.add(
            _curation_attempt(
                project_id=project_id,
                curation_attempt_id="curation-a",
                idempotency_key="same-key",
            )
        )
        session.add(
            _curation_attempt(
                project_id=project_id,
                curation_attempt_id="curation-b",
                idempotency_key="same-key",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_authority_curation_allows_one_running_attempt_per_authority(
    engine: Engine,
) -> None:
    """Only one running curation may exist for one project/source authority."""
    ensure_schema_current(engine)
    project_id = _seed_product(engine)

    with Session(engine) as session:
        session.add(
            _curation_attempt(
                project_id=project_id,
                curation_attempt_id="curation-running-a",
                idempotency_key="running-a",
            )
        )
        session.add(
            _curation_attempt(
                project_id=project_id,
                curation_attempt_id="curation-running-b",
                idempotency_key="running-b",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    with Session(engine) as session:
        failed = _curation_attempt(
            project_id=project_id,
            curation_attempt_id="curation-failed",
            idempotency_key="failed-a",
        )
        failed.status = "failed"
        session.add(failed)
        running = _curation_attempt(
            project_id=project_id,
            curation_attempt_id="curation-running-c",
            idempotency_key="running-c",
        )
        session.add(running)
        session.commit()


def _column_defaults(engine: Engine, table_name: str) -> dict[str, str | None]:
    """Return SQLite column defaults keyed by column name."""
    return {
        column["name"]: column["default"]
        for column in inspect(engine).get_columns(table_name)
    }


def _seed_product(engine: Engine) -> int:
    """Create a product for curation persistence tests."""
    with Session(engine) as session:
        product = Product(name="Authority Curation Persistence Product")
        session.add(product)
        session.commit()
        session.refresh(product)
        return require_id(product.product_id, "product_id")


def _feedback_attempt(
    *,
    project_id: int,
    feedback_attempt_id: str,
    idempotency_key: str,
) -> AuthorityFeedbackAttempt:
    """Build a minimal feedback attempt row."""
    return AuthorityFeedbackAttempt(
        project_id=project_id,
        feedback_attempt_id=feedback_attempt_id,
        source_authority_id=1,
        source_authority_fingerprint="sha256:authority",
        feedback_fingerprint="sha256:feedback",
        feedback_json="{}",
        request_hash=f"sha256:{feedback_attempt_id}",
        idempotency_key=idempotency_key,
    )


def _curation_attempt(
    *,
    project_id: int,
    curation_attempt_id: str,
    idempotency_key: str,
) -> AuthorityCurationAttempt:
    """Build a minimal curation attempt row."""
    return AuthorityCurationAttempt(
        project_id=project_id,
        curation_attempt_id=curation_attempt_id,
        source_authority_id=1,
        source_authority_fingerprint="sha256:authority",
        spec_version_id=1,
        feedback_attempt_id="feedback-a",
        request_hash=f"sha256:{curation_attempt_id}",
        idempotency_key=idempotency_key,
    )
