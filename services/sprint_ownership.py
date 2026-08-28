# services/sprint_ownership.py
"""Provider-free Sprint ownership resolution and durable evidence."""

import json
from datetime import datetime
from hashlib import sha256
from typing import Literal, NoReturn, cast
from unicodedata import category

from sqlmodel import Session, col, select

from models.core import Project, ProjectTeam, Team
from models.workflow import (
    SprintPlanArtifact,
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
    WorkflowTransitionReceipt,
)
from workflow.contracts import (
    FrozenModel,
    JsonObject,
    TransitionResult,
    WorkflowErrorCode,
)
from workflow.fingerprints import (
    canonical_hash,
    canonical_json,
    workflow_node_attempt_fingerprint,
)
from workflow.requests import StartNodeAttempt

_RESERVED_OWNER_PREFIX = "[agileforge:sprint-owner:"
_SOLO_OWNER_KEY_PREFIX = "agileforge:sprint-owner:solo-project:v1:project:"
_NAMED_OWNER_KEY_PREFIX = "agileforge:sprint-owner:named-team:v1:sha256:"
_LEGACY_NAMED_OWNER_KEY_PREFIX = (
    "agileforge:sprint-owner:legacy-named-team:v1:sha256:"
)
_DURABLE_OWNER_KINDS = frozenset({"solo_project", "named_team"})
_MAX_PROJECT_NAME_LENGTH = 200


class ResolvedSprintOwner(FrozenModel):
    """Exact host-owned identity used for one Sprint planning attempt."""

    kind: Literal["solo_project", "named_team"]
    key: str
    label: str


class SprintOwnerResolutionError(ValueError):
    """Closed provider-free ownership resolution failure."""

    def __init__(self, code: WorkflowErrorCode, message: str) -> None:
        """Preserve one public ownership error code beside its message."""
        super().__init__(message)
        self.code = code


class SprintOwnerEvidenceError(ValueError):
    """Durable Sprint-owner evidence is missing, malformed, or contradictory."""


class SprintOwnerEvidence(FrozenModel):
    """Owner identity reloaded from an immutable Sprint artifact chain."""

    kind: Literal["solo_project", "named_team", "legacy_named_team"]
    key: str
    label: str


def is_reserved_sprint_owner_name(value: str) -> bool:
    """Return whether explicit text occupies AgileForge's owner namespace."""
    return value.strip().casefold().startswith(_RESERVED_OWNER_PREFIX)


def _project_name_snapshot(project: Project) -> str:
    name = project.name
    if not _is_valid_project_name_snapshot(name):
        message = "Project identity cannot resolve a durable solo Sprint owner."
        raise SprintOwnerResolutionError(
            WorkflowErrorCode.SPRINT_OWNER_UNAVAILABLE,
            message,
        )
    return name


def _assert_reserved_team_is_available(
    session: Session,
    *,
    project_id: int,
    owner_label: str,
) -> None:
    teams = session.exec(select(Team).where(Team.name == owner_label)).all()
    if len(teams) > 1:
        message = "Reserved solo Sprint owner does not resolve uniquely."
        raise SprintOwnerResolutionError(
            WorkflowErrorCode.SPRINT_OWNER_CONFLICT,
            message,
        )
    if not teams:
        return
    team_id = teams[0].team_id
    if team_id is None:
        message = "Reserved solo Sprint owner has no durable Team identity."
        raise SprintOwnerResolutionError(
            WorkflowErrorCode.SPRINT_OWNER_CONFLICT,
            message,
        )
    linked_project_ids = set(
        session.exec(
            select(ProjectTeam.project_id).where(
                col(ProjectTeam.team_id) == team_id,
            )
        ).all()
    )
    if linked_project_ids != {project_id}:
        message = (
            "A reserved solo Sprint owner must be linked exclusively to its "
            "encoded Project."
        )
        raise SprintOwnerResolutionError(
            WorkflowErrorCode.SPRINT_OWNER_CONFLICT,
            message,
        )


