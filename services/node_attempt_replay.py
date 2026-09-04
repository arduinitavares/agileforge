"""Read-only replay of durable node-attempt command receipts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import TypeAdapter
from sqlmodel import Session, col, select

from models.workflow import (
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
    WorkflowTransitionReceipt,
)
from workflow.contracts import (
    FrozenModel,
    JsonObject,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
)
from workflow.fingerprints import canonical_hash
from workflow.requests import StartNodeAttempt, TransitionRequest

_TRANSITION_REQUEST = TypeAdapter(TransitionRequest)
_CALLER_OWNED_ATTEMPT_SELECTOR_NODES = frozenset({"planning.story.generate"})
_CALLER_OWNED_TRANSITION_SELECTOR_KINDS = frozenset({"decide_story"})
_SPRINT_OWNER_KINDS = frozenset({"solo_project", "named_team"})

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


class NodeAttemptReplayQuery(FrozenModel):
    """Attempt identity; omitted host guards select semantic adapter replay."""

    project_id: int
    graph_version: str | None
    fact_fingerprint: str | None
    decision_fingerprint: str | None
    node_id: str
    instance_key: str | None = None
    idempotency_key: str
    actor: str
    correlation_id: str | None = None
    user_text: str | None = None
    semantic_input: JsonObject | None = None
    reuse_stored_instance_key: bool = False


class TransitionReplayQuery(FrozenModel):
    """Identity and operator choice for one replayed host transition."""

    request_kind: str
    project_id: int
    idempotency_key: str
    actor: str
    correlation_id: str | None = None
    operator_input: JsonObject


@dataclass(frozen=True)
class DurableNodeAttemptReplayService:
    """Recover persisted attempt results without rebuilding prepared input."""

    engine: Engine

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None:
        """Return one matching in-flight or terminal receipt when it exists."""
        with Session(self.engine) as session:
            receipt = session.exec(
                select(WorkflowTransitionReceipt).where(
                    col(WorkflowTransitionReceipt.request_kind) == "start_node_attempt",
                    col(WorkflowTransitionReceipt.idempotency_key)
                    == query.idempotency_key,
                )
            ).one_or_none()
            if receipt is None:
                return None
            stored = StartNodeAttempt.model_validate_json(receipt.request_json)
            if _sprint_replay_conflicts(
                stored, query
            ) or _backlog_correction_replay_conflicts(stored, query):
                return _fact_conflict(
                    "The idempotency key was already used for different input."
                )
            expected = StartNodeAttempt(
                project_id=query.project_id,
                graph_version=(
                    stored.graph_version
                    if query.graph_version is None
                    else query.graph_version
                ),
                fact_fingerprint=(
                    stored.fact_fingerprint
                    if query.fact_fingerprint is None
                    else query.fact_fingerprint
                ),
                decision_fingerprint=(
                    stored.decision_fingerprint
                    if query.decision_fingerprint is None
                    else query.decision_fingerprint
                ),
                idempotency_key=query.idempotency_key,
                actor=query.actor,
                correlation_id=query.correlation_id,
                target_node_id=query.node_id,
                target_instance_key=(
                    stored.target_instance_key
                    if _reuses_stored_story_correction_selector(stored, query)
                    else query.instance_key
                    if query.node_id in _CALLER_OWNED_ATTEMPT_SELECTOR_NODES
                    else (
                        stored.target_instance_key
                        if query.instance_key is None
                        else query.instance_key
                    )
                ),
                normalized_input=_replay_normalized_input(stored, query),
                model_id=stored.model_id,
                execution_settings=stored.execution_settings,
                lease_seconds=stored.lease_seconds,
            )
            if canonical_hash(expected.model_dump(mode="json")) != (
                receipt.request_fingerprint
            ):
                return _fact_conflict(
                    "The idempotency key was already used for different input."
                )
            if receipt.result_json is None or receipt.completed_at is None:
                return _fact_conflict("The idempotency receipt is incomplete.")
            return _replay_attempt_result(session, receipt, stored)


@dataclass(frozen=True)
class DurableTransitionReplayService:
    """Recover one completed host request before any current-state read."""

    engine: Engine

    def replay(self, query: TransitionReplayQuery) -> TransitionResult | None:
        """Return a compatible terminal receipt or no result for a new request."""
        with Session(self.engine) as session:
            receipt = session.exec(
                select(WorkflowTransitionReceipt).where(
                    col(WorkflowTransitionReceipt.request_kind) == query.request_kind,
                    col(WorkflowTransitionReceipt.idempotency_key)
                    == query.idempotency_key,
                )
            ).one_or_none()
            if receipt is None:
                return None
            stored = _TRANSITION_REQUEST.validate_json(receipt.request_json)
            stored_payload = stored.model_dump(mode="json")
            query_payload = query.model_dump(mode="json")
            operator_input = query_payload["operator_input"]
            if not isinstance(operator_input, dict):
                message = "Semantic replay input must serialize as an object."
                raise TypeError(message)
            if (
                stored.project_id != query.project_id
                or stored.actor != query.actor
                or stored.correlation_id != query.correlation_id
                or (
                    query.request_kind in _CALLER_OWNED_TRANSITION_SELECTOR_KINDS
                    and "instance_key" not in operator_input
                )
                or any(
                    stored_payload.get(key) != value
                    for key, value in operator_input.items()
                )
            ):
                return _fact_conflict(
                    "The idempotency key was already used for different input."
                )
            if receipt.result_json is None or receipt.completed_at is None:
                return _fact_conflict("The idempotency receipt is incomplete.")
            return _replayed_result(receipt)


def _replay_normalized_input(
    stored: StartNodeAttempt,
    query: NodeAttemptReplayQuery,
) -> JsonObject:
    """Replace only current human input before checking stored attempt identity."""
    normalized_input = dict(stored.normalized_input)
    if query.user_text is not None:
        if stored.target_node_id == "vision.interview":
            request = normalized_input.get("request")
            if isinstance(request, dict):
                nested_request = dict(request)
                nested_request["human_response"] = query.user_text
                normalized_input["request"] = nested_request
            else:
                normalized_input["user_response"] = query.user_text
        else:
            normalized_input["user_response"] = query.user_text
    if query.semantic_input is not None:
        correction = query.semantic_input.get("correction")
        stored_correction = normalized_input.get("correction")
        if (
            query.reuse_stored_instance_key
            and isinstance(correction, dict)
            and isinstance(stored_correction, dict)
        ):
            normalized_input["correction"] = {**stored_correction, **correction}
            normalized_input.update(
                {
                    key: value
                    for key, value in query.semantic_input.items()
                    if key != "correction"
                }
            )
        else:
            semantic_input = query.semantic_input
            if (
                stored.target_node_id == "planning.sprint.plan"
                and "owner_kind" not in stored.normalized_input
            ):
                semantic_input = {
                    key: value
                    for key, value in semantic_input.items()
                    if key != "owner_kind"
                }
            normalized_input.update(semantic_input)
    return normalized_input


def _sprint_replay_conflicts(
    stored: StartNodeAttempt,
    query: NodeAttemptReplayQuery,
) -> bool:
    """Apply the closed owner-kind replay rules without rewriting legacy input."""
    if stored.target_node_id != "planning.sprint.plan" or query.semantic_input is None:
        return False
    requested_kind = query.semantic_input.get("owner_kind")
    stored_kind = stored.normalized_input.get("owner_kind")
    if requested_kind not in _SPRINT_OWNER_KINDS:
        return True
    if stored_kind is None:
        return (
            requested_kind != "named_team"
            or query.semantic_input.get("team_name")
            != stored.normalized_input.get("team_name")
        )
    return stored_kind not in _SPRINT_OWNER_KINDS or stored_kind != requested_kind


_BACKLOG_CORRECTION_FIELDS = frozenset(
    {
        "accepted_backlog_artifact_id",
        "accepted_backlog_artifact_fingerprint",
        "guidance",
    }
)


def _backlog_correction_replay_conflicts(
    stored: StartNodeAttempt,
    query: NodeAttemptReplayQuery,
) -> bool:
    requested_semantics = query.semantic_input
    requested = (
        None
        if requested_semantics is None
        else requested_semantics.get("backlog_correction")
    )
    persisted = stored.normalized_input.get("backlog_correction")
    if requested is None and persisted is None:
        return False
    return (
        query.node_id != "backlog.generate"
        or stored.target_node_id != "backlog.generate"
        or requested_semantics is None
        or set(requested_semantics) != {"backlog_correction"}
        or not isinstance(requested, dict)
        or set(requested) != _BACKLOG_CORRECTION_FIELDS
        or not isinstance(persisted, dict)
        or set(persisted) != _BACKLOG_CORRECTION_FIELDS
        or persisted != requested
    )


def _reuses_stored_story_correction_selector(
    stored: StartNodeAttempt,
    query: NodeAttemptReplayQuery,
) -> bool:
    """Allow only an explicit exact correction retry to reuse its host selector."""
    if (
        not query.reuse_stored_instance_key
        or query.node_id != "planning.story.generate"
        or stored.target_node_id != query.node_id
        or query.instance_key is not None
        or query.semantic_input is None
        or set(query.semantic_input) != {"correction"}
    ):
        return False
    semantic = query.semantic_input.get("correction")
    persisted = stored.normalized_input.get("correction")
    if (
        not isinstance(semantic, dict)
        or set(semantic) != {"story_id", "guidance"}
        or not isinstance(persisted, dict)
    ):
        return False
    return all(persisted.get(key) == value for key, value in semantic.items())


def _replay_attempt_result(
    session: Session,
    receipt: WorkflowTransitionReceipt,
    stored: StartNodeAttempt,
) -> TransitionResult:
    """Return the terminal outcome for a completed attempt or its start receipt."""
    attempt = session.exec(
        select(WorkflowNodeAttempt).where(
            col(WorkflowNodeAttempt.project_id) == stored.project_id,
            col(WorkflowNodeAttempt.idempotency_key) == stored.idempotency_key,
        )
    ).one_or_none()
    if attempt is None:
        return _replayed_result(receipt)
    outcome = session.exec(
        select(WorkflowNodeAttemptOutcome).where(
            col(WorkflowNodeAttemptOutcome.project_id) == attempt.project_id,
            col(WorkflowNodeAttemptOutcome.workflow_node_attempt_id)
            == attempt.workflow_node_attempt_id,
        )
    ).one_or_none()
    if outcome is None:
        return _replayed_result(receipt)
    start_result = _receipt_result(receipt)
    if not start_result.ok:
        return start_result.model_copy(update={"replayed": True})
    completion_receipt = _terminal_receipt(session, attempt)
    if completion_receipt is None:
        return _fact_conflict("The terminal node attempt has no completion receipt.")
    return _replayed_result(completion_receipt)


def _terminal_receipt(
    session: Session,
    attempt: WorkflowNodeAttempt,
) -> WorkflowTransitionReceipt | None:
    """Return the persisted terminal transition matching one durable attempt."""
    attempt_id = attempt.workflow_node_attempt_id
    if attempt_id is None:
        return None
    receipts = session.exec(
        select(WorkflowTransitionReceipt).where(
            col(WorkflowTransitionReceipt.completed_at).is_not(None)
        )
    ).all()
    for receipt in receipts:
        request = _TRANSITION_REQUEST.validate_json(receipt.request_json)
        if (
            request.project_id == attempt.project_id
            and getattr(request, "attempt_id", None) == attempt_id
            and getattr(request, "attempt_fingerprint", None)
            == attempt.attempt_fingerprint
        ):
            return receipt
    return None


def _replayed_result(receipt: WorkflowTransitionReceipt) -> TransitionResult:
    """Decode one completed durable receipt as an idempotent replay."""
    return _receipt_result(receipt).model_copy(update={"replayed": True})


def _receipt_result(receipt: WorkflowTransitionReceipt) -> TransitionResult:
    """Decode one completed durable transition receipt."""
    if receipt.result_json is None:
        message = "The idempotency receipt is incomplete."
        raise RuntimeError(message)
    return TransitionResult.model_validate_json(receipt.result_json)


def _fact_conflict(message: str) -> TransitionResult:
    return TransitionResult(
        ok=False,
        error=WorkflowError(
            code=WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
            message=message,
        ),
    )


__all__ = [
    "DurableNodeAttemptReplayService",
    "DurableTransitionReplayService",
    "NodeAttemptReplayQuery",
    "TransitionReplayQuery",
]
