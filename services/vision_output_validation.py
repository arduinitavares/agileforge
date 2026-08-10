"""Pure semantic validation for strict, context-grounded Vision drafts."""

from __future__ import annotations

from services.contracts.vision import (
    VisionClarificationInput,
    VisionDraftOutput,
    VisionOperationInput,
    VisionRevisionInput,
)


class VisionDraftValidationError(ValueError):
    """All deterministic findings for one invalid Vision draft."""

    def __init__(self, findings: tuple[str, ...]) -> None:
        """Expose every finding in a readable exception message."""
        self.findings = findings
        super().__init__("; ".join(findings))


def _duplicate_ids(values: tuple[str, ...]) -> set[str]:
    """Return identifiers repeated within one collection."""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _duplicate_findings(
    output: VisionDraftOutput,
    input_payload: VisionOperationInput,
) -> list[str]:
    """Collect duplicate identity findings across input and output collections."""
    collections = (
        ("evidence", tuple(item.evidence_id for item in input_payload.evidence.items)),
        ("assumption", tuple(item.assumption_id for item in output.assumptions)),
        ("conflict", tuple(item.conflict_id for item in output.conflicts)),
        ("question", tuple(item.question_id for item in output.clarifying_questions)),
    )
    return [
        f"duplicate {label} IDs: {sorted(duplicates)}"
        for label, values in collections
        if (duplicates := _duplicate_ids(values))
    ]


def _reference_findings(
    output: VisionDraftOutput,
    input_payload: VisionOperationInput,
) -> list[str]:
    """Collect every reference that is absent from its owning collection."""
    findings: list[str] = []
    evidence_ids = {item.evidence_id for item in input_payload.evidence.items}
    assumption_ids = {item.assumption_id for item in output.assumptions}
    conflict_ids = {item.conflict_id for item in output.conflicts}
    findings.extend(
        f"unknown evidence ID in basis: {evidence_id}"
        for basis in output.component_basis
        for evidence_id in basis.evidence_ids
        if evidence_id not in evidence_ids
    )
    findings.extend(
        f"unknown assumption ID in basis: {assumption_id}"
        for basis in output.component_basis
        for assumption_id in basis.assumption_ids
        if assumption_id not in assumption_ids
    )
    findings.extend(
        f"unknown evidence ID in conflict: {evidence_id}"
        for conflict in output.conflicts
        for evidence_id in conflict.evidence_ids
        if evidence_id not in evidence_ids
    )
    findings.extend(
        f"unknown assumption ID in conflict: {assumption_id}"
        for conflict in output.conflicts
        for assumption_id in conflict.assumption_ids
        if assumption_id not in assumption_ids
    )
    findings.extend(
        f"unknown conflict ID in question: {conflict_id}"
        for question in output.clarifying_questions
        for conflict_id in question.conflict_ids
        if conflict_id not in conflict_ids
    )
    if isinstance(input_payload, VisionClarificationInput):
        current_question_ids = {
            question.question_id for question in input_payload.current_questions
        }
        findings.extend(
            f"unknown addressed question ID: {question_id}"
            for question_id in input_payload.addressed_question_ids
            if question_id not in current_question_ids
        )
    return findings


def _basis_presence_findings(output: VisionDraftOutput) -> list[str]:
    """Require one basis row for every substantive component and none for nulls."""
    findings: list[str] = []
    component_values = output.components.model_dump()
    for component, value in component_values.items():
        matching_rows = [
            basis for basis in output.component_basis if basis.component == component
        ]
        if value is None and matching_rows:
            findings.append(f"null component {component} must not have a basis row")
        if value is not None and len(matching_rows) != 1:
            findings.append(
                f"non-null component {component} requires exactly one basis row"
            )
    return findings


def _basis_source_findings(
    output: VisionDraftOutput,
    input_payload: VisionOperationInput,
) -> list[str]:
    """Validate evidence, inference, and human basis prerequisites."""
    findings: list[str] = []
    human_input_available = isinstance(
        input_payload, VisionClarificationInput | VisionRevisionInput
    )
    for basis in output.component_basis:
        has_evidence = "evidence" in basis.source_kinds
        has_inference = "inference" in basis.source_kinds
        if has_evidence and not basis.evidence_ids:
            findings.append(
                f"evidence basis for {basis.component} requires evidence IDs"
            )
        if not has_evidence and basis.evidence_ids:
            findings.append(
                f"basis for {basis.component} has evidence IDs without evidence"
            )
        if has_inference and not basis.assumption_ids:
            findings.append(
                f"inference basis for {basis.component} requires assumption IDs"
            )
        if not has_inference and basis.assumption_ids:
            findings.append(
                f"basis for {basis.component} has assumption IDs without inference"
            )
        if "human" in basis.source_kinds and not human_input_available:
            findings.append(f"human basis for {basis.component} requires human input")
    return findings


def _completion_findings(output: VisionDraftOutput) -> list[str]:
    """Keep completion, substantive components, conflicts, and questions aligned."""
    resolved_conflicts = all(
        conflict.status == "resolved" for conflict in output.conflicts
    )
    expected_complete = (
        output.components.is_fully_defined()
        and resolved_conflicts
        and not output.clarifying_questions
    )
    findings: list[str] = []
    if output.is_complete != expected_complete:
        findings.append(
            "is_complete must match complete components, resolved conflicts, "
            "and no questions"
        )
    if not output.is_complete and not output.clarifying_questions:
        findings.append("incomplete draft requires at least one clarifying question")
    return findings


def _unresolved_conflict_findings(output: VisionDraftOutput) -> list[str]:
    """Require every unresolved conflict to be linked from an output question."""
    questioned_conflict_ids = {
        conflict_id
        for question in output.clarifying_questions
        for conflict_id in question.conflict_ids
    }
    return [
        f"unresolved conflict {conflict.conflict_id} requires a linked question"
        for conflict in output.conflicts
        if conflict.status == "unresolved"
        and conflict.conflict_id not in questioned_conflict_ids
    ]


def validate_vision_draft(
    output: VisionDraftOutput,
    input_payload: VisionOperationInput,
) -> None:
    """Validate output provenance and completion without I/O or model calls."""
    findings = [
        *_duplicate_findings(output, input_payload),
        *_reference_findings(output, input_payload),
        *_basis_presence_findings(output),
        *_basis_source_findings(output, input_payload),
        *_completion_findings(output),
        *_unresolved_conflict_findings(output),
    ]
    if findings:
        raise VisionDraftValidationError(tuple(findings))
