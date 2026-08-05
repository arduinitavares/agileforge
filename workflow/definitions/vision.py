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
        VisionArtifactDecisionFact,
        VisionArtifactFact,
        VisionRevisionIntentFact,
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


LEGACY_VISION_NODES: tuple[NodeSpec, ...] = (
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


@dataclass(frozen=True)
class VisionInterviewState:
    """Current isolated Vision artifact, decision, and open revision intent."""

    artifact: VisionArtifactFact | None
    decision: VisionArtifactDecisionFact | None
    open_revision: VisionRevisionIntentFact | None
    conflict: bool


def _interview_reference(turn: VisionRevisionIntentFact) -> FactReference:
    return _reference(
        "vision_revision_intent",
        turn.vision_revision_intent_id,
        turn.source_vision_fingerprint,
    )


def _isolated_vision_state(snapshot: WorkflowFactSnapshot) -> VisionInterviewState:
    """Derive one current Vision chain without consulting authority or ADK traces."""
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
        return VisionInterviewState(None, None, None, True)
    artifact = current[0] if current else None
    decisions = {
        item.vision_artifact_id: item for item in snapshot.vision_artifact_decisions
    }
    if len(decisions) != len(snapshot.vision_artifact_decisions):
        return VisionInterviewState(None, None, None, True)
    for item in snapshot.vision_artifact_decisions:
        referenced = by_id.get(item.vision_artifact_id)
        if (
            referenced is None
            or referenced.content_fingerprint != item.artifact_fingerprint
        ):
            return VisionInterviewState(None, None, None, True)
    open_intents = []
    for intent in snapshot.vision_revision_intents:
        source = by_id.get(intent.source_vision_artifact_id)
        if (
            source is None
            or source.content_fingerprint != intent.source_vision_fingerprint
        ):
            return VisionInterviewState(None, None, None, True)
        completed = any(
            turn.mode == "revision"
            and turn.revision_intent_id == intent.vision_revision_intent_id
            and any(
                artifact_item.source_interview_turn_id == turn.vision_interview_turn_id
                for artifact_item in snapshot.vision_artifacts
            )
            for turn in snapshot.vision_interview_turns
        )
        if not completed:
            open_intents.append(intent)
    if len(open_intents) > 1:
        return VisionInterviewState(None, None, None, True)
    return VisionInterviewState(
        artifact,
        None if artifact is None else decisions.get(artifact.vision_artifact_id),
        open_intents[0] if open_intents else None,
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
    revision: VisionRevisionIntentFact | None,
) -> str | None:
    """Advance the attempt instance after each persisted interview turn."""
    mode = "revision" if revision is not None else "initial"
    revision_id = None if revision is None else revision.vision_revision_intent_id
    turns = tuple(
        item
        for item in snapshot.vision_interview_turns
        if item.mode == mode and item.revision_intent_id == revision_id
    )
    if not turns:
        return None
    return f"after-turn:{max(item.vision_interview_turn_id for item in turns)}"


def _vision_interview_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    """Offer only the isolated human Vision interview lifecycle."""
    state = _isolated_vision_state(snapshot)
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if state.open_revision is not None:
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "VISION_REVISION_INTERVIEW_REQUIRED",
                instance_key=_interview_instance_key(snapshot, state.open_revision),
                fact_references=(_interview_reference(state.open_revision),),
            ),
        )
    if state.artifact is None:
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "VISION_INTERVIEW_REQUIRED",
                instance_key=_interview_instance_key(snapshot, None),
            ),
        )
    if state.decision is None:
        return (RuleEvaluation(RuleCategory.SATISFIED, "VISION_REVIEW_PENDING"),)
    if state.decision.decision in {"feedback", "rejected"}:
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "VISION_REVISION_REQUIRED",
                instance_key=_interview_instance_key(snapshot, None),
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
    return (RuleEvaluation(RuleCategory.SATISFIED, "VISION_ACCEPTED"),)


def _vision_interview_review_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = _isolated_vision_state(snapshot)
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
    state = _isolated_vision_state(snapshot)
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


def _goal_interview_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    state = _isolated_vision_state(snapshot)
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if (
        state.artifact is None
        or state.decision is None
        or state.decision.decision != "accepted"
    ):
        return (RuleEvaluation(RuleCategory.SATISFIED, "PRODUCT_GOAL_NOT_READY"),)
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "PRODUCT_GOAL_INTERVIEW_REQUIRED",
            fact_references=(
                _reference(
                    "vision",
                    state.artifact.vision_artifact_id,
                    state.artifact.content_fingerprint,
                ),
            ),
        ),
    )


VISION_INTERVIEW_NODES: tuple[NodeSpec, ...] = (
    NodeSpec(
        node_id="vision.interview",
        child_graph_id="vision",
        request_kind="record_vision_interview_turn",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="mode", value_type="string"),
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
    NodeSpec(
        node_id="goal.interview",
        child_graph_id="product_goal",
        request_kind="goal_interview_pending_implementation",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(),
        evaluate_rule=_goal_interview_rule,
    ),
)

# The root graph intentionally retains its legacy nodes until the Task 5 cutover.
VISION_NODES = LEGACY_VISION_NODES


__all__ = [
    "VISION_INTERVIEW_NODES",
    "VISION_NODES",
    "PhaseArtifactState",
    "accepted_current_artifact",
    "artifact_reference",
    "authority_reference",
    "phase_artifact_state",
]
