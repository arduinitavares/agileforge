"""Tests for authority regeneration mutations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from sqlmodel import Session, select

from models.agent_workbench import CliMutationLedger
from models.core import Product
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from tests.typing_helpers import require_id

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def _approved_spec_content() -> str:
    return '{"schema_version":"agileforge.spec.v1","items":[]}'


@pytest.fixture
def product_id(session: Session) -> int:
    """Create a product for regeneration tests."""
    product = Product(name="Authority Regenerate Product")
    session.add(product)
    session.commit()
    session.refresh(product)
    return require_id(product.product_id, "product_id")


@pytest.fixture
def approved_spec_version_id(session: Session, product_id: int) -> int:
    """Create an approved spec version for the seeded product."""
    spec = SpecRegistry(
        product_id=product_id,
        spec_hash="sha256:approved-spec",
        content=_approved_spec_content(),
        content_ref="/tmp/spec.json",
        status="approved",
        approved_at=datetime.now(UTC),
        approved_by="test",
        approval_notes="Approved for regenerate tests.",
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)
    return require_id(spec.spec_version_id, "spec_version_id")


@pytest.fixture
def authority_regenerate_runner(engine: Engine) -> Any:
    """Build the authority regenerate runner under test."""
    from services.agent_workbench.authority_regenerate import (  # noqa: PLC0415
        AuthorityRegenerateRunner,
    )

    return AuthorityRegenerateRunner(engine=engine)


def test_regenerate_requires_approved_spec_version(
    authority_regenerate_runner: Any,
    product_id: int,
) -> None:
    """Reject regeneration for a spec version that is missing or unapproved."""
    from services.agent_workbench.authority_regenerate import (  # noqa: PLC0415
        AuthorityRegenerateRequest,
    )

    result = authority_regenerate_runner.regenerate(
        AuthorityRegenerateRequest(
            project_id=product_id,
            spec_version_id=100,
            idempotency_key="regen-unapproved-001",
            changed_by="test",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] in {
        "SPEC_VERSION_NOT_FOUND",
        "AUTHORITY_REVIEW_REQUIRED",
    }


def test_regenerate_dry_run_validates_guards_without_mutation(
    authority_regenerate_runner: Any,
    session: Session,
    product_id: int,
    approved_spec_version_id: int,
) -> None:
    """Dry-run should validate guards without mutating authority or ledger rows."""
    from services.agent_workbench.authority_regenerate import (  # noqa: PLC0415
        AuthorityRegenerateRequest,
    )

    before_ledger_count = len(session.exec(select(CliMutationLedger)).all())
    before_authority_count = len(session.exec(select(CompiledSpecAuthority)).all())

    result = authority_regenerate_runner.regenerate(
        AuthorityRegenerateRequest(
            project_id=product_id,
            spec_version_id=approved_spec_version_id,
            dry_run=True,
            changed_by="test",
        )
    )

    after_ledger_count = len(session.exec(select(CliMutationLedger)).all())
    after_authority_count = len(session.exec(select(CompiledSpecAuthority)).all())

    assert result["ok"] is True
    assert result["data"]["status"] == "dry_run"
    assert result["data"]["would_regenerate"] is True
    assert result["data"].get("mutation_event_id") is None
    assert before_ledger_count == after_ledger_count
    assert before_authority_count == after_authority_count


def test_regenerate_persists_pending_v2_authority_and_does_not_accept(
    authority_regenerate_runner: Any,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    product_id: int,
    approved_spec_version_id: int,
) -> None:
    """Real regenerate should persist pending authority and stop before accept."""
    from services.agent_workbench import authority_regenerate as regenerate_mod  # noqa: PLC0415
    from services.agent_workbench.authority_regenerate import (  # noqa: PLC0415
        AuthorityRegenerateRequest,
    )

    def fake_compile(
        *,
        engine: Engine,
        spec_version_id: int,
        force_recompile: bool | None = None,
        tool_context: object | None = None,
        lease_guard: object | None = None,
        record_progress: object | None = None,
    ) -> dict[str, Any]:
        del tool_context, lease_guard, record_progress
        assert force_recompile is True
        with Session(engine) as compile_session:
            authority = CompiledSpecAuthority(
                spec_version_id=spec_version_id,
                compiler_version="2.0.0",
                prompt_hash="a" * 64,
                compiled_at=datetime.now(UTC),
                compiled_artifact_json=(
                    '{"schema_version":"agileforge.compiled_authority.v2",'
                    '"scope_themes":[],"domain":"operations","invariants":[],'
                    '"eligible_feature_rules":[],"rejected_features":[],"gaps":[],'
                    '"assumptions":[],"source_map":[],"compiler_version":"2.0.0",'
                    '"prompt_hash":"' + ("a" * 64) + '","ir_schema_version":null,'
                    '"ir_provenance":null}'
                ),
                scope_themes="[]",
                invariants="[]",
                eligible_feature_ids="[]",
                rejected_features="[]",
                spec_gaps="[]",
            )
            compile_session.add(authority)
            product = compile_session.get(Product, product_id)
            assert product is not None
            product.compiled_authority_json = authority.compiled_artifact_json
            compile_session.add(product)
            compile_session.commit()
            compile_session.refresh(authority)
            return {
                "success": True,
                "authority_id": require_id(authority.authority_id, "authority_id"),
                "spec_version_id": spec_version_id,
                "compiler_version": "2.0.0",
                "prompt_hash": "a" * 8,
                "cached": False,
            }

    monkeypatch.setattr(
        regenerate_mod,
        "compile_spec_authority_for_version_with_engine",
        fake_compile,
    )

    result = authority_regenerate_runner.regenerate(
        AuthorityRegenerateRequest(
            project_id=product_id,
            spec_version_id=approved_spec_version_id,
            idempotency_key="regen-approved-001",
            changed_by="test",
        )
    )

    ledger_rows = session.exec(select(CliMutationLedger)).all()
    authority_rows = session.exec(select(CompiledSpecAuthority)).all()
    acceptance_rows = session.exec(select(SpecAuthorityAcceptance)).all()

    assert result["ok"] is True
    assert result["data"]["status"] == "authority_pending_review"
    assert result["data"]["compiled_authority_schema_version"] == (
        "agileforge.compiled_authority.v2"
    )
    assert result["data"]["accepted_authority_id"] is None
    assert result["data"]["next_actions"][0]["command"] == "agileforge authority review"
    assert len(ledger_rows) == 1
    assert ledger_rows[0].status == "succeeded"
    assert len(authority_rows) == 1
    assert authority_rows[0].spec_version_id == approved_spec_version_id
    assert authority_rows[0].compiler_version == "2.0.0"
    assert acceptance_rows == []


def test_regenerate_idempotency_replays_completed_mutation(
    authority_regenerate_runner: Any,
    monkeypatch: pytest.MonkeyPatch,
    product_id: int,
    approved_spec_version_id: int,
) -> None:
    """Reusing the same idempotency key should replay the completed response."""
    from services.agent_workbench import authority_regenerate as regenerate_mod  # noqa: PLC0415
    from services.agent_workbench.authority_regenerate import (  # noqa: PLC0415
        AuthorityRegenerateRequest,
    )

    compile_calls: list[tuple[int, bool | None]] = []

    def fake_compile(
        *,
        engine: Engine,
        spec_version_id: int,
        force_recompile: bool | None = None,
        tool_context: object | None = None,
        lease_guard: object | None = None,
        record_progress: object | None = None,
    ) -> dict[str, Any]:
        del tool_context, lease_guard, record_progress
        compile_calls.append((spec_version_id, force_recompile))
        with Session(engine) as compile_session:
            authority = CompiledSpecAuthority(
                spec_version_id=spec_version_id,
                compiler_version="2.0.0",
                prompt_hash="b" * 64,
                compiled_at=datetime.now(UTC),
                compiled_artifact_json=(
                    '{"schema_version":"agileforge.compiled_authority.v2",'
                    '"scope_themes":[],"domain":"operations","invariants":[],'
                    '"eligible_feature_rules":[],"rejected_features":[],"gaps":[],'
                    '"assumptions":[],"source_map":[],"compiler_version":"2.0.0",'
                    '"prompt_hash":"' + ("b" * 64) + '","ir_schema_version":null,'
                    '"ir_provenance":null}'
                ),
                scope_themes="[]",
                invariants="[]",
                eligible_feature_ids="[]",
                rejected_features="[]",
                spec_gaps="[]",
            )
            compile_session.add(authority)
            product = compile_session.get(Product, product_id)
            assert product is not None
            product.compiled_authority_json = authority.compiled_artifact_json
            compile_session.add(product)
            compile_session.commit()
            compile_session.refresh(authority)
            return {
                "success": True,
                "authority_id": require_id(authority.authority_id, "authority_id"),
                "spec_version_id": spec_version_id,
                "compiler_version": "2.0.0",
                "prompt_hash": "b" * 8,
                "cached": False,
            }

    monkeypatch.setattr(
        regenerate_mod,
        "compile_spec_authority_for_version_with_engine",
        fake_compile,
    )

    request = AuthorityRegenerateRequest(
        project_id=product_id,
        spec_version_id=approved_spec_version_id,
        idempotency_key="regen-replay-001",
        changed_by="test",
    )

    first = authority_regenerate_runner.regenerate(request)
    second = authority_regenerate_runner.regenerate(request)

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["data"]["mutation_event_id"] == first["data"]["mutation_event_id"]
    assert compile_calls == [(approved_spec_version_id, True)]
