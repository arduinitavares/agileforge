"""Tests for read-only agent workbench Spec Authority projections."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from sqlalchemy import create_engine, text
from sqlmodel import select

from models.core import Product
from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from services.agent_workbench.authority_projection import AuthorityProjectionService
from services.agent_workbench.error_codes import ErrorCode, error_metadata
from tests.typing_helpers import require_id

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.engine import Engine
    from sqlmodel import Session

SCHEMA_NOT_READY_EXIT_CODE: Final[int] = 5
NOT_FOUND_EXIT_CODE: Final[int] = 4
AUTHORITY_ERROR_EXIT_CODE: Final[int] = 4


def _spec_hash(content: str) -> str:
    """Return the persisted SHA-256 hash for spec content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _engine(session: Session) -> Engine:
    """Return the test session bind as an engine for projection services."""
    return cast("Engine", session.get_bind())


def _seed_product(
    session: Session,
    *,
    spec_file_path: str | None = None,
) -> Product:
    """Persist a product used by authority projection tests."""
    product = Product(
        name="Authority Product",
        description="Product for authority projection tests",
        spec_file_path=spec_file_path,
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


def _seed_spec(
    session: Session,
    *,
    product_id: int,
    content: str,
    content_ref: str | None = None,
) -> SpecRegistry:
    """Persist an approved spec version."""
    spec = SpecRegistry(
        product_id=product_id,
        spec_hash=_spec_hash(content),
        content=content,
        content_ref=content_ref,
        status="approved",
        approved_at=datetime(2026, 5, 14, tzinfo=UTC),
        approved_by="tester",
        approval_notes="approved",
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)
    return spec


def _seed_authority(
    session: Session,
    *,
    spec_version_id: int,
    compiler_version: str = "1.0.0",
    prompt_hash: str = "a" * 64,
    invariants: str = '[{"id":"INV-1","text":"Must stay in scope"}]',
) -> CompiledSpecAuthority:
    """Persist a compiled authority row without accepting it."""
    authority = CompiledSpecAuthority(
        spec_version_id=spec_version_id,
        compiler_version=compiler_version,
        prompt_hash=prompt_hash,
        compiled_at=datetime(2026, 5, 14, 12, tzinfo=UTC),
        compiled_artifact_json=json.dumps(
            {"invariants": [{"id": "INV-1", "text": "Must stay in scope"}]}
        ),
        scope_themes="[]",
        invariants=invariants,
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps="[]",
    )
    session.add(authority)
    session.commit()
    session.refresh(authority)
    return authority


def _accept_spec(
    session: Session,
    *,
    product_id: int,
    spec: SpecRegistry,
    decided_at: datetime | None = None,
) -> SpecAuthorityAcceptance:
    """Persist an accepted authority decision for a spec version."""
    spec_version_id = require_id(spec.spec_version_id, "spec_version_id")
    compiled_authority = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id == spec_version_id
        )
    ).first()
    pending_authority_id = (
        require_id(compiled_authority.authority_id, "authority_id")
        if compiled_authority is not None
        else spec_version_id
    )
    acceptance = SpecAuthorityAcceptance(
        product_id=product_id,
        spec_version_id=spec_version_id,
        status="accepted",
        policy="manual",
        decided_by="reviewer",
        decided_at=decided_at or datetime(2026, 5, 14, 13, tzinfo=UTC),
        rationale="Accepted for test",
        compiler_version="1.0.0",
        prompt_hash="a" * 64,
        spec_hash=spec.spec_hash,
        pending_authority_id=pending_authority_id,
        actor_mode="human_review_token",
        review_completeness="complete",
        terminal_decision_key=(
            f"{product_id}:{spec_version_id}:{pending_authority_id}"
        ),
        provenance_source="test",
    )
    session.add(acceptance)
    session.commit()
    session.refresh(acceptance)
    return acceptance


def test_authority_status_reports_schema_not_ready_without_creating_database(
    tmp_path: Path,
) -> None:
    """Report missing schema without creating or migrating a SQLite database."""
    db_path = tmp_path / "missing.sqlite3"
    service = AuthorityProjectionService(
        engine=create_engine(f"sqlite:///{db_path.as_posix()}"),
        repo_root=tmp_path,
    )

    result = service.status(project_id=1)

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "SCHEMA_NOT_READY"
    assert result["errors"][0]["exit_code"] == SCHEMA_NOT_READY_EXIT_CODE
    assert result["errors"][0]["retryable"] is True
    assert "products" in result["errors"][0]["details"]["missing"]
    assert not db_path.exists()


