"""Current, display-safe Vision attempt failures in the durable status read."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from sqlmodel import create_engine

from services.read_projections import DurableReadProjectionService
from workflow.contracts import GRAPH_VERSION, JsonObject
from workflow.facts import (
    NodeAttemptFact,
    ProjectFact,
    VisionArtifactDecisionFact,
    VisionArtifactFact,
    VisionEvidenceSnapshotFact,
    VisionInterviewTurnFact,
    WorkflowFactSnapshot,
)
from workflow.fingerprints import business_fact_fingerprint

NOW = datetime(2026, 9, 26, tzinfo=UTC)


def _snapshot(*, interview: bool = False) -> WorkflowFactSnapshot:
    snapshot = WorkflowFactSnapshot(
        project=ProjectFact(project_id=1, name="Vision status", created_at=NOW)
    )
    if not interview:
        return snapshot
    evidence = VisionEvidenceSnapshotFact(
        vision_evidence_snapshot_id=10,
        repository_binding_id=None,
        supersedes_vision_evidence_snapshot_id=None,
        workflow_node_attempt_id=11,
        evidence={},
        evidence_fingerprint="sha256:evidence",
        warnings=(),
        created_at=NOW,
    )
    turn = VisionInterviewTurnFact(
        vision_interview_turn_id=12,
        operation="bootstrap",
        turn_number=1,
        revision_intent_id=None,
        vision_evidence_snapshot_id=10,
        prior_turn_id=None,
        user_text=None,
        components={},
        vision_statement="Initial draft.",
        is_complete=False,
        clarifying_questions=(),
        output_fingerprint="sha256:output",
        workflow_node_attempt_id=11,
        attempt_fingerprint="sha256:initial-attempt",
        recorded_at=NOW,
    )
    return snapshot.model_copy(
        update={
            "vision_evidence_snapshots": (evidence,),
            "vision_interview_turns": (turn,),
        }
    )


def _candidate_snapshot(*, accepted: bool) -> WorkflowFactSnapshot:
    snapshot = _snapshot(interview=True)
    turn = snapshot.vision_interview_turns[0].model_copy(update={"is_complete": True})
    artifact = VisionArtifactFact(
        vision_artifact_id=30,
        version_number=1,
        components={},
        statement=turn.vision_statement,
        content_fingerprint="sha256:candidate",
        vision_evidence_snapshot_id=10,
        supersedes_vision_artifact_id=None,
        source_interview_turn_id=turn.vision_interview_turn_id,
        created_by="test",
        created_at=NOW,
    )
    decision = VisionArtifactDecisionFact(
        vision_artifact_decision_id=31,
        vision_artifact_id=artifact.vision_artifact_id,
        artifact_fingerprint=artifact.content_fingerprint,
        decision="accepted",
        rationale="Reviewed.",
        reviewer="test",
        idempotency_key="test-accepted",
        decided_at=NOW,
    )
    return snapshot.model_copy(
        update={
            "vision_interview_turns": (turn,),
            "vision_artifacts": (artifact,),
            "vision_artifact_decisions": (decision,) if accepted else (),
        }
    )


def _attempt(
    snapshot: WorkflowFactSnapshot,
    *,
    attempt_id: int,
    code: str | None,
    outcome: Literal["success", "failure", "obsolete"] | None = "failure",
) -> NodeAttemptFact:
    return NodeAttemptFact(
        attempt_id=attempt_id,
        node_id="vision.bootstrap",
        instance_key=None,
        graph_version=GRAPH_VERSION,
        input_fingerprint="sha256:input",
        fact_fingerprint="sha256:facts",
        business_fact_fingerprint=business_fact_fingerprint(snapshot),
        decision_fingerprint="sha256:decision",
        attempt_fingerprint=f"sha256:attempt-{attempt_id}",
        model_id="fake/model",
        lease_expires_at=NOW + timedelta(minutes=11),
        outcome=outcome,
        failure_code=code,
    )


def _status(
    snapshot: WorkflowFactSnapshot, monkeypatch: pytest.MonkeyPatch
) -> JsonObject:
    reads = DurableReadProjectionService(engine=create_engine("sqlite://"))
    monkeypatch.setattr(reads, "_snapshot", lambda _project_id: snapshot)
    result = reads.vision_status(project_id=1)
    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, dict)
    return data


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (
            "VISION_OUTPUT_INCOMPLETE",
            "Vision returned an incomplete response. No new draft was saved.",
        ),
        (
            "INVALID_VISION_PAYLOAD",
            "Vision returned an invalid response. No new draft was saved.",
        ),
        ("ADK_EXECUTION_FAILED", "Vision generation failed. No new draft was saved."),
    ],
)
def test_current_bootstrap_failure_has_bounded_distinct_message(
    monkeypatch: pytest.MonkeyPatch, code: str, expected: str
) -> None:
    """Keep supported error codes distinct without returning provider prose."""
    base = _snapshot()
    attempted = base.model_copy(
        update={"node_attempts": (_attempt(base, attempt_id=21, code=code),)}
    )

    assert _status(attempted, monkeypatch)["last_failure"] == {
        "code": code,
        "message": expected,
        "attempt_id": 21,
    }


def test_current_interview_failure_is_visible_without_original_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Show the failure of the current clarification turn."""
    base = _snapshot(interview=True)
    attempt = _attempt(base, attempt_id=22, code="INVALID_VISION_PAYLOAD")
    interview_attempt = attempt.model_copy(
        update={"node_id": "vision.interview", "instance_key": "after-turn:12"}
    )
    attempted = base.model_copy(update={"node_attempts": (interview_attempt,)})

    failure = _status(attempted, monkeypatch)["last_failure"]
    assert isinstance(failure, dict)
    assert failure["code"] == "INVALID_VISION_PAYLOAD"


