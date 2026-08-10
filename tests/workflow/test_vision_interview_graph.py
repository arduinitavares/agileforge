"""Pure state-matrix coverage for the isolated Project Vision graph."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from workflow.contracts import JsonObject, NodeCategory
from workflow.definitions.root import project_graph
from workflow.facts import (
    ProductGoalArtifactDecisionFact,
    ProductGoalOutcomeFact,
    ProjectFact,
    VisionArtifactDecisionFact,
    VisionArtifactFact,
    VisionInterviewTurnFact,
    VisionRevisionIntentFact,
    WorkflowFactSnapshot,
)

if TYPE_CHECKING:
    from workflow.contracts import NodeDecision, WorkflowPosition

NOW = datetime(2026, 8, 5, 12, tzinfo=UTC)
COMPONENTS: JsonObject = {
    "project_name": "Vision",
    "target_user": "Operators",
    "problem": "Untrusted workflow state",
    "product_category": "Tool",
    "key_benefit": "Durable decisions",
    "competitors": "Spreadsheets",
    "differentiator": "Typed facts",
}


def _artifact(identifier: int = 1, parent: int | None = None) -> VisionArtifactFact:
    return VisionArtifactFact(
        vision_artifact_id=identifier,
        version_number=identifier,
        components=COMPONENTS,
        statement="A durable Vision.",
        content_fingerprint=f"sha256:vision-{identifier}",
        supersedes_vision_artifact_id=parent,
        source_interview_turn_id=identifier,
        created_by="operator@example.com",
        created_at=NOW,
    )


def _decision(
    identifier: int = 1, decision: str = "accepted"
) -> VisionArtifactDecisionFact:
    return VisionArtifactDecisionFact.model_validate(
        {
            "vision_artifact_decision_id": identifier,
            "vision_artifact_id": identifier,
            "artifact_fingerprint": f"sha256:vision-{identifier}",
            "decision": decision,
            "rationale": "Reviewed.",
            "reviewer": "operator@example.com",
            "idempotency_key": f"review-{identifier}",
            "decided_at": NOW,
        }
    )


def _turn(
    *, complete: bool, mode: str = "initial", intent: int | None = None
) -> VisionInterviewTurnFact:
    return VisionInterviewTurnFact.model_validate(
        {
            "vision_interview_turn_id": 1,
            "mode": mode,
            "turn_number": 1,
            "revision_intent_id": intent,
            "prior_turn_id": None,
            "user_text": "Build a trusted workflow tool.",
            "components": COMPONENTS,
            "vision_statement": "A durable Vision.",
            "is_complete": complete,
            "clarifying_questions": [] if complete else ["Who is the user?"],
            "output_fingerprint": "sha256:turn",
            "workflow_node_attempt_id": 1,
            "attempt_fingerprint": "sha256:attempt",
            "recorded_at": NOW,
        }
    )


def _snapshot(**changes: object) -> WorkflowFactSnapshot:
    values: dict[str, object] = {
        "project": ProjectFact(
            project_id=1,
            name="Vision",
            origin="greenfield",
            created_at=NOW,
        )
    }
    values.update(changes)
    return WorkflowFactSnapshot.model_validate(values)


def _position(**changes: object) -> WorkflowPosition:
    return project_graph().evaluate(_snapshot(**changes), NOW)


def _node(position: WorkflowPosition, node_id: str) -> NodeDecision:
    return next(item for item in position.decisions if item.node_id == node_id)


def test_new_project_exposes_only_the_vision_interview_without_authority() -> None:
    """Vision begins from Project identity rather than authority or discovery."""
    position = _position()

    assert "vision.interview" in position.available_nodes
    assert "goal.interview" not in position.available_nodes
    assert all("discovery" not in node_id for node_id in position.available_nodes)


def test_incomplete_turn_keeps_interview_available() -> None:
    """An incomplete answer remains in the same human interview loop."""
    position = _position(vision_interview_turns=(_turn(complete=False),))

    assert _node(position, "vision.interview").category is NodeCategory.AVAILABLE
    assert "vision.review" not in position.waiting_nodes


def test_complete_vision_waits_for_exact_review_reference() -> None:
    """A complete turn creates one pending Vision and a fingerprinted review."""
    position = _position(
        vision_interview_turns=(_turn(complete=True),),
        vision_artifacts=(_artifact(),),
    )
    review = _node(position, "vision.review")

    assert review.category is NodeCategory.WAITING
    assert review.fact_references[0].fact_id == "1"
    assert review.fact_references[0].fingerprint == "sha256:vision-1"


def test_accepted_vision_unlocks_goal_but_not_discovery() -> None:
    """Vision acceptance advances only to Product Goal interviewing."""
    position = _position(
        vision_interview_turns=(_turn(complete=True),),
        vision_artifacts=(_artifact(),),
        vision_artifact_decisions=(_decision(),),
    )

    assert "goal.interview" in position.available_nodes
    assert "vision.revision.start" in position.available_nodes
    assert all("discovery" not in node_id for node_id in position.available_nodes)


def test_revision_is_blocked_while_an_accepted_goal_is_active() -> None:
    """Task 2 Goal outcomes gate optional Vision revision."""
    active_goal = ProductGoalArtifactDecisionFact(
        product_goal_artifact_decision_id=1,
        product_goal_artifact_id=7,
        artifact_fingerprint="sha256:goal",
        decision="accepted",
        rationale="Accepted.",
        reviewer="operator@example.com",
        idempotency_key="goal-review",
        decided_at=NOW,
    )
    position = _position(
        vision_interview_turns=(_turn(complete=True),),
        vision_artifacts=(_artifact(),),
        vision_artifact_decisions=(_decision(),),
        product_goal_artifact_decisions=(active_goal,),
    )

    assert "vision.revision.start" not in position.available_nodes


def test_resolved_goal_allows_revision_and_trace_rows_do_not_change_position() -> None:
    """Vision state is derived from durable facts, never ADK session traces."""
    outcome = ProductGoalOutcomeFact(
        product_goal_outcome_id=1,
        product_goal_artifact_id=7,
        artifact_fingerprint="sha256:goal",
        outcome="fulfilled",
        rationale="Delivered.",
        decided_by="operator@example.com",
        decided_at=NOW,
    )
    accepted_goal = ProductGoalArtifactDecisionFact(
        product_goal_artifact_decision_id=1,
        product_goal_artifact_id=7,
        artifact_fingerprint="sha256:goal",
        decision="accepted",
        rationale="Accepted.",
        reviewer="operator@example.com",
        idempotency_key="goal-review",
        decided_at=NOW,
    )
    common = {
        "vision_interview_turns": (_turn(complete=True),),
        "vision_artifacts": (_artifact(),),
        "vision_artifact_decisions": (_decision(),),
        "product_goal_artifact_decisions": (accepted_goal,),
        "product_goal_outcomes": (outcome,),
    }

    assert _position(**common).available_nodes == _position(**common).available_nodes
    assert "vision.revision.start" in _position(**common).available_nodes


def test_open_revision_requires_a_revision_interview() -> None:
    """A revision intent changes only the Vision interview mode and parent."""
    intent = VisionRevisionIntentFact(
        vision_revision_intent_id=2,
        source_vision_artifact_id=1,
        source_vision_fingerprint="sha256:vision-1",
        reason="Intent changed.",
        initiated_by="operator@example.com",
        initiated_at=NOW,
    )
    position = _position(
        vision_interview_turns=(_turn(complete=True),),
        vision_artifacts=(_artifact(),),
        vision_artifact_decisions=(_decision(),),
        vision_revision_intents=(intent,),
    )

    assert (
        _node(position, "vision.interview").reason_code
        == "VISION_REVISION_INTERVIEW_REQUIRED"
    )
