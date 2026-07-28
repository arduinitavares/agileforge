"""Shared deterministic selection for compiled-authority history."""

from __future__ import annotations

import json
from json import JSONDecodeError
from typing import Any, cast

from sqlmodel import Session, select

from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from services.agent_workbench.fingerprints import canonical_hash

_COMPILED_AUTHORITY_V3: str = "agileforge.compiled_authority.v3"


def _json_field_for_fingerprint(raw: str | None) -> object:
    """Return a canonical JSON field value without unstable object reprs."""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except JSONDecodeError:
        return {"malformed_json": raw}


def pending_authority_fingerprint(
    authority: CompiledSpecAuthority | None,
) -> str | None:
    """Return the canonical immutable fingerprint for a compiled authority."""
    if authority is None:
        return None
    return canonical_hash(
        {
            "command": "agileforge authority status",
            "pending_compiled": {
                "authority_id": authority.authority_id,
                "spec_version_id": authority.spec_version_id,
                "compiler_version": authority.compiler_version,
                "prompt_hash": authority.prompt_hash,
                "compiled_at": authority.compiled_at,
                "compiled_artifact_json": _json_field_for_fingerprint(
                    authority.compiled_artifact_json
                ),
            },
        }
    )


def _is_v3_authority(authority: CompiledSpecAuthority) -> bool:
    """Return whether the stored artifact declares the current v3 contract."""
    raw = authority.compiled_artifact_json
    if raw is None:
        return False
    try:
        payload = json.loads(raw)
    except JSONDecodeError:
        return False
    return (
        isinstance(payload, dict)
        and payload.get("schema_version") == _COMPILED_AUTHORITY_V3
    )


def _authority_matches_acceptance(
    *,
    authority: CompiledSpecAuthority,
    acceptance: SpecAuthorityAcceptance,
    spec: SpecRegistry,
) -> bool:
    """Return whether one exact row still matches its accepted provenance."""
    if (
        acceptance.status != "accepted"
        or acceptance.product_id != spec.product_id
        or acceptance.spec_version_id != spec.spec_version_id
        or authority.authority_id != acceptance.pending_authority_id
        or authority.spec_version_id != acceptance.spec_version_id
        or authority.compiler_version != acceptance.compiler_version
        or authority.prompt_hash != acceptance.prompt_hash
    ):
        return False
    if acceptance.spec_hash != spec.spec_hash:
        return False

    current_fingerprint = pending_authority_fingerprint(authority)
    if acceptance.authority_fingerprint is not None:
        return current_fingerprint == acceptance.authority_fingerprint
    return not _is_v3_authority(authority)


def compiled_authority_by_id(
    session: Session,
    *,
    authority_id: int,
    expected_spec_version_id: int | None = None,
) -> CompiledSpecAuthority | None:
    """Load one exact authority row, optionally enforcing its spec version."""
    authority = session.get(CompiledSpecAuthority, authority_id)
    if (
        authority is not None
        and expected_spec_version_id is not None
        and authority.spec_version_id != expected_spec_version_id
    ):
        return None
    return authority


def latest_compiled_authority(
    session: Session,
    *,
    spec_version_id: int,
) -> CompiledSpecAuthority | None:
    """Load the newest inserted authority row for a spec version."""
    return session.exec(
        select(CompiledSpecAuthority)
        .where(CompiledSpecAuthority.spec_version_id == spec_version_id)
        .order_by(cast("Any", CompiledSpecAuthority.authority_id).desc())
    ).first()


def latest_compiled_authority_for_product(
    session: Session,
    *,
    product_id: int,
) -> CompiledSpecAuthority | None:
    """Load the newest row for the newest compiled spec owned by a product."""
    return session.exec(
        select(CompiledSpecAuthority)
        .join(
            SpecRegistry,
            cast("Any", CompiledSpecAuthority.spec_version_id)
            == SpecRegistry.spec_version_id,
        )
        .where(SpecRegistry.product_id == product_id)
        .order_by(
            cast("Any", SpecRegistry.spec_version_id).desc(),
            cast("Any", CompiledSpecAuthority.authority_id).desc(),
        )
    ).first()


def compiled_authority_for_acceptance(
    session: Session,
    *,
    acceptance: SpecAuthorityAcceptance,
) -> CompiledSpecAuthority | None:
    """Load only the exact authority row named by a terminal decision."""
    authority_id = acceptance.pending_authority_id
    if authority_id is None:
        return None
    return compiled_authority_by_id(
        session,
        authority_id=authority_id,
        expected_spec_version_id=acceptance.spec_version_id,
    )


def latest_accepted_authority_decision(
    session: Session,
    *,
    product_id: int,
    spec_version_id: int,
) -> SpecAuthorityAcceptance | None:
    """Load the newest accepted decision for a product/spec pair."""
    return session.exec(
        select(SpecAuthorityAcceptance)
        .where(
            SpecAuthorityAcceptance.product_id == product_id,
            SpecAuthorityAcceptance.spec_version_id == spec_version_id,
            SpecAuthorityAcceptance.status == "accepted",
        )
        .order_by(
            cast("Any", SpecAuthorityAcceptance.decided_at).desc(),
            cast("Any", SpecAuthorityAcceptance.id).desc(),
        )
    ).first()


def accepted_compiled_authority(
    session: Session,
    *,
    product_id: int,
    spec_version_id: int,
) -> CompiledSpecAuthority | None:
    """Load the exact accepted authority for one product-owned spec."""
    spec = session.get(SpecRegistry, spec_version_id)
    if spec is None or spec.product_id != product_id:
        return None
    acceptance = latest_accepted_authority_decision(
        session,
        product_id=product_id,
        spec_version_id=spec_version_id,
    )
    if acceptance is None:
        return None
    authority = compiled_authority_for_acceptance(session, acceptance=acceptance)
    if authority is None:
        return None
    if not _authority_matches_acceptance(
        authority=authority,
        acceptance=acceptance,
        spec=spec,
    ):
        return None
    return authority
