"""Contract tests for the isolated Vision interview agent."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.contracts.vision import (
    VisionAgentInput,
    VisionBootstrapInput,
    VisionClarificationInput,
    VisionComponents,
    VisionDraftOutput,
    VisionPreflight,
    VisionRevisionInput,
)
from services.contracts.vision_evidence import VisionEvidenceBundle, VisionEvidenceItem
from workflow.fingerprints import canonical_hash


def _components(**overrides: str | None) -> VisionComponents:
    """Build one complete Vision component set with focused overrides."""
    values: dict[str, str | None] = {
        "project_name": "AgileForge",
        "target_user": "Product operators",
        "problem": "Workflow state is difficult to trust",
        "product_category": "Local workflow tool",
        "key_benefit": "Durable workflow decisions",
        "competitors": "Spreadsheets",
        "differentiator": "Evidence-backed state",
    }
    values.update(overrides)
    return VisionComponents(**values)


def _evidence_bundle(*, content: str = "Repository context") -> VisionEvidenceBundle:
    item = VisionEvidenceItem(
        evidence_id="file:README.md",
        kind="readme",
        relative_path="README.md",
        content_fingerprint=canonical_hash(content),
        trust="unreviewed_repository_evidence",
        content=content,
        truncated=False,
    )
    return VisionEvidenceBundle(
        schema_version="agileforge.vision-evidence.v1",
        items=(item,),
        warnings=(),
        evidence_fingerprint=canonical_hash(
            {
                "schema_version": "agileforge.vision-evidence.v1",
                "items": [item.model_dump(mode="json")],
                "warnings": [],
            }
        ),
    )


def test_bootstrap_input_and_agent_envelope_are_strict() -> None:
    """Bootstrap has no snapshot or preflight lineage."""
    bootstrap = VisionBootstrapInput(
        schema_version="agileforge.vision-input.v1",
        operation="bootstrap",
        project_name="AgileForge",
        project_description=None,
        evidence=_evidence_bundle(),
    )
    parsed = VisionAgentInput(request=bootstrap, preflight=None)

    assert parsed.request.operation == "bootstrap"
    with pytest.raises(ValidationError, match="extra"):
        VisionBootstrapInput(
            **(bootstrap.model_dump() | {"vision_evidence_snapshot_id": 1})
        )
    with pytest.raises(ValidationError, match="preflight"):
        VisionAgentInput(
            request=bootstrap,
            preflight=VisionPreflight(
                expected_evidence_fingerprint="sha256:" + "0" * 64,
                observed_evidence=_evidence_bundle(content="Fresh repository context"),
            ),
        )


def test_clarification_requires_fresh_preflight_bound_to_persisted_evidence() -> None:
    """Clarification preflight records fresh evidence against the stored bundle."""
    persisted_evidence = _evidence_bundle()
    clarification = VisionClarificationInput(
        schema_version="agileforge.vision-input.v1",
        operation="clarification",
        project_name="AgileForge",
        project_description=None,
        vision_evidence_snapshot_id=1,
        evidence=persisted_evidence,
        current_components=_components(),
        current_statement="A durable workflow tool.",
        current_component_basis=(),
        current_assumptions=(),
        current_conflicts=(),
        current_questions=(),
        human_response="Keep the current target audience.",
        addressed_question_ids=(),
    )
    fresh_evidence = _evidence_bundle(content="Fresh repository context")

    with pytest.raises(ValidationError, match="preflight"):
        VisionAgentInput(request=clarification)
    with pytest.raises(ValidationError, match="expected_evidence_fingerprint"):
        VisionAgentInput(
            request=clarification,
            preflight=VisionPreflight(
                expected_evidence_fingerprint=fresh_evidence.evidence_fingerprint,
                observed_evidence=fresh_evidence,
            ),
        )

    parsed = VisionAgentInput(
        request=clarification,
        preflight=VisionPreflight(
            expected_evidence_fingerprint=persisted_evidence.evidence_fingerprint,
            observed_evidence=fresh_evidence,
        ),
    )

    assert parsed.preflight is not None
    assert (
        parsed.preflight.observed_evidence.evidence_fingerprint
        == fresh_evidence.evidence_fingerprint
    )


def test_revision_rejects_preflight() -> None:
    """Only clarification performs a persisted-evidence preflight."""
    revision = VisionRevisionInput(
        schema_version="agileforge.vision-input.v1",
        operation="revision",
        project_name="AgileForge",
        project_description=None,
        evidence=_evidence_bundle(),
        accepted_components=_components(),
        accepted_statement="A durable workflow tool.",
        accepted_vision_fingerprint="sha256:" + "0" * 64,
        revision_reason="Clarify the target user.",
        active_product_goal_status="none",
        prior_review_feedback=None,
    )

    with pytest.raises(ValidationError, match="preflight"):
        VisionAgentInput(
            request=revision,
            preflight=VisionPreflight(
                expected_evidence_fingerprint="sha256:" + "0" * 64,
                observed_evidence=_evidence_bundle(content="Fresh repository context"),
            ),
        )


def test_revision_contract_prohibits_active_product_goal() -> None:
    """Revisions cannot proceed after Product Goal activation."""
    values: dict[str, object] = {
        "schema_version": "agileforge.vision-input.v1",
        "operation": "revision",
        "project_name": "AgileForge",
        "project_description": None,
        "evidence": _evidence_bundle(),
        "accepted_components": _components(),
        "accepted_statement": "A durable workflow tool.",
        "accepted_vision_fingerprint": "sha256:" + "0" * 64,
        "revision_reason": "Clarify the target user.",
        "active_product_goal_status": "none",
        "prior_review_feedback": None,
    }

    assert VisionRevisionInput(**values).active_product_goal_status == "none"
    with pytest.raises(ValidationError, match="active_product_goal_status"):
        VisionRevisionInput.model_validate(
            values | {"active_product_goal_status": "accepted"}
        )


def test_draft_output_exposes_only_vision_fields() -> None:
    """Drafts exclude Product Goal and delivery-plan fields."""
    expected = {
        "schema_version",
        "components",
        "component_basis",
        "draft_statement",
        "assumptions",
        "conflicts",
        "clarifying_questions",
        "is_complete",
    }

    assert set(VisionDraftOutput.model_fields) == expected
    with pytest.raises(ValidationError, match="extra"):
        VisionDraftOutput.model_validate(
            {
                "schema_version": "agileforge.vision-draft.v1",
                "components": _components(),
                "component_basis": (),
                "draft_statement": "A durable workflow tool.",
                "assumptions": (),
                "conflicts": (),
                "clarifying_questions": (),
                "is_complete": True,
                "product_goal": "Forbidden downstream field.",
            }
        )
