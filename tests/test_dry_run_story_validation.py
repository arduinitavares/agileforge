"""Tests for the read-only story-validation inspection script."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agile_sqlmodel import (
    CompiledSpecAuthority,
    Project,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from scripts import dry_run_story_validation as dry_run
from services.specs.authority_selection import pending_authority_fingerprint
from tests.authority_assumption_fixtures import current_v3_compiled_authority_json
from tests.typing_helpers import require_id

if TYPE_CHECKING:
    from sqlmodel import Session


def test_load_accepted_invariants_uses_exact_valid_authority(
    session: Session,
) -> None:
    """Dry-run inspection ignores retained malformed authority history."""
    product = Project(name="Dry Run Project")
    session.add(product)
    session.commit()
    session.refresh(product)
    project_id = require_id(product.project_id, "project_id")
    spec = SpecRegistry(
        project_id=project_id,
        spec_hash="dry-run-spec",
        content="# Dry run",
        status="approved",
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)
    spec_version_id = require_id(spec.spec_version_id, "spec_version_id")
    session.add(
        CompiledSpecAuthority(
            spec_version_id=spec_version_id,
            compiler_version="2.0.0",
            prompt_hash="a" * 64,
            compiled_artifact_json="not-json",
            scope_themes="[]",
            invariants="[]",
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
        )
    )
    payload = json.loads(current_v3_compiled_authority_json(prompt_hash="b" * 64))
    payload["invariants"] = [
        {
            "id": "INV-fedcba9876543210",
            "type": "FORBIDDEN_CAPABILITY",
            "parameters": {"capability": "cloud sync"},
        }
    ]
    accepted = CompiledSpecAuthority(
        spec_version_id=spec_version_id,
        compiler_version="3.0.0",
        prompt_hash="b" * 64,
        compiled_artifact_json=json.dumps(payload),
        scope_themes="[]",
        invariants="[]",
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps="[]",
    )
    session.add(accepted)
    session.flush()
    session.add(
        SpecAuthorityAcceptance(
            project_id=project_id,
            spec_version_id=spec_version_id,
            status="accepted",
            policy="test",
            decided_by="dry-run-test",
            compiler_version=accepted.compiler_version,
            prompt_hash=accepted.prompt_hash,
            spec_hash=spec.spec_hash,
            pending_authority_id=accepted.authority_id,
            authority_fingerprint=pending_authority_fingerprint(accepted),
        )
    )
    session.commit()

    invariants = dry_run._load_accepted_invariants(
        session,
        project_id=project_id,
        spec_version_id=spec_version_id,
    )

    assert invariants is not None
    assert invariants[0]["id"] == "INV-fedcba9876543210"
    assert invariants[0]["type"] == "FORBIDDEN_CAPABILITY"
    assert invariants[0]["parameters"] == {"capability": "cloud sync"}