def test_authority_status_reports_missing_project(
    session: Session,
    tmp_path: Path,
) -> None:
    """Return a structured CLI usage error when the project is unknown."""
    service = AuthorityProjectionService(engine=_engine(session), repo_root=tmp_path)

    result = service.status(project_id=404)

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "PROJECT_NOT_FOUND"
    assert result["errors"][0]["exit_code"] == NOT_FOUND_EXIT_CODE
    assert result["errors"][0]["retryable"] is False
    assert result["errors"][0]["details"] == {"project_id": 404}


def test_authority_status_distinguishes_missing_authority_without_specs(
    session: Session,
    tmp_path: Path,
) -> None:
    """Report missing authority when the project has no spec versions."""
    product = _seed_product(session)
    product_id = require_id(product.product_id, "product_id")
    service = AuthorityProjectionService(engine=_engine(session), repo_root=tmp_path)

    result = service.status(project_id=product_id)

    assert result["ok"] is True
    assert result["data"]["status"] == "missing"
    assert result["data"]["reason"] == "no_spec_versions"
    assert result["data"]["latest_spec_version_id"] is None
    assert result["data"]["accepted_spec_version_id"] is None
    assert result["data"]["authority_fingerprint"] is None


def test_authority_status_keeps_compiled_but_unaccepted_authority_pending(
    session: Session,
    tmp_path: Path,
) -> None:
    """Do not treat compilation alone as an accepted authority decision."""
    product = _seed_product(session)
    product_id = require_id(product.product_id, "product_id")
    spec = _seed_spec(session, product_id=product_id, content="# Spec\n")
    authority = _seed_authority(
        session,
        spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
    )
    service = AuthorityProjectionService(engine=_engine(session), repo_root=tmp_path)

    result = service.status(project_id=product_id)

    assert result["ok"] is True
    assert result["data"]["status"] == "pending_acceptance"
    assert result["data"]["reason"] == "spec_versions_without_accepted_authority"
    assert result["data"]["latest_spec_version_id"] == spec.spec_version_id
    assert result["data"]["accepted_spec_version_id"] is None
    assert result["data"]["authority_id"] is None
    assert result["data"]["pending_authority_id"] == authority.authority_id
    assert (
        result["data"]["pending_compiled_spec_version_id"]
        == spec.spec_version_id
    )
    assert result["data"]["pending_compiled_at"] == "2026-05-14T12:00:00Z"
    assert result["data"]["pending_compiler_version"] == "1.0.0"
    assert result["data"]["pending_prompt_hash"] == "a" * 64
    assert result["data"]["pending_invariant_count"] == 1
    assert result["data"]["pending_authority_fingerprint"].startswith("sha256:")


