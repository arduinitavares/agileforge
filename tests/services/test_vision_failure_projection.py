"""Current, display-safe Vision attempt failures in the durable status read."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

import pytest
from sqlmodel import Session, col, create_engine, select

from models.core import Project
from models.workflow import WorkflowNodeAttempt, WorkflowTransitionReceipt
from services.read_projections import DurableReadProjectionService
from workflow.contracts import (
    GRAPH_VERSION,
    JsonObject,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
)
from workflow.facts import (
    NodeAttemptFact,
    ProjectFact,
    VisionArtifactDecisionFact,
    VisionArtifactFact,
    VisionEvidenceSnapshotFact,
    VisionInterviewTurnFact,
    WorkflowFactSnapshot,
)
from workflow.fingerprints import (
    business_fact_fingerprint,
    canonical_hash,
    canonical_json,
)
from workflow.requests import StartNodeAttempt

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

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


def _temporary_failure_read(
    engine: Engine,
    *,
    interview: bool,
    summary: JsonObject,
    message: str,
) -> DurableReadProjectionService:
    base = _snapshot(interview=interview)
    attempt = _attempt(base, attempt_id=21, code="EXTERNAL_PROVIDER_TEMPORARY")
    if interview:
        attempt = attempt.model_copy(
            update={"node_id": "vision.interview", "instance_key": "after-turn:12"}
        )
    snapshot = base.model_copy(update={"node_attempts": (attempt,)})
    start = StartNodeAttempt(
        project_id=1,
        graph_version=GRAPH_VERSION,
        fact_fingerprint=attempt.fact_fingerprint,
        decision_fingerprint=attempt.decision_fingerprint,
        target_node_id=attempt.node_id,
        target_instance_key=attempt.instance_key,
        normalized_input={},
        model_id=attempt.model_id,
        execution_settings={},
        lease_seconds=60,
        idempotency_key="vision-temporary",
        actor="operator",
    )
    result = TransitionResult(
        ok=False,
        applied_node_id=attempt.node_id,
        output={"provider_failure": summary},
        error=WorkflowError(
            code=WorkflowErrorCode.EXTERNAL_PROVIDER_TEMPORARY, message=message
        ),
    )
    with Session(engine) as session:
        session.add(Project(project_id=1, name="Vision reload"))
        session.add(
            WorkflowNodeAttempt(
                workflow_node_attempt_id=attempt.attempt_id,
                project_id=1,
                node_id=attempt.node_id,
                instance_key=attempt.instance_key,
                graph_version=GRAPH_VERSION,
                fact_fingerprint=attempt.fact_fingerprint,
                business_fact_fingerprint=attempt.business_fact_fingerprint,
                decision_fingerprint=attempt.decision_fingerprint,
                normalized_input_json="{}",
                input_fingerprint=attempt.input_fingerprint,
                model_id=attempt.model_id,
                execution_settings_json="{}",
                idempotency_key=start.idempotency_key,
                actor="operator",
                started_at=NOW,
                lease_expires_at=attempt.lease_expires_at,
                attempt_fingerprint=attempt.attempt_fingerprint,
            )
        )
        session.add(
            WorkflowTransitionReceipt(
                request_kind="start_node_attempt",
                idempotency_key=start.idempotency_key,
                request_fingerprint=canonical_hash(start.model_dump(mode="json")),
                request_json=canonical_json(start.model_dump(mode="json")),
                result_json=canonical_json(result.model_dump(mode="json")),
                started_at=NOW,
                completed_at=NOW,
            )
        )
        session.commit()
    return DurableReadProjectionService(engine=engine, snapshot=snapshot)


def _provider_summary(*, status: int, reason: str) -> JsonObject:
    return {
        "schema_version": "agileforge.provider-failure.v1",
        "provider": "openrouter",
        "category": "external_temporary",
        "retryable": True,
        "reason": reason,
        "termination_reason": "attempts_exhausted",
        "http_status": status,
        "call_id": "vision-original-call",
        "attempts": 3,
        "max_attempts": 3,
        "retry_after_seconds": None,
        "manual_retry_requires_new_key": True,
    }


@pytest.mark.parametrize("interview", [False, True])
@pytest.mark.parametrize(
    ("status", "reason", "state"),
    [(429, "rate_limited", "rate-limited"), (503, "unavailable", "unavailable")],
)
def test_current_temporary_failure_reload_retains_original_summary_without_candidate(
    engine: Engine, interview: bool, status: int, reason: str, state: str
) -> None:
    """The existing status read retains frozen provider facts after reload."""
    summary = _provider_summary(status=status, reason=reason)
    message = (
        f"OpenRouter is temporarily {state}. Automatic retries stopped. "
        "Retry this action with a new idempotency key."
    )
    reads = _temporary_failure_read(
        engine, interview=interview, summary=summary, message=message
    )
    for _ in range(2):
        result = reads.vision_status(project_id=1)
        assert result["ok"] is True
        data = result["data"]
        assert isinstance(data, dict)
        assert data["last_failure"] == {
            "code": "EXTERNAL_PROVIDER_TEMPORARY",
            "message": message,
            "attempt_id": 21,
            "provider_failure": summary,
        }
        assert data["candidate"] is None
        assert data["current"] is None
        assert (data["draft"] is not None) is interview


@pytest.mark.parametrize("receipt_state", ["absent", "unsafe", "invalid", "wrong_code"])
def test_temporary_failure_reload_never_fabricates_or_leaks_receipt_data(
    engine: Engine, receipt_state: str
) -> None:
    """Missing or invalid terminal facts cannot create a temporary alert."""
    summary = _provider_summary(status=429, reason="rate_limited")
    message = (
        "OpenRouter is temporarily rate-limited. Automatic retries stopped. "
        "Retry this action with a new idempotency key."
    )
    if receipt_state == "invalid":
        summary["http_status"] = 401
    if receipt_state == "unsafe":
        message = "untrusted provider body"
    reads = _temporary_failure_read(
        engine, interview=False, summary=summary, message=message
    )
    with Session(engine) as session:
        receipt = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.idempotency_key) == "vision-temporary"
            )
        ).one()
        if receipt_state == "absent":
            session.delete(receipt)
        elif receipt_state == "wrong_code":
            result = TransitionResult(
                ok=False,
                output={"provider_failure": summary},
                error=WorkflowError(
                    code=WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED, message=message
                ),
            )
            receipt.result_json = canonical_json(result.model_dump(mode="json"))
            session.add(receipt)
        session.commit()
    result = reads.vision_status(project_id=1)
    data = result["data"]
    assert isinstance(data, dict)
    assert "last_failure" not in data
    assert data["candidate"] is None
