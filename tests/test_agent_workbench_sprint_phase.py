"""Tests for agent workbench Sprint phase runner."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from sqlmodel import Session, select

from models.core import (
    Product,
    Sprint,
    SprintStory,
    Task,
    Team,
    UserStory,
    UserStoryDependency,
)
from models.enums import SprintStatus, StoryStatus, TaskStatus, WorkflowEventType
from models.events import WorkflowEvent
from services.agent_workbench import sprint_phase as sprint_phase_module
from services.agent_workbench.sprint_phase import SprintPhaseRunner
from services.phases import sprint_service
from utils.task_metadata import TaskMetadata, serialize_task_metadata

if TYPE_CHECKING:
    import pytest

JsonDict = dict[str, Any]
MIDDLE_STORY_EXECUTION_ORDER = 2
DEPENDENT_STORY_EXECUTION_ORDER = 3


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
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
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
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", workflow),
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
            "task_execution_order": 1,
            "story_execution_order": 1,
            "direct_blocked_by_story_ids": [],
            "blocked_by_story_ids": [],
            "unblocks_story_ids": [],
            "is_blocked": False,
            "dependency_order_source": "active_story_dependencies",
        }
    ]
    assert tasks["data"]["dependency_summary"] == {
        "active_edge_count": 0,
        "cycle_count": 0,
        "blocked_story_count": 0,
        "ordering": "topological",
    }

    persisted_sprint = session.get(Sprint, sprint.sprint_id)
    assert persisted_sprint is not None
    assert persisted_sprint.status == SprintStatus.ACTIVE
    event = session.exec(
        select(WorkflowEvent).where(
            WorkflowEvent.event_type == WorkflowEventType.SPRINT_STARTED
        )
    ).first()
    assert event is not None


def test_sprint_runner_tasks_include_dependency_safe_execution_metadata(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint tasks should be ordered by story dependencies and expose blockers."""
    product = Product(name="Dependency Product")
    team = Team(name="Dependency Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    upstream = UserStory(
        product_id=product.product_id,
        title="Capture pre-lock market data",
        story_points=3,
        rank="201",
        status=StoryStatus.TO_DO,
        is_refined=True,
    )
    middle = UserStory(
        product_id=product.product_id,
        title="Validate pre-lock capture",
        story_points=2,
        rank="202",
        status=StoryStatus.TO_DO,
        is_refined=True,
    )
    dependent = UserStory(
        product_id=product.product_id,
        title="Execute live workflow",
        story_points=3,
        rank="102",
        status=StoryStatus.TO_DO,
        is_refined=True,
    )
    historical_done = UserStory(
        product_id=product.product_id,
        title="Historical budget gate",
        story_points=1,
        rank="101",
        status=StoryStatus.DONE,
        is_refined=True,
    )
    session.add_all([upstream, middle, dependent, historical_done])
    session.flush()
    assert upstream.story_id is not None
    assert middle.story_id is not None
    assert dependent.story_id is not None
    assert historical_done.story_id is not None

    session.add_all(
        [
            UserStoryDependency(
                product_id=product.product_id,
                dependent_story_id=middle.story_id,
                prerequisite_story_id=upstream.story_id,
                status="active",
                source="manual_review",
                confidence="reviewed",
            ),
            UserStoryDependency(
                product_id=product.product_id,
                dependent_story_id=dependent.story_id,
                prerequisite_story_id=middle.story_id,
                status="active",
                source="manual_review",
                confidence="reviewed",
            ),
            UserStoryDependency(
                product_id=product.product_id,
                dependent_story_id=dependent.story_id,
                prerequisite_story_id=historical_done.story_id,
                status="active",
                source="manual_review",
                confidence="reviewed",
            ),
        ]
    )

    sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Deliver live workflow safely",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.ACTIVE,
    )
    session.add(sprint)
    session.flush()
    assert sprint.sprint_id is not None
    session.add_all(
        [
            SprintStory(sprint_id=sprint.sprint_id, story_id=dependent.story_id),
            SprintStory(sprint_id=sprint.sprint_id, story_id=upstream.story_id),
            SprintStory(sprint_id=sprint.sprint_id, story_id=middle.story_id),
        ]
    )
    session.add_all(
        [
            Task(story_id=dependent.story_id, description="Implement live workflow"),
            Task(story_id=upstream.story_id, description="Implement market capture"),
            Task(story_id=middle.story_id, description="Implement capture guard"),
        ]
    )
    session.commit()

    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )

    result = runner.tasks(project_id=product.product_id)

    assert result["ok"] is True
    assert result["warnings"] == []
    data = result["data"]
    assert data["dependency_summary"] == {
        "active_edge_count": 3,
        "cycle_count": 0,
        "blocked_story_count": 2,
        "ordering": "topological",
    }
    assert [task["story_id"] for task in data["tasks"]] == [
        upstream.story_id,
        middle.story_id,
        dependent.story_id,
    ]
    by_story_id = {task["story_id"]: task for task in data["tasks"]}
    assert by_story_id[upstream.story_id]["story_execution_order"] == 1
    assert by_story_id[upstream.story_id]["direct_blocked_by_story_ids"] == []
    assert by_story_id[upstream.story_id]["blocked_by_story_ids"] == []
    assert by_story_id[upstream.story_id]["unblocks_story_ids"] == [middle.story_id]
    assert by_story_id[upstream.story_id]["is_blocked"] is False

    assert (
        by_story_id[middle.story_id]["story_execution_order"]
        == MIDDLE_STORY_EXECUTION_ORDER
    )
    assert by_story_id[middle.story_id]["direct_blocked_by_story_ids"] == [
        upstream.story_id
    ]
    assert by_story_id[middle.story_id]["blocked_by_story_ids"] == [
        upstream.story_id
    ]
    assert by_story_id[middle.story_id]["is_blocked"] is True

    assert (
        by_story_id[dependent.story_id]["story_execution_order"]
        == DEPENDENT_STORY_EXECUTION_ORDER
    )
    assert by_story_id[dependent.story_id]["direct_blocked_by_story_ids"] == [
        middle.story_id,
        historical_done.story_id,
    ]
    assert by_story_id[dependent.story_id]["blocked_by_story_ids"] == [
        upstream.story_id,
        middle.story_id,
    ]
    assert by_story_id[dependent.story_id]["unblocks_story_ids"] == []
    assert by_story_id[dependent.story_id]["is_blocked"] is True
    assert by_story_id[dependent.story_id]["dependency_order_source"] == (
        "active_story_dependencies"
    )


