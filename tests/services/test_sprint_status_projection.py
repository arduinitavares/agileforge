"""Lifecycle-aware additive Sprint-status projection coverage for issue #227."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, cast

from sqlmodel import Session, select

from models.core import Sprint, Task
from models.enums import SprintStatus
from models.workflow import SprintPlanArtifactDecision
from repositories.workflow import WorkflowFactRepository
from services.contracts.sprint import SprintPlannerOutput
from services.read_projections import DurableReadProjectionService
from tests.workflow.execution_fixtures import seed_started_execution
from tests.workflow.test_planning_transitions import (
    _domain,
    _guards,
    _record_and_accept_roadmap,
    _record_and_accept_story,
    _record_sprint_plan_draft,
    _seed_accepted_backlog,
)
from workflow.definitions.product_discovery import accepted_current_spec
from workflow.requests import DecideSprintPlan, RecordSprintPlan

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from workflow.contracts import JsonObject

_EXPECTED_STORY_POINTS = 3


def _seed_planned_sprint(engine: Engine) -> tuple[int, int, int, JsonObject]:
    project_id = _seed_accepted_backlog(engine)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(
        engine,
        domain,
        project_id,
    )
    plan_id, _candidate_fingerprint, plan, plan_fingerprint = (
        _record_sprint_plan_draft(
            engine,
            domain,
            project_id,
            story_id,
            team_name="Issue 227 status team",
            idempotency_key="issue-227-record-sprint-plan",
        )
    )
    accepted = domain.transition(
        DecideSprintPlan(
            **_guards(domain.position(project_id), "planning.sprint.review"),
            idempotency_key="issue-227-accept-sprint-plan",
            sprint_plan_artifact_id=plan_id,
            plan_fingerprint=plan_fingerprint,
            decision="accepted",
            rationale="Approved exact Sprint scope for issue 227.",
        )
    )
    assert accepted.ok is True
    sprint_id = cast("int", accepted.output["activated_sprint_id"])
    return project_id, sprint_id, story_id, plan


def test_sprint_status_adds_exact_accepted_plan_without_changing_old_fields(
    engine: Engine,
) -> None:
    """Expose one complete Planned summary while retaining the old response shape."""
    project_id, sprint_id, story_id, _plan = _seed_planned_sprint(engine)

    result = DurableReadProjectionService(engine=engine).sprint_status(
        project_id=project_id
    )

    assert result["ok"] is True
    data = cast("dict[str, object]", result["data"])
    sprint = cast("dict[str, object]", data["sprint"])
    assert sprint == {
        "sprint_id": sprint_id,
        "status": "planned",
        "completed_at": None,
    }
    assert data["start"] is None
    tasks = cast("list[object]", data["tasks"])
    assert len(tasks) == 1
    task = cast("dict[str, object]", tasks[0])
    assert str(task["fact_fingerprint"]).startswith("sha256:")
    assert data["review"] is None
    assert data["closure"] is None

    accepted_plan = cast("dict[str, object]", data["accepted_plan"])
    acceptance = cast("dict[str, object]", accepted_plan["acceptance"])
    assert accepted_plan["sprint_id"] == sprint_id
    assert accepted_plan["status"] == "planned"
    assert accepted_plan["goal"] == "Persist planning workflow facts."
    assert accepted_plan["total_points"] == _EXPECTED_STORY_POINTS
    assert accepted_plan["task_count"] == 1
    assert acceptance == {
        "rationale": "Approved exact Sprint scope for issue 227.",
        "reviewer": "operator@example.com",
        "decided_at": acceptance["decided_at"],
    }
    assert acceptance["decided_at"] is not None
    owner = cast("dict[str, object]", accepted_plan["owner"])
    assert owner["display_label"] == "Issue 227 status team"
    stories = cast("list[dict[str, object]]", accepted_plan["selected_stories"])
    assert stories == [
        {
            "story_id": story_id,
            "story_item_id": "US-0001",
            "title": "Story for Plan immutable work",
            "story_points": _EXPECTED_STORY_POINTS,
            "task_count": 1,
        }
    ]
    for key in (
        "sprint_plan_artifact_id",
        "sprint_plan_artifact_decision_id",
        "plan_fingerprint",
        "candidate_set_fingerprint",
        "task_content_fingerprint",
    ):
        assert accepted_plan[key]


def test_sprint_status_keeps_same_accepted_plan_visible_after_start(
    engine: Engine,
) -> None:
    """Prove Planned-to-Active continuity on the same authoritative read path."""
    project_id, sprint_id, story_id, _task_id = seed_started_execution(engine)

    result = DurableReadProjectionService(engine=engine).sprint_status(
        project_id=project_id
    )

    assert result["ok"] is True
    data = cast("dict[str, object]", result["data"])
    sprint = cast("dict[str, object]", data["sprint"])
    accepted_plan = cast("dict[str, object]", data["accepted_plan"])
    start = cast("dict[str, object]", data["start"])
    assert sprint["sprint_id"] == sprint_id
    assert sprint["status"] == "active"
    assert accepted_plan["sprint_id"] == sprint_id
    assert accepted_plan["status"] == "active"
    assert cast("list[dict[str, object]]", accepted_plan["selected_stories"])[0][
        "story_id"
    ] == story_id
    assert start["sprint_id"] == sprint_id
    assert start["sprint_plan_artifact_id"] == accepted_plan[
        "sprint_plan_artifact_id"
    ]
    assert start["plan_fingerprint"] == accepted_plan["plan_fingerprint"]
    assert start["task_content_fingerprint"] == accepted_plan[
        "task_content_fingerprint"
    ]


def test_sprint_status_selects_current_accepted_correction_for_same_sprint(
    engine: Engine,
) -> None:
    """Project corrected C, not superseded A, for the unchanged Planned Sprint."""
    project_id, sprint_id, _story_id, original_plan = _seed_planned_sprint(engine)
    domain = _domain(engine)
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
    specification = accepted_current_spec(snapshot)
    assert specification is not None
    replacement = deepcopy(original_plan)
    replacement["sprint_goal"] = "Corrected accepted Sprint goal."
    recorded = domain.transition(
        RecordSprintPlan(
            **_guards(domain.position(project_id), "planning.sprint.plan"),
            idempotency_key="issue-227-record-correction",
            team_name="Issue 227 status team",
            spec_version_id=specification.spec_version_id,
            spec_hash=specification.spec_hash,
            planner_output=SprintPlannerOutput.model_validate(replacement),
        )
    )
    assert recorded.ok is True
    replacement_id = cast("int", recorded.output["sprint_plan_artifact_id"])
    replacement_fingerprint = cast("str", recorded.output["plan_fingerprint"])
    accepted = domain.transition(
        DecideSprintPlan(
            **_guards(domain.position(project_id), "planning.sprint.review"),
            idempotency_key="issue-227-accept-correction",
            sprint_plan_artifact_id=replacement_id,
            plan_fingerprint=replacement_fingerprint,
            decision="accepted",
            rationale="Accepted the corrected Sprint plan.",
        )
    )
    assert accepted.ok is True
    assert accepted.output["activated_sprint_id"] == sprint_id

    result = DurableReadProjectionService(engine=engine).sprint_status(
        project_id=project_id
    )

    assert result["ok"] is True
    data = cast("dict[str, object]", result["data"])
    plan = cast("dict[str, object]", data["accepted_plan"])
    assert plan["sprint_id"] == sprint_id
    assert plan["sprint_plan_artifact_id"] == replacement_id
    assert plan["goal"] == "Corrected accepted Sprint goal."
    assert cast("dict[str, object]", plan["acceptance"])["rationale"] == (
        "Accepted the corrected Sprint plan."
    )


def test_sprint_status_fails_closed_when_accepted_plan_linkage_is_contradictory(
    engine: Engine,
) -> None:
    """Never return a partial Sprint when accepted-plan linkage contradicts status."""
    project_id, sprint_id, _story_id, _plan = _seed_planned_sprint(engine)
    with Session(engine) as session:
        sprint = session.get_one(Sprint, sprint_id)
        sprint.status = SprintStatus.ACTIVE
        session.add(sprint)
        task = session.exec(select(Task).where(Task.story_id == _story_id)).one()
        task.description = "Tampered after acceptance"
        session.add(task)
        decision = session.exec(
            select(SprintPlanArtifactDecision).where(
                SprintPlanArtifactDecision.activated_sprint_id == sprint_id
            )
        ).one()
        assert decision.decision == "accepted"
        session.commit()

    result = DurableReadProjectionService(engine=engine).sprint_status(
        project_id=project_id
    )

    assert result["ok"] is False
    assert cast("list[dict[str, object]]", result["errors"])[0]["code"] == (
        "SPRINT_STATUS_INCONSISTENT"
    )
