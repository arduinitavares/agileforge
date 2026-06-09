"""Tests for read-only agent workbench projections."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import create_engine
from sqlmodel import select

from models.core import (
    Product,
    Sprint,
    SprintStory,
    Task,
    Team,
    UserStory,
    UserStoryDependency,
)
from models.enums import SprintStatus, StoryStatus
from services.agent_workbench.read_projection import ReadProjectionService
from services.orchestrator_query_service import fetch_sprint_candidates_from_session
from tests.typing_helpers import require_id
from utils.task_metadata import TaskMetadata, serialize_task_metadata

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine
    from sqlmodel import Session

    from services.agent_workbench.session_reader import ReadOnlySessionReader

SCHEMA_NOT_READY_EXIT_CODE = 5


def _engine(session: Session) -> Engine:
    """Return the test session bind as an engine for projection services."""
    return cast("Engine", session.get_bind())


class _FakeSessionReader:
    """Session reader test double that records read-only workflow lookups."""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.project_ids: list[int] = []
        self.state = state or {"fsm_state": "SPRINT_SETUP", "setup_status": "ready"}

    def get_project_state(self, project_id: int) -> dict[str, Any]:
        """Return a deterministic workflow state payload."""
        self.project_ids.append(project_id)
        return dict(self.state)


def _seed_project_with_story(session: Session) -> tuple[int, int, int, int]:
    """Persist a project, story, task, team, and planned sprint."""
    product = Product(name="Workbench Project", description="Demo")
    session.add(product)
    session.commit()
    session.refresh(product)
    product_id = require_id(product.product_id, "product_id")

    story = UserStory(
        product_id=product_id,
        title="Implement CLI",
        story_description="As an agent, I can inspect the project.",
        acceptance_criteria="- shows state",
        story_points=3,
        rank="1",
        is_refined=True,
    )
    session.add(story)
    session.commit()
    session.refresh(story)
    story_id = require_id(story.story_id, "story_id")

    task = Task(
        story_id=story_id,
        description="Add read projection",
        metadata_json=serialize_task_metadata(
            TaskMetadata(checklist_items=["Return JSON"])
        ),
    )
    session.add(task)

    team = Team(name="Workbench Team")
    session.add(team)
    session.commit()
    session.refresh(team)

    sprint = Sprint(
        product_id=product_id,
        team_id=require_id(team.team_id, "team_id"),
        goal="Inspect safely",
        start_date=date(2026, 5, 14),
        end_date=date(2026, 5, 28),
        status=SprintStatus.PLANNED,
    )
    session.add(sprint)
    session.commit()
    session.refresh(sprint)
    sprint_id = require_id(sprint.sprint_id, "sprint_id")

    session.add(SprintStory(sprint_id=sprint_id, story_id=story_id))
    session.commit()
    session.refresh(task)
    return product_id, story_id, sprint_id, require_id(task.task_id, "task_id")


def test_project_list_returns_counts_and_fingerprint(session: Session) -> None:
    """Verify project list is a read-only projection."""
    product_id, _story_id, _sprint_id, _task_id = _seed_project_with_story(session)
    service = ReadProjectionService(engine=_engine(session))

    result = service.project_list()

    assert result["ok"] is True
    assert result["data"]["count"] == 1
    assert result["data"]["items"][0]["product_id"] == product_id
    assert result["data"]["items"][0]["user_stories_count"] == 1
    assert result["data"]["items"][0]["sprint_count"] == 1
    assert result["data"]["source_fingerprint"].startswith("sha256:")


def test_project_list_counts_only_active_user_stories(session: Session) -> None:
    """Superseded backlog rows are audit history, not active project structure."""
    product_id, _story_id, _sprint_id, _task_id = _seed_project_with_story(session)
    session.add(
        UserStory(
            product_id=product_id,
            title="Superseded backlog seed",
            status=StoryStatus.TO_DO,
            is_superseded=True,
        )
    )
    session.commit()
    service = ReadProjectionService(engine=_engine(session))

    result = service.project_list()

    assert result["ok"] is True
    assert result["data"]["items"][0]["user_stories_count"] == 1


def test_project_show_returns_structure_counts_and_latest_approved_spec(
    session: Session,
) -> None:
    """Verify project show exposes read-only structure summary data."""
    product_id, _story_id, _sprint_id, _task_id = _seed_project_with_story(session)
    service = ReadProjectionService(engine=_engine(session))

    result = service.project_show(project_id=product_id)

    assert result["ok"] is True
    assert result["data"]["product_id"] == product_id
    assert result["data"]["structure_counts"]["user_stories"] == 1
    assert result["data"]["structure_counts"]["sprints"] == 1
    assert result["data"]["latest_approved_spec"] is None
    assert result["data"]["source_fingerprint"].startswith("sha256:")


def test_project_show_counts_only_active_user_stories(session: Session) -> None:
    """Project status should not present superseded backlog rows as active."""
    product_id, _story_id, _sprint_id, _task_id = _seed_project_with_story(session)
    session.add(
        UserStory(
            product_id=product_id,
            title="Superseded backlog seed",
            status=StoryStatus.TO_DO,
            is_superseded=True,
        )
    )
    session.commit()
    service = ReadProjectionService(engine=_engine(session))

    result = service.project_show(project_id=product_id)

    assert result["ok"] is True
    assert result["data"]["structure_counts"]["user_stories"] == 1


def test_workflow_state_uses_injected_read_only_session_reader(
    session: Session,
) -> None:
    """Verify workflow state delegates to the read-only session reader."""
    product_id, _story_id, _sprint_id, _task_id = _seed_project_with_story(session)
    reader = _FakeSessionReader()
    service = ReadProjectionService(
        engine=_engine(session),
        session_reader=cast("ReadOnlySessionReader", reader),
    )

    result = service.workflow_state(project_id=product_id)

    assert result["ok"] is True
    assert reader.project_ids == [product_id]
    assert result["data"]["project_id"] == product_id
    assert result["data"]["state"]["fsm_state"] == "SPRINT_SETUP"
    assert result["data"]["source_fingerprint"].startswith("sha256:")


def test_workflow_state_reconciles_completed_active_sprint(
    session: Session,
) -> None:
    """Prevent stale session state from advertising a completed Sprint as active."""
    product_id, _story_id, sprint_id, _task_id = _seed_project_with_story(session)
    sprint = session.get(Sprint, sprint_id)
    assert sprint is not None
    sprint.status = SprintStatus.COMPLETED
    sprint.completed_at = datetime(2026, 5, 28, 18, tzinfo=UTC)
    session.add(sprint)
    session.commit()
    reader = _FakeSessionReader(
        {
            "fsm_state": "SPRINT_PERSISTENCE",
            "setup_status": "passed",
            "active_sprint_id": sprint_id,
        }
    )
    service = ReadProjectionService(
        engine=_engine(session),
        session_reader=cast("ReadOnlySessionReader", reader),
    )

    result = service.workflow_state(project_id=product_id)

    assert result["ok"] is True
    state = result["data"]["state"]
    assert state["fsm_state"] == "SPRINT_COMPLETE"
    assert state["active_sprint_id"] is None
    assert state["latest_completed_sprint_id"] == sprint_id
    assert state["sprint_completed_at"] == "2026-05-28T18:00:00Z"
    assert state["sprint_state_reconciled_reason"] == "active_sprint_completed"


def test_story_show_returns_validation_and_fingerprint(session: Session) -> None:
    """Verify story show exposes story details without validation side effects."""
    _product_id, story_id, _sprint_id, _task_id = _seed_project_with_story(session)
    service = ReadProjectionService(engine=_engine(session))

    result = service.story_show(story_id=story_id)

    assert result["ok"] is True
    assert result["data"]["story_id"] == story_id
    assert result["data"]["title"] == "Implement CLI"
    assert result["data"]["validation"]["present"] is False
    assert result["data"]["source_fingerprint"].startswith("sha256:")


def test_sprint_candidates_returns_refined_unplanned_stories(
    session: Session,
) -> None:
    """Verify sprint candidates use existing eligibility without mutation."""
    product_id, story_id, _sprint_id, _task_id = _seed_project_with_story(session)
    service = ReadProjectionService(engine=_engine(session))

    result = service.sprint_candidates(project_id=product_id)

    assert result["ok"] is True
    assert result["data"]["count"] == 0
    assert story_id not in [item["story_id"] for item in result["data"]["items"]]
    assert result["data"]["excluded_counts"]["open_sprint"] == 1
    assert result["data"]["source_fingerprint"].startswith("sha256:")


def test_sprint_candidates_counts_excluded_story_reasons(
    session: Session,
) -> None:
    """Verify sprint candidate diagnostics match existing eligibility semantics."""
    product_id, _story_id, _sprint_id, _task_id = _seed_project_with_story(session)
    eligible = UserStory(
        product_id=product_id,
        title="Ready for sprint",
        story_description="Ready",
        acceptance_criteria="- AC",
        status=StoryStatus.TO_DO,
        is_refined=True,
        rank="2",
    )
    non_refined = UserStory(
        product_id=product_id,
        title="Needs refinement",
        status=StoryStatus.TO_DO,
        is_refined=False,
        rank="3",
    )
    superseded = UserStory(
        product_id=product_id,
        title="Superseded",
        status=StoryStatus.TO_DO,
        is_refined=True,
        is_superseded=True,
        rank="4",
    )
    done = UserStory(
        product_id=product_id,
        title="Done",
        status=StoryStatus.DONE,
        is_refined=True,
        rank="5",
    )
    session.add_all([eligible, non_refined, superseded, done])
    session.commit()
    session.refresh(eligible)
    service = ReadProjectionService(engine=_engine(session))

    result = service.sprint_candidates(project_id=product_id)

    assert result["ok"] is True
    assert [item["story_id"] for item in result["data"]["items"]] == [eligible.story_id]
    assert result["data"]["excluded_counts"] == {
        "non_refined": 1,
        "superseded": 1,
        "open_sprint": 1,
    }


def test_sprint_candidates_filters_to_story_completion_scope(
    session: Session,
) -> None:
    """Sprint candidate projection should honor scoped Story completion."""
    product = Product(name="Scoped Sprint Project", description="Demo")
    session.add(product)
    session.commit()
    session.refresh(product)
    product_id = require_id(product.product_id, "product_id")
    in_scope = UserStory(
        product_id=product_id,
        title="Login UI",
        story_description="Ready",
        acceptance_criteria="- AC",
        status=StoryStatus.TO_DO,
        is_refined=True,
        rank="1",
        story_points=2,
        source_requirement="enable login",
    )
    out_of_scope = UserStory(
        product_id=product_id,
        title="Invite teammates",
        story_description="Later",
        acceptance_criteria="- AC",
        status=StoryStatus.TO_DO,
        is_refined=True,
        rank="2",
        story_points=3,
        source_requirement="invite teammates",
    )
    session.add_all([in_scope, out_of_scope])
    session.commit()
    session.refresh(in_scope)
    session.refresh(out_of_scope)
    service = ReadProjectionService(
        engine=_engine(session),
        session_reader=cast(
            "ReadOnlySessionReader",
            _FakeSessionReader(
                {
                    "fsm_state": "SPRINT_SETUP",
                    "story_completion_scope": {
                        "scope": "milestone",
                        "scope_id": "milestone_0",
                        "requirements": ["Enable Login"],
                    },
                }
            ),
        ),
    )

    result = service.sprint_candidates(project_id=product_id)

    assert result["ok"] is True
    assert [item["story_id"] for item in result["data"]["items"]] == [
        in_scope.story_id
    ]
    assert result["data"]["count"] == 1
    assert result["data"]["excluded_counts"]["story_completion_scope"] == 1
    assert out_of_scope.story_id not in [
        item["story_id"] for item in result["data"]["items"]
    ]


def test_sprint_candidates_blocks_selection_with_external_dependency(
    session: Session,
) -> None:
    """Selection-scoped candidates should block external dependencies."""
    product = Product(name="Scoped Dependency Project", description="Demo")
    session.add(product)
    session.commit()
    session.refresh(product)
    product_id = require_id(product.product_id, "product_id")
    selected = UserStory(
        product_id=product_id,
        title="Selected story",
        story_description="Ready",
        acceptance_criteria="- AC",
        status=StoryStatus.TO_DO,
        is_refined=True,
        rank="1",
        story_points=2,
        source_requirement="Research slice",
    )
    excluded = UserStory(
        product_id=product_id,
        title="Excluded dependency",
        story_description="Later",
        acceptance_criteria="- AC",
        status=StoryStatus.TO_DO,
        is_refined=True,
        rank="2",
        story_points=3,
        source_requirement="Later slice",
    )
    session.add_all([selected, excluded])
    session.commit()
    session.refresh(selected)
    session.refresh(excluded)
    session.add(
        UserStoryDependency(
            product_id=product_id,
            dependent_story_id=require_id(selected.story_id, "story_id"),
            prerequisite_story_id=require_id(excluded.story_id, "story_id"),
            status="active",
            source="manual_review",
            confidence="reviewed",
        )
    )
    session.commit()

    service = ReadProjectionService(
        engine=_engine(session),
        session_reader=cast(
            "ReadOnlySessionReader",
            _FakeSessionReader(
                {
                    "fsm_state": "SPRINT_SETUP",
                    "story_completion_scope": {
                        "scope": "selection",
                        "scope_id": "selection:sha256:fixture",
                        "requirements": ["Research slice"],
                    },
                }
            ),
        ),
    )

    result = service.sprint_candidates(project_id=product_id)

    assert result["ok"] is True
    readiness = result["data"]["readiness"]
    assert readiness["status"] == "blocked"
    assert readiness["blocking_codes"] == ["SPRINT_SCOPE_EXTERNAL_DEPENDENCY"]
    assert readiness["blocking_story_ids"] == [selected.story_id]
    assert readiness["external_dependency_story_ids"] == [excluded.story_id]


def test_sprint_candidates_reports_readiness_blockers(
    session: Session,
) -> None:
    """Read projection should surface candidate readiness blockers."""
    product = Product(name="Readiness Project", description="Demo")
    session.add(product)
    session.commit()
    session.refresh(product)
    product_id = require_id(product.product_id, "product_id")
    candidate = UserStory(
        product_id=product_id,
        title="Legacy refined story",
        story_description="As a user, I want a thing.",
        acceptance_criteria="- Verify behavior.",
        status=StoryStatus.TO_DO,
        is_refined=True,
        is_superseded=False,
        story_points=None,
        rank=None,
    )
    session.add(candidate)
    session.commit()
    service = ReadProjectionService(engine=_engine(session))

    payload = service.sprint_candidates(project_id=product_id)

    assert payload["ok"] is True
    assert payload["data"]["readiness"]["status"] == "blocked"
    assert payload["data"]["readiness"]["blocking_codes"] == [
        "SPRINT_CANDIDATES_UNSIZED",
        "SPRINT_CANDIDATES_DEFAULT_PRIORITY",
    ]


def test_sprint_candidates_match_canonical_session_helper(
    session: Session,
) -> None:
    """Verify workbench output uses the shared sprint eligibility helper."""
    product_id, _story_id, _sprint_id, _task_id = _seed_project_with_story(session)
    eligible = UserStory(
        product_id=product_id,
        title="Ready for sprint",
        story_description="Ready",
        acceptance_criteria="- AC",
        status=StoryStatus.TO_DO,
        is_refined=True,
        rank="2",
    )
    session.add(eligible)
    session.commit()
    service = ReadProjectionService(engine=_engine(session))

    canonical = fetch_sprint_candidates_from_session(session, product_id)
    result = service.sprint_candidates(project_id=product_id)

    assert result["ok"] is True
    assert result["data"]["items"] == canonical["stories"]
    assert result["data"]["count"] == canonical["count"]
    assert result["data"]["excluded_counts"] == canonical["excluded_counts"]
    assert result["data"]["message"] == canonical["message"]


def test_sprint_candidates_fingerprint_changes_when_open_sprint_link_moves(
    session: Session,
) -> None:
    """Verify sprint candidate fingerprints include open sprint link identity."""
    product_id, blocked_story_id, sprint_id, _task_id = _seed_project_with_story(
        session
    )
    candidate = UserStory(
        product_id=product_id,
        title="Ready for sprint",
        story_description="Ready",
        acceptance_criteria="- AC",
        status=StoryStatus.TO_DO,
        is_refined=True,
        rank="2",
    )
    session.add(candidate)
    session.commit()
    session.refresh(candidate)
    candidate_story_id = require_id(candidate.story_id, "candidate_story_id")
    service = ReadProjectionService(engine=_engine(session))

    before = service.sprint_candidates(project_id=product_id)
    original_link = session.exec(
        select(SprintStory).where(
            SprintStory.sprint_id == sprint_id,
            SprintStory.story_id == blocked_story_id,
        )
    ).one()
    session.delete(original_link)
    session.add(SprintStory(sprint_id=sprint_id, story_id=candidate_story_id))
    session.commit()
    after = service.sprint_candidates(project_id=product_id)

    assert [item["story_id"] for item in before["data"]["items"]] == [
        candidate_story_id
    ]
    assert [item["story_id"] for item in after["data"]["items"]] == [blocked_story_id]
    assert before["data"]["count"] == after["data"]["count"] == 1
    assert before["data"]["excluded_counts"] == after["data"]["excluded_counts"]
    assert before["data"]["source_fingerprint"] != after["data"]["source_fingerprint"]


def test_sprint_candidates_fingerprint_changes_when_candidate_row_state_changes(
    session: Session,
) -> None:
    """Verify fingerprints include private candidate row-state inputs."""
    product_id, _story_id, _sprint_id, _task_id = _seed_project_with_story(session)
    candidate = UserStory(
        product_id=product_id,
        title="Ready for sprint",
        story_description="Ready",
        acceptance_criteria="- AC",
        status=StoryStatus.TO_DO,
        is_refined=True,
        rank="2",
    )
    session.add(candidate)
    session.commit()
    session.refresh(candidate)
    service = ReadProjectionService(engine=_engine(session))

    before = service.sprint_candidates(project_id=product_id)
    candidate.updated_at = datetime(2026, 5, 15, 12, tzinfo=UTC)
    session.add(candidate)
    session.commit()
    after = service.sprint_candidates(project_id=product_id)

    assert before["data"]["items"] == after["data"]["items"]
    assert before["data"]["source_fingerprint"] != after["data"]["source_fingerprint"]


def test_read_projection_reports_schema_not_ready_without_creating_database(
    tmp_path: Path,
) -> None:
    """Report missing schema without creating or migrating a SQLite database."""
    db_path = tmp_path / "missing.sqlite3"
    service = ReadProjectionService(
        engine=create_engine(f"sqlite:///{db_path.as_posix()}"),
    )

    result = service.project_list()

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "SCHEMA_NOT_READY"
    assert result["errors"][0]["exit_code"] == SCHEMA_NOT_READY_EXIT_CODE
    assert result["errors"][0]["retryable"] is True
    assert "products" in result["errors"][0]["details"]["missing"]
    assert not db_path.exists()
