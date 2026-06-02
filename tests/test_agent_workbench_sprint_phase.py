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
from models.enums import (
    SprintStatus,
    StoryResolution,
    StoryStatus,
    TaskAcceptanceResult,
    TaskStatus,
    WorkflowEventType,
)
from models.events import StoryCompletionLog, TaskExecutionLog, WorkflowEvent
from services.agent_workbench import sprint_phase as sprint_phase_module
from services.agent_workbench.sprint_phase import SprintPhaseRunner
from services.phases import sprint_service
from utils.task_metadata import TaskMetadata, serialize_task_metadata

if TYPE_CHECKING:
    import pytest

JsonDict = dict[str, Any]
MIDDLE_STORY_EXECUTION_ORDER = 2
DEPENDENT_STORY_EXECUTION_ORDER = 3
TASK_FINGERPRINT_PREFIX = "sha256:"


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


def test_sprint_runner_generate_blocks_stale_downstream_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint generate returns existing failure envelope when backlog is stale."""
    captured: JsonDict = {"agent_calls": 0}

    async def fake_run_sprint_agent(_state: object, **_kwargs: object) -> JsonDict:
        captured["agent_calls"] += 1
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
    workflow_service = _FakeWorkflowService()
    workflow_service.state.update(
        {
            "downstream_backlog_stale": True,
            "stale_backlog_reason": "backlog refinement changed",
            "stale_since_backlog_attempt_id": "backlog-attempt-7",
        }
    )
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", workflow_service),
    )

    result = runner.generate(project_id=7)

    assert result["ok"] is False
    assert result["data"] is None
    assert result["warnings"] == []
    assert result["errors"][0]["code"] == "INVALID_COMMAND"
    assert "downstream backlog is stale" in result["errors"][0]["message"]
    assert "backlog refinement changed" in result["errors"][0]["message"]
    assert "backlog-attempt-7" in result["errors"][0]["message"]
    assert captured["agent_calls"] == 0
    assert workflow_service.state["downstream_backlog_stale"] is True
    assert workflow_service.state["stale_backlog_reason"] == (
        "backlog refinement changed"
    )
    assert workflow_service.state["stale_since_backlog_attempt_id"] == (
        "backlog-attempt-7"
    )


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
        ),
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
        "dependency_review_required_story_count": 0,
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
        "dependency_review_required_story_count": 0,
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
    assert by_story_id[middle.story_id]["blocked_by_story_ids"] == [upstream.story_id]
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
        "dependency_review_required_story_count": 0,
        "ordering": "rank_fallback",
    }
    assert [task["story_id"] for task in result["data"]["tasks"]] == [
        first.story_id,
        second.story_id,
    ]


def test_sprint_task_next_skips_story_with_missing_semantic_dependency(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task next should not recommend integration work with missing prereq edges."""
    product = Product(name="Risk Product")
    team = Team(name="Risk Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    live_workflow = UserStory(
        product_id=product.product_id,
        title="Execute Live Pre-Lock Recommendation Workflow",
        story_description=(
            "Run the live recommendation workflow using a valid pre-lock market "
            "capture and reject finalized target-round data leaks."
        ),
        acceptance_criteria=(
            "- Verify the command uses a valid pre-lock market capture.\n"
            "- Verify no finalized target-round data is present."
        ),
        story_points=3,
        rank="102",
        status=StoryStatus.TO_DO,
        is_refined=True,
    )
    capture = UserStory(
        product_id=product.product_id,
        title="Capture Pre-Lock Cartola Market Data",
        story_description="Capture the pre-lock market data before recommendations.",
        acceptance_criteria="- Verify capture metadata is written.",
        story_points=3,
        rank="201",
        status=StoryStatus.TO_DO,
        is_refined=True,
    )
    session.add_all([live_workflow, capture])
    session.flush()
    assert live_workflow.story_id is not None
    assert capture.story_id is not None

    sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Live recommendation",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.ACTIVE,
    )
    session.add(sprint)
    session.flush()
    assert sprint.sprint_id is not None
    session.add_all(
        [
            SprintStory(sprint_id=sprint.sprint_id, story_id=live_workflow.story_id),
            SprintStory(sprint_id=sprint.sprint_id, story_id=capture.story_id),
            Task(story_id=live_workflow.story_id, description="Design live workflow"),
            Task(story_id=capture.story_id, description="Design capture contract"),
        ]
    )
    session.commit()

    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )

    tasks = runner.tasks(project_id=product.product_id)
    next_ticket = runner.task_next(project_id=product.product_id)

    by_story_id = {task["story_id"]: task for task in tasks["data"]["tasks"]}
    assert by_story_id[live_workflow.story_id]["dependency_review_required"] is True
    assert by_story_id[live_workflow.story_id]["missing_dependency_story_ids"] == [
        capture.story_id
    ]
    assert (
        by_story_id[live_workflow.story_id]["dependency_review_candidates"][0][
            "story_id"
        ]
        == capture.story_id
    )
    assert tasks["warnings"] == [
        {
            "code": "SPRINT_TASK_DEPENDENCY_REVIEW_REQUIRED",
            "message": (
                "Some Sprint stories reference unfinished peer stories without "
                "active dependency edges."
            ),
            "details": {
                "story_ids": [live_workflow.story_id],
                "missing_dependency_pairs": [
                    {
                        "dependent_story_id": live_workflow.story_id,
                        "prerequisite_story_id": capture.story_id,
                        "matched_terms": ["capture", "market", "pre-lock"],
                    }
                ],
            },
        }
    ]
    assert tasks["data"]["dependency_summary"] == {
        "active_edge_count": 0,
        "cycle_count": 0,
        "blocked_story_count": 0,
        "dependency_review_required_story_count": 1,
        "ordering": "topological",
    }
    assert next_ticket["data"]["task_ticket"]["story"]["story_id"] == capture.story_id


