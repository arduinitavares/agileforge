"""Immutable Sprint-plan drafting, atomic activation, and Sprint lifecycle writes."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from sqlmodel import Session, col, select

from models.core import ProjectTeam, Sprint, SprintStory, Task, Team
from models.enums import SprintStatus, WorkflowEventType
from models.events import TaskExecutionLog, WorkflowEvent
from models.workflow import (
    SprintClosure,
    SprintPlanArtifact,
    SprintPlanArtifactDecision,
    SprintReview,
    SprintStart,
    StoryClosure,
    StoryDependencyReview,
    TaskCompletionEvidence,
)
from repositories.workflow import WorkflowFactRepository
from services.contracts.specification_references import AcceptedSpecificationReference
from services.contracts.sprint import SprintPlannerOutput, validate_task_spec_references
from services.planning_artifact_content import (
    SprintPlanEnvelope,
    build_sprint_plan_envelope,
    load_bound_sprint_plan_envelope,
)
from services.planning_lineage import (
    ArtifactLineageNode,
    PlanningLineageError,
    SprintStreamState,
    next_artifact_version,
    select_current_accepted_artifact,
    select_physical_leaf,
    select_reusable_sprint_stream,
)
from services.planning_lineage import Decision as PlanningLineageDecision
from services.specs.accepted_specification import (
    load_current_accepted_specification,
)
from utils.task_metadata import metadata_from_structured_task, serialize_task_metadata
from workflow.definitions.planning import candidate_set_fingerprint
from workflow.execution_integrity import (
    ExecutionIntegrityError,
    SelectedStoryDependencySnapshot,
    SprintStartAudit,
    selected_story_dependency_snapshot,
    sprint_close_fingerprint,
    sprint_review_fingerprint,
    sprint_start_audit_metadata,
)
from workflow.fingerprints import canonical_json
from workflow.planning_integrity import (
    current_task_content_fingerprint,
    planned_task_content_fingerprint,
)

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.facts import StoryFact


class SprintPlanStreamCollisionError(ValueError):
    """One host-minted Sprint stream identifier already exists."""


class ActiveSprintExistsError(ValueError):
    """A different Project Sprint is already active."""


class StaleSpecificationError(ValueError):
    """New Sprint work targets a superseded Specification."""


@dataclass(frozen=True)
class RecordSprintPlanInput:
    """Caller-independent values used to record one immutable Sprint plan."""

    project_id: int
    spec_version_id: int
    spec_hash: str
    team_name: str
    planner_output: SprintPlannerOutput
    actor: str
    recorded_at: datetime


@dataclass(frozen=True)
class RecordSprintPlanDecisionInput:
    """Exact append-only review values for one immutable Sprint plan."""

    artifact: SprintPlanArtifact
    decision: str
    rationale: str
    reviewer: str
    idempotency_key: str
    decided_at: datetime


def _sprint_plan_rows(
    session: Session,
    *,
    project_id: int,
    spec_version_id: int | None = None,
    spec_hash: str | None = None,
) -> tuple[SprintPlanArtifact, ...]:
    statement = select(SprintPlanArtifact).where(
        SprintPlanArtifact.project_id == project_id
    )
    if spec_version_id is not None:
        statement = statement.where(
            SprintPlanArtifact.spec_version_id == spec_version_id,
            SprintPlanArtifact.spec_hash == spec_hash,
        )
    return tuple(
        session.exec(
            statement.order_by(col(SprintPlanArtifact.sprint_plan_artifact_id))
        ).all()
    )


def _decision_rows(
    session: Session,
    *,
    project_id: int,
) -> dict[int, SprintPlanArtifactDecision]:
    rows = session.exec(
        select(SprintPlanArtifactDecision).where(
            SprintPlanArtifactDecision.project_id == project_id
        )
    ).all()
    return {row.sprint_plan_artifact_id: row for row in rows}


def _lineage_nodes(
    artifacts: tuple[SprintPlanArtifact, ...],
    decisions: dict[int, SprintPlanArtifactDecision],
) -> tuple[ArtifactLineageNode, ...]:
    nodes: list[ArtifactLineageNode] = []
    for artifact in artifacts:
        artifact_id = artifact.sprint_plan_artifact_id
        if artifact_id is None:
            message = "Sprint plan artifact has no durable identity."
            raise ValueError(message)
        decision = decisions.get(artifact_id)
        decision_value: PlanningLineageDecision = None
        if decision is not None:
            if decision.decision not in {"accepted", "feedback", "rejected"}:
                message = "Sprint plan decision is invalid."
                raise ValueError(message)
            decision_value = cast("PlanningLineageDecision", decision.decision)
        nodes.append(
            ArtifactLineageNode(
                artifact_id=artifact_id,
                chain_key=(
                    artifact.project_id,
                    artifact.spec_version_id,
                    artifact.spec_hash,
                    artifact.sprint_plan_stream_id,
                ),
                version_number=artifact.version_number,
                supersedes_artifact_id=artifact.supersedes_sprint_plan_artifact_id,
                decision=decision_value,
            )
        )
    return tuple(nodes)


def _current_accepted_for_stream(
    artifacts: tuple[SprintPlanArtifact, ...],
    decisions: dict[int, SprintPlanArtifactDecision],
    *,
    stream_id: str,
) -> tuple[SprintPlanArtifact, SprintPlanArtifactDecision] | None:
    stream = tuple(
        artifact
        for artifact in artifacts
        if artifact.sprint_plan_stream_id == stream_id
    )
    if not stream:
        return None
    nodes = _lineage_nodes(stream, decisions)
    chain_key = nodes[0].chain_key
    try:
        selected = select_current_accepted_artifact(nodes, chain_key=chain_key)
    except PlanningLineageError as error:
        if error.code.value == "ACCEPTED_LEAF_MISSING":
            return None
        raise
    artifact = next(
        item for item in stream if item.sprint_plan_artifact_id == selected.artifact_id
    )
    decision = decisions.get(selected.artifact_id)
    if decision is None or decision.decision != "accepted":
        message = "Current accepted Sprint plan decision is missing."
        raise ValueError(message)
    return artifact, decision


def _stream_states(
    session: Session,
    artifacts: tuple[SprintPlanArtifact, ...],
    decisions: dict[int, SprintPlanArtifactDecision],
) -> tuple[SprintStreamState, ...]:
    stream_ids = tuple(dict.fromkeys(row.sprint_plan_stream_id for row in artifacts))
    states: list[SprintStreamState] = []
    for order, stream_id in enumerate(stream_ids, start=1):
        accepted = _current_accepted_for_stream(
            artifacts,
            decisions,
            stream_id=stream_id,
        )
        sprint = (
            None
            if accepted is None or accepted[1].activated_sprint_id is None
            else session.get(Sprint, accepted[1].activated_sprint_id)
        )
        started = bool(
            sprint is not None
            and (
                sprint.status is SprintStatus.ACTIVE
                or sprint.started_at is not None
                or session.exec(
                    select(SprintStart).where(SprintStart.sprint_id == sprint.sprint_id)
                ).first()
                is not None
            )
        )
        terminal = bool(
            sprint is not None
            and (
                sprint.status is SprintStatus.COMPLETED
                or session.exec(
                    select(SprintClosure).where(
                        SprintClosure.sprint_id == sprint.sprint_id
                    )
                ).first()
                is not None
            )
        )
        first = next(row for row in artifacts if row.sprint_plan_stream_id == stream_id)
        states.append(
            SprintStreamState(
                spec_identity=(first.spec_version_id, first.spec_hash),
                stream_id=stream_id,
                created_order=order,
                sprint_started=started,
                sprint_terminal=terminal,
            )
        )
    return tuple(states)


def _mint_stream_id(session: Session, *, project_id: int) -> str:
    stream_id = f"SPS-{uuid.uuid4().hex}"
    collision = session.exec(
        select(SprintPlanArtifact).where(
            SprintPlanArtifact.project_id == project_id,
            SprintPlanArtifact.sprint_plan_stream_id == stream_id,
        )
    ).first()
    if collision is not None:
        message = (
            "Generated Sprint plan stream ID already exists. Retry Sprint planning."
        )
        raise SprintPlanStreamCollisionError(message)
    return stream_id


def _validated_plan_candidates(
    session: Session,
    *,
    inputs: RecordSprintPlanInput,
) -> tuple[SprintPlannerOutput, tuple[StoryFact, ...], str]:
    specification = load_current_accepted_specification(
        session,
        project_id=inputs.project_id,
    )
    if (
        specification is None
        or specification.spec_version_id != inputs.spec_version_id
        or specification.spec_hash != inputs.spec_hash
    ):
        message = "Sprint planning requires the current accepted Specification."
        raise StaleSpecificationError(message)
    snapshot = WorkflowFactRepository(session).load(inputs.project_id)
    candidates = tuple(item for item in snapshot.stories if item.sprint_candidate)
    current_fingerprint = candidate_set_fingerprint(
        candidates,
        snapshot.story_dependencies,
    )
    candidate_by_id = {item.story_id: item for item in candidates}
    selected_ids = tuple(
        item.story_id for item in inputs.planner_output.selected_stories
    )
    if any(story_id not in candidate_by_id for story_id in selected_ids):
        message = "Sprint plan selected Story is not a current candidate."
        raise ValueError(message)
    accepted_reference = AcceptedSpecificationReference(
        spec_version_id=specification.spec_version_id,
        spec_hash=specification.spec_hash,
        canonical_specification_json=specification.canonical_specification_json,
        payload=specification.payload,
    )
    for selected in inputs.planner_output.selected_stories:
        parent = candidate_by_id[selected.story_id]
        if (
            parent.source_story_item_id != selected.story_item_id
            or parent.accepted_spec_version_id != inputs.spec_version_id
            or parent.accepted_spec_hash != inputs.spec_hash
        ):
            message = "Sprint plan selected Story identity changed."
            raise ValueError(message)
        for task in selected.tasks:
            validate_task_spec_references(
                accepted_reference,
                task,
                parent_story_spec_item_ids=parent.spec_item_ids,
            )
    return inputs.planner_output, candidates, current_fingerprint


def record_sprint_plan_in_session(
    session: Session,
    *,
    inputs: RecordSprintPlanInput,
) -> SprintPlanArtifact:
    """Persist one immutable plan artifact and no operational delivery rows."""
    plan, _candidates, candidate_fingerprint = _validated_plan_candidates(
        session,
        inputs=inputs,
    )
    artifacts = _sprint_plan_rows(
        session,
        project_id=inputs.project_id,
        spec_version_id=inputs.spec_version_id,
        spec_hash=inputs.spec_hash,
    )
    decisions = _decision_rows(session, project_id=inputs.project_id)
    states = _stream_states(session, artifacts, decisions)
    stream_id = select_reusable_sprint_stream(
        states,
        spec_identity=(inputs.spec_version_id, inputs.spec_hash),
    )
    if stream_id is None:
        stream_id = _mint_stream_id(session, project_id=inputs.project_id)
        parent_id = None
    else:
        nodes = _lineage_nodes(artifacts, decisions)
        chain_key = next(
            node.chain_key for node in nodes if node.chain_key[-1] == stream_id
        )
        parent_id = select_physical_leaf(
            nodes,
            chain_key=chain_key,
        ).artifact_id
    chain_key = (
        inputs.project_id,
        inputs.spec_version_id,
        inputs.spec_hash,
        stream_id,
    )
    version = next_artifact_version(
        _lineage_nodes(artifacts, decisions),
        chain_key=chain_key,
        supersedes_id=parent_id,
    )
    _envelope, canonical_content_json, plan_fingerprint = build_sprint_plan_envelope(
        team_name=inputs.team_name,
        spec_version_id=inputs.spec_version_id,
        spec_hash=inputs.spec_hash,
        candidate_set_fingerprint=candidate_fingerprint,
        planner_output=plan,
    )
    selected_ids = tuple(item.story_id for item in plan.selected_stories)
    row = SprintPlanArtifact(
        project_id=inputs.project_id,
        spec_version_id=inputs.spec_version_id,
        spec_hash=inputs.spec_hash,
        sprint_plan_stream_id=stream_id,
        version_number=version,
        selected_story_ids_json=canonical_json(list(selected_ids)),
        canonical_task_plan_json=canonical_content_json,
        plan_fingerprint=plan_fingerprint,
        candidate_set_fingerprint=candidate_fingerprint,
        supersedes_sprint_plan_artifact_id=parent_id,
        created_by=inputs.actor,
        created_at=inputs.recorded_at,
    )
    session.add(row)
    session.add(
        WorkflowEvent(
            event_type=WorkflowEventType.SPRINT_PLAN_SAVED,
            timestamp=inputs.recorded_at,
            project_id=inputs.project_id,
            event_metadata=canonical_json(
                {
                    "action": "sprint_plan_recorded",
                    "candidate_set_fingerprint": candidate_fingerprint,
                    "sprint_plan_stream_id": stream_id,
                    "plan_fingerprint": plan_fingerprint,
                    "selected_story_ids": list(selected_ids),
                }
            ),
            duration_seconds=0.0,
        )
    )
    session.flush()
    return row


def _assert_current_physical_leaf(
    session: Session,
    artifact: SprintPlanArtifact,
) -> SprintPlanEnvelope:
    rows = _sprint_plan_rows(
        session,
        project_id=artifact.project_id,
        spec_version_id=artifact.spec_version_id,
        spec_hash=artifact.spec_hash,
    )
    nodes = _lineage_nodes(
        rows,
        _decision_rows(session, project_id=artifact.project_id),
    )
    chain_key = (
        artifact.project_id,
        artifact.spec_version_id,
        artifact.spec_hash,
        artifact.sprint_plan_stream_id,
    )
    leaf = select_physical_leaf(nodes, chain_key=chain_key)
    if leaf.artifact_id != artifact.sprint_plan_artifact_id:
        message = "Sprint plan review does not target the physical stream leaf."
        raise ValueError(message)
    return load_bound_sprint_plan_envelope(
        artifact.canonical_task_plan_json,
        expected_fingerprint=artifact.plan_fingerprint,
        spec_version_id=artifact.spec_version_id,
        spec_hash=artifact.spec_hash,
        candidate_set_fingerprint=artifact.candidate_set_fingerprint,
        selected_story_ids_json=artifact.selected_story_ids_json,
    )


def _assert_replaceable_sprint(session: Session, sprint: Sprint) -> None:
    sprint_id = sprint.sprint_id
    if sprint_id is None:
        message = "Activated Sprint has no durable identity."
        raise ValueError(message)
    memberships = session.exec(
        select(SprintStory).where(SprintStory.sprint_id == sprint_id)
    ).all()
    story_ids = tuple(item.story_id for item in memberships)
    tasks = (
        session.exec(select(Task).where(col(Task.story_id).in_(story_ids))).all()
        if story_ids
        else []
    )
    task_ids = tuple(task.task_id for task in tasks if task.task_id is not None)
    blocked = (
        sprint.status is not SprintStatus.PLANNED
        or sprint.started_at is not None
        or session.exec(
            select(SprintStart).where(SprintStart.sprint_id == sprint_id)
        ).first()
        is not None
        or (
            bool(task_ids)
            and session.exec(
                select(TaskExecutionLog).where(
                    col(TaskExecutionLog.task_id).in_(task_ids)
                )
            ).first()
            is not None
        )
        or (
            bool(task_ids)
            and session.exec(
                select(TaskCompletionEvidence).where(
                    col(TaskCompletionEvidence.task_id).in_(task_ids)
                )
            ).first()
            is not None
        )
        or session.exec(
            select(StoryClosure).where(StoryClosure.sprint_id == sprint_id)
        ).first()
        is not None
        or session.exec(
            select(SprintClosure).where(SprintClosure.sprint_id == sprint_id)
        ).first()
        is not None
    )
    if blocked:
        message = "Accepted Sprint plan projection can no longer be replaced."
        raise ValueError(message)


def _ensure_team(
    session: Session, *, project_id: int, team_name: str, now: datetime
) -> int:
    teams = session.exec(select(Team).where(Team.name == team_name)).all()
    if len(teams) > 1:
        message = "Sprint Team name does not resolve to one durable Team."
        raise ValueError(message)
    team = teams[0] if teams else None
    if team is None:
        team = Team(name=team_name, created_at=now, updated_at=now)
        session.add(team)
        session.flush()
    team_id = team.team_id
    if team_id is None:
        message = "Sprint Team name does not resolve to one durable Team."
        raise ValueError(message)
    link = session.get(ProjectTeam, (project_id, team_id))
    if link is None:
        session.add(ProjectTeam(project_id=project_id, team_id=team_id))
        session.flush()
    return team_id


def _replace_operational_projection(  # noqa: PLR0913
    session: Session,
    *,
    artifact: SprintPlanArtifact,
    envelope: SprintPlanEnvelope,
    prior_sprint: Sprint | None,
    team_id: int,
    activated_at: datetime,
) -> Sprint:
    if prior_sprint is None:
        sprint = Sprint(
            goal=envelope.planner_output.sprint_goal,
            status=SprintStatus.PLANNED,
            project_id=artifact.project_id,
            team_id=team_id,
            created_at=activated_at,
            updated_at=activated_at,
        )
        session.add(sprint)
        session.flush()
    else:
        _assert_replaceable_sprint(session, prior_sprint)
        sprint = prior_sprint
        sprint.goal = envelope.planner_output.sprint_goal
        sprint.team_id = team_id
        sprint.updated_at = activated_at
        memberships = session.exec(
            select(SprintStory).where(SprintStory.sprint_id == sprint.sprint_id)
        ).all()
        old_story_ids = tuple(item.story_id for item in memberships)
        if old_story_ids:
            for task in session.exec(
                select(Task).where(col(Task.story_id).in_(old_story_ids))
            ).all():
                session.delete(task)
        for membership in memberships:
            session.delete(membership)
        session.add(sprint)
        session.flush()
    sprint_id = sprint.sprint_id
    artifact_id = artifact.sprint_plan_artifact_id
    if sprint_id is None or artifact_id is None:
        message = "Sprint activation identities are incomplete."
        raise ValueError(message)
    for selected in envelope.planner_output.selected_stories:
        session.add(
            SprintStory(
                sprint_id=sprint_id,
                story_id=selected.story_id,
                added_at=activated_at,
            )
        )
        for task_spec in selected.tasks:
            metadata = metadata_from_structured_task(
                task_spec,
                spec_version_id=artifact.spec_version_id,
                spec_hash=artifact.spec_hash,
                sprint_plan_stream_id=artifact.sprint_plan_stream_id,
                sprint_plan_artifact_id=artifact_id,
                sprint_plan_fingerprint=artifact.plan_fingerprint,
            )
            session.add(
                Task(
                    story_id=selected.story_id,
                    description=task_spec.description,
                    metadata_json=serialize_task_metadata(metadata),
                    created_at=activated_at,
                    updated_at=activated_at,
                )
            )
    session.flush()
    return sprint


def record_sprint_plan_decision_in_session(
    session: Session,
    *,
    inputs: RecordSprintPlanDecisionInput,
) -> SprintPlanArtifactDecision:
    """Persist feedback/rejection only, or accept and activate atomically."""
    artifact = inputs.artifact
    if inputs.decision not in {"accepted", "rejected", "feedback"}:
        message = "Sprint plan decision is invalid."
        raise ValueError(message)
    artifact_id = artifact.sprint_plan_artifact_id
    if artifact_id is None:
        message = "Sprint plan artifact has no durable identity."
        raise ValueError(message)
    if (
        session.exec(
            select(SprintPlanArtifactDecision).where(
                SprintPlanArtifactDecision.project_id == artifact.project_id,
                SprintPlanArtifactDecision.sprint_plan_artifact_id == artifact_id,
            )
        ).first()
        is not None
    ):
        message = "Sprint plan already has an authoritative decision."
        raise ValueError(message)
    envelope = _assert_current_physical_leaf(session, artifact)
    activated_sprint_id: int | None = None
    if inputs.decision == "accepted":
        _validated_plan_candidates(
            session,
            inputs=RecordSprintPlanInput(
                project_id=artifact.project_id,
                spec_version_id=artifact.spec_version_id,
                spec_hash=artifact.spec_hash,
                team_name=envelope.team_name,
                planner_output=envelope.planner_output,
                actor=inputs.reviewer,
                recorded_at=inputs.decided_at,
            ),
        )
        artifacts = _sprint_plan_rows(
            session,
            project_id=artifact.project_id,
            spec_version_id=artifact.spec_version_id,
            spec_hash=artifact.spec_hash,
        )
        decisions = _decision_rows(session, project_id=artifact.project_id)
        prior = _current_accepted_for_stream(
            artifacts,
            decisions,
            stream_id=artifact.sprint_plan_stream_id,
        )
        prior_sprint = (
            None
            if prior is None or prior[1].activated_sprint_id is None
            else session.get(Sprint, prior[1].activated_sprint_id)
        )
        team_id = _ensure_team(
            session,
            project_id=artifact.project_id,
            team_name=envelope.team_name,
            now=inputs.decided_at,
        )
        sprint = _replace_operational_projection(
            session,
            artifact=artifact,
            envelope=envelope,
            prior_sprint=prior_sprint,
            team_id=team_id,
            activated_at=inputs.decided_at,
        )
        activated_sprint_id = sprint.sprint_id
    row = SprintPlanArtifactDecision(
        project_id=artifact.project_id,
        sprint_plan_artifact_id=artifact_id,
        plan_fingerprint=artifact.plan_fingerprint,
        decision=inputs.decision,
        activated_sprint_id=activated_sprint_id,
        rationale=inputs.rationale,
        reviewer=inputs.reviewer,
        idempotency_key=inputs.idempotency_key,
        decided_at=inputs.decided_at,
    )
    session.add(row)
    session.flush()
    return row


@dataclass(frozen=True)
class SprintStartInput:
    """Graph-selected guard and caller-owned audit facts for one Sprint start."""

    project_id: int
    expected_sprint_id: int
    expected_task_content_fingerprint: str
    decision_fingerprint: str
    started_by: str
    started_at: datetime


@dataclass(frozen=True)
class _ResolvedSprintStart:
    project_id: int
    sprint_id: int
    sprint_plan_artifact_id: int
    sprint_plan_artifact_decision_id: int
    plan_fingerprint: str
    candidate_set_fingerprint: str
    selected_story_ids: tuple[int, ...]
    task_content_fingerprint: str
    dependency_snapshot: SelectedStoryDependencySnapshot
    decision_fingerprint: str
    started_by: str
    started_at: datetime


def _resolve_sprint_start(
    session: Session,
    command: SprintStartInput,
) -> _ResolvedSprintStart:
    """Resolve and revalidate accepted start facts in the write transaction."""
    specification = load_current_accepted_specification(
        session,
        project_id=command.project_id,
    )
    if specification is None:
        message = "Sprint start requires the current accepted Specification."
        raise StaleSpecificationError(message)
    snapshot = WorkflowFactRepository(session).load(command.project_id)
    plans = tuple(
        item
        for item in snapshot.planning_artifacts
        if item.artifact_type == "sprint_plan"
        and item.status == "accepted"
        and item.spec_version_id == specification.spec_version_id
        and item.spec_hash == specification.spec_hash
        and item.activated_sprint_id is not None
        and not any(
            start.sprint_plan_artifact_id == item.artifact_id
            for start in snapshot.sprint_starts
        )
    )
    if len(plans) != 1:
        message = "Sprint start cannot resolve one current accepted plan."
        raise ValueError(message)
    plan_fact = plans[0]
    plan = session.get(SprintPlanArtifact, plan_fact.artifact_id)
    decisions = _decision_rows(session, project_id=command.project_id)
    accepted = decisions.get(plan_fact.artifact_id)
    if (
        plan is None
        or accepted is None
        or accepted.decision != "accepted"
        or accepted.activated_sprint_id != command.expected_sprint_id
        or plan.plan_fingerprint != plan_fact.artifact_fingerprint
    ):
        message = "Sprint start does not match an exact accepted Sprint plan."
        raise ValueError(message)
    artifacts = _sprint_plan_rows(
        session,
        project_id=command.project_id,
        spec_version_id=plan.spec_version_id,
        spec_hash=plan.spec_hash,
    )
    current = _current_accepted_for_stream(
        artifacts,
        decisions,
        stream_id=plan.sprint_plan_stream_id,
    )
    if current is None or current[0].sprint_plan_artifact_id != plan_fact.artifact_id:
        message = "Sprint start does not target the current accepted plan."
        raise ValueError(message)
    envelope = load_bound_sprint_plan_envelope(
        plan.canonical_task_plan_json,
        expected_fingerprint=plan.plan_fingerprint,
        spec_version_id=plan.spec_version_id,
        spec_hash=plan.spec_hash,
        candidate_set_fingerprint=plan.candidate_set_fingerprint,
        selected_story_ids_json=plan.selected_story_ids_json,
    )
    candidates = tuple(item for item in snapshot.stories if item.sprint_candidate)
    if (
        candidate_set_fingerprint(candidates, snapshot.story_dependencies)
        != plan.candidate_set_fingerprint
    ):
        message = "Accepted Sprint plan candidate set changed before start."
        raise ValueError(message)
    selected_ids = tuple(
        item.story_id for item in envelope.planner_output.selected_stories
    )
    task_fingerprint = planned_task_content_fingerprint(
        envelope.planner_output,
        spec_version_id=plan.spec_version_id,
        spec_hash=plan.spec_hash,
        sprint_plan_stream_id=plan.sprint_plan_stream_id,
        sprint_plan_artifact_id=plan_fact.artifact_id,
        sprint_plan_fingerprint=plan.plan_fingerprint,
    )
    actual_task_fingerprint = current_task_content_fingerprint(
        snapshot.tasks,
        sprint_id=command.expected_sprint_id,
        story_ids=selected_ids,
    )
    if (
        command.expected_task_content_fingerprint != task_fingerprint
        or actual_task_fingerprint != task_fingerprint
    ):
        message = "Sprint start task projection changed after plan acceptance."
        raise ValueError(message)
    decision_id = accepted.sprint_plan_artifact_decision_id
    if decision_id is None or plan_fact.candidate_set_fingerprint is None:
        message = "Accepted Sprint plan has incomplete durable identity."
        raise ValueError(message)
    try:
        dependency_snapshot = selected_story_dependency_snapshot(
            snapshot,
            selected_ids,
        )
    except ExecutionIntegrityError as error:
        raise ValueError(str(error)) from error
    return _ResolvedSprintStart(
        project_id=command.project_id,
        sprint_id=command.expected_sprint_id,
        sprint_plan_artifact_id=plan_fact.artifact_id,
        sprint_plan_artifact_decision_id=decision_id,
        plan_fingerprint=plan.plan_fingerprint,
        candidate_set_fingerprint=plan_fact.candidate_set_fingerprint,
        selected_story_ids=selected_ids,
        task_content_fingerprint=task_fingerprint,
        dependency_snapshot=dependency_snapshot,
        decision_fingerprint=command.decision_fingerprint,
        started_by=command.started_by,
        started_at=command.started_at,
    )


def _selected_dependency_review_id(
    session: Session,
    command: _ResolvedSprintStart,
) -> int:
    dependency = command.dependency_snapshot
    if dependency.story_ids != command.selected_story_ids:
        message = "Sprint dependency scope does not match selected Stories."
        raise ValueError(message)
    selected_json = canonical_json(list(dependency.story_ids))
    edges_json = canonical_json(
        [item.model_dump(mode="json") for item in dependency.reviewed_edges]
    )
    existing = session.exec(
        select(StoryDependencyReview).where(
            StoryDependencyReview.project_id == command.project_id,
            StoryDependencyReview.source_fingerprint == dependency.source_fingerprint,
        )
    ).one_or_none()
    if existing is None:
        existing = StoryDependencyReview(
            project_id=command.project_id,
            selected_story_ids_json=selected_json,
            reviewed_edges_json=edges_json,
            source_fingerprint=dependency.source_fingerprint,
            dependency_fingerprint=dependency.dependency_fingerprint,
            reviewed_by=command.started_by,
            reviewed_at=command.started_at,
        )
        session.add(existing)
        session.flush()
    elif (
        existing.selected_story_ids_json != selected_json
        or existing.reviewed_edges_json != edges_json
        or existing.dependency_fingerprint != dependency.dependency_fingerprint
    ):
        message = "Sprint dependency review conflicts with selected facts."
        raise ValueError(message)
    review_id = existing.story_dependency_review_id
    if review_id is None:
        message = "Sprint dependency review has no durable identity."
        raise ValueError(message)
    return review_id


def start_sprint_in_session(session: Session, inputs: SprintStartInput) -> Sprint:
    """Start the exact Sprint resolved only through its current accepted plan."""
    command = _resolve_sprint_start(session, inputs)
    sprint = session.get(Sprint, command.sprint_id)
    plan = session.get(SprintPlanArtifact, command.sprint_plan_artifact_id)
    decision = session.get(
        SprintPlanArtifactDecision,
        command.sprint_plan_artifact_decision_id,
    )
    if (
        sprint is None
        or sprint.project_id != command.project_id
        or plan is None
        or plan.project_id != command.project_id
        or decision is None
        or decision.project_id != command.project_id
        or decision.sprint_plan_artifact_id != command.sprint_plan_artifact_id
        or decision.plan_fingerprint != command.plan_fingerprint
        or decision.decision != "accepted"
        or decision.activated_sprint_id != command.sprint_id
    ):
        message = "Sprint start does not match an exact accepted Sprint plan."
        raise ValueError(message)
    if sprint.status is not SprintStatus.PLANNED or sprint.started_at is not None:
        message = "Only an unstarted planned Sprint can start."
        raise ValueError(message)
    other_active = session.exec(
        select(Sprint).where(
            Sprint.project_id == command.project_id,
            Sprint.status == SprintStatus.ACTIVE,
            Sprint.sprint_id != command.sprint_id,
        )
    ).first()
    if other_active is not None:
        message = (
            "Another Sprint is already active for this Project. Close it before "
            "starting this Sprint."
        )
        raise ActiveSprintExistsError(message)
    if (
        session.exec(
            select(SprintStart).where(SprintStart.sprint_id == command.sprint_id)
        ).first()
        is not None
    ):
        message = "Sprint has already started from this accepted plan."
        raise ValueError(message)
    dependency_review_id = _selected_dependency_review_id(session, command)
    dependency = command.dependency_snapshot
    metadata = sprint_start_audit_metadata(
        SprintStartAudit(
            sprint_id=command.sprint_id,
            team_id=sprint.team_id,
            sprint_plan_artifact_id=command.sprint_plan_artifact_id,
            sprint_plan_artifact_decision_id=command.sprint_plan_artifact_decision_id,
            story_dependency_review_id=dependency_review_id,
            plan_fingerprint=command.plan_fingerprint,
            candidate_set_fingerprint=command.candidate_set_fingerprint,
            selected_story_ids=command.selected_story_ids,
            task_content_fingerprint=command.task_content_fingerprint,
            dependency_source_fingerprint=dependency.source_fingerprint,
            dependency_fingerprint=dependency.dependency_fingerprint,
            dependency_rows_fingerprint=dependency.rows_fingerprint,
            dependency_rows_snapshot=dependency.rows,
            decision_fingerprint=command.decision_fingerprint,
            started_by=command.started_by,
        )
    )
    event = WorkflowEvent(
        event_type=WorkflowEventType.SPRINT_STARTED,
        timestamp=command.started_at,
        project_id=command.project_id,
        sprint_id=command.sprint_id,
        event_metadata=canonical_json(metadata),
        duration_seconds=0.0,
    )
    session.add(event)
    session.flush()
    if event.event_id is None:
        message = "Sprint start audit event has no durable identity."
        raise ValueError(message)
    sprint.status = SprintStatus.ACTIVE
    sprint.started_at = command.started_at
    sprint.updated_at = command.started_at
    session.add(sprint)
    session.add(
        SprintStart(
            project_id=command.project_id,
            sprint_id=command.sprint_id,
            sprint_plan_artifact_id=command.sprint_plan_artifact_id,
            sprint_plan_artifact_decision_id=command.sprint_plan_artifact_decision_id,
            story_dependency_review_id=dependency_review_id,
            plan_fingerprint=command.plan_fingerprint,
            candidate_set_fingerprint=command.candidate_set_fingerprint,
            selected_story_ids_json=canonical_json(list(command.selected_story_ids)),
            task_content_fingerprint=command.task_content_fingerprint,
            dependency_source_fingerprint=dependency.source_fingerprint,
            dependency_fingerprint=dependency.dependency_fingerprint,
            dependency_rows_fingerprint=dependency.rows_fingerprint,
            decision_fingerprint=command.decision_fingerprint,
            audit_event_id=event.event_id,
            started_by=command.started_by,
            started_at=command.started_at,
        )
    )
    session.flush()
    return sprint


@dataclass(frozen=True)
class SprintReviewInput:
    """Caller-owned inputs for a persisted Sprint review."""

    project_id: int
    sprint_id: int
    review_fingerprint: str
    reviewed_by: str
    reviewed_at: datetime


@dataclass(frozen=True)
class SprintCloseInput:
    """Caller-owned inputs for a persisted Sprint closure."""

    project_id: int
    sprint_id: int
    review_fingerprint: str
    close_fingerprint: str
    closed_by: str
    closed_at: datetime


def review_sprint_in_session(
    session: Session, command: SprintReviewInput
) -> SprintReview:
    """Persist one exact Sprint review in the caller's transaction."""
    snapshot = WorkflowFactRepository(session).load(command.project_id)
    sprint = next(
        (item for item in snapshot.sprints if item.sprint_id == command.sprint_id),
        None,
    )
    attached = tuple(
        item for item in snapshot.stories if command.sprint_id in item.sprint_ids
    )
    closure_ids = {
        item.story_id
        for item in snapshot.story_completions
        if item.sprint_id == command.sprint_id
    }
    if (
        sprint is None
        or sprint.status != "active"
        or not attached
        or any(item.status not in {"Done", "Accepted"} for item in attached)
        or closure_ids != {item.story_id for item in attached}
    ):
        message = "Sprint review requires every attached Story terminal."
        raise ValueError(message)
    expected = sprint_review_fingerprint(snapshot, command.sprint_id)
    if command.review_fingerprint != expected:
        message = "Sprint review fingerprint is stale."
        raise ValueError(message)
    if (
        session.exec(
            select(SprintReview).where(SprintReview.sprint_id == command.sprint_id)
        ).first()
        is not None
    ):
        message = "Sprint review is immutable."
        raise ValueError(message)
    row = SprintReview(
        project_id=command.project_id,
        sprint_id=command.sprint_id,
        review_fingerprint=command.review_fingerprint,
        reviewed_by=command.reviewed_by,
        reviewed_at=command.reviewed_at,
    )
    session.add(row)
    session.flush()
    return row


