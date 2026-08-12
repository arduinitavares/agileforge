"""Pure direct Vision/Goal selectors for specification authoring and review."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from workflow.contracts import FactReference, InputField, RecommendationKind
from workflow.definitions.product_goal import (
    accepted_current_goal,
    accepted_current_vision,
    lifecycle_is_quiescent,
)
from workflow.graph import AgenticExecutionSpec, NodeSpec, RuleCategory, RuleEvaluation

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.facts import (
        ProductGoalArtifactFact,
        SpecificationCandidateFact,
        SpecVersionFact,
        VisionArtifactFact,
        WorkflowFactSnapshot,
    )


def _reference(kind: str, identifier: int, fingerprint: str) -> FactReference:
    return FactReference(
        fact_type=kind,
        fact_id=str(identifier),
        fingerprint=fingerprint,
    )


def _lineage_references(
    vision: VisionArtifactFact,
    goal: ProductGoalArtifactFact,
) -> tuple[FactReference, FactReference]:
    return (
        _reference("vision", vision.vision_artifact_id, vision.content_fingerprint),
        _reference(
            "product_goal", goal.product_goal_artifact_id, goal.content_fingerprint
        ),
    )


def _direct_candidates(
    snapshot: WorkflowFactSnapshot,
) -> tuple[tuple[SpecificationCandidateFact, ...], bool]:
    """Return unsuperseded candidates for the accepted Vision/Goal pair."""
    vision = accepted_current_vision(snapshot)
    goal = accepted_current_goal(snapshot)
    if vision is None or goal is None:
        return (), False
    superseded = {
        candidate.supersedes_specification_candidate_id
        for candidate in snapshot.specification_candidates
        if candidate.supersedes_specification_candidate_id is not None
    }
    direct: list[SpecificationCandidateFact] = []
    conflict = False
    for candidate in snapshot.specification_candidates:
        if candidate.specification_candidate_id in superseded:
            continue
        lineage = (
            candidate.vision_artifact_id,
            candidate.vision_fingerprint,
            candidate.product_goal_artifact_id,
            candidate.product_goal_fingerprint,
        )
        expected = (
            vision.vision_artifact_id,
            vision.content_fingerprint,
            goal.product_goal_artifact_id,
            goal.content_fingerprint,
        )
        if lineage == expected:
            direct.append(candidate)
        elif candidate.product_goal_artifact_id == goal.product_goal_artifact_id:
            conflict = True
    return tuple(direct), conflict


def _candidate_decision(
    snapshot: WorkflowFactSnapshot,
    candidate: SpecificationCandidateFact,
) -> tuple[str | None, bool]:
    """Return the sole decision bound to an exact candidate fingerprint."""
    decisions = [
        decision
        for decision in snapshot.specification_decisions
        if decision.specification_candidate_id == candidate.specification_candidate_id
    ]
    exact = [
        decision
        for decision in decisions
        if decision.candidate_fingerprint == candidate.candidate_fingerprint
    ]
    return (exact[0].decision if len(exact) == 1 else None), (
        len(decisions) != len(exact) or len(exact) > 1
    )


def _approved_spec_for_candidate(
    snapshot: WorkflowFactSnapshot,
    candidate: SpecificationCandidateFact,
) -> tuple[SpecVersionFact | None, bool]:
    """Return the one approved SpecRegistry row sourced by this candidate."""
    choices = [
        spec
        for spec in snapshot.spec_versions
        if spec.status == "approved"
        and (
            spec.source_specification_candidate_id,
            spec.source_specification_candidate_fingerprint,
            spec.source_vision_artifact_id,
            spec.source_vision_fingerprint,
            spec.source_product_goal_artifact_id,
            spec.source_product_goal_fingerprint,
        )
        == (
            candidate.specification_candidate_id,
            candidate.candidate_fingerprint,
            candidate.vision_artifact_id,
            candidate.vision_fingerprint,
            candidate.product_goal_artifact_id,
            candidate.product_goal_fingerprint,
        )
    ]
    malformed = any(
        spec.status == "approved"
        and spec.source_specification_candidate_id
        == candidate.specification_candidate_id
        and spec.source_specification_candidate_fingerprint
        != candidate.candidate_fingerprint
        for spec in snapshot.spec_versions
    )
    return (choices[0] if len(choices) == 1 else None), malformed or len(choices) > 1


def _has_superseded_spec(
    snapshot: WorkflowFactSnapshot,
    candidate: SpecificationCandidateFact,
) -> bool:
    """Return whether this accepted candidate's registry row is no longer current."""
    return any(
        spec.status == "superseded"
        and spec.source_specification_candidate_id
        == candidate.specification_candidate_id
        and spec.source_specification_candidate_fingerprint
        == candidate.candidate_fingerprint
        for spec in snapshot.spec_versions
    )


