"""Vision ADK adapter tests without provider execution."""

from __future__ import annotations

from adapters.adk.agents.vision import legacy_root_agent, root_agent
from adapters.adk.recipes import (
    AgenticRecipeNodes,
    AttemptCompletionContext,
    RecipeOutput,
    _vision_interview_output_adapter,
    build_agentic_recipe_registry,
)
from services.application import (
    AgileForgeApplication,
    VisionReviewRequest,
    VisionRevisionRequest,
)
from services.contracts.vision import (
    InputSchema,
    OutputSchema,
    VisionInterviewInput,
    VisionInterviewOutput,
)
from workflow.contracts import TransitionResult
from workflow.requests import RecordVisionDraft


def test_legacy_and_interview_agents_keep_separate_contracts() -> None:
    """Keep the live root recipe separate from the isolated interview recipe."""
    assert legacy_root_agent.input_schema is InputSchema
    assert legacy_root_agent.output_schema is OutputSchema
    assert root_agent.input_schema is VisionInterviewInput
    assert root_agent.output_schema is VisionInterviewOutput


def test_legacy_and_interview_recipes_keep_separate_output_adapters() -> None:
    """The live root and isolated graph cannot adapt each other's output shape."""
    registry = build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            brownfield_curator=root_agent,
            authority_compile=root_agent,
            authority_repair=root_agent,
            vision_generation=legacy_root_agent,
            vision_interview=root_agent,
            backlog_generation=root_agent,
            roadmap_generation=root_agent,
            story_generation=root_agent,
            sprint_planning=root_agent,
        ),
        execution_settings={"timeout_seconds": 5.0, "max_attempts": 1},
    )
    context = AttemptCompletionContext(
        project_id=1,
        graph_version="agileforge.workflow.v1",
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

    legacy = registry.require("vision.generate").output_adapter(
        RecipeOutput(
            payload={
                "authority_id": 3,
                "authority_fingerprint": "sha256:authority",
                "canonical_content": {},
                "content_fingerprint": "sha256:legacy-vision",
            }
        ),
        context,
    )
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

    assert isinstance(legacy, RecordVisionDraft)
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
            graph_version="agileforge.workflow.v1",
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
        self.calls: list[tuple[str, int, str, str]] = []

    def replay_transition(
        self,
        *,
        request_kind: str,
        project_id: int,
        idempotency_key: str,
        actor: str,
        correlation_id: str | None,
    ) -> TransitionResult | None:
        del correlation_id
        self.calls.append((request_kind, project_id, idempotency_key, actor))
        return self.result


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
    assert replay.calls == [
        ("decide_vision_review", 7, "review-retry", "operator@example.com"),
        ("begin_vision_revision", 7, "revision-retry", "operator@example.com"),
    ]
