"""Tests for agent workbench Sprint phase runner."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
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
from services.agent_workbench.application import AgentWorkbenchApplication
from services.agent_workbench.post_sprint_triage import build_triage_payload
from services.agent_workbench.sprint_phase import SprintPhaseRunner
from services.phases import sprint_service
from services.sprint_input import load_sprint_candidates
from utils.task_metadata import TaskMetadata, serialize_task_metadata

if TYPE_CHECKING:
    import pytest

JsonDict = dict[str, Any]
MIDDLE_STORY_EXECUTION_ORDER = 2
DEPENDENT_STORY_EXECUTION_ORDER = 3
TASK_FINGERPRINT_PREFIX = "sha256:"
AMENDED_SPEC_VERSION_ID = 12


class _FakeProductRepository:
    def get_by_id(self, product_id: int) -> object:
        """Return a lightweight product sentinel."""
        return SimpleNamespace(product_id=product_id, name="Product")


class _MissingProductRepository:
    def get_by_id(self, _product_id: int) -> None:
        """Return no project to exercise the existing load error envelope."""


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
        lambda _project_id, **_kwargs: {
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

    result = runner.generate(project_id=7, max_story_points=9)

    assert result["ok"] is True
    assert result["data"]["fsm_state"] == "SPRINT_DRAFT"
    assert result["data"]["attempt_id"] == "sprint-attempt-1"


def test_scope_extension_sprint_candidates_keep_only_extension_stories() -> None:
    """Extension Story completion excludes legacy candidates from Sprint planning."""

    def fake_fetch_candidates(*, product_id: int) -> JsonDict:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "stories": [
                {
                    "story_id": 1,
                    "story_title": "Legacy shared requirement",
                    "source_requirement": "Shared requirement",
                    "story_origin": "refined",
                    "accepted_spec_version_id": 7,
                    "story_points": 2,
                    "priority": 101,
                },
                {
                    "story_id": 2,
                    "story_title": "Amended spec requirement",
                    "source_requirement": "Shared requirement",
                    "story_origin": "refined",
                    "accepted_spec_version_id": AMENDED_SPEC_VERSION_ID,
                    "story_points": 3,
                    "priority": 201,
                },
                {
                    "story_id": 3,
                    "story_title": "Missing spec extension requirement",
                    "source_requirement": "Shared requirement",
                    "story_origin": "scope_extension",
                    "accepted_spec_version_id": None,
                    "story_points": 5,
                    "priority": 202,
                },
                {
                    "story_id": 4,
                    "story_title": "Wrong spec extension requirement",
                    "source_requirement": "Shared requirement",
                    "story_origin": "scope_extension",
                    "accepted_spec_version_id": 9,
                    "story_points": 5,
                    "priority": 203,
                },
            ],
        }

    result = load_sprint_candidates(
        7,
        fetch_candidates=fake_fetch_candidates,
        story_completion_scope={
            "scope": "milestone",
            "scope_id": "milestone_1",
            "requirements": ["Shared requirement"],
            "extension_scope": True,
            "accepted_spec_version_id": AMENDED_SPEC_VERSION_ID,
        },
    )

    assert result["count"] == 1
    assert [story["story_id"] for story in result["stories"]] == [2]
    assert result["excluded_counts"]["story_completion_scope"] == 3  # noqa: PLR2004
    assert result["story_completion_scope"]["extension_scope"] is True
    assert (
        result["story_completion_scope"]["accepted_spec_version_id"]
        == AMENDED_SPEC_VERSION_ID
    )


def test_sprint_runner_generate_normalizes_invalid_model_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model-response Sprint failures should be retryable structured errors."""

    async def fake_run_sprint_agent(_state: object, **_kwargs: object) -> JsonDict:
        return {
            "success": False,
            "input_context": {"available_stories": []},
            "output_artifact": {
                "error": "SPRINT_GENERATION_FAILED",
                "message": "Sprint response is not valid JSON",
                "is_complete": False,
            },
            "is_complete": None,
            "error": "Sprint response is not valid JSON",
            "failure_artifact_id": "sprint-failure-001",
            "failure_stage": "invalid_json",
            "failure_summary": "Sprint response is not valid JSON",
            "raw_output_preview": "",
            "has_full_artifact": True,
        }

    monkeypatch.setattr(
        sprint_phase_module,
        "run_sprint_agent_from_state",
        fake_run_sprint_agent,
    )
    monkeypatch.setattr(
        sprint_service,
        "load_sprint_candidates",
        lambda _project_id, **_kwargs: {
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

    result = runner.generate(project_id=7, max_story_points=9)

    assert result["ok"] is False
    error = result["errors"][0]
    assert error["code"] == "SPRINT_GENERATION_MODEL_RESPONSE_INVALID"
    assert error["message"] == "Sprint response is not valid JSON"
    assert error["details"]["failure_stage"] == "invalid_json"
    assert error["details"]["failure_artifact_id"] == "sprint-failure-001"
    assert error["details"]["attempt_id"] == "sprint-attempt-1"
    assert error["details"]["attempt_count"] == 1
    assert error["details"]["attempt_persisted"] is True
    assert error["details"]["fsm_state"] == "SPRINT_SETUP"
    assert (
        error["details"]["safe_retry_command"]
        == "agileforge sprint generate --project-id 7"
    )
    assert "Retry agileforge sprint generate" in error["remediation"][-1]


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
        lambda _project_id, **_kwargs: {
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

    result = runner.generate(project_id=7, max_story_points=9)

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


def test_sprint_runner_generate_rejects_non_positive_metric_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint generate must not call planner with unusable metric capacity."""
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

    async def fake_metrics(_self: object, _project_id: int) -> JsonDict:
        return {
            "status": "success",
            "data": {
                "recommendation": {
                    "recommended_next_sprint_points": 0,
                    "basis": "last_3_completed_sprints_average",
                }
            },
        }

    monkeypatch.setattr(
        sprint_phase_module,
        "run_sprint_agent_from_state",
        fake_run_sprint_agent,
    )
    monkeypatch.setattr(SprintPhaseRunner, "_metrics", fake_metrics)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )

    result = runner.generate(project_id=7)

    assert result["ok"] is False
    assert result["data"] is None
    assert result["errors"][0]["code"] == "INVALID_COMMAND"
    assert result["errors"][0]["details"]["error_code"] == "SPRINT_CAPACITY_REQUIRED"
    assert "positive" in result["errors"][0]["message"]
    assert captured["agent_calls"] == 0


def test_sprint_runner_generate_blocks_active_reset_stale_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint generation remains blocked by active-reset stale markers."""
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
        lambda _project_id, **_kwargs: {
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
            "stale_backlog_reason": "active_backlog_reset",
            "stale_since_backlog_attempt_id": "backlog-attempt-12",
            "active_backlog_reset_attempt_id": "backlog-attempt-12",
        }
    )
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", workflow_service),
    )

    result = runner.generate(project_id=7, max_story_points=9)

    assert result["ok"] is False
    assert result["data"] is None
    assert result["warnings"] == []
    assert result["errors"][0]["code"] == "INVALID_COMMAND"
    assert "downstream backlog is stale" in result["errors"][0]["message"]
    assert "active_backlog_reset" in result["errors"][0]["message"]
    assert "backlog-attempt-12" in result["errors"][0]["message"]
    assert captured["agent_calls"] == 0
    assert workflow_service.state["downstream_backlog_stale"] is True
    assert workflow_service.state["stale_backlog_reason"] == "active_backlog_reset"
    assert workflow_service.state["stale_since_backlog_attempt_id"] == (
        "backlog-attempt-12"
    )


def test_sprint_runner_generate_blocks_sprint_complete_without_impact_none_triage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Block SPRINT_COMPLETE generation when current triage is absent or non-none."""
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
        lambda _project_id, **_kwargs: {
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

    for triage in (
        None,
        build_triage_payload(
            project_id=7,
            sprint_id=13,
            impact="story",
            affected_requirements=["Story follow-up"],
            affected_task_ids=None,
            affected_story_ids=None,
            affected_backlog_item_ids=None,
            affected_roadmap_item_ids=None,
            affected_layers=None,
            learning_summary="Story-level follow-up is needed.",
            decision_reason="A completed task exposed missing story-level detail.",
            idempotency_key="triage-story-for-generate-guard",
            replace_existing=False,
            recorded_at="2026-06-10T00:00:00Z",
            recorded_by="cli-agent",
        ),
    ):
        workflow_service = _FakeWorkflowService()
        workflow_service.state = {
            "fsm_state": "SPRINT_COMPLETE",
            "latest_completed_sprint_id": 13,
        }
        if triage is not None:
            workflow_service.state["post_sprint_triage"] = triage
        runner = SprintPhaseRunner(
            product_repo=cast("Any", _FakeProductRepository()),
            workflow_service=cast("Any", workflow_service),
        )

        result = runner.generate(project_id=7, max_story_points=9)

        assert result["ok"] is False
        assert result["data"] is None
        assert result["errors"][0]["code"] == "INVALID_COMMAND"

    assert captured["agent_calls"] == 0


def test_sprint_runner_generate_allows_sprint_complete_with_impact_none_triage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allow SPRINT_COMPLETE generation only after current impact=none triage."""
    captured: JsonDict = {"agent_calls": 0}

    async def fake_run_sprint_agent(_state: object, **_kwargs: object) -> JsonDict:
        captured["agent_calls"] += 1
        return {
            "success": True,
            "input_context": {"available_stories": [{"story_id": 1}]},
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
        lambda _project_id, **_kwargs: {
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
    workflow_service.state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": 13,
        "post_sprint_triage": build_triage_payload(
            project_id=7,
            sprint_id=13,
            impact="none",
            affected_requirements=None,
            affected_task_ids=None,
            affected_story_ids=None,
            affected_backlog_item_ids=None,
            affected_roadmap_item_ids=None,
            affected_layers=None,
            learning_summary="No follow-up changes are needed.",
            decision_reason="Sprint outcomes matched the current backlog.",
            idempotency_key="triage-none-for-generate-bridge",
            replace_existing=False,
            recorded_at="2026-06-10T00:00:00Z",
            recorded_by="cli-agent",
        ),
    }
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", workflow_service),
    )

    result = runner.generate(project_id=7, max_story_points=9)

    assert result["ok"] is True
    assert result["data"]["fsm_state"] == "SPRINT_DRAFT"
    assert result["data"]["attempt_id"] == "sprint-attempt-1"
    assert captured["agent_calls"] == 1


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
    metadata = json.loads(event.event_metadata or "{}")
    assert metadata["planned_start_date"] == "2026-05-26"
    assert metadata["planned_end_date"] == "2026-06-09"


def test_sprint_runner_start_logs_null_planned_dates(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint runner should log dateless capacity-planned starts as JSON null."""
    product = Product(name="Dateless Runner Product")
    team = Team(name="Dateless Runner Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Deliver dateless workflow",
        start_date=None,
        end_date=None,
        status=SprintStatus.PLANNED,
    )
    session.add(sprint)
    session.commit()
    assert sprint.sprint_id is not None

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
        idempotency_key="start-dateless-runner-sprint-001",
    )

    assert started["ok"] is True
    event = session.exec(
        select(WorkflowEvent).where(
            WorkflowEvent.event_type == WorkflowEventType.SPRINT_STARTED
        )
    ).one()
    metadata = json.loads(event.event_metadata or "{}")
    assert metadata["planned_start_date"] is None
    assert metadata["planned_end_date"] is None


def test_sprint_status_without_active_sprint_guides_to_completed_id(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status without --sprint-id should point to completed Sprint history."""
    product = Product(name="Completed Status Product")
    team = Team(name="Completed Status Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    completed_sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Already completed",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.COMPLETED,
    )
    session.add(completed_sprint)
    session.commit()
    assert completed_sprint.sprint_id is not None

    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )

    result = runner.status(project_id=product.product_id)

    assert result["ok"] is False
    error = result["errors"][0]
    assert error["code"] == "INVALID_COMMAND"
    assert error["message"] == "No active or planned Sprint found."
    assert error["details"] == {
        "project_id": product.product_id,
        "latest_completed_sprint_id": completed_sprint.sprint_id,
    }
    assert error["remediation"] == [
        (
            f"Run agileforge sprint status --project-id {product.product_id} "
            f"--sprint-id {completed_sprint.sprint_id} to inspect the latest "
            "completed Sprint."
        )
    ]


def test_sprint_runner_history_includes_execution_summary(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint history should include durable execution records for CLI agents."""
    expected_elapsed_seconds = 600
    expected_story_points = 8
    product = Product(name="Execution History Product")
    team = Team(name="Execution History Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    story = UserStory(
        product_id=product.product_id,
        title="Completed execution story",
        story_description="As a user, I need execution history.",
        acceptance_criteria="- Done",
        status=StoryStatus.DONE,
        story_points=expected_story_points,
    )
    session.add(story)
    session.flush()
    assert story.story_id is not None

    sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Expose execution history",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 15),
        started_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        completed_at=datetime(2026, 6, 1, 12, 10, tzinfo=UTC),
        status=SprintStatus.COMPLETED,
    )
    session.add(sprint)
    session.flush()
    assert sprint.sprint_id is not None
    session.add(SprintStory(sprint_id=sprint.sprint_id, story_id=story.story_id))
    session.add_all(
        [
            Task(
                story_id=story.story_id,
                description="Completed task",
                status=TaskStatus.DONE,
            ),
            Task(
                story_id=story.story_id,
                description="Open task",
                status=TaskStatus.TO_DO,
            ),
        ]
    )
    session.commit()

    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    workflow_service = _FakeWorkflowService()
    workflow_service.state["sprint_attempts"] = [{"attempt_id": "sprint-attempt-1"}]
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", workflow_service),
    )

    result = runner.history(project_id=product.product_id)

    assert result["ok"] is True
    payload = result["data"]
    assert payload["count"] == 1
    assert payload["items"] == payload["attempt_items"]
    assert payload["attempt_count"] == 1
    assert payload["execution_count"] == 1
    execution = payload["execution_items"][0]
    assert execution["sprint_id"] == sprint.sprint_id
    assert execution["status"] == "Completed"
    assert execution["story_count"] == 1
    assert execution["completed_story_count"] == 1
    assert execution["task_count"] == 2  # noqa: PLR2004
    assert execution["completed_task_count"] == 1
    assert execution["story_points_total"] == expected_story_points
    assert execution["story_points_done"] == expected_story_points
    assert execution["elapsed_seconds"] == expected_elapsed_seconds
    assert execution["history_fidelity"] == "derived"


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


def test_sprint_task_next_stops_dependency_blockers_at_completed_story(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task next should not inherit blockers through a completed prerequisite."""
    product = Product(name="Completed Chain Product")
    team = Team(name="Completed Chain Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    stale_duplicate = UserStory(
        product_id=product.product_id,
        title="Duplicate integration story",
        story_points=2,
        rank="101",
        status=StoryStatus.TO_DO,
        is_refined=True,
    )
    completed_prerequisite = UserStory(
        product_id=product.product_id,
        title="Completed prerequisite",
        story_points=2,
        rank="102",
        status=StoryStatus.DONE,
        is_refined=True,
    )
    runnable_story = UserStory(
        product_id=product.product_id,
        title="Runnable downstream story",
        story_points=3,
        rank="103",
        status=StoryStatus.TO_DO,
        is_refined=True,
    )
    session.add_all([stale_duplicate, completed_prerequisite, runnable_story])
    session.flush()
    assert stale_duplicate.story_id is not None
    assert completed_prerequisite.story_id is not None
    assert runnable_story.story_id is not None

    session.add_all(
        [
            UserStoryDependency(
                product_id=product.product_id,
                dependent_story_id=completed_prerequisite.story_id,
                prerequisite_story_id=stale_duplicate.story_id,
                status="active",
                source="manual_review",
                confidence="reviewed",
            ),
            UserStoryDependency(
                product_id=product.product_id,
                dependent_story_id=runnable_story.story_id,
                prerequisite_story_id=completed_prerequisite.story_id,
                status="active",
                source="manual_review",
                confidence="reviewed",
            ),
        ]
    )
    sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Continue after completed prerequisite",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.ACTIVE,
    )
    session.add(sprint)
    session.flush()
    assert sprint.sprint_id is not None
    session.add(
        SprintStory(sprint_id=sprint.sprint_id, story_id=runnable_story.story_id)
    )
    task = Task(story_id=runnable_story.story_id, description="Run downstream work")
    session.add(task)
    session.commit()
    assert task.task_id is not None

    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )

    tasks = runner.tasks(project_id=product.product_id)
    next_ticket = runner.task_next(project_id=product.product_id)

    task_row = tasks["data"]["tasks"][0]
    assert task_row["story_id"] == runnable_story.story_id
    assert task_row["direct_blocked_by_story_ids"] == [completed_prerequisite.story_id]
    assert task_row["blocked_by_story_ids"] == []
    assert task_row["is_blocked"] is False
    assert tasks["data"]["dependency_summary"]["blocked_story_count"] == 0
    assert next_ticket["data"]["task_ticket"]["task"]["task_id"] == task.task_id
    assert next_ticket["data"]["reason"] == "next_unblocked_todo"


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
    update_command = ticket["next_actions"]["update"]
    assert update_command == (
        f"agileforge sprint task update --project-id {product.product_id} "
        f"--task-id {first.task_id} --status Done "
        '--expected-status "In Progress" '
        f"--expected-task-fingerprint {ticket['guards']['expected_task_fingerprint']} "
        "--idempotency-key <idempotency_key> "
        '--outcome-summary "<outcome_summary>" '
        '--validation-summary "<validation_summary>" '
        "--checklist-result fully_met "
        "--artifact-ref <artifact_ref>"
    )


