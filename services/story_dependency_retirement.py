# services/story_dependency_retirement.py
"""Receipt-bound historical edge retirement without rewriting accepted evidence."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic resolves evidence at runtime.
from typing import Literal

from pydantic import Field
from sqlmodel import Session, col, select

from models.core import Sprint, SprintStory, UserStory, UserStoryDependency
from models.enums import SprintStatus, StoryStatus, WorkflowEventType
from models.events import WorkflowEvent
from models.workflow import WorkflowTransitionReceipt
from services.sprint_plan_retirement import load_sprint_plan_retirements
from workflow.contracts import FrozenModel, TransitionResult, WorkflowErrorCode
from workflow.facts import StoryDependencyFact, StoryDependencyRetirementFact
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.requests.base import ReviewRationale  # noqa: TC001
from workflow.requests.planning import RetireHistoricalStoryDependency

_NODE: str = "planning.story_dependencies.retire_historical"


class StoryDependencyRetirementError(ValueError):
    """A typed refusal before historical edge mutation."""

    def __init__(self, code: WorkflowErrorCode, message: str) -> None:
        """Retain the stable public error code."""
        self.code = code
        super().__init__(message)


class _RetirementEvidence(FrozenModel):
    """Canonical immutable audit payload bound to the original edge."""

    schema_version: Literal[1] = 1
    project_id: int = Field(strict=True, gt=0)
    edge: StoryDependencyFact
    dependency_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    dependent_identity_fingerprint: str
    prerequisite_identity_fingerprint: str
    receipt_id: int = Field(strict=True, gt=0)
    actor: ReviewRationale
    rationale: ReviewRationale
    retired_at: datetime


def dependency_fact(row: UserStoryDependency) -> StoryDependencyFact:
    """Project only the original durable semantic edge fields."""
    return StoryDependencyFact.model_validate(
        row.model_dump(
            include={
                "dependency_id",
                "dependent_story_id",
                "prerequisite_story_id",
                "status",
                "source",
                "confidence",
                "reason",
            }
        )
    )


def _story_identity(row: UserStory) -> str:
    return canonical_hash(
        row.model_dump(
            include={
                "project_id",
                "story_id",
                "source_story_artifact_id",
                "source_story_artifact_fingerprint",
                "source_story_item_id",
                "source_story_item_fingerprint",
                "accepted_spec_version_id",
                "accepted_spec_hash",
            }
        )
    )


def _historical_endpoint(
    session: Session, row: UserStory, *, retired_sprint_ids: frozenset[int]
) -> bool:
    statuses = session.exec(
        select(Sprint.status)
        .join(SprintStory, col(SprintStory.sprint_id) == col(Sprint.sprint_id))
        .where(
            SprintStory.story_id == row.story_id,
            ~col(Sprint.sprint_id).in_(retired_sprint_ids),
        )
    ).all()
    return not {SprintStatus.PLANNED, SprintStatus.ACTIVE}.intersection(statuses) and (
        row.status in {StoryStatus.DONE, StoryStatus.ACCEPTED}
        or SprintStatus.COMPLETED in statuses
    )


def _receipt_matches(
    session: Session,
    event: WorkflowEvent,
    evidence: _RetirementEvidence,
    receipt: WorkflowTransitionReceipt,
) -> bool:
    if receipt.result_json is None:
        marker = session.info.get("agileforge.active_transition_receipt")
        return (
            isinstance(marker, dict) and marker.get("receipt_id") == evidence.receipt_id
        )
    result = TransitionResult.model_validate_json(receipt.result_json)
    return (
        receipt.completed_at is not None
        and result.ok
        and result.applied_node_id == _NODE
        and result.output.get("retirement_event_id") == event.event_id
        and result.output.get("dependency_id") == evidence.edge.dependency_id
        and result.output.get("retirement_fingerprint")
        == canonical_hash(evidence.model_dump())
    )


def load_story_dependency_retirements(
    session: Session, project_id: int
) -> tuple[StoryDependencyRetirementFact, ...]:
    """Validate canonical audit, original edge, endpoint lineage and writer receipt."""
    retired_sprint_ids = frozenset(
        row.sprint_id
        for row in load_sprint_plan_retirements(session, project_id).values()
    )
    identity_token = object()
    events = session.exec(
        select(WorkflowEvent)
        .where(
            WorkflowEvent.project_id == project_id,
            WorkflowEvent.event_type == WorkflowEventType.STORY_DEPENDENCY_RETIRED,
        )
        .order_by(col(WorkflowEvent.event_id)),
        execution_options={"identity_token": identity_token},
    ).all()
    facts: list[StoryDependencyRetirementFact] = []
    seen: set[int] = set()
    for event in events:
        if event.event_metadata is None:
            message = "Historical dependency retirement metadata is missing."
            raise ValueError(message)
        evidence = _RetirementEvidence.model_validate_json(event.event_metadata)
        edge = session.get(
            UserStoryDependency,
            evidence.edge.dependency_id,
            identity_token=identity_token,
        )
        dependent = session.get(
            UserStory, evidence.edge.dependent_story_id, identity_token=identity_token
        )
        prerequisite = session.get(
            UserStory,
            evidence.edge.prerequisite_story_id,
            identity_token=identity_token,
        )
        receipt = session.get(
            WorkflowTransitionReceipt,
            evidence.receipt_id,
            identity_token=identity_token,
        )
        if (
            receipt is None
            or receipt.request_kind != "retire_historical_story_dependency"
        ):
            message = (
                "Historical dependency retirement has no valid transition receipt."
            )
            raise ValueError(message)
        request = RetireHistoricalStoryDependency.model_validate_json(
            receipt.request_json
        )
        valid = (
            event.event_id is not None
            and event.sprint_id is None
            and event.duration_seconds is None
            and event.turn_count is None
            and canonical_json(evidence.model_dump()) == event.event_metadata
            and canonical_hash(event.timestamp) == canonical_hash(evidence.retired_at)
            and edge is not None
            and dependent is not None
            and prerequisite is not None
            and edge.project_id
            == dependent.project_id
            == prerequisite.project_id
            == evidence.project_id
            == project_id
            and evidence.edge.status == "active"
            and dependency_fact(edge) == evidence.edge
            and canonical_hash(evidence.edge.model_dump(mode="json"))
            == evidence.dependency_fingerprint
            and _story_identity(dependent) == evidence.dependent_identity_fingerprint
            and _story_identity(prerequisite)
            == evidence.prerequisite_identity_fingerprint
            and _historical_endpoint(
                session, dependent, retired_sprint_ids=retired_sprint_ids
            )
            and _historical_endpoint(
                session, prerequisite, retired_sprint_ids=retired_sprint_ids
            )
            and request.project_id == project_id
            and receipt.idempotency_key == request.idempotency_key
            and receipt.request_fingerprint
            == canonical_hash(request.model_dump(mode="json"))
            and request.instance_key == f"story_dependency:{edge.dependency_id}"
            and request.dependency_id == edge.dependency_id
            and request.dependency_fingerprint == evidence.dependency_fingerprint
            and request.actor == evidence.actor
            and request.rationale == evidence.rationale
            and _receipt_matches(session, event, evidence, receipt)
            and edge.dependency_id not in seen
        )
        if not valid:
            message = (
                "Historical dependency retirement evidence conflicts with its "
                "original edge or receipt."
            )
            raise ValueError(message)
        seen.add(evidence.edge.dependency_id)
        facts.append(
            StoryDependencyRetirementFact(
                retirement_event_id=event.event_id,
                dependency_id=evidence.edge.dependency_id,
                dependency_fingerprint=evidence.dependency_fingerprint,
                receipt_id=evidence.receipt_id,
                actor=evidence.actor,
                rationale=evidence.rationale,
                retired_at=evidence.retired_at,
                retirement_fingerprint=canonical_hash(evidence.model_dump()),
            )
        )
    return tuple(facts)


def retire_historical_story_dependency_in_session(
    session: Session, request: RetireHistoricalStoryDependency, retired_at: datetime
) -> StoryDependencyRetirementFact:
    """Append one exact edge retirement in the guarded caller transaction."""
    from repositories.workflow import WorkflowFactRepository  # noqa: PLC0415
    from workflow.execution_scope import retry_blocks_planning  # noqa: PLC0415
    from workflow.planning_integrity import historical_dependency_edges  # noqa: PLC0415

    snapshot = WorkflowFactRepository(session).load(request.project_id)
    edge = next(
        (
            item
            for item in snapshot.story_dependencies
            if item.dependency_id == request.dependency_id
        ),
        None,
    )
    if (
        edge is None
        or canonical_hash(edge.model_dump(mode="json"))
        != request.dependency_fingerprint
        or request.instance_key != f"story_dependency:{request.dependency_id}"
    ):
        raise StoryDependencyRetirementError(
            WorkflowErrorCode.STORY_DEPENDENCY_IDENTITY_CHANGED,
            "The exact Project dependency identity changed.",
        )
    if retry_blocks_planning(snapshot) or edge not in historical_dependency_edges(
        snapshot
    ):
        raise StoryDependencyRetirementError(
            WorkflowErrorCode.HISTORICAL_DEPENDENCY_NOT_ELIGIBLE,
            "Retirement requires historical accepted endpoints outside every "
            "planned, active or retry lifecycle.",
        )
    marker = session.info.get("agileforge.active_transition_receipt")
    if not isinstance(marker, dict) or not isinstance(marker.get("receipt_id"), int):
        message = (
            "Historical dependency retirement requires its canonical "
            "transition receipt."
        )
        raise ValueError(message)  # noqa: TRY004 - this is a domain refusal.
    dependent = session.get_one(UserStory, edge.dependent_story_id)
    prerequisite = session.get_one(UserStory, edge.prerequisite_story_id)
    evidence = _RetirementEvidence(
        project_id=request.project_id,
        edge=edge,
        dependency_fingerprint=request.dependency_fingerprint,
        dependent_identity_fingerprint=_story_identity(dependent),
        prerequisite_identity_fingerprint=_story_identity(prerequisite),
        receipt_id=marker["receipt_id"],
        actor=request.actor,
        rationale=request.rationale,
        retired_at=retired_at,
    )
    event = WorkflowEvent(
        project_id=request.project_id,
        event_type=WorkflowEventType.STORY_DEPENDENCY_RETIRED,
        timestamp=retired_at,
        event_metadata=canonical_json(evidence.model_dump()),
    )
    session.add(event)
    session.flush()
    return next(
        item
        for item in load_story_dependency_retirements(session, request.project_id)
        if item.retirement_event_id == event.event_id
    )
