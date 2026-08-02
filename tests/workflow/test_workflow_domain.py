"""Public workflow-domain and guarded transition tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import inspect
from sqlmodel import Session, col, select

import workflow
import workflow.domain as workflow_domain_module
from models.core import Product
from models.workflow import (
    ChallengeArtifact,
    DiscoveryRun,
    InitialScopeRegistration,
    ProjectAbandonment,
    WorkflowTransitionReceipt,
)
from workflow import (
    AbandonProjectShell,
    OpenProjectShell,
    TransitionRequest,
    WorkflowDomain,
)
from workflow.clock import FixedClock
from workflow.contracts import (
    GRAPH_VERSION,
    NodeCategory,
    RecommendationKind,
    TransitionResult,
    WorkflowErrorCode,
    WorkflowPosition,
)
from workflow.definitions.root import ROOT_GRAPH
from workflow.graph import (
    ChildGraphSpec,
    NodeSpec,
    RuleCategory,
    RuleEvaluation,
    WorkflowGraph,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.engine import Engine

    from workflow.facts import WorkflowFactSnapshot


EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)


@dataclass
class MutableClock:
    """Clock whose value can advance between offer and transition."""

    now_value: datetime

    def now(self) -> datetime:
        """Return the current configured time."""
        return self.now_value


@pytest.fixture
def domain(engine: Engine) -> WorkflowDomain:
    """Build the domain against the Task 6 root graph."""
    return WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=EVALUATED_AT),
    )


def open_request(
    *,
    name: str = "Task 6 Project",
    idempotency_key: str = "open-task-6",
) -> OpenProjectShell:
    """Build one canonical shell-open request."""
    return OpenProjectShell(
        name=name,
        origin="greenfield",
        idempotency_key=idempotency_key,
        actor="operator@example.com",
        correlation_id="task-6",
    )


def require_project_id(result: TransitionResult) -> int:
    """Narrow the project ID returned by shell creation."""
    project_id = result.output.get("project_id")
    assert isinstance(project_id, int)
    return project_id


def open_greenfield_shell(domain: WorkflowDomain, *, name: str = "Task 6") -> int:
    """Open a shell and return its persisted project identity."""
    result = domain.transition(open_request(name=name, idempotency_key=f"open-{name}"))
    assert result.ok is True
    return require_project_id(result)


def available_abandon_decision(position: WorkflowPosition) -> tuple[str, str | None]:
    """Return the exact abandonment decision fingerprint and instance key."""
    decision = next(
        item
        for item in position.decisions
        if item.node_id == AbandonProjectShell.node_id
    )
    assert decision.category is NodeCategory.AVAILABLE
    return decision.decision_fingerprint, decision.instance_key


def abandon_request(
    position: WorkflowPosition,
    *,
    idempotency_key: str = "abandon-task-6",
    **overrides: object,
) -> AbandonProjectShell:
    """Build an abandonment request from one exact offered position."""
    decision_fingerprint_value, instance_key = available_abandon_decision(position)
    payload: dict[str, object] = {
        "project_id": position.project_id,
        "graph_version": position.graph_version,
        "fact_fingerprint": position.fact_fingerprint,
        "decision_fingerprint": decision_fingerprint_value,
        "idempotency_key": idempotency_key,
        "actor": "operator@example.com",
        "correlation_id": "task-6",
        "instance_key": instance_key,
        "reason": "No longer pursuing this project.",
    }
    payload.update(overrides)
    return AbandonProjectShell.model_validate(payload)


def graph_with_abandon_rule(
    rule: Callable[[WorkflowFactSnapshot, datetime], tuple[RuleEvaluation, ...]],
) -> WorkflowGraph:
    """Build a graph that isolates abandonment guard behavior."""

    def typed_rule(
        snapshot: WorkflowFactSnapshot,
        evaluated_at: datetime,
    ) -> tuple[RuleEvaluation, ...]:
        return rule(snapshot, evaluated_at)

    return WorkflowGraph(
        graph_version=GRAPH_VERSION,
        root=ChildGraphSpec(
            child_graph_id="project_lifecycle",
            nodes=(),
            children=(
                ChildGraphSpec(
                    child_graph_id="onboarding",
                    nodes=(
                        NodeSpec(
                            node_id=AbandonProjectShell.node_id,
                            child_graph_id="onboarding",
                            request_kind="abandon_project_shell",
                            recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
                            required_inputs=(),
                            evaluate_rule=typed_rule,
                        ),
                    ),
                ),
            ),
        ),
    )


def test_workflow_domain_has_only_two_public_operations() -> None:
    """Keep routing reads and mutations behind the approved two-method API."""
    public_callables = {
        name
        for name, value in WorkflowDomain.__dict__.items()
        if not name.startswith("_") and callable(value)
    }

    assert public_callables == {"position", "transition"}
    adapter = TypeAdapter(TransitionRequest)
    assert isinstance(adapter.validate_python(open_request()), OpenProjectShell)
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "unsupported"})
    assert workflow.WorkflowDomain is WorkflowDomain
    assert workflow.TransitionRequest is TransitionRequest


def test_open_project_shell_writes_only_project_and_initial_discovery(
    domain: WorkflowDomain,
    engine: Engine,
) -> None:
    """Create one atomic shell without sessions or downstream facts."""
    result = domain.transition(open_request())

    assert result.ok is True
    assert result.replayed is False
    assert result.applied_node_id == "onboarding.open_project_shell"
    project_id = require_project_id(result)
    assert result.position == domain.position(project_id)
    with Session(engine) as session:
        projects = session.exec(select(Product)).all()
        runs = session.exec(select(DiscoveryRun)).all()
        assert [(item.product_id, item.name, item.origin) for item in projects] == [
            (project_id, "Task 6 Project", "greenfield")
        ]
        assert [
            (item.project_id, item.purpose, item.ordinal, item.closed_at)
            for item in runs
        ] == [(project_id, "initial", 1, None)]
        assert session.exec(select(ProjectAbandonment)).all() == []
        assert session.exec(select(ChallengeArtifact)).all() == []
        assert session.exec(select(InitialScopeRegistration)).all() == []
    assert inspect(engine).has_table("sessions") is False


def test_open_project_shell_name_conflict_has_no_fact_mutation(
    domain: WorkflowDomain,
    engine: Engine,
) -> None:
    """Map a duplicate Project name to a workflow fact conflict."""
    first = domain.transition(open_request(name="Unique Name", idempotency_key="one"))
    second = domain.transition(open_request(name="Unique Name", idempotency_key="two"))

    assert first.ok is True
    assert second.ok is False
    assert second.error is not None
    assert second.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    with Session(engine) as session:
        assert len(session.exec(select(Product)).all()) == 1
        assert len(session.exec(select(DiscoveryRun)).all()) == 1


@pytest.mark.parametrize(
    ("guard_name", "guard_value"),
    [
        ("graph_version", "agileforge.workflow.stale"),
        ("decision_fingerprint", "sha256:stale-decision"),
    ],
)
def test_stale_position_guards_return_new_position_without_fact_mutation(
    domain: WorkflowDomain,
    engine: Engine,
    guard_name: str,
    guard_value: str,
) -> None:
    """Reject stale graph and exact-decision guards before dispatch."""
    project_id = open_greenfield_shell(domain, name=f"Stale {guard_name}")
    offered = domain.position(project_id)

    result = domain.transition(
        abandon_request(
            offered,
            idempotency_key=f"stale-{guard_name}",
            **{guard_name: guard_value},
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_POSITION
    assert result.position == domain.position(project_id)
    with Session(engine) as session:
        assert session.exec(select(ProjectAbandonment)).all() == []


def test_stale_fact_fingerprint_returns_new_position_without_mutation(
    domain: WorkflowDomain,
    engine: Engine,
) -> None:
    """Reload complete facts under the write lock before dispatch."""
    project_id = open_greenfield_shell(domain, name="Stale Facts")
    offered = domain.position(project_id)
    with Session(engine) as session:
        project = session.exec(
            select(Product).where(col(Product.product_id) == project_id)
        ).one()
        project.name = "Facts Changed Outside Domain"
        session.add(project)
        session.commit()

    result = domain.transition(
        abandon_request(
            offered,
            idempotency_key="stale-facts",
            fact_fingerprint="sha256:old",
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_POSITION
    assert result.position is not None
    assert result.position.fact_fingerprint != offered.fact_fingerprint
    with Session(engine) as session:
        assert session.exec(select(ProjectAbandonment)).all() == []


def test_expired_decision_is_rejected_before_dispatch(engine: Engine) -> None:
    """Reject a once-offered decision after its explicit validity window."""
    expires_at = EVALUATED_AT + timedelta(minutes=5)
    clock = MutableClock(now_value=EVALUATED_AT)
    graph = graph_with_abandon_rule(
        lambda _snapshot, _now: (
            RuleEvaluation(
                category=RuleCategory.AVAILABLE,
                reason_code="SHELL_CAN_BE_ABANDONED",
                valid_until=expires_at,
            ),
        )
    )
    domain = WorkflowDomain(engine=engine, graph=graph, clock=clock)
    project_id = open_greenfield_shell(domain, name="Expired Decision")
    offered = domain.position(project_id)
    clock.now_value = expires_at + timedelta(microseconds=1)

    result = domain.transition(abandon_request(offered, idempotency_key="expired"))

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_POSITION
    with Session(engine) as session:
        assert session.exec(select(ProjectAbandonment)).all() == []


def test_currently_unavailable_node_is_rejected(engine: Engine) -> None:
    """Require the exact node instance to remain available at dispatch time."""
    graph = graph_with_abandon_rule(
        lambda _snapshot, _now: (
            RuleEvaluation(
                category=RuleCategory.WAITING,
                reason_code="WAITING_FOR_OPERATOR",
            ),
        )
    )
    domain = WorkflowDomain(
        engine=engine,
        graph=graph,
        clock=FixedClock(now_value=EVALUATED_AT),
    )
    project_id = open_greenfield_shell(domain, name="Unavailable Node")
    position = domain.position(project_id)
    decision = position.decisions[0]
    request = AbandonProjectShell(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=decision.decision_fingerprint,
        idempotency_key="unavailable",
        actor="operator@example.com",
        reason="Cannot abandon now.",
    )

    result = domain.transition(request)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.TRANSITION_NOT_AVAILABLE
    with Session(engine) as session:
        assert session.exec(select(ProjectAbandonment)).all() == []


def test_abandon_project_shell_records_typed_fact(
    domain: WorkflowDomain,
    engine: Engine,
) -> None:
    """Persist abandonment as an attributed fact and derive the new position."""
    project_id = open_greenfield_shell(domain, name="Abandon Fact")
    offered = domain.position(project_id)

    result = domain.transition(abandon_request(offered))

    assert result.ok is True
    assert result.applied_node_id == AbandonProjectShell.node_id
    assert result.position == domain.position(project_id)
    with Session(engine) as session:
        rows = session.exec(select(ProjectAbandonment)).all()
        assert len(rows) == 1
        assert rows[0].project_id == project_id
        assert rows[0].reason == "No longer pursuing this project."
        assert rows[0].abandoned_by == "operator@example.com"


def test_handler_exception_rolls_back_facts_and_receipt(
    domain: WorkflowDomain,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep claim, fact writes, and completion in one rollback boundary."""

    def exploding_handler(
        session: Session,
        request: OpenProjectShell,
        _graph: WorkflowGraph,
        evaluated_at: datetime,
    ) -> TransitionResult:
        project = Product(
            name=request.name,
            origin=request.origin,
            created_at=evaluated_at,
            updated_at=evaluated_at,
        )
        session.add(project)
        session.flush()
        assert project.product_id is not None
        session.add(
            DiscoveryRun(
                project_id=project.product_id,
                purpose="initial",
                ordinal=1,
                created_at=evaluated_at,
            )
        )
        session.flush()
        message = "handler failed after writes"
        raise RuntimeError(message)

    monkeypatch.setattr(
        workflow_domain_module,
        "execute_open_project_shell",
        exploding_handler,
    )

    with pytest.raises(RuntimeError, match="handler failed after writes"):
        domain.transition(open_request(name="Rollback", idempotency_key="rollback"))

    with Session(engine) as session:
        assert session.exec(select(Product)).all() == []
        assert session.exec(select(DiscoveryRun)).all() == []
        assert session.exec(select(WorkflowTransitionReceipt)).all() == []
