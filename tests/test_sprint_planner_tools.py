"""Tests for sprint planner persistence tool."""

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import ValidationError
from sqlmodel import Session, col, select

from agile_sqlmodel import (
    Product,
    Sprint,
    SprintStatus,
    SprintStory,
    Task,
    UserStory,
    WorkflowEvent,
    WorkflowEventType,
)
from models.core import Team
from orchestrator_agent.agent_tools.sprint_planner_tool.tools import (
    SaveSprintPlanInput,
    save_sprint_plan_tool,
)
from tests.typing_helpers import require_id
from tools.orchestrator_tools import fetch_sprint_candidates
from utils.spec_schemas import ValidationEvidence
from utils.task_metadata import TaskMetadata, serialize_task_metadata

if TYPE_CHECKING:
    from google.adk.tools import ToolContext


def _task_metadata(task: Task) -> TaskMetadata:
    assert task.metadata_json is not None
    return TaskMetadata.model_validate(json.loads(task.metadata_json))


def _seed_product_team_stories(session: Session) -> tuple[int, int, list[int]]:
    product = Product(name="Test Product", vision="Vision", description="Desc")
    team = Team(name="Team Alpha")
    session.add(product)
    session.add(team)
    session.commit()
    session.refresh(product)
    session.refresh(team)

    assert product.product_id is not None
    assert team.team_id is not None

    stories: list[int] = []
    for idx in range(2):
        story = UserStory(
            product_id=product.product_id,
            title=f"Story {idx + 1}",
            story_description="As a user, I want...",
            acceptance_criteria="- AC",
            validation_evidence=ValidationEvidence(
                spec_version_id=1,
                validated_at=datetime.now(UTC),
                passed=True,
                rules_checked=["SPEC_VERSION_EXISTS"],
                invariants_checked=[],
                evaluated_invariant_ids=["INV-VALID"] if idx == 0 else [],
                finding_invariant_ids=[],
                failures=[],
                warnings=[],
                alignment_warnings=[],
                alignment_failures=[],
                validator_version="1.0.0",
                input_hash="hash",
            ).model_dump_json(),
        )
        session.add(story)
        session.flush()
        assert story.story_id is not None
        stories.append(story.story_id)

    session.commit()
    return product.product_id, team.team_id, stories


def test_save_sprint_plan_input_enforces_duration_bounds() -> None:
    """Sprint persistence input must reject durations outside the documented range."""
    with pytest.raises(ValidationError):
        SaveSprintPlanInput(
            product_id=1,
            team_id=1,
            sprint_start_date="2026-02-01",
            sprint_duration_days=0,
        )

    with pytest.raises(ValidationError):
        SaveSprintPlanInput(
            product_id=1,
            team_id=1,
            sprint_start_date="2026-02-01",
            sprint_duration_days=32,
        )


def _build_sprint_plan(story_ids: list[int]) -> dict[str, Any]:
    return {
        "sprint_goal": "Deliver authentication essentials",
        "sprint_number": 1,
        "duration_days": 14,
        "selected_stories": [
            {
                "story_id": story_ids[0],
                "story_title": "Story 1",
                "tasks": [
                    {
                        "description": "Create auth table",
                        "task_kind": "implementation",
                        "checklist_items": [
                            "Define the auth table columns",
                            "Add persistence coverage for auth records",
                        ],
                        "artifact_targets": ["auth schema"],
                        "workstream_tags": ["backend", "auth"],
                        "relevant_invariant_ids": ["INV-VALID"],
                    },
                    {
                        "description": "Add login UI",
                        "task_kind": "implementation",
                        "checklist_items": [
                            "Render the login form locally",
                            "Wire submit handling to the login flow",
                        ],
                        "artifact_targets": ["login form"],
                        "workstream_tags": ["frontend", "auth"],
                        "relevant_invariant_ids": [],
                    },
                ],
                "reason_for_selection": "Core to sprint goal",
            }
        ],
        "deselected_stories": [
            {"story_id": story_ids[1], "reason": "Does not fit capacity"}
        ],
        "capacity_analysis": {
            "velocity_assumption": "Medium",
            "capacity_band": "4-5 stories",
            "selected_count": 1,
            "story_points_used": 3,
            "max_story_points": 10,
            "commitment_note": "Does this scope feel achievable in 2 weeks?",
            "reasoning": "Scope fits capacity band.",
        },
    }


