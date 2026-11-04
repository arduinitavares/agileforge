# services/sprint_plan_retirement.py
"""Immutable retirement evidence shared by planning reads and transitions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import Session, select

from models.core import Sprint
from models.enums import SprintStatus
from models.workflow import (
    SprintPlanArtifact,
    SprintPlanArtifactDecision,
    SprintPlanRetirement,
    SprintStart,
    WorkflowTransitionReceipt,
)
from workflow.contracts import TransitionResult
from workflow.fingerprints import canonical_hash
from workflow.requests.planning import RetireSprintPlan

if TYPE_CHECKING:
    from datetime import datetime


def retirement_fingerprint(row: SprintPlanRetirement) -> str:
    """Bind every retirement value except its generated identity and digest."""
    return canonical_hash(
        row.model_dump(exclude={"retirement_id", "retirement_fingerprint"})
    )


def load_sprint_plan_retirements(
    session: Session, project_id: int
) -> dict[int, SprintPlanRetirement]:
    """Fail closed when retirement identity, evidence, or lifecycle is corrupt."""
    identity_token = object()
    with session.no_autoflush:
        rows = session.exec(
            select(SprintPlanRetirement).where(
                SprintPlanRetirement.project_id == project_id
            ),
            execution_options={"identity_token": identity_token},
        ).all()
        result: dict[int, SprintPlanRetirement] = {}
        for row in rows:
            plan = session.get(
                SprintPlanArtifact,
                row.sprint_plan_artifact_id,
                identity_token=identity_token,
            )
            decision = session.get(
                SprintPlanArtifactDecision,
                row.sprint_plan_artifact_decision_id,
                identity_token=identity_token,
            )
            sprint = session.get(Sprint, row.sprint_id, identity_token=identity_token)
            receipt = session.get(
                WorkflowTransitionReceipt,
                row.receipt_id,
                identity_token=identity_token,
            )
            if receipt is None or receipt.request_kind != "retire_sprint_plan":
                message = "Sprint Plan retirement receipt is missing or invalid."
                raise ValueError(message)
            request = RetireSprintPlan.model_validate_json(receipt.request_json)
            valid = (
                plan is not None
                and decision is not None
                and sprint is not None
                and plan.project_id
                == decision.project_id
                == sprint.project_id
                == project_id
                and decision.sprint_plan_artifact_id == row.sprint_plan_artifact_id
                and decision.decision == "accepted"
                and decision.activated_sprint_id == row.sprint_id
                and plan.plan_fingerprint
                == decision.plan_fingerprint
                == row.plan_fingerprint
                and sprint.status is SprintStatus.PLANNED
                and sprint.started_at is None
                and sprint.completed_at is None
                and session.exec(
                    select(SprintStart).where(SprintStart.sprint_id == row.sprint_id),
                    execution_options={"identity_token": identity_token},
                ).first()
                is None
                and row.retirement_fingerprint == retirement_fingerprint(row)
                and receipt.request_fingerprint
                == canonical_hash(request.model_dump(mode="json"))
                and _receipt_matches_retirement(session, row, receipt)
                and request.project_id == project_id
                and request.sprint_plan_artifact_id == row.sprint_plan_artifact_id
                and request.sprint_plan_artifact_decision_id
                == row.sprint_plan_artifact_decision_id
                and request.sprint_id == row.sprint_id
                and request.plan_fingerprint == row.plan_fingerprint
                and request.actor == row.retired_by
                and request.rationale == row.rationale
                and row.sprint_plan_artifact_id not in result
            )
            if not valid:
                message = (
                    "Sprint Plan retirement evidence conflicts with its accepted "
                    "unstarted Sprint."
                )
                raise ValueError(message)
            result[row.sprint_plan_artifact_id] = row
    return result


def _receipt_matches_retirement(
    session: Session, row: SprintPlanRetirement, receipt: WorkflowTransitionReceipt
) -> bool:
    """Bind historical facts to completed outcomes or the current writer only."""
    if receipt.result_json is None:
        marker = session.info.get("agileforge.active_transition_receipt")
        return isinstance(marker, dict) and marker.get("receipt_id") == row.receipt_id
    result = TransitionResult.model_validate_json(receipt.result_json)
    return (
        receipt.completed_at is not None
        and result.ok
        and result.applied_node_id == "planning.sprint.retire"
        and result.output.get("retirement_id") == row.retirement_id
        and result.output.get("retirement_fingerprint") == row.retirement_fingerprint
        and result.output.get("sprint_id") == row.sprint_id
        and result.output.get("sprint_plan_artifact_id") == row.sprint_plan_artifact_id
    )


def retired_sprint_stream_ids(session: Session, project_id: int) -> frozenset[str]:
    """Return verified retired streams without altering their accepted history."""
    return frozenset(
        session.get_one(SprintPlanArtifact, plan_id).sprint_plan_stream_id
        for plan_id in load_sprint_plan_retirements(session, project_id)
    )


def retire_sprint_plan_in_session(
    session: Session, request: RetireSprintPlan, retired_at: datetime
) -> SprintPlanRetirement:
    """Append one retirement under the caller's guarded writer transaction."""
    from repositories.workflow import WorkflowFactRepository  # noqa: PLC0415
    from services.agent_workbench.sprint_phase import (  # noqa: PLC0415
        _assert_replaceable_sprint,
    )
    from workflow.definitions.planning import candidate_set_fingerprint  # noqa: PLC0415

    snapshot = WorkflowFactRepository(session).load(request.project_id)
    plan = next(
        (
            item
            for item in snapshot.planning_artifacts
            if item.artifact_type == "sprint_plan"
            and item.artifact_id == request.sprint_plan_artifact_id
        ),
        None,
    )
    sprint = session.get(Sprint, request.sprint_id)
    accepted_decision = session.get(
        SprintPlanArtifactDecision, request.sprint_plan_artifact_decision_id
    )
    if (
        plan is None
        or sprint is None
        or accepted_decision is None
        or accepted_decision.project_id != request.project_id
        or accepted_decision.sprint_plan_artifact_id != request.sprint_plan_artifact_id
        or accepted_decision.decision != "accepted"
        or accepted_decision.activated_sprint_id != request.sprint_id
        or accepted_decision.plan_fingerprint != request.plan_fingerprint
        or plan.status != "accepted"
        or plan.activated_sprint_id != request.sprint_id
        or plan.artifact_fingerprint != request.plan_fingerprint
        or sprint.project_id != request.project_id
        or sprint.completed_at is not None
    ):
        message = "Retirement does not target the exact accepted unstarted Sprint Plan."
        raise ValueError(message)
    _assert_replaceable_sprint(session, sprint)
    marker = session.info.get("agileforge.active_transition_receipt")
    if not isinstance(marker, dict) or not isinstance(marker.get("receipt_id"), int):
        message = "Sprint Plan retirement requires its canonical transition receipt."
        raise ValueError(message)  # noqa: TRY004 - domain rejection remains a ValueError.
    row = SprintPlanRetirement(
        project_id=request.project_id,
        sprint_plan_artifact_id=request.sprint_plan_artifact_id,
        sprint_plan_artifact_decision_id=request.sprint_plan_artifact_decision_id,
        sprint_id=request.sprint_id,
        plan_fingerprint=request.plan_fingerprint,
        candidate_set_fingerprint=candidate_set_fingerprint(
            tuple(item for item in snapshot.stories if item.sprint_candidate),
            snapshot.story_dependencies,
        ),
        rationale=request.rationale,
        retired_by=request.actor,
        retired_at=retired_at,
        receipt_id=marker["receipt_id"],
        retirement_fingerprint="",
    )
    row.retirement_fingerprint = retirement_fingerprint(row)
    session.add(row)
    session.flush()
    return row
