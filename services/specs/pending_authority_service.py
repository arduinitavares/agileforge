# services/specs/pending_authority_service.py
"""Compile pending project spec authority without accepting it."""

# ruff: noqa: SIM300

from __future__ import annotations

import hashlib
from collections.abc import Callable  # noqa: TC003
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003
from typing import Any, Protocol

from sqlmodel import Session, select

from models.core import Product
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from services.specs.profile_content import (
    SpecContentNormalizationError,
    normalize_spec_content_for_registry,
)

_MAX_SPEC_SIZE_KB = 100
_MAX_SPEC_SIZE_BYTES = _MAX_SPEC_SIZE_KB * 1024
_PENDING_APPROVAL_NOTES = (
    "Required compiler precondition for pending authority generation"
)
_SPEC_PRODUCT_ID: Any = SpecRegistry.product_id
_SPEC_VERSION_ID: Any = SpecRegistry.spec_version_id


class PendingAuthorityCompiler(Protocol):
    """Callable seam used to compile a pending authority artifact."""

    def __call__(
        self,
        *,
        spec_version_id: int,
        force_recompile: bool | None = None,
        tool_context: object | None = None,
        lease_guard: Callable[[str], bool] | None = None,
        record_progress: Callable[[str], bool] | None = None,
    ) -> dict[str, object]:
        """Compile the approved spec version and return compiler metadata."""


@dataclass(frozen=True)
class PendingAuthorityResult:
    """Result for pending authority compilation."""

    ok: bool
    product_id: int
    spec_path: str
    error_code: str | None = None
    spec_hash: str | None = None
    spec_version_id: int | None = None
    authority_id: int | None = None
    compiler_version: str | None = None
    prompt_hash: str | None = None
    error: str | None = None
    reason: str | None = None
    failure_artifact_id: str | None = None
    failure_stage: str | None = None
    failure_summary: str | None = None
    raw_output_preview: str | None = None
    has_full_artifact: bool | None = None
    blocking_gaps: list[str] | None = None


def _result(  # noqa: PLR0913
    *,
    ok: bool,
    product_id: int,
    spec_path: Path | str,
    error_code: str | None = None,
    spec_hash: str | None = None,
    spec_version_id: int | None = None,
    authority_id: int | None = None,
    compiler_version: str | None = None,
    prompt_hash: str | None = None,
    error: str | None = None,
    reason: str | None = None,
    failure_artifact_id: str | None = None,
    failure_stage: str | None = None,
    failure_summary: str | None = None,
    raw_output_preview: str | None = None,
    has_full_artifact: bool | None = None,
    blocking_gaps: list[str] | None = None,
) -> PendingAuthorityResult:
    """Build a pending authority result."""
    return PendingAuthorityResult(
        ok=ok,
        product_id=product_id,
        spec_path=str(spec_path),
        error_code=error_code,
        spec_hash=spec_hash,
        spec_version_id=spec_version_id,
        authority_id=authority_id,
        compiler_version=compiler_version,
        prompt_hash=prompt_hash,
        error=error,
        reason=reason,
        failure_artifact_id=failure_artifact_id,
        failure_stage=failure_stage,
        failure_summary=failure_summary,
        raw_output_preview=raw_output_preview,
        has_full_artifact=has_full_artifact,
        blocking_gaps=blocking_gaps,
    )


def _lease_lost(
    *, product_id: int, spec_path: Path, spec_hash: str | None, boundary: str
) -> PendingAuthorityResult:
    """Return the canonical pending-authority lease-loss result."""
    return _result(
        ok=False,
        product_id=product_id,
        spec_path=spec_path,
        error_code="MUTATION_IN_PROGRESS",
        spec_hash=spec_hash,
        error=f"MUTATION_LEASE_LOST:{boundary}",
    )