def test_save_sprint_plan_creates_records(session: Session) -> None:
    """Ensure sprint plan persistence creates sprint, links, and tasks."""
    product_id, team_id, story_ids = _seed_product_team_stories(session)

    tool_context = cast(
        "ToolContext",
        SimpleNamespace(state={"sprint_plan": _build_sprint_plan(story_ids)}),
    )
    input_data = SaveSprintPlanInput(
        product_id=product_id,
        team_id=team_id,
        sprint_start_date="2026-02-01",
        sprint_duration_days=14,
    )

    result = save_sprint_plan_tool(input_data, tool_context)
    assert result["success"] is True

    sprint = session.exec(select(Sprint)).first()
    assert sprint is not None
    assert sprint.goal == "Deliver authentication essentials"
    assert sprint.started_at is None

    links = session.exec(select(SprintStory)).all()
    assert len(links) == 1

    tasks = session.exec(select(Task)).all()
    assert len(tasks) == 2  # noqa: PLR2004
    metadata_by_description = {task.description: _task_metadata(task) for task in tasks}
    assert metadata_by_description["Create auth table"].task_kind == "implementation"
    assert metadata_by_description["Create auth table"].artifact_targets == [
        "auth schema"
    ]
    assert metadata_by_description["Create auth table"].checklist_items == [
        "Define the auth table columns",
        "Add persistence coverage for auth records",
    ]


def test_save_sprint_plan_uses_orchestrator_duration_when_valid(
    session: Session,
) -> None:
    """Persisted workflow event must keep a valid orchestrator-provided duration."""
    product_id, team_id, story_ids = _seed_product_team_stories(session)

    tool_context = cast(
        "ToolContext",
        SimpleNamespace(
            state={
                "sprint_plan": _build_sprint_plan(story_ids),
                "sprint_planning_duration": 12.75,
            }
        ),
    )
    input_data = SaveSprintPlanInput(
        product_id=product_id,
        team_id=team_id,
        sprint_start_date="2026-02-01",
        sprint_duration_days=14,
    )

    result = save_sprint_plan_tool(input_data, tool_context)
    assert result["success"] is True

    event = session.exec(
        select(WorkflowEvent).where(
            WorkflowEvent.event_type == WorkflowEventType.SPRINT_PLAN_SAVED
        )
    ).first()
    assert event is not None
    assert event.duration_seconds == 12.75  # noqa: PLR2004


def test_save_sprint_plan_falls_back_to_elapsed_duration(session: Session) -> None:
    """Persisted workflow event must include numeric duration when state key is missing."""  # noqa: E501
    product_id, team_id, story_ids = _seed_product_team_stories(session)

    tool_context = cast(
        "ToolContext",
        SimpleNamespace(state={"sprint_plan": _build_sprint_plan(story_ids)}),
    )
    input_data = SaveSprintPlanInput(
        product_id=product_id,
        team_id=team_id,
        sprint_start_date="2026-02-01",
        sprint_duration_days=14,
    )

    result = save_sprint_plan_tool(input_data, tool_context)
    assert result["success"] is True

    event = session.exec(
        select(WorkflowEvent).where(
            WorkflowEvent.event_type == WorkflowEventType.SPRINT_PLAN_SAVED
        )
    ).first()
    assert event is not None
    assert event.duration_seconds is not None
    assert isinstance(event.duration_seconds, float)
    assert event.duration_seconds >= 0.0


