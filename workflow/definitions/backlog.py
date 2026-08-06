"""Pure Backlog generation and review rules for current delivery lineage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from workflow.contracts import Blocker, FactReference, InputField, RecommendationKind
from workflow.definitions.authority import accepted_current_authority
from workflow.definitions.product_goal import accepted_current_goal
from workflow.graph import AgenticExecutionSpec, NodeSpec, RuleCategory, RuleEvaluation

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.facts import (
        AuthorityFact,
        PhaseArtifactFact,
        ProductGoalArtifactFact,
        WorkflowFactSnapshot,
    )


@dataclass(frozen=True)
class BacklogLineage:
    """Exact current Goal, Authority, and accepted Backlog selection."""

    authority: AuthorityFact | None
    goal: ProductGoalArtifactFact | None
    backlog: PhaseArtifactFact | None
    latest: PhaseArtifactFact | None
    conflict: bool


def _reference(fact_type: str, fact_id: int, fingerprint: str) -> FactReference:
    return FactReference(
        fact_type=fact_type,
        fact_id=str(fact_id),
        fingerprint=fingerprint,
    )


def _blocked(reason_code: str, message: str) -> tuple[RuleEvaluation, ...]:
    return (
        RuleEvaluation(
            RuleCategory.BLOCKED,
            reason_code,
            blockers=(Blocker(code=reason_code, message=message),),
        ),
    )


def _lineage_artifacts(
    snapshot: WorkflowFactSnapshot,
    *,
    authority: AuthorityFact,
    goal: ProductGoalArtifactFact,
) -> tuple[PhaseArtifactFact, ...]:
    return tuple(
        artifact
        for artifact in snapshot.phase_artifacts
        if artifact.artifact_type == "backlog"
        and artifact.authority_id == authority.authority_id
        and artifact.authority_fingerprint == authority.authority_fingerprint
        and artifact.product_goal_artifact_id == goal.product_goal_artifact_id
        and artifact.product_goal_fingerprint == goal.content_fingerprint
    )


def current_backlog_lineage(snapshot: WorkflowFactSnapshot) -> BacklogLineage:
    """Select only current delivery facts without mutating historical facts."""
    authority, authority_conflict = accepted_current_authority(snapshot)
    goal = accepted_current_goal(snapshot)
    if authority_conflict:
        return BacklogLineage(authority, goal, None, None, True)
    if authority is None or goal is None:
        return BacklogLineage(authority, goal, None, None, False)
    return _selected_backlog_lineage(snapshot, authority=authority, goal=goal)


def _selected_backlog_lineage(
    snapshot: WorkflowFactSnapshot,
    *,
    authority: AuthorityFact,
    goal: ProductGoalArtifactFact,
) -> BacklogLineage:
    """Validate the one immutable Backlog chain for exact current lineage."""
    artifacts = _lineage_artifacts(snapshot, authority=authority, goal=goal)
    by_id = {artifact.artifact_id: artifact for artifact in artifacts}
    invalid_identifiers = len(by_id) != len(artifacts) or any(
        not isinstance(item_id, int) for item_id in by_id
    )
    superseded_ids = {
        artifact.supersedes_artifact_id
        for artifact in artifacts
        if artifact.supersedes_artifact_id is not None
    }
    missing_parent = any(parent_id not in by_id for parent_id in superseded_ids)
    current = tuple(
        artifact
        for artifact in artifacts
        if artifact.artifact_id not in superseded_ids
        and artifact.status != "superseded"
    )
    latest = current[0] if len(current) == 1 else None
    decisions = tuple(
        decision
        for decision in snapshot.review_decisions
        if decision.artifact_type == "backlog" and decision.artifact_id in by_id
    )
    decisions_by_id = {decision.artifact_id: decision for decision in decisions}
    duplicate_decision = len(decisions_by_id) != len(decisions)
    if latest is None:
        return BacklogLineage(
            authority,
            goal,
            None,
            None,
            invalid_identifiers or missing_parent or duplicate_decision,
        )
    decision = decisions_by_id.get(int(latest.artifact_id))
    fingerprint_mismatch = (
        decision is not None
        and decision.artifact_fingerprint != latest.artifact_fingerprint
    )
    conflict = (
        invalid_identifiers
        or missing_parent
        or len(current) > 1
        or duplicate_decision
        or fingerprint_mismatch
    )
    accepted = (
        latest
        if decision is not None
        and decision.decision == "accepted"
        and latest.status == "accepted"
        else None
    )
    return BacklogLineage(authority, goal, accepted, latest, conflict)


def _references(lineage: BacklogLineage) -> tuple[FactReference, ...]:
    if lineage.authority is None or lineage.goal is None:
        return ()
    return (
        _reference(
            "product_goal",
            lineage.goal.product_goal_artifact_id,
            lineage.goal.content_fingerprint,
        ),
        _reference(
            "authority",
            lineage.authority.authority_id,
            lineage.authority.authority_fingerprint,
        ),
    )


def _backlog_generate_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    lineage = current_backlog_lineage(snapshot)
    if lineage.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if lineage.goal is None:
        return _blocked(
            "ACCEPTED_PRODUCT_GOAL_REQUIRED",
            "Backlog generation requires the active accepted Product Goal.",
        )
    if lineage.authority is None:
        return _blocked(
            "ACCEPTED_AUTHORITY_REQUIRED",
            "Backlog generation requires accepted current authority.",
        )
    if lineage.latest is None:
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "BACKLOG_GENERATION_REQUIRED",
                fact_references=_references(lineage),
            ),
        )
    if lineage.latest.status == "pending_review":
        return (RuleEvaluation(RuleCategory.SATISFIED, "BACKLOG_REVIEW_PENDING"),)
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "BACKLOG_REVISION_REQUIRED"
            if lineage.latest.status in {"rejected", "feedback", "superseded"}
            else "BACKLOG_CORRECTION_AVAILABLE",
            fact_references=(
                *_references(lineage),
                _reference(
                    "backlog",
                    int(lineage.latest.artifact_id),
                    lineage.latest.artifact_fingerprint,
                ),
            ),
            recommendation_kind=(
                RecommendationKind.RECOVERY
                if lineage.latest.status in {"rejected", "feedback", "superseded"}
                else RecommendationKind.OPTIONAL_REENTRY
            ),
        ),
    )


def _backlog_review_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    lineage = current_backlog_lineage(snapshot)
    if lineage.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if lineage.latest is None or lineage.latest.status != "pending_review":
        return (RuleEvaluation(RuleCategory.SATISFIED, "BACKLOG_REVIEW_NOT_PENDING"),)
    return (
        RuleEvaluation(
            RuleCategory.WAITING,
            "BACKLOG_REVIEW_REQUIRED",
            fact_references=(
                _reference(
                    "backlog",
                    int(lineage.latest.artifact_id),
                    lineage.latest.artifact_fingerprint,
                ),
            ),
        ),
    )


BACKLOG_NODES: tuple[NodeSpec, ...] = (
    NodeSpec(
        node_id="backlog.generate",
        child_graph_id="backlog",
        request_kind="record_backlog_draft",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="authority_id", value_type="integer"),
            InputField(name="authority_fingerprint", value_type="string"),
            InputField(name="product_goal_artifact_id", value_type="integer"),
            InputField(name="product_goal_fingerprint", value_type="string"),
            InputField(name="canonical_content", value_type="object"),
            InputField(name="content_fingerprint", value_type="string"),
            InputField(name="supersedes_backlog_artifact_id", value_type="integer"),
        ),
        evaluate_rule=_backlog_generate_rule,
        agentic_execution=AgenticExecutionSpec(
            active_reason="BACKLOG_GENERATION_ACTIVE",
            failure_reason="BACKLOG_GENERATION_FAILED",
            recovery_reason="BACKLOG_GENERATION_RECOVERY_REQUIRED",
        ),
    ),
    NodeSpec(
        node_id="backlog.review",
        child_graph_id="backlog",
        request_kind="decide_backlog",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="backlog_artifact_id", value_type="integer"),
            InputField(name="artifact_fingerprint", value_type="string"),
            InputField(name="decision", value_type="string"),
            InputField(name="rationale", value_type="string"),
        ),
        evaluate_rule=_backlog_review_rule,
    ),
)

__all__ = ["BACKLOG_NODES", "BacklogLineage", "current_backlog_lineage"]
