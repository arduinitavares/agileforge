"""Pure Vision artifact generation and review rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from workflow.contracts import Blocker, FactReference, InputField, RecommendationKind
from workflow.definitions.authority import accepted_current_authority
from workflow.graph import (
    AgenticExecutionSpec,
    NodeSpec,
    RuleCategory,
    RuleEvaluation,
)

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.facts import (
        AuthorityFact,
        PhaseArtifactFact,
        ReviewDecisionFact,
        WorkflowFactSnapshot,
    )


@dataclass(frozen=True)
class PhaseArtifactState:
    """Validated active artifact and its exact review decision."""

    latest: PhaseArtifactFact | None
    decision: ReviewDecisionFact | None
    stale_accepted_ids: tuple[int, ...]
    conflict: bool


def _reference(fact_type: str, fact_id: int, fingerprint: str) -> FactReference:
    return FactReference(
        fact_type=fact_type,
        fact_id=str(fact_id),
        fingerprint=fingerprint,
    )


def authority_reference(authority: AuthorityFact) -> FactReference:
    """Return the canonical graph reference for accepted authority."""
    return _reference(
        "authority",
        authority.authority_id,
        authority.authority_fingerprint,
    )


def artifact_reference(artifact: PhaseArtifactFact) -> FactReference:
    """Return the canonical graph reference for one phase artifact."""
    if not isinstance(artifact.artifact_id, int):
        message = "Project-definition artifacts require integer identities."
        raise TypeError(message)
    return _reference(
        artifact.artifact_type,
        artifact.artifact_id,
        artifact.artifact_fingerprint,
    )


def _decision_for(
    snapshot: WorkflowFactSnapshot,
    artifact: PhaseArtifactFact,
) -> tuple[ReviewDecisionFact | None, bool]:
    if not isinstance(artifact.artifact_id, int):
        return None, True
    decisions = tuple(
        item
        for item in snapshot.review_decisions
        if item.artifact_type == artifact.artifact_type
        and item.artifact_id == artifact.artifact_id
    )
    if len(decisions) > 1:
        return None, True
    if decisions and decisions[0].artifact_fingerprint != artifact.artifact_fingerprint:
        return None, True
    return (decisions[0] if decisions else None), False


def phase_artifact_state(  # noqa: C901
    snapshot: WorkflowFactSnapshot,
    *,
    artifact_type: str,
    authority: AuthorityFact | None,
) -> PhaseArtifactState:
    """Validate one immutable artifact chain and derive its current member."""
    artifacts = tuple(
        item for item in snapshot.phase_artifacts if item.artifact_type == artifact_type
    )
    integer_ids = {
        item.artifact_id for item in artifacts if isinstance(item.artifact_id, int)
    }
    if len(integer_ids) != len(artifacts):
        return PhaseArtifactState(None, None, (), True)
    by_id = {int(item.artifact_id): item for item in artifacts}
    authority_by_id = {item.authority_id: item for item in snapshot.authorities}
    children_by_parent: dict[int, int] = {}
    decisions: dict[int, ReviewDecisionFact | None] = {}
    stale_accepted_ids: list[int] = []
    conflict = False

    for artifact_id, artifact in by_id.items():
        parent_id = artifact.supersedes_artifact_id
        if parent_id is None:
            continue
        if parent_id not in by_id or parent_id >= artifact_id:
            conflict = True
        if parent_id in children_by_parent:
            conflict = True
        children_by_parent[parent_id] = artifact_id

    for artifact_id, artifact in by_id.items():
        stored_authority = (
            None
            if artifact.authority_id is None
            else authority_by_id.get(artifact.authority_id)
        )
        if (
            stored_authority is None
            or artifact.authority_fingerprint is None
            or stored_authority.authority_fingerprint != artifact.authority_fingerprint
        ):
            conflict = True
        decision, decision_conflict = _decision_for(snapshot, artifact)
        conflict = conflict or decision_conflict
        decisions[artifact_id] = decision
        expected_status = "pending_review" if decision is None else decision.decision
        status_matches = artifact.status == expected_status or (
            decision is not None
            and decision.decision == "feedback"
            and artifact.status == "rejected"
        )
        if artifact_id in children_by_parent:
            status_matches = artifact.status == "superseded"
        if artifact.status != "superseded" and not status_matches:
            conflict = True
        if (
            decision is not None
            and decision.decision == "accepted"
            and authority is not None
            and (
                artifact.authority_id != authority.authority_id
                or artifact.authority_fingerprint != authority.authority_fingerprint
            )
        ):
            stale_accepted_ids.append(artifact_id)

    referenced_ids = set(children_by_parent)
    current = tuple(
        artifact
        for artifact_id, artifact in by_id.items()
        if artifact_id not in referenced_ids and artifact.status != "superseded"
    )
    if len(current) > 1:
        conflict = True
    latest = current[0] if len(current) == 1 else None
    if latest is None and not current and artifacts:
        latest = max(artifacts, key=lambda item: int(item.artifact_id))
    latest_decision = (
        decisions.get(int(latest.artifact_id)) if latest is not None else None
    )
    orphan_decisions = tuple(
        item
        for item in snapshot.review_decisions
        if item.artifact_type == artifact_type and item.artifact_id not in by_id
    )
    return PhaseArtifactState(
        latest=latest,
        decision=latest_decision,
        stale_accepted_ids=tuple(sorted(stale_accepted_ids)),
        conflict=conflict or bool(orphan_decisions),
    )


def accepted_current_artifact(
    state: PhaseArtifactState,
    authority: AuthorityFact,
) -> PhaseArtifactFact | None:
    """Return an accepted artifact bound to the exact current authority."""
    artifact = state.latest
    if (
        artifact is None
        or state.decision is None
        or state.decision.decision != "accepted"
        or artifact.status != "accepted"
        or artifact.authority_id != authority.authority_id
        or artifact.authority_fingerprint != authority.authority_fingerprint
    ):
        return None
    return artifact


def _vision_generate_rule(  # noqa: PLR0911
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    if snapshot.project_abandonments:
        return (RuleEvaluation(RuleCategory.SATISFIED, "PROJECT_ABANDONED"),)
    authority, authority_conflict = accepted_current_authority(snapshot)
    if authority_conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if authority is None:
        return (
            RuleEvaluation(
                RuleCategory.BLOCKED,
                "ACCEPTED_AUTHORITY_REQUIRED",
                blockers=(
                    Blocker(
                        code="ACCEPTED_AUTHORITY_REQUIRED",
                        message=(
                            "Vision generation requires accepted current authority."
                        ),
                    ),
                ),
            ),
        )
    state = phase_artifact_state(snapshot, artifact_type="vision", authority=authority)
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    authority_fact = authority_reference(authority)
    if state.stale_accepted_ids:
        references = (
            authority_fact,
            *(
                artifact_reference(item)
                for item in snapshot.phase_artifacts
                if item.artifact_id in state.stale_accepted_ids
            ),
        )
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "VISION_STALE_AFTER_AUTHORITY_REPLACEMENT",
                fact_references=references,
                recommendation_kind=RecommendationKind.RECOVERY,
            ),
        )
    if state.latest is not None:
        if state.latest.status == "pending_review":
            return (RuleEvaluation(RuleCategory.SATISFIED, "VISION_REVIEW_PENDING"),)
        if state.latest.status in {"rejected", "feedback", "superseded"}:
            reason = (
                "VISION_SUPERSEDED"
                if state.latest.status == "superseded"
                else "VISION_REVISION_REQUIRED"
            )
            return (
                RuleEvaluation(
                    RuleCategory.AVAILABLE,
                    reason,
                    fact_references=(authority_fact, artifact_reference(state.latest)),
                    recommendation_kind=RecommendationKind.RECOVERY,
                ),
            )
        if accepted_current_artifact(state, authority) is not None:
            return (
                RuleEvaluation(
                    RuleCategory.AVAILABLE,
                    "VISION_CORRECTION_AVAILABLE",
                    fact_references=(authority_fact, artifact_reference(state.latest)),
                    recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
                ),
            )
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "VISION_GENERATION_REQUIRED",
            fact_references=(authority_fact,),
        ),
    )


def _vision_review_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    if snapshot.project_abandonments:
        return (RuleEvaluation(RuleCategory.SATISFIED, "PROJECT_ABANDONED"),)
    authority, authority_conflict = accepted_current_authority(snapshot)
    if authority_conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if authority is None:
        return (RuleEvaluation(RuleCategory.SATISFIED, "VISION_REVIEW_NOT_READY"),)
    state = phase_artifact_state(snapshot, artifact_type="vision", authority=authority)
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if state.latest is None or state.latest.status != "pending_review":
        return (RuleEvaluation(RuleCategory.SATISFIED, "VISION_REVIEW_NOT_PENDING"),)
    return (
        RuleEvaluation(
            RuleCategory.WAITING,
            "VISION_REVIEW_REQUIRED",
            fact_references=(artifact_reference(state.latest),),
        ),
    )


VISION_NODES: tuple[NodeSpec, ...] = (
    NodeSpec(
        node_id="vision.generate",
        child_graph_id="vision",
        request_kind="record_vision_draft",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="authority_id", value_type="integer"),
            InputField(name="authority_fingerprint", value_type="string"),
            InputField(name="canonical_content", value_type="object"),
            InputField(name="content_fingerprint", value_type="string"),
            InputField(name="supersedes_vision_artifact_id", value_type="integer"),
        ),
        evaluate_rule=_vision_generate_rule,
        agentic_execution=AgenticExecutionSpec(
            active_reason="VISION_GENERATION_ACTIVE",
            failure_reason="VISION_GENERATION_FAILED",
            recovery_reason="VISION_GENERATION_RECOVERY_REQUIRED",
        ),
    ),
    NodeSpec(
        node_id="vision.review",
        child_graph_id="vision",
        request_kind="decide_vision",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="vision_artifact_id", value_type="integer"),
            InputField(name="artifact_fingerprint", value_type="string"),
            InputField(name="decision", value_type="string"),
            InputField(name="rationale", value_type="string"),
        ),
        evaluate_rule=_vision_review_rule,
    ),
)


__all__ = [
    "VISION_NODES",
    "PhaseArtifactState",
    "accepted_current_artifact",
    "artifact_reference",
    "authority_reference",
    "phase_artifact_state",
]