def resolve_sprint_owner(
    session: Session,
    *,
    project_id: int,
    team_name: str | None,
) -> ResolvedSprintOwner:
    """Resolve one caller request to a durable Project-scoped owner."""
    project = session.get(Project, project_id)
    if project is None:
        message = "Project was not found."
        raise SprintOwnerResolutionError(WorkflowErrorCode.PROJECT_NOT_FOUND, message)
    if team_name is not None:
        owner_label = team_name.strip()
        if not owner_label:
            message = "Named Sprint owner must not be blank."
            raise SprintOwnerResolutionError(
                WorkflowErrorCode.SPRINT_OWNER_CONFLICT,
                message,
            )
        if is_reserved_sprint_owner_name(owner_label):
            message = "Named Team override uses AgileForge's reserved owner namespace."
            raise SprintOwnerResolutionError(
                WorkflowErrorCode.SPRINT_OWNER_CONFLICT,
                message,
            )
        digest = sha256(owner_label.encode()).hexdigest()
        return ResolvedSprintOwner(
            kind="named_team",
            key=f"{_NAMED_OWNER_KEY_PREFIX}{digest}",
            label=owner_label,
        )
    project_name = _project_name_snapshot(project)
    owner_key = f"{_SOLO_OWNER_KEY_PREFIX}{project_id}"
    owner = ResolvedSprintOwner(
        kind="solo_project",
        key=owner_key,
        label=f"[{owner_key}] Solo operator for {project_name}",
    )
    _assert_reserved_team_is_available(
        session,
        project_id=project_id,
        owner_label=owner.label,
    )
    return owner


def ensure_solo_project_owner_team(
    session: Session,
    *,
    project_id: int,
    owner_label: str,
    now: datetime,
) -> int:
    """Create or reuse only the reserved Team row exclusive to this Project."""
    try:
        _validate_solo_evidence_label(project_id=project_id, label=owner_label)
    except SprintOwnerEvidenceError as error:
        message = "Solo Sprint owner identity conflicts with its Project."
        raise SprintOwnerResolutionError(
            WorkflowErrorCode.SPRINT_OWNER_CONFLICT,
            message,
        ) from error
    _assert_reserved_team_is_available(
        session,
        project_id=project_id,
        owner_label=owner_label,
    )
    team = session.exec(select(Team).where(Team.name == owner_label)).one_or_none()
    if team is None:
        team = Team(name=owner_label, created_at=now, updated_at=now)
        session.add(team)
        session.flush()
        team_id = team.team_id
        if team_id is None:
            message = "Reserved solo Sprint owner has no durable Team identity."
            raise SprintOwnerResolutionError(
                WorkflowErrorCode.SPRINT_OWNER_CONFLICT,
                message,
            )
        session.add(ProjectTeam(project_id=project_id, team_id=team_id))
        session.flush()
        return team_id
    team_id = team.team_id
    if team_id is None:
        message = "Reserved solo Sprint owner has no durable Team identity."
        raise SprintOwnerResolutionError(
            WorkflowErrorCode.SPRINT_OWNER_CONFLICT,
            message,
        )
    return team_id


