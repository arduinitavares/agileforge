"""Tests for deterministic compiled-authority row selection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from models.core import Product
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from services.specs import authority_selection
from tests.authority_assumption_fixtures import current_v3_compiled_authority_json
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
            compiler_version="3.0.0",
            prompt_hash=prompt,
            compiled_artifact_json=f'{{"prompt_hash":"{prompt}"}}',
            scope_themes="[]",
            invariants="[]",
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
        )
        for prompt in ("a" * 64, "b" * 64)
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

    assert (
        authority_selection.compiled_authority_by_id(
            session,
            authority_id=require_id(old.authority_id, "authority_id"),
            expected_spec_version_id=spec_version_id + 1,
        )
        is None
    )


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


def test_accepted_compiled_authority_selects_exact_accepted_row(
    session: Session,
) -> None:
    """Execution selection ignores retained history and newer pending rows."""
    product_id, spec_version_id, retained, accepted = _history(session)
    acceptance = SpecAuthorityAcceptance(
        product_id=product_id,
        spec_version_id=spec_version_id,
        status="accepted",
        policy="test",
        decided_by="test",
        compiler_version=accepted.compiler_version,
        prompt_hash=accepted.prompt_hash,
        spec_hash="sha256:selection",
        pending_authority_id=accepted.authority_id,
    )
    session.add(acceptance)
    session.commit()
    pending = CompiledSpecAuthority(
        spec_version_id=spec_version_id,
        compiler_version="3.0.0",
        prompt_hash="c" * 64,
        compiled_artifact_json="{}",
        scope_themes="[]",
        invariants="[]",
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps="[]",
    )
    session.add(pending)
    session.commit()

    selected = authority_selection.accepted_compiled_authority(
        session,
        product_id=product_id,
        spec_version_id=spec_version_id,
    )

    assert selected is accepted
    assert selected is not retained
    assert selected is not pending


def test_accepted_compiled_authority_uses_latest_deterministic_decision(
    session: Session,
) -> None:
    """Multiple accepted decisions use decided-at then id ordering."""
    product_id, spec_version_id, first, second = _history(session)
    decided_at = datetime.now(UTC)
    decisions = [
        SpecAuthorityAcceptance(
            product_id=product_id,
            spec_version_id=spec_version_id,
            status="accepted",
            policy="test",
            decided_by="test",
            decided_at=decided_at,
            compiler_version=authority.compiler_version,
            prompt_hash=authority.prompt_hash,
            spec_hash="sha256:selection",
            pending_authority_id=authority.authority_id,
        )
        for authority in (first, second)
    ]
    session.add_all(decisions)
    session.commit()

    selected = authority_selection.accepted_compiled_authority(
        session,
        product_id=product_id,
        spec_version_id=spec_version_id,
    )

    assert selected is second


def test_accepted_compiled_authority_rejects_foreign_product_spec(
    session: Session,
) -> None:
    """The selected spec must belong to the requested product."""
    _, spec_version_id, _, accepted = _history(session)
    foreign_product = Product(name="Foreign Authority Selection")
    session.add(foreign_product)
    session.commit()
    session.refresh(foreign_product)
    foreign_product_id = require_id(foreign_product.product_id, "product_id")
    session.add(
        SpecAuthorityAcceptance(
            product_id=foreign_product_id,
            spec_version_id=spec_version_id,
            status="accepted",
            policy="test",
            decided_by="test",
            compiler_version=accepted.compiler_version,
            prompt_hash=accepted.prompt_hash,
            spec_hash="sha256:selection",
            pending_authority_id=accepted.authority_id,
        )
    )
    session.commit()

    assert (
        authority_selection.accepted_compiled_authority(
            session,
            product_id=foreign_product_id,
            spec_version_id=spec_version_id,
        )
        is None
    )


def test_accepted_compiled_authority_rejects_acceptance_authority_spec_mismatch(
    session: Session,
) -> None:
    """A mismatched acceptance target never falls back to another row."""
    product_id, spec_version_id, _, accepted = _history(session)
    other_spec = SpecRegistry(
        product_id=product_id,
        spec_hash="sha256:other-spec",
        content="other",
        status="approved",
    )
    session.add(other_spec)
    session.commit()
    session.refresh(other_spec)
    other_authority = CompiledSpecAuthority(
        spec_version_id=require_id(other_spec.spec_version_id, "spec_version_id"),
        compiler_version="3.0.0",
        prompt_hash="d" * 64,
        compiled_artifact_json="{}",
        scope_themes="[]",
        invariants="[]",
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps="[]",
    )
    session.add(other_authority)
    session.commit()
    session.add(
        SpecAuthorityAcceptance(
            product_id=product_id,
            spec_version_id=spec_version_id,
            status="accepted",
            policy="test",
            decided_by="test",
            compiler_version=other_authority.compiler_version,
            prompt_hash=other_authority.prompt_hash,
            spec_hash="sha256:selection",
            pending_authority_id=other_authority.authority_id,
        )
    )
    session.commit()

    assert (
        authority_selection.accepted_compiled_authority(
            session,
            product_id=product_id,
            spec_version_id=spec_version_id,
        )
        is None
    )
    assert accepted.authority_id is not None


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("compiler_version", "3.0.1"),
        ("prompt_hash", "f" * 64),
        ("spec_hash", "sha256:changed"),
    ],
)
def test_accepted_compiled_authority_rejects_acceptance_provenance_mismatch(
    session: Session,
    field_name: str,
    replacement: str,
) -> None:
    """Accepted execution requires exact compiler, prompt, and spec provenance."""
    product_id, spec_version_id, _, authority = _history(session)
    acceptance = SpecAuthorityAcceptance(
        product_id=product_id,
        spec_version_id=spec_version_id,
        status="accepted",
        policy="test",
        decided_by="test",
        compiler_version=authority.compiler_version,
        prompt_hash=authority.prompt_hash,
        spec_hash="sha256:selection",
        pending_authority_id=authority.authority_id,
    )
    setattr(acceptance, field_name, replacement)
    session.add(acceptance)
    session.commit()

    assert (
        authority_selection.accepted_compiled_authority(
            session,
            product_id=product_id,
            spec_version_id=spec_version_id,
        )
        is None
    )


def test_accepted_compiled_authority_rejects_blank_acceptance_spec_hash(
    session: Session,
) -> None:
    """A blank decision-time hash never bypasses exact spec provenance."""
    product_id, spec_version_id, _, authority = _history(session)
    session.add(
        SpecAuthorityAcceptance(
            product_id=product_id,
            spec_version_id=spec_version_id,
            status="accepted",
            policy="test",
            decided_by="test",
            compiler_version=authority.compiler_version,
            prompt_hash=authority.prompt_hash,
            spec_hash="",
            pending_authority_id=authority.authority_id,
        )
    )
    session.commit()

    assert (
        authority_selection.accepted_compiled_authority(
            session,
            product_id=product_id,
            spec_version_id=spec_version_id,
        )
        is None
    )


def test_accepted_v3_authority_requires_acceptance_fingerprint(
    session: Session,
) -> None:
    """A v3 decision without immutable artifact identity is not executable."""
    product_id, spec_version_id, _, authority = _history(session)
    authority.compiled_artifact_json = current_v3_compiled_authority_json(
        prompt_hash=authority.prompt_hash,
    )
    session.add(authority)
    session.commit()
    session.add(
        SpecAuthorityAcceptance(
            product_id=product_id,
            spec_version_id=spec_version_id,
            status="accepted",
            policy="test",
            decided_by="test",
            compiler_version=authority.compiler_version,
            prompt_hash=authority.prompt_hash,
            spec_hash="sha256:selection",
            pending_authority_id=authority.authority_id,
            authority_fingerprint=None,
        )
    )
    session.commit()

    assert (
        authority_selection.accepted_compiled_authority(
            session,
            product_id=product_id,
            spec_version_id=spec_version_id,
        )
        is None
    )


def test_accepted_v3_authority_rejects_post_acceptance_artifact_mutation(
    session: Session,
) -> None:
    """A valid in-place artifact mutation invalidates the accepted decision."""
    product_id, spec_version_id, _, authority = _history(session)
    authority.compiled_artifact_json = current_v3_compiled_authority_json(
        prompt_hash=authority.prompt_hash,
        scope_themes=["accepted"],
    )
    session.add(authority)
    session.commit()
    acceptance = SpecAuthorityAcceptance(
        product_id=product_id,
        spec_version_id=spec_version_id,
        status="accepted",
        policy="test",
        decided_by="test",
        compiler_version=authority.compiler_version,
        prompt_hash=authority.prompt_hash,
        spec_hash="sha256:selection",
        pending_authority_id=authority.authority_id,
        authority_fingerprint=authority_selection.pending_authority_fingerprint(
            authority
        ),
    )
    session.add(acceptance)
    session.commit()

    authority.compiled_artifact_json = current_v3_compiled_authority_json(
        prompt_hash=authority.prompt_hash,
        scope_themes=["mutated"],
    )
    session.add(authority)
    session.commit()

    assert (
        authority_selection.accepted_compiled_authority(
            session,
            product_id=product_id,
            spec_version_id=spec_version_id,
        )
        is None
    )
