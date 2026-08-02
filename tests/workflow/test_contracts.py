"""Tests for immutable workflow domain contracts."""

from datetime import UTC, datetime
from typing import ClassVar, Literal

import pytest
from pydantic import ValidationError

from workflow import (
    GRAPH_VERSION as PUBLIC_GRAPH_VERSION,
)
from workflow import (
    PositionedRequest as PublicPositionedRequest,
)
from workflow import (
    WorkflowFactSnapshot as PublicWorkflowFactSnapshot,
)
from workflow import (
    decision_fingerprint as public_decision_fingerprint,
)
from workflow import (
    fact_fingerprint as public_fact_fingerprint,
)
from workflow.clock import FixedClock
from workflow.contracts import (
    GRAPH_VERSION,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    WorkflowErrorCode,
    WorkflowPosition,
)
from workflow.requests.base import PositionedRequest


class ExampleRequest(PositionedRequest):
    """Concrete request used to exercise the shared guard contract."""

    kind: Literal["test"] = "test"
    node_id: ClassVar[str] = "test.node"


def _request_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "test",
        "project_id": 1,
        "graph_version": GRAPH_VERSION,
        "fact_fingerprint": "sha256:facts",
        "decision_fingerprint": "sha256:decision",
        "idempotency_key": "key-1",
        "actor": "test",
        "correlation_id": None,
        "instance_key": None,
        "attempt_id": None,
        "attempt_fingerprint": None,
    }
    payload.update(overrides)
    return payload


def test_positioned_request_rejects_unknown_fields() -> None:
    """Reject request fields outside the frozen public contract."""
    with pytest.raises(ValidationError):
        ExampleRequest.model_validate(
            {
                "kind": "test",
                "project_id": 1,
                "graph_version": "agileforge.workflow.v1",
                "fact_fingerprint": "sha256:facts",
                "decision_fingerprint": "sha256:decision",
                "idempotency_key": "key-1",
                "actor": "test",
                "correlation_id": None,
                "unexpected": True,
            }
        )


def test_contract_enums_are_closed() -> None:
    """Keep recommendation and workflow-error categories finite."""
    assert {item.value for item in RecommendationKind} == {
        "required",
        "optional_reentry",
        "recovery",
    }
    assert {item.value for item in WorkflowErrorCode} == {
        "STALE_POSITION",
        "TRANSITION_NOT_AVAILABLE",
        "WORKFLOW_FACT_CONFLICT",
        "ATTEMPT_OBSOLETE",
        "EXTERNAL_EXECUTION_FAILED",
    }


def test_workflow_package_exposes_the_public_contract_surface() -> None:
    """Keep the framework-neutral contract available from the domain package."""
    assert PUBLIC_GRAPH_VERSION == GRAPH_VERSION
    assert PublicPositionedRequest is PositionedRequest
    assert PublicWorkflowFactSnapshot.__name__ == "WorkflowFactSnapshot"
    assert callable(public_fact_fingerprint)
    assert callable(public_decision_fingerprint)


def test_frozen_contracts_reject_mutation() -> None:
    """Keep evaluated positions immutable after construction."""
    position = WorkflowPosition(
        project_id=1,
        graph_version=GRAPH_VERSION,
        fact_fingerprint="sha256:facts",
        evaluated_at=datetime(2026, 8, 2, tzinfo=UTC),
        available_nodes=(),
        waiting_nodes=(),
        blocked_nodes=(),
        invalid_nodes=(),
        terminal=False,
        decisions=(),
    )

    with pytest.raises(ValidationError):
        position.terminal = True


def test_positioned_request_requires_both_attempt_guards() -> None:
    """Prevent durable-attempt completion from carrying a partial guard."""
    with pytest.raises(ValidationError):
        ExampleRequest.model_validate(_request_payload(attempt_id=4))
    with pytest.raises(ValidationError):
        ExampleRequest.model_validate(
            _request_payload(attempt_fingerprint="sha256:attempt")
        )


def test_positioned_request_exposes_canonical_decision_lookup() -> None:
    """Resolve a request decision without inspecting request-specific fields."""
    request = ExampleRequest.model_validate(_request_payload(instance_key="phase-1"))

    assert request.decision_node_id() == "test.node"
    assert request.decision_instance_key() == "phase-1"


def test_fixed_clock_returns_its_configured_time() -> None:
    """Keep time deterministic for graph evaluation tests."""
    expected = datetime(2026, 8, 2, 12, tzinfo=UTC)

    assert FixedClock(now_value=expected).now() == expected


def test_node_decision_uses_closed_category_enum() -> None:
    """Reject unsupported node categories at the public boundary."""
    payload = {
        "node_id": "discovery.start",
        "child_graph_id": "discovery",
        "request_kind": "start_discovery",
        "category": NodeCategory.AVAILABLE,
        "recommendation_kind": RecommendationKind.REQUIRED,
        "reason_code": "PROJECT_CREATED",
        "decision_fingerprint": "sha256:decision",
    }
    assert NodeDecision.model_validate(payload).category is NodeCategory.AVAILABLE

    with pytest.raises(ValidationError):
        NodeDecision.model_validate({**payload, "category": "unknown"})
