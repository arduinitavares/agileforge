"""Shared deterministic selection for compiled-authority history."""

from __future__ import annotations

from typing import Any, cast

from sqlmodel import Session, select

from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)


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