def test_save_sprint_plan_falls_back_when_state_duration_invalid(
    session: Session,
) -> None:
    """Invalid state duration must not propagate as NULL to workflow event."""
    product_id, team_id, story_ids = _seed_product_team_stories(session)

    tool_context = cast(
        "ToolContext",
        SimpleNamespace(
            state={
                "sprint_plan": _build_sprint_plan(story_ids),
                "sprint_planning_duration": "not-a-number",
            }
        ),
    )
    input_data = SaveSprintPlanInput(
        product_id=product_id,
        team_id=team_id,
        sprint_start_date="2026-02-01",
        sprint_duration_days=14,
    )

    result = save_sprint_plan_tool(input_data, tool_context)
    assert result["success"] is True

    event = session.exec(
        select(WorkflowEvent).where(
            WorkflowEvent.event_type == WorkflowEventType.SPRINT_PLAN_SAVED
        )
    ).first()
    assert event is not None
    assert event.duration_seconds is not None
    assert isinstance(event.duration_seconds, float)
    assert event.duration_seconds >= 0.0


def test_save_sprint_plan_rejects_story_conflict(session: Session) -> None:
    """Ensure a story cannot be assigned to another open sprint."""
    product_id, team_id, story_ids = _seed_product_team_stories(session)

    existing_sprint = Sprint(
        goal="Existing sprint",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 14),
        status=SprintStatus.ACTIVE,
        product_id=product_id,
        team_id=team_id,
    )
    session.add(existing_sprint)
    session.flush()
    assert existing_sprint.sprint_id is not None
    session.add(SprintStory(sprint_id=existing_sprint.sprint_id, story_id=story_ids[0]))
    session.commit()

    tool_context = cast(
        "ToolContext",
        SimpleNamespace(state={"sprint_plan": _build_sprint_plan(story_ids)}),
    )
    input_data = SaveSprintPlanInput(
        product_id=product_id,
        team_id=team_id,
        sprint_start_date="2026-02-01",
        sprint_duration_days=14,
    )

    result = save_sprint_plan_tool(input_data, tool_context)
    assert result["success"] is False
    assert "Stories already assigned" in result["error"]


def test_save_sprint_plan_updates_existing_planned_sprint_in_place(
    session: Session,
) -> None:
    """Saving a revised draft should reuse the open planned sprint for the product."""
    product_id, team_id, story_ids = _seed_product_team_stories(session)

    existing_sprint = Sprint(
        goal="Initial sprint goal",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 14),
        status=SprintStatus.PLANNED,
        product_id=product_id,
        team_id=team_id,
    )
    session.add(existing_sprint)
    session.flush()
    assert existing_sprint.sprint_id is not None

    session.add(SprintStory(sprint_id=existing_sprint.sprint_id, story_id=story_ids[0]))
    session.add(
        Task(
            story_id=story_ids[0],
            description="Create auth table",
            metadata_json=serialize_task_metadata(
                TaskMetadata(
                    task_kind="implementation",
                    artifact_targets=["auth schema"],
                    workstream_tags=["backend", "auth"],
                    relevant_invariant_ids=["INV-VALID"],
                    checklist_items=[
                        "Define the auth table columns",
                        "Add persistence coverage for auth records",
                    ],
                )
            ),
        )
    )
    session.commit()

    sprint_plan = _build_sprint_plan(story_ids)
    sprint_plan["sprint_goal"] = "Updated sprint goal"
    sprint_plan["selected_stories"].append(
        {
            "story_id": story_ids[1],
            "story_title": "Story 2",
            "tasks": [
                {
                    "description": "Add audit logging",
                    "task_kind": "implementation",
                    "checklist_items": [
                        "Emit audit events for auth updates",
                        "Cover the new audit trail with tests",
                    ],
                    "artifact_targets": ["audit logging"],
                    "workstream_tags": ["backend"],
                    "relevant_invariant_ids": [],
                }
            ],
            "reason_for_selection": "Still fits the revised sprint scope",
        }
    )
    sprint_plan["deselected_stories"] = []
    sprint_plan["capacity_analysis"]["selected_count"] = 2
    sprint_plan["capacity_analysis"]["story_points_used"] = 5

    tool_context = cast(
        "ToolContext",
        SimpleNamespace(state={"sprint_plan": sprint_plan}),
    )
    input_data = SaveSprintPlanInput(
        product_id=product_id,
        team_id=team_id,
        sprint_start_date="2026-02-01",
        sprint_duration_days=10,
    )

    result = save_sprint_plan_tool(input_data, tool_context)

    assert result["success"] is True
    assert result["sprint_id"] == existing_sprint.sprint_id

    sprints = session.exec(select(Sprint)).all()
    assert len(sprints) == 1
    session.refresh(existing_sprint)
    assert existing_sprint.goal == "Updated sprint goal"
    assert existing_sprint.start_date == date(2026, 2, 1)
    assert existing_sprint.end_date == date(2026, 2, 11)

    links = session.exec(
        select(SprintStory).where(SprintStory.sprint_id == existing_sprint.sprint_id)
    ).all()
    assert sorted(link.story_id for link in links) == sorted(story_ids)

    tasks = session.exec(
        select(Task).order_by(col(Task.story_id), col(Task.description))
    ).all()
    assert len(tasks) == 3  # noqa: PLR2004
    assert sum(1 for task in tasks if task.description == "Create auth table") == 1
    assert sorted(task.description for task in tasks) == [
        "Add audit logging",
        "Add login UI",
        "Create auth table",
    ]