def test_sprint_task_next_omits_artifact_ref_when_no_targets(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task next update command should omit artifact refs without targets."""
    product = Product(name="No Artifact Product")
    team = Team(name="No Artifact Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    story = UserStory(
        product_id=product.product_id,
        title="Finish non-artifact work",
        story_description="As an operator, I complete checklist-only work.",
        acceptance_criteria="- Checklist is fully met",
        story_points=1,
        rank="102",
        status=StoryStatus.IN_PROGRESS,
        is_refined=True,
    )
    session.add(story)
    session.flush()
    assert story.story_id is not None

    sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Close checklist-only task",
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
        description="Validate checklist-only completion",
        status=TaskStatus.IN_PROGRESS,
        metadata_json=serialize_task_metadata(
            TaskMetadata(
                task_kind="testing",
                checklist_items=["Checklist-only work is done"],
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

    result = runner.task_next(project_id=product.product_id)

    assert result["ok"] is True
    ticket = result["data"]["task_ticket"]
    assert ticket["task"]["task_id"] == task.task_id
    update_command = ticket["next_actions"]["update"]
    assert update_command == (
        f"agileforge sprint task update --project-id {product.product_id} "
        f"--task-id {task.task_id} --status Done "
        '--expected-status "In Progress" '
        f"--expected-task-fingerprint {ticket['guards']['expected_task_fingerprint']} "
        "--idempotency-key <idempotency_key> "
        '--outcome-summary "<outcome_summary>" '
        '--validation-summary "<validation_summary>" '
        "--checklist-result fully_met"
    )
    assert "--artifact-ref" not in update_command


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


def _seed_active_task_for_evidence_tests(
    session: Session,
    *,
    artifact_targets: list[str] | None = None,
) -> tuple[Product, Task, str]:
    product = Product(name="Evidence Contract Product")
    team = Team(name="Evidence Contract Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    story = UserStory(
        product_id=product.product_id,
        title="Close sprint task with evidence",
        story_description="As an agent, I close a task with the required evidence.",
        acceptance_criteria="- Done closure includes required evidence",
        story_points=1,
        rank="111",
        status=StoryStatus.IN_PROGRESS,
        is_refined=True,
    )
    session.add(story)
    session.flush()
    assert story.story_id is not None

    sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Verify Done evidence contract",
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
        description="Close task with structured evidence",
        status=TaskStatus.IN_PROGRESS,
        metadata_json=serialize_task_metadata(
            TaskMetadata(
                task_kind="testing",
                artifact_targets=list(artifact_targets or []),
                checklist_items=["Evidence contract is satisfied"],
            )
        ),
    )
    session.add(task)
    session.commit()
    assert task.task_id is not None

    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )
    show = runner.task_show(project_id=product.product_id, task_id=task.task_id)
    fingerprint = show["data"]["task_ticket"]["guards"]["expected_task_fingerprint"]
    return product, task, str(fingerprint)


def test_sprint_task_update_rejects_done_without_outcome_summary(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Done updates require a nonblank outcome summary."""
    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    product, task, fingerprint = _seed_active_task_for_evidence_tests(session)
    assert product.product_id is not None
    assert task.task_id is not None
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )

    result = runner.task_update(
        project_id=product.product_id,
        task_id=task.task_id,
        status="Done",
        expected_status="In Progress",
        expected_task_fingerprint=fingerprint,
        idempotency_key="close-task-missing-outcome-001",
        checklist_result="fully_met",
        validation_summary="uv run pytest tests/test_contract.py -q",
        changed_by="cli-agent",
    )

    assert result["ok"] is False
    details = result["errors"][0]["details"]
    assert details["reason_code"] == "TASK_CLOSE_EVIDENCE_REQUIRED"
    assert details["missing_fields"] == ["outcome_summary"]


