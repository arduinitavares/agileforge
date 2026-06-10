"""Tests for shared phase authority guards."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from services.phases.authority_guard import (
    phase_authority_block_error,
    sync_compiled_authority_cache,
)
from tests.test_agent_workbench_authority_projection import (
    _accept_spec,
    _seed_authority,
    _seed_product,
    _seed_spec,
)
from tests.typing_helpers import require_id

if TYPE_CHECKING:
    from sqlmodel import Session


def test_phase_authority_block_error_returns_none_for_current_authority(
    session: Session,
    tmp_path: Path,
) -> None:
    """Current accepted authority should not block phase generation."""
    product = _seed_product(session)
    product_id = require_id(product.product_id, "product_id")
    spec = _seed_spec(session, product_id=product_id, content="# Spec\n")
    _seed_authority(
        session,
        spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
        compiler_version="1.0.0",
    )
    _accept_spec(session, product_id=product_id, spec=spec)

    block_error = phase_authority_block_error(project_id=product_id)

    assert block_error is None


def test_phase_authority_block_error_blocks_stale_acceptance(
    session: Session,
    tmp_path: Path,
) -> None:
    """Stale accepted authority must block phase generation."""
    product = _seed_product(session)
    product_id = require_id(product.product_id, "product_id")
    spec = _seed_spec(session, product_id=product_id, content="# Spec\n")
    _seed_authority(
        session,
        spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
        compiler_version="2.0.0",
    )
    _accept_spec(session, product_id=product_id, spec=spec)

    block_error = phase_authority_block_error(project_id=product_id)

    assert block_error is not None
    assert block_error["code"] == "STALE_AUTHORITY_VERSION"
    assert block_error["details"]["authority_status"] == "stale"
    assert block_error["details"]["stale_reason"] == "accepted_compiler_prompt_mismatch"


def test_sync_compiled_authority_cache_updates_stale_session_value() -> None:
    """Session authority cache should follow the product row before generation."""
    state = {"compiled_authority_cached": '{"old": true}'}
    product_json = '{"new": true}'

    changed = sync_compiled_authority_cache(
        state=state,
        product_authority_json=product_json,
    )

    assert changed is True
    assert state["compiled_authority_cached"] == product_json


def test_sync_compiled_authority_cache_is_noop_when_already_current() -> None:
    """Avoid rewriting session state when the cache already matches."""
    authority_json = '{"same": true}'
    state = {"compiled_authority_cached": authority_json}

    changed = sync_compiled_authority_cache(
        state=state,
        product_authority_json=authority_json,
    )

    assert changed is False