def test_save_sprint_plan_handles_large_task_deletion_volume(session: Session) -> None:
    """Large volumes of task deletions should be chunked safely for SQLite."""
    product_id, team_id, story_ids = _seed_product_team_stories(session)

    existing_sprint = Sprint(
        goal="Initial sprint goal",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 14),
        status=SprintStatus.PLANNED,
        product_id=product_id,
        team_id=team_id,
    )
    session.add(existing_sprint)
    session.flush()
    assert existing_sprint.sprint_id is not None

    session.add(SprintStory(sprint_id=existing_sprint.sprint_id, story_id=story_ids[0]))

    # Add 501 tasks to trigger chunking logic safely past the 500 limit
    bulk_tasks = []
    for _ in range(501):
        bulk_tasks.append(  # noqa: PERF401
            Task(
                story_id=story_ids[0],
                description="Obsolete task description",
                metadata_json=serialize_task_metadata(TaskMetadata()),
            )
        )
    session.add_all(bulk_tasks)
    session.commit()

    sprint_plan = _build_sprint_plan(story_ids)
    sprint_plan["selected_stories"][0]["tasks"] = [
        {
            "description": "Retained task description",
            "task_kind": "implementation",
            "checklist_items": ["A valid checklist item"],
            "artifact_targets": ["Some valid target"],
            "workstream_tags": ["backend"],
            "relevant_invariant_ids": [],
        }
    ]

    tool_context = cast(
        "ToolContext",
        SimpleNamespace(state={"sprint_plan": sprint_plan}),
    )
    input_data = SaveSprintPlanInput(
        product_id=product_id,
        team_id=team_id,
        sprint_start_date="2026-02-01",
        sprint_duration_days=14,
    )

    result = save_sprint_plan_tool(input_data, tool_context)
    assert result["success"] is True

    story_tasks = session.exec(select(Task).where(Task.story_id == story_ids[0])).all()
    assert len(story_tasks) == 1
    assert story_tasks[0].description == "Retained task description"


