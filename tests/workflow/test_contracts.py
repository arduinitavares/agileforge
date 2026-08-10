"""Tests for immutable workflow domain contracts."""

from collections.abc import Mapping, MutableMapping, MutableSequence
from datetime import UTC, datetime
from operator import setitem
from typing import Annotated, ClassVar, Literal, cast

import pytest
from pydantic import Field, ValidationError

import workflow
import workflow.requests as workflow_requests
from workflow import (
    GRAPH_VERSION as PUBLIC_GRAPH_VERSION,
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
    TransitionResult,
    WorkflowErrorCode,
    WorkflowPosition,
)
from workflow.requests.base import GuardedRequest, PositionedRequest


class ExampleRequest(PositionedRequest):
    """Concrete request used to exercise the shared guard contract."""

    kind: Literal["test"] = "test"
    node_id: ClassVar[str] = "test.node"


class MatchingKindDefaultRequest(GuardedRequest):
    """Concrete request with a valid closed discriminator default."""

    kind: Literal["declared"] = "declared"


class MismatchedKindDefaultRequest(GuardedRequest):
    """Concrete request with a default outside its closed discriminator."""

    kind: Annotated[Literal["declared"], Field(default="other")]


def _guarded_request_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "project_id": 1,
        "graph_version": GRAPH_VERSION,
        "fact_fingerprint": "sha256:facts",
        "decision_fingerprint": "sha256:decision",
        "idempotency_key": "key-1",
        "actor": "test",
        "correlation_id": None,
    }
    payload.update(overrides)
    return payload


def _request_payload(**overrides: object) -> dict[str, object]:
    payload = _guarded_request_payload(
        kind="test",
        instance_key=None,
        attempt_id=None,
        attempt_fingerprint=None,
    )
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
        "SPRINT_CAPACITY_REQUIRED",
    }


def test_workflow_package_exposes_the_public_contract_surface() -> None:
    """Keep the framework-neutral contract available from the domain package."""
    assert PUBLIC_GRAPH_VERSION == GRAPH_VERSION
    assert PublicWorkflowFactSnapshot.__name__ == "WorkflowFactSnapshot"
    assert callable(public_fact_fingerprint)
    assert callable(public_decision_fingerprint)
    assert not hasattr(workflow, "GuardedRequest")
    assert not hasattr(workflow, "PositionedRequest")
    assert not hasattr(workflow_requests, "GuardedRequest")
    assert not hasattr(workflow_requests, "PositionedRequest")


def test_guarded_request_rejects_direct_instantiation() -> None:
    """Prevent guarded scaffolding from becoming a generic transition action."""
    with pytest.raises(ValidationError, match="request scaffolding"):
        GuardedRequest(
            kind="generic_action",
            project_id=1,
            graph_version=GRAPH_VERSION,
            fact_fingerprint="sha256:facts",
            decision_fingerprint="sha256:decision",
            idempotency_key="key-1",
            actor="test",
        )


def test_positioned_request_rejects_direct_instantiation() -> None:
    """Prevent positioned scaffolding from becoming a generic transition action."""
    with pytest.raises(ValidationError, match="request scaffolding"):
        PositionedRequest(
            kind="generic_action",
            project_id=1,
            graph_version=GRAPH_VERSION,
            fact_fingerprint="sha256:facts",
            decision_fingerprint="sha256:decision",
            idempotency_key="key-1",
            actor="test",
        )


def test_guarded_request_subclass_requires_one_literal_kind() -> None:
    """Reject concrete requests that retain an open string action kind."""
    with pytest.raises(TypeError, match="one non-empty string Literal"):

        class GenericStringRequest(GuardedRequest):
            kind: str = "generic_action"


def test_guarded_request_rejects_mismatched_kind_default() -> None:
    """Validate a concrete request's discriminator when its default is used."""
    with pytest.raises(ValidationError):
        MismatchedKindDefaultRequest.model_validate(_guarded_request_payload())


def test_guarded_request_accepts_matching_kind_default() -> None:
    """Allow a concrete request's sole literal value as its default."""
    request = MatchingKindDefaultRequest.model_validate(_guarded_request_payload())

    assert request.kind == "declared"


def test_guarded_request_rejects_caller_supplied_mismatched_kind() -> None:
    """Reject caller input outside a concrete request's closed discriminator."""
    with pytest.raises(ValidationError):
        MatchingKindDefaultRequest.model_validate(
            _guarded_request_payload(kind="other")
        )


def test_positioned_request_subclass_requires_stable_node_id() -> None:
    """Reject positioned variants without a non-empty class-level node ID."""
    with pytest.raises(TypeError, match="non-empty stable node_id"):

        class MissingNodeRequest(PositionedRequest):
            kind: Literal["missing_node"] = "missing_node"

    with pytest.raises(TypeError, match="non-empty stable node_id"):

        class EmptyNodeRequest(PositionedRequest):
            kind: Literal["empty_node"] = "empty_node"
            node_id: ClassVar[str] = ""


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


def test_transition_result_accepts_and_dumps_ordinary_json_output() -> None:
    """Keep immutable output compatible with JSON-shaped API payloads."""
    attempt_id = 7
    output = {
        "attempt_id": attempt_id,
        "metadata": {"ready": True, "labels": ["authority", None]},
    }

    result = TransitionResult(ok=True, output=output)

    assert result.output["attempt_id"] == attempt_id
    assert result.model_dump(mode="json")["output"] == output
    assert isinstance(result.model_dump(mode="json")["output"], dict)


def test_transition_result_rejects_non_json_output() -> None:
    """Keep transition output constrained to recursively valid JSON values."""
    with pytest.raises(ValidationError):
        TransitionResult(ok=True, output={"unsupported": object()})


def test_transition_result_output_rejects_top_level_mutation() -> None:
    """Keep the top-level transition output immutable."""
    result = TransitionResult(ok=True, output={"attempt_id": 7})

    with pytest.raises(TypeError):
        setitem(
            cast("MutableMapping[str, object]", result.output),
            "attempt_id",
            8,
        )


def test_transition_result_output_rejects_nested_mutation() -> None:
    """Keep nested transition dictionaries and lists immutable."""
    result = TransitionResult(
        ok=True,
        output={"metadata": {"labels": ["authority", "review"]}},
    )
    metadata_value = result.output["metadata"]
    assert isinstance(metadata_value, Mapping)
    metadata = cast("Mapping[str, object]", metadata_value)
    labels = metadata["labels"]
    assert isinstance(labels, tuple)

    with pytest.raises(TypeError):
        setitem(cast("MutableMapping[str, object]", metadata), "labels", [])
    with pytest.raises(TypeError):
        setitem(cast("MutableSequence[object]", labels), 0, "changed")


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
