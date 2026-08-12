"""Vision ADK adapter tests without provider execution."""

from __future__ import annotations

from pydantic import TypeAdapter

from adapters.adk.agents.vision import repair_agent, root_agent
from adapters.adk.recipes import (
    AgenticRecipeNodes,
    AttemptCompletionContext,
    RecipeOutput,
    _vision_interview_output_adapter,
    build_agentic_recipe_registry,
)
from services.application import (
    AgileForgeApplication,
    VisionBootstrapRequest,
    VisionInterviewRequest,
    VisionReviewRequest,
    VisionRevisionRequest,
)
from services.contracts.vision import (
    VisionDraftOutput,
    VisionModelInput,
    VisionRepairInput,
)
from services.node_attempt_replay import NodeAttemptReplayQuery, TransitionReplayQuery
from workflow.contracts import (
    GRAPH_VERSION,
    JsonObject,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
)
from workflow.fingerprints import canonical_hash
from workflow.requests import RecordVisionInterviewTurn

_JSON_OBJECT = TypeAdapter(JsonObject)
EXECUTION_SETTINGS: JsonObject = {"timeout_seconds": 5.0, "max_attempts": 1}


def test_vision_interview_agent_uses_the_strict_v2_contract() -> None:
    """Keep the active Vision recipe bound to its interview contract."""
    assert root_agent.input_schema is VisionModelInput
    assert set(root_agent.input_schema.model_fields) == {"request"}
    assert root_agent.output_schema is VisionDraftOutput
    assert repair_agent.input_schema is VisionRepairInput
    assert repair_agent.output_schema is VisionDraftOutput


def _evidence() -> JsonObject:
    content = {"name": "Vision", "description": None}
    item = {
        "evidence_id": "project:metadata",
        "kind": "project_metadata",
        "relative_path": None,
        "content_fingerprint": canonical_hash(content),
        "trust": "operator_provided",
        "content": content,
        "truncated": False,
    }
    return _JSON_OBJECT.validate_python(
        {
            "schema_version": "agileforge.vision-evidence.v1",
            "items": [item],
            "warnings": [],
            "evidence_fingerprint": canonical_hash(
                {
                    "schema_version": "agileforge.vision-evidence.v1",
                    "items": [item],
                    "warnings": [],
                }
            ),
        }
    )


def _draft_payload() -> JsonObject:
    components = {
        "project_name": "Vision",
        "target_user": "Operators",
        "problem": "State drift",
        "product_category": "Tool",
        "key_benefit": "Trust",
        "competitors": "Spreadsheets",
        "differentiator": "Facts",
    }
    return _JSON_OBJECT.validate_python(
        {
            "schema_version": "agileforge.vision-draft.v1",
            "components": components,
            "component_basis": [
                {
                    "component": name,
                    "source_kinds": ["evidence"],
                    "evidence_ids": ["project:metadata"],
                    "assumption_ids": [],
                }
                for name in components
            ],
            "draft_statement": "A trusted workflow tool.",
            "assumptions": [],
            "conflicts": [],
            "clarifying_questions": [],
            "is_complete": True,
        }
    )


def _bootstrap_input() -> JsonObject:
    return _JSON_OBJECT.validate_python(
        {
            "request": {
                "schema_version": "agileforge.vision-input.v1",
                "operation": "bootstrap",
                "project_name": "Vision",
                "project_description": None,
                "evidence": _evidence(),
            },
            "preflight": None,
        }
    )


def _clarification_input() -> JsonObject:
    """Return a strict host-owned clarification envelope."""
    evidence = _evidence()
    return _JSON_OBJECT.validate_python(
        {
            "request": {
                "schema_version": "agileforge.vision-input.v1",
                "operation": "clarification",
                "project_name": "Vision",
                "project_description": None,
                "vision_evidence_snapshot_id": 10,
                "evidence": evidence,
                "current_components": {
                    "project_name": "Vision",
                    "target_user": None,
                    "problem": "State drift",
                    "product_category": "Tool",
                    "key_benefit": "Trust",
                    "competitors": "Spreadsheets",
                    "differentiator": "Facts",
                },
                "current_statement": "A draft.",
                "current_component_basis": [],
                "current_assumptions": [],
                "current_conflicts": [],
                "current_questions": [
                    {
                        "question_id": "question:target",
                        "text": "Who is the user?",
                        "affected_components": ["target_user"],
                        "conflict_ids": [],
                    }
                ],
                "human_response": "Correct the target user.",
                "addressed_question_ids": ["question:target"],
            },
            "preflight": {
                "expected_evidence_fingerprint": evidence["evidence_fingerprint"],
                "observed_evidence": evidence,
            },
        }
    )


def test_recipe_catalog_excludes_legacy_vision_and_adapts_interview_output() -> None:
    """Expose both explicit Vision recipes and their output adapter."""
    registry = build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            authority_compile=root_agent,
            authority_repair=root_agent,
            vision_interview=root_agent,
            vision_repair=repair_agent,
            product_goal=root_agent,
            specification_structurer=root_agent,
            backlog_generation=root_agent,
            roadmap_generation=root_agent,
            story_generation=root_agent,
            sprint_planning=root_agent,
        ),
        execution_settings=EXECUTION_SETTINGS,
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
        normalized_input=_bootstrap_input(),
    )

    bootstrap = registry.require("vision.bootstrap").output_adapter(
        RecipeOutput(payload=_draft_payload()),
        context,
    )

    assert registry.node_ids[:4] == (
        "authority.compile",
        "authority.repair",
        "vision.bootstrap",
        "vision.interview",
    )
    assert bootstrap.kind == "generate_vision_bootstrap"


def test_vision_adapter_uses_trusted_attempt_input_for_human_turn() -> None:
    """Model output cannot replace the captured human response or snapshot."""
    completion = _vision_interview_output_adapter(
        RecipeOutput(payload=_draft_payload()),
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
            normalized_input=_clarification_input(),
        ),
    )

    assert isinstance(completion, RecordVisionInterviewTurn)
    assert completion.operation == "clarification"
    expected_snapshot_id = 10
    assert completion.vision_evidence_snapshot_id == expected_snapshot_id
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
        if query.node_id == "vision.bootstrap" and query.user_text is None:
            return self.result
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
    app._vision_input = replay

    bootstrap = app.bootstrap_vision(
        VisionBootstrapRequest(
            project_id=7,
            idempotency_key="bootstrap-retry",
            actor="operator@example.com",
        )
    )
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
            expected_candidate_fingerprint="sha256:vision-replaced-after-review",
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
    assert bootstrap == result
    assert isinstance(replay.calls[0], NodeAttemptReplayQuery)
    assert isinstance(replay.calls[1], NodeAttemptReplayQuery)
    assert isinstance(replay.calls[2], TransitionReplayQuery)
    assert isinstance(replay.calls[3], TransitionReplayQuery)


def test_replay_rejects_changed_vision_operator_input_before_position_reads() -> None:
    """A reused key cannot replay a different human answer, decision, or reason."""
    app = object.__new__(AgileForgeApplication)
    app._workflow_domain = _PositionMustNotRunDomain()
    replay = _ReceiptReplay(TransitionResult(ok=True, replayed=True))
    app._vision_input = replay

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