def test_sprint_task_update_rejects_done_without_checklist_result(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Done updates require an explicit checklist result."""
    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    product, task, fingerprint = _seed_active_task_for_evidence_tests(session)
    assert product.product_id is not None
    assert task.task_id is not None
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )

    result = runner.task_update(
        project_id=product.product_id,
        task_id=task.task_id,
        status="Done",
        expected_status="In Progress",
        expected_task_fingerprint=fingerprint,
        idempotency_key="close-task-missing-checklist-001",
        outcome_summary="Closed the task with complete implementation evidence.",
        validation_summary="uv run pytest tests/test_contract.py -q",
        changed_by="cli-agent",
    )

    assert result["ok"] is False
    details = result["errors"][0]["details"]
    assert details["reason_code"] == "TASK_CLOSE_EVIDENCE_REQUIRED"
    assert details["missing_fields"] == ["checklist_result"]


def test_sprint_task_update_rejects_done_with_not_checked_result(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Done updates reject the not_checked checklist result."""
    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    product, task, fingerprint = _seed_active_task_for_evidence_tests(session)
    assert product.product_id is not None
    assert task.task_id is not None
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )

    result = runner.task_update(
        project_id=product.product_id,
        task_id=task.task_id,
        status="Done",
        expected_status="In Progress",
        expected_task_fingerprint=fingerprint,
        idempotency_key="close-task-not-checked-001",
        outcome_summary="Closed the task with complete implementation evidence.",
        checklist_result="not_checked",
        validation_summary="uv run pytest tests/test_contract.py -q",
        changed_by="cli-agent",
    )

    assert result["ok"] is False
    details = result["errors"][0]["details"]
    assert details["reason_code"] == "TASK_CLOSE_EVIDENCE_REQUIRED"
    assert details["missing_fields"] == ["checklist_result"]


