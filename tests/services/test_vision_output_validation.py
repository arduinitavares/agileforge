"""Semantic validation tests for grounded Vision drafts."""

from __future__ import annotations

import pytest

from services.contracts.vision import (
    VisionAssumption,
    VisionBootstrapInput,
    VisionClarificationInput,
    VisionClarifyingQuestion,
    VisionComponentBasis,
    VisionComponentName,
    VisionComponents,
    VisionConflict,
    VisionDraftOutput,
)
from services.contracts.vision_evidence import VisionEvidenceBundle, VisionEvidenceItem
from services.vision_output_validation import (
    VisionDraftValidationError,
    validate_vision_draft,
)
from workflow.fingerprints import canonical_hash

COMPONENT_NAMES: tuple[VisionComponentName, ...] = (
    "project_name",
    "target_user",
    "problem",
    "product_category",
    "key_benefit",
    "competitors",
    "differentiator",
)


def _components(**overrides: str | None) -> VisionComponents:
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


def _bundle(*, duplicate_item: bool = False) -> VisionEvidenceBundle:
    content = "Repository context"
    item = VisionEvidenceItem(
        evidence_id="file:README.md",
        kind="readme",
        relative_path="README.md",
        content_fingerprint=canonical_hash(content),
        trust="unreviewed_repository_evidence",
        content=content,
        truncated=False,
    )
    items = (item, item) if duplicate_item else (item,)
    return VisionEvidenceBundle(
        schema_version="agileforge.vision-evidence.v1",
        items=items,
        warnings=(),
        evidence_fingerprint=canonical_hash(
            {
                "schema_version": "agileforge.vision-evidence.v1",
                "items": [entry.model_dump(mode="json") for entry in items],
                "warnings": [],
            }
        ),
    )


def bootstrap_input(*, duplicate_item: bool = False) -> VisionBootstrapInput:
    """Build a bootstrap request with optional duplicate evidence identity."""
    return VisionBootstrapInput(
        schema_version="agileforge.vision-input.v1",
        operation="bootstrap",
        project_name="AgileForge",
        project_description=None,
        evidence=_bundle(duplicate_item=duplicate_item),
    )


def _basis(component: VisionComponentName, **overrides: object) -> VisionComponentBasis:
    values: dict[str, object] = {
        "component": component,
        "source_kinds": ("evidence",),
        "evidence_ids": ("file:README.md",),
        "assumption_ids": (),
    }
    values.update(overrides)
    return VisionComponentBasis(**values)


def complete_draft_output(**overrides: object) -> VisionDraftOutput:
    """Build a fully evidenced complete draft with focused overrides."""
    components = _components()
    values: dict[str, object] = {
        "schema_version": "agileforge.vision-draft.v1",
        "components": components,
        "component_basis": tuple(_basis(name) for name in COMPONENT_NAMES),
        "draft_statement": "A durable workflow tool.",
        "assumptions": (),
        "conflicts": (),
        "clarifying_questions": (),
        "is_complete": True,
    }
    values.update(overrides)
    return VisionDraftOutput(**values)


def clarification_input() -> VisionClarificationInput:
    """Build a clarification request against an existing snapshot."""
    question = VisionClarifyingQuestion(
        question_id="question:audience",
        text="Who benefits first?",
        affected_components=("target_user",),
    )
    return VisionClarificationInput(
        schema_version="agileforge.vision-input.v1",
        operation="clarification",
        project_name="AgileForge",
        project_description=None,
        vision_evidence_snapshot_id=1,
        evidence=_bundle(),
        current_components=_components(problem=None),
        current_statement="A workflow tool.",
        current_component_basis=(),
        current_assumptions=(),
        current_conflicts=(),
        current_questions=(question,),
        human_response="Product operators benefit first.",
        addressed_question_ids=(question.question_id,),
    )


def test_complete_draft_requires_no_open_questions() -> None:
    """Complete drafts leave no pending clarification question."""
    output = complete_draft_output()
    output.clarifying_questions = (
        VisionClarifyingQuestion(
            question_id="question:audience",
            text="Who benefits first?",
            affected_components=("target_user",),
        ),
    )

    with pytest.raises(VisionDraftValidationError, match="complete"):
        validate_vision_draft(output, bootstrap_input())


def test_human_basis_requires_human_input_in_lineage() -> None:
    """Bootstrap evidence cannot claim a human source."""
    output = complete_draft_output(
        component_basis=(
            _basis("project_name", source_kinds=("human",), evidence_ids=()),
            *(_basis(name) for name in COMPONENT_NAMES if name != "project_name"),
        )
    )

    with pytest.raises(VisionDraftValidationError, match="human"):
        validate_vision_draft(output, bootstrap_input())


