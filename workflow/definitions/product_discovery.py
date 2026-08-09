"""Pure discovery and specification selectors backed by durable facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from workflow.contracts import FactReference, InputField, RecommendationKind
from workflow.definitions.product_goal import (
    accepted_current_goal,
    accepted_current_vision,
    lifecycle_is_quiescent,
)
from workflow.graph import NodeSpec, RuleCategory, RuleEvaluation

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.facts import (
        DiscoveryArtifactFact,
        SpecificationCandidateFact,
        SpecVersionFact,
        WorkflowFactSnapshot,
    )


def _reference(kind: str, identifier: int, fingerprint: str) -> FactReference:
    return FactReference(
        fact_type=kind,
        fact_id=str(identifier),
        fingerprint=fingerprint,
    )


def _current_discovery_state(
    snapshot: WorkflowFactSnapshot,
) -> tuple[DiscoveryArtifactFact | None, bool]:
    """Return the current discovery and whether its active chain is invalid."""
    vision = accepted_current_vision(snapshot)
    goal = accepted_current_goal(snapshot)
    if vision is None or goal is None:
        return None, False
    superseded = {
        artifact.supersedes_discovery_artifact_id
        for artifact in snapshot.discovery_artifacts
        if artifact.supersedes_discovery_artifact_id is not None
    }
    leaves = [
        artifact
        for artifact in snapshot.discovery_artifacts
        if artifact.discovery_artifact_id not in superseded
    ]
    choices = [
        artifact
        for artifact in leaves
        if (
            artifact.vision_artifact_id,
            artifact.vision_fingerprint,
            artifact.product_goal_artifact_id,
            artifact.product_goal_fingerprint,
        )
        == (
            vision.vision_artifact_id,
            vision.content_fingerprint,
            goal.product_goal_artifact_id,
            goal.content_fingerprint,
        )
    ]
    malformed = any(
        artifact.product_goal_artifact_id == goal.product_goal_artifact_id
        and (
            artifact.product_goal_fingerprint != goal.content_fingerprint
            or (artifact.vision_artifact_id, artifact.vision_fingerprint)
            != (vision.vision_artifact_id, vision.content_fingerprint)
        )
        for artifact in leaves
    )
    return (choices[0] if len(choices) == 1 else None), malformed or len(choices) > 1


def current_discovery(snapshot: WorkflowFactSnapshot) -> DiscoveryArtifactFact | None:
    """Return the sole exact discovery leaf or ``None`` on conflict."""
    discovery, _conflict = _current_discovery_state(snapshot)
    return discovery


@dataclass(frozen=True)
class ProductDefinitionSelection:
    """Stable current-state selection shared by graph rules and projections."""

    discovery: DiscoveryArtifactFact | None
    discovery_conflict: bool
    specification_candidate: SpecificationCandidateFact | None
    specification_candidate_conflict: bool
    accepted_spec: SpecVersionFact | None
    accepted_spec_conflict: bool

    @property
    def has_conflict(self) -> bool:
        """Return whether any current product-definition chain is invalid."""
        return (
            self.discovery_conflict
            or self.specification_candidate_conflict
            or self.accepted_spec_conflict
        )


def _current_specification_candidate_state(
    snapshot: WorkflowFactSnapshot,
) -> tuple[SpecificationCandidateFact | None, bool]:
    """Return the candidate and whether its selected parent chain is invalid."""
    discovery, discovery_conflict = _current_discovery_state(snapshot)
    if discovery is None:
        return None, discovery_conflict
    superseded = {
        candidate.supersedes_specification_candidate_id
        for candidate in snapshot.specification_candidates
        if candidate.supersedes_specification_candidate_id is not None
    }
    leaves = [
        candidate
        for candidate in snapshot.specification_candidates
        if candidate.specification_candidate_id not in superseded
    ]
    choices = [
        candidate
        for candidate in leaves
        if (
            candidate.vision_artifact_id,
            candidate.vision_fingerprint,
            candidate.product_goal_artifact_id,
            candidate.product_goal_fingerprint,
            candidate.discovery_artifact_id,
            candidate.discovery_fingerprint,
        )
        == (
            discovery.vision_artifact_id,
            discovery.vision_fingerprint,
            discovery.product_goal_artifact_id,
            discovery.product_goal_fingerprint,
            discovery.discovery_artifact_id,
            discovery.content_fingerprint,
        )
    ]
    malformed = any(
        candidate.discovery_artifact_id == discovery.discovery_artifact_id
        and (
            candidate.discovery_fingerprint != discovery.content_fingerprint
            or (
                candidate.vision_artifact_id,
                candidate.vision_fingerprint,
                candidate.product_goal_artifact_id,
                candidate.product_goal_fingerprint,
            )
            != (
                discovery.vision_artifact_id,
                discovery.vision_fingerprint,
                discovery.product_goal_artifact_id,
                discovery.product_goal_fingerprint,
            )
        )
        for candidate in leaves
    )
    return (
        choices[0] if len(choices) == 1 else None,
        discovery_conflict or malformed or len(choices) > 1,
    )


def current_specification_candidate(
    snapshot: WorkflowFactSnapshot,
) -> SpecificationCandidateFact | None:
    """Return the sole exact candidate leaf or ``None`` on conflict."""
    candidate, _conflict = _current_specification_candidate_state(snapshot)
    return candidate


def _accepted_current_spec_state(
    snapshot: WorkflowFactSnapshot,
) -> tuple[SpecVersionFact | None, bool]:
    """Return the approved registry row and any exact-lineage conflict."""
    candidate, candidate_conflict = _current_specification_candidate_state(snapshot)
    if candidate is None:
        return None, candidate_conflict
    choices = [
        spec
        for spec in snapshot.spec_versions
        if spec.status == "approved"
        and (
            spec.source_vision_artifact_id,
            spec.source_vision_fingerprint,
            spec.source_product_goal_artifact_id,
            spec.source_product_goal_fingerprint,
            spec.source_discovery_artifact_id,
            spec.source_discovery_fingerprint,
            spec.source_specification_candidate_id,
            spec.source_specification_candidate_fingerprint,
        )
        == (
            candidate.vision_artifact_id,
            candidate.vision_fingerprint,
            candidate.product_goal_artifact_id,
            candidate.product_goal_fingerprint,
            candidate.discovery_artifact_id,
            candidate.discovery_fingerprint,
            candidate.specification_candidate_id,
            candidate.content_fingerprint,
        )
    ]
    malformed = any(
        spec.status == "approved"
        and spec.source_specification_candidate_id
        == candidate.specification_candidate_id
        and (
            spec.source_specification_candidate_fingerprint
            != candidate.content_fingerprint
            or (
                spec.source_vision_artifact_id,
                spec.source_vision_fingerprint,
                spec.source_product_goal_artifact_id,
                spec.source_product_goal_fingerprint,
                spec.source_discovery_artifact_id,
                spec.source_discovery_fingerprint,
            )
            != (
                candidate.vision_artifact_id,
                candidate.vision_fingerprint,
                candidate.product_goal_artifact_id,
                candidate.product_goal_fingerprint,
                candidate.discovery_artifact_id,
                candidate.discovery_fingerprint,
            )
        )
        for spec in snapshot.spec_versions
    )
    return choices[0] if len(
        choices
    ) == 1 else None, candidate_conflict or malformed or len(choices) > 1


def accepted_current_spec(snapshot: WorkflowFactSnapshot) -> SpecVersionFact | None:
    """Return the approved exact-lineage registry row or ``None`` on conflict."""
    spec, _conflict = _accepted_current_spec_state(snapshot)
    return spec


def select_product_definition_state(
    snapshot: WorkflowFactSnapshot,
) -> ProductDefinitionSelection:
    """Select durable discovery, specification, and registry state once."""
    discovery, discovery_conflict = _current_discovery_state(snapshot)
    candidate, candidate_conflict = _current_specification_candidate_state(snapshot)
    spec, spec_conflict = _accepted_current_spec_state(snapshot)
    return ProductDefinitionSelection(
        discovery=discovery,
        discovery_conflict=discovery_conflict,
        specification_candidate=candidate,
        specification_candidate_conflict=candidate_conflict,
        accepted_spec=spec,
        accepted_spec_conflict=spec_conflict,
    )


def _fact_conflict(snapshot: WorkflowFactSnapshot) -> bool:
    """Report malformed current product-definition lineage for graph rules."""
    return select_product_definition_state(snapshot).has_conflict


def _candidate_decision(
    snapshot: WorkflowFactSnapshot,
    candidate: SpecificationCandidateFact,
) -> str | None:
    decisions = [
        decision
        for decision in snapshot.specification_decisions
        if decision.specification_candidate_id == candidate.specification_candidate_id
    ]
    return decisions[0].decision if len(decisions) == 1 else None


def _discovery_rule(
    snapshot: WorkflowFactSnapshot,
    _at: datetime,
) -> tuple[RuleEvaluation, ...]:
    if _fact_conflict(snapshot):
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    vision = accepted_current_vision(snapshot)
    goal = accepted_current_goal(snapshot)
    if vision is None or goal is None:
        return (RuleEvaluation(RuleCategory.SATISFIED, "DISCOVERY_NOT_READY"),)
    discovery = current_discovery(snapshot)
    if discovery is not None and _increment_is_available(snapshot, discovery):
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "DISCOVERY_INCREMENT_AVAILABLE",
                recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
                fact_references=(
                    _reference(
                        "vision",
                        vision.vision_artifact_id,
                        vision.content_fingerprint,
                    ),
                    _reference(
                        "product_goal",
                        goal.product_goal_artifact_id,
                        goal.content_fingerprint,
                    ),
                    _reference(
                        "discovery",
                        discovery.discovery_artifact_id,
                        discovery.content_fingerprint,
                    ),
                ),
            ),
        )
    if discovery is not None:
        return (RuleEvaluation(RuleCategory.SATISFIED, "DISCOVERY_RECORDED"),)
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "DISCOVERY_REQUIRED",
            fact_references=(
                _reference(
                    "vision",
                    vision.vision_artifact_id,
                    vision.content_fingerprint,
                ),
                _reference(
                    "product_goal",
                    goal.product_goal_artifact_id,
                    goal.content_fingerprint,
                ),
            ),
        ),
    )


def _increment_is_available(
    snapshot: WorkflowFactSnapshot,
    discovery: DiscoveryArtifactFact,
) -> bool:
    """Allow one new discovery after a later fully triaged Sprint."""
    if not lifecycle_is_quiescent(snapshot):
        return False
    return any(
        sprint.status == "completed"
        and sprint.completed_at is not None
        and sprint.completed_at > discovery.recorded_at
        for sprint in snapshot.sprints
    )


def _specification_rule(
    snapshot: WorkflowFactSnapshot,
    _at: datetime,
) -> tuple[RuleEvaluation, ...]:
    if _fact_conflict(snapshot):
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    discovery = current_discovery(snapshot)
    if discovery is None:
        return (RuleEvaluation(RuleCategory.SATISFIED, "SPECIFICATION_NOT_READY"),)
    candidate = current_specification_candidate(snapshot)
    decision = None if candidate is None else _candidate_decision(snapshot, candidate)
    if candidate is not None and decision is None:
        return (
            RuleEvaluation(
                RuleCategory.SATISFIED,
                "SPECIFICATION_REVIEW_PENDING",
            ),
        )
    if decision == "accepted":
        return (RuleEvaluation(RuleCategory.SATISFIED, "SPECIFICATION_ACCEPTED"),)
    if decision in {"rejected", "feedback"}:
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                f"SPECIFICATION_{decision.upper()}_REPLACEMENT_REQUIRED",
                fact_references=(
                    _reference(
                        "discovery",
                        discovery.discovery_artifact_id,
                        discovery.content_fingerprint,
                    ),
                ),
            ),
        )
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "SPECIFICATION_REQUIRED",
            fact_references=(
                _reference(
                    "discovery",
                    discovery.discovery_artifact_id,
                    discovery.content_fingerprint,
                ),
            ),
        ),
    )


def _review_rule(
    snapshot: WorkflowFactSnapshot,
    _at: datetime,
) -> tuple[RuleEvaluation, ...]:
    if _fact_conflict(snapshot):
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    candidate = current_specification_candidate(snapshot)
    if candidate is None:
        return (
            RuleEvaluation(
                RuleCategory.SATISFIED,
                "SPECIFICATION_REVIEW_NOT_PENDING",
            ),
        )
    decision = _candidate_decision(snapshot, candidate)
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
                    candidate.content_fingerprint,
                ),
            ),
        ),
    )


PRODUCT_DISCOVERY_NODES: tuple[NodeSpec, ...] = (
    NodeSpec(
        node_id="discovery.record",
        child_graph_id="product_discovery",
        request_kind="record_discovery_artifact",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(InputField(name="canonical_content", value_type="object"),),
        evaluate_rule=_discovery_rule,
    ),
    NodeSpec(
        node_id="specification.record",
        child_graph_id="product_discovery",
        request_kind="record_specification_candidate",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(InputField(name="canonical_content", value_type="object"),),
        evaluate_rule=_specification_rule,
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
