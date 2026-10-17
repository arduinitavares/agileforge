"""Execution request bindings preserve legacy bytes and isolate retry subjects."""

from __future__ import annotations

import json
from typing import TypedDict

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from workflow.fingerprints import canonical_hash
from workflow.requests.execution import CompleteTask, RecordPostSprintTriage


class _RequestGuards(TypedDict):
    """Precisely type the request fields expanded into both request models."""

    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    idempotency_key: str
    actor: str


def _guards() -> _RequestGuards:
    return {
        "project_id": 31,
        "graph_version": "agileforge.workflow.v2",
        "fact_fingerprint": "sha256:fact",
        "decision_fingerprint": "sha256:decision",
        "idempotency_key": "request-binding",
        "actor": "owner@example.com",
    }


def test_original_task_request_dump_and_hash_stay_stable() -> None:
    """Adding retry bindings must not add fields to original receipt input."""
    request = CompleteTask(
        **_guards(),
        instance_key="task:7",
        task_id=7,
        outcome_summary="Implemented the exact Task.",
        artifact_refs=("tests/workflow/test_execution_requests.py",),
        acceptance_result="fully_met",
        checklist_result={"focused test": "passed"},
    )

    assert request.model_dump(mode="json") == {
        "kind": "complete_task",
        "project_id": 31,
        "graph_version": "agileforge.workflow.v2",
        "fact_fingerprint": "sha256:fact",
        "decision_fingerprint": "sha256:decision",
        "idempotency_key": "request-binding",
        "actor": "owner@example.com",
        "correlation_id": None,
        "instance_key": "task:7",
        "attempt_id": None,
        "attempt_fingerprint": None,
        "task_id": 7,
        "outcome_summary": "Implemented the exact Task.",
        "artifact_refs": ["tests/workflow/test_execution_requests.py"],
        "acceptance_result": "fully_met",
        "checklist_result": {"focused test": "passed"},
    }
    assert canonical_hash(request.model_dump(mode="json")) == (
        "sha256:30537f2d50c24d48fb9e30f49d236af360fb74d0721fd651f00624d7fec40a15"
    )


def _completion_payload() -> dict[str, object]:
    """Supply the independently specified legacy canonical request inputs."""
    return {
        **_guards(),
        "instance_key": "task:7",
        "task_id": 7,
        "outcome_summary": "Implemented the exact Task.",
        "artifact_refs": ("tests/workflow/test_execution_requests.py",),
        "acceptance_result": "fully_met",
        "checklist_result": {"focused test": "passed"},
    }


class _CompletionEnvelope(BaseModel):
    """Exercise recursive serialization without overriding the request dump."""

    request: CompleteTask


@pytest.mark.parametrize("acknowledgement", [None, False])
def test_default_completion_semantics_preserve_all_serialization_routes(
    acknowledgement: bool | None,
) -> None:
    """Serializing new default keys would change persisted legacy hashes."""
    request = CompleteTask.model_validate(
        {**_completion_payload(), "uncommitted": acknowledgement, "worktree_path": None}
    )
    assert request.uncommitted is None
    python_payload = request.model_dump()
    assert isinstance(python_payload["artifact_refs"], tuple)
    serialized = request.model_dump(mode="json")
    assert "uncommitted" not in serialized
    assert "worktree_path" not in serialized
    assert serialized["correlation_id"] is None
    assert serialized["attempt_id"] is None
    assert serialized["attempt_fingerprint"] is None
    assert canonical_hash(serialized) == (
        "sha256:30537f2d50c24d48fb9e30f49d236af360fb74d0721fd651f00624d7fec40a15"
    )
    assert json.loads(request.model_dump_json()) == serialized
    assert _CompletionEnvelope(request=request).model_dump(mode="json") == {
        "request": serialized
    }
    assert TypeAdapter(CompleteTask).dump_python(request, mode="json") == serialized
    assert CompleteTask.model_validate_json(request.model_dump_json()) == request
    copied = request.model_copy(update={"uncommitted": False})
    assert copied.model_dump(mode="json") == serialized


@pytest.mark.parametrize(
    "semantic", [{"uncommitted": True}, {"worktree_path": "/synthetic/linked"}]
)
def test_explicit_completion_semantics_participate_in_request_hash(
    semantic: dict[str, object],
) -> None:
    """Dropping opt-in semantics would make changed requests replay as identical."""
    request = CompleteTask.model_validate({**_completion_payload(), **semantic})
    serialized = request.model_dump(mode="json")
    for key, value in semantic.items():
        assert serialized[key] == value
    assert canonical_hash(serialized) != (
        "sha256:30537f2d50c24d48fb9e30f49d236af360fb74d0721fd651f00624d7fec40a15"
    )


@pytest.mark.parametrize("acknowledgement", ["true", 1, 0])
def test_guarded_completion_rejects_nonboolean_acknowledgement(
    acknowledgement: object,
) -> None:
    """Coercing strings or integers would grant an unintended acknowledgement."""
    with pytest.raises(ValidationError):
        CompleteTask.model_validate(
            {**_completion_payload(), "uncommitted": acknowledgement}
        )


def test_retry_request_accepts_only_its_exact_task_binding() -> None:
    """A retry Task binding cannot name another Task or another action type."""
    request = CompleteTask(
        **_guards(),
        instance_key="retry:2:task:7",
        task_id=7,
        outcome_summary="Implemented the exact Task again.",
        artifact_refs=("tests/workflow/test_execution_requests.py",),
        acceptance_result="fully_met",
        checklist_result={"focused test": "passed"},
    )
    assert request.instance_key == "retry:2:task:7"

    with pytest.raises(ValidationError, match="Task binding"):
        CompleteTask(
            **_guards(),
            instance_key="retry:2:story:7",
            task_id=7,
            outcome_summary="Implemented the exact Task again.",
            artifact_refs=("tests/workflow/test_execution_requests.py",),
            acceptance_result="fully_met",
            checklist_result={"focused test": "passed"},
        )


def test_retry_triage_accepts_only_its_exact_sprint_binding() -> None:
    """A retry triage request cannot be rebound to another Sprint."""
    request = RecordPostSprintTriage(
        **_guards(),
        instance_key="retry:2:sprint:11",
        sprint_id=11,
        impact="none",
        canonical_payload={"summary": "No downstream change."},
    )
    assert request.instance_key == "retry:2:sprint:11"

    with pytest.raises(ValidationError, match="Sprint binding"):
        RecordPostSprintTriage(
            **_guards(),
            instance_key="retry:2:sprint:12",
            sprint_id=11,
            impact="none",
            canonical_payload={"summary": "No downstream change."},
        )