def _record_progress_or_error(  # noqa: PLR0913
    *,
    record_progress: Callable[[str], bool],
    product_id: int,
    spec_path: Path,
    spec_hash: str,
    spec_version_id: int | None,
    boundary: str,
) -> PendingAuthorityResult | None:
    """Record progress for a committed boundary and normalize failures."""
    try:
        progress_recorded = record_progress(boundary)
    except Exception as exc:  # noqa: BLE001
        return _result(
            ok=False,
            product_id=product_id,
            spec_path=spec_path,
            error_code="MUTATION_RECOVERY_REQUIRED",
            spec_hash=spec_hash,
            spec_version_id=spec_version_id,
            error=f"MUTATION_PROGRESS_RECORD_FAILED:{boundary}:{exc}",
        )
    if progress_recorded is False:
        return _result(
            ok=False,
            product_id=product_id,
            spec_path=spec_path,
            error_code="MUTATION_RECOVERY_REQUIRED",
            spec_hash=spec_hash,
            spec_version_id=spec_version_id,
            error=f"MUTATION_PROGRESS_RECORD_FAILED:{boundary}",
        )
    return None


def _load_spec_file(
    spec_path: Path,
) -> tuple[Path, str, str] | PendingAuthorityResult:
    """Resolve and read a UTF-8 spec file within the lifecycle size limit."""
    resolved_path = spec_path.resolve()
    if not resolved_path.exists():
        return _result(
            ok=False,
            product_id=0,
            spec_path=resolved_path,
            error_code="SPEC_FILE_NOT_FOUND",
            error=f"Specification file not found: {resolved_path}",
        )
    if resolved_path.stat().st_size > _MAX_SPEC_SIZE_BYTES:
        return _result(
            ok=False,
            product_id=0,
            spec_path=resolved_path,
            error_code="SPEC_FILE_INVALID",
            error=f"Specification file exceeds {_MAX_SPEC_SIZE_KB}KB",
        )
    try:
        spec_content = resolved_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _result(
            ok=False,
            product_id=0,
            spec_path=resolved_path,
            error_code="SPEC_FILE_INVALID",
            error=f"Failed to read specification file: {exc}",
        )
    spec_hash = hashlib.sha256(spec_content.encode("utf-8")).hexdigest()
    return resolved_path, spec_content, spec_hash


def _latest_spec_for_product(
    session: Session, *, product_id: int
) -> SpecRegistry | None:
    """Return the latest spec registry row for the product."""
    return session.exec(
        select(SpecRegistry)
        .where(_SPEC_PRODUCT_ID == product_id)
        .order_by(_SPEC_VERSION_ID.desc())
    ).first()


def _normalize_compiler_failure(
    *,
    product_id: int,
    spec_path: Path,
    spec_hash: str,
    spec_version_id: int,
    compile_result: dict[str, object],
) -> PendingAuthorityResult:
    """Map compiler failures to the pending-authority result contract."""
    error_code = compile_result.get("error_code")
    if error_code in {"MUTATION_IN_PROGRESS", "MUTATION_RECOVERY_REQUIRED"}:
        error = str(compile_result.get("error", error_code))
        boundary = compile_result.get("boundary")
        if boundary is not None and str(boundary) not in error:
            error = f"{error}:{boundary}"
        return _result(
            ok=False,
            product_id=product_id,
            spec_path=spec_path,
            error_code=str(error_code),
            spec_hash=spec_hash,
            spec_version_id=spec_version_id,
            error=error,
        )
    return _result(
        ok=False,
        product_id=product_id,
        spec_path=spec_path,
        error_code="SPEC_COMPILE_FAILED",
        spec_hash=spec_hash,
        spec_version_id=spec_version_id,
        error=str(compile_result.get("error", "Spec authority compile failed")),
        reason=_optional_str(compile_result.get("reason")),
        failure_artifact_id=_optional_str(compile_result.get("failure_artifact_id")),
        failure_stage=_optional_str(compile_result.get("failure_stage")),
        failure_summary=_optional_str(compile_result.get("failure_summary")),
        raw_output_preview=_optional_str(compile_result.get("raw_output_preview")),
        has_full_artifact=_optional_bool(compile_result.get("has_full_artifact")),
        blocking_gaps=_optional_str_list(compile_result.get("blocking_gaps")),
    )


