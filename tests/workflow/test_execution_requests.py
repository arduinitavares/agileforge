"""Execution request bindings preserve legacy bytes and isolate retry subjects."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from workflow.fingerprints import canonical_hash
from workflow.requests.execution import CompleteTask, RecordPostSprintTriage


def _guards() -> dict[str, object]:
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
