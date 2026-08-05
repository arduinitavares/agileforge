"""Pure discovery and specification selectors backed by durable facts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from workflow.contracts import FactReference, InputField, RecommendationKind
from workflow.definitions.product_goal import (
    accepted_current_goal,
    accepted_current_vision,
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


def current_discovery(snapshot: WorkflowFactSnapshot) -> DiscoveryArtifactFact | None:
    """Return the sole leaf discovery for the current accepted Goal."""
    goal = accepted_current_goal(snapshot)
    if goal is None:
        return None
    superseded = {
        artifact.supersedes_discovery_artifact_id
        for artifact in snapshot.discovery_artifacts
        if artifact.supersedes_discovery_artifact_id is not None
    }
    choices = [
        artifact
        for artifact in snapshot.discovery_artifacts
        if artifact.discovery_artifact_id not in superseded
        and artifact.product_goal_artifact_id == goal.product_goal_artifact_id
        and artifact.product_goal_fingerprint == goal.content_fingerprint
    ]
    return choices[0] if len(choices) == 1 else None


def current_specification_candidate(
    snapshot: WorkflowFactSnapshot,
) -> SpecificationCandidateFact | None:
    """Return the sole current candidate for the current discovery."""
    discovery = current_discovery(snapshot)
    if discovery is None:
        return None
    superseded = {
        candidate.supersedes_specification_candidate_id
        for candidate in snapshot.specification_candidates
        if candidate.supersedes_specification_candidate_id is not None
    }
    choices = [
        candidate
        for candidate in snapshot.specification_candidates
        if candidate.specification_candidate_id not in superseded
        and candidate.discovery_artifact_id == discovery.discovery_artifact_id
        and candidate.discovery_fingerprint == discovery.content_fingerprint
    ]
    return choices[0] if len(choices) == 1 else None


def accepted_current_spec(snapshot: WorkflowFactSnapshot) -> SpecVersionFact | None:
    """Return the current approved registry row for the active Goal chain."""
    goal = accepted_current_goal(snapshot)
    if goal is None:
        return None
    choices = [
        spec
        for spec in snapshot.spec_versions
        if spec.status == "approved"
        and spec.source_product_goal_artifact_id == goal.product_goal_artifact_id
        and spec.source_product_goal_fingerprint == goal.content_fingerprint
    ]
    return choices[0] if len(choices) == 1 else None


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
    vision = accepted_current_vision(snapshot)
    goal = accepted_current_goal(snapshot)
    if vision is None or goal is None:
        return (RuleEvaluation(RuleCategory.SATISFIED, "DISCOVERY_NOT_READY"),)
    if current_discovery(snapshot) is not None:
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


def _specification_rule(
    snapshot: WorkflowFactSnapshot,
    _at: datetime,
) -> tuple[RuleEvaluation, ...]:
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
