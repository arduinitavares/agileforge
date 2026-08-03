"""Pure Backlog generation, review, reconciliation, and planning-join rules."""

from __future__ import annotations

from typing import TYPE_CHECKING

from workflow.contracts import Blocker, InputField, RecommendationKind
from workflow.definitions.authority import accepted_current_authority
from workflow.definitions.vision import (
    accepted_current_artifact,
    artifact_reference,
    authority_reference,
    phase_artifact_state,
)
from workflow.graph import (
    AgenticExecutionSpec,
    NodeSpec,
    RuleCategory,
    RuleEvaluation,
)

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.facts import AuthorityFact, WorkflowFactSnapshot


def _blocked(reason_code: str, message: str) -> tuple[RuleEvaluation, ...]:
    return (
        RuleEvaluation(
            RuleCategory.BLOCKED,
            reason_code,
            blockers=(Blocker(code=reason_code, message=message),),
        ),
    )


def _backlog_generate_rule(  # noqa: PLR0911
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    if snapshot.project_abandonments:
        return (RuleEvaluation(RuleCategory.SATISFIED, "PROJECT_ABANDONED"),)
    authority, authority_conflict = accepted_current_authority(snapshot)
    if authority_conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if authority is None:
        return _blocked(
            "ACCEPTED_AUTHORITY_REQUIRED",
            "Backlog generation requires accepted current authority.",
        )
    backlog_state = phase_artifact_state(
        snapshot,
        artifact_type="backlog",
        authority=authority,
    )
    if backlog_state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    references = (authority_reference(authority),)
    affected_ids = stale_accepted_artifact_ids(snapshot, authority)
    if backlog_state.stale_accepted_ids and not _matching_reconciliation(
        snapshot,
        authority,
        affected_ids,
    ):
        return _blocked(
            "BACKLOG_RECONCILIATION_REQUIRED",
            "Stale Backlog artifacts require explicit reconciliation.",
        )
    if backlog_state.latest is not None:
        latest = backlog_state.latest
        if latest.status == "pending_review":
            return (RuleEvaluation(RuleCategory.SATISFIED, "BACKLOG_REVIEW_PENDING"),)
        if latest.status in {"rejected", "feedback", "superseded"}:
            reason = (
                "BACKLOG_SUPERSEDED"
                if latest.status == "superseded"
                else "BACKLOG_REVISION_REQUIRED"
            )
            return (
                RuleEvaluation(
                    RuleCategory.AVAILABLE,
                    reason,
                    fact_references=(*references, artifact_reference(latest)),
                    recommendation_kind=RecommendationKind.RECOVERY,
                ),
            )
        if accepted_current_artifact(backlog_state, authority) is not None:
            return (
                RuleEvaluation(
                    RuleCategory.AVAILABLE,
                    "BACKLOG_CORRECTION_AVAILABLE",
                    fact_references=(*references, artifact_reference(latest)),
                    recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
                ),
            )
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "BACKLOG_GENERATION_REQUIRED",
            fact_references=references,
        ),
    )


def _backlog_review_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    if snapshot.project_abandonments:
        return (RuleEvaluation(RuleCategory.SATISFIED, "PROJECT_ABANDONED"),)
    authority, authority_conflict = accepted_current_authority(snapshot)
    if authority_conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if authority is None:
        return (RuleEvaluation(RuleCategory.SATISFIED, "BACKLOG_REVIEW_NOT_READY"),)
    state = phase_artifact_state(snapshot, artifact_type="backlog", authority=authority)
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if state.latest is None or state.latest.status != "pending_review":
        return (RuleEvaluation(RuleCategory.SATISFIED, "BACKLOG_REVIEW_NOT_PENDING"),)
    return (
        RuleEvaluation(
            RuleCategory.WAITING,
            "BACKLOG_REVIEW_REQUIRED",
            fact_references=(artifact_reference(state.latest),),
        ),
    )


def stale_accepted_artifact_ids(
    snapshot: WorkflowFactSnapshot,
    authority: AuthorityFact,
) -> tuple[int, ...]:
    """Return all accepted Vision/Backlog artifacts bound to prior authority."""
    vision_state = phase_artifact_state(
        snapshot,
        artifact_type="vision",
        authority=authority,
    )
    backlog_state = phase_artifact_state(
        snapshot,
        artifact_type="backlog",
        authority=authority,
    )
    return tuple(
        sorted({*vision_state.stale_accepted_ids, *backlog_state.stale_accepted_ids})
    )


