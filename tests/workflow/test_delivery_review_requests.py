"""Validation contracts for guarded delivery review requests."""

import pytest
from pydantic import BaseModel, ValidationError

from workflow.requests import (
    DecideBacklog,
    DecideRoadmap,
    DecideSprintPlan,
    DecideStory,
)

_REQUEST_CASES: tuple[tuple[type[BaseModel], dict[str, object]], ...] = (
    (
        DecideBacklog,
        {
            "backlog_artifact_id": 7,
            "artifact_fingerprint": "sha256:backlog-7",
        },
    ),
    (
        DecideRoadmap,
        {
            "roadmap_artifact_id": 8,
            "artifact_fingerprint": "sha256:roadmap-8",
        },
    ),
    (
        DecideStory,
        {
            "instance_key": "requirement:req-7",
            "requirement_id": "req-7",
            "story_artifact_id": 9,
            "artifact_fingerprint": "sha256:story-9",
        },
    ),
    (
        DecideSprintPlan,
        {
            "sprint_plan_artifact_id": 10,
            "plan_fingerprint": "sha256:sprint-10",
        },
    ),
)


def _request_payload(rationale: str, extra: dict[str, object]) -> dict[str, object]:
    return {
        "project_id": 41,
        "graph_version": "agileforge.workflow.v2",
        "fact_fingerprint": "sha256:facts",
        "decision_fingerprint": "sha256:decision",
        "idempotency_key": "delivery-review-41",
        "actor": "operator",
        "decision": "accepted",
        "rationale": rationale,
        **extra,
    }


@pytest.mark.parametrize(("request_type", "extra"), _REQUEST_CASES)
def test_delivery_review_request_strips_rationale(
    request_type: type[BaseModel],
    extra: dict[str, object],
) -> None:
    """Normalize internal direct requests before persistence."""
    request = request_type.model_validate(
        _request_payload("  Reviewed current artifact.  ", extra)
    )

    assert request.model_dump()["rationale"] == "Reviewed current artifact."


@pytest.mark.parametrize(("request_type", "extra"), _REQUEST_CASES)
def test_delivery_review_request_rejects_whitespace_rationale(
    request_type: type[BaseModel],
    extra: dict[str, object],
) -> None:
    """Reject normalized-empty rationale in guarded domain requests."""
    with pytest.raises(ValidationError):
        request_type.model_validate(_request_payload("  \t", extra))
