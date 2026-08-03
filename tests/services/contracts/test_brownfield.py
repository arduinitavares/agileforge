"""Strict pre-authority Brownfield curation contract tests."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from services.contracts.brownfield import (
    BrownfieldCurationInput,
    BrownfieldCurationOutput,
)
from utils.agileforge_spec_profile import TechnicalSpecArtifact

INVENTORY_ID = 41


def _content_sha256(content: str) -> str:
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def _canonical_spec() -> dict[str, object]:
    return {
        "schema_version": "agileforge.spec.v1",
        "artifact_id": "SPEC.brownfield.contract",
        "title": "Brownfield Initial Scope",
        "status": "draft",
        "version": "0.1",
        "created_at": "2026-08-03",
        "updated_at": "2026-08-03",
        "summary": "Initial scope curated from selected repository evidence.",
        "problem_statement": "Existing behavior needs reviewed Project authority.",
        "items": [
            {
                "id": "REQ.brownfield.contract",
                "type": "REQ",
                "status": "proposed",
                "title": "Preserve reviewed behavior",
                "statement": "The system MUST preserve reviewed behavior.",
                "level": "MUST",
                "verification": "system-test",
                "acceptance": ["The reviewed behavior remains available."],
            }
        ],
        "relations": [],
        "controlled_terms": [],
        "external_references": [],
        "rendering": {
            "markdown_profile": "agileforge.spec_markdown.v1",
            "rendered_markdown_sha256": None,
        },
    }


def _curation_input(
    *,
    inventory_fingerprint: str | None = f"sha256:{'a' * 64}",
    selected_for_model: tuple[str, ...] = ("README.md",),
) -> dict[str, object]:
    content = "# Existing product\n\nThe service preserves reviewed behavior.\n"
    inventory: dict[str, object] = {
        "repository_inventory_id": INVENTORY_ID,
        "file_count": 2,
        "total_bytes": 84,
        "selected_for_model": selected_for_model,
    }
    if inventory_fingerprint is not None:
        inventory["repository_inventory_fingerprint"] = inventory_fingerprint
    return {
        "inventory": inventory,
        "selected_evidence": [
            {
                "path": "README.md",
                "content": content,
                "content_sha256": _content_sha256(content),
            }
        ],
    }


def test_brownfield_contract_round_trips_pre_authority_evidence_and_spec() -> None:
    """Expose inventory-bound evidence and canonical initial-spec output only."""
    curation_input = BrownfieldCurationInput.model_validate(_curation_input())
    output = BrownfieldCurationOutput.model_validate(
        {"canonical_spec": _canonical_spec()}
    )

    assert curation_input.inventory.repository_inventory_id == INVENTORY_ID
    assert curation_input.selected_evidence[0].path == "README.md"
    assert isinstance(output.canonical_spec, TechnicalSpecArtifact)
    assert output.canonical_spec.schema_version == "agileforge.spec.v1"
    assert "compiled_authority" not in curation_input.model_dump(mode="json")


def test_brownfield_input_rejects_missing_or_model_owned_binding() -> None:
    """Require host-owned inventory identity and reject post-authority fields."""
    with pytest.raises(ValidationError):
        BrownfieldCurationInput.model_validate(
            _curation_input(inventory_fingerprint=None)
        )

    post_authority = {
        **_curation_input(),
        "compiled_authority": {"invariants": []},
    }
    with pytest.raises(ValidationError):
        BrownfieldCurationInput.model_validate(post_authority)


def test_brownfield_input_rejects_evidence_not_bound_to_inventory_selection() -> None:
    """Keep the exact ordered model selection outside generated output."""
    with pytest.raises(ValidationError, match="selected evidence"):
        BrownfieldCurationInput.model_validate(
            _curation_input(selected_for_model=("pyproject.toml",))
        )


@pytest.mark.parametrize(
    "generated",
    [
        {"assessment_summary": "post-authority As-Built output"},
        {
            "canonical_spec": {
                "schema_version": "agileforge.spec.v0",
                "title": "Noncanonical",
            }
        },
    ],
)
def test_brownfield_output_rejects_malformed_or_noncanonical_model_output(
    generated: dict[str, object],
) -> None:
    """Fail closed unless model output is the canonical spec profile."""
    with pytest.raises(ValidationError):
        BrownfieldCurationOutput.model_validate(generated)