def _matching_reconciliation(
    snapshot: WorkflowFactSnapshot,
    authority: AuthorityFact,
    affected_ids: tuple[int, ...],
) -> bool:
    matching = tuple(
        item
        for item in snapshot.backlog_reconciliations
        if item.replacement_authority_id == authority.authority_id
        and item.replacement_authority_fingerprint == authority.authority_fingerprint
    )
    return len(matching) == 1 and matching[0].affected_artifact_ids == affected_ids


def _backlog_reconcile_rule(  # noqa: PLR0911
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    if snapshot.project_abandonments:
        return (RuleEvaluation(RuleCategory.SATISFIED, "PROJECT_ABANDONED"),)
    authority, authority_conflict = accepted_current_authority(snapshot)
    if authority_conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if authority is None:
        return (RuleEvaluation(RuleCategory.SATISFIED, "RECONCILIATION_NOT_READY"),)
    vision_state = phase_artifact_state(
        snapshot,
        artifact_type="vision",
        authority=authority,
    )
    backlog_state = phase_artifact_state(
        snapshot,
        artifact_type="backlog",
        authority=authority,
    )
    if vision_state.conflict or backlog_state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    affected_ids = stale_accepted_artifact_ids(snapshot, authority)
    current_reconciliations = tuple(
        item
        for item in snapshot.backlog_reconciliations
        if item.replacement_authority_id == authority.authority_id
        and item.replacement_authority_fingerprint == authority.authority_fingerprint
    )
    if len(current_reconciliations) > 1:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if not affected_ids:
        if current_reconciliations:
            return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
        return (RuleEvaluation(RuleCategory.SATISFIED, "NO_STALE_ARTIFACTS"),)
    if _matching_reconciliation(snapshot, authority, affected_ids):
        return (RuleEvaluation(RuleCategory.SATISFIED, "BACKLOG_RECONCILED"),)
    references = [authority_reference(authority)]
    stale_by_id = {
        int(item.artifact_id): item
        for item in snapshot.phase_artifacts
        if isinstance(item.artifact_id, int) and item.artifact_id in affected_ids
    }
    references.extend(artifact_reference(stale_by_id[item]) for item in affected_ids)
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "BACKLOG_RECONCILIATION_REQUIRED",
            fact_references=tuple(references),
            recommendation_kind=RecommendationKind.RECOVERY,
        ),
    )


def _planning_boundary_rule(  # noqa: PLR0911
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    if snapshot.project_abandonments:
        return (RuleEvaluation(RuleCategory.SATISFIED, "PROJECT_ABANDONED"),)
    authority, authority_conflict = accepted_current_authority(snapshot)
    if authority_conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if authority is None:
        return (RuleEvaluation(RuleCategory.SATISFIED, "PLANNING_NOT_READY"),)
    vision_state = phase_artifact_state(
        snapshot,
        artifact_type="vision",
        authority=authority,
    )
    backlog_state = phase_artifact_state(
        snapshot,
        artifact_type="backlog",
        authority=authority,
    )
    if vision_state.conflict or backlog_state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    vision = accepted_current_artifact(vision_state, authority)
    backlog = accepted_current_artifact(backlog_state, authority)
    if vision is None or backlog is None:
        return (RuleEvaluation(RuleCategory.SATISFIED, "PLANNING_JOIN_INCOMPLETE"),)
    affected_ids = stale_accepted_artifact_ids(snapshot, authority)
    if affected_ids and not _matching_reconciliation(
        snapshot,
        authority,
        affected_ids,
    ):
        return (RuleEvaluation(RuleCategory.SATISFIED, "PLANNING_JOIN_STALE"),)
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "ACCEPTED_PRODUCT_DEFINITION_UNLOCKS_PLANNING",
            fact_references=(artifact_reference(vision), artifact_reference(backlog)),
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
    NodeSpec(
        node_id="backlog.reconcile",
        child_graph_id="backlog",
        request_kind="reconcile_backlog",
        recommendation_kind=RecommendationKind.RECOVERY,
        required_inputs=(
            InputField(name="replacement_authority_id", value_type="integer"),
            InputField(
                name="replacement_authority_fingerprint",
                value_type="string",
            ),
            InputField(name="affected_artifact_ids", value_type="array"),
        ),
        evaluate_rule=_backlog_reconcile_rule,
    ),
)

__all__ = [
    "BACKLOG_NODES",
    "stale_accepted_artifact_ids",
]
