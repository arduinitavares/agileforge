"""Pure authority workflow shared by initial and extension specifications."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from workflow.contracts import (
    GRAPH_VERSION,
    FactReference,
    InputField,
    RecommendationKind,
)
from workflow.graph import (
    AgenticExecutionSpec,
    ChildGraphSpec,
    NodeSpec,
    RuleCategory,
    RuleEvaluation,
    WorkflowGraph,
)

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.facts import (
        AuthorityFact,
        ReviewDecisionFact,
        SpecVersionFact,
        WorkflowFactSnapshot,
    )


@dataclass(frozen=True)
class _AuthorityState:
    """Validated authority state for the one current registered spec."""

    spec: SpecVersionFact | None
    candidate: AuthorityFact | None
    decision: ReviewDecisionFact | None
    conflict: bool


def _evaluation(
    category: RuleCategory,
    reason_code: str,
    *,
    fact_references: tuple[FactReference, ...] = (),
    valid_until: datetime | None = None,
    recommendation_kind: RecommendationKind | None = None,
) -> tuple[RuleEvaluation, ...]:
    return (
        RuleEvaluation(
            category=category,
            reason_code=reason_code,
            fact_references=fact_references,
            valid_until=valid_until,
            recommendation_kind=recommendation_kind,
        ),
    )


def _invalid() -> tuple[RuleEvaluation, ...]:
    return _evaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT")


def _for_instance(
    evaluations: tuple[RuleEvaluation, ...],
    instance_key: str,
) -> tuple[RuleEvaluation, ...]:
    return (replace(evaluations[0], instance_key=instance_key),)


def _reference(fact_type: str, fact_id: int, fingerprint: str) -> FactReference:
    return FactReference(
        fact_type=fact_type,
        fact_id=str(fact_id),
        fingerprint=fingerprint,
    )


def _decision_for_authority(
    snapshot: WorkflowFactSnapshot,
    authority: AuthorityFact,
) -> tuple[ReviewDecisionFact | None, bool]:
    decisions = tuple(
        decision
        for decision in snapshot.review_decisions
        if decision.artifact_type == "authority"
        and decision.artifact_id == authority.authority_id
    )
    conflict = len(decisions) > 1 or any(
        decision.artifact_fingerprint != authority.authority_fingerprint
        for decision in decisions
    )
    decision = decisions[0] if len(decisions) == 1 else None
    expected_status = "pending_review" if decision is None else decision.decision
    return decision, conflict or authority.status != expected_status


def _authority_state(snapshot: WorkflowFactSnapshot) -> _AuthorityState:
    approved = tuple(
        spec for spec in snapshot.spec_versions if spec.status == "approved"
    )
    if len(approved) != 1:
        return _AuthorityState(None, None, None, bool(approved))
    spec = approved[0]
    candidates = tuple(
        authority
        for authority in snapshot.authorities
        if authority.spec_version_id == spec.spec_version_id
    )
    candidate_ids = {authority.authority_id for authority in snapshot.authorities}
    if any(
        decision.artifact_type == "authority"
        and decision.artifact_id not in candidate_ids
        for decision in snapshot.review_decisions
    ):
        return _AuthorityState(spec, None, None, True)

    decisions_by_authority: dict[int, ReviewDecisionFact | None] = {}
    for authority in candidates:
        decision, conflict = _decision_for_authority(snapshot, authority)
        if conflict:
            return _AuthorityState(spec, None, None, True)
        decisions_by_authority[authority.authority_id] = decision

    if not candidates:
        return _AuthorityState(spec, None, None, False)
    candidate = max(candidates, key=lambda item: item.authority_id)
    for older in candidates:
        if older.authority_id == candidate.authority_id:
            continue
        decision = decisions_by_authority[older.authority_id]
        if decision is None or decision.decision != "rejected":
            return _AuthorityState(spec, None, None, True)
    return _AuthorityState(
        spec,
        candidate,
        decisions_by_authority[candidate.authority_id],
        False,
    )


def accepted_current_authority(
    snapshot: WorkflowFactSnapshot,
) -> tuple[AuthorityFact | None, bool]:
    """Return the exact accepted authority for the one current spec."""
    state = _authority_state(snapshot)
    if state.conflict:
        return None, True
    if (
        state.candidate is None
        or state.decision is None
        or state.decision.decision != "accepted"
        or state.candidate.status != "accepted"
    ):
        return None, False
    return state.candidate, False


def _compile_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = _authority_state(snapshot)
    if state.conflict:
        return _invalid()
    if state.spec is None:
        return _evaluation(RuleCategory.WAITING, "WAITING_FOR_REGISTERED_SPEC")
    spec_reference = (
        _reference("spec_version", state.spec.spec_version_id, state.spec.spec_hash),
    )
    instance_key = f"spec:{state.spec.spec_version_id}:{state.spec.spec_hash}"
    if state.candidate is not None:
        return _for_instance(
            _evaluation(RuleCategory.SATISFIED, "AUTHORITY_COMPILED"),
            instance_key,
        )
    return _for_instance(
        _evaluation(
            RuleCategory.AVAILABLE,
            "AUTHORITY_COMPILE_REQUIRED",
            fact_references=spec_reference,
        ),
        instance_key,
    )


def _review_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = _authority_state(snapshot)
    if state.conflict:
        return _invalid()
    if state.candidate is None or state.decision is not None:
        return _evaluation(RuleCategory.SATISFIED, "AUTHORITY_REVIEW_NOT_PENDING")
    return _evaluation(
        RuleCategory.WAITING,
        "AUTHORITY_REVIEW_REQUIRED",
        fact_references=(
            _reference(
                "authority",
                state.candidate.authority_id,
                state.candidate.authority_fingerprint,
            ),
        ),
    )


def _feedback_for_candidate(
    snapshot: WorkflowFactSnapshot,
    candidate: AuthorityFact,
) -> bool:
    return any(
        feedback.source_authority_id == candidate.authority_id
        and feedback.source_authority_fingerprint == candidate.authority_fingerprint
        for feedback in snapshot.authority_feedback
    )


def _feedback_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = _authority_state(snapshot)
    if state.conflict:
        return _invalid()
    if (
        state.candidate is None
        or state.decision is None
        or state.decision.decision != "rejected"
    ):
        return _evaluation(RuleCategory.SATISFIED, "AUTHORITY_FEEDBACK_NOT_REQUIRED")
    if _feedback_for_candidate(snapshot, state.candidate):
        return _evaluation(RuleCategory.SATISFIED, "AUTHORITY_FEEDBACK_RECORDED")
    return _evaluation(
        RuleCategory.AVAILABLE,
        "AUTHORITY_FEEDBACK_REQUIRED",
        fact_references=(
            _reference(
                "authority",
                state.candidate.authority_id,
                state.candidate.authority_fingerprint,
            ),
        ),
    )


def _repair_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = _authority_state(snapshot)
    if state.conflict:
        return _invalid()
    if (
        state.candidate is None
        or state.decision is None
        or state.decision.decision != "rejected"
        or not _feedback_for_candidate(snapshot, state.candidate)
    ):
        return _evaluation(RuleCategory.SATISFIED, "AUTHORITY_REPAIR_NOT_READY")
    return _evaluation(
        RuleCategory.AVAILABLE,
        "AUTHORITY_REPAIR_REQUIRED",
        fact_references=(
            _reference(
                "authority",
                state.candidate.authority_id,
                state.candidate.authority_fingerprint,
            ),
        ),
    )


AUTHORITY_NODES: tuple[NodeSpec, ...] = (
    NodeSpec(
        node_id="authority.compile",
        child_graph_id="authority",
        request_kind="compile_authority",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="spec_version_id", value_type="integer"),
            InputField(name="expected_spec_hash", value_type="string"),
            InputField(name="compiler_model", value_type="string"),
        ),
        evaluate_rule=_compile_rule,
        agentic_execution=AgenticExecutionSpec(
            active_reason="AUTHORITY_COMPILE_ACTIVE",
            failure_reason="AUTHORITY_COMPILE_FAILED",
            recovery_reason="AUTHORITY_COMPILE_RECOVERY_REQUIRED",
        ),
    ),
    NodeSpec(
        node_id="authority.review",
        child_graph_id="authority",
        request_kind="decide_authority",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="pending_authority_id", value_type="integer"),
            InputField(name="authority_fingerprint", value_type="string"),
            InputField(name="review_fingerprint", value_type="string"),
            InputField(name="decision", value_type="string"),
            InputField(name="rationale", value_type="string"),
        ),
        evaluate_rule=_review_rule,
    ),
    NodeSpec(
        node_id="authority.feedback",
        child_graph_id="authority",
        request_kind="record_authority_feedback",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="pending_authority_id", value_type="integer"),
            InputField(name="authority_fingerprint", value_type="string"),
            InputField(name="feedback", value_type="object"),
        ),
        evaluate_rule=_feedback_rule,
    ),
    NodeSpec(
        node_id="authority.repair",
        child_graph_id="authority",
        request_kind="repair_authority",
        recommendation_kind=RecommendationKind.RECOVERY,
        required_inputs=(
            InputField(name="source_authority_id", value_type="integer"),
            InputField(name="source_authority_fingerprint", value_type="string"),
        ),
        evaluate_rule=_repair_rule,
        agentic_execution=AgenticExecutionSpec(
            active_reason="AUTHORITY_REPAIR_ACTIVE",
            failure_reason="AUTHORITY_REPAIR_FAILED",
            recovery_reason="AUTHORITY_REPAIR_RECOVERY_REQUIRED",
        ),
    ),
)


def authority_graph() -> WorkflowGraph:
    """Return the isolated Authority graph."""
    return WorkflowGraph(
        graph_version=GRAPH_VERSION,
        root=ChildGraphSpec(
            child_graph_id="product_lifecycle",
            nodes=(),
            children=(
                ChildGraphSpec(child_graph_id="authority", nodes=AUTHORITY_NODES),
            ),
        ),
    )


__all__ = [
    "AUTHORITY_NODES",
    "accepted_current_authority",
    "authority_graph",
]