def test_sprint_task_update_done_accepts_complete_evidence_without_artifact_targets(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Done updates accept complete close evidence when no artifacts are targeted."""
    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    product, task, fingerprint = _seed_active_task_for_evidence_tests(session)
    assert product.product_id is not None
    assert task.task_id is not None
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )

    result = runner.task_update(
        project_id=product.product_id,
        task_id=task.task_id,
        status="Done",
        expected_status="In Progress",
        expected_task_fingerprint=fingerprint,
        idempotency_key="close-task-complete-evidence-001",
        outcome_summary="Closed the task with complete implementation evidence.",
        checklist_result="fully_met",
        validation_summary="uv run pytest tests/test_contract.py -q",
        changed_by="cli-agent",
    )

    assert result["ok"] is True
    assert result["data"]["execution"]["current_status"] == "Done"


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
    assert close["data"]["ineligible_reason"] is None
    assert replay["ok"] is True
    assert replay["data"]["idempotency"]["replayed"] is True
    assert replay["data"]["ineligible_reason"] is None
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
    assert close["data"]["ineligible_reason"] is None
    assert replay["ok"] is True
    assert replay["data"]["idempotency"]["replayed"] is True
    assert replay["data"]["ineligible_reason"] is None
    assert workflow.state["fsm_state"] == "SPRINT_COMPLETE"
    assert workflow.state["active_sprint_id"] is None
    assert workflow.state["latest_completed_sprint_id"] == sprint.sprint_id
    assert workflow.state["sprint_completed_at"] is not None

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


def test_sprint_runner_metrics_projects_completed_sprints_and_events(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint metrics should read durable completed Sprints and events."""
    product = Product(name="Metrics Product")
    team = Team(name="Metrics Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    done_story = UserStory(
        product_id=product.product_id,
        title="Complete the durable path",
        story_description="As an agent, I finish the durable path.",
        acceptance_criteria="- Metrics are source backed",
        story_points=5,
        rank="101",
        status=StoryStatus.DONE,
        is_refined=True,
    )
    accepted_story = UserStory(
        product_id=product.product_id,
        title="Accept the projected path",
        story_description="As a reviewer, I accept the projection.",
        acceptance_criteria="- Counts accepted stories",
        story_points=3,
        rank="102",
        status=StoryStatus.ACCEPTED,
        is_refined=True,
    )
    open_story = UserStory(
        product_id=product.product_id,
        title="Leave future capacity out",
        story_description="As an agent, I leave future work open.",
        acceptance_criteria="- Open work is not completed",
        story_points=13,
        rank="103",
        status=StoryStatus.TO_DO,
        is_refined=True,
    )
    session.add_all([done_story, accepted_story, open_story])
    session.flush()
    assert done_story.story_id is not None
    assert accepted_story.story_id is not None
    assert open_story.story_id is not None

    completed_sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Measure completed work",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 14),
        status=SprintStatus.COMPLETED,
        started_at=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        completed_at=datetime(2026, 6, 1, 11, 0, tzinfo=UTC),
    )
    active_sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Active work should not count",
        start_date=date(2026, 6, 15),
        end_date=date(2026, 6, 28),
        status=SprintStatus.ACTIVE,
    )
    session.add_all([completed_sprint, active_sprint])
    session.flush()
    assert completed_sprint.sprint_id is not None
    assert active_sprint.sprint_id is not None

    session.add_all(
        [
            SprintStory(
                sprint_id=completed_sprint.sprint_id,
                story_id=done_story.story_id,
            ),
            SprintStory(
                sprint_id=completed_sprint.sprint_id,
                story_id=accepted_story.story_id,
            ),
            SprintStory(
                sprint_id=completed_sprint.sprint_id,
                story_id=open_story.story_id,
            ),
        ]
    )
    session.add_all(
        [
            Task(
                story_id=done_story.story_id,
                description="Build durable metrics",
                status=TaskStatus.DONE,
            ),
            Task(
                story_id=done_story.story_id,
                description="Polish durable metrics",
                status=TaskStatus.TO_DO,
            ),
            Task(
                story_id=accepted_story.story_id,
                description="Review durable metrics",
                status=TaskStatus.DONE,
            ),
            Task(
                story_id=open_story.story_id,
                description="Plan next capacity",
                status=TaskStatus.TO_DO,
            ),
            WorkflowEvent(
                event_type=WorkflowEventType.SPRINT_STARTED,
                product_id=product.product_id,
                sprint_id=completed_sprint.sprint_id,
                duration_seconds=12.0,
                turn_count=2,
            ),
            WorkflowEvent(
                event_type=WorkflowEventType.SPRINT_COMPLETED,
                product_id=product.product_id,
                sprint_id=completed_sprint.sprint_id,
                duration_seconds=18.0,
                turn_count=3,
            ),
            WorkflowEvent(
                event_type=WorkflowEventType.SPRINT_PLAN_REVIEW,
                product_id=product.product_id,
                sprint_id=completed_sprint.sprint_id,
                duration_seconds=4.0,
                turn_count=None,
            ),
            WorkflowEvent(
                event_type=WorkflowEventType.SPRINT_STARTED,
                product_id=product.product_id,
                sprint_id=active_sprint.sprint_id,
                duration_seconds=99.0,
                turn_count=9,
            ),
        ]
    )
    session.commit()

    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )

    result = runner.metrics(project_id=product.product_id)

    assert result["ok"] is True
    assert result["warnings"] == []
    data = result["data"]
    assert data["project_id"] == product.product_id
    assert data["status"] == "ready"
    expected_summary = {
        "completed_sprint_count": 1,
        "completed_story_count": 2,
        "completed_task_count": 2,
        "completed_story_points": 8,
        "total_elapsed_seconds": 7200,
    }
    assert {
        key: data["summary"][key] for key in expected_summary
    } == expected_summary
    assert data["recommendation"]["source_sprint_ids"] == [
        completed_sprint.sprint_id
    ]
    assert data["data_quality_warnings"] == []

    row = data["completed_sprints"][0]
    expected_row = {
        "sprint_id": completed_sprint.sprint_id,
        "goal": "Measure completed work",
        "status": "Completed",
        "started_at": "2026-06-01T09:00:00",
        "completed_at": "2026-06-01T11:00:00",
        "start_date": "2026-06-01",
        "end_date": "2026-06-14",
        "story_count": 3,
        "completed_story_count": 2,
        "task_count": 4,
        "completed_task_count": 2,
        "story_points_planned": 21,
        "story_points_completed": 8,
        "elapsed_seconds": 7200,
        "workflow_event_count": 3,
        "workflow_event_duration_seconds": 34,
        "turn_count": 5,
        "history_fidelity": "derived",
    }
    assert {key: row[key] for key in expected_row} == expected_row