def _candidate_rank(
    snapshot: WorkflowFactSnapshot,
    candidate: SpecificationCandidateFact,
) -> tuple[int, bool]:
    """Rank direct candidates without treating superseded registry rows as conflicts."""
    decision, decision_conflict = _candidate_decision(snapshot, candidate)
    if decision_conflict:
        return 0, True
    if decision is None:
        return 4, False
    if decision in {"rejected", "feedback"}:
        return 3, False
    approved_spec, spec_conflict = _approved_spec_for_candidate(snapshot, candidate)
    if spec_conflict:
        return 0, True
    if _has_superseded_spec(snapshot, candidate):
        return 0, False
    return (1 if approved_spec is not None else 2), False


def _current_specification_candidate_state(
    snapshot: WorkflowFactSnapshot,
) -> tuple[SpecificationCandidateFact | None, bool]:
    """Select the current direct candidate by pending/terminal/registry state."""
    direct, conflict = _direct_candidates(snapshot)
    ranked: list[tuple[int, SpecificationCandidateFact]] = []
    for candidate in direct:
        rank, candidate_conflict = _candidate_rank(snapshot, candidate)
        conflict = conflict or candidate_conflict
        if not candidate_conflict and rank > 0:
            ranked.append((rank, candidate))
    if conflict or not ranked:
        return None, conflict or bool(direct)
    highest = max(rank for rank, _candidate in ranked)
    choices = [candidate for rank, candidate in ranked if rank == highest]
    return (choices[0] if len(choices) == 1 else None), len(choices) > 1


def current_specification_candidate(
    snapshot: WorkflowFactSnapshot,
) -> SpecificationCandidateFact | None:
    """Return the sole direct current candidate or ``None`` on conflict."""
    candidate, _conflict = _current_specification_candidate_state(snapshot)
    return candidate


def _accepted_current_spec_state(
    snapshot: WorkflowFactSnapshot,
) -> tuple[SpecVersionFact | None, bool]:
    """Return the approved exact-lineage SpecRegistry row for the current candidate."""
    candidate, candidate_conflict = _current_specification_candidate_state(snapshot)
    if candidate is None:
        return None, candidate_conflict
    spec, spec_conflict = _approved_spec_for_candidate(snapshot, candidate)
    return spec, candidate_conflict or spec_conflict


def accepted_current_spec(snapshot: WorkflowFactSnapshot) -> SpecVersionFact | None:
    """Return the current approved SpecRegistry row or ``None`` on conflict."""
    spec, _conflict = _accepted_current_spec_state(snapshot)
    return spec


@dataclass(frozen=True)
class ProductDefinitionSelection:
    """Direct current candidate and approved specification selection."""

    specification_candidate: SpecificationCandidateFact | None
    specification_candidate_conflict: bool
    accepted_spec: SpecVersionFact | None
    accepted_spec_conflict: bool

    @property
    def has_conflict(self) -> bool:
        """Return whether direct specification lineage is malformed."""
        return self.specification_candidate_conflict or self.accepted_spec_conflict


def select_product_definition_state(
    snapshot: WorkflowFactSnapshot,
) -> ProductDefinitionSelection:
    """Select direct current candidate and exact approved base once."""
    candidate, candidate_conflict = _current_specification_candidate_state(snapshot)
    spec, spec_conflict = _accepted_current_spec_state(snapshot)
    return ProductDefinitionSelection(
        specification_candidate=candidate,
        specification_candidate_conflict=candidate_conflict,
        accepted_spec=spec,
        accepted_spec_conflict=spec_conflict,
    )


def _fact_conflict(snapshot: WorkflowFactSnapshot) -> bool:
    """Report malformed direct product-definition lineage."""
    return select_product_definition_state(snapshot).has_conflict


def _amendment_is_available(
    snapshot: WorkflowFactSnapshot,
    candidate: SpecificationCandidateFact,
    spec: SpecVersionFact,
) -> bool:
    """Permit one amendment after a later quiescent completed Sprint."""
    if not lifecycle_is_quiescent(snapshot) or spec.approved_at is None:
        return False
    baseline = max(spec.approved_at, candidate.recorded_at)
    return any(
        sprint.status == "completed"
        and sprint.completed_at is not None
        and sprint.completed_at > baseline
        for sprint in snapshot.sprints
    )


