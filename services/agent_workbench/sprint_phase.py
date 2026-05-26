"""Agent workbench Sprint phase command runner."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NoReturn, cast

import anyio
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from models.core import Sprint, UserStory
from models.db import get_engine
from models.enums import SprintStatus, StoryStatus, WorkflowEventType
from models.events import WorkflowEvent
from orchestrator_agent.agent_tools.sprint_planner_tool.tools import (
    save_sprint_plan_tool,
)
from repositories.product import ProductRepository
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.phases import workflow_state
from services.phases.sprint_service import (
    SprintPhaseError,
    generate_sprint_plan,
    get_sprint_history,
    save_sprint_plan,
    start_sprint_execution,
)
from services.sprint_runtime import run_sprint_agent_from_state
from services.story_dependencies import load_story_dependency_graph
from services.workflow import WorkflowService
from tools.orchestrator_tools import select_project
from utils.task_metadata import parse_task_metadata

if TYPE_CHECKING:
    from google.adk.tools import ToolContext
    from sqlmodel.sql._expression_select_cls import SelectOfScalar

    from models.core import Product
else:
    ToolContext = Any

_DEPENDENCY_ORDER_FALLBACK_INDEX = 1_000_000


@dataclass(frozen=True)
class _StoryDependencyMetadataContext:
    """Inputs needed to build per-story execution dependency metadata."""

    active_edges: dict[int, set[int]]
    downstream_edges: dict[int, set[int]]
    story_statuses: dict[int, StoryStatus]
    execution_index_by_story_id: dict[int, int]
    dependency_order_source: str


class SprintPhaseRunner:
    """Run Sprint phase commands through the same service boundary as the API."""

    def __init__(
        self,
        *,
        product_repo: ProductRepository | None = None,
        workflow_service: WorkflowService | None = None,
    ) -> None:
        """Initialize repositories for CLI Sprint commands."""
        self._product_repo = product_repo or ProductRepository()
        self._workflow_service = workflow_service or WorkflowService()

    def generate(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        user_input: str | None = None,
        selected_story_ids: list[int] | None = None,
        team_velocity_assumption: str = "Medium",
        sprint_duration_days: int = 14,
        max_story_points: int | None = None,
        include_task_decomposition: bool = True,
    ) -> dict[str, Any]:
        """Generate or refine a Sprint draft."""
        return anyio.run(
            self._generate,
            project_id,
            user_input,
            selected_story_ids,
            team_velocity_assumption,
            sprint_duration_days,
            max_story_points,
            include_task_decomposition,
        )

    def history(self, *, project_id: int) -> dict[str, Any]:
        """Return Sprint draft attempt history."""
        return anyio.run(self._history, project_id)

    def save(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        team_name: str,
        sprint_start_date: str,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Persist the current complete Sprint draft."""
        return anyio.run(
            self._save,
            project_id,
            team_name,
            sprint_start_date,
            attempt_id,
            expected_artifact_fingerprint,
            expected_state,
            idempotency_key,
        )

    def start(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Start a saved Sprint and move the workflow into execution."""
        return anyio.run(
            self._start,
            project_id,
            sprint_id,
            expected_state,
            idempotency_key,
        )

    def status(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return active/planned Sprint execution status."""
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            with Session(get_engine()) as session:
                resolved_sprint_id = self._resolve_execution_sprint_id(
                    project_id,
                    sprint_id=sprint_id,
                    session=session,
                )
                sprint = _get_saved_sprint(session, project_id, resolved_sprint_id)
                if sprint is None:
                    _raise_sprint_not_found()
                data = {
                    "project_id": project_id,
                    "sprint_id": resolved_sprint_id,
                    "sprint": _serialize_sprint_detail(
                        sprint,
                        runtime_summary=_build_sprint_runtime_summary(
                            session=session,
                            project_id=project_id,
                        ),
                    ),
                    "task_summary": _task_summary(sprint),
                }
        except SprintPhaseError as exc:
            return _phase_error(exc)
        return _data_envelope(data)

    def tasks(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return task rows for the active/planned Sprint."""
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            with Session(get_engine()) as session:
                resolved_sprint_id = self._resolve_execution_sprint_id(
                    project_id,
                    sprint_id=sprint_id,
                    session=session,
                )
                sprint = _get_saved_sprint(session, project_id, resolved_sprint_id)
                if sprint is None:
                    _raise_sprint_not_found()
                task_view = _build_sprint_task_view(
                    session=session,
                    project_id=project_id,
                    sprint=sprint,
                )
                data = {
                    "project_id": project_id,
                    "sprint_id": resolved_sprint_id,
                    "task_count": len(task_view["tasks"]),
                    "tasks": task_view["tasks"],
                    "dependency_summary": task_view["dependency_summary"],
                }
        except SprintPhaseError as exc:
            return _phase_error(exc)
        return _data_envelope(data, warnings=task_view["warnings"])

    async def _generate(  # noqa: PLR0913
        self,
        project_id: int,
        user_input: str | None,
        selected_story_ids: list[int] | None,
        team_velocity_assumption: str,
        sprint_duration_days: int,
        max_story_points: int | None,
        include_task_decomposition: bool,
    ) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await generate_sprint_plan(
                project_id=project_id,
                load_state=lambda: self._ensure_session(str(project_id)),
                save_state=lambda state: self._save_session_state(
                    str(project_id), state
                ),
                current_planned_sprint_id=self._current_planned_sprint_id(project_id),
                now_iso=_now_iso,
                run_sprint_agent=run_sprint_agent_from_state,
                failure_meta_builder=lambda source, fallback_summary=None: (
                    workflow_state.failure_meta(
                        source,
                        fallback_summary=fallback_summary,
                    )
                ),
                team_velocity_assumption=team_velocity_assumption,
                sprint_duration_days=sprint_duration_days,
                max_story_points=max_story_points,
                include_task_decomposition=include_task_decomposition,
                selected_story_ids=selected_story_ids,
                user_input=user_input,
            )
        except SprintPhaseError as exc:
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        if data.get("sprint_run_success") is False:
            return _sprint_runtime_error(project_id=project_id, data=data)
        return _data_envelope(data)

    async def _history(self, project_id: int) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await get_sprint_history(
                load_state=lambda: self._ensure_session(str(project_id)),
                save_state=lambda state: self._save_session_state(
                    str(project_id), state
                ),
                current_planned_sprint_id=self._current_planned_sprint_id(project_id),
            )
        except SprintPhaseError as exc:
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        return _data_envelope(data)

    async def _save(  # noqa: PLR0913
        self,
        project_id: int,
        team_name: str,
        sprint_start_date: str,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        try:
            data = await save_sprint_plan(
                project_id=project_id,
                load_state=lambda: self._ensure_session(str(project_id)),
                save_state=lambda state: self._save_session_state(
                    str(project_id), state
                ),
                current_planned_sprint_id=self._current_planned_sprint_id(project_id),
                now_iso=_now_iso,
                hydrate_context=self._hydrate_context,
                build_tool_context=_build_tool_context,
                save_plan_tool=save_sprint_plan_tool,
                team_name=team_name,
                sprint_start_date=sprint_start_date,
                attempt_id=attempt_id,
                expected_artifact_fingerprint=expected_artifact_fingerprint,
                expected_state=expected_state,
                idempotency_key=idempotency_key,
            )
        except SprintPhaseError as exc:
            return _phase_error(exc)
        except RuntimeError as exc:
            return _workflow_error(exc)
        return _data_envelope(data)

    async def _start(
        self,
        project_id: int,
        sprint_id: int | None,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        product = self._load_project(project_id)
        if isinstance(product, dict):
            return product

        state = await self._ensure_session(str(project_id))
        with Session(get_engine()) as session:

            def _persist_started_sprint(resolved_sprint_id: int) -> Sprint | None:
                sprint = _get_saved_sprint(session, project_id, resolved_sprint_id)
                if sprint is None:
                    return None
                if sprint.started_at is None:
                    sprint.started_at = datetime.now(UTC)
                    sprint.status = SprintStatus.ACTIVE
                    session.add(sprint)
                    session.add(
                        WorkflowEvent(
                            event_type=WorkflowEventType.SPRINT_STARTED,
                            product_id=project_id,
                            sprint_id=resolved_sprint_id,
                            session_id=str(project_id),
                            event_metadata=json.dumps(
                                {
                                    "team_id": sprint.team_id,
                                    "planned_start_date": str(sprint.start_date),
                                    "planned_end_date": str(sprint.end_date),
                                }
                            ),
                        )
                    )
                    session.commit()
                    session.refresh(sprint)
                return _get_saved_sprint(session, project_id, resolved_sprint_id)

            try:
                data = start_sprint_execution(
                    project_id=project_id,
                    sprint_id=sprint_id,
                    expected_state=expected_state,
                    idempotency_key=idempotency_key,
                    load_state=lambda: state,
                    save_state=lambda state: self._save_session_state(
                        str(project_id), state
                    ),
                    now_iso=_now_iso,
                    resolve_sprint_id=lambda: self._current_planned_sprint_id(
                        project_id
                    ),
                    load_sprint=lambda resolved_id: _get_saved_sprint(
                        session, project_id, resolved_id
                    ),
                    load_other_active=lambda resolved_id: session.exec(
                        select(Sprint).where(
                            Sprint.product_id == project_id,
                            Sprint.status == SprintStatus.ACTIVE,
                            Sprint.sprint_id != resolved_id,
                        )
                    ).first(),
                    persist_started_sprint=_persist_started_sprint,
                    build_runtime_summary=lambda: _build_sprint_runtime_summary(
                        session=session,
                        project_id=project_id,
                    ),
                    serialize_sprint=lambda sprint, runtime_summary: (
                        _serialize_sprint_detail(
                            sprint,
                            runtime_summary=runtime_summary,
                        )
                    ),
                )
            except SprintPhaseError as exc:
                return _phase_error(exc)
            except RuntimeError as exc:
                return _workflow_error(exc)
        return _data_envelope(data)

    def _load_project(self, project_id: int) -> Product | dict[str, Any]:
        product = self._product_repo.get_by_id(project_id)
        if product is not None:
            return product
        return _error_envelope(
            ErrorCode.PROJECT_NOT_FOUND,
            f"Project {project_id} not found.",
            details={"project_id": project_id},
            remediation=["Run agileforge project list."],
        )

    async def _ensure_session(self, session_id: str) -> dict[str, Any]:
        state = self._workflow_service.get_session_status(session_id) or {}
        if not state.get("fsm_state"):
            await self._workflow_service.initialize_session(session_id=session_id)
            state = self._workflow_service.get_session_status(session_id) or {}
        return state

    async def _hydrate_context(
        self,
        session_id: str,
        project_id: int,
    ) -> SimpleNamespace:
        state = await self._ensure_session(session_id)
        context = SimpleNamespace(state=dict(state), session_id=session_id)
        result = select_project(project_id, _build_tool_context(context))
        if not result.get("success"):
            raise SprintPhaseError(str(result.get("error", "Project hydration failed")))
        return context

    def _save_session_state(self, session_id: str, state: dict[str, Any]) -> None:
        self._workflow_service.update_session_status(session_id, state)

    def _current_planned_sprint_id(self, project_id: int) -> int | None:
        with Session(get_engine()) as session:
            sprint = session.exec(
                select(Sprint)
                .where(
                    Sprint.product_id == project_id,
                    Sprint.status == SprintStatus.PLANNED,
                )
                .order_by(
                    cast("Any", Sprint.updated_at).desc(),
                    cast("Any", Sprint.sprint_id).desc(),
                )
            ).first()
            return sprint.sprint_id if sprint else None

    def _current_active_sprint_id(self, project_id: int) -> int | None:
        with Session(get_engine()) as session:
            sprint = session.exec(
                select(Sprint)
                .where(
                    Sprint.product_id == project_id,
                    Sprint.status == SprintStatus.ACTIVE,
                )
                .order_by(
                    cast("Any", Sprint.started_at).desc(),
                    cast("Any", Sprint.sprint_id).desc(),
                )
            ).first()
            return sprint.sprint_id if sprint else None

    def _resolve_execution_sprint_id(
        self,
        project_id: int,
        *,
        sprint_id: int | None,
        session: Session,
    ) -> int:
        if sprint_id is not None:
            return sprint_id
        sprint = session.exec(
            select(Sprint)
            .where(
                Sprint.product_id == project_id,
                Sprint.status == SprintStatus.ACTIVE,
            )
            .order_by(
                cast("Any", Sprint.started_at).desc(),
                cast("Any", Sprint.sprint_id).desc(),
            )
        ).first()
        if sprint is None:
            sprint = session.exec(
                select(Sprint)
                .where(
                    Sprint.product_id == project_id,
                    Sprint.status == SprintStatus.PLANNED,
                )
                .order_by(
                    cast("Any", Sprint.updated_at).desc(),
                    cast("Any", Sprint.sprint_id).desc(),
                )
            ).first()
        if sprint is None or sprint.sprint_id is None:
            _raise_no_execution_sprint_found()
        return sprint.sprint_id


def _raise_sprint_not_found() -> NoReturn:
    message = "Sprint not found"
    raise SprintPhaseError(message, status_code=404)


def _raise_no_execution_sprint_found() -> NoReturn:
    message = "No active or planned Sprint found."
    raise SprintPhaseError(message, status_code=404)


def _saved_sprint_query() -> SelectOfScalar[Sprint]:
    """Return saved Sprint query with execution relationships loaded."""
    return select(Sprint).options(
        selectinload(cast("Any", Sprint.team)),
        selectinload(cast("Any", Sprint.stories)).selectinload(
            cast("Any", UserStory.tasks)
        ),
    )


def _get_saved_sprint(
    session: Session,
    project_id: int,
    sprint_id: int,
) -> Sprint | None:
    """Return a saved Sprint constrained to one project."""
    return session.exec(
        _saved_sprint_query().where(
            Sprint.product_id == project_id,
            Sprint.sprint_id == sprint_id,
        )
    ).first()


def _enum_value(value: object) -> str | None:
    """Return enum value text for JSON payloads."""
    enum_value = getattr(value, "value", value)
    return str(enum_value) if enum_value is not None else None


def _serialize_temporal(value: object) -> str | None:
    """Serialize date/datetime-like values for CLI JSON."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return value.isoformat()
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def _sorted_sprint_stories(sprint: Sprint) -> list[UserStory]:
    """Return Sprint stories in stable planning order."""
    return sorted(
        sprint.stories,
        key=lambda story: (
            story.rank or "",
            story.story_id or 0,
        ),
    )


def _serialize_sprint_story(story: UserStory) -> dict[str, Any]:
    """Return compact story detail for Sprint execution commands."""
    return {
        "story_id": story.story_id,
        "title": story.title,
        "rank": story.rank,
        "status": _enum_value(story.status),
        "story_points": story.story_points,
        "task_count": len(story.tasks),
    }


def _serialize_sprint_detail(
    sprint: Sprint,
    *,
    runtime_summary: dict[str, Any],
) -> dict[str, Any]:
    """Return Sprint detail payload for CLI execution commands."""
    stories = _sorted_sprint_stories(sprint)
    return {
        "id": sprint.sprint_id,
        "goal": sprint.goal,
        "status": _enum_value(sprint.status),
        "started_at": _serialize_temporal(sprint.started_at),
        "completed_at": _serialize_temporal(sprint.completed_at),
        "start_date": _serialize_temporal(sprint.start_date),
        "end_date": _serialize_temporal(sprint.end_date),
        "team_id": sprint.team_id,
        "team_name": sprint.team.name if sprint.team else None,
        "story_count": len(stories),
        "selected_stories": [_serialize_sprint_story(story) for story in stories],
        "runtime_summary": runtime_summary,
    }


def _build_sprint_runtime_summary(
    *,
    session: Session,
    project_id: int,
) -> dict[str, Any]:
    """Return active/planned Sprint summary for guard messaging."""
    active = session.exec(
        select(Sprint).where(
            Sprint.product_id == project_id,
            Sprint.status == SprintStatus.ACTIVE,
        )
    ).first()
    planned = session.exec(
        select(Sprint).where(
            Sprint.product_id == project_id,
            Sprint.status == SprintStatus.PLANNED,
        )
    ).first()
    return {
        "active_sprint_id": active.sprint_id if active else None,
        "planned_sprint_id": planned.sprint_id if planned else None,
    }


def _build_sprint_task_view(
    *,
    session: Session,
    project_id: int,
    sprint: Sprint,
) -> dict[str, Any]:
    """Return dependency-aware task rows and read-side diagnostics."""
    graph = load_story_dependency_graph(session, project_id=project_id)
    active_edge_count = sum(
        len(prerequisites) for prerequisites in graph.active_edges.values()
    )
    warnings: list[dict[str, Any]] = []
    ordering = "topological"

    if graph.cycle_paths:
        ordering = "rank_fallback"
        ordered_stories = _sorted_sprint_stories(sprint)
        warnings.append(
            {
                "code": "SPRINT_TASK_DEPENDENCY_CYCLE_FALLBACK",
                "message": (
                    "Active story dependencies contain a cycle; Sprint tasks are "
                    "returned using rank fallback order."
                ),
                "details": {"cycle_paths": graph.cycle_paths},
            }
        )
    else:
        ordered_stories = _topologically_sorted_sprint_stories(
            sprint,
            active_edges=graph.active_edges,
        )

    story_statuses = _story_statuses(
        session,
        sprint=sprint,
        active_edges=graph.active_edges,
    )
    current_story_ids = {
        story.story_id for story in sprint.stories if story.story_id is not None
    }
    execution_index_by_story_id = {
        story.story_id: index
        for index, story in enumerate(ordered_stories, start=1)
        if story.story_id is not None
    }
    downstream_edges = _downstream_edges(
        active_edges=graph.active_edges,
        current_story_ids=current_story_ids,
    )
    dependency_order_source = (
        "rank_fallback"
        if ordering == "rank_fallback"
        else "active_story_dependencies"
    )
    story_metadata = _story_dependency_metadata(
        ordered_stories,
        context=_StoryDependencyMetadataContext(
            active_edges=graph.active_edges,
            downstream_edges=downstream_edges,
            story_statuses=story_statuses,
            execution_index_by_story_id=execution_index_by_story_id,
            dependency_order_source=dependency_order_source,
        ),
    )
    tasks = _serialize_sprint_tasks(
        ordered_stories,
        story_dependency_metadata=story_metadata,
    )
    return {
        "tasks": tasks,
        "warnings": warnings,
        "dependency_summary": {
            "active_edge_count": active_edge_count,
            "cycle_count": len(graph.cycle_paths),
            "blocked_story_count": sum(
                1
                for metadata in story_metadata.values()
                if metadata["is_blocked"]
            ),
            "ordering": ordering,
        },
    }


def _serialize_sprint_tasks(
    stories: list[UserStory],
    *,
    story_dependency_metadata: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return Sprint task rows grouped by dependency-aware story order."""
    dependency_metadata = story_dependency_metadata or {}
    rows: list[dict[str, Any]] = []
    task_execution_order = 1
    for story in stories:
        tasks = sorted(story.tasks, key=lambda task: task.task_id or 0)
        for task in tasks:
            metadata = parse_task_metadata(
                task.metadata_json,
                task_id=task.task_id,
            )
            row = {
                "task_id": task.task_id,
                "story_id": story.story_id,
                "story_title": story.title,
                "description": task.description,
                "status": _enum_value(task.status),
                "task_kind": metadata.task_kind,
                "artifact_targets": list(metadata.artifact_targets),
                "workstream_tags": list(metadata.workstream_tags),
                "relevant_invariant_ids": list(metadata.relevant_invariant_ids),
                "checklist_items": list(metadata.checklist_items),
                "task_execution_order": task_execution_order,
            }
            if story.story_id is not None:
                row.update(dependency_metadata.get(story.story_id, {}))
            rows.append(row)
            task_execution_order += 1
    return rows


def _task_summary(sprint: Sprint) -> dict[str, Any]:
    """Return status counts for Sprint tasks."""
    counts: dict[str, int] = {}
    tasks = _serialize_sprint_tasks(_sorted_sprint_stories(sprint))
    for task in tasks:
        status = str(task.get("status") or "Unknown")
        counts[status] = counts.get(status, 0) + 1
    return {"task_count": len(tasks), "status_counts": counts}


def _topologically_sorted_sprint_stories(
    sprint: Sprint,
    *,
    active_edges: dict[int, set[int]],
) -> list[UserStory]:
    """Return Sprint stories ordered with in-sprint prerequisites first."""
    fallback_order = _sorted_sprint_stories(sprint)
    stories_by_id = {
        story.story_id: story for story in fallback_order if story.story_id is not None
    }
    story_ids = set(stories_by_id)
    fallback_index = {
        story_id: index for index, story_id in enumerate(stories_by_id, start=1)
    }
    indegree = dict.fromkeys(story_ids, 0)
    dependents_by_prerequisite: dict[int, set[int]] = {}
    for dependent_id in sorted(story_ids):
        for prerequisite_id in sorted(active_edges.get(dependent_id, set())):
            if prerequisite_id not in story_ids:
                continue
            indegree[dependent_id] += 1
            dependents_by_prerequisite.setdefault(prerequisite_id, set()).add(
                dependent_id
            )

    ready = sorted(
        [story_id for story_id, count in indegree.items() if count == 0],
        key=lambda story_id: fallback_index[story_id],
    )
    ordered_ids: list[int] = []
    while ready:
        story_id = ready.pop(0)
        ordered_ids.append(story_id)
        for dependent_id in sorted(
            dependents_by_prerequisite.get(story_id, set()),
            key=lambda item: fallback_index[item],
        ):
            indegree[dependent_id] -= 1
            if indegree[dependent_id] == 0:
                ready.append(dependent_id)
                ready.sort(key=lambda item: fallback_index[item])

    if len(ordered_ids) != len(story_ids):
        return fallback_order
    return [stories_by_id[story_id] for story_id in ordered_ids]


def _story_statuses(
    session: Session,
    *,
    sprint: Sprint,
    active_edges: dict[int, set[int]],
) -> dict[int, StoryStatus]:
    """Return statuses for current and dependency graph stories."""
    story_ids = {
        story.story_id for story in sprint.stories if story.story_id is not None
    }
    for dependent_id, prerequisite_ids in active_edges.items():
        story_ids.add(dependent_id)
        story_ids.update(prerequisite_ids)
    if not story_ids:
        return {}

    rows = session.exec(
        select(UserStory).where(cast("Any", UserStory.story_id).in_(story_ids))
    ).all()
    return {
        story.story_id: story.status for story in rows if story.story_id is not None
    }


def _downstream_edges(
    *,
    active_edges: dict[int, set[int]],
    current_story_ids: set[int],
) -> dict[int, set[int]]:
    """Return current-sprint dependents keyed by prerequisite story id."""
    downstream: dict[int, set[int]] = {}
    for dependent_id, prerequisite_ids in active_edges.items():
        if dependent_id not in current_story_ids:
            continue
        for prerequisite_id in prerequisite_ids:
            downstream.setdefault(prerequisite_id, set()).add(dependent_id)
    return downstream


def _story_dependency_metadata(
    stories: list[UserStory],
    *,
    context: _StoryDependencyMetadataContext,
) -> dict[int, dict[str, Any]]:
    """Return dependency metadata safe to attach to every task row."""
    metadata_by_story_id: dict[int, dict[str, Any]] = {}
    for story in stories:
        story_id = story.story_id
        if story_id is None:
            continue
        direct_blocked_by = sorted(context.active_edges.get(story_id, set()))
        closure = _dependency_closure(story_id, context.active_edges)
        blocked_by = sorted(
            prerequisite_id
            for prerequisite_id in closure
            if context.story_statuses.get(prerequisite_id) != StoryStatus.DONE
        )
        unblocks = sorted(
            context.downstream_edges.get(story_id, set()),
            key=lambda dependent_id: (
                context.execution_index_by_story_id.get(
                    dependent_id,
                    _DEPENDENCY_ORDER_FALLBACK_INDEX,
                ),
                dependent_id,
            ),
        )
        metadata_by_story_id[story_id] = {
            "story_execution_order": context.execution_index_by_story_id.get(story_id),
            "direct_blocked_by_story_ids": direct_blocked_by,
            "blocked_by_story_ids": blocked_by,
            "unblocks_story_ids": unblocks,
            "is_blocked": bool(blocked_by),
            "dependency_order_source": context.dependency_order_source,
        }
    return metadata_by_story_id


def _dependency_closure(
    story_id: int,
    active_edges: dict[int, set[int]],
) -> set[int]:
    """Return all transitive prerequisites for one story, cycle-safe."""
    closure: set[int] = set()
    visiting: set[int] = set()

    def visit(current_id: int) -> None:
        visiting.add(current_id)
        for prerequisite_id in sorted(active_edges.get(current_id, set())):
            if prerequisite_id not in closure:
                closure.add(prerequisite_id)
            if prerequisite_id in visiting:
                continue
            visit(prerequisite_id)
        visiting.remove(current_id)

    visit(story_id)
    closure.discard(story_id)
    return closure


def _now_iso() -> str:
    """Return canonical UTC timestamp."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _build_tool_context(context: object) -> ToolContext:
    """Return a lightweight ToolContext-compatible state holder."""
    return cast("ToolContext", context)


def _flatten_phase_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Flatten phase service payloads for CLI consumers."""
    payload: dict[str, Any] = {
        str(key): value for key, value in data.items() if key != "data"
    }
    inner = data.get("data")
    if isinstance(inner, dict):
        payload.update({str(key): value for key, value in inner.items()})
    return payload


def _data_envelope(
    data: dict[str, Any],
    *,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return application facade success envelope."""
    return {
        "ok": True,
        "data": _flatten_phase_payload(data),
        "warnings": warnings or [],
        "errors": [],
    }


def _error_envelope(
    code: ErrorCode,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    remediation: list[str] | None = None,
) -> dict[str, Any]:
    """Return application facade failure envelope."""
    return {
        "ok": False,
        "data": None,
        "warnings": [],
        "errors": [
            workbench_error(
                code,
                message=message,
                details=details or {},
                remediation=remediation or [],
            ).to_dict()
        ],
    }


def _phase_error(exc: SprintPhaseError) -> dict[str, Any]:
    """Map Sprint phase errors onto registered CLI errors."""
    message = exc.detail
    code = (
        ErrorCode.AUTHORITY_NOT_ACCEPTED
        if message.startswith("Setup required")
        else ErrorCode.INVALID_COMMAND
    )
    return _error_envelope(code, message)


def _workflow_error(exc: RuntimeError) -> dict[str, Any]:
    """Map workflow persistence errors onto registered CLI errors."""
    return _error_envelope(ErrorCode.WORKFLOW_SESSION_FAILED, str(exc))


def _sprint_runtime_error(*, project_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """Map a recorded Sprint runtime failure onto a hard CLI failure."""
    message = str(
        data.get("failure_summary") or data.get("error") or "Sprint generation failed."
    )
    details = {
        "project_id": project_id,
        "sprint_run_success": False,
        "failure_stage": data.get("failure_stage"),
        "failure_artifact_id": data.get("failure_artifact_id"),
        "attempt_count": data.get("attempt_count"),
        "fsm_state": data.get("fsm_state"),
    }
    return _error_envelope(
        ErrorCode.MUTATION_FAILED,
        message,
        details={key: value for key, value in details.items() if value is not None},
        remediation=[
            "Inspect agileforge sprint history --project-id <project_id>.",
            "Fix the Sprint runtime/provider configuration or refine the input.",
        ],
    )
