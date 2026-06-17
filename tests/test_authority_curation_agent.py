"""Tests for the authority curation ADK workflow package."""

from __future__ import annotations

import pytest
from google.adk.workflow import Workflow
from pydantic import ValidationError

from orchestrator_agent.agent_tools.authority_curation import (
    build_authority_curation_workflow,
    validate_workflow_input,
    workflow_sub_agents,
)
from orchestrator_agent.agent_tools.authority_curation.schemes import (
    AuthorityCurationGateDecision,
    AuthorityCurationRepairOutput,
    AuthorityCurationRepairPlan,
    AuthorityCurationRepairSelectionPayload,
    AuthorityCurationWorkflowInput,
)

EXPECTED_MAX_ITERATIONS = 2


def _instruction_text(value: object) -> str:
    assert isinstance(value, str)
    return value


def _valid_workflow_payload() -> dict[str, object]:
    return {
        "project_id": 3,
        "spec_version_id": 4,
        "source_authority_id": 6,
        "source_authority_fingerprint": "sha256:abc",
        "source_authority_json": {"invariants": []},
        "feedback_json": {"feedback_items": []},
        "max_iterations": EXPECTED_MAX_ITERATIONS,
    }


def test_authority_curation_workflow_input_rejects_unknown_fields() -> None:
    """ADK node payloads must be strict."""
    payload = _valid_workflow_payload()
    payload["extra"] = "rejected"

    with pytest.raises(ValidationError):
        AuthorityCurationWorkflowInput.model_validate(payload)


def test_authority_curation_workflow_input_rejects_scalar_coercion() -> None:
    """Strict payload validation must reject stringified integer fields."""
    payload = _valid_workflow_payload()
    payload["project_id"] = "3"

    with pytest.raises(ValidationError):
        AuthorityCurationWorkflowInput.model_validate(payload)


def test_gate_decision_requires_reason_for_fail() -> None:
    """Failing gates must explain why the loop stops."""
    with pytest.raises(ValidationError):
        AuthorityCurationGateDecision(
            status="fail",
            review_ready=False,
            unresolved_feedback_ids=["AFB-1"],
        )


def test_gate_decision_rejects_bool_coercion() -> None:
    """Strict gate validation must reject stringified boolean fields."""
    with pytest.raises(ValidationError):
        AuthorityCurationGateDecision.model_validate(
            {
                "status": "pass",
                "review_ready": "false",
            }
        )


def test_no_candidate_repair_mode_validates_across_plan_and_output() -> None:
    """No-candidate repair mode must use one spelling through the workflow."""
    plan = AuthorityCurationRepairPlan(
        mode="fail_no_candidate",
        feedback_ids=["AFB-1"],
        reason="candidate evidence is missing",
    )
    output = AuthorityCurationRepairOutput(
        mode=plan.mode,
        unresolved_feedback_ids=plan.feedback_ids,
        failure_reason=plan.reason,
    )

    assert output.mode == plan.mode


@pytest.mark.parametrize(
    "legacy_field",
    ["patches", "candidate_authority_json"],
)
def test_repair_output_rejects_legacy_fields(legacy_field: str) -> None:
    """V2 ADK repair output must not expose legacy mutation channels."""
    with pytest.raises(ValidationError):
        AuthorityCurationRepairOutput.model_validate(
            {
                "mode": "targeted",
                legacy_field: [],
                "resolved_feedback_ids": [],
                "unresolved_feedback_ids": [],
            }
        )


def test_repair_selection_payload_rejects_model_authored_targets() -> None:
    """V2 repair selections must not let the model address authority internals."""
    payload = {
        "repairs": [
            {
                "feedback_id": "AFB-1",
                "target_handle": "R1",
                "target_id": "INV-1",
                "repair_kind": "replace_text",
                "replacement_text": "Safer wording.",
            }
        ]
    }

    with pytest.raises(ValidationError):
        AuthorityCurationRepairSelectionPayload.model_validate(payload)


def test_repair_selection_payload_accepts_replace_text() -> None:
    """V2 repair selections reference host handles and replacement text only."""
    payload = AuthorityCurationRepairSelectionPayload.model_validate(
        {
            "repairs": [
                {
                    "feedback_id": "AFB-1",
                    "target_handle": "R1",
                    "repair_kind": "replace_text",
                    "replacement_text": "Safer wording.",
                }
            ]
        }
    )

    assert payload.repairs[0].target_handle == "R1"
    assert payload.repairs[0].replacement_text == "Safer wording."


