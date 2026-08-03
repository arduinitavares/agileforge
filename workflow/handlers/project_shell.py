"""Transactional handlers for the first Project-shell transitions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from models.core import Project
from models.workflow import DiscoveryRun, ProjectAbandonment
from repositories.workflow import WorkflowFactRepository
from workflow.contracts import (
    NodeDecision,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
)

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.graph import WorkflowGraph
    from workflow.requests import AbandonProjectShell, OpenProjectShell


def execute_open_project_shell(
    session: Session,
    request: OpenProjectShell,
    graph: WorkflowGraph,
    evaluated_at: datetime,
) -> TransitionResult:
    """Insert one Project and exactly one initial DiscoveryRun."""
    existing = session.exec(
        select(Project).where(col(Project.name) == request.name)
    ).first()
    if existing is not None:
        return TransitionResult(
            ok=False,
            error=WorkflowError(
                code=WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
                message=f"Project name {request.name!r} already exists.",
            ),
        )

    project = Project(
        name=request.name,
        origin=request.origin,
        created_at=evaluated_at,
        updated_at=evaluated_at,
    )
    session.add(project)
    session.flush()
    if project.project_id is None:
        msg = "Project identity was not assigned after flush."
        raise RuntimeError(msg)

    discovery_run = DiscoveryRun(
        project_id=project.project_id,
        purpose="initial",
        ordinal=1,
        created_at=evaluated_at,
    )
    session.add(discovery_run)
    session.flush()
    if discovery_run.discovery_run_id is None:
        msg = "Initial discovery-run identity was not assigned after flush."
        raise RuntimeError(msg)

    snapshot = WorkflowFactRepository(session).load(project.project_id)
    position = graph.evaluate(snapshot, evaluated_at)
    return TransitionResult(
        ok=True,
        applied_node_id="onboarding.open_project_shell",
        output={
            "project_id": project.project_id,
            "discovery_run_id": discovery_run.discovery_run_id,
        },
        position=position,
    )


def execute_abandon_project_shell(
    session: Session,
    request: AbandonProjectShell,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Insert the attributed typed abandonment fact."""
    abandonment = ProjectAbandonment(
        project_id=request.project_id,
        reason=request.reason,
        abandoned_by=request.actor,
        abandoned_at=evaluated_at,
    )
    session.add(abandonment)
    session.flush()
    if abandonment.project_abandonment_id is None:
        msg = "Project-abandonment identity was not assigned after flush."
        raise RuntimeError(msg)
    return TransitionResult(
        ok=True,
        applied_node_id=decision.node_id,
        output={"project_abandonment_id": abandonment.project_abandonment_id},
    )