def test_sprint_task_next_returns_in_progress_ticket_before_new_todo(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task next should resume existing work before selecting new work."""
    product = Product(name="Ticket Product")
    team = Team(name="Ticket Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    story = UserStory(
        product_id=product.product_id,
        title="Enforce explicit budget",
        story_description="As an operator, I provide explicit budget.",
        acceptance_criteria="- Missing budget fails",
        story_points=1,
        rank="101",
        status=StoryStatus.IN_PROGRESS,
        is_refined=True,
    )
    session.add(story)
    session.flush()
    assert story.story_id is not None

    sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Deliver budget guard",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.ACTIVE,
    )
    session.add(sprint)
    session.flush()
    assert sprint.sprint_id is not None
    session.add(SprintStory(sprint_id=sprint.sprint_id, story_id=story.story_id))

    first = Task(
        story_id=story.story_id,
        description="Design budget contract",
        status=TaskStatus.IN_PROGRESS,
        metadata_json=serialize_task_metadata(
            TaskMetadata(
                task_kind="design",
                artifact_targets=["docs/budget.md"],
                checklist_items=["Define missing-budget behavior"],
            )
        ),
    )
    second = Task(
        story_id=story.story_id,
        description="Implement budget parser",
        status=TaskStatus.TO_DO,
        metadata_json=serialize_task_metadata(
            TaskMetadata(
                task_kind="implementation",
                artifact_targets=["scripts/run_live_round.py"],
                checklist_items=["Require --budget"],
            )
        ),
    )
    session.add_all([first, second])
    session.commit()
    assert first.task_id is not None

    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )

    result = runner.task_next(project_id=product.product_id)

    assert result["ok"] is True
    ticket = result["data"]["task_ticket"]
    assert ticket["task"]["task_id"] == first.task_id
    assert ticket["task"]["status"] == "In Progress"
    assert ticket["story"]["story_id"] == story.story_id
    assert ticket["story"]["acceptance_criteria"] == "- Missing budget fails"
    assert ticket["execution"]["is_blocked"] is False
    assert ticket["work_contract"]["artifact_targets"] == ["docs/budget.md"]
    assert ticket["guards"]["expected_status"] == "In Progress"
    assert str(ticket["guards"]["expected_task_fingerprint"]).startswith(
        TASK_FINGERPRINT_PREFIX
    )
    assert "agileforge sprint task update" in ticket["next_actions"]["update"]


def test_sprint_task_update_replays_done_without_duplicate_logs(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task update idempotency must replay before stale status guards run."""
    product = Product(name="Replay Product")
    team = Team(name="Replay Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    story = UserStory(
        product_id=product.product_id,
        title="Budget implementation",
        story_description="As an operator, I run with explicit budget.",
        acceptance_criteria="- Budget is explicit",
        story_points=1,
        rank="101",
        status=StoryStatus.IN_PROGRESS,
        is_refined=True,
    )
    session.add(story)
    session.flush()
    assert story.story_id is not None

    sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Deliver budget implementation",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.ACTIVE,
    )
    session.add(sprint)
    session.flush()
    assert sprint.sprint_id is not None
    session.add(SprintStory(sprint_id=sprint.sprint_id, story_id=story.story_id))

    task = Task(
        story_id=story.story_id,
        description="Implement required --budget",
        status=TaskStatus.IN_PROGRESS,
        metadata_json=serialize_task_metadata(
            TaskMetadata(
                task_kind="implementation",
                artifact_targets=["scripts/run_live_round.py"],
                checklist_items=["Parser rejects missing budget"],
            )
        ),
    )
    session.add(task)
    session.commit()
    assert task.task_id is not None

    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )
    show = runner.task_show(project_id=product.product_id, task_id=task.task_id)
    fingerprint = show["data"]["task_ticket"]["guards"]["expected_task_fingerprint"]

    first = runner.task_update(
        project_id=product.product_id,
        task_id=task.task_id,
        status="Done",
        expected_status="In Progress",
        expected_task_fingerprint=str(fingerprint),
        idempotency_key="complete-task-budget-001",
        outcome_summary="Implemented required budget validation.",
        artifact_refs=["scripts/run_live_round.py"],
        checklist_result="fully_met",
        validation_summary="uv run pytest tests/test_live_budget.py -q",
        changed_by="cli-agent",
    )
    replay = runner.task_update(
        project_id=product.product_id,
        task_id=task.task_id,
        status="Done",
        expected_status="In Progress",
        expected_task_fingerprint=str(fingerprint),
        idempotency_key="complete-task-budget-001",
        outcome_summary="Implemented required budget validation.",
        artifact_refs=["scripts/run_live_round.py"],
        checklist_result="fully_met",
        validation_summary="uv run pytest tests/test_live_budget.py -q",
        changed_by="cli-agent",
    )

    assert first["ok"] is True
    assert replay["ok"] is True
    assert replay["data"]["idempotency"]["replayed"] is True
    assert first["data"]["execution"]["current_status"] == "Done"
    assert replay["data"]["execution"]["current_status"] == "Done"
    session.expire_all()
    persisted_task = session.get(Task, task.task_id)
    assert persisted_task is not None
    assert persisted_task.status == TaskStatus.DONE
    logs = session.exec(select(TaskExecutionLog)).all()
    assert len(logs) == 1
    assert logs[0].changed_by == "cli-agent"
    assert logs[0].acceptance_result == TaskAcceptanceResult.FULLY_MET