def load_sprint_owner_evidence(  # noqa: C901, PLR0912, PLR0915
    session: Session,
    *,
    artifact: SprintPlanArtifact,
    owner_label: str,
) -> SprintOwnerEvidence:
    """Reload an owner only from an artifact's attempt, receipt, and outcome."""
    artifact_id = artifact.sprint_plan_artifact_id
    if artifact_id is None or not isinstance(owner_label, str) or not owner_label:
        _raise_owner_evidence_error("Sprint artifact owner identity is incomplete.")
    matches: list[tuple[WorkflowNodeAttempt, JsonObject]] = []
    related = False
    outcomes = session.exec(
        select(WorkflowNodeAttemptOutcome).where(
            col(WorkflowNodeAttemptOutcome.project_id) == artifact.project_id,
            col(WorkflowNodeAttemptOutcome.status) == "success",
        )
    ).all()
    for outcome in outcomes:
        attempt = session.get(WorkflowNodeAttempt, outcome.workflow_node_attempt_id)
        if (
            attempt is None
            or attempt.project_id != artifact.project_id
            or attempt.node_id != "planning.sprint.plan"
        ):
            continue
        output = _canonical_object(outcome.output_json, "Sprint attempt outcome")
        if outcome.output_fingerprint != canonical_hash(output):
            _raise_owner_evidence_error(
                "Sprint attempt outcome fingerprint is invalid."
            )
        if output.get("sprint_plan_artifact_id") != artifact_id:
            continue
        related = True
        if output.get("plan_fingerprint") != artifact.plan_fingerprint:
            _raise_owner_evidence_error("Sprint attempt outcome targets another plan.")
        matches.append((attempt, output))
    if not matches:
        if related:
            _raise_owner_evidence_error("Sprint artifact outcome is incomplete.")
        return _legacy_named_owner(owner_label)
    if len(matches) != 1:
        _raise_owner_evidence_error(
            "Sprint artifact resolves multiple attempt outcomes."
        )
    attempt, outcome_output = matches[0]
    attempt_input = _canonical_object(
        attempt.normalized_input_json,
        "Sprint attempt input",
    )
    if attempt.input_fingerprint != canonical_hash(attempt_input):
        _raise_owner_evidence_error("Sprint attempt input fingerprint is invalid.")
    execution_settings = _canonical_object(
        attempt.execution_settings_json,
        "Sprint attempt execution settings",
    )
    _validate_attempt_fingerprint(
        attempt,
        normalized_input=attempt_input,
        execution_settings=execution_settings,
    )
    receipts = session.exec(
        select(WorkflowTransitionReceipt).where(
            col(WorkflowTransitionReceipt.request_kind) == "start_node_attempt",
            col(WorkflowTransitionReceipt.idempotency_key) == attempt.idempotency_key,
        )
    ).all()
    if len(receipts) != 1:
        _raise_owner_evidence_error("Sprint attempt start receipt is not unique.")
    receipt = receipts[0]
    try:
        start = StartNodeAttempt.model_validate_json(receipt.request_json)
    except ValueError as error:
        _raise_owner_evidence_error(
            "Sprint attempt start receipt is malformed.",
            cause=error,
        )
    if (
        receipt.request_json != canonical_json(start.model_dump(mode="json"))
        or receipt.request_fingerprint != canonical_hash(start.model_dump(mode="json"))
        or start.project_id != artifact.project_id
        or start.target_node_id != "planning.sprint.plan"
        or start.idempotency_key != attempt.idempotency_key
        or start.normalized_input != attempt_input
    ):
        _raise_owner_evidence_error(
            "Sprint attempt start receipt does not match input."
        )
    if receipt.completed_at is None:
        _raise_owner_evidence_error("Sprint attempt start receipt is incomplete.")
    receipt_result = _canonical_object(
        receipt.result_json,
        "Sprint attempt start receipt result",
    )
    try:
        result = TransitionResult.model_validate(receipt_result)
    except ValueError as error:
        _raise_owner_evidence_error(
            "Sprint attempt start receipt result is malformed.",
            cause=error,
        )
    persisted_result = result.model_dump(mode="json")
    if (
        persisted_result != receipt_result
        or not result.ok
        or result.replayed
        or result.applied_node_id != "planning.sprint.plan"
        or persisted_result.get("output") != outcome_output
        or result.error is not None
    ):
        _raise_owner_evidence_error(
            "Sprint attempt start receipt result does not match outcome."
        )
    attempt_kind = attempt_input.get("owner_kind")
    receipt_kind = start.normalized_input.get("owner_kind")
    attempt_label = attempt_input.get("team_name")
    receipt_label = start.normalized_input.get("team_name")
    if attempt_label != owner_label or receipt_label != owner_label:
        _raise_owner_evidence_error("Sprint owner label does not match the plan.")
    if attempt_kind is None and receipt_kind is None:
        return _legacy_named_owner(owner_label)
    if attempt_kind != receipt_kind or attempt_kind not in _DURABLE_OWNER_KINDS:
        _raise_owner_evidence_error("Sprint owner kinds are inconsistent.")
    owner = _owner_evidence(
        kind=cast("Literal['solo_project', 'named_team']", attempt_kind),
        label=owner_label,
        project_id=artifact.project_id,
    )
    validate_sprint_owner_identity(owner, project_id=artifact.project_id)
    return owner


def validate_sprint_owner_identity(
    owner: SprintOwnerEvidence,
    *,
    project_id: int,
) -> None:
    """Validate one proven owner kind, key, and label without inferring its kind."""
    label = owner.label
    if not label:
        _raise_owner_evidence_error("Sprint owner label is empty.")
    if owner.kind == "solo_project":
        expected_key = f"{_SOLO_OWNER_KEY_PREFIX}{project_id}"
        _validate_solo_evidence_label(project_id=project_id, label=label)
    elif owner.kind == "named_team":
        if label != label.strip() or is_reserved_sprint_owner_name(label):
            _raise_owner_evidence_error("Named Sprint owner label is invalid.")
        expected_key = f"{_NAMED_OWNER_KEY_PREFIX}{sha256(label.encode()).hexdigest()}"
    else:
        expected_key = (
            f"{_LEGACY_NAMED_OWNER_KEY_PREFIX}{sha256(label.encode()).hexdigest()}"
        )
    if owner.key != expected_key:
        _raise_owner_evidence_error("Sprint owner key does not match its identity.")


