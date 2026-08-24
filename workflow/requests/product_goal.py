"""Typed Product Goal lifecycle transition requests."""

from typing import ClassVar, Literal

from pydantic import Field

from workflow.contracts import JsonObject
from workflow.requests.base import PositionedRequest


class RecordProductGoalInterviewTurn(PositionedRequest):
    """Persist one host-validated Goal interview turn."""

    kind: Literal["record_product_goal_interview_turn"] = (
        "record_product_goal_interview_turn"
    )
    node_id: ClassVar[str] = "goal.interview"
    user_text: str = Field(min_length=1)
    updated_components: JsonObject
    product_goal_statement: str = Field(min_length=1)
    is_complete: bool
    clarifying_questions: tuple[str, ...]
    attempt_id: int
    attempt_fingerprint: str = Field(min_length=1)


class DecideProductGoalReview(PositionedRequest):
    """Record one exact human decision for the pending Goal candidate."""

    kind: Literal["decide_product_goal_review"] = "decide_product_goal_review"
    node_id: ClassVar[str] = "goal.review"
    product_goal_artifact_id: int
    product_goal_fingerprint: str = Field(min_length=1)
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: str = ""


class FulfillProductGoal(PositionedRequest):
    """Record the terminal fulfillment of the active Product Goal."""

    kind: Literal["fulfill_product_goal"] = "fulfill_product_goal"
    node_id: ClassVar[str] = "goal.fulfill"
    product_goal_artifact_id: int
    product_goal_fingerprint: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class AbandonProductGoal(PositionedRequest):
    """Record the terminal abandonment of the active Product Goal."""

    kind: Literal["abandon_product_goal"] = "abandon_product_goal"
    node_id: ClassVar[str] = "goal.abandon"
    product_goal_artifact_id: int
    product_goal_fingerprint: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
