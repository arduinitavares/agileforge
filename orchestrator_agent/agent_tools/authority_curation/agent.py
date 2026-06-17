"""ADK 2.0 workflow factory for authority curation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from google.adk.agents import LlmAgent
from google.adk.workflow import START, Workflow

from orchestrator_agent.agent_tools.authority_curation.schemes import (
    AuthorityCurationCriticOutput,
    AuthorityCurationGateDecision,
    AuthorityCurationRepairOutput,
    AuthorityCurationRepairPlan,
    AuthorityCurationWorkflowInput,
)

if TYPE_CHECKING:
    from google.adk.models.base_llm import BaseLlm

AUTHORITY_CURATION_STATE_INPUT = "authority_curation_input"
AUTHORITY_CURATION_STATE_SEMANTIC_FINDINGS = "authority_curation_semantic_findings"
AUTHORITY_CURATION_STATE_QUALITY_FINDINGS = "authority_curation_quality_findings"
AUTHORITY_CURATION_STATE_REPAIR_PLAN = "authority_curation_repair_plan"
AUTHORITY_CURATION_STATE_REPAIR_OUTPUT = "authority_curation_repair_output"
AUTHORITY_CURATION_STATE_GATE = "authority_curation_gate_decision"
AUTHORITY_CURATION_MAX_ITERATIONS = 2
AUTHORITY_CURATION_INPUT_PLACEHOLDER = f"{{{AUTHORITY_CURATION_STATE_INPUT}}}"
AUTHORITY_CURATION_SEMANTIC_FINDINGS_PLACEHOLDER = (
    f"{{{AUTHORITY_CURATION_STATE_SEMANTIC_FINDINGS}}}"
)
AUTHORITY_CURATION_QUALITY_FINDINGS_PLACEHOLDER = (
    f"{{{AUTHORITY_CURATION_STATE_QUALITY_FINDINGS}}}"
)
AUTHORITY_CURATION_REPAIR_PLAN_PLACEHOLDER = (
    f"{{{AUTHORITY_CURATION_STATE_REPAIR_PLAN}}}"
)
AUTHORITY_CURATION_REPAIR_OUTPUT_PLACEHOLDER = (
    f"{{{AUTHORITY_CURATION_STATE_REPAIR_OUTPUT}}}"
)

_LOOP_CONTRACT = (
    "This workflow runs as a bounded curation loop with max_iterations="
    f"{AUTHORITY_CURATION_MAX_ITERATIONS}. Prefer targeted repair. Preserve "
    "untouched accepted authority invariants and source mappings exactly. Use "
    "the source authority JSON and structured feedback from "
    f"{AUTHORITY_CURATION_INPUT_PLACEHOLDER} as the only repair source. If a gap "
    "cannot be repaired from the provided candidate and feedback, return an "
    "explicit fail or unresolved gap reason."
)


def build_authority_curation_workflow(*, model: str | BaseLlm) -> Workflow:
    """Build an ADK 2.0 workflow preserving Loop template semantics."""
    semantic_critic = LlmAgent(
        name="AuthoritySemanticFidelityCritic",
        model=model,
        include_contents="none",
        input_schema=AuthorityCurationWorkflowInput,
        output_schema=AuthorityCurationCriticOutput,
        output_key=AUTHORITY_CURATION_STATE_SEMANTIC_FINDINGS,
        instruction=(
            "Review the authority candidate against structured feedback for "
            "semantic fidelity. Emit strict JSON with a findings array. Flag "
            "overstrong, materially wrong, brittle, duplicate, missing, and "
            "source-misaligned authority issues. Do not propose broad rewrites "
            f"when feedback names a bounded target.\n\n{_LOOP_CONTRACT}"
        ),
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )
    quality_critic = LlmAgent(
        name="AuthorityQualityCritic",
        model=model,
        include_contents="none",
        output_schema=AuthorityCurationCriticOutput,
        output_key=AUTHORITY_CURATION_STATE_QUALITY_FINDINGS,
        instruction=(
            "Review authority quality groups and emit strict JSON with a "
            "findings array. Identify over-split rules, near duplicates, "
            "unresolved gaps, weak traceability, hidden assumptions, and any "
            "feedback that remains unsafe to mark resolved.\n\n"
            f"{_LOOP_CONTRACT}"
        ),
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )
    repair_planner = LlmAgent(
        name="AuthorityRepairPlanner",
        model=model,
        include_contents="none",
        output_schema=AuthorityCurationRepairPlan,
        output_key=AUTHORITY_CURATION_STATE_REPAIR_PLAN,
        instruction=(
            "Create a bounded repair plan from host input at "
            f"{AUTHORITY_CURATION_INPUT_PLACEHOLDER}, semantic findings at "
            f"{AUTHORITY_CURATION_SEMANTIC_FINDINGS_PLACEHOLDER}, and quality "
            f"findings at {AUTHORITY_CURATION_QUALITY_FINDINGS_PLACEHOLDER}. "
            "Prefer mode 'targeted' with explicit target_ids and feedback_ids. "
            "Use 'full_recompile' only when target isolation is impossible. "
            "Use 'fail_no_candidate' when the candidate or evidence is "
            f"insufficient, and explain the precise gap in reason.\n\n"
            f"{_LOOP_CONTRACT}"
        ),
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )
    repair_compiler = LlmAgent(
        name="AuthorityTargetedRepairCompiler",
        model=model,
        include_contents="none",
        output_schema=AuthorityCurationRepairOutput,
        output_key=AUTHORITY_CURATION_STATE_REPAIR_OUTPUT,
        instruction=(
            "Apply only the repair plan at "
            f"{AUTHORITY_CURATION_REPAIR_PLAN_PLACEHOLDER}, using semantic "
            f"findings at {AUTHORITY_CURATION_SEMANTIC_FINDINGS_PLACEHOLDER} "
            "and quality findings at "
            f"{AUTHORITY_CURATION_QUALITY_FINDINGS_PLACEHOLDER}. For "
            "authority_curation.v2 inputs, return only RepairSelectionPayload "
            "selections. Emit one repair selection for every repair_menu "
            "entry. Pick target_handle values exactly from the repair_menu "
            "handle field, such as R1 or R2. Never use ASM-*, GAP-*, INV-*, "
            "authority:*, or collection-index ids as target_handle values. "
            "Do not emit target_id, target_kind, op, path, value, patches, or "
            "candidate_authority_json. Use replace_text only when a menu "
            "entry allows replace_text. Use mark_unresolvable with a reason "
            "when feedback cannot be safely repaired from the menu. Mark "
            "unresolved_feedback_ids and failure_reason instead of inventing "
            f"authority for missing evidence.\n\n"
            f"{_LOOP_CONTRACT}"
        ),
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )
    gate_decision = LlmAgent(
        name="AuthorityGateDecision",
        model=model,
        include_contents="none",
        output_schema=AuthorityCurationGateDecision,
        output_key=AUTHORITY_CURATION_STATE_GATE,
        instruction=(
            "Decide pass, retry, or fail for the loop iteration from repair "
            f"output at {AUTHORITY_CURATION_REPAIR_OUTPUT_PLACEHOLDER}, repair "
            f"plan at {AUTHORITY_CURATION_REPAIR_PLAN_PLACEHOLDER}, semantic "
            f"findings at {AUTHORITY_CURATION_SEMANTIC_FINDINGS_PLACEHOLDER}, "
            "and quality findings at "
            f"{AUTHORITY_CURATION_QUALITY_FINDINGS_PLACEHOLDER}. Pass only "
            "when all blocking feedback is resolved and host-validatable "
            "repaired authority exists. Retry only when another bounded "
            "iteration can close explicit unresolved feedback. The "
            "unresolved_feedback_ids list may contain only original feedback "
            "ids or unresolved ids reported by the repair output; do not "
            "invent ids for compiler gaps, assumptions, or unrelated review "
            "notes. Compiler gaps not named by structured feedback are "
            "non-blocking curation context. Fail with reason when recorded "
            "feedback remains unresolved, evidence is missing for a recorded "
            f"target, or max loop semantics prevent another safe repair.\n\n"
            f"{_LOOP_CONTRACT}"
        ),
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )

    return Workflow(
        name="AuthorityCurationWorkflow",
        edges=[
            (
                START,
                semantic_critic,
                quality_critic,
                repair_planner,
                repair_compiler,
                gate_decision,
            ),
        ],
        max_concurrency=1,
        input_schema=AuthorityCurationWorkflowInput,
        output_schema=AuthorityCurationGateDecision,
        description=(
            "ADK 2.0 authority curation workflow using graph execution and "
            "ordered Loop template semantics for targeted repair and explicit "
            "gate decisions."
        ),
    )


def workflow_sub_agents(workflow: Workflow) -> list[LlmAgent]:
    """Return ordered LLM graph nodes for tests and diagnostics."""
    if workflow.graph is None:
        return []
    expected_names = [
        "AuthoritySemanticFidelityCritic",
        "AuthorityQualityCritic",
        "AuthorityRepairPlanner",
        "AuthorityTargetedRepairCompiler",
        "AuthorityGateDecision",
    ]
    nodes_by_name = {
        node.name: node
        for node in workflow.graph.nodes
        if isinstance(node, LlmAgent) and node.name in expected_names
    }
    return [nodes_by_name[name] for name in expected_names if name in nodes_by_name]


def validate_workflow_input(
    payload: dict[str, object],
) -> AuthorityCurationWorkflowInput:
    """Validate host input before invoking ADK."""
    return AuthorityCurationWorkflowInput.model_validate(payload)