def test_sprint_runner_metrics_missing_project_uses_load_error() -> None:
    """Sprint metrics should preserve the existing missing-project envelope."""
    missing_project_id = 404
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _MissingProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )

    result = runner.metrics(project_id=missing_project_id)

    assert result["ok"] is False
    assert result["data"] is None
    assert result["warnings"] == []
    assert result["errors"][0]["code"] == "PROJECT_NOT_FOUND"
    assert result["errors"][0]["details"] == {"project_id": missing_project_id}


def test_application_sprint_metrics_forwards_to_runner() -> None:
    """The application facade should expose Sprint metrics."""

    class _MetricsRunner:
        def __init__(self) -> None:
            self.project_ids: list[int] = []

        def metrics(self, *, project_id: int) -> dict[str, Any]:
            self.project_ids.append(project_id)
            return {
                "ok": True,
                "data": {"project_id": project_id, "status": "ready"},
                "warnings": [],
                "errors": [],
            }

    project_id = 17
    metrics_runner = _MetricsRunner()
    application = AgentWorkbenchApplication(sprint_runner=cast("Any", metrics_runner))

    result = application.sprint_metrics(project_id=project_id)

    assert metrics_runner.project_ids == [project_id]
    assert result["ok"] is True
    assert result["data"] == {"project_id": project_id, "status": "ready"}