def _candidate_authoring_rule(
    snapshot: WorkflowFactSnapshot,
    candidate: SpecificationCandidateFact,
    lineage: tuple[FactReference, FactReference],
) -> tuple[RuleEvaluation, ...]:
    """Resolve direct revision and amendment behavior for one candidate."""
    decision, _decision_conflict = _candidate_decision(snapshot, candidate)
    if decision is None:
        return (RuleEvaluation(RuleCategory.SATISFIED, "SPECIFICATION_REVIEW_PENDING"),)
    if decision in {"rejected", "feedback"}:
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                f"SPECIFICATION_{decision.upper()}_REVISION_REQUIRED",
                fact_references=(
                    *lineage,
                    _reference(
                        "specification_candidate",
                        candidate.specification_candidate_id,
                        candidate.candidate_fingerprint,
                    ),
                ),
            ),
        )
    spec = accepted_current_spec(snapshot)
    if spec is None:
        return (
            RuleEvaluation(
                RuleCategory.SATISFIED, "SPECIFICATION_ACCEPTANCE_PENDING"
            ),
        )
    if _amendment_is_available(snapshot, candidate, spec):
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "SPECIFICATION_AMENDMENT_REQUIRED",
                recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
                fact_references=(
                    *lineage,
                    _reference("specification", spec.spec_version_id, spec.spec_hash),
                ),
            ),
        )
    return (RuleEvaluation(RuleCategory.SATISFIED, "SPECIFICATION_ACCEPTED"),)


def _specification_rule(
    snapshot: WorkflowFactSnapshot,
    _at: datetime,
) -> tuple[RuleEvaluation, ...]:
    """Expose direct initial, revision, and amendment authoring actions."""
    if _fact_conflict(snapshot):
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    vision = accepted_current_vision(snapshot)
    goal = accepted_current_goal(snapshot)
    if vision is None or goal is None:
        return (RuleEvaluation(RuleCategory.SATISFIED, "SPECIFICATION_NOT_READY"),)
    candidate = current_specification_candidate(snapshot)
    lineage = _lineage_references(vision, goal)
    if candidate is not None:
        return _candidate_authoring_rule(snapshot, candidate, lineage)
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "SPECIFICATION_INITIAL_REQUIRED",
            fact_references=lineage,
        ),
    )


def _review_rule(
    snapshot: WorkflowFactSnapshot,
    _at: datetime,
) -> tuple[RuleEvaluation, ...]:
    """Wait for one exact pending candidate decision."""
    if _fact_conflict(snapshot):
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    candidate = current_specification_candidate(snapshot)
    if candidate is None:
        return (
            RuleEvaluation(RuleCategory.SATISFIED, "SPECIFICATION_REVIEW_NOT_PENDING"),
        )
    decision, _decision_conflict = _candidate_decision(snapshot, candidate)
    if decision is not None:
        return (
            RuleEvaluation(
                RuleCategory.SATISFIED,
                f"SPECIFICATION_REVIEW_{decision.upper()}",
            ),
        )
    return (
        RuleEvaluation(
            RuleCategory.WAITING,
            "SPECIFICATION_REVIEW_REQUIRED",
            fact_references=(
                _reference(
                    "specification_candidate",
                    candidate.specification_candidate_id,
                    candidate.candidate_fingerprint,
                ),
            ),
        ),
    )


PRODUCT_DISCOVERY_NODES: tuple[NodeSpec, ...] = (
    NodeSpec(
        node_id="specification.author",
        child_graph_id="product_discovery",
        request_kind="author_specification",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="accepted_vision_artifact_id", value_type="integer"),
            InputField(name="accepted_vision_fingerprint", value_type="string"),
            InputField(
                name="accepted_product_goal_artifact_id", value_type="integer"
            ),
            InputField(
                name="accepted_product_goal_fingerprint", value_type="string"
            ),
            InputField(
                name="base_spec_version_id", value_type="integer", required=False
            ),
            InputField(name="base_spec_hash", value_type="string", required=False),
        ),
        evaluate_rule=_specification_rule,
        agentic_execution=AgenticExecutionSpec(
            active_reason="SPECIFICATION_AUTHOR_ACTIVE",
            failure_reason="SPECIFICATION_AUTHOR_FAILED",
            recovery_reason="SPECIFICATION_AUTHOR_RECOVERY_REQUIRED",
        ),
    ),
    NodeSpec(
        node_id="specification.review",
        child_graph_id="product_discovery",
        request_kind="decide_specification",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="decision", value_type="string"),
            InputField(name="rationale", value_type="string"),
        ),
        evaluate_rule=_review_rule,
    ),
)
