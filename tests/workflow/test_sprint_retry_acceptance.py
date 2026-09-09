"""Integrated acceptance coverage for the Sprint retry lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from git import Git
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from adapters.adk.runner import AdkWorkflowRunner
from cli.dev_main import _verify_business_schema
from cli.dev_profiles import initialize_profile_record, load_profile
from models.core import Sprint, UserStory
from models.db import (
    CURRENT_BUSINESS_SCHEMA_MANIFEST,
    _inspect_business_schema_manifest,
    ensure_business_db_ready,
)
from repositories.workflow import WorkflowFactRepository
from services.application import (
    AgileForgeApplication,
    CloseStoryRequest,
    CompleteTaskRequest,
    ExecutionActionSelectionService,
    PostSprintTriageRequest,
    SprintCloseRequest,
    SprintRetryRequest,
    SprintReviewRequest,
    SprintStartRequest,
)
from services.contracts.story import (
    CanonicalStoryItem,
    CanonicalStoryOutput,
    InvestDimensionAssessment,
    StoryInvestAssessment,
    StoryItemEnvelope,
)
from services.project_lifecycle import ProjectLifecycleService
from tests.adapters.sprint_retry_fixtures import (
    DurableRows,
    durable_rows,
    preserves_existing_rows,
)
from tests.workflow.planning_fixtures import (
    planning_guards,
    planning_output_int,
    record_and_accept_roadmap,
    seed_accepted_backlog,
    validate_story_structurally,
)
from tests.workflow.retry_execution_fixtures import (
    SprintPlanFixtureInput,
    _plan_and_start,
    _task_id,
)
from workflow.clock import FixedClock
from workflow.contracts import JsonObject, NodeCategory
from workflow.definitions.planning import planning_graph
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain
from workflow.execution_scope import current_execution_scope, resolve_execution_scope
from workflow.fingerprints import canonical_hash
from workflow.requests import DecideStory, RecordStoryDraft

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.engine import Engine


_ACTOR = "owner@example.com"
_COUNT = 3
_FIRST_ID = 200
_SECOND_ID = 100
_TABLES = (
    "task_completion_evidence",
    "story_completion_logs",
    "sprint_reviews",
    "sprint_closures",
    "post_sprint_triage",
    "workflow_events",
    "workflow_transition_receipts",
)
_PRE_RETRY_SCHEMA = (
    Path(__file__).parents[1]
    / "fixtures"
    / "issue_260"
    / "pre_retry_business_schema_da3dbf63.sql"
)


@dataclass(frozen=True)
class _Scope:
    project_id: int
    sprint_id: int
    story_id: int
    task_id: int
    key: str
    retry_id: int | None = None

    def instance(self, kind: str, entity_id: int) -> str:
        prefix = "" if self.retry_id is None else f"retry:{self.retry_id}:"
        return f"{prefix}{kind}:{entity_id}"


@dataclass(frozen=True)
class _Source:
    engine: Engine
    artifact_id: int
    first: _Scope
    second: _Scope
    unselected_id: int
    original_task_request: CompleteTaskRequest
    rows: DurableRows
    artifacts: dict[tuple[object, ...], tuple[object, ...]]
    executions: dict[str, dict[tuple[object, ...], tuple[object, ...]]]
    unselected_row: tuple[object, ...]


@dataclass
class _Journey:
    source: _Source
    engine: Engine
    retry_id: int
    bound_tree: Path
    tree_before: dict[str, bytes]

    def scope(self) -> _Scope:
        source = self.source.second
        return _Scope(
            project_id=source.project_id,
            sprint_id=source.sprint_id,
            story_id=source.story_id,
            task_id=source.task_id,
            key="retry-second",
            retry_id=self.retry_id,
        )


def _assessment() -> StoryInvestAssessment:
    """Build complete canonical data rather than a partial fixture mock."""
    dimension = InvestDimensionAssessment(
        result="pass", rationale="Bounded observable work.", evidence="Focused journey."
    )
    return StoryInvestAssessment(
        independent=dimension,
        negotiable=dimension,
        valuable=dimension,
        estimable=dimension,
        small=dimension,
        testable=dimension,
    )


def _content() -> JsonObject:
    """Create three genuinely distinct envelopes in one canonical output."""
    items = tuple(
        CanonicalStoryItem(
            story_item_id=f"US-{ordinal:04d}",
            story_title=f"Retry Story {ordinal}",
            statement=(
                "As an operator, I want retry history to remain immutable, so that "
                f"Story {ordinal} can be audited after a fresh attempt."
            ),
            persona="operator",
            acceptance_criteria=(f"Keep Story {ordinal} immutable.",),
            spec_item_ids=("REQ.planning-1",),
            invest_assessment=_assessment(),
            estimated_effort="M",
            effort_rationale="Focused acceptance work.",
            order_rationale=f"Accepted sequence {ordinal}.",
            produced_artifacts=("retry evidence",),
            research_caveats=(),
            dependency_candidates=(),
        )
        for ordinal in range(1, _COUNT + 1)
    )
    output = CanonicalStoryOutput(
        story_items=tuple(
            StoryItemEnvelope(
                item=item, item_fingerprint=canonical_hash(item.model_dump(mode="json"))
            )
            for item in items
        ),
        is_complete=True,
        clarifying_questions=(),
    )
    return output.model_dump(mode="json")


def _domain(engine: Engine, minute: int) -> WorkflowDomain:
    return WorkflowDomain(
        engine=engine,
        graph=project_graph(),
        clock=FixedClock(now_value=datetime(2026, 9, 9, 9, minute, tzinfo=UTC)),
    )


def _app(engine: Engine, minute: int) -> AgileForgeApplication:
    return AgileForgeApplication(
        workflow_domain=_domain(engine, minute),
        execution_action_selection=ExecutionActionSelectionService(engine=engine),
    )


def _reopen(engine: Engine) -> Engine:
    database_url = str(engine.url)
    engine.dispose()
    return create_engine(database_url)


def _git(checkout: Path, *arguments: str) -> None:
    """Create only the disposable tracked checkout needed by profile loading."""
    Git().execute(command=["git", "-C", str(checkout), *arguments])


def _profile_checkout(tmp_path: Path) -> Path:
    """Build current source pins in an isolated Git checkout."""
    checkout = tmp_path / "profile-checkout"
    checkout.mkdir()
    _git(checkout, "init", "-b", "profile-acceptance")
    _git(checkout, "config", "user.name", "Acceptance")
    _git(checkout, "config", "user.email", "acceptance@example.invalid")
    for relative, content in {
        ".gitignore": ".agileforge/\n",
        "README.md": "profile acceptance\n",
        "agileforge-dev": "#!/bin/sh\n",
        "agile_sqlmodel.py": "SCHEMA = 1\n",
        "models/__init__.py": "",
        "models/core.py": "MODEL = 1\n",
        "cli/main.py": "CLI = 1\n",
        "services/application.py": "SERVICE = 1\n",
        "uv.lock": "version = 1\n",
        "config/models.yaml": "models:\n  default: test-model\n",
    }.items():
        path = checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-m", "profile source")
    return checkout


def _tree_state(root: Path) -> dict[str, bytes]:
    """Snapshot representative bound-tree bytes without touching the checkout."""
    return {
        item.relative_to(root).as_posix(): item.read_bytes()
        for item in sorted(root.rglob("*"))
        if item.is_file()
    }


def _record_three(
    engine: Engine, domain: WorkflowDomain, project_id: int
) -> tuple[int, dict[str, int]]:
    position = domain.position(project_id)
    decision = next(
        item
        for item in position.decisions
        if item.node_id == "planning.story.generate"
        and item.category is NodeCategory.AVAILABLE
        and item.reason_code != "STORY_CORRECTION_AVAILABLE"
    )
    assert decision.instance_key is not None
    refs = {item.fact_type: item for item in decision.fact_references}
    content = _content()
    recorded = domain.transition(
        RecordStoryDraft(
            **planning_guards(
                position, "planning.story.generate", decision.instance_key
            ),
            idempotency_key="acceptance-record-stories",
            backlog_item_id=refs["backlog_item"].fact_id,
            source_backlog_artifact_id=int(refs["backlog"].fact_id),
            source_backlog_artifact_fingerprint=refs["backlog"].fingerprint,
            roadmap_artifact_id=int(refs["roadmap"].fact_id),
            roadmap_artifact_fingerprint=refs["roadmap"].fingerprint,
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
        )
    )
    assert recorded.ok is True
    artifact_id = planning_output_int(recorded, "story_artifact_id")
    assert recorded.output["story_item_ids"] == ("US-0001", "US-0002", "US-0003")
    accepted = domain.transition(
        DecideStory(
            **planning_guards(
                domain.position(project_id),
                "planning.story.review",
                decision.instance_key,
            ),
            idempotency_key="acceptance-accept-stories",
            backlog_item_id=refs["backlog_item"].fact_id,
            story_artifact_id=artifact_id,
            artifact_fingerprint=str(recorded.output["content_fingerprint"]),
            decision="accepted",
            rationale="All accepted Stories are ready for planning.",
        )
    )
    assert accepted.ok is True
    activated_ids = accepted.output["activated_story_ids"]
    assert isinstance(activated_ids, tuple)
    assert len(activated_ids) == _COUNT
    assert all(type(item) is int for item in activated_ids)
    ids = tuple(item for item in activated_ids if type(item) is int)
    for story_id in ids:
        validate_story_structurally(engine, story_id)
    with Session(engine) as session:
        stories = tuple(
            item for item in session.exec(select(UserStory)) if item.story_id in ids
        )
    assert len(stories) == _COUNT
    assert {item.source_story_artifact_id for item in stories} == {artifact_id}
    assert {item.source_story_item_id for item in stories} == {
        "US-0001",
        "US-0002",
        "US-0003",
    }
    materialized_ids: dict[str, int] = {}
    for item in stories:
        assert item.story_id is not None
        materialized_ids[item.source_story_item_id] = item.story_id
    return artifact_id, materialized_ids


def _complete(application: AgileForgeApplication, scope: _Scope) -> CompleteTaskRequest:
    """Write fresh Task evidence in one exact scope."""
    request = CompleteTaskRequest(
        project_id=scope.project_id,
        instance_key=scope.instance("task", scope.task_id),
        idempotency_key=f"{scope.key}-task",
        actor=_ACTOR,
        outcome_summary="Completed exact accepted work.",
        artifact_refs=("tests/workflow/test_sprint_retry_acceptance.py",),
        acceptance_result="fully_met",
        checklist_result={"Run focused tests": "passed"},
    )
    assert application.complete_task(request).ok is True
    return request


def _close_story(application: AgileForgeApplication, scope: _Scope) -> None:
    """Close one exact Story after its fresh Task evidence."""
    assert (
        application.close_story(
            CloseStoryRequest(
                project_id=scope.project_id,
                instance_key=scope.instance("story", scope.story_id),
                idempotency_key=f"{scope.key}-story",
                actor=_ACTOR,
                resolution="Completed",
                delivered="Fresh closure.",
                evidence="Focused test passed.",
                known_gaps="None.",
            )
        ).ok
        is True
    )


def _review(application: AgileForgeApplication, scope: _Scope) -> None:
    """Record retry-local Sprint review."""
    assert (
        application.review_sprint(
            SprintReviewRequest(
                project_id=scope.project_id,
                instance_key=scope.instance("sprint", scope.sprint_id),
                idempotency_key=f"{scope.key}-review",
                actor=_ACTOR,
            )
        ).ok
        is True
    )


def _close_sprint(application: AgileForgeApplication, scope: _Scope) -> None:
    """Explicitly close the reviewed Sprint."""
    assert (
        application.close_sprint(
            SprintCloseRequest(
                project_id=scope.project_id,
                instance_key=scope.instance("sprint", scope.sprint_id),
                idempotency_key=f"{scope.key}-close",
                actor=_ACTOR,
            )
        ).ok
        is True
    )


def _triage(application: AgileForgeApplication, scope: _Scope) -> None:
    """Record terminal post-Sprint triage for one exact scope."""
    assert (
        application.record_post_sprint_triage(
            PostSprintTriageRequest(
                project_id=scope.project_id,
                instance_key=scope.instance("sprint", scope.sprint_id),
                idempotency_key=f"{scope.key}-triage",
                actor=_ACTOR,
                impact="none",
                canonical_payload={"summary": "No downstream change."},
            )
        ).ok
        is True
    )


def _finish(application: AgileForgeApplication, scope: _Scope) -> CompleteTaskRequest:
    """Finish original fixture work through the same public action helpers."""
    task = _complete(application, scope)
    _close_story(application, scope)
    _review(application, scope)
    _close_sprint(application, scope)
    _triage(application, scope)
    return task


def _plan_sprints(
    engine: Engine, project_id: int, ids: dict[str, int]
) -> tuple[_Scope, _Scope, CompleteTaskRequest]:
    values = iter((_FIRST_ID, _SECOND_ID))

    def assign(session: Session, *_args: object) -> None:
        for row in tuple(session.new):
            if isinstance(row, Sprint) and row.sprint_id is None:
                row.sprint_id = next(values)

    event.listen(Session, "before_flush", assign)
    try:
        first_sprint = _plan_and_start(
            engine,
            SprintPlanFixtureInput(
                project_id,
                (ids["US-0001"],),
                "first",
                datetime(2026, 9, 9, 9, tzinfo=UTC),
            ),
        )
        first = _Scope(
            project_id,
            first_sprint,
            ids["US-0001"],
            _task_id(engine, ids["US-0001"]),
            "first",
        )
        _finish(_app(engine, 1), first)
        second_sprint = _plan_and_start(
            engine,
            SprintPlanFixtureInput(
                project_id,
                (ids["US-0002"],),
                "second",
                datetime(2026, 9, 9, 9, 5, tzinfo=UTC),
            ),
        )
    finally:
        event.remove(Session, "before_flush", assign)
    second = _Scope(
        project_id,
        second_sprint,
        ids["US-0002"],
        _task_id(engine, ids["US-0002"]),
        "second",
    )
    return first, second, _finish(_app(engine, 6), second)


def _source(tmp_path: Path) -> _Source:
    engine = create_engine(f"sqlite:///{(tmp_path / 'retry.sqlite').as_posix()}")
    SQLModel.metadata.create_all(engine)
    project_id = seed_accepted_backlog(engine, requirements=("Retry history",))
    planning = WorkflowDomain(
        engine=engine,
        graph=planning_graph(),
        clock=FixedClock(now_value=datetime(2026, 9, 9, 8, 55, tzinfo=UTC)),
    )
    record_and_accept_roadmap(
        planning,
        project_id,
        requirements=("Retry history",),
        idempotency_suffix="-acceptance",
    )
    artifact_id, ids = _record_three(engine, planning, project_id)
    first, second, original_task = _plan_sprints(engine, project_id, ids)
    assert (first.sprint_id, second.sprint_id) == (_FIRST_ID, _SECOND_ID)
    with Session(engine) as session:
        sprints = {
            item.sprint_id: item
            for item in session.exec(select(Sprint))
            if item.sprint_id in {first.sprint_id, second.sprint_id}
        }
    assert first.sprint_id > second.sprint_id
    first_completed_at = sprints[first.sprint_id].completed_at
    second_completed_at = sprints[second.sprint_id].completed_at
    assert first_completed_at is not None
    assert second_completed_at is not None
    assert first_completed_at < second_completed_at
    rows = durable_rows(engine)
    return _Source(
        engine,
        artifact_id,
        first,
        second,
        ids["US-0003"],
        original_task,
        rows,
        dict(rows.tables["story_artifacts"]),
        {table: dict(rows.tables[table]) for table in _TABLES},
        rows.tables["user_stories"][(ids["US-0003"],)],
    )


def _preserved(source: _Source, engine: Engine) -> None:
    assert preserves_existing_rows(source.rows, durable_rows(engine))


def _preserved_journey(journey: _Journey) -> None:
    """Check durable history and the bound tree at every retry action."""
    _preserved(journey.source, journey.engine)
    assert _tree_state(journey.bound_tree) == journey.tree_before


def _guard_retry_boundaries(monkeypatch: pytest.MonkeyPatch, bound_tree: Path) -> None:
    """Make provider, Git, and cleanup use of the bound tree fail visibly."""
    message = "Retry reached an external mutation boundary."

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(message)

    def unlink_guard(path: Path, missing_ok: bool = False) -> None:
        if path.resolve().is_relative_to(bound_tree.resolve()):
            raise AssertionError(message)
        original_unlink(path, missing_ok=missing_ok)

    def rename_guard(path: Path, target: str | Path) -> Path:
        if path.resolve().is_relative_to(bound_tree.resolve()) or Path(
            target
        ).resolve().is_relative_to(bound_tree.resolve()):
            raise AssertionError(message)
        return original_rename(path, target)

    original_unlink = Path.unlink
    original_rename = Path.rename
    monkeypatch.setattr(AdkWorkflowRunner, "run_request", forbidden)
    monkeypatch.setattr(Git, "execute", forbidden)
    monkeypatch.setattr(Path, "unlink", unlink_guard)
    monkeypatch.setattr(Path, "rename", rename_guard)


def _plan_retry(
    source: _Source,
    monkeypatch: pytest.MonkeyPatch,
    bound_tree: Path,
    tree_before: dict[str, bytes],
) -> _Journey:
    message = "Retry reached an external mutation boundary."

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(message)

    monkeypatch.setattr(AgileForgeApplication, "run_agentic_action", forbidden)
    monkeypatch.setattr(ProjectLifecycleService, "attach_repository", forbidden)
    monkeypatch.setattr(ProjectLifecycleService, "refresh_repository", forbidden)
    _guard_retry_boundaries(monkeypatch, bound_tree)
    application = _app(source.engine, 10)
    preview = application.sprint_retry_preview(
        project_id=source.second.project_id, sprint_id=source.second.sprint_id
    )
    assert preview["ok"] is True
    data = preview["data"]
    assert isinstance(data, dict)
    assert data["sprint_id"] == source.second.sprint_id
    assert data["story_ids"] == [source.second.story_id]
    assert data["task_ids"] == [source.second.task_id]
    assert data["blockers"] == []
    expected = data["expected_state_fingerprint"]
    assert isinstance(expected, str)
    assert durable_rows(source.engine) == source.rows
    planned = application.retry_sprint(
        SprintRetryRequest(
            project_id=source.second.project_id,
            sprint_id=source.second.sprint_id,
            idempotency_key="retry-plan",
            actor=_ACTOR,
            confirm=True,
            expected_state_fingerprint=expected,
            rationale="Fresh evidence is required.",
        )
    )
    assert planned.ok is True
    assert type(planned.output["retry_attempt_id"]) is int
    _preserved(source, source.engine)
    assert _tree_state(bound_tree) == tree_before
    return _Journey(
        source,
        source.engine,
        planned.output["retry_attempt_id"],
        bound_tree,
        tree_before,
    )


def _apply_retry_lifecycle(journey: _Journey) -> None:
    scope = journey.scope()
    journey.engine = _reopen(journey.engine)
    application = _app(journey.engine, 11)
    assert (
        application.start_sprint(
            SprintStartRequest(
                project_id=scope.project_id,
                instance_key=scope.instance("sprint", scope.sprint_id),
                idempotency_key="retry-start",
                actor=_ACTOR,
            )
        ).ok
        is True
    )
    after_start = durable_rows(journey.engine)
    _preserved_journey(journey)
    with Session(journey.engine) as session:
        snapshot = WorkflowFactRepository(session).load(scope.project_id)
    retry = next(
        item
        for item in snapshot.sprint_retries
        if item.retry_attempt_id == journey.retry_id
    )
    fresh = resolve_execution_scope(
        snapshot, sprint_id=scope.sprint_id, retry_attempt_id=journey.retry_id
    )
    assert retry.status == fresh.status == "active"
    assert retry.task_statuses == ((scope.task_id, "To Do"),)
    assert retry.story_statuses == ((scope.story_id, "To Do"),)
    assert fresh.task_completions == ()
    assert fresh.story_completions == ()
    assert fresh.sprint_reviews == ()
    assert fresh.sprint_closures == ()
    assert fresh.post_sprint_triage == ()
    journey.engine = _reopen(journey.engine)
    replayed = _app(journey.engine, 12).complete_task(
        journey.source.original_task_request
    )
    assert replayed.ok is True
    assert replayed.replayed is True
    assert durable_rows(journey.engine) == after_start
    journey.engine = _reopen(journey.engine)
    _complete(_app(journey.engine, 13), scope)
    _preserved_journey(journey)
    journey.engine = _reopen(journey.engine)
    _close_story(_app(journey.engine, 14), scope)
    _preserved_journey(journey)
    journey.engine = _reopen(journey.engine)
    _review(_app(journey.engine, 15), scope)
    _preserved_journey(journey)
    journey.engine = _reopen(journey.engine)
    _close_sprint(_app(journey.engine, 16), scope)
    _preserved_journey(journey)
    journey.engine = _reopen(journey.engine)
    _triage(_app(journey.engine, 17), scope)
    _preserved_journey(journey)


def _assert_terminal(journey: _Journey) -> None:
    source = journey.source
    rows = durable_rows(journey.engine)
    assert rows.tables["story_artifacts"] == source.artifacts
    assert {
        table: {key: rows.tables[table][key] for key in values}
        for table, values in source.executions.items()
    } == source.executions
    assert rows.tables["user_stories"][(source.unselected_id,)] == source.unselected_row
    journey.engine = _reopen(journey.engine)
    with Session(journey.engine) as session:
        snapshot = WorkflowFactRepository(session).load(source.second.project_id)
    retry = next(
        item
        for item in snapshot.sprint_retries
        if item.retry_attempt_id == journey.retry_id
    )
    scope = resolve_execution_scope(
        snapshot, sprint_id=source.second.sprint_id, retry_attempt_id=journey.retry_id
    )
    assert retry.status == scope.status == "completed"
    assert retry.start is not None
    assert retry.task_statuses == ((source.second.task_id, "Done"),)
    assert retry.story_statuses == ((source.second.story_id, "Done"),)
    assert len(scope.task_completions) == len(scope.story_completions) == 1
    assert (
        len(scope.sprint_reviews)
        == len(scope.sprint_closures)
        == len(scope.post_sprint_triage)
        == 1
    )
    assert {item.status for item in scope.tasks} == {"Done"}
    assert {item.status for item in scope.stories} == {"Done"}
    current = current_execution_scope(snapshot)
    assert current is not None
    assert current.retry_attempt_id == journey.retry_id
    position = _domain(journey.engine, 18).position(source.second.project_id)
    required_retry_nodes = {
        "execution.sprint.retry.start",
        "execution.task.complete",
        "execution.story.close",
        "execution.sprint.review",
        "execution.sprint.close",
    }
    assert not any(
        item.category is NodeCategory.AVAILABLE
        and item.node_id in required_retry_nodes
        and item.instance_key is not None
        and item.instance_key.startswith(f"retry:{journey.retry_id}:")
        for item in position.decisions
    )
    assert source.unselected_id not in {item.story_id for item in scope.stories}
    assert {item.source_story_artifact_id for item in snapshot.stories} == {
        source.artifact_id
    }


def test_retrying_exact_second_sprint_preserves_one_artifact_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch max-ID target choice, old-row mutation, and stale receipt reuse."""
    source = _source(tmp_path)
    bound_tree = tmp_path / "repository"
    bound_tree.mkdir()
    (bound_tree / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (bound_tree / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    (bound_tree / "evidence.txt").write_text("evidence\n", encoding="utf-8")
    (bound_tree / ".git").mkdir()
    (bound_tree / ".git" / "HEAD").write_text(
        "ref: refs/heads/main\n", encoding="utf-8"
    )
    (bound_tree / ".git" / "index").write_bytes(b"representative-index\n")
    before_tree = _tree_state(bound_tree)
    monkeypatch.chdir(tmp_path)
    journey = _plan_retry(source, monkeypatch, bound_tree, before_tree)
    _apply_retry_lifecycle(journey)
    _assert_terminal(journey)
    assert _tree_state(bound_tree) == before_tree


def test_current_profile_reopens_after_upgrading_frozen_retry_schema(
    tmp_path: Path,
) -> None:
    """Catch a profile reopen that accepts source pins but loses old database rows."""
    checkout = _profile_checkout(tmp_path)
    profile = initialize_profile_record(checkout, "retry-profile")
    baseline = create_engine(f"sqlite:///{profile.business_database.as_posix()}")
    with baseline.begin() as connection:
        connection.connection.executescript(
            _PRE_RETRY_SCHEMA.read_text(encoding="utf-8")
        )
        connection.exec_driver_sql("INSERT INTO projects (name) VALUES ('retained')")
        retained_project = connection.exec_driver_sql("SELECT * FROM projects").one()
    before_manifest = profile.model_dump(mode="json")
    ensure_business_db_ready(baseline)
    baseline.dispose()
    reopened = create_engine(f"sqlite:///{profile.business_database.as_posix()}")
    try:
        assert (
            _inspect_business_schema_manifest(reopened)
            == CURRENT_BUSINESS_SCHEMA_MANIFEST
        )
        with reopened.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT * FROM projects").one()
                == retained_project
            )
    finally:
        reopened.dispose()
    loaded = load_profile(checkout, profile.name)
    assert loaded.model_dump(mode="json") == before_manifest
    validation = _verify_business_schema(profile.business_database)
    assert validation.tables == tuple(
        sorted(CURRENT_BUSINESS_SCHEMA_MANIFEST.table_names)
    )