def test_sprint_review_returns_latest_completed_sprint_without_mutation(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint review should expose post-sprint context without changing state."""
    product = Product(name="Review Completed Product")
    team = Team(name="Review Completed Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    completed_sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Review latest completed sprint",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.COMPLETED,
    )
    session.add(completed_sprint)
    session.commit()
    assert completed_sprint.sprint_id is not None

    workflow = _FakeWorkflowService()
    workflow.state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": completed_sprint.sprint_id,
        "planned_sprint_id": 777,
        "backlog_stale": True,
    }
    original_state = dict(workflow.state)
    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", workflow),
    )

    result = runner.review(project_id=product.product_id)

    assert result["ok"] is True
    assert result["data"]["fsm_state"] == "SPRINT_COMPLETE"
    assert result["data"]["latest_completed_sprint_id"] == completed_sprint.sprint_id
    assert result["data"]["sprint_id"] == completed_sprint.sprint_id
    assert result["data"]["post_sprint_triage_required"] is True
    assert workflow.state == original_state


def test_sprint_review_scopes_triage_to_explicit_completed_sprint(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit old Sprint review should not borrow latest Sprint triage."""
    product = Product(name="Review Explicit Completed Product")
    team = Team(name="Review Explicit Completed Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    earlier_sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Review earlier completed sprint",
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 14),
        status=SprintStatus.COMPLETED,
    )
    latest_sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Review latest completed sprint",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.COMPLETED,
    )
    session.add_all([earlier_sprint, latest_sprint])
    session.commit()
    assert earlier_sprint.sprint_id is not None
    assert latest_sprint.sprint_id is not None

    workflow = _FakeWorkflowService()
    workflow.state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": latest_sprint.sprint_id,
    }
    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", workflow),
    )
    triage = runner.triage(
        project_id=product.product_id,
        expected_state="SPRINT_COMPLETE",
        impact="none",
        learning_summary="No follow-up changes are needed.",
        decision_reason="Latest sprint outcomes matched the current backlog.",
        idempotency_key="triage-latest-for-explicit-review-001",
        changed_by="cli-agent",
    )

    result = runner.review(
        project_id=product.product_id,
        sprint_id=earlier_sprint.sprint_id,
    )

    assert triage["ok"] is True
    assert triage["data"]["post_sprint_triage"]["sprint_id"] == latest_sprint.sprint_id
    assert result["ok"] is True
    assert result["data"]["sprint_id"] == earlier_sprint.sprint_id
    assert result["data"]["post_sprint_triage"] is None
    assert result["data"]["post_sprint_triage_required"] is False


