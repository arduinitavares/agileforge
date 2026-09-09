"""Lifecycle-aware additive Sprint-status projection coverage for issue #227."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, cast

from sqlmodel import Session, select

from models.core import Sprint, Task
from models.enums import SprintStatus
from models.workflow import SprintPlanArtifactDecision
from repositories.workflow import WorkflowFactRepository
from services.application import SprintRetryRequest, SprintStartRequest
from services.contracts.sprint import SprintPlannerOutput
from services.read_projections import DurableReadProjectionService
from tests.adapters.sprint_retry_fixtures import (
    durable_rows,
    retry_transport_fixture,
)
from tests.workflow.execution_fixtures import seed_started_execution
from tests.workflow.retry_execution_fixtures import (
    CompletedRetrySource,
    close_retry_sprint,
    close_retry_story,
    complete_retry_task,
    record_pending_successor_plan,
    review_retry_sprint,
    triage_retry_sprint,
)
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
    plan_id, _candidate_fingerprint, plan, plan_fingerprint = _record_sprint_plan_draft(
        engine,
        domain,
        project_id,
        story_id,
        team_name="Issue 227 status team",
        idempotency_key="issue-227-record-sprint-plan",
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
    assert (
        cast("list[dict[str, object]]", accepted_plan["selected_stories"])[0][
            "story_id"
        ]
        == story_id
    )
    assert start["sprint_id"] == sprint_id
    assert start["sprint_plan_artifact_id"] == accepted_plan["sprint_plan_artifact_id"]
    assert start["plan_fingerprint"] == accepted_plan["plan_fingerprint"]
    assert (
        start["task_content_fingerprint"] == accepted_plan["task_content_fingerprint"]
    )


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


def _start_retry_through_application(
    engine: Engine,
    *,
    start: bool = True,
    close_first_story: bool = False,
) -> tuple[
    CompletedRetrySource,
    int,
    int,
    int,
    DurableReadProjectionService,
    JsonObject,
]:
    """Create and start one genuine retry receipt for scoped read assertions."""
    fixture = retry_transport_fixture(engine)
    source = fixture.source
    with Session(engine) as session:
        original_snapshot = WorkflowFactRepository(session).load(source.project_id)
    original_evidence: JsonObject = {
        "start": next(
            item.model_dump(mode="json")
            for item in original_snapshot.sprint_starts
            if item.sprint_id == source.source_sprint_id
        ),
        "task": next(
            item.model_dump(mode="json")
            for item in original_snapshot.tasks
            if item.task_id == source.first_task_id
        ),
        "completion": next(
            item.model_dump(mode="json")
            for item in original_snapshot.task_completions
            if item.task_id == source.first_task_id
        ),
        "task_completions": [
            item.model_dump(mode="json")
            for item in original_snapshot.task_completions
            if item.sprint_id == source.source_sprint_id
        ],
        "story_completions": [
            item.model_dump(mode="json")
            for item in original_snapshot.story_completions
            if item.sprint_id == source.source_sprint_id
        ],
        "review": next(
            item.model_dump(mode="json")
            for item in original_snapshot.sprint_reviews
            if item.sprint_id == source.source_sprint_id
        ),
        "closure": next(
            item.model_dump(mode="json")
            for item in original_snapshot.sprint_closures
            if item.sprint_id == source.source_sprint_id
        ),
        "triage": [
            item.model_dump(mode="json")
            for item in original_snapshot.post_sprint_triage
            if item.sprint_id == source.source_sprint_id
        ],
    }
    preview = fixture.application.sprint_retry_preview(
        project_id=source.project_id,
        sprint_id=source.source_sprint_id,
    )
    preview_data = cast("dict[str, object]", preview["data"])
    applied = fixture.application.retry_sprint(
        SprintRetryRequest(
            project_id=source.project_id,
            sprint_id=source.source_sprint_id,
            confirm=True,
            expected_state_fingerprint=cast(
                "str", preview_data["expected_state_fingerprint"]
            ),
            rationale="Fresh evidence is required for scoped read coverage.",
            actor="owner@example.com",
            idempotency_key="issue-260-projection-retry",
        )
    )
    assert applied.ok is True
    retry_attempt_id = cast("int", applied.output["retry_attempt_id"])
    instance_key = f"retry:{retry_attempt_id}:sprint:{source.source_sprint_id}"
    if start:
        decision = next(
            item
            for item in fixture.application.position(
                project_id=source.project_id
            ).decisions
            if item.node_id == "execution.sprint.retry.start"
            and item.instance_key == instance_key
        )
        started = fixture.application.start_sprint(
            SprintStartRequest(
                project_id=source.project_id,
                instance_key=instance_key,
                expected_decision_fingerprint=decision.decision_fingerprint,
                actor="owner@example.com",
                idempotency_key="issue-260-projection-start-retry",
            )
        )
        assert started.ok is True
    if close_first_story:
        complete_retry_task(
            source.domain,
            project_id=source.project_id,
            retry_id=retry_attempt_id,
            task_id=source.first_task_id,
            suffix="issue-260-projection",
        )
        close_retry_story(
            source.domain,
            project_id=source.project_id,
            retry_id=retry_attempt_id,
            story_id=source.first_story_id,
            suffix="issue-260-projection",
        )
    return (
        source,
        source.source_sprint_id,
        source.first_task_id,
        retry_attempt_id,
        DurableReadProjectionService(engine=engine),
        original_evidence,
    )


def test_sprint_reads_use_the_current_retry_scope_without_rewriting_history(  # noqa: PLR0915
    engine: Engine,
) -> None:
    """Every Sprint and Task read names the live retry and its exact binding."""
    source, sprint_id, task_id, retry_attempt_id, reads, original_evidence = (
        _start_retry_through_application(engine)
    )
    project_id = source.project_id
    before_reads = durable_rows(engine)
    retry_scope = {
        "retry_attempt_id": retry_attempt_id,
        "ordinal": 2,
        "status": "active",
        "predecessor_retry_attempt_id": None,
        "sprint_instance_key": f"retry:{retry_attempt_id}:sprint:{sprint_id}",
    }

    status = reads.sprint_status(project_id=project_id, sprint_id=sprint_id)
    assert status["ok"] is True
    status_data = cast("dict[str, object]", status["data"])
    assert cast("dict[str, object]", status_data["sprint"])["status"] == "completed"
    assert status_data["current_retry"] == retry_scope
    assert status_data["effective_status"] == "active"
    start = cast("dict[str, object]", status_data["start"])
    assert start["retry_attempt_id"] == retry_attempt_id
    assert start["started_by"] == "owner@example.com"
    assert start["started_at"] is not None
    original_start = cast("dict[str, object]", status_data["original_start"])
    assert original_start == original_evidence["start"]
    status_tasks = cast("list[dict[str, object]]", status_data["tasks"])
    assert {task["status"] for task in status_tasks} == {"To Do"}
    assert {task["instance_key"] for task in status_tasks} == {
        f"retry:{retry_attempt_id}:task:{task['task_id']}" for task in status_tasks
    }
    status_stories = cast("list[dict[str, object]]", status_data["stories"])
    assert {story["status"] for story in status_stories} == {"To Do"}
    assert {story["instance_key"] for story in status_stories} == {
        f"retry:{retry_attempt_id}:story:{story['story_id']}"
        for story in status_stories
    }

    tasks = reads.sprint_tasks(project_id=project_id, sprint_id=sprint_id)
    assert tasks["ok"] is True
    tasks_data = cast("dict[str, object]", tasks["data"])
    assert tasks_data["current_retry"] == retry_scope
    assert tasks_data["effective_status"] == "active"

    detail = reads.sprint_task_show(
        project_id=project_id,
        sprint_id=sprint_id,
        task_id=task_id,
    )
    assert detail["ok"] is True
    detail_data = cast("dict[str, object]", detail["data"])
    assert detail_data["current_retry"] == retry_scope
    assert cast("dict[str, object]", detail_data["task"])["status"] == "To Do"
    assert cast("dict[str, object]", detail_data["task"])["instance_key"] == (
        f"retry:{retry_attempt_id}:task:{task_id}"
    )
    assert detail_data["completion"] is None
    original_task = cast("dict[str, object]", original_evidence["task"])
    assert detail_data["original_task"] == {
        **original_task,
        "instance_key": f"task:{task_id}",
    }
    assert detail_data["original_completion"] == original_evidence["completion"]

    history = reads.sprint_task_history(
        project_id=project_id,
        sprint_id=sprint_id,
        task_id=task_id,
    )
    assert history["ok"] is True
    history_data = cast("dict[str, object]", history["data"])
    assert history_data["current_retry"] == retry_scope
    assert cast("dict[str, object]", history_data["task"])["status"] == "To Do"
    assert history_data["original_completion"] == original_evidence["completion"]

    review = reads.sprint_review(project_id=project_id, sprint_id=sprint_id)
    assert review["ok"] is True
    review_data = cast("dict[str, object]", review["data"])
    assert review_data["current_retry"] == retry_scope
    assert review_data["effective_status"] == "active"
    assert review_data["review"] is None
    assert review_data["closure"] is None
    assert review_data["original_review"] == original_evidence["review"]
    assert review_data["original_closure"] == original_evidence["closure"]
    assert review_data["original_triage"] == original_evidence["triage"]

    sprint_history = reads.sprint_history(project_id=project_id)
    assert sprint_history["ok"] is True
    history_data = cast("dict[str, object]", sprint_history["data"])
    attempts = cast("list[dict[str, object]]", history_data["execution_attempts"])
    assert attempts[-1] == {
        **retry_scope,
        "sprint_id": sprint_id,
        "task_instance_keys": [
            f"retry:{retry_attempt_id}:task:{task['task_id']}" for task in status_tasks
        ],
        "start": start,
        "task_completions": [],
        "story_completions": [],
        "review": None,
        "closure": None,
        "triage": [],
    }
    original_attempt = next(
        item
        for item in attempts
        if item["sprint_id"] == sprint_id and item["retry_attempt_id"] is None
    )
    assert original_attempt["start"] == original_evidence["start"]
    assert original_attempt["task_completions"] == original_evidence["task_completions"]
    assert (
        original_attempt["story_completions"] == original_evidence["story_completions"]
    )
    assert original_attempt["review"] == original_evidence["review"]
    assert original_attempt["closure"] == original_evidence["closure"]
    assert original_attempt["triage"] == original_evidence["triage"]
    assert durable_rows(engine) == before_reads


def test_planned_retry_hides_the_original_start_from_current_progress(
    engine: Engine,
) -> None:
    """A planned retry keeps original start evidence out of current progress."""
    source, sprint_id, _task_id, retry_attempt_id, reads, _original_evidence = (
        _start_retry_through_application(engine, start=False)
    )
    project_id = source.project_id

    status = reads.sprint_status(project_id=project_id, sprint_id=sprint_id)

    assert status["ok"] is True
    data = cast("dict[str, object]", status["data"])
    assert cast("dict[str, object]", data["current_retry"])["retry_attempt_id"] == (
        retry_attempt_id
    )
    assert data["effective_status"] == "planned"
    assert data["start"] is None
    assert cast("dict[str, object]", data["original_start"])["sprint_id"] == sprint_id


def test_sprint_status_exposes_retry_local_story_closure_progress(
    engine: Engine,
) -> None:
    """Retry Story progress never falls back to the completed original Story state."""
    source, sprint_id, _task_id, retry_attempt_id, reads, _original_evidence = (
        _start_retry_through_application(engine, close_first_story=True)
    )
    project_id = source.project_id

    status = reads.sprint_status(project_id=project_id, sprint_id=sprint_id)

    assert status["ok"] is True
    data = cast("dict[str, object]", status["data"])
    stories = cast("list[dict[str, object]]", data["stories"])
    assert {story["status"] for story in stories} == {"Done", "To Do"}
    assert {story["instance_key"] for story in stories} == {
        f"retry:{retry_attempt_id}:story:{story['story_id']}" for story in stories
    }
    completions = cast("list[dict[str, object]]", data["story_completions"])
    assert len(completions) == 1
    assert completions[0]["sprint_id"] == sprint_id


def test_retry_review_close_and_triage_survive_fresh_projection_reads(
    engine: Engine,
) -> None:
    """Reviewed and closed retry facts stay scoped across fresh durable readers."""
    source, sprint_id, _task_id, retry_attempt_id, _reads, _original_evidence = (
        _start_retry_through_application(engine)
    )
    project_id = source.project_id
    for task_id, story_id, suffix in (
        (source.first_task_id, source.first_story_id, "first"),
        (source.second_task_id, source.second_story_id, "second"),
    ):
        complete_retry_task(
            source.domain,
            project_id=project_id,
            retry_id=retry_attempt_id,
            task_id=task_id,
            suffix=f"issue-260-terminal-{suffix}",
        )
        close_retry_story(
            source.domain,
            project_id=project_id,
            retry_id=retry_attempt_id,
            story_id=story_id,
            suffix=f"issue-260-terminal-{suffix}",
        )
    review_retry_sprint(
        source.domain,
        project_id=project_id,
        retry_id=retry_attempt_id,
        sprint_id=sprint_id,
        suffix="issue-260-terminal",
    )

    reviewed = DurableReadProjectionService(engine=engine).sprint_review(
        project_id=project_id,
        sprint_id=sprint_id,
    )

    assert reviewed["ok"] is True
    reviewed_data = cast("dict[str, object]", reviewed["data"])
    assert cast("dict[str, object]", reviewed_data["current_retry"])["status"] == (
        "active"
    )
    assert reviewed_data["review"] is not None
    assert reviewed_data["closure"] is None
    close_retry_sprint(
        source.domain,
        project_id=project_id,
        retry_id=retry_attempt_id,
        sprint_id=sprint_id,
        suffix="issue-260-terminal",
    )

    awaiting_triage = DurableReadProjectionService(engine=engine).sprint_review(
        project_id=project_id,
        sprint_id=sprint_id,
    )

    assert awaiting_triage["ok"] is True
    awaiting_data = cast("dict[str, object]", awaiting_triage["data"])
    assert cast("dict[str, object]", awaiting_data["current_retry"])["status"] == (
        "completed"
    )
    assert awaiting_data["review"] is not None
    assert awaiting_data["closure"] is not None
    assert awaiting_data["triage"] == []
    triage_retry_sprint(
        source.domain,
        project_id=project_id,
        retry_id=retry_attempt_id,
        sprint_id=sprint_id,
        suffix="issue-260-terminal",
    )

    finalized = DurableReadProjectionService(engine=engine).sprint_review(
        project_id=project_id,
        sprint_id=sprint_id,
    )

    assert finalized["ok"] is True
    final_data = cast("dict[str, object]", finalized["data"])
    assert cast("dict[str, object]", final_data["current_retry"])["status"] == (
        "completed"
    )
    assert len(cast("list[object]", final_data["triage"])) == 1


def test_pending_successor_plan_keeps_source_history_and_review_visible(
    engine: Engine,
) -> None:
    """A pending successor keeps source history and its own exact review."""
    fixture = retry_transport_fixture(engine)
    source = fixture.source
    _domain, pending_plan_artifact_id = record_pending_successor_plan(engine, source)
    reads = DurableReadProjectionService(engine=engine)
    before_reads = durable_rows(engine)

    source_status = reads.sprint_status(
        project_id=source.project_id,
        sprint_id=source.source_sprint_id,
    )
    history = reads.sprint_history(project_id=source.project_id)
    pending_review = reads.sprint_plan_review(
        project_id=source.project_id,
        sprint_plan_artifact_id=pending_plan_artifact_id,
    )

    assert source_status["ok"] is True
    source_data = cast("dict[str, object]", source_status["data"])
    assert cast("dict[str, object]", source_data["sprint"])["sprint_id"] == (
        source.source_sprint_id
    )
    assert history["ok"] is True
    history_data = cast("dict[str, object]", history["data"])
    sprints = cast("list[dict[str, object]]", history_data["sprints"])
    assert any(item["sprint_id"] == source.source_sprint_id for item in sprints)
    assert pending_review["ok"] is True
    review_data = cast("dict[str, object]", pending_review["data"])
    candidate = cast("dict[str, object]", review_data["candidate"])
    assert candidate["sprint_plan_artifact_id"] == pending_plan_artifact_id
    assert durable_rows(engine) == before_reads