def close_sprint_in_session(
    session: Session, command: SprintCloseInput
) -> SprintClosure:
    """Close one reviewed Sprint in the caller's transaction."""
    sprint = session.get(Sprint, command.sprint_id)
    if (
        sprint is None
        or sprint.project_id != command.project_id
        or sprint.status is not SprintStatus.ACTIVE
    ):
        message = "Sprint close requires the exact active Project Sprint."
        raise ValueError(message)
    review = session.exec(
        select(SprintReview).where(
            SprintReview.project_id == command.project_id,
            SprintReview.sprint_id == command.sprint_id,
        )
    ).one_or_none()
    if review is None or review.review_fingerprint != command.review_fingerprint:
        message = "Sprint close review fingerprint is stale or missing."
        raise ValueError(message)
    snapshot = WorkflowFactRepository(session).load(command.project_id)
    if (
        sprint_review_fingerprint(snapshot, command.sprint_id)
        != command.review_fingerprint
    ):
        message = "Sprint facts changed after review."
        raise ValueError(message)
    expected_close = sprint_close_fingerprint(
        snapshot,
        command.sprint_id,
        command.review_fingerprint,
    )
    if command.close_fingerprint != expected_close:
        message = "Sprint close fingerprint is stale."
        raise ValueError(message)
    if (
        session.exec(
            select(SprintClosure).where(SprintClosure.sprint_id == command.sprint_id)
        ).first()
        is not None
    ):
        message = "Sprint closure is immutable."
        raise ValueError(message)
    sprint.status = SprintStatus.COMPLETED
    sprint.completed_at = command.closed_at
    sprint.updated_at = command.closed_at
    sprint.close_snapshot_json = None
    closure = SprintClosure(
        project_id=command.project_id,
        sprint_id=command.sprint_id,
        review_fingerprint=command.review_fingerprint,
        close_fingerprint=command.close_fingerprint,
        closed_by=command.closed_by,
        closed_at=command.closed_at,
    )
    session.add(sprint)
    session.add(closure)
    session.add(
        WorkflowEvent(
            event_type=WorkflowEventType.SPRINT_COMPLETED,
            timestamp=command.closed_at,
            project_id=command.project_id,
            sprint_id=command.sprint_id,
            event_metadata=canonical_json(
                {
                    "action": "sprint_closed",
                    "review_fingerprint": command.review_fingerprint,
                    "close_fingerprint": command.close_fingerprint,
                }
            ),
            duration_seconds=0.0,
        )
    )
    session.flush()
    return closure


__all__ = [
    "ActiveSprintExistsError",
    "RecordSprintPlanDecisionInput",
    "RecordSprintPlanInput",
    "SprintCloseInput",
    "SprintPlanStreamCollisionError",
    "SprintReviewInput",
    "SprintStartInput",
    "StaleSpecificationError",
    "close_sprint_in_session",
    "record_sprint_plan_decision_in_session",
    "record_sprint_plan_in_session",
    "review_sprint_in_session",
    "start_sprint_in_session",
]