def test_sprint_triage_records_metadata_without_changing_fsm_state(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint triage should record durable metadata and keep SPRINT_COMPLETE."""
    product = Product(name="Triage Metadata Product")
    team = Team(name="Triage Metadata Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    completed_sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Record post-sprint triage",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.COMPLETED,
    )
    session.add(completed_sprint)
    session.commit()
    assert completed_sprint.sprint_id is not None

    planned_sprint_id = 888
    workflow = _FakeWorkflowService()
    workflow.state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": completed_sprint.sprint_id,
        "planned_sprint_id": planned_sprint_id,
    }
    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", workflow),
    )

    result = runner.triage(
        project_id=product.product_id,
        expected_state="SPRINT_COMPLETE",
        impact="none",
        learning_summary="No follow-up changes are needed.",
        decision_reason="Sprint outcomes matched the current backlog.",
        idempotency_key="triage-record-001",
        changed_by="cli-agent",
    )

    assert result["ok"] is True
    assert result["data"]["fsm_state"] == "SPRINT_COMPLETE"
    assert workflow.state["fsm_state"] == "SPRINT_COMPLETE"
    assert workflow.state["planned_sprint_id"] == planned_sprint_id
    assert workflow.state["post_sprint_triage"]["impact"] == "none"
    assert (
        workflow.state["post_sprint_triage_history"][-1]["history_action"]
        == "recorded"
    )
    triage_event = session.exec(
        select(WorkflowEvent).where(
            WorkflowEvent.event_type == WorkflowEventType.POST_SPRINT_TRIAGE_RECORDED
        )
    ).first()
    assert triage_event is not None
    assert triage_event.product_id == product.product_id
    assert triage_event.sprint_id == completed_sprint.sprint_id
    event_metadata = json.loads(triage_event.event_metadata or "{}")
    assert event_metadata["history_action"] == "recorded"
    assert event_metadata["replace_existing"] is False
    assert (
        event_metadata["triage_fingerprint"]
        == result["data"]["post_sprint_triage"]["triage_fingerprint"]
    )


def test_sprint_triage_guarded_correction_supersedes_previous_payload(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint triage correction should replace current payload under guard."""
    product = Product(name="Triage Correction Product")
    team = Team(name="Triage Correction Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    completed_sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Correct post-sprint triage",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.COMPLETED,
    )
    session.add(completed_sprint)
    session.commit()
    assert completed_sprint.sprint_id is not None

    workflow = _FakeWorkflowService()
    workflow.state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": completed_sprint.sprint_id,
    }
    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", workflow),
    )

    first = runner.triage(
        project_id=product.product_id,
        expected_state="SPRINT_COMPLETE",
        impact="none",
        learning_summary="No follow-up changes are needed.",
        decision_reason="Sprint outcomes matched the current backlog.",
        idempotency_key="triage-correction-001",
        changed_by="cli-agent",
    )
    first_fingerprint = first["data"]["post_sprint_triage"]["triage_fingerprint"]
    first_request_fingerprint = first["data"]["post_sprint_triage"][
        "request_fingerprint"
    ]
    second = runner.triage(
        project_id=product.product_id,
        expected_state="SPRINT_COMPLETE",
        impact="story",
        affected_story_ids=[42],
        learning_summary="One story needs acceptance criteria follow-up.",
        decision_reason="A completed task exposed missing story-level detail.",
        idempotency_key="triage-correction-002",
        replace_existing=True,
        expected_triage_fingerprint=first_fingerprint,
        changed_by="cli-agent",
    )

    assert first["ok"] is True
    assert first["data"]["post_sprint_triage"]["impact"] == "none"
    assert second["ok"] is True
    assert second["data"]["fsm_state"] == "SPRINT_COMPLETE"
    assert second["data"]["post_sprint_triage"]["impact"] == "story"
    assert workflow.state["post_sprint_triage"]["impact"] == "story"
    assert [
        entry["history_action"]
        for entry in workflow.state["post_sprint_triage_history"]
    ] == ["recorded", "superseded", "corrected"]
    history_entries = workflow.state["post_sprint_triage_history"]
    superseded_entry = history_entries[1]
    corrected_entry = history_entries[2]
    assert (
        superseded_entry["superseded_by_request_fingerprint"]
        == second["data"]["post_sprint_triage"]["request_fingerprint"]
    )
    assert superseded_entry["superseded_at"] == superseded_entry[
        "history_recorded_at"
    ]
    assert corrected_entry["replaces_triage_fingerprint"] == first_fingerprint
    assert (
        corrected_entry["correction_of_request_fingerprint"]
        == first_request_fingerprint
    )
    triage_events = session.exec(
        select(WorkflowEvent)
        .where(
            WorkflowEvent.event_type == WorkflowEventType.POST_SPRINT_TRIAGE_RECORDED
        )
        .order_by(cast("Any", WorkflowEvent.event_id))
    ).all()
    expected_event_count = 2
    assert len(triage_events) == expected_event_count
    correction_metadata = json.loads(triage_events[-1].event_metadata or "{}")
    assert correction_metadata["history_action"] == "corrected"
    assert correction_metadata["superseded_triage_fingerprint"] == first_fingerprint
    assert (
        correction_metadata["triage_fingerprint"]
        == second["data"]["post_sprint_triage"]["triage_fingerprint"]
    )


def test_sprint_triage_detects_existing_triage_with_string_latest_sprint_id(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint triage should not miss current triage when state ids are strings."""
    product = Product(name="Triage String Latest Product")
    team = Team(name="Triage String Latest Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    completed_sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Detect existing triage with string latest id",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.COMPLETED,
    )
    session.add(completed_sprint)
    session.commit()
    assert completed_sprint.sprint_id is not None

    workflow = _FakeWorkflowService()
    workflow.state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": str(completed_sprint.sprint_id),
    }
    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", workflow),
    )

    first = runner.triage(
        project_id=product.product_id,
        expected_state="SPRINT_COMPLETE",
        impact="none",
        learning_summary="No follow-up changes are needed.",
        decision_reason="Sprint outcomes matched the current backlog.",
        idempotency_key="triage-string-latest-001",
        changed_by="cli-agent",
    )
    second = runner.triage(
        project_id=product.product_id,
        expected_state="SPRINT_COMPLETE",
        impact="none",
        learning_summary="No follow-up changes are still needed.",
        decision_reason="Trying to record an unguarded duplicate.",
        idempotency_key="triage-string-latest-002",
        changed_by="cli-agent",
    )

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["errors"][0]["code"] == "TRIAGE_ALREADY_RECORDED"


def test_sprint_triage_replays_normalized_equivalent_idempotency_request(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint triage idempotency should use normalized request semantics."""
    product = Product(name="Triage Normalized Replay Product")
    team = Team(name="Triage Normalized Replay Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    completed_sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Replay normalized triage request",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.COMPLETED,
    )
    session.add(completed_sprint)
    session.commit()
    assert completed_sprint.sprint_id is not None

    workflow = _FakeWorkflowService()
    workflow.state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": completed_sprint.sprint_id,
    }
    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", workflow),
    )

    first = runner.triage(
        project_id=product.product_id,
        expected_state="SPRINT_COMPLETE",
        impact="none",
        learning_summary="No follow-up changes are needed.",
        decision_reason="Sprint outcomes matched the current backlog.",
        idempotency_key="triage-normalized-replay-001",
        replace_existing=cast("Any", "false"),
        changed_by="cli-agent",
    )
    replay = runner.triage(
        project_id=product.product_id,
        expected_state="SPRINT_COMPLETE",
        impact="none",
        learning_summary="No follow-up changes are needed.",
        decision_reason="Sprint outcomes matched the current backlog.",
        idempotency_key="triage-normalized-replay-001",
        replace_existing=False,
        changed_by="cli-agent",
    )

    assert first["ok"] is True
    assert replay["ok"] is True
    assert replay["data"]["idempotency"]["replayed"] is True
    assert (
        replay["data"]["post_sprint_triage"]["request_fingerprint"]
        == first["data"]["post_sprint_triage"]["request_fingerprint"]
    )


