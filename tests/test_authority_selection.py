"""Tests for deterministic compiled-authority row selection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from models.core import Product
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from services.specs import authority_selection
from tests.typing_helpers import require_id

if TYPE_CHECKING:
    from sqlmodel import Session


def _history(
    session: Session,
) -> tuple[int, int, CompiledSpecAuthority, CompiledSpecAuthority]:
    product = Product(name="Authority Selection")
    session.add(product)
    session.commit()
    session.refresh(product)
    product_id = require_id(product.product_id, "product_id")
    spec = SpecRegistry(
        product_id=product_id,
        spec_hash="sha256:selection",
        content="selection",
        status="approved",
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)
    spec_version_id = require_id(spec.spec_version_id, "spec_version_id")
    rows = [
        CompiledSpecAuthority(
            spec_version_id=spec_version_id,
            compiler_version=version,
            prompt_hash=prompt,
            compiled_artifact_json=f'{{"row":"{version}"}}',
            scope_themes="[]",
            invariants="[]",
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
        )
        for version, prompt in (("2.0.0", "a" * 64), ("3.0.0", "b" * 64))
    ]
    session.add_all(rows)
    session.commit()
    for row in rows:
        session.refresh(row)
    return product_id, spec_version_id, rows[0], rows[1]


def test_compiled_authority_by_id_rejects_spec_version_mismatch(
    session: Session,
) -> None:
    """Exact-id selection also enforces the caller's expected spec version."""
    _, spec_version_id, old, _ = _history(session)

    assert authority_selection.compiled_authority_by_id(
        session,
        authority_id=require_id(old.authority_id, "authority_id"),
        expected_spec_version_id=spec_version_id + 1,
    ) is None


def test_latest_compiled_authority_orders_by_authority_id_desc(
    session: Session,
) -> None:
    """Current/pending lookup deterministically selects the newest inserted row."""
    _, spec_version_id, _, newest = _history(session)

    selected = authority_selection.latest_compiled_authority(
        session,
        spec_version_id=spec_version_id,
    )

    assert selected is not None
    assert selected.authority_id == newest.authority_id


def test_latest_compiled_authority_for_product_prefers_newest_spec_then_row(
    session: Session,
) -> None:
    """Cross-spec status selection stays project-owned and deterministic."""
    product = Product(name="Selected Product")
    other_product = Product(name="Other Product")
    session.add_all([product, other_product])
    session.commit()
    session.refresh(product)
    session.refresh(other_product)
    product_id = require_id(product.product_id, "product_id")
    other_product_id = require_id(other_product.product_id, "product_id")
    specs = [
        SpecRegistry(
            product_id=owner_id,
            spec_hash=spec_hash,
            content=spec_hash,
            status="approved",
        )
        for owner_id, spec_hash in (
            (product_id, "sha256:selected-old"),
            (product_id, "sha256:selected-new"),
            (other_product_id, "sha256:other"),
        )
    ]
    session.add_all(specs)
    session.commit()
    for spec in specs:
        session.refresh(spec)

    def authority(spec: SpecRegistry, prompt: str) -> CompiledSpecAuthority:
        return CompiledSpecAuthority(
            spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
            compiler_version="3.0.0",
            prompt_hash=prompt,
            compiled_artifact_json="{}",
            scope_themes="[]",
            invariants="[]",
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
        )

    newest_spec_old_row = authority(specs[1], "a" * 64)
    newest_spec_new_row = authority(specs[1], "b" * 64)
    foreign_row = authority(specs[2], "c" * 64)
    older_spec_late_row = authority(specs[0], "d" * 64)
    session.add_all(
        [
            newest_spec_old_row,
            newest_spec_new_row,
            foreign_row,
            older_spec_late_row,
        ]
    )
    session.commit()

    selected = authority_selection.latest_compiled_authority_for_product(
        session,
        product_id=product_id,
    )

    assert selected is not None
    assert selected.authority_id == newest_spec_new_row.authority_id
    assert selected.authority_id != foreign_row.authority_id
    assert selected.authority_id != older_spec_late_row.authority_id


def test_compiled_authority_for_acceptance_uses_exact_pending_id(
    session: Session,
) -> None:
    """Terminal decisions never fall forward to a newer pending row."""
    product_id, spec_version_id, accepted_row, _ = _history(session)
    acceptance = SpecAuthorityAcceptance(
        product_id=product_id,
        spec_version_id=spec_version_id,
        status="accepted",
        policy="test",
        decided_by="test",
        compiler_version=accepted_row.compiler_version,
        prompt_hash=accepted_row.prompt_hash,
        spec_hash="sha256:selection",
        pending_authority_id=accepted_row.authority_id,
    )

    selected = authority_selection.compiled_authority_for_acceptance(
        session,
        acceptance=acceptance,
    )

    assert selected is accepted_row


def test_latest_accepted_authority_decision_orders_by_time_then_id(
    session: Session,
) -> None:
    """Accepted decision lookup deterministically breaks timestamp ties by id."""
    product_id, spec_version_id, old, newest = _history(session)
    decided_at = datetime.now(UTC)
    decisions = [
        SpecAuthorityAcceptance(
            product_id=product_id,
            spec_version_id=spec_version_id,
            status=status,
            policy="test",
            decided_by="test",
            decided_at=when,
            compiler_version=authority.compiler_version,
            prompt_hash=authority.prompt_hash,
            spec_hash="sha256:selection",
            pending_authority_id=authority.authority_id,
        )
        for status, when, authority in (
            ("accepted", decided_at - timedelta(seconds=1), old),
            ("accepted", decided_at, old),
            ("accepted", decided_at, newest),
            ("rejected", decided_at + timedelta(seconds=1), newest),
        )
    ]
    session.add_all(decisions)
    session.commit()

    selected = authority_selection.latest_accepted_authority_decision(
        session,
        product_id=product_id,
        spec_version_id=spec_version_id,
    )

    assert selected is not None
    assert selected.id == decisions[2].id