def test_save_sprint_plan_reconciles_selected_story_tasks_on_planned_update(
    session: Session,
) -> None:
    """Selected-story tasks should exactly match the revised planned sprint."""
    product_id, team_id, story_ids = _seed_product_team_stories(session)

    existing_sprint = Sprint(
        goal="Initial sprint goal",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 14),
        status=SprintStatus.PLANNED,
        product_id=product_id,
        team_id=team_id,
    )
    session.add(existing_sprint)
    session.flush()
    assert existing_sprint.sprint_id is not None

    session.add(SprintStory(sprint_id=existing_sprint.sprint_id, story_id=story_ids[0]))
    session.add_all(
        [
            Task(
                story_id=story_ids[0],
                description="Create auth table",
                metadata_json=serialize_task_metadata(
                    TaskMetadata(
                        task_kind="documentation",
                        artifact_targets=["old auth schema"],
                        workstream_tags=["legacy"],
                        relevant_invariant_ids=[],
                        checklist_items=["Obsolete checklist"],
                    )
                ),
            ),
            Task(
                story_id=story_ids[0],
                description="Create auth table",
                metadata_json=serialize_task_metadata(
                    TaskMetadata(
                        task_kind="analysis",
                        artifact_targets=["duplicate schema"],
                        workstream_tags=["legacy"],
                        relevant_invariant_ids=[],
                        checklist_items=["Duplicate checklist"],
                    )
                ),
            ),
            Task(
                story_id=story_ids[0],
                description="Remove legacy auth path",
                metadata_json=serialize_task_metadata(
                    TaskMetadata(
                        task_kind="implementation",
                        artifact_targets=["legacy auth path"],
                        workstream_tags=["backend"],
                        relevant_invariant_ids=[],
                        checklist_items=["Delete dead code"],
                    )
                ),
            ),
        ]
    )
    session.commit()

    sprint_plan = _build_sprint_plan(story_ids)
    sprint_plan["selected_stories"][0]["tasks"] = [
        {
            "description": "Create auth table",
            "task_kind": "implementation",
            "checklist_items": [
                "Define the auth table columns",
                "Add persistence coverage for auth records",
            ],
            "artifact_targets": ["auth schema"],
            "workstream_tags": ["backend", "auth"],
            "relevant_invariant_ids": ["INV-VALID"],
        }
    ]

    tool_context = cast(
        "ToolContext",
        SimpleNamespace(state={"sprint_plan": sprint_plan}),
    )
    input_data = SaveSprintPlanInput(
        product_id=product_id,
        team_id=team_id,
        sprint_start_date="2026-02-01",
        sprint_duration_days=14,
    )

    result = save_sprint_plan_tool(input_data, tool_context)

    assert result["success"] is True

    story_tasks = session.exec(
        select(Task).where(Task.story_id == story_ids[0]).order_by(col(Task.task_id))
    ).all()
    assert len(story_tasks) == 1
    assert story_tasks[0].description == "Create auth table"
    metadata = _task_metadata(story_tasks[0])
    assert metadata.task_kind == "implementation"
    assert metadata.artifact_targets == ["auth schema"]
    assert metadata.workstream_tags == ["backend", "auth"]
    assert metadata.relevant_invariant_ids == ["INV-VALID"]
    assert metadata.checklist_items == [
        "Define the auth table columns",
        "Add persistence coverage for auth records",
    ]