def test_complete_draft_with_valid_evidence_basis_passes() -> None:
    """A complete draft with valid evidence provenance is accepted."""
    validate_vision_draft(complete_draft_output(), bootstrap_input())


def test_incomplete_draft_requires_a_question() -> None:
    """An incomplete draft directs the next human response."""
    output = complete_draft_output(
        components=_components(problem=None),
        component_basis=tuple(
            _basis(name) for name in COMPONENT_NAMES if name != "problem"
        ),
        is_complete=False,
    )

    with pytest.raises(VisionDraftValidationError, match="incomplete"):
        validate_vision_draft(output, bootstrap_input())


@pytest.mark.parametrize("collection", ["assumptions", "conflicts", "questions"])
def test_duplicate_output_ids_are_rejected(collection: str) -> None:
    """Each output identity collection must be unique."""
    assumption = VisionAssumption(
        assumption_id="assumption:audience",
        text="Operators are the first audience.",
        affected_components=("target_user",),
    )
    conflict = VisionConflict(
        conflict_id="conflict:audience",
        text="Audience is disputed.",
        status="resolved",
        affected_components=("target_user",),
        resolution="Use operators.",
    )
    question = VisionClarifyingQuestion(
        question_id="question:audience",
        text="Who benefits first?",
        affected_components=("target_user",),
    )
    values: dict[str, object] = {
        "assumptions": (assumption, assumption),
        "conflicts": (conflict, conflict),
        "questions": (question, question),
    }
    output = complete_draft_output(
        **{
            collection if collection != "questions" else "clarifying_questions": values[
                collection
            ]
        }
    )

    with pytest.raises(VisionDraftValidationError, match="duplicate"):
        validate_vision_draft(output, bootstrap_input())


def test_unknown_references_and_missing_basis_rows_are_rejected() -> None:
    """References and basis rows must resolve against the draft lineage."""
    unknown_evidence = complete_draft_output(
        component_basis=(
            _basis("project_name", evidence_ids=("file:missing.md",)),
            *(_basis(name) for name in COMPONENT_NAMES if name != "project_name"),
        )
    )
    missing_basis = complete_draft_output(component_basis=())

    with pytest.raises(VisionDraftValidationError, match="unknown evidence"):
        validate_vision_draft(unknown_evidence, bootstrap_input())
    with pytest.raises(VisionDraftValidationError, match="exactly one basis"):
        validate_vision_draft(missing_basis, bootstrap_input())


def test_basis_requires_references_matching_its_declared_sources() -> None:
    """Basis IDs are allowed only for the declared source kinds."""
    evidence_without_ids = complete_draft_output(
        component_basis=(
            _basis("project_name", evidence_ids=()),
            *(_basis(name) for name in COMPONENT_NAMES if name != "project_name"),
        )
    )
    inference_without_assumptions = complete_draft_output(
        component_basis=(
            _basis(
                "project_name",
                source_kinds=("inference",),
                evidence_ids=(),
                assumption_ids=(),
            ),
            *(_basis(name) for name in COMPONENT_NAMES if name != "project_name"),
        )
    )

    with pytest.raises(VisionDraftValidationError, match="evidence"):
        validate_vision_draft(evidence_without_ids, bootstrap_input())
    with pytest.raises(VisionDraftValidationError, match="inference"):
        validate_vision_draft(inference_without_assumptions, bootstrap_input())


def test_unresolved_conflicts_require_a_linked_question() -> None:
    """Each unresolved conflict is linked to a clarification question."""
    conflict = VisionConflict(
        conflict_id="conflict:audience",
        text="Audience is disputed.",
        status="unresolved",
        affected_components=("target_user",),
    )
    question = VisionClarifyingQuestion(
        question_id="question:audience",
        text="Who benefits first?",
        affected_components=("target_user",),
    )
    output = complete_draft_output(
        conflicts=(conflict,),
        clarifying_questions=(question,),
        is_complete=False,
    )

    with pytest.raises(VisionDraftValidationError, match="unresolved"):
        validate_vision_draft(output, bootstrap_input())


def test_input_reference_invariants_are_validated() -> None:
    """Input evidence and addressed questions must be unique and known."""
    output = complete_draft_output()
    clarification = clarification_input()
    clarification.addressed_question_ids = ("question:missing",)

    with pytest.raises(VisionDraftValidationError, match="addressed"):
        validate_vision_draft(output, clarification)
    with pytest.raises(VisionDraftValidationError, match="duplicate evidence"):
        validate_vision_draft(output, bootstrap_input(duplicate_item=True))
