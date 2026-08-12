"""Provider-free public Product Goal lifecycle application tests."""

from __future__ import annotations

from datetime import UTC, datetime

from services.application import (
    AgenticActionRequest,
    AgileForgeApplication,
    ProductGoalInterviewRequest,
    ProductGoalLifecycleServices,
    ProductGoalOutcomeRequest,
    ProductGoalReviewRequest,
)
from services.node_attempt_replay import NodeAttemptReplayQuery, TransitionReplayQuery
from workflow.contracts import (
    FactReference,
    JsonObject,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    TransitionResult,
    WorkflowPosition,
)
from workflow.requests import (
    DecideProductGoalReview,
    FulfillProductGoal,
)

NOW = datetime(2026, 8, 5, tzinfo=UTC)
PROJECT_ID = 7
PRODUCT_GOAL_ARTIFACT_ID = 11
PRODUCT_GOAL_COMMAND_COUNT = 3
type _GoalInputCall = (
    NodeAttemptReplayQuery | TransitionReplayQuery | tuple[int, NodeDecision, str]
)


def _decision(
    node_id: str,
    *,
    category: NodeCategory,
    references: tuple[FactReference, ...] = (),
) -> NodeDecision:
    return NodeDecision(
        node_id=node_id,
        child_graph_id="product_goal",
        request_kind=node_id.replace(".", "_"),
        category=category,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="TEST",
        fact_references=references,
        decision_fingerprint=f"sha256:{node_id}",
    )


def _position(*decisions: NodeDecision) -> WorkflowPosition:
    return WorkflowPosition(
        project_id=PROJECT_ID,
        graph_version="agileforge.workflow.v2",
        fact_fingerprint="sha256:facts",
        evaluated_at=NOW,
        available_nodes=tuple(
            item.node_id
            for item in decisions
            if item.category is NodeCategory.AVAILABLE
        ),
        waiting_nodes=tuple(
            item.node_id for item in decisions if item.category is NodeCategory.WAITING
        ),
        blocked_nodes=(),
        invalid_nodes=(),
        terminal=False,
        decisions=decisions,
    )


class _Domain:
    """Capture public application interactions without durable I/O."""

    def __init__(self, position: WorkflowPosition) -> None:
        self.current_position = position
        self.calls: list[object] = []

    def position(self, project_id: int) -> WorkflowPosition:
        assert project_id == PROJECT_ID
        self.calls.append("position")
        return self.current_position

    def transition(self, request: object) -> TransitionResult:
        self.calls.append(request)
        return TransitionResult(ok=True)

    def load_persisted_attempt_input(
        self,
        *,
        project_id: int,
        attempt_id: int,
        attempt_fingerprint: str,
    ) -> JsonObject:
        del project_id, attempt_id, attempt_fingerprint
        message = "Agent execution is intercepted in this test."
        raise AssertionError(message)


class _GoalInput:
    """Capture replay and host-built input without touching a database."""

    def __init__(self, replay: TransitionResult | None = None) -> None:
        self.result = replay
        self.calls: list[_GoalInputCall] = []

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None:
        self.calls.append(query)
        return self.result

    def replay_transition(
        self, query: TransitionReplayQuery
    ) -> TransitionResult | None:
        self.calls.append(query)
        return self.result

    def build(
        self, project_id: int, decision: NodeDecision, user_text: str
    ) -> JsonObject:
        self.calls.append((project_id, decision, user_text))
        return {
            "project_name": "Goal application",
            "accepted_vision_statement": "A durable Vision.",
            "user_response": user_text,
            "prior_components": None,
        }


class _Application(AgileForgeApplication):
    """Capture the provider-facing handoff without executing ADK."""

    def __init__(
        self,
        *,
        workflow_domain: _Domain,
        product_goal_services: ProductGoalLifecycleServices,
    ) -> None:
        super().__init__(
            workflow_domain=workflow_domain,
            product_goal_services=product_goal_services,
        )
        self.agent_requests: list[AgenticActionRequest] = []

    def run_agentic_action(self, request: AgenticActionRequest) -> TransitionResult:
        self.agent_requests.append(request)
        return TransitionResult(ok=True, applied_node_id="goal.interview")


def _goal_interview_request() -> ProductGoalInterviewRequest:
    return ProductGoalInterviewRequest(
        project_id=PROJECT_ID,
        graph_version="agileforge.workflow.v2",
        fact_fingerprint="sha256:facts",
        decision_fingerprint="sha256:goal.interview",
        user_text="Operators need reliable lifecycle evidence.",
        idempotency_key="goal-interview",
        actor="operator@example.com",
    )