def test_authority_status_reports_current_accepted_authority_from_repo_root(
    session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return current authority when latest, accepted, compiled, and disk match."""
    unrelated_cwd = tmp_path / "elsewhere"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)
    spec_content = "# Spec\n"
    spec_path = tmp_path / "specs" / "app.md"
    spec_path.parent.mkdir()
    spec_path.write_text(spec_content, encoding="utf-8")
    product = _seed_product(session, spec_file_path="specs/app.md")
    product_id = require_id(product.product_id, "product_id")
    spec = _seed_spec(
        session,
        product_id=product_id,
        content=spec_content,
        content_ref="specs/app.md",
    )
    authority = _seed_authority(
        session,
        spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
    )
    acceptance = _accept_spec(session, product_id=product_id, spec=spec)
    service = AuthorityProjectionService(engine=_engine(session), repo_root=tmp_path)

    result = service.status(project_id=product_id)

    assert result["ok"] is True
    assert result["data"]["status"] == "current"
    assert result["data"]["accepted_decision_id"] == acceptance.id
    assert result["data"]["accepted_spec_version_id"] == spec.spec_version_id
    assert result["data"]["spec_hash"] == spec.spec_hash
    assert result["data"]["stale_reason"] is None
    assert result["data"]["authority_id"] == authority.authority_id
    assert result["data"]["pending_authority_id"] is None
    assert result["data"]["pending_authority_fingerprint"] is None
    assert result["data"]["invariant_count"] == 1
    assert result["data"]["disk_spec"]["resolved_path"] == str(spec_path.resolve())
    assert result["data"]["disk_spec"]["sha256"] == spec.spec_hash
    assert result["data"]["disk_spec"]["matches_accepted"] is True
    assert result["data"]["authority_fingerprint"].startswith("sha256:")


def test_authority_status_uses_latest_accepted_decision(
    session: Session,
    tmp_path: Path,
) -> None:
    """Select the latest accepted decision, not an older accepted version."""
    product = _seed_product(session)
    product_id = require_id(product.product_id, "product_id")
    older_spec = _seed_spec(session, product_id=product_id, content="older")
    newer_spec = _seed_spec(session, product_id=product_id, content="newer")
    _seed_authority(
        session,
        spec_version_id=require_id(older_spec.spec_version_id, "spec_version_id"),
    )
    newer_authority = _seed_authority(
        session,
        spec_version_id=require_id(newer_spec.spec_version_id, "spec_version_id"),
    )
    _accept_spec(
        session,
        product_id=product_id,
        spec=older_spec,
        decided_at=datetime(2026, 5, 14, 12, tzinfo=UTC),
    )
    newer_acceptance = _accept_spec(
        session,
        product_id=product_id,
        spec=newer_spec,
        decided_at=datetime(2026, 5, 14, 12, tzinfo=UTC) + timedelta(minutes=1),
    )
    service = AuthorityProjectionService(engine=_engine(session), repo_root=tmp_path)

    result = service.status(project_id=product_id)

    assert result["ok"] is True
    assert result["data"]["status"] == "current"
    assert result["data"]["accepted_decision_id"] == newer_acceptance.id
    assert result["data"]["accepted_spec_version_id"] == newer_spec.spec_version_id
    assert result["data"]["authority_id"] == newer_authority.authority_id


def test_authority_status_marks_compiler_prompt_mismatch_stale(
    session: Session,
    tmp_path: Path,
) -> None:
    """Reject compiled rows whose provenance differs from acceptance."""
    product = _seed_product(session)
    product_id = require_id(product.product_id, "product_id")
    spec = _seed_spec(session, product_id=product_id, content="# Spec\n")
    _seed_authority(
        session,
        spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
        compiler_version="2.0.0",
    )
    _accept_spec(
        session,
        product_id=product_id,
        spec=spec,
    )
    service = AuthorityProjectionService(engine=_engine(session), repo_root=tmp_path)

    result = service.status(project_id=product_id)

    assert result["ok"] is True
    assert result["data"]["status"] == "stale"
    assert result["data"]["reason"] == "accepted_compiler_prompt_mismatch"
    assert result["data"]["stale_reason"] == "accepted_compiler_prompt_mismatch"


def test_authority_status_marks_latest_spec_hash_drift_stale(
    session: Session,
    tmp_path: Path,
) -> None:
    """Mark accepted authority stale when a newer spec hash exists."""
    product = _seed_product(session)
    product_id = require_id(product.product_id, "product_id")
    accepted_spec = _seed_spec(session, product_id=product_id, content="accepted")
    latest_spec = _seed_spec(session, product_id=product_id, content="latest")
    _seed_authority(
        session,
        spec_version_id=require_id(accepted_spec.spec_version_id, "spec_version_id"),
    )
    _accept_spec(session, product_id=product_id, spec=accepted_spec)
    service = AuthorityProjectionService(engine=_engine(session), repo_root=tmp_path)

    result = service.status(project_id=product_id)

    assert result["ok"] is True
    assert result["data"]["status"] == "stale"
    assert result["data"]["reason"] == "latest_spec_hash_mismatch"
    assert result["data"]["stale_reason"] == "latest_spec_hash_mismatch"
    assert result["data"]["spec_hash"] == accepted_spec.spec_hash
    assert result["data"]["latest_spec_version_id"] == latest_spec.spec_version_id
    assert result["data"]["latest_spec_hash"] == latest_spec.spec_hash
    assert result["data"]["accepted_spec_hash"] == accepted_spec.spec_hash


def test_authority_status_marks_missing_accepted_spec_stale_before_latest_drift(
    session: Session,
    tmp_path: Path,
) -> None:
    """Classify a dangling accepted spec reference before latest spec drift."""
    product = _seed_product(session)
    product_id = require_id(product.product_id, "product_id")
    accepted_spec = _seed_spec(session, product_id=product_id, content="accepted")
    latest_spec = _seed_spec(session, product_id=product_id, content="latest")
    accepted_spec_id = require_id(
        accepted_spec.spec_version_id,
        "spec_version_id",
    )
    _seed_authority(session, spec_version_id=accepted_spec_id)
    _accept_spec(session, product_id=product_id, spec=accepted_spec)
    session.exec(cast("Any", text("PRAGMA foreign_keys=OFF")))
    session.exec(
        cast(
            "Any",
            text("DELETE FROM spec_registry WHERE spec_version_id = :spec_version_id"),
        ),
        params={"spec_version_id": accepted_spec_id},
    )
    session.commit()
    service = AuthorityProjectionService(engine=_engine(session), repo_root=tmp_path)

    result = service.status(project_id=product_id)

    assert result["ok"] is True
    assert result["data"]["status"] == "stale"
    assert result["data"]["reason"] == "accepted_spec_missing"
    assert result["data"]["stale_reason"] == "accepted_spec_missing"
    assert result["data"]["latest_spec_version_id"] == latest_spec.spec_version_id


def test_authority_status_marks_missing_accepted_spec_stale_without_authority(
    session: Session,
    tmp_path: Path,
) -> None:
    """Classify missing accepted spec before missing compiled authority."""
    product = _seed_product(session)
    product_id = require_id(product.product_id, "product_id")
    accepted_spec = _seed_spec(session, product_id=product_id, content="accepted")
    latest_spec = _seed_spec(session, product_id=product_id, content="latest")
    accepted_spec_id = require_id(
        accepted_spec.spec_version_id,
        "spec_version_id",
    )
    _accept_spec(session, product_id=product_id, spec=accepted_spec)
    session.exec(cast("Any", text("PRAGMA foreign_keys=OFF")))
    session.exec(
        cast(
            "Any",
            text("DELETE FROM spec_registry WHERE spec_version_id = :spec_version_id"),
        ),
        params={"spec_version_id": accepted_spec_id},
    )
    session.commit()
    service = AuthorityProjectionService(engine=_engine(session), repo_root=tmp_path)

    result = service.status(project_id=product_id)

    assert result["ok"] is True
    assert result["data"]["status"] == "stale"
    assert result["data"]["reason"] == "accepted_spec_missing"
    assert result["data"]["stale_reason"] == "accepted_spec_missing"
    assert result["data"]["latest_spec_version_id"] == latest_spec.spec_version_id


def test_authority_status_marks_disk_spec_hash_drift_stale(
    session: Session,
    tmp_path: Path,
) -> None:
    """Mark accepted authority stale when the repo-root spec file drifts."""
    accepted_content = "# Accepted\n"
    spec_path = tmp_path / "specs" / "app.md"
    spec_path.parent.mkdir()
    spec_path.write_text("# Changed\n", encoding="utf-8")
    product = _seed_product(session, spec_file_path="specs/app.md")
    product_id = require_id(product.product_id, "product_id")
    spec = _seed_spec(session, product_id=product_id, content=accepted_content)
    _seed_authority(
        session,
        spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
    )
    _accept_spec(session, product_id=product_id, spec=spec)
    service = AuthorityProjectionService(engine=_engine(session), repo_root=tmp_path)

    result = service.status(project_id=product_id)

    assert result["ok"] is True
    assert result["data"]["status"] == "stale"
    assert result["data"]["reason"] == "disk_spec_hash_mismatch"
    assert result["data"]["disk_spec"]["sha256"] == _spec_hash("# Changed\n")
    assert result["data"]["disk_spec"]["matches_accepted"] is False


def test_authority_status_fingerprint_changes_on_latest_spec_drift(
    session: Session,
    tmp_path: Path,
) -> None:
    """Include latest spec status inputs in the authority fingerprint."""
    product = _seed_product(session)
    product_id = require_id(product.product_id, "product_id")
    accepted_spec = _seed_spec(session, product_id=product_id, content="accepted")
    _seed_authority(
        session,
        spec_version_id=require_id(accepted_spec.spec_version_id, "spec_version_id"),
    )
    _accept_spec(session, product_id=product_id, spec=accepted_spec)
    service = AuthorityProjectionService(engine=_engine(session), repo_root=tmp_path)
    current_result = service.status(project_id=product_id)

    _seed_spec(session, product_id=product_id, content="latest")
    drift_result = service.status(project_id=product_id)

    assert current_result["data"]["status"] == "current"
    assert drift_result["data"]["status"] == "stale"
    assert drift_result["data"]["stale_reason"] == "latest_spec_hash_mismatch"
    assert (
        current_result["data"]["authority_fingerprint"]
        != drift_result["data"]["authority_fingerprint"]
    )


def test_authority_status_fingerprint_changes_on_disk_spec_drift(
    session: Session,
    tmp_path: Path,
) -> None:
    """Include disk spec hash state in the authority fingerprint."""
    accepted_content = "# Accepted\n"
    spec_path = tmp_path / "specs" / "app.md"
    spec_path.parent.mkdir()
    spec_path.write_text(accepted_content, encoding="utf-8")
    product = _seed_product(session, spec_file_path="specs/app.md")
    product_id = require_id(product.product_id, "product_id")
    spec = _seed_spec(session, product_id=product_id, content=accepted_content)
    _seed_authority(
        session,
        spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
    )
    _accept_spec(session, product_id=product_id, spec=spec)
    service = AuthorityProjectionService(engine=_engine(session), repo_root=tmp_path)
    current_result = service.status(project_id=product_id)

    spec_path.write_text("# Changed\n", encoding="utf-8")
    drift_result = service.status(project_id=product_id)

    assert current_result["data"]["status"] == "current"
    assert drift_result["data"]["status"] == "stale"
    assert drift_result["data"]["stale_reason"] == "disk_spec_hash_mismatch"
    assert (
        current_result["data"]["authority_fingerprint"]
        != drift_result["data"]["authority_fingerprint"]
    )


def test_authority_status_marks_missing_disk_spec_stale_with_warning(
    session: Session,
    tmp_path: Path,
) -> None:
    """Do not report current when the stored disk spec path is missing."""
    product = _seed_product(session, spec_file_path="specs/missing.md")
    product_id = require_id(product.product_id, "product_id")
    spec = _seed_spec(session, product_id=product_id, content="# Spec\n")
    _seed_authority(
        session,
        spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
    )
    _accept_spec(session, product_id=product_id, spec=spec)
    service = AuthorityProjectionService(engine=_engine(session), repo_root=tmp_path)

    result = service.status(project_id=product_id)

    assert result["ok"] is True
    assert result["data"]["status"] == "stale"
    assert result["data"]["reason"] == "disk_spec_missing"
    assert result["data"]["stale_reason"] == "disk_spec_missing"
    assert result["data"]["disk_spec"]["exists"] is False
    assert result["warnings"][0]["code"] == "DISK_SPEC_MISSING"
    assert result["warnings"][0]["details"]["resolved_path"] == str(
        (tmp_path / "specs" / "missing.md").resolve()
    )


def test_authority_status_marks_unreadable_disk_spec_existing_with_warning(
    session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report unreadable disk specs as existing without a usable hash."""
    spec_content = "# Accepted\n"
    spec_path = tmp_path / "specs" / "app.md"
    spec_path.parent.mkdir()
    spec_path.write_text(spec_content, encoding="utf-8")
    resolved_spec_path = spec_path.resolve()
    product = _seed_product(session, spec_file_path="specs/app.md")
    product_id = require_id(product.product_id, "product_id")
    spec = _seed_spec(session, product_id=product_id, content=spec_content)
    _seed_authority(
        session,
        spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
    )
    _accept_spec(session, product_id=product_id, spec=spec)
    original_read_bytes = Path.read_bytes

    def fake_read_bytes(path: Path) -> bytes:
        if path == resolved_spec_path:
            message = "permission denied"
            raise OSError(message)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)
    service = AuthorityProjectionService(engine=_engine(session), repo_root=tmp_path)

    result = service.status(project_id=product_id)

    assert result["ok"] is True
    assert result["data"]["status"] == "stale"
    assert result["data"]["reason"] == "disk_spec_unreadable"
    assert result["data"]["disk_spec"]["status"] == "unreadable"
    assert result["data"]["disk_spec"]["exists"] is True
    assert result["data"]["disk_spec"]["sha256"] is None
    assert result["data"]["disk_spec"]["matches_accepted"] is None
    assert result["warnings"][0]["code"] == "DISK_SPEC_UNREADABLE"