def test_sprint_runner_tasks_warn_and_fallback_on_dependency_cycle(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint task view should stay usable when active dependencies contain a cycle."""
    product = Product(name="Cycle Product")
    team = Team(name="Cycle Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    first = UserStory(
        product_id=product.product_id,
        title="First cyclic story",
        story_points=1,
        rank="101",
        status=StoryStatus.TO_DO,
        is_refined=True,
    )
    second = UserStory(
        product_id=product.product_id,
        title="Second cyclic story",
        story_points=1,
        rank="102",
        status=StoryStatus.TO_DO,
        is_refined=True,
    )
    session.add_all([first, second])
    session.flush()
    assert first.story_id is not None
    assert second.story_id is not None
    session.add_all(
        [
            UserStoryDependency(
                product_id=product.product_id,
                dependent_story_id=first.story_id,
                prerequisite_story_id=second.story_id,
                status="active",
                source="manual_review",
                confidence="reviewed",
            ),
            UserStoryDependency(
                product_id=product.product_id,
                dependent_story_id=second.story_id,
                prerequisite_story_id=first.story_id,
                status="active",
                source="manual_review",
                confidence="reviewed",
            ),
        ]
    )
    sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Cycle fallback",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.ACTIVE,
    )
    session.add(sprint)
    session.flush()
    assert sprint.sprint_id is not None
    session.add_all(
        [
            SprintStory(sprint_id=sprint.sprint_id, story_id=first.story_id),
            SprintStory(sprint_id=sprint.sprint_id, story_id=second.story_id),
            Task(story_id=first.story_id, description="First task"),
            Task(story_id=second.story_id, description="Second task"),
        ]
    )
    session.commit()

    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )

    result = runner.tasks(project_id=product.product_id)

    assert result["ok"] is True
    assert result["warnings"] == [
        {
            "code": "SPRINT_TASK_DEPENDENCY_CYCLE_FALLBACK",
            "message": (
                "Active story dependencies contain a cycle; Sprint tasks are "
                "returned using rank fallback order."
            ),
            "details": {
                "cycle_paths": [[first.story_id, second.story_id, first.story_id]]
            },
        }
    ]
    assert result["data"]["dependency_summary"] == {
        "active_edge_count": 2,
        "cycle_count": 1,
        "blocked_story_count": 2,
        "ordering": "rank_fallback",
    }
    assert [task["story_id"] for task in result["data"]["tasks"]] == [
        first.story_id,
        second.story_id,
    ]