@pytest.mark.parametrize("newer_outcome", [None, "success", "obsolete"])
def test_newer_attempt_clears_older_failure(
    monkeypatch: pytest.MonkeyPatch,
    newer_outcome: Literal["success", "obsolete"] | None,
) -> None:
    """Only the latest current attempt may contribute an alert."""
    base = _snapshot()
    attempted = base.model_copy(
        update={
            "node_attempts": (
                _attempt(base, attempt_id=21, code="VISION_OUTPUT_INCOMPLETE"),
                _attempt(base, attempt_id=22, code=None, outcome=newer_outcome),
            )
        }
    )

    assert "last_failure" not in _status(attempted, monkeypatch)


def test_stale_business_facts_unrelated_nodes_and_unknown_codes_do_not_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hide stale facts, other nodes, and unrecognized raw error codes."""
    base = _snapshot()
    stale = _snapshot(interview=True)
    stale_attempt = _attempt(
        base, attempt_id=20, code="VISION_OUTPUT_INCOMPLETE"
    ).model_copy(update={"business_fact_fingerprint": business_fact_fingerprint(stale)})
    unrelated_attempt = _attempt(
        base, attempt_id=21, code="VISION_OUTPUT_INCOMPLETE"
    ).model_copy(update={"node_id": "goal.bootstrap"})
    attempted = base.model_copy(
        update={
            "node_attempts": (
                stale_attempt,
                unrelated_attempt,
                _attempt(base, attempt_id=22, code="<script>unsafe()</script>"),
            )
        }
    )

    assert "last_failure" not in _status(attempted, monkeypatch)


def test_interview_history_does_not_appear_in_bootstrap_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A previous interview node must not decorate a new bootstrap state."""
    base = _snapshot()
    interview_attempt = _attempt(
        base, attempt_id=21, code="INVALID_VISION_PAYLOAD"
    ).model_copy(update={"node_id": "vision.interview"})
    attempted = base.model_copy(update={"node_attempts": (interview_attempt,)})

    assert "last_failure" not in _status(attempted, monkeypatch)


@pytest.mark.parametrize("accepted", [False, True])
def test_pending_or_accepted_candidate_hides_generation_failure(
    monkeypatch: pytest.MonkeyPatch, accepted: bool
) -> None:
    """A reviewable or accepted draft does not carry an old generation alert."""
    base = _candidate_snapshot(accepted=accepted)
    attempted = base.model_copy(
        update={
            "node_attempts": (
                _attempt(base, attempt_id=40, code="VISION_OUTPUT_INCOMPLETE"),
            )
        }
    )

    data = _status(attempted, monkeypatch)
    assert data["candidate"] is not None or data["current"] is not None
    assert "last_failure" not in data