def test_invariants_requires_accepted_authority_by_default(
    session: Session,
    tmp_path: Path,
) -> None:
    """Do not choose arbitrary compiled authority without acceptance."""
    product = _seed_product(session)
    product_id = require_id(product.product_id, "product_id")
    spec = _seed_spec(session, product_id=product_id, content="# Spec\n")
    _seed_authority(
        session,
        spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
    )
    service = AuthorityProjectionService(engine=_engine(session), repo_root=tmp_path)

    result = service.invariants(project_id=product_id)

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_NOT_ACCEPTED"


def test_invariants_default_rejects_unaccepted_recompile(
    session: Session,
    tmp_path: Path,
) -> None:
    """Do not expose mismatched recompile output as accepted invariants."""
    product = _seed_product(session)
    product_id = require_id(product.product_id, "product_id")
    spec = _seed_spec(session, product_id=product_id, content="# Spec\n")
    _seed_authority(
        session,
        spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
        compiler_version="2.0.0",
        prompt_hash="b" * 64,
    )
    _accept_spec(session, product_id=product_id, spec=spec)
    service = AuthorityProjectionService(engine=_engine(session), repo_root=tmp_path)

    result = service.invariants(project_id=product_id)

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_ACCEPTANCE_MISMATCH"
    assert result["errors"][0]["exit_code"] == AUTHORITY_ERROR_EXIT_CODE
    assert result["errors"][0]["details"] == {
        "project_id": product_id,
        "spec_version_id": spec.spec_version_id,
        "accepted_compiler_version": "1.0.0",
        "accepted_prompt_hash": "a" * 64,
        "compiled_compiler_version": "2.0.0",
        "compiled_prompt_hash": "b" * 64,
    }


