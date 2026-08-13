"""Semantic application boundary for Specification structuring."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from services.application import (
    AgenticActionRequest,
    AgileForgeApplication,
    SpecificationReviewRequest,
    SpecificationStructuringRequest,
    _agentic_execution_settings,
)
from workflow.contracts import (
    FactReference,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    TransitionResult,
    WorkflowError,
    WorkflowPosition,
)
from workflow.requests import DecideSpecification

if TYPE_CHECKING:
    import pytest

    from services.node_attempt_replay import NodeAttemptReplayQuery
    from workflow.contracts import JsonObject
    from workflow.requests import TransitionRequest

PROJECT_ID = 7
CANDIDATE_ID = 31


def _decision() -> NodeDecision:
    return NodeDecision(
        node_id="specification.structure",
        child_graph_id="specification",
        request_kind="structure_specification",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="SPECIFICATION_INITIAL_REQUIRED",
        fact_references=(
            FactReference(fact_type="vision", fact_id="1", fingerprint="vision"),
            FactReference(
                fact_type="product_goal",
                fact_id="2",
                fingerprint="goal",
            ),
            FactReference(
                fact_type="specification_source",
                fact_id="3",
                fingerprint="source",
            ),
        ),
        decision_fingerprint="decision",
    )


class _Domain:
    def __init__(self) -> None:
        decision = _decision()
        self.current = WorkflowPosition(
            project_id=PROJECT_ID,
            graph_version="agileforge.workflow.v2",
            fact_fingerprint="facts",
            evaluated_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
            available_nodes=(decision.node_id,),
            waiting_nodes=(),
            blocked_nodes=(),
            invalid_nodes=(),
            terminal=False,
            decisions=(decision,),
        )

    def position(self, project_id: int) -> WorkflowPosition:
        assert project_id == PROJECT_ID
        return self.current

    def transition(self, request: TransitionRequest) -> TransitionResult:
        raise AssertionError(request.kind)

    def load_persisted_attempt_input(
        self,
        *,
        project_id: int,
        attempt_id: int,
        attempt_fingerprint: str,
    ) -> JsonObject:
        del project_id, attempt_id, attempt_fingerprint
        return {}


class _InputService:
    def __init__(self, replay: TransitionResult | None = None) -> None:
        self.replay_result = replay
        self.build_calls = 0

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None:
        assert query.node_id == "specification.structure"
        return self.replay_result

    def build(self, *, project_id: int, decision: NodeDecision) -> JsonObject:
        assert project_id == PROJECT_ID
        assert decision == _decision()
        self.build_calls += 1
        return {
            "schema_version": "agileforge.spec-structuring-input.v1",
            "project_id": PROJECT_ID,
        }

    def revalidate_sources(
        self,
        project_id: int,
        persisted_input: JsonObject,
        /,
    ) -> WorkflowError | None:
        del project_id, persisted_input
        return None


class _ReviewDomain(_Domain):
    def __init__(self) -> None:
        review = NodeDecision(
            node_id="specification.review",
            child_graph_id="specification",
            request_kind="decide_specification",
            category=NodeCategory.WAITING,
            recommendation_kind=RecommendationKind.REQUIRED,
            reason_code="SPECIFICATION_REVIEW_REQUIRED",
            fact_references=(
                FactReference(
                    fact_type="specification_candidate",
                    fact_id=str(CANDIDATE_ID),
                    fingerprint="sha256:candidate",
                ),
            ),
            decision_fingerprint="review-decision",
        )
        self.current = WorkflowPosition(
            project_id=PROJECT_ID,
            graph_version="agileforge.workflow.v2",
            fact_fingerprint="review-facts",
            evaluated_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
            available_nodes=(),
            waiting_nodes=(review.node_id,),
            blocked_nodes=(),
            invalid_nodes=(),
            terminal=False,
            decisions=(review,),
        )
        self.requests: list[TransitionRequest] = []

    def transition(self, request: TransitionRequest) -> TransitionResult:
        self.requests.append(request)
        return TransitionResult(ok=True, applied_node_id="specification.review")


def _request() -> SpecificationStructuringRequest:
    return SpecificationStructuringRequest(
        project_id=PROJECT_ID,
        idempotency_key="structure-specification-7",
        actor="operator",
        correlation_id="manual-test-7",
    )


def test_structuring_uses_host_input_and_exact_graph_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public callers supply no payload, IDs, hashes, or lineage metadata."""
    input_service = _InputService()
    application = AgileForgeApplication(
        workflow_domain=_Domain(),
        specification_structuring_input=input_service,
    )
    captured: list[AgenticActionRequest] = []

    def _capture(
        _self: AgileForgeApplication,
        request: AgenticActionRequest,
    ) -> TransitionResult:
        captured.append(request)
        return TransitionResult(ok=True, applied_node_id=request.node_id)

    monkeypatch.setattr(AgileForgeApplication, "run_agentic_action", _capture)

    result = application.structure_specification(_request())

    assert result.ok
    assert input_service.build_calls == 1
    assert len(captured) == 1
    attempt = captured[0]
    assert attempt.node_id == "specification.structure"
    assert attempt.input_payload["schema_version"] == (
        "agileforge.spec-structuring-input.v1"
    )
    assert attempt.decision_fingerprint == "decision"


