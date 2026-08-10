"""Pure Vision artifact generation and review rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from workflow.contracts import FactReference, InputField, RecommendationKind
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
        VisionArtifactDecisionFact,
        VisionArtifactFact,
        VisionInterviewTurnFact,
        VisionRevisionIntentFact,
        WorkflowFactSnapshot,
    )


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


@dataclass(frozen=True)
class VisionInterviewState:
    """Current isolated Vision review context and active interview chain."""

    artifact: VisionArtifactFact | None
    decision: VisionArtifactDecisionFact | None
    open_revision: VisionRevisionIntentFact | None
    transcript: tuple[VisionInterviewTurnFact, ...]
    conflict: bool


@dataclass(frozen=True)
class _VisionReviewSelection:
    """Validated selected Vision artifact, review, and revision intent."""

    artifact: VisionArtifactFact | None
    decision: VisionArtifactDecisionFact | None
    open_revision: VisionRevisionIntentFact | None


def _interview_reference(turn: VisionRevisionIntentFact) -> FactReference:
    return _reference(
        "vision_revision_intent",
        turn.vision_revision_intent_id,
        turn.source_vision_fingerprint,
    )


def _vision_turn_chain(
    turns: tuple[VisionInterviewTurnFact, ...],
    *,
    leaf_id: int | None = None,
    completed_source_ids: frozenset[int] = frozenset(),
) -> tuple[VisionInterviewTurnFact, ...] | None:
    """Return one chronological linked chain, optionally after prior artifacts."""
    if not turns:
        return ()
    by_id = {item.vision_interview_turn_id: item for item in turns}
    prior_ids = {item.prior_turn_id for item in turns if item.prior_turn_id is not None}
    leaves = [item for item in turns if item.vision_interview_turn_id not in prior_ids]
    if (
        len(by_id) != len(turns)
        or len(leaves) != 1
        or (leaf_id is not None and leaves[0].vision_interview_turn_id != leaf_id)
    ):
        return None
    leaf = leaves[0]
    reversed_chain: list[VisionInterviewTurnFact] = []
    visited: set[int] = set()
    current: VisionInterviewTurnFact | None = leaf
    missing_parent = False
    while current is not None and current.vision_interview_turn_id not in visited:
        identifier = current.vision_interview_turn_id
        visited.add(identifier)
        reversed_chain.append(current)
        prior_id = current.prior_turn_id
        missing_parent = prior_id is not None and prior_id not in by_id
        current = None if prior_id is None else by_id.get(prior_id)
        if missing_parent:
            break
    if missing_parent or current is not None or len(visited) != len(turns):
        return None
    chain = tuple(reversed(reversed_chain))
    cut = max(
        (
            index
            for index, item in enumerate(chain)
            if item.vision_interview_turn_id in completed_source_ids
        ),
        default=-1,
    )
    return chain[cut + 1 :]


def _vision_review_selection(
    snapshot: WorkflowFactSnapshot,
) -> _VisionReviewSelection | None:
    """Validate and select the sole current Vision artifact lineage."""
    by_id = {item.vision_artifact_id: item for item in snapshot.vision_artifacts}
    children = {
        item.supersedes_vision_artifact_id
        for item in snapshot.vision_artifacts
        if item.supersedes_vision_artifact_id is not None
    }
    current = [
        item
        for item in snapshot.vision_artifacts
        if item.vision_artifact_id not in children
    ]
    if len(current) > 1:
        return None
    artifact = current[0] if current else None
    decisions = {
        item.vision_artifact_id: item for item in snapshot.vision_artifact_decisions
    }
    if len(decisions) != len(snapshot.vision_artifact_decisions):
        return None
    for item in snapshot.vision_artifact_decisions:
        referenced = by_id.get(item.vision_artifact_id)
        if (
            referenced is None
            or referenced.content_fingerprint != item.artifact_fingerprint
        ):
            return None
    open_intents = []
    for intent in snapshot.vision_revision_intents:
        source = by_id.get(intent.source_vision_artifact_id)
        if (
            source is None
            or source.content_fingerprint != intent.source_vision_fingerprint
        ):
            return None
        completed = _vision_revision_completed(
            snapshot,
            revision_intent_id=intent.vision_revision_intent_id,
        )
        if not completed:
            open_intents.append(intent)
    open_revision = open_intents[0] if len(open_intents) == 1 else None
    decision = None if artifact is None else decisions.get(artifact.vision_artifact_id)
    if len(open_intents) > 1 or (
        open_revision is not None
        and (
            artifact is None
            or decision is None
            or decision.decision != "accepted"
            or (
                open_revision.source_vision_artifact_id,
                open_revision.source_vision_fingerprint,
            )
            != (artifact.vision_artifact_id, artifact.content_fingerprint)
        )
    ):
        return None
    return _VisionReviewSelection(artifact, decision, open_revision)


def _vision_revision_completed(
    snapshot: WorkflowFactSnapshot,
    *,
    revision_intent_id: int,
) -> bool:
    """Return whether any turn in a revision chain produced a Vision artifact."""
    source_turn_ids = {
        item.source_interview_turn_id for item in snapshot.vision_artifacts
    }
    return any(
        turn.revision_intent_id == revision_intent_id
        and turn.vision_interview_turn_id in source_turn_ids
        for turn in snapshot.vision_interview_turns
    )


def _active_revision_turns(
    snapshot: WorkflowFactSnapshot,
    revision_intent_id: int,
) -> tuple[VisionInterviewTurnFact, ...]:
    """Return every interview turn in the open revision chain."""
    active_snapshot_ids = _active_vision_snapshot_ids(snapshot)
    return tuple(
        item
        for item in snapshot.vision_interview_turns
        if item.revision_intent_id == revision_intent_id
        and item.operation in {"revision", "clarification"}
        and item.vision_evidence_snapshot_id in active_snapshot_ids
    )


def _active_vision_snapshot_ids(snapshot: WorkflowFactSnapshot) -> frozenset[int]:
    """Return explicit snapshot leaves after append-only supersession."""
    superseded_ids = {
        item.supersedes_vision_evidence_snapshot_id
        for item in snapshot.vision_evidence_snapshots
        if item.supersedes_vision_evidence_snapshot_id is not None
    }
    return frozenset(
        item.vision_evidence_snapshot_id
        for item in snapshot.vision_evidence_snapshots
        if item.vision_evidence_snapshot_id not in superseded_ids
    )


def _active_vision_snapshot_descendant(
    snapshot: WorkflowFactSnapshot,
    root_id: int,
) -> int:
    """Follow explicit snapshot supersession from one reviewed source root."""
    children = {
        item.supersedes_vision_evidence_snapshot_id: item.vision_evidence_snapshot_id
        for item in snapshot.vision_evidence_snapshots
        if item.supersedes_vision_evidence_snapshot_id is not None
    }
    current = root_id
    visited: set[int] = set()
    while current in children:
        if current in visited:
            return root_id
        visited.add(current)
        current = children[current]
    return current


def _vision_transcript(
    snapshot: WorkflowFactSnapshot,
    review: _VisionReviewSelection,
) -> tuple[VisionInterviewTurnFact, ...] | None:
    """Select only the active interview segment for one Vision review state."""
    artifact = review.artifact
    decision = review.decision
    open_revision = review.open_revision
    completed_source_ids = frozenset(
        item.source_interview_turn_id for item in snapshot.vision_artifacts
    )
    active_snapshot_ids = _active_vision_snapshot_ids(snapshot)
    source_turn = (
        None
        if artifact is None
        else next(
            (
                item
                for item in snapshot.vision_interview_turns
                if item.vision_interview_turn_id == artifact.source_interview_turn_id
            ),
            None,
        )
    )
    if artifact is not None and source_turn is None:
        return None
    if open_revision is not None:
        transcript = _vision_turn_chain(
            _active_revision_turns(
                snapshot,
                open_revision.vision_revision_intent_id,
            )
        )
    elif artifact is None:
        turns = tuple(
            item
            for item in snapshot.vision_interview_turns
            if item.operation in {"bootstrap", "clarification"}
            and item.revision_intent_id is None
            and item.vision_evidence_snapshot_id in active_snapshot_ids
        )
        transcript = _vision_turn_chain(turns)
    elif decision is None:
        if source_turn is None:
            return None
        turns = tuple(
            item
            for item in snapshot.vision_interview_turns
            if item.vision_evidence_snapshot_id
            == source_turn.vision_evidence_snapshot_id
            and item.revision_intent_id == source_turn.revision_intent_id
        )
        transcript = _vision_turn_chain(
            turns,
            leaf_id=source_turn.vision_interview_turn_id,
            completed_source_ids=completed_source_ids
            - {source_turn.vision_interview_turn_id},
        )
    elif decision.decision in {"feedback", "rejected"}:
        if source_turn is None:
            return None
        active_source_snapshot_id = _active_vision_snapshot_descendant(
            snapshot,
            source_turn.vision_evidence_snapshot_id,
        )
        turns = tuple(
            item
            for item in snapshot.vision_interview_turns
            if item.vision_evidence_snapshot_id == active_source_snapshot_id
            and item.revision_intent_id == source_turn.revision_intent_id
        )
        transcript = _vision_turn_chain(
            turns,
            completed_source_ids=completed_source_ids,
        )
    else:
        transcript = ()
    if transcript is None or any(
        item.is_complete and item.vision_interview_turn_id not in completed_source_ids
        for item in transcript
    ):
        return None
    return transcript


def select_vision_interview_state(
    snapshot: WorkflowFactSnapshot,
) -> VisionInterviewState:
    """Derive one current Vision chain without mutable caches or row recency."""
    review = _vision_review_selection(snapshot)
    if review is None:
        return VisionInterviewState(None, None, None, (), True)
    transcript = _vision_transcript(snapshot, review)
    if transcript is None:
        return VisionInterviewState(None, None, None, (), True)
    return VisionInterviewState(
        review.artifact,
        review.decision,
        review.open_revision,
        transcript,
        False,
    )


def _active_goal_exists(snapshot: WorkflowFactSnapshot) -> bool:
    """Use Task 2 durable Goal facts to gate Vision revision only."""
    accepted_ids = {
        item.product_goal_artifact_id
        for item in snapshot.product_goal_artifact_decisions
        if item.decision == "accepted"
    }
    resolved_ids = {
        item.product_goal_artifact_id for item in snapshot.product_goal_outcomes
    }
    return bool(accepted_ids - resolved_ids)


def _interview_instance_key(
    snapshot: WorkflowFactSnapshot,
    state: VisionInterviewState,
) -> str | None:
    """Advance the attempt instance after each persisted interview turn."""
    revision = state.open_revision
    if state.transcript:
        turn_id = max(item.vision_interview_turn_id for item in state.transcript)
        return f"after-turn:{turn_id}"
    if (
        state.artifact is not None
        and state.decision is not None
        and state.decision.decision in {"feedback", "rejected"}
    ):
        return f"after-turn:{state.artifact.source_interview_turn_id}"
    revision_id = None if revision is None else revision.vision_revision_intent_id
    turns = tuple(
        item
        for item in snapshot.vision_interview_turns
        if item.revision_intent_id == revision_id
    )
    if not turns:
        if revision is None:
            return None
        return f"revision:{revision.vision_revision_intent_id}"
    return f"after-turn:{max(item.vision_interview_turn_id for item in turns)}"


def _latest_vision_evidence_stale_failure(
    snapshot: WorkflowFactSnapshot,
    *,
    instance_key: str | None,
) -> bool:
    """Return whether the current clarification attempt failed on stale evidence."""
    attempts = tuple(
        item
        for item in snapshot.node_attempts
        if item.node_id == "vision.interview"
        and item.instance_key == instance_key
        and item.outcome == "failure"
    )
    if not attempts:
        return False
    latest = max(attempts, key=lambda item: item.attempt_id)
    return latest.failure_code == "VISION_EVIDENCE_STALE"


def _interview_rule_for_revision(
    state: VisionInterviewState,
    instance_key: str | None,
) -> tuple[RuleEvaluation, ...]:
    """Route clarification for an active revision lineage."""
    if not state.transcript:
        return (
            RuleEvaluation(RuleCategory.SATISFIED, "VISION_BOOTSTRAP_REQUIRED"),
        )
    if state.open_revision is None:
        message = "Revision interview routing requires an open revision intent."
        raise RuntimeError(message)
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "VISION_REVISION_INTERVIEW_REQUIRED",
            instance_key=instance_key,
            fact_references=(_interview_reference(state.open_revision),),
        ),
    )


def _interview_rule_for_candidate(
    state: VisionInterviewState,
    instance_key: str | None,
) -> tuple[RuleEvaluation, ...]:
    """Route bootstrap-derived draft clarification or review."""
    if not state.transcript:
        return (
            RuleEvaluation(RuleCategory.SATISFIED, "VISION_BOOTSTRAP_REQUIRED"),
        )
    latest = state.transcript[-1]
    if latest.is_complete:
        return (RuleEvaluation(RuleCategory.SATISFIED, "VISION_REVIEW_PENDING"),)
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "VISION_CLARIFICATION_REQUIRED",
            instance_key=instance_key,
        ),
    )


def _interview_rule_for_review(
    state: VisionInterviewState,
    instance_key: str | None,
) -> tuple[RuleEvaluation, ...]:
    """Route accepted and returned Vision artifact states."""
    if state.decision is None:
        return (RuleEvaluation(RuleCategory.SATISFIED, "VISION_REVIEW_PENDING"),)
    if state.decision.decision not in {"feedback", "rejected"}:
        return (RuleEvaluation(RuleCategory.SATISFIED, "VISION_ACCEPTED"),)
    if state.artifact is None:
        message = "Vision review recovery requires an artifact."
        raise RuntimeError(message)
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "VISION_REVISION_REQUIRED",
            instance_key=instance_key,
            fact_references=(
                _reference(
                    "vision",
                    state.artifact.vision_artifact_id,
                    state.artifact.content_fingerprint,
                ),
            ),
            recommendation_kind=RecommendationKind.RECOVERY,
        ),
    )


def _interview_rule_for_state(
    state: VisionInterviewState,
    instance_key: str | None,
) -> tuple[RuleEvaluation, ...]:
    """Select the current Vision interview route after common guards."""
    if state.open_revision is not None:
        return _interview_rule_for_revision(state, instance_key)
    if state.artifact is None:
        return _interview_rule_for_candidate(state, instance_key)
    return _interview_rule_for_review(state, instance_key)


def _vision_interview_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    """Offer clarification only after a bootstrap draft exists."""
    state = select_vision_interview_state(snapshot)
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    instance_key = _interview_instance_key(snapshot, state)
    if _latest_vision_evidence_stale_failure(snapshot, instance_key=instance_key):
        return (RuleEvaluation(RuleCategory.SATISFIED, "VISION_BOOTSTRAP_REQUIRED"),)
    return _interview_rule_for_state(state, instance_key)


def _bootstrap_rule_for_state(
    state: VisionInterviewState,
) -> tuple[RuleEvaluation, ...]:
    """Select normal bootstrap availability after common recovery guards."""
    if state.open_revision is not None:
        if state.transcript:
            return (RuleEvaluation(RuleCategory.SATISFIED, "VISION_REVISION_ACTIVE"),)
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "VISION_REVISION_BOOTSTRAP_REQUIRED",
                instance_key=f"revision:{state.open_revision.vision_revision_intent_id}",
                fact_references=(_interview_reference(state.open_revision),),
            ),
        )
    if state.artifact is None and not state.transcript:
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "VISION_BOOTSTRAP_REQUIRED",
            ),
        )
    if state.decision is not None and state.decision.decision in {
        "feedback",
        "rejected",
    }:
        return (
            RuleEvaluation(RuleCategory.SATISFIED, "VISION_CLARIFICATION_REQUIRED"),
        )
    return (RuleEvaluation(RuleCategory.SATISFIED, "VISION_BOOTSTRAP_NOT_REQUIRED"),)


def _vision_bootstrap_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    """Offer explicit context-grounded generation before clarification."""
    state = select_vision_interview_state(snapshot)
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    instance_key = _interview_instance_key(snapshot, state)
    if _latest_vision_evidence_stale_failure(snapshot, instance_key=instance_key):
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "VISION_EVIDENCE_STALE",
                instance_key=(
                    None
                    if state.open_revision is None
                    else f"revision:{state.open_revision.vision_revision_intent_id}"
                ),
                recommendation_kind=RecommendationKind.RECOVERY,
            ),
        )
    return _bootstrap_rule_for_state(state)


def _vision_interview_review_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = select_vision_interview_state(snapshot)
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if state.artifact is None or state.decision is not None:
        return (RuleEvaluation(RuleCategory.SATISFIED, "VISION_REVIEW_NOT_PENDING"),)
    return (
        RuleEvaluation(
            RuleCategory.WAITING,
            "VISION_REVIEW_REQUIRED",
            fact_references=(
                _reference(
                    "vision",
                    state.artifact.vision_artifact_id,
                    state.artifact.content_fingerprint,
                ),
            ),
        ),
    )


def _vision_revision_start_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = select_vision_interview_state(snapshot)
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if (
        state.artifact is None
        or state.decision is None
        or state.decision.decision != "accepted"
        or state.open_revision is not None
        or _active_goal_exists(snapshot)
    ):
        return (RuleEvaluation(RuleCategory.SATISFIED, "VISION_REVISION_NOT_ELIGIBLE"),)
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "VISION_REVISION_AVAILABLE",
            fact_references=(
                _reference(
                    "vision",
                    state.artifact.vision_artifact_id,
                    state.artifact.content_fingerprint,
                ),
            ),
            recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
        ),
    )


VISION_INTERVIEW_NODES: tuple[NodeSpec, ...] = (
    NodeSpec(
        node_id="vision.bootstrap",
        child_graph_id="vision",
        request_kind="generate_vision_bootstrap",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(),
        evaluate_rule=_vision_bootstrap_rule,
        agentic_execution=AgenticExecutionSpec(
            active_reason="VISION_BOOTSTRAP_ACTIVE",
            failure_reason="VISION_BOOTSTRAP_FAILED",
            recovery_reason="VISION_BOOTSTRAP_RECOVERY_REQUIRED",
        ),
    ),
    NodeSpec(
        node_id="vision.interview",
        child_graph_id="vision",
        request_kind="record_vision_interview_turn",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="user_text", value_type="string"),
        ),
        evaluate_rule=_vision_interview_rule,
        agentic_execution=AgenticExecutionSpec(
            active_reason="VISION_INTERVIEW_ACTIVE",
            failure_reason="VISION_INTERVIEW_FAILED",
            recovery_reason="VISION_INTERVIEW_RECOVERY_REQUIRED",
        ),
    ),
    NodeSpec(
        node_id="vision.review",
        child_graph_id="vision",
        request_kind="decide_vision_review",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="vision_artifact_id", value_type="integer"),
            InputField(name="vision_fingerprint", value_type="string"),
            InputField(name="decision", value_type="string"),
            InputField(name="rationale", value_type="string"),
        ),
        evaluate_rule=_vision_interview_review_rule,
    ),
    NodeSpec(
        node_id="vision.revision.start",
        child_graph_id="vision",
        request_kind="begin_vision_revision",
        recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
        required_inputs=(
            InputField(name="source_vision_artifact_id", value_type="integer"),
            InputField(name="source_vision_fingerprint", value_type="string"),
            InputField(name="reason", value_type="string"),
        ),
        evaluate_rule=_vision_revision_start_rule,
    ),
)

__all__ = [
    "VISION_INTERVIEW_NODES",
    "VisionInterviewState",
    "artifact_reference",
    "authority_reference",
    "select_vision_interview_state",
]
