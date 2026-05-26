"""Tests for agent workbench Sprint phase runner."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from sqlmodel import Session, select

from models.core import Product, Sprint, SprintStory, Task, Team, UserStory
from models.enums import SprintStatus, TaskStatus, WorkflowEventType
from models.events import WorkflowEvent
from services.agent_workbench import sprint_phase as sprint_phase_module
from services.agent_workbench.sprint_phase import SprintPhaseRunner
from services.phases import sprint_service
from utils.task_metadata import TaskMetadata, serialize_task_metadata

if TYPE_CHECKING:
    import pytest

JsonDict = dict[str, Any]


class _FakeProductRepository:
    def get_by_id(self, product_id: int) -> object:
        """Return a lightweight product sentinel."""
        return SimpleNamespace(product_id=product_id, name="Product")


class _FakeWorkflowService:
    def __init__(self) -> None:
        self.state: JsonDict = {"fsm_state": "SPRINT_SETUP"}

    def get_session_status(self, _session_id: str) -> JsonDict:
        """Return current workflow state."""
        return self.state

    async def initialize_session(self, *, session_id: str) -> None:
        """Initialize fake state."""
        self.state = {"fsm_state": "SPRINT_SETUP", "session_id": session_id}

    def update_session_status(self, _session_id: str, state: JsonDict) -> None:
        """Persist fake state."""
        self.state = dict(state)


def test_sprint_runner_generate_wraps_keyword_only_failure_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint runner should not pass keyword-only failure metadata positionally."""

    async def fake_run_sprint_agent(_state: object, **_kwargs: object) -> JsonDict:
        return {
            "success": True,
            "input_context": {"available_stories": []},
            "output_artifact": {"is_complete": True},
            "is_complete": True,
            "error": None,
        }

    monkeypatch.setattr(
        sprint_phase_module,
        "run_sprint_agent_from_state",
        fake_run_sprint_agent,
    )
    monkeypatch.setattr(
        sprint_service,
        "load_sprint_candidates",
        lambda _project_id: {
            "success": True,
            "count": 1,
            "stories": [{"story_id": 1}],
            "readiness": {"status": "ready"},
        },
    )
    monkeypatch.setattr(
        SprintPhaseRunner,
        "_current_planned_sprint_id",
        lambda _self, _project_id: None,
    )

    runner = SprintPhaseRunner(
        product_repo=_FakeProductRepository(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.generate(project_id=7)

    assert result["ok"] is True
    assert result["data"]["fsm_state"] == "SPRINT_DRAFT"
    assert result["data"]["attempt_id"] == "sprint-attempt-1"


def test_sprint_runner_start_status_and_tasks_use_persisted_sprint(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint runner starts the saved Sprint and exposes execution task rows."""
    product = Product(name="Runner Product")
    team = Team(name="Runner Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    story = UserStory(
        product_id=product.product_id,
        title="Execute live workflow",
        story_description="As an operator, I run the workflow.",
        acceptance_criteria="- Produces artifacts",
        story_points=3,
        rank="101",
        is_refined=True,
    )
    session.add(story)
    session.flush()
    assert story.story_id is not None

    sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Deliver live workflow",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.PLANNED,
    )
    session.add(sprint)
    session.flush()
    assert sprint.sprint_id is not None

    session.add(SprintStory(sprint_id=sprint.sprint_id, story_id=story.story_id))
    task = Task(
        story_id=story.story_id,
        description="Implement live command",
        status=TaskStatus.TO_DO,
        metadata_json=serialize_task_metadata(
            TaskMetadata(
                task_kind="implementation",
                artifact_targets=["live.py"],
                checklist_items=["CLI accepts explicit budget"],
            )
        )
    )
    session.add(task)
    session.commit()
    assert task.task_id is not None

    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    workflow = _FakeWorkflowService()
    workflow.state = {"fsm_state": "SPRINT_PERSISTENCE"}
    runner = SprintPhaseRunner(
        product_repo=_FakeProductRepository(),
        workflow_service=workflow,
    )

    started = runner.start(
        project_id=product.product_id,
        expected_state="SPRINT_PERSISTENCE",
        idempotency_key="start-runner-sprint-001",
    )
    status = runner.status(project_id=product.product_id)
    tasks = runner.tasks(project_id=product.product_id)

    assert started["ok"] is True
    assert started["data"]["fsm_state"] == "SPRINT_VIEW"
    assert workflow.state["fsm_state"] == "SPRINT_VIEW"
    assert status["data"]["sprint"]["status"] == "Active"
    assert tasks["data"]["tasks"] == [
        {
            "task_id": task.task_id,
            "story_id": story.story_id,
            "story_title": "Execute live workflow",
            "description": "Implement live command",
            "status": "To Do",
            "task_kind": "implementation",
            "artifact_targets": ["live.py"],
            "workstream_tags": [],
            "relevant_invariant_ids": [],
            "checklist_items": ["CLI accepts explicit budget"],
        }
    ]

    persisted_sprint = session.get(Sprint, sprint.sprint_id)
    assert persisted_sprint is not None
    assert persisted_sprint.status == SprintStatus.ACTIVE
    event = session.exec(
        select(WorkflowEvent).where(
            WorkflowEvent.event_type == WorkflowEventType.SPRINT_STARTED
        )
    ).first()
    assert event is not None
