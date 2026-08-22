"""Pure Backlog rules for the current accepted Specification lineage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from services.planning_lineage import (
    ArtifactLineageNode,
    PlanningLineageCode,
    PlanningLineageError,
    select_current_accepted_artifact,
    validate_artifact_lineage,
)
from workflow.contracts import Blocker, FactReference, InputField, RecommendationKind
from workflow.definitions.product_discovery import select_product_definition_state
from workflow.definitions.product_goal import accepted_current_goal
from workflow.graph import AgenticExecutionSpec, NodeSpec, RuleCategory, RuleEvaluation

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.facts import (
        PhaseArtifactFact,
        ProductGoalArtifactFact,
        SpecVersionFact,
        WorkflowFactSnapshot,
    )


@dataclass(frozen=True)
class BacklogLineage:
    """Exact current Goal, Specification, and accepted Backlog selection."""

    specification: SpecVersionFact | None
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
    specification: SpecVersionFact,
    goal: ProductGoalArtifactFact,
) -> tuple[PhaseArtifactFact, ...]:
    return tuple(
        artifact
        for artifact in snapshot.phase_artifacts
        if artifact.artifact_type == "backlog"
        and artifact.spec_version_id == specification.spec_version_id
        and artifact.spec_hash == specification.spec_hash
        and artifact.product_goal_artifact_id == goal.product_goal_artifact_id
        and artifact.product_goal_fingerprint == goal.content_fingerprint
    )


def _lineage_nodes(
    artifacts: tuple[PhaseArtifactFact, ...],
    *,
    chain_key: tuple[object, ...],
) -> tuple[ArtifactLineageNode, ...]:
    return tuple(
        ArtifactLineageNode(
            artifact_id=artifact.artifact_id,
            chain_key=chain_key,
            version_number=artifact.version_number,
            supersedes_artifact_id=artifact.supersedes_artifact_id,
            decision=(
                "accepted"
                if artifact.status in {"accepted", "superseded"}
                else artifact.status
                if artifact.status in {"feedback", "rejected"}
                else None
            ),
        )
        for artifact in artifacts
    )


def _physical_leaf(
    artifacts: tuple[PhaseArtifactFact, ...],
) -> PhaseArtifactFact | None:
    parent_ids = {
        artifact.supersedes_artifact_id
        for artifact in artifacts
        if artifact.supersedes_artifact_id is not None
    }
    leaves = tuple(
        artifact for artifact in artifacts if artifact.artifact_id not in parent_ids
    )
    return leaves[0] if len(leaves) == 1 else None


def current_backlog_lineage(  # noqa: PLR0911
    snapshot: WorkflowFactSnapshot,
) -> BacklogLineage:
    """Select exact accepted leaves through the shared ancestry implementation."""
    selection = select_product_definition_state(snapshot)
    specification = selection.accepted_spec
    goal = accepted_current_goal(snapshot)
    if selection.has_conflict:
        return BacklogLineage(specification, goal, None, None, True)
    if specification is None or goal is None:
        return BacklogLineage(specification, goal, None, None, False)

    artifacts = _lineage_artifacts(
        snapshot,
        specification=specification,
        goal=goal,
    )
    if not artifacts:
        return BacklogLineage(specification, goal, None, None, False)
    chain_key = (
        snapshot.project.project_id,
        goal.product_goal_artifact_id,
        goal.content_fingerprint,
        specification.spec_version_id,
        specification.spec_hash,
    )
    nodes = _lineage_nodes(artifacts, chain_key=chain_key)
    latest = _physical_leaf(artifacts)
    try:
        validate_artifact_lineage(nodes)
        if latest is None:
            return BacklogLineage(specification, goal, None, None, True)
        accepted_node = select_current_accepted_artifact(nodes, chain_key=chain_key)
    except PlanningLineageError as error:
        if error.code is PlanningLineageCode.ACCEPTED_LEAF_MISSING:
            return BacklogLineage(specification, goal, None, latest, False)
        return BacklogLineage(specification, goal, None, None, True)
    by_id = {artifact.artifact_id: artifact for artifact in artifacts}
    return BacklogLineage(
        specification,
        goal,
        by_id[accepted_node.artifact_id],
        latest,
        False,
    )


def _references(lineage: BacklogLineage) -> tuple[FactReference, ...]:
    if lineage.specification is None or lineage.goal is None:
        return ()
    return (
        _reference(
            "product_goal",
            lineage.goal.product_goal_artifact_id,
            lineage.goal.content_fingerprint,
        ),
        _reference(
            "specification",
            lineage.specification.spec_version_id,
            lineage.specification.spec_hash,
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
    if lineage.specification is None:
        return _blocked(
            "ACCEPTED_SPECIFICATION_REQUIRED",
            "Backlog generation requires the current accepted Specification.",
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
            (
                "BACKLOG_REVISION_REQUIRED"
                if lineage.latest.status in {"rejected", "feedback"}
                else "BACKLOG_CORRECTION_AVAILABLE"
            ),
            fact_references=(
                *_references(lineage),
                _reference(
                    "backlog",
                    lineage.latest.artifact_id,
                    lineage.latest.artifact_fingerprint,
                ),
            ),
            recommendation_kind=(
                RecommendationKind.RECOVERY
                if lineage.latest.status in {"rejected", "feedback"}
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
    if lineage.specification is None or lineage.goal is None:
        return (RuleEvaluation(RuleCategory.INVALID, "BACKLOG_REVIEW_SOURCE_STALE"),)
    return (
        RuleEvaluation(
            RuleCategory.WAITING,
            "BACKLOG_REVIEW_REQUIRED",
            fact_references=(
                *_references(lineage),
                _reference(
                    "backlog",
                    lineage.latest.artifact_id,
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
            InputField(name="spec_version_id", value_type="integer"),
            InputField(name="spec_hash", value_type="string"),
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