def sprint_owner_projection(
    owner: ResolvedSprintOwner | SprintOwnerEvidence,
    *,
    project_id: int,
) -> JsonObject:
    """Separate durable owner identity from its validated human display label."""
    evidence = SprintOwnerEvidence(
        kind=owner.kind,
        key=owner.key,
        label=owner.label,
    )
    validate_sprint_owner_identity(evidence, project_id=project_id)
    display_label = evidence.label
    if evidence.kind == "solo_project":
        display_label = evidence.label.removeprefix(f"[{evidence.key}] ")
    return {
        "kind": evidence.kind,
        "key": evidence.key,
        "label": evidence.label,
        "display_label": display_label,
    }


def _canonical_object(raw_value: str | None, subject: str) -> JsonObject:
    if raw_value is None:
        _raise_owner_evidence_error(f"{subject} is missing.")
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError as error:
        _raise_owner_evidence_error(f"{subject} is malformed.", cause=error)
    if not isinstance(value, dict) or canonical_json(value) != raw_value:
        _raise_owner_evidence_error(f"{subject} is not canonical.")
    return value


def _validate_attempt_fingerprint(
    attempt: WorkflowNodeAttempt,
    *,
    normalized_input: JsonObject,
    execution_settings: JsonObject,
) -> None:
    attempt_id = attempt.workflow_node_attempt_id
    if attempt_id is None:
        _raise_owner_evidence_error("Sprint attempt does not have an identity.")
    expected = workflow_node_attempt_fingerprint(
        {
            "attempt_id": attempt_id,
            "project_id": attempt.project_id,
            "node_id": attempt.node_id,
            "instance_key": attempt.instance_key,
            "graph_version": attempt.graph_version,
            "fact_fingerprint": attempt.fact_fingerprint,
            "business_fact_fingerprint": attempt.business_fact_fingerprint,
            "decision_fingerprint": attempt.decision_fingerprint,
            "normalized_input": normalized_input,
            "input_fingerprint": attempt.input_fingerprint,
            "model_id": attempt.model_id,
            "execution_settings": execution_settings,
            "idempotency_key": attempt.idempotency_key,
            "actor": attempt.actor,
            "correlation_id": attempt.correlation_id,
            "started_at": attempt.started_at,
            "lease_expires_at": attempt.lease_expires_at,
        }
    )
    if attempt.attempt_fingerprint != expected:
        _raise_owner_evidence_error("Sprint attempt fingerprint is invalid.")


def _validate_solo_evidence_label(*, project_id: int, label: str) -> None:
    owner_key = f"{_SOLO_OWNER_KEY_PREFIX}{project_id}"
    prefix = f"[{owner_key}] Solo operator for "
    if not label.startswith(prefix):
        _raise_owner_evidence_error("Solo Sprint owner label is not project-scoped.")
    snapshot = label.removeprefix(prefix)
    if not _is_valid_project_name_snapshot(snapshot):
        _raise_owner_evidence_error("Solo Sprint owner label snapshot is invalid.")


def _is_valid_project_name_snapshot(name: str) -> bool:
    return (
        bool(name)
        and name == name.strip()
        and len(name) <= _MAX_PROJECT_NAME_LENGTH
        and not any(category(character) == "Cc" for character in name)
        and not any(category(character) in {"Zl", "Zp"} for character in name)
    )


def _raise_owner_evidence_error(
    message: str,
    *,
    cause: Exception | None = None,
) -> NoReturn:
    if cause is None:
        raise SprintOwnerEvidenceError(message)
    raise SprintOwnerEvidenceError(message) from cause


def _legacy_named_owner(label: str) -> SprintOwnerEvidence:
    digest = sha256(label.encode()).hexdigest()
    return SprintOwnerEvidence(
        kind="legacy_named_team",
        key=f"{_LEGACY_NAMED_OWNER_KEY_PREFIX}{digest}",
        label=label,
    )


def _owner_evidence(
    *,
    kind: Literal["solo_project", "named_team"],
    label: str,
    project_id: int,
) -> SprintOwnerEvidence:
    if kind == "solo_project":
        return SprintOwnerEvidence(
            kind=kind,
            key=f"{_SOLO_OWNER_KEY_PREFIX}{project_id}",
            label=label,
        )
    digest = sha256(label.encode()).hexdigest()
    return SprintOwnerEvidence(
        kind=kind,
        key=f"{_NAMED_OWNER_KEY_PREFIX}{digest}",
        label=label,
    )
