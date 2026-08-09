"""Vision ADK adapter tests without provider execution."""

from __future__ import annotations

import pytest

from adapters.adk.agents.vision import root_agent
from adapters.adk.recipes import (
    AgenticRecipeNodes,
    AttemptCompletionContext,
    RecipeOutput,
    UnknownAdkRecipeError,
    _vision_interview_output_adapter,
    build_agentic_recipe_registry,
)
from services.application import (
    AgileForgeApplication,
    VisionInterviewRequest,
    VisionReviewRequest,
    VisionRevisionRequest,
)
from services.contracts.vision import (
    VisionInterviewInput,
    VisionInterviewOutput,
)
from services.node_attempt_replay import NodeAttemptReplayQuery, TransitionReplayQuery
from workflow.contracts import (
    GRAPH_VERSION,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
)


def test_vision_interview_agent_uses_the_strict_v2_contract() -> None:
    """Keep the active Vision recipe bound to its interview contract."""
    assert root_agent.input_schema is VisionInterviewInput
    assert root_agent.output_schema is VisionInterviewOutput


def test_recipe_catalog_excludes_legacy_vision_and_adapts_interview_output() -> None:
    """Expose only the v2 Vision interview recipe and its output adapter."""
    registry = build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            authority_compile=root_agent,
            authority_repair=root_agent,
            vision_interview=root_agent,
            product_goal=root_agent,
            backlog_generation=root_agent,
            roadmap_generation=root_agent,
            story_generation=root_agent,
            sprint_planning=root_agent,
        ),
        execution_settings={"timeout_seconds": 5.0, "max_attempts": 1},
    )
    context = AttemptCompletionContext(
        project_id=1,
        graph_version=GRAPH_VERSION,
        fact_fingerprint="sha256:facts",
        decision_fingerprint="sha256:decision",
        instance_key=None,
        attempt_id=2,
        attempt_fingerprint="sha256:attempt",
        idempotency_key="vision:complete",
        actor="operator@example.com",
        correlation_id=None,
        normalized_input={"mode": "initial", "user_response": "Build a tool."},
    )

    with pytest.raises(UnknownAdkRecipeError):
        registry.require("vision.generate")
    interview = registry.require("vision.interview").output_adapter(
        RecipeOutput(
            payload={
                "updated_components": {
                    "project_name": "Vision",
                    "target_user": "Operators",
                    "problem": "State drift",
                    "product_category": "Tool",
                    "key_benefit": "Trust",
                    "competitors": "Spreadsheets",
                    "differentiator": "Facts",
                },
                "project_vision_statement": "A trusted workflow tool.",
                "is_complete": True,
                "clarifying_questions": [],
            }
        ),
        context,
    )

    assert interview.kind == "record_vision_interview_turn"


def test_vision_adapter_uses_trusted_attempt_input_for_human_turn() -> None:
    """Model output cannot replace the captured user response or interview mode."""
    completion = _vision_interview_output_adapter(
        RecipeOutput(
            payload={
                "updated_components": {
                    "project_name": "Vision",
                    "target_user": "Operators",
                    "problem": "State drift",
                    "product_category": "Tool",
                    "key_benefit": "Trust",
                    "competitors": "Spreadsheets",
                    "differentiator": "Facts",
                },
                "project_vision_statement": "A trusted workflow tool.",
                "is_complete": True,
                "clarifying_questions": [],
            }
        ),
        AttemptCompletionContext(
            project_id=1,
            graph_version=GRAPH_VERSION,
            fact_fingerprint="sha256:facts",
            decision_fingerprint="sha256:decision",
            instance_key=None,
            attempt_id=2,
            attempt_fingerprint="sha256:attempt",
            idempotency_key="vision:complete",
            actor="operator@example.com",
            correlation_id=None,
            normalized_input={
                "mode": "revision",
                "user_response": "Correct the target user.",
            },
        ),
    )

    assert completion.mode == "revision"
    assert completion.user_text == "Correct the target user."