def test_repair_selection_payload_rejects_replace_text_without_text() -> None:
    """Replacement text is mandatory when a selected repair replaces text."""
    with pytest.raises(ValidationError):
        AuthorityCurationRepairSelectionPayload.model_validate(
            {
                "repairs": [
                    {
                        "feedback_id": "AFB-1",
                        "target_handle": "R1",
                        "repair_kind": "replace_text",
                    }
                ]
            }
        )


def test_repair_output_schema_exposes_only_v2_selection_channel() -> None:
    """Response schema must leave no legacy patch/full-candidate escape hatch."""
    schema = AuthorityCurationRepairOutput.model_json_schema()
    properties = schema["properties"]

    assert "selection_payload" in properties
    assert "patches" not in properties
    assert "candidate_authority_json" not in properties


def test_validate_workflow_input_returns_strict_model() -> None:
    """The public validator returns the strict workflow input model."""
    validated = validate_workflow_input(_valid_workflow_payload())

    assert isinstance(validated, AuthorityCurationWorkflowInput)
    assert validated.max_iterations == EXPECTED_MAX_ITERATIONS


def test_workflow_input_accepts_not_repairable_menu_entries() -> None:
    """Host-built structural menu entries must survive strict ADK validation."""
    payload = _valid_workflow_payload()
    payload["contract_version"] = "authority_curation.v2"
    payload["repair_menu"] = [
        {
            "handle": "R1",
            "feedback_id": "AFB-param",
            "target_kind": "invariant",
            "target_id": "INV-1",
            "target_field": "text",
            "target_review_label": "INV-1",
            "overlay_target_key": "REQ-1:invariant:text:0",
            "allowed_repair_kinds": ["mark_unresolvable"],
            "target_content_hash": "sha256:" + ("a" * 64),
            "not_repairable_reason": "structural_repair_deferred",
        }
    ]

    validated = AuthorityCurationWorkflowInput.model_validate(payload)

    assert validated.repair_menu[0].not_repairable_reason == (
        "structural_repair_deferred"
    )


def test_authority_curation_workflow_uses_loop_agent_contract() -> None:
    """The factory builds an ordered ADK Workflow without invoking a model."""
    workflow = build_authority_curation_workflow(model="test-model")

    assert isinstance(workflow, Workflow)
    assert workflow.name == "AuthorityCurationWorkflow"
    assert workflow.max_concurrency == 1
    agents = workflow_sub_agents(workflow)
    assert [agent.name for agent in agents] == [
        "AuthoritySemanticFidelityCritic",
        "AuthorityQualityCritic",
        "AuthorityRepairPlanner",
        "AuthorityTargetedRepairCompiler",
        "AuthorityGateDecision",
    ]
    assert {agent.include_contents for agent in agents} == {"none"}


def test_downstream_agents_reference_state_placeholders() -> None:
    """Bounded-context agents must explicitly read prior node outputs."""
    workflow = build_authority_curation_workflow(model="test-model")
    agents = {agent.name: agent for agent in workflow_sub_agents(workflow)}

    planner_instruction = _instruction_text(
        agents["AuthorityRepairPlanner"].instruction
    )
    assert "{authority_curation_input}" in planner_instruction
    assert "{authority_curation_semantic_findings}" in planner_instruction
    assert "{authority_curation_quality_findings}" in planner_instruction

    compiler_instruction = _instruction_text(
        agents["AuthorityTargetedRepairCompiler"].instruction
    )
    assert "{authority_curation_repair_plan}" in compiler_instruction
    assert "{authority_curation_semantic_findings}" in compiler_instruction
    assert "{authority_curation_quality_findings}" in compiler_instruction
    assert "For authority_curation.v2 inputs" in compiler_instruction
    assert "Pick target_handle values exactly from the repair_menu" in (
        compiler_instruction
    )
    assert "Emit one repair selection for every repair_menu entry" in (
        compiler_instruction
    )
    assert "Never use ASM-*, GAP-*, INV-*, authority:*, or collection-index" in (
        compiler_instruction
    )
    forbidden_output_clause = (
        "Do not emit target_id, target_kind, op, path, value, patches, or "
        "candidate_authority_json"
    )
    assert forbidden_output_clause in compiler_instruction
    assert "targeted patch operations in patches" not in compiler_instruction

    gate_instruction = _instruction_text(agents["AuthorityGateDecision"].instruction)
    assert "{authority_curation_repair_output}" in gate_instruction
    assert "{authority_curation_repair_plan}" in gate_instruction
    assert "{authority_curation_semantic_findings}" in gate_instruction
    assert "{authority_curation_quality_findings}" in gate_instruction
    assert "unresolved_feedback_ids list may contain only original feedback ids" in (
        gate_instruction
    )
    assert "Compiler gaps not named by structured feedback are non-blocking" in (
        gate_instruction
    )
