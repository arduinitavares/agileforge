"""Uniform durable lease lifecycle for domain-classified agentic nodes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlmodel import Session, select

from models.core import Project
from models.workflow import WorkflowNodeAttempt, WorkflowNodeAttemptOutcome
from workflow.contracts import (
    GRAPH_VERSION,
    JsonObject,
    NodeCategory,
    RecommendationKind,
    WorkflowErrorCode,
)
from workflow.definitions.root import ROOT_GRAPH
from workflow.domain import WorkflowDomain
from workflow.graph import (
    AgenticExecutionSpec,
    ChildGraphSpec,
    NodeRule,
    NodeSpec,
    RuleCategory,
    RuleEvaluation,
    WorkflowGraph,
)
from workflow.requests import StartNodeAttempt

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from workflow.contracts import NodeDecision
    from workflow.facts import WorkflowFactSnapshot

EVALUATED_AT = datetime(2026, 8, 3, 12, tzinfo=UTC)
LEASE_SECONDS = 60
EXECUTION_SETTINGS: JsonObject = {"timeout_seconds": 5.0, "max_attempts": 1}
EXPECTED_AGENTIC_NODE_COUNT = 8
EXPECTED_REPLACEMENT_ATTEMPT_COUNT = 2


@dataclass
class MutableClock:
    """Clock advanced through one complete lease lifecycle."""

    now_value: datetime

    def now(self) -> datetime:
        """Return the controlled current instant."""
        return self.now_value


class CatalogRecipeRegistry:
    """Domain-test registry implementing only the lookup protocol."""

    def __init__(self, node_ids: tuple[str, ...]) -> None:
        """Retain the exact domain-classified node IDs."""
        self._node_ids = frozenset(node_ids)

    def require(self, node_id: str) -> object:
        """Return a marker for catalog nodes and fail closed otherwise."""
        if node_id not in self._node_ids:
            raise LookupError(node_id)
        return node_id


def _seed_project(engine: Engine) -> int:
    with Session(engine) as session:
        project = Project(name="Agentic lifecycle", origin="greenfield")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        return project.project_id


def _available_rule(node_id: str) -> NodeRule:
    instance_key = f"instance:{node_id}"

    def evaluate(
        _snapshot: WorkflowFactSnapshot,
        _evaluated_at: datetime,
    ) -> tuple[RuleEvaluation, ...]:
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "AGENTIC_TEST_READY",
                instance_key=instance_key,
            ),
        )

    return evaluate


def _agentic_graph(node_id: str, rule: NodeRule | None = None) -> WorkflowGraph:
    return WorkflowGraph(
        graph_version=GRAPH_VERSION,
        root=ChildGraphSpec(
            child_graph_id="agentic_test",
            nodes=(
                NodeSpec(
                    node_id=node_id,
                    child_graph_id="agentic_test",
                    request_kind="complete_agentic_test",
                    recommendation_kind=RecommendationKind.REQUIRED,
                    required_inputs=(),
                    evaluate_rule=rule or _available_rule(node_id),
                    agentic_execution=AgenticExecutionSpec(
                        active_reason="AGENTIC_TEST_ACTIVE",
                        failure_reason="AGENTIC_TEST_FAILED",
                        recovery_reason="AGENTIC_TEST_RECOVERY_REQUIRED",
                    ),
                ),
            ),
        ),
    )


def _decision(
    domain: WorkflowDomain,
    project_id: int,
    node_id: str,
    *,
    instance_key: str | None = None,
) -> NodeDecision:
    return next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == node_id
        and (instance_key is None or item.instance_key == instance_key)
    )


def _start_request(
    domain: WorkflowDomain,
    project_id: int,
    node_id: str,
    *,
    idempotency_key: str,
    instance_key: str | None = None,
) -> StartNodeAttempt:
    position = domain.position(project_id)
    decision = next(
        item
        for item in position.decisions
        if item.node_id == node_id
        and (instance_key is None or item.instance_key == instance_key)
    )
    return StartNodeAttempt(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=decision.decision_fingerprint,
        idempotency_key=idempotency_key,
        actor="operator@example.com",
        correlation_id="task-15-second-review",
        target_node_id=node_id,
        target_instance_key=decision.instance_key,
        normalized_input={"node_id": node_id},
        model_id="fake/model",
        execution_settings=EXECUTION_SETTINGS,
        lease_seconds=LEASE_SECONDS,
    )


def test_root_graph_owns_complete_agentic_execution_catalog() -> None:
    """Derive the authoritative inventory from domain node classification."""
    marked = tuple(
        node.node_id
        for node in ROOT_GRAPH.root.iter_nodes()
        if node.agentic_execution is not None
    )

    assert ROOT_GRAPH.agentic_node_ids == marked
    assert len(marked) == EXPECTED_AGENTIC_NODE_COUNT
    assert "onboarding.brownfield.curation" in marked


@pytest.mark.parametrize("node_id", ROOT_GRAPH.agentic_node_ids)
def test_every_agentic_node_uses_uniform_expiry_replacement_lifecycle(
    engine: Engine,
    node_id: str,
) -> None:
    """Block concurrent starts, then replace the exact expired attempt."""
    project_id = _seed_project(engine)
    clock = MutableClock(EVALUATED_AT)
    graph = _agentic_graph(node_id)
    domain = WorkflowDomain(
        engine=engine,
        graph=graph,
        clock=clock,
        adk_recipe_registry=CatalogRecipeRegistry(graph.agentic_node_ids),
    )
    first = domain.transition(
        _start_request(
            domain,
            project_id,
            node_id,
            idempotency_key=f"start:{node_id}",
        )
    )
    first_id = first.output.get("attempt_id")
    assert first.ok is True
    assert isinstance(first_id, int)

    waiting = _decision(domain, project_id, node_id)
    concurrent = domain.transition(
        _start_request(
            domain,
            project_id,
            node_id,
            idempotency_key=f"concurrent:{node_id}",
        )
    )
    assert waiting.category is NodeCategory.WAITING
    assert waiting.valid_until == EVALUATED_AT + timedelta(seconds=LEASE_SECONDS)
    assert concurrent.ok is False
    assert concurrent.error is not None
    assert concurrent.error.code is WorkflowErrorCode.TRANSITION_NOT_AVAILABLE

    clock.now_value += timedelta(seconds=LEASE_SECONDS)
    recovery = _decision(domain, project_id, node_id)
    old_reference = next(
        item for item in recovery.fact_references if item.fact_type == "node_attempt"
    )
    replacement = domain.transition(
        _start_request(
            domain,
            project_id,
            node_id,
            idempotency_key=f"recovery:{node_id}",
        )
    )

    assert recovery.category is NodeCategory.AVAILABLE
    assert recovery.recommendation_kind is RecommendationKind.RECOVERY
    assert old_reference.fact_id == str(first_id)
    assert replacement.ok is True
    with Session(engine) as session:
        attempts = session.exec(select(WorkflowNodeAttempt)).all()
        outcomes = session.exec(select(WorkflowNodeAttemptOutcome)).all()
        assert len(attempts) == EXPECTED_REPLACEMENT_ATTEMPT_COUNT
        assert len(outcomes) == 1
        assert outcomes[0].workflow_node_attempt_id == first_id
        assert outcomes[0].status == "obsolete"


def test_attempt_overlay_matches_exact_instance_key(engine: Engine) -> None:
    """Leave sibling instances available while one exact instance is active."""
    node_id = "planning.story.generate"
    instance_a = "requirement:A"
    instance_b = "requirement:B"

    def instances(
        _snapshot: WorkflowFactSnapshot,
        _evaluated_at: datetime,
    ) -> tuple[RuleEvaluation, ...]:
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "STORY_READY",
                instance_key=instance_a,
            ),
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "STORY_READY",
                instance_key=instance_b,
            ),
        )

    project_id = _seed_project(engine)
    graph = _agentic_graph(node_id, instances)
    domain = WorkflowDomain(
        engine=engine,
        graph=graph,
        clock=MutableClock(EVALUATED_AT),
        adk_recipe_registry=CatalogRecipeRegistry(graph.agentic_node_ids),
    )

    started = domain.transition(
        _start_request(
            domain,
            project_id,
            node_id,
            instance_key=instance_a,
            idempotency_key="start:story:A",
        )
    )
    decisions = {
        item.instance_key: item for item in domain.position(project_id).decisions
    }

    assert started.ok is True
    assert decisions[instance_a].category is NodeCategory.WAITING
    assert decisions[instance_b].category is NodeCategory.AVAILABLE