def _cleanup_bad_acceptance(
    session: Session,
    *,
    product_id: int,
    spec_version_id: int,
    existing_acceptance_ids: set[int],
) -> bool:
    """Delete matching acceptance rows left by an invalid compiler seam."""
    rows_before_rollback = session.exec(
        select(SpecAuthorityAcceptance).where(
            SpecAuthorityAcceptance.product_id == product_id,
            SpecAuthorityAcceptance.spec_version_id == spec_version_id,
        )
    ).all()
    new_rows_before_rollback = [
        row for row in rows_before_rollback if row.id not in existing_acceptance_ids
    ]
    session.rollback()
    session.expire_all()
    rows_after_rollback = session.exec(
        select(SpecAuthorityAcceptance).where(
            SpecAuthorityAcceptance.product_id == product_id,
            SpecAuthorityAcceptance.spec_version_id == spec_version_id,
        )
    ).all()
    new_rows_after_rollback = [
        row for row in rows_after_rollback if row.id not in existing_acceptance_ids
    ]
    for row in new_rows_after_rollback:
        session.delete(row)
    if new_rows_after_rollback:
        session.commit()
    return bool(new_rows_before_rollback or new_rows_after_rollback)


def _matching_acceptance_ids(
    session: Session,
    *,
    product_id: int,
    spec_version_id: int,
) -> set[int]:
    """Return IDs of existing acceptance rows for this product/spec pair."""
    return {
        row_id
        for row_id in session.exec(
            select(SpecAuthorityAcceptance.id).where(
                SpecAuthorityAcceptance.product_id == product_id,
                SpecAuthorityAcceptance.spec_version_id == spec_version_id,
            )
        ).all()
        if row_id is not None
    }