class _PositionMustNotRunDomain:
    """Fail if a replaying application request reads graph state."""

    def position(self, project_id: int) -> object:
        del project_id
        message = "receipt replay must happen before position reads"
        raise AssertionError(message)

    def transition(self, request: object) -> TransitionResult:
        del request
        message = "receipt replay must happen before transitions"
        raise AssertionError(message)


class _ReceiptReplay:
    """Return a stored result for one repeated Vision lifecycle request."""

    def __init__(self, result: TransitionResult) -> None:
        self.result = result
        self.calls: list[object] = []

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult:
        self.calls.append(query)
        if query.user_text == "Same answer.":
            return self.result
        return _conflict()

    def replay_transition(self, query: TransitionReplayQuery) -> TransitionResult:
        self.calls.append(query)
        expected = {
            "decide_vision_review": {
                "decision": "feedback",
                "rationale": "Clarify the audience.",
            },
            "begin_vision_revision": {"reason": "Intent changed."},
        }
        if query.operator_input == expected[query.request_kind]:
            return self.result
        return _conflict()


def _conflict() -> TransitionResult:
    return TransitionResult(
        ok=False,
        error=WorkflowError(
            code=WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
            message="The idempotency key was already used for different input.",
        ),
    )


def test_review_and_revision_replay_before_position_reads() -> None:
    """Repeated Vision commands recover their durable receipts after advancement."""
    result = TransitionResult(ok=True, replayed=True)
    replay = _ReceiptReplay(result)
    app = object.__new__(AgileForgeApplication)
    app._workflow_domain = _PositionMustNotRunDomain()
    app._vision_interview_input = replay
    app._prepared_agentic_inputs = type(
        "PreparedVisionInputServices",
        (),
        {"vision_interview": replay},
    )()

    interview = app.run_vision_interview(
        VisionInterviewRequest(
            project_id=7,
            graph_version="agileforge.workflow.v2",
            fact_fingerprint="sha256:facts",
            decision_fingerprint="sha256:decision",
            user_text="Same answer.",
            idempotency_key="interview-retry",
            actor="operator@example.com",
        )
    )
    review = app.review_vision(
        VisionReviewRequest(
            project_id=7,
            decision="feedback",
            rationale="Clarify the audience.",
            idempotency_key="review-retry",
            actor="operator@example.com",
        )
    )
    revision = app.begin_vision_revision(
        VisionRevisionRequest(
            project_id=7,
            reason="Intent changed.",
            idempotency_key="revision-retry",
            actor="operator@example.com",
        )
    )

    assert review == result
    assert revision == result
    assert interview == result
    assert isinstance(replay.calls[0], NodeAttemptReplayQuery)
    assert isinstance(replay.calls[1], TransitionReplayQuery)
    assert isinstance(replay.calls[2], TransitionReplayQuery)


def test_replay_rejects_changed_vision_operator_input_before_position_reads() -> None:
    """A reused key cannot replay a different human answer, decision, or reason."""
    app = object.__new__(AgileForgeApplication)
    app._workflow_domain = _PositionMustNotRunDomain()
    replay = _ReceiptReplay(TransitionResult(ok=True, replayed=True))
    app._vision_interview_input = replay
    app._prepared_agentic_inputs = type(
        "PreparedVisionInputServices",
        (),
        {"vision_interview": replay},
    )()

    interview = app.run_vision_interview(
        VisionInterviewRequest(
            project_id=7,
            graph_version="agileforge.workflow.v2",
            fact_fingerprint="sha256:facts",
            decision_fingerprint="sha256:decision",
            user_text="Changed answer.",
            idempotency_key="interview-retry",
            actor="operator@example.com",
        )
    )
    review = app.review_vision(
        VisionReviewRequest(
            project_id=7,
            decision="accepted",
            rationale="Clarify the audience.",
            idempotency_key="review-retry",
            actor="operator@example.com",
        )
    )
    revision = app.begin_vision_revision(
        VisionRevisionRequest(
            project_id=7,
            reason="Changed intent.",
            idempotency_key="revision-retry",
            actor="operator@example.com",
        )
    )

    assert interview.error is not None
    assert review.error is not None
    assert revision.error is not None
    assert interview.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert review.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    assert revision.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