def test_structuring_persists_its_effective_generation_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bind recovery identity to the same explicit budget used by the ADK agent."""
    monkeypatch.setenv("SPECIFICATION_STRUCTURER_MAX_TOKENS", "24576")

    assert _agentic_execution_settings("specification.structure") == {
        "timeout_seconds": 120,
        "max_attempts": 2,
        "generation_config": {"max_output_tokens": 24_576},
    }
    assert _agentic_execution_settings("vision.interview") == {
        "timeout_seconds": 120,
        "max_attempts": 2,
    }


def test_structuring_uses_the_composed_agent_config_after_environment_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persist the immutable composed request config, not a later environment read."""
    composed_config: JsonObject = {"max_output_tokens": 11_111}
    monkeypatch.setenv("SPECIFICATION_STRUCTURER_MAX_TOKENS", "22222")

    assert _agentic_execution_settings(
        "specification.structure",
        specification_generation_config=composed_config,
    ) == {
        "timeout_seconds": 120,
        "max_attempts": 2,
        "generation_config": composed_config,
    }


def test_structuring_replays_before_reading_current_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated key returns its durable outcome without another provider call."""
    replay = TransitionResult(
        ok=True,
        replayed=True,
        applied_node_id="specification.structure",
    )
    service = _InputService(replay)
    application = AgileForgeApplication(
        workflow_domain=_Domain(),
        specification_structuring_input=service,
    )

    def _unexpected(*_args: object, **_kwargs: object) -> TransitionResult:
        message = "provider execution must not repeat"
        raise AssertionError(message)

    monkeypatch.setattr(AgileForgeApplication, "run_agentic_action", _unexpected)

    assert application.structure_specification(_request()) == replay
    assert service.build_calls == 0


def test_review_delegates_live_source_check_to_domain_transaction() -> None:
    """The application forwards acceptance without an earlier source probe."""
    service = _InputService()
    domain = _ReviewDomain()
    application = AgileForgeApplication(
        workflow_domain=domain,
        specification_structuring_input=service,
    )

    result = application.review_specification(
        SpecificationReviewRequest(
            project_id=PROJECT_ID,
            decision="accepted",
            rationale="Reviewed exact candidate.",
            expected_candidate_fingerprint="sha256:candidate",
            idempotency_key="review-source-stale",
            actor="operator",
        )
    )

    assert result.ok
    assert len(domain.requests) == 1
    decision = domain.requests[0]
    assert isinstance(decision, DecideSpecification)
    assert decision.repository_source_fingerprint is None


def test_feedback_delegates_without_application_source_probe() -> None:
    """Feedback reaches the domain without any application source precheck."""
    service = _InputService()
    domain = _ReviewDomain()
    application = AgileForgeApplication(
        workflow_domain=domain,
        specification_structuring_input=service,
    )

    result = application.review_specification(
        SpecificationReviewRequest(
            project_id=PROJECT_ID,
            decision="feedback",
            rationale="Refresh the repository-backed requirement.",
            expected_candidate_fingerprint="sha256:candidate",
            idempotency_key="review-source-feedback",
            actor="operator",
        )
    )

    assert result.ok
    assert len(domain.requests) == 1
    decision = domain.requests[0]
    assert isinstance(decision, DecideSpecification)
    assert decision.decision == "feedback"
    assert decision.repository_source_fingerprint is None