def test_fetch_sprint_candidates_excludes_stories_in_open_sprints(
    session: Session,
) -> None:
    """Only stories tied to open planned/active sprints should be excluded."""
    product = Product(name="Candidate Product", vision="Vision", description="Desc")
    team = Team(name="Candidate Team")
    session.add(product)
    session.add(team)
    session.commit()
    session.refresh(product)
    session.refresh(team)

    assert product.product_id is not None
    assert team.team_id is not None

    stories = {
        "planned": UserStory(
            product_id=product.product_id,
            title="Planned story",
            story_description="Assigned to a planned sprint",
            acceptance_criteria="- AC",
            is_refined=True,
            rank="1",
        ),
        "active": UserStory(
            product_id=product.product_id,
            title="Active story",
            story_description="Assigned to an active sprint",
            acceptance_criteria="- AC",
            is_refined=True,
            rank="2",
        ),
        "completed_only": UserStory(
            product_id=product.product_id,
            title="Completed-only story",
            story_description="Assigned only to a completed sprint",
            acceptance_criteria="- AC",
            is_refined=True,
            rank="3",
        ),
        "eligible": UserStory(
            product_id=product.product_id,
            title="Eligible story",
            story_description="Not assigned to any open sprint",
            acceptance_criteria="- AC",
            is_refined=True,
            rank="4",
        ),
        "non_refined": UserStory(
            product_id=product.product_id,
            title="Needs refinement",
            story_description="Still rough",
            acceptance_criteria="- AC",
            is_refined=False,
            rank="5",
        ),
        "superseded": UserStory(
            product_id=product.product_id,
            title="Superseded story",
            story_description="Replaced by another story",
            acceptance_criteria="- AC",
            is_refined=True,
            is_superseded=True,
            rank="6",
        ),
    }
    session.add_all(stories.values())
    session.flush()

    planned_sprint = Sprint(
        goal="Planned",
        start_date=date(2026, 2, 1),
        end_date=date(2026, 2, 14),
        status=SprintStatus.PLANNED,
        product_id=product.product_id,
        team_id=team.team_id,
    )
    active_sprint = Sprint(
        goal="Active",
        start_date=date(2026, 2, 15),
        end_date=date(2026, 2, 28),
        status=SprintStatus.ACTIVE,
        product_id=product.product_id,
        team_id=team.team_id,
    )
    completed_sprint = Sprint(
        goal="Completed",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 14),
        status=SprintStatus.COMPLETED,
        product_id=product.product_id,
        team_id=team.team_id,
    )
    session.add_all([planned_sprint, active_sprint, completed_sprint])
    session.flush()

    session.add_all(
        [
            SprintStory(
                sprint_id=require_id(planned_sprint.sprint_id, "planned_sprint_id"),
                story_id=require_id(stories["planned"].story_id, "planned_story_id"),
            ),
            SprintStory(
                sprint_id=require_id(active_sprint.sprint_id, "active_sprint_id"),
                story_id=require_id(stories["active"].story_id, "active_story_id"),
            ),
            SprintStory(
                sprint_id=require_id(
                    completed_sprint.sprint_id,
                    "completed_sprint_id",
                ),
                story_id=require_id(
                    stories["completed_only"].story_id,
                    "completed_story_id",
                ),
            ),
        ]
    )
    session.commit()

    result = fetch_sprint_candidates(product.product_id)

    assert result["success"] is True
    assert [story["story_title"] for story in result["stories"]] == [
        "Completed-only story",
        "Eligible story",
    ]
    assert result["excluded_counts"] == {
        "non_refined": 1,
        "superseded": 1,
        "open_sprint": 2,
    }


def test_fetch_sprint_candidates_reports_blocked_readiness_for_unsized_rows(
    session: Session,
) -> None:
    """Candidate diagnostics should block planning for legacy unsized rows."""
    product = Product(name="Test Product", vision="Vision", description="Desc")
    session.add(product)
    session.commit()
    session.refresh(product)

    unsized = UserStory(
        product_id=product.product_id,
        title="Unsized story",
        story_description="As a user, I want a thing, so that I get value.",
        acceptance_criteria="- Verify behavior.",
        story_origin="refined",
        is_refined=True,
        is_superseded=False,
        story_points=None,
        rank=None,
    )
    sized = UserStory(
        product_id=product.product_id,
        title="Sized story",
        story_description="As a user, I want another thing, so that I get value.",
        acceptance_criteria="- Verify behavior.",
        story_origin="refined",
        is_refined=True,
        is_superseded=False,
        story_points=3,
        rank="101",
    )
    session.add_all([unsized, sized])
    session.commit()
    session.refresh(unsized)

    result = fetch_sprint_candidates(product.product_id)

    assert result["success"] is True
    assert result["readiness"] == {
        "status": "blocked",
        "unsized_count": 1,
        "default_priority_count": 1,
        "blocking_codes": [
            "SPRINT_CANDIDATES_UNSIZED",
            "SPRINT_CANDIDATES_DEFAULT_PRIORITY",
        ],
        "blocking_story_ids": [unsized.story_id],
        "dependency_issue_count": 0,
        "dependency_cycle_paths": [],
    }