def compile_pending_authority_for_project(  # noqa: C901, PLR0911, PLR0912, PLR0913, PLR0915
    *,
    session: Session,
    product_id: int,
    spec_path: Path,
    approved_by: str,
    compile_authority: PendingAuthorityCompiler,
    lease_guard: Callable[[str], bool],
    record_progress: Callable[[str], bool],
) -> PendingAuthorityResult:
    """Compile a reviewable authority artifact without accepting it."""
    loaded = _load_spec_file(spec_path)
    if isinstance(loaded, PendingAuthorityResult):
        return _result(
            ok=False,
            product_id=product_id,
            spec_path=loaded.spec_path,
            error_code=loaded.error_code,
            error=loaded.error,
        )
    resolved_path, spec_content, spec_hash = loaded
    try:
        normalized_spec = normalize_spec_content_for_registry(spec_content)
    except SpecContentNormalizationError as exc:
        return _result(
            ok=False,
            product_id=product_id,
            spec_path=resolved_path,
            error_code=exc.error_code,
            spec_hash=spec_hash,
            error=str(exc),
        )
    spec_content = normalized_spec.content
    spec_hash = normalized_spec.spec_hash

    product = session.get(Product, product_id)
    if product is None:
        return _result(
            ok=False,
            product_id=product_id,
            spec_path=resolved_path,
            error_code="PRODUCT_NOT_FOUND",
            spec_hash=spec_hash,
            error=f"Product {product_id} not found",
        )

    product.spec_file_path = str(resolved_path)
    product.spec_loaded_at = datetime.now(UTC)
    if not lease_guard("product_spec_linked"):
        session.rollback()
        return _lease_lost(
            product_id=product_id,
            spec_path=resolved_path,
            spec_hash=spec_hash,
            boundary="product_spec_linked",
        )
    session.add(product)
    session.commit()
    progress_error = _record_progress_or_error(
        record_progress=record_progress,
        product_id=product_id,
        spec_path=resolved_path,
        spec_hash=spec_hash,
        spec_version_id=None,
        boundary="product_spec_linked",
    )
    if progress_error is not None:
        return progress_error

    latest_spec = _latest_spec_for_product(session, product_id=product_id)
    if latest_spec and latest_spec.spec_hash == spec_hash:
        spec_version = latest_spec
    else:
        spec_version = SpecRegistry(
            product_id=product_id,
            spec_hash=spec_hash,
            content=spec_content,
            content_ref=str(resolved_path),
            status="draft",
        )
    if not lease_guard("spec_registry_written"):
        session.rollback()
        return _lease_lost(
            product_id=product_id,
            spec_path=resolved_path,
            spec_hash=spec_hash,
            boundary="spec_registry_written",
        )
    session.add(spec_version)
    session.commit()
    session.refresh(spec_version)
    spec_version_id = spec_version.spec_version_id
    if spec_version_id is None:
        return _result(
            ok=False,
            product_id=product_id,
            spec_path=resolved_path,
            error_code="MUTATION_FAILED",
            spec_hash=spec_hash,
            error="Spec registry row did not receive a primary key",
        )
    progress_error = _record_progress_or_error(
        record_progress=record_progress,
        product_id=product_id,
        spec_path=resolved_path,
        spec_hash=spec_hash,
        spec_version_id=spec_version_id,
        boundary="spec_registry_written",
    )
    if progress_error is not None:
        return progress_error

    spec_version.status = "approved"
    spec_version.approved_at = datetime.now(UTC)
    spec_version.approved_by = approved_by
    spec_version.approval_notes = _PENDING_APPROVAL_NOTES
    if not lease_guard("spec_marked_approved"):
        session.rollback()
        return _lease_lost(
            product_id=product_id,
            spec_path=resolved_path,
            spec_hash=spec_hash,
            boundary="spec_marked_approved",
        )
    session.add(spec_version)
    session.commit()
    progress_error = _record_progress_or_error(
        record_progress=record_progress,
        product_id=product_id,
        spec_path=resolved_path,
        spec_hash=spec_hash,
        spec_version_id=spec_version_id,
        boundary="spec_marked_approved",
    )
    if progress_error is not None:
        return progress_error

    existing_acceptance_ids = _matching_acceptance_ids(
        session,
        product_id=product_id,
        spec_version_id=spec_version_id,
    )
    try:
        compile_result = compile_authority(
            spec_version_id=spec_version_id,
            force_recompile=False,
            tool_context=None,
            lease_guard=lease_guard,
            record_progress=record_progress,
        )
    except Exception as exc:  # noqa: BLE001
        _cleanup_bad_acceptance(
            session,
            product_id=product_id,
            spec_version_id=spec_version_id,
            existing_acceptance_ids=existing_acceptance_ids,
        )
        return _result(
            ok=False,
            product_id=product_id,
            spec_path=resolved_path,
            error_code="MUTATION_FAILED",
            spec_hash=spec_hash,
            spec_version_id=spec_version_id,
            error=str(exc),
        )
    if _cleanup_bad_acceptance(
        session,
        product_id=product_id,
        spec_version_id=spec_version_id,
        existing_acceptance_ids=existing_acceptance_ids,
    ):
        return _result(
            ok=False,
            product_id=product_id,
            spec_path=resolved_path,
            error_code="MUTATION_FAILED",
            spec_hash=spec_hash,
            spec_version_id=spec_version_id,
            error="Pending authority path must not accept authority.",
        )
    if not compile_result.get("success"):
        return _normalize_compiler_failure(
            product_id=product_id,
            spec_path=resolved_path,
            spec_hash=spec_hash,
            spec_version_id=spec_version_id,
            compile_result=compile_result,
        )

    authority = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id == spec_version_id
        )
    ).first()
    compile_authority_id = compile_result.get("authority_id")
    authority_id = (
        int(str(compile_authority_id))
        if compile_authority_id is not None
        else None
    )
    if authority is not None and authority.authority_id is not None:
        authority_id = authority.authority_id

    return _result(
        ok=True,
        product_id=product_id,
        spec_path=resolved_path,
        spec_hash=spec_hash,
        spec_version_id=spec_version_id,
        authority_id=authority_id,
        compiler_version=_optional_str(compile_result.get("compiler_version")),
        prompt_hash=_optional_str(compile_result.get("prompt_hash")),
    )


def _optional_str(value: object) -> str | None:
    """Return value as a string when present."""
    if value is None:
        return None
    return str(value)


def _optional_bool(value: object) -> bool | None:
    """Return value as a bool when present."""
    if value is None:
        return None
    return bool(value)


def _optional_str_list(value: object) -> list[str] | None:
    """Return value as a string list when possible."""
    if not isinstance(value, list):
        return None
    return [str(item) for item in value]
