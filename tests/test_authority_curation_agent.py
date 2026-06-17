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
    AuthorityCurationPatch,
    AuthorityCurationRepairOutput,
    AuthorityCurationRepairPlan,
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


def test_repair_output_accepts_targeted_patch_without_full_authority() -> None:
    """ADK repair output should describe patches, not copy full authority JSON."""
    output = AuthorityCurationRepairOutput(
        mode="targeted",
        patches=[
            AuthorityCurationPatch(
                target_kind="assumption",
                target_id="ASM-39",
                op="replace_text",
                new_text=(
                    "Report contexts are required baseline examples, not "
                    "an exhaustive list."
                ),
            )
        ],
        resolved_feedback_ids=["AFB-ASM-39"],
    )

    dumped = output.model_dump(mode="json")

    assert dumped["patches"][0]["target_id"] == "ASM-39"
    assert dumped["candidate_authority_json"] is None


def test_repair_patch_rejects_unknown_fields() -> None:
    """Patch schema must stay strict so model output cannot smuggle rewrites."""
    with pytest.raises(ValidationError):
        AuthorityCurationPatch.model_validate(
            {
                "target_kind": "invariant",
                "target_id": "INV-943d18f5ecffcd3c",
                "op": "replace_text",
                "new_text": "Use qualified observational language.",
                "candidate_authority_json": {"invariants": []},
            }
        )


def test_repair_patch_accepts_structured_parameter_replacement() -> None:
    """Typed invariants should be repairable through bounded JSON paths."""
    patch = AuthorityCurationPatch(
        target_kind="invariant",
        target_id="INV-943d18f5ecffcd3c",
        op="replace_value",
        path="/parameters/rule",
        value=(
            "Use qualified observational language instead of literal token "
            "whitelists."
        ),
    )

    dumped = patch.model_dump(mode="json")

    assert dumped["path"] == "/parameters/rule"
    assert dumped["value"].startswith("Use qualified")


def test_repair_patch_value_schema_has_explicit_json_types() -> None:
    """OpenAI/Azure response_format rejects schema nodes without a type."""
    schema = AuthorityCurationPatch.model_json_schema()
    value_schema = schema["properties"]["value"]

    assert "anyOf" in value_schema
    assert all("type" in option for option in value_schema["anyOf"])


def test_repair_output_schema_does_not_request_open_candidate_json() -> None:
    """Strict providers reject arbitrary object fields in response_format."""
    schema = AuthorityCurationRepairOutput.model_json_schema()
    candidate_schema = schema["properties"]["candidate_authority_json"]
    branches = candidate_schema.get("anyOf", [candidate_schema])

    assert not any(
        option.get("type") == "object"
        and option.get("additionalProperties") is not False
        for option in branches
    )


def test_validate_workflow_input_returns_strict_model() -> None:
    """The public validator returns the strict workflow input model."""
    validated = validate_workflow_input(_valid_workflow_payload())

    assert isinstance(validated, AuthorityCurationWorkflowInput)
    assert validated.max_iterations == EXPECTED_MAX_ITERATIONS


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
    assert "Never use authority:* ids as patch targets" in compiler_instruction
    assert "ASM-*, GAP-*, or INV-*" in compiler_instruction

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