def test_sprint_task_update_rejects_blocked_task_start(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocked tasks cannot be moved into execution."""
    product = Product(name="Blocked Product")
    team = Team(name="Blocked Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    prerequisite = UserStory(
        product_id=product.product_id,
        title="Capture market data",
        story_points=3,
        rank="201",
        status=StoryStatus.TO_DO,
        is_refined=True,
    )
    dependent = UserStory(
        product_id=product.product_id,
        title="Run live workflow",
        story_points=3,
        rank="202",
        status=StoryStatus.TO_DO,
        is_refined=True,
    )
    session.add_all([prerequisite, dependent])
    session.flush()
    assert prerequisite.story_id is not None
    assert dependent.story_id is not None
    session.add(
        UserStoryDependency(
            product_id=product.product_id,
            dependent_story_id=dependent.story_id,
            prerequisite_story_id=prerequisite.story_id,
            status="active",
            source="manual_review",
            confidence="reviewed",
        )
    )

    sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Respect blockers",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.ACTIVE,
    )
    session.add(sprint)
    session.flush()
    assert sprint.sprint_id is not None
    session.add_all(
        [
            SprintStory(sprint_id=sprint.sprint_id, story_id=prerequisite.story_id),
            SprintStory(sprint_id=sprint.sprint_id, story_id=dependent.story_id),
        ]
    )
    blocked_task = Task(
        story_id=dependent.story_id,
        description="Run workflow",
        status=TaskStatus.TO_DO,
        metadata_json=serialize_task_metadata(
            TaskMetadata(checklist_items=["Workflow uses captured data"])
        ),
    )
    session.add(blocked_task)
    session.commit()
    assert blocked_task.task_id is not None

    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )
    show = runner.task_show(
        project_id=product.product_id,
        task_id=blocked_task.task_id,
    )
    fingerprint = show["data"]["task_ticket"]["guards"]["expected_task_fingerprint"]

    result = runner.task_update(
        project_id=product.product_id,
        task_id=blocked_task.task_id,
        status="In Progress",
        expected_status="To Do",
        expected_task_fingerprint=str(fingerprint),
        idempotency_key="start-blocked-task-001",
        notes="Trying to start blocked work.",
        changed_by="cli-agent",
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "MUTATION_FAILED"
    assert result["errors"][0]["details"]["reason_code"] == "SPRINT_TASK_BLOCKED"
    assert result["errors"][0]["details"]["blocked_by_story_ids"] == [
        prerequisite.story_id
    ]
    persisted_task = session.get(Task, blocked_task.task_id)
    assert persisted_task is not None
    assert persisted_task.status == TaskStatus.TO_DO


def test_sprint_task_update_rejects_dependency_review_required_task_start(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agents cannot start risky integration tasks before dependency review."""
    product = Product(name="Risk Update Product")
    team = Team(name="Risk Update Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    live_workflow = UserStory(
        product_id=product.product_id,
        title="Execute Live Pre-Lock Recommendation Workflow",
        story_description=(
            "Run the live recommendation workflow using a valid pre-lock market "
            "capture."
        ),
        acceptance_criteria="- Verify the command uses pre-lock market capture.",
        story_points=3,
        rank="102",
        status=StoryStatus.TO_DO,
        is_refined=True,
    )
    capture = UserStory(
        product_id=product.product_id,
        title="Capture Pre-Lock Cartola Market Data",
        story_points=3,
        rank="201",
        status=StoryStatus.TO_DO,
        is_refined=True,
    )
    session.add_all([live_workflow, capture])
    session.flush()
    assert live_workflow.story_id is not None
    assert capture.story_id is not None

    sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Reject risky start",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.ACTIVE,
    )
    session.add(sprint)
    session.flush()
    assert sprint.sprint_id is not None
    risky_task = Task(
        story_id=live_workflow.story_id,
        description="Design live workflow",
        status=TaskStatus.TO_DO,
    )
    session.add_all(
        [
            SprintStory(sprint_id=sprint.sprint_id, story_id=live_workflow.story_id),
            SprintStory(sprint_id=sprint.sprint_id, story_id=capture.story_id),
            risky_task,
            Task(story_id=capture.story_id, description="Design capture contract"),
        ]
    )
    session.commit()
    assert risky_task.task_id is not None

    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )
    show = runner.task_show(
        project_id=product.product_id,
        task_id=risky_task.task_id,
    )
    fingerprint = show["data"]["task_ticket"]["guards"]["expected_task_fingerprint"]

    result = runner.task_update(
        project_id=product.product_id,
        task_id=risky_task.task_id,
        status="In Progress",
        expected_status="To Do",
        expected_task_fingerprint=str(fingerprint),
        idempotency_key="start-risky-task-001",
        notes="Trying to start risky workflow work.",
        changed_by="cli-agent",
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "MUTATION_FAILED"
    assert result["errors"][0]["details"]["reason_code"] == (
        "SPRINT_TASK_DEPENDENCY_REVIEW_REQUIRED"
    )
    assert result["errors"][0]["details"]["missing_dependency_story_ids"] == [
        capture.story_id
    ]
    persisted_task = session.get(Task, risky_task.task_id)
    assert persisted_task is not None
    assert persisted_task.status == TaskStatus.TO_DO


def test_sprint_story_close_marks_story_done_and_unblocks_next_task(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story close should mark Done and refresh dependency-aware task selection."""
    product = Product(name="Story Close Product")
    team = Team(name="Story Close Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    completed_story = UserStory(
        product_id=product.product_id,
        title="Enforce budget",
        story_points=1,
        rank="101",
        status=StoryStatus.TO_DO,
        is_refined=True,
    )
    dependent_story = UserStory(
        product_id=product.product_id,
        title="Run live workflow",
        story_points=3,
        rank="102",
        status=StoryStatus.TO_DO,
        is_refined=True,
    )
    session.add_all([completed_story, dependent_story])
    session.flush()
    assert completed_story.story_id is not None
    assert dependent_story.story_id is not None

    session.add(
        UserStoryDependency(
            product_id=product.product_id,
            dependent_story_id=dependent_story.story_id,
            prerequisite_story_id=completed_story.story_id,
            status="active",
            source="manual_review",
            confidence="reviewed",
        )
    )
    sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Close story explicitly",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.ACTIVE,
    )
    session.add(sprint)
    session.flush()
    assert sprint.sprint_id is not None
    session.add_all(
        [
            SprintStory(sprint_id=sprint.sprint_id, story_id=completed_story.story_id),
            SprintStory(sprint_id=sprint.sprint_id, story_id=dependent_story.story_id),
            Task(
                story_id=completed_story.story_id,
                description="Implement required budget",
                status=TaskStatus.DONE,
                metadata_json=serialize_task_metadata(
                    TaskMetadata(checklist_items=["Budget is required"])
                ),
            ),
            Task(
                story_id=completed_story.story_id,
                description="Test required budget",
                status=TaskStatus.DONE,
                metadata_json=serialize_task_metadata(
                    TaskMetadata(checklist_items=["Missing budget fails"])
                ),
            ),
            Task(
                story_id=dependent_story.story_id,
                description="Implement live workflow",
                status=TaskStatus.TO_DO,
                metadata_json=serialize_task_metadata(
                    TaskMetadata(checklist_items=["Workflow uses budget gate"])
                ),
            ),
        ]
    )
    session.commit()

    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )

    before_next = runner.task_next(project_id=product.product_id)
    readiness = runner.story_readiness(
        project_id=product.product_id,
        story_id=completed_story.story_id,
    )
    close = runner.story_close(
        project_id=product.product_id,
        story_id=completed_story.story_id,
        expected_status="To Do",
        expected_story_fingerprint=readiness["data"]["story_fingerprint"],
        idempotency_key="close-story-budget-001",
        resolution="Completed",
        completion_notes="All budget tasks completed and validated.",
        evidence_links=["scripts/run_live_round.py"],
        changed_by="cli-agent",
    )
    replay = runner.story_close(
        project_id=product.product_id,
        story_id=completed_story.story_id,
        expected_status="To Do",
        expected_story_fingerprint=readiness["data"]["story_fingerprint"],
        idempotency_key="close-story-budget-001",
        resolution="Completed",
        completion_notes="All budget tasks completed and validated.",
        evidence_links=["scripts/run_live_round.py"],
        changed_by="cli-agent",
    )
    after_next = runner.task_next(project_id=product.product_id)

    assert before_next["data"]["task_ticket"] is None
    assert before_next["data"]["reason"] == "no_available_task"
    assert readiness["ok"] is True
    assert readiness["data"]["close_eligible"] is True
    assert readiness["data"]["current_status"] == "To Do"
    assert str(readiness["data"]["story_fingerprint"]).startswith(
        TASK_FINGERPRINT_PREFIX
    )
    assert close["ok"] is True
    assert close["data"]["idempotency"]["replayed"] is False
    assert close["data"]["current_status"] == "Done"
    assert close["data"]["resolution"] == StoryResolution.COMPLETED.value
    assert replay["ok"] is True
    assert replay["data"]["idempotency"]["replayed"] is True
    assert after_next["data"]["task_ticket"]["story"]["story_id"] == (
        dependent_story.story_id
    )

    session.expire_all()
    persisted_story = session.get(UserStory, completed_story.story_id)
    assert persisted_story is not None
    assert persisted_story.status == StoryStatus.DONE
    completion_log = session.exec(select(StoryCompletionLog)).first()
    assert completion_log is not None
    assert completion_log.changed_by == "cli-agent"


def test_sprint_story_close_rejects_stale_story_fingerprint(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story close should fail when task state changes after readiness."""
    product = Product(name="Stale Story Close Product")
    team = Team(name="Stale Story Close Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    story = UserStory(
        product_id=product.product_id,
        title="Close stale story",
        story_points=1,
        rank="101",
        status=StoryStatus.TO_DO,
        is_refined=True,
    )
    session.add(story)
    session.flush()
    assert story.story_id is not None
    sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Reject stale close",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.ACTIVE,
    )
    session.add(sprint)
    session.flush()
    assert sprint.sprint_id is not None
    session.add(SprintStory(sprint_id=sprint.sprint_id, story_id=story.story_id))
    task = Task(
        story_id=story.story_id,
        description="Finish implementation",
        status=TaskStatus.DONE,
        metadata_json=serialize_task_metadata(
            TaskMetadata(checklist_items=["Implementation completed"])
        ),
    )
    session.add(task)
    session.commit()
    assert task.task_id is not None

    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )
    readiness = runner.story_readiness(
        project_id=product.product_id,
        story_id=story.story_id,
    )
    stale_fingerprint = str(readiness["data"]["story_fingerprint"])

    task.status = TaskStatus.IN_PROGRESS
    session.add(task)
    session.commit()

    result = runner.story_close(
        project_id=product.product_id,
        story_id=story.story_id,
        expected_status="To Do",
        expected_story_fingerprint=stale_fingerprint,
        idempotency_key="close-stale-story-001",
        resolution="Completed",
        completion_notes="This should not close.",
        evidence_links=["stale.py"],
        changed_by="cli-agent",
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "MUTATION_FAILED"
    assert result["errors"][0]["details"]["reason_code"] == (
        "SPRINT_STORY_FINGERPRINT_STALE"
    )
    persisted_story = session.get(UserStory, story.story_id)
    assert persisted_story is not None
    assert persisted_story.status == StoryStatus.TO_DO


def test_sprint_close_marks_sprint_completed_and_updates_workflow(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint close should snapshot a fully completed active sprint."""
    product = Product(name="Sprint Close Product")
    team = Team(name="Sprint Close Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    story = UserStory(
        product_id=product.product_id,
        title="Closed story",
        story_points=1,
        rank="101",
        status=StoryStatus.DONE,
        is_refined=True,
    )
    session.add(story)
    session.flush()
    assert story.story_id is not None

    sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Close completed sprint",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.ACTIVE,
    )
    session.add(sprint)
    session.flush()
    assert sprint.sprint_id is not None
    session.add_all(
        [
            SprintStory(sprint_id=sprint.sprint_id, story_id=story.story_id),
            Task(
                story_id=story.story_id,
                description="Ship final task",
                status=TaskStatus.DONE,
                metadata_json=serialize_task_metadata(
                    TaskMetadata(checklist_items=["Final task done"])
                ),
            ),
        ]
    )
    session.commit()

    workflow = _FakeWorkflowService()
    workflow.state = {"fsm_state": "SPRINT_VIEW"}
    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", workflow),
    )

    readiness = runner.close_readiness(project_id=product.product_id)
    close = runner.close(
        project_id=product.product_id,
        expected_state="SPRINT_VIEW",
        expected_status="Active",
        expected_sprint_fingerprint=readiness["data"]["sprint_fingerprint"],
        idempotency_key="close-sprint-001",
        completion_notes="All committed stories completed.",
        follow_up_notes="Prepare the next sprint.",
        changed_by="cli-agent",
    )
    replay = runner.close(
        project_id=product.product_id,
        expected_state="SPRINT_VIEW",
        expected_status="Active",
        expected_sprint_fingerprint=readiness["data"]["sprint_fingerprint"],
        idempotency_key="close-sprint-001",
        completion_notes="All committed stories completed.",
        follow_up_notes="Prepare the next sprint.",
        changed_by="cli-agent",
    )

    assert readiness["ok"] is True
    assert readiness["data"]["close_eligible"] is True
    assert str(readiness["data"]["sprint_fingerprint"]).startswith(
        TASK_FINGERPRINT_PREFIX
    )
    assert readiness["data"]["guards"] == {
        "expected_state": "SPRINT_VIEW",
        "expected_status": "Active",
        "expected_sprint_fingerprint": readiness["data"]["sprint_fingerprint"],
    }
    assert close["ok"] is True
    assert close["data"]["current_status"] == SprintStatus.COMPLETED.value
    assert close["data"]["fsm_state"] == "SPRINT_COMPLETE"
    assert close["data"]["idempotency"]["replayed"] is False
    assert replay["ok"] is True
    assert replay["data"]["idempotency"]["replayed"] is True
    assert workflow.state["fsm_state"] == "SPRINT_COMPLETE"

    session.expire_all()
    persisted_sprint = session.get(Sprint, sprint.sprint_id)
    assert persisted_sprint is not None
    assert persisted_sprint.status == SprintStatus.COMPLETED
    assert persisted_sprint.completed_at is not None
    assert persisted_sprint.close_snapshot_json is not None
    completion_event = session.exec(
        select(WorkflowEvent).where(
            WorkflowEvent.event_type == WorkflowEventType.SPRINT_COMPLETED
        )
    ).first()
    assert completion_event is not None
    assert completion_event.product_id == product.product_id


def test_sprint_close_rejects_stale_sprint_fingerprint(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint close should fail when task state changes after readiness."""
    product = Product(name="Stale Sprint Close Product")
    team = Team(name="Stale Sprint Close Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    story = UserStory(
        product_id=product.product_id,
        title="Stale sprint story",
        story_points=1,
        rank="101",
        status=StoryStatus.DONE,
        is_refined=True,
    )
    session.add(story)
    session.flush()
    assert story.story_id is not None

    sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Reject stale close",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.ACTIVE,
    )
    task = Task(
        story_id=story.story_id,
        description="Mutable task",
        status=TaskStatus.DONE,
        metadata_json=serialize_task_metadata(
            TaskMetadata(checklist_items=["Mutable task done"])
        ),
    )
    session.add(sprint)
    session.flush()
    assert sprint.sprint_id is not None
    session.add_all(
        [
            SprintStory(sprint_id=sprint.sprint_id, story_id=story.story_id),
            task,
        ]
    )
    session.commit()
    assert task.task_id is not None

    workflow = _FakeWorkflowService()
    workflow.state = {"fsm_state": "SPRINT_VIEW"}
    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", workflow),
    )
    readiness = runner.close_readiness(project_id=product.product_id)
    task.status = TaskStatus.CANCELLED
    session.add(task)
    session.commit()

    result = runner.close(
        project_id=product.product_id,
        expected_state="SPRINT_VIEW",
        expected_status="Active",
        expected_sprint_fingerprint=readiness["data"]["sprint_fingerprint"],
        idempotency_key="close-sprint-stale-001",
        completion_notes="Trying stale close.",
        changed_by="cli-agent",
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "MUTATION_FAILED"
    assert result["errors"][0]["details"]["reason_code"] == ("SPRINT_FINGERPRINT_STALE")