def test_save_sprint_plan_rejects_out_of_scope_task_invariants(
    session: Session,
) -> None:
    """Verify save sprint plan rejects out of scope task invariants."""
    product_id, team_id, story_ids = _seed_product_team_stories(session)
    sprint_plan = _build_sprint_plan(story_ids)
    sprint_plan["selected_stories"][0]["tasks"][0]["relevant_invariant_ids"] = [
        "INV-UNKNOWN"
    ]

    tool_context = cast(
        "ToolContext",
        SimpleNamespace(state={"sprint_plan": sprint_plan}),
    )
    input_data = SaveSprintPlanInput(
        product_id=product_id,
        team_id=team_id,
        sprint_start_date="2026-02-01",
        sprint_duration_days=14,
    )

    result = save_sprint_plan_tool(input_data, tool_context)

    assert result["success"] is False
    assert "invalid invariant IDs" in result["error"]


def test_save_sprint_plan_rejects_checklist_items_copied_from_story_acceptance_criteria(
    session: Session,
) -> None:
    """Verify save sprint plan rejects checklist items copied from story acceptance criteria."""  # noqa: E501
    product = Product(name="Checklist Product", vision="Vision", description="Desc")
    team = Team(name="Checklist Team")
    session.add(product)
    session.add(team)
    session.commit()
    session.refresh(product)
    session.refresh(team)

    assert product.product_id is not None
    assert team.team_id is not None

    story = UserStory(
        product_id=product.product_id,
        title="Story with AC",
        story_description="As a user, I want...",
        acceptance_criteria="1. Persist the event\n2. Surface a success response",
        validation_evidence=ValidationEvidence(
            spec_version_id=1,
            validated_at=datetime.now(UTC),
            passed=True,
            rules_checked=["SPEC_VERSION_EXISTS"],
            invariants_checked=[],
            evaluated_invariant_ids=["INV-VALID"],
            finding_invariant_ids=[],
            failures=[],
            warnings=[],
            alignment_warnings=[],
            alignment_failures=[],
            validator_version="1.0.0",
            input_hash="hash",
        ).model_dump_json(),
    )
    session.add(story)
    session.commit()
    session.refresh(story)

    assert story.story_id is not None

    sprint_plan = {
        "sprint_goal": "Deliver authentication essentials",
        "sprint_number": 1,
        "duration_days": 14,
        "selected_stories": [
            {
                "story_id": story.story_id,
                "story_title": "Story with AC",
                "tasks": [
                    {
                        "description": "Create auth table",
                        "task_kind": "implementation",
                        "checklist_items": [
                            "Persist the event",
                            "Add persistence coverage for auth records",
                        ],
                        "artifact_targets": ["auth schema"],
                        "workstream_tags": ["backend", "auth"],
                        "relevant_invariant_ids": ["INV-VALID"],
                    }
                ],
                "reason_for_selection": "Core to sprint goal",
            }
        ],
        "deselected_stories": [],
        "capacity_analysis": {
            "velocity_assumption": "Medium",
            "capacity_band": "4-5 stories",
            "selected_count": 1,
            "story_points_used": 3,
            "max_story_points": 10,
            "commitment_note": "Does this scope feel achievable in 2 weeks?",
            "reasoning": "Scope fits capacity band.",
        },
    }

    tool_context = cast(
        "ToolContext",
        SimpleNamespace(state={"sprint_plan": sprint_plan}),
    )
    input_data = SaveSprintPlanInput(
        product_id=product.product_id,
        team_id=team.team_id,
        sprint_start_date="2026-02-01",
        sprint_duration_days=14,
    )

    result = save_sprint_plan_tool(input_data, tool_context)

    assert result["success"] is False
    assert "duplicates story acceptance criteria" in result["error"]
