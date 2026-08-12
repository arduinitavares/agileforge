"""Semantic application boundary for direct Specification authoring."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from services.application import (
    AgenticActionRequest,
    AgileForgeApplication,
    SpecificationAuthoringRequest,
)
from workflow.contracts import (
    FactReference,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    TransitionResult,
    WorkflowPosition,
)

if TYPE_CHECKING:
    import pytest

    from services.node_attempt_replay import NodeAttemptReplayQuery
    from workflow.contracts import JsonObject
    from workflow.requests import TransitionRequest

PROJECT_ID = 7


def _decision() -> NodeDecision:
    return NodeDecision(
        node_id="specification.author",
        child_graph_id="product_discovery",
        request_kind="author_specification",
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
        assert query.node_id == "specification.author"
        return self.replay_result

    def build(self, *, project_id: int, decision: NodeDecision) -> JsonObject:
        assert project_id == PROJECT_ID
        assert decision == _decision()
        self.build_calls += 1
        return {
            "schema_version": "agileforge.spec-authoring-input.v2",
            "project_id": PROJECT_ID,
        }


def _request() -> SpecificationAuthoringRequest:
    return SpecificationAuthoringRequest(
        project_id=PROJECT_ID,
        idempotency_key="author-specification-7",
        actor="operator",
        correlation_id="manual-test-7",
    )


def test_authoring_uses_host_input_and_exact_graph_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public callers supply no payload, IDs, hashes, or lineage metadata."""
    input_service = _InputService()
    application = AgileForgeApplication(
        workflow_domain=_Domain(),
        specification_authoring_input=input_service,
    )
    captured: list[AgenticActionRequest] = []

    def _capture(
        _self: AgileForgeApplication,
        request: AgenticActionRequest,
    ) -> TransitionResult:
        captured.append(request)
        return TransitionResult(ok=True, applied_node_id=request.node_id)

    monkeypatch.setattr(AgileForgeApplication, "run_agentic_action", _capture)

    result = application.author_specification(_request())

    assert result.ok
    assert input_service.build_calls == 1
    assert len(captured) == 1
    attempt = captured[0]
    assert attempt.node_id == "specification.author"
    assert attempt.input_payload["schema_version"] == (
        "agileforge.spec-authoring-input.v2"
    )
    assert attempt.decision_fingerprint == "decision"


def test_authoring_replays_before_reading_current_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated key returns its durable outcome without another provider call."""
    replay = TransitionResult(
        ok=True,
        replayed=True,
        applied_node_id="specification.author",
    )
    service = _InputService(replay)
    application = AgileForgeApplication(
        workflow_domain=_Domain(),
        specification_authoring_input=service,
    )

    def _unexpected(*_args: object, **_kwargs: object) -> TransitionResult:
        message = "provider execution must not repeat"
        raise AssertionError(message)

    monkeypatch.setattr(AgileForgeApplication, "run_agentic_action", _unexpected)

    assert application.author_specification(_request()) == replay
    assert service.build_calls == 0