def test_public_goal_interview_replays_then_uses_host_prepared_input() -> None:
    """The transport submits only text; the host derives all durable context."""
    decision = _decision(
        "goal.interview",
        category=NodeCategory.AVAILABLE,
        references=(FactReference(fact_type="vision", fact_id="3", fingerprint="v"),),
    )
    domain = _Domain(_position(decision))
    prepared = _GoalInput()
    app = _Application(
        workflow_domain=domain,
        product_goal_services=ProductGoalLifecycleServices(
            interview_input=prepared,
        ),
    )

    result = app.run_product_goal_interview(_goal_interview_request())

    assert result.ok is True
    assert domain.calls == ["position"]
    assert isinstance(prepared.calls[0], NodeAttemptReplayQuery)
    assert prepared.calls[0].user_text == "Operators need reliable lifecycle evidence."
    assert isinstance(prepared.calls[1], tuple)
    assert prepared.calls[1][2] == "Operators need reliable lifecycle evidence."
    assert len(app.agent_requests) == 1
    agent_request = app.agent_requests[0]
    assert agent_request.node_id == "goal.interview"
    assert agent_request.input_payload["accepted_vision_statement"] == (
        "A durable Vision."
    )


def test_product_goal_commands_replay_before_any_state_read() -> None:
    """All public child-graph commands recover receipts before reading state."""
    replayed = TransitionResult(ok=True, replayed=True)
    prepared = _GoalInput(replay=replayed)
    domain = _Domain(_position())
    app = _Application(
        workflow_domain=domain,
        product_goal_services=ProductGoalLifecycleServices(
            interview_input=prepared,
        ),
    )

    result = app.run_product_goal_interview(_goal_interview_request())
    review = app.review_product_goal(
        ProductGoalReviewRequest(
            project_id=PROJECT_ID,
            decision="feedback",
            rationale="Clarify the success signal.",
            expected_candidate_fingerprint="sha256:goal-replaced-after-review",
            idempotency_key="goal-review",
            actor="operator@example.com",
        )
    )
    outcome = app.resolve_product_goal(
        ProductGoalOutcomeRequest(
            project_id=PROJECT_ID,
            outcome="fulfilled",
            rationale="The goal outcome is clear.",
            idempotency_key="goal-outcome",
            actor="operator@example.com",
        )
    )
    assert all(item == replayed for item in (result, review, outcome))
    assert domain.calls == []
    assert len(prepared.calls) == PRODUCT_GOAL_COMMAND_COUNT
    assert isinstance(prepared.calls[0], NodeAttemptReplayQuery)
    assert all(isinstance(item, TransitionReplayQuery) for item in prepared.calls[1:])


def test_public_goal_review_and_outcome_resolve_exact_identities() -> None:
    """Review and outcome callers cannot submit artifact IDs or fingerprints."""
    goal = FactReference(
        fact_type="product_goal",
        fact_id=str(PRODUCT_GOAL_ARTIFACT_ID),
        fingerprint="g",
    )
    review_domain = _Domain(
        _position(
            _decision(
                "goal.review",
                category=NodeCategory.WAITING,
                references=(goal,),
            )
        )
    )
    prepared = _GoalInput()
    review_app = _Application(
        workflow_domain=review_domain,
        product_goal_services=ProductGoalLifecycleServices(
            interview_input=prepared,
        ),
    )

    review = review_app.review_product_goal(
        ProductGoalReviewRequest(
            project_id=PROJECT_ID,
            decision="feedback",
            rationale="Clarify the measurable signal.",
            idempotency_key="goal-review",
            actor="operator@example.com",
        )
    )

    assert review.ok is True
    assert isinstance(prepared.calls[0], TransitionReplayQuery)
    review_request = review_domain.calls[-1]
    assert isinstance(review_request, DecideProductGoalReview)
    assert review_request.product_goal_artifact_id == PRODUCT_GOAL_ARTIFACT_ID
    assert review_request.product_goal_fingerprint == "g"

    outcome_domain = _Domain(
        _position(
            _decision(
                "goal.fulfill",
                category=NodeCategory.AVAILABLE,
                references=(goal,),
            )
        )
    )
    outcome_app = _Application(
        workflow_domain=outcome_domain,
        product_goal_services=ProductGoalLifecycleServices(
            interview_input=_GoalInput(),
        ),
    )
    outcome = outcome_app.resolve_product_goal(
        ProductGoalOutcomeRequest(
            project_id=PROJECT_ID,
            outcome="fulfilled",
            rationale="The agreed success signal was reached.",
            idempotency_key="goal-outcome",
            actor="operator@example.com",
        )
    )

    assert outcome.ok is True
    outcome_request = outcome_domain.calls[-1]
    assert isinstance(outcome_request, FulfillProductGoal)
    assert outcome_request.product_goal_artifact_id == PRODUCT_GOAL_ARTIFACT_ID
    assert outcome_request.product_goal_fingerprint == "g"