def test_invariants_returns_explicit_compiled_authority_without_acceptance(
    session: Session,
    tmp_path: Path,
) -> None:
    """Return explicit compiled invariants even when no accepted decision exists."""
    product = _seed_product(session)
    product_id = require_id(product.product_id, "product_id")
    spec = _seed_spec(session, product_id=product_id, content="# Spec\n")
    authority = _seed_authority(
        session,
        spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
    )
    service = AuthorityProjectionService(engine=_engine(session), repo_root=tmp_path)

    result = service.invariants(
        project_id=product_id,
        spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
    )

    assert result["ok"] is True
    assert result["data"]["authority_id"] == authority.authority_id
    assert result["data"]["spec_version_id"] == spec.spec_version_id
    assert result["data"]["count"] == 1
    assert result["data"]["invariants"] == [
        {"id": "INV-1", "text": "Must stay in scope"}
    ]
    assert result["data"]["authority_fingerprint"].startswith("sha256:")


def test_invariants_reports_missing_compiled_authority(
    session: Session,
    tmp_path: Path,
) -> None:
    """Return a structured error when the selected authority was not compiled."""
    product = _seed_product(session)
    product_id = require_id(product.product_id, "product_id")
    spec = _seed_spec(session, product_id=product_id, content="# Spec\n")
    service = AuthorityProjectionService(engine=_engine(session), repo_root=tmp_path)

    result = service.invariants(
        project_id=product_id,
        spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_NOT_COMPILED"
    assert result["errors"][0]["details"] == {
        "project_id": product_id,
        "spec_version_id": spec.spec_version_id,
    }


def test_invariants_reports_missing_spec_version_with_registry_metadata(
    session: Session,
    tmp_path: Path,
) -> None:
    """Use registry defaults when an explicit spec version is unknown."""
    product = _seed_product(session)
    product_id = require_id(product.product_id, "product_id")
    missing_spec_version_id = 999_999
    metadata = error_metadata(ErrorCode.SPEC_VERSION_NOT_FOUND)
    service = AuthorityProjectionService(engine=_engine(session), repo_root=tmp_path)

    result = service.invariants(
        project_id=product_id,
        spec_version_id=missing_spec_version_id,
    )

    error = result["errors"][0]
    assert result["ok"] is False
    assert error["code"] == metadata.code
    assert error["exit_code"] == metadata.default_exit_code
    assert error["retryable"] is metadata.retryable
    assert error["details"] == {
        "project_id": product_id,
        "spec_version_id": missing_spec_version_id,
    }


def test_malformed_invariants_do_not_crash_status_and_error_invariants(
    session: Session,
    tmp_path: Path,
) -> None:
    """Warn in status and return a structured invariants error for bad JSON."""
    product = _seed_product(session)
    product_id = require_id(product.product_id, "product_id")
    spec = _seed_spec(session, product_id=product_id, content="# Spec\n")
    _seed_authority(
        session,
        spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
        invariants="{bad json",
    )
    _accept_spec(session, product_id=product_id, spec=spec)
    service = AuthorityProjectionService(engine=_engine(session), repo_root=tmp_path)

    status_result = service.status(project_id=product_id)
    invariants_result = service.invariants(project_id=product_id)

    assert status_result["ok"] is True
    assert status_result["data"]["invariant_count"] == 0
    assert status_result["warnings"][0]["code"] == "AUTHORITY_INVARIANTS_INVALID"
    assert invariants_result["ok"] is False
    assert invariants_result["errors"][0]["code"] == "AUTHORITY_INVARIANTS_INVALID"
