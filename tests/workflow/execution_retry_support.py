# tests/workflow/execution_retry_support.py
"""Shared synthetic execution lifecycle setup for retry and scope tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict

from tests.workflow.execution_fixtures import seed_started_execution
from workflow.clock import FixedClock
from workflow.definitions.execution import execution_graph
from workflow.domain import WorkflowDomain
from workflow.requests import (
    CloseSprint,
    CloseStory,
    CompleteTask,
    RecordPostSprintTriage,
    ReviewSprint,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from workflow.contracts import NodeDecision


EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)


class _PositionGuards(TypedDict):
    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    actor: str


def _domain(engine: Engine) -> WorkflowDomain:
    return WorkflowDomain(
        engine=engine,
        graph=execution_graph(),
        clock=FixedClock(now_value=EVALUATED_AT),
    )


def _decision(
    domain: WorkflowDomain,
    project_id: int,
    node_id: str,
    instance_key: str | None = None,
) -> NodeDecision:
    return next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == node_id and item.instance_key == instance_key
    )


def _guards(
    domain: WorkflowDomain,
    project_id: int,
    node_id: str,
    instance_key: str | None = None,
) -> _PositionGuards:
    position = domain.position(project_id)
    decision = next(
        item
        for item in position.decisions
        if item.node_id == node_id and item.instance_key == instance_key
    )
    return {
        "project_id": project_id,
        "graph_version": position.graph_version,
        "fact_fingerprint": position.fact_fingerprint,
        "decision_fingerprint": decision.decision_fingerprint,
        "actor": "operator@example.com",
    }


def _complete_task(
    domain: WorkflowDomain,
    project_id: int,
    task_id: int,
    *,
    idempotency_key: str = "complete-task",
) -> CompleteTask:
    return CompleteTask(
        **_guards(domain, project_id, "execution.task.complete", f"task:{task_id}"),
        instance_key=f"task:{task_id}",
        idempotency_key=idempotency_key,
        task_id=task_id,
        outcome_summary="Implemented execution graph.",
        artifact_refs=("workflow/definitions/execution.py",),
        acceptance_result="fully_met",
        checklist_result={"Run focused tests": "passed"},
    )


def _close_execution_sprint(  # noqa: PLR0913
    domain: WorkflowDomain,
    *,
    project_id: int,
    sprint_id: int,
    story_id: int,
    task_id: int,
    idempotency_suffix: str = "",
) -> str:
    """Complete one normalized single-Story Sprint through explicit close."""
    assert (
        domain.transition(
            _complete_task(
                domain,
                project_id,
                task_id,
                idempotency_key=f"complete-task{idempotency_suffix}",
            )
        ).ok
        is True
    )
    assert (
        domain.transition(
            CloseStory(
                **_guards(
                    domain, project_id, "execution.story.close", f"story:{story_id}"
                ),
                instance_key=f"story:{story_id}",
                idempotency_key=f"close-story{idempotency_suffix}",
                story_id=story_id,
                resolution="Completed",
                delivered="Execution graph delivered.",
                evidence="Focused tests pass.",
                known_gaps="None.",
            )
        ).ok
        is True
    )
    review_decision = _decision(
        domain,
        project_id,
        "execution.sprint.review",
        f"sprint:{sprint_id}",
    )
    review_fingerprint = next(
        ref.fingerprint
        for ref in review_decision.fact_references
        if ref.fact_type == "sprint_review"
    )
    assert (
        domain.transition(
            ReviewSprint(
                **_guards(
                    domain,
                    project_id,
                    "execution.sprint.review",
                    f"sprint:{sprint_id}",
                ),
                instance_key=f"sprint:{sprint_id}",
                idempotency_key=f"review-sprint{idempotency_suffix}",
                sprint_id=sprint_id,
                review_fingerprint=review_fingerprint,
            )
        ).ok
        is True
    )
    assert (
        domain.transition(
            CloseSprint(
                **_guards(
                    domain,
                    project_id,
                    "execution.sprint.close",
                    f"sprint:{sprint_id}",
                ),
                instance_key=f"sprint:{sprint_id}",
                idempotency_key=f"close-sprint{idempotency_suffix}",
                sprint_id=sprint_id,
                review_fingerprint=review_fingerprint,
            )
        ).ok
        is True
    )
    return review_fingerprint


def _complete_execution_sprint(
    engine: Engine,
) -> tuple[WorkflowDomain, int, int, int, int, str]:
    project_id, sprint_id, story_id, task_id = seed_started_execution(engine)
    domain = _domain(engine)
    review_fingerprint = _close_execution_sprint(
        domain,
        project_id=project_id,
        sprint_id=sprint_id,
        story_id=story_id,
        task_id=task_id,
    )
    return domain, project_id, sprint_id, story_id, task_id, review_fingerprint


def _triage_execution_sprint(
    domain: WorkflowDomain,
    *,
    project_id: int,
    sprint_id: int,
) -> None:
    triaged = domain.transition(
        RecordPostSprintTriage(
            **_guards(
                domain,
                project_id,
                "execution.post_sprint_triage",
                f"sprint:{sprint_id}",
            ),
            instance_key=f"sprint:{sprint_id}",
            idempotency_key=f"triage-sprint-{sprint_id}",
            sprint_id=sprint_id,
            impact="none",
            canonical_payload={"summary": "No downstream change."},
        )
    )
    assert triaged.ok is True