def test_sprint_triage_replays_before_state_guards_when_workflow_moves(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint triage idempotency replay should run before current state guards."""
    product = Product(name="Triage Replay Before Guards Product")
    team = Team(name="Triage Replay Before Guards Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    completed_sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Replay triage before state guards",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.COMPLETED,
    )
    newer_completed_sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Newer completed sprint after replayed triage",
        start_date=date(2026, 6, 10),
        end_date=date(2026, 6, 24),
        status=SprintStatus.COMPLETED,
    )
    session.add_all([completed_sprint, newer_completed_sprint])
    session.commit()
    assert completed_sprint.sprint_id is not None
    assert newer_completed_sprint.sprint_id is not None

    workflow = _FakeWorkflowService()
    workflow.state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": completed_sprint.sprint_id,
    }
    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", workflow),
    )

    first = runner.triage(
        project_id=product.product_id,
        expected_state="SPRINT_COMPLETE",
        impact="none",
        learning_summary="No follow-up changes are needed.",
        decision_reason="Sprint outcomes matched the current backlog.",
        idempotency_key="triage-replay-before-guards-001",
        changed_by="cli-agent",
    )
    workflow.state["fsm_state"] = "SPRINT_VIEW"
    workflow.state["latest_completed_sprint_id"] = newer_completed_sprint.sprint_id
    replay = runner.triage(
        project_id=product.product_id,
        expected_state="SPRINT_COMPLETE",
        impact="none",
        learning_summary="No follow-up changes are needed.",
        decision_reason="Sprint outcomes matched the current backlog.",
        idempotency_key="triage-replay-before-guards-001",
        changed_by="cli-agent",
    )

    assert first["ok"] is True
    assert replay["ok"] is True
    assert replay["data"]["idempotency"]["replayed"] is True


def test_sprint_triage_reusing_key_with_changed_expected_fingerprint_is_rejected(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint triage idempotency should include correction guard inputs."""
    product = Product(name="Triage Changed Guard Product")
    team = Team(name="Triage Changed Guard Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    completed_sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Reject changed expected triage fingerprint",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.COMPLETED,
    )
    session.add(completed_sprint)
    session.commit()
    assert completed_sprint.sprint_id is not None

    workflow = _FakeWorkflowService()
    workflow.state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": completed_sprint.sprint_id,
    }
    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", workflow),
    )

    first = runner.triage(
        project_id=product.product_id,
        expected_state="SPRINT_COMPLETE",
        impact="none",
        learning_summary="No follow-up changes are needed.",
        decision_reason="Sprint outcomes matched the current backlog.",
        idempotency_key="triage-changed-guard-001",
        changed_by="cli-agent",
    )
    first_fingerprint = first["data"]["post_sprint_triage"]["triage_fingerprint"]
    correction = runner.triage(
        project_id=product.product_id,
        expected_state="SPRINT_COMPLETE",
        impact="story",
        affected_story_ids=[42],
        learning_summary="One story needs acceptance criteria follow-up.",
        decision_reason="A completed task exposed missing story-level detail.",
        idempotency_key="triage-changed-guard-002",
        replace_existing=True,
        expected_triage_fingerprint=first_fingerprint,
        changed_by="cli-agent",
    )
    changed_guard = runner.triage(
        project_id=product.product_id,
        expected_state="SPRINT_COMPLETE",
        impact="story",
        affected_story_ids=[42],
        learning_summary="One story needs acceptance criteria follow-up.",
        decision_reason="A completed task exposed missing story-level detail.",
        idempotency_key="triage-changed-guard-002",
        replace_existing=True,
        expected_triage_fingerprint="sha256:different",
        changed_by="cli-agent",
    )

    assert first["ok"] is True
    assert correction["ok"] is True
    assert changed_guard["ok"] is False
    assert changed_guard["errors"][0]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_sprint_triage_preserves_required_field_validation_code(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint triage should preserve Task 1 required-field validation codes."""
    product = Product(name="Triage Required Field Product")
    team = Team(name="Triage Required Field Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    completed_sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Validate required triage fields",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.COMPLETED,
    )
    session.add(completed_sprint)
    session.commit()
    assert completed_sprint.sprint_id is not None

    workflow = _FakeWorkflowService()
    workflow.state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": completed_sprint.sprint_id,
    }
    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", workflow),
    )

    result = runner.triage(
        project_id=product.product_id,
        expected_state="SPRINT_COMPLETE",
        impact="none",
        learning_summary=" ",
        decision_reason="Sprint outcomes matched the current backlog.",
        idempotency_key="triage-required-field-001",
        changed_by="cli-agent",
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "TRIAGE_REQUIRED_FIELD_MISSING"


def test_sprint_triage_preserves_field_invalid_validation_code(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint triage should preserve Task 1 invalid-field validation codes."""
    product = Product(name="Triage Invalid Field Product")
    team = Team(name="Triage Invalid Field Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    completed_sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Validate invalid triage fields",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.COMPLETED,
    )
    session.add(completed_sprint)
    session.commit()
    assert completed_sprint.sprint_id is not None

    workflow = _FakeWorkflowService()
    workflow.state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": completed_sprint.sprint_id,
    }
    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", workflow),
    )

    result = runner.triage(
        project_id=product.product_id,
        expected_state="SPRINT_COMPLETE",
        impact="none",
        learning_summary="No follow-up changes are needed.",
        decision_reason="Sprint outcomes matched the current backlog.",
        idempotency_key="triage-invalid-field-001",
        replace_existing=cast("Any", "maybe"),
        changed_by="cli-agent",
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "TRIAGE_FIELD_INVALID"


def test_sprint_triage_preserves_impact_fields_invalid_validation_code(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint triage should preserve Task 1 impact-field validation codes."""
    product = Product(name="Triage Impact Fields Product")
    team = Team(name="Triage Impact Fields Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    completed_sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Validate triage impact fields",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.COMPLETED,
    )
    session.add(completed_sprint)
    session.commit()
    assert completed_sprint.sprint_id is not None

    workflow = _FakeWorkflowService()
    workflow.state = {
        "fsm_state": "SPRINT_COMPLETE",
        "latest_completed_sprint_id": completed_sprint.sprint_id,
    }
    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", workflow),
    )

    result = runner.triage(
        project_id=product.product_id,
        expected_state="SPRINT_COMPLETE",
        impact="story",
        learning_summary="Story-level follow-up is needed.",
        decision_reason="A story-level decision needs structured affected fields.",
        idempotency_key="triage-impact-fields-invalid-001",
        changed_by="cli-agent",
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "TRIAGE_IMPACT_FIELDS_INVALID"
