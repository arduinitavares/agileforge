"""Durable non-routing projections for production transports."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import TypeAdapter
from sqlmodel import Session, col, select

from models.core import Product, Sprint, UserStory
from models.events import TaskExecutionLog
from models.workflow import WorkflowNodeAttempt, WorkflowNodeAttemptOutcome
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.agent_workbench.authority_projection import AuthorityProjectionService
from services.agent_workbench.authority_review import AuthorityReviewService
from workflow.contracts import JsonObject, JsonValue

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.engine import Engine

    from workflow.facts import SprintFact, WorkflowFactSnapshot

_JSON_OBJECT = TypeAdapter(JsonObject)


def _success(data: JsonObject) -> JsonObject:
    return {"ok": True, "data": data, "warnings": [], "errors": []}


def _error(code: str, message: str, **details: JsonValue) -> JsonObject:
    return {
        "ok": False,
        "data": details,
        "warnings": [],
        "errors": [{"code": code, "message": message, "details": details}],
    }


def _validated(value: object) -> JsonObject:
    return _JSON_OBJECT.validate_python(value)


def _iso(value: object) -> str | None:
    isoformat = getattr(value, "isoformat", None)
    if not callable(isoformat):
        return None
    rendered = isoformat()
    return rendered if isinstance(rendered, str) else None


def _enum_value(value: object) -> JsonValue:
    candidate = getattr(value, "value", value)
    if candidate is None or isinstance(candidate, str | int | float | bool):
        return candidate
    return str(candidate)


def _result_data(result: JsonObject) -> JsonObject:
    data = result.get("data")
    return data if isinstance(data, dict) else {}


class DurableReadProjectionService:
    """Read supported operator views without deriving workflow availability."""

    def __init__(self, *, engine: Engine) -> None:
        """Bind durable records and existing read-only authority projections."""
        self._engine = engine
        self._authority = AuthorityProjectionService(engine=engine)
        self._authority_review = AuthorityReviewService(engine=engine)

    def project_list(self) -> JsonObject:
        """Return durable Project identities and aggregate counts."""
        with Session(self._engine) as session:
            products = session.exec(
                select(Product).order_by(col(Product.product_id))
            ).all()
            stories = session.exec(select(UserStory)).all()
            sprints = session.exec(select(Sprint)).all()
        items: list[JsonValue] = []
        for product in products:
            project_id = product.product_id
            if project_id is None:
                continue
            items.append(
                {
                    "id": project_id,
                    "product_id": project_id,
                    "name": product.name,
                    "origin": product.origin,
                    "description": product.description,
                    "user_stories_count": sum(
                        1
                        for story in stories
                        if story.product_id == project_id and not story.is_superseded
                    ),
                    "sprint_count": sum(
                        1 for sprint in sprints if sprint.product_id == project_id
                    ),
                    "updated_at": _iso(product.updated_at),
                }
            )
        return _success({"items": items, "count": len(items)})

    def project_show(self, *, project_id: int) -> JsonObject:
        """Return one Project detail without routing state."""
        with Session(self._engine) as session:
            product = session.get(Product, project_id)
            if product is None:
                return _error(
                    "PROJECT_NOT_FOUND",
                    f"Project {project_id} was not found.",
                    project_id=project_id,
                )
            stories = session.exec(
                select(UserStory).where(col(UserStory.product_id) == project_id)
            ).all()
            sprints = session.exec(
                select(Sprint).where(col(Sprint.product_id) == project_id)
            ).all()
        return _success(
            {
                "id": project_id,
                "product_id": project_id,
                "name": product.name,
                "origin": product.origin,
                "description": product.description,
                "vision_present": bool(product.vision),
                "roadmap_present": bool(product.roadmap),
                "spec_file_path": product.spec_file_path,
                "structure_counts": {
                    "user_stories": sum(
                        1 for story in stories if not story.is_superseded
                    ),
                    "sprints": len(sprints),
                },
                "updated_at": _iso(product.updated_at),
            }
        )

    def authority_status(self, *, project_id: int) -> JsonObject:
        """Delegate to the durable authority projection."""
        return _validated(self._authority.status(project_id=project_id))

    def authority_invariants(
        self,
        *,
        project_id: int,
        spec_version_id: int | None = None,
    ) -> JsonObject:
        """Delegate to the durable invariant projection."""
        return _validated(
            self._authority.invariants(
                project_id=project_id,
                spec_version_id=spec_version_id,
            )
        )

    def authority_review(
        self,
        *,
        project_id: int,
        include_spec: str = "auto",
    ) -> JsonObject:
        """Return the durable pending-authority review packet."""
        return _validated(
            self._authority_review.review(
                project_id=project_id,
                include_spec=include_spec,
                output_format="json",
            )
        )

    def artifact_history(
        self,
        *,
        project_id: int,
        node_id: str,
        instance_key: str | None = None,
    ) -> JsonObject:
        """Return durable attempt and outcome history for one exact node."""
        with Session(self._engine) as session:
            statement = select(WorkflowNodeAttempt).where(
                col(WorkflowNodeAttempt.project_id) == project_id,
                col(WorkflowNodeAttempt.node_id) == node_id,
            )
            if instance_key is not None:
                statement = statement.where(
                    col(WorkflowNodeAttempt.instance_key) == instance_key
                )
            attempts = session.exec(
                statement.order_by(
                    col(WorkflowNodeAttempt.workflow_node_attempt_id).desc()
                )
            ).all()
            outcomes = session.exec(
                select(WorkflowNodeAttemptOutcome).where(
                    col(WorkflowNodeAttemptOutcome.project_id) == project_id
                )
            ).all()
        outcome_by_attempt = {item.workflow_node_attempt_id: item for item in outcomes}
        items: list[JsonValue] = []
        for attempt in attempts:
            attempt_id = attempt.workflow_node_attempt_id
            outcome = (
                outcome_by_attempt.get(attempt_id) if attempt_id is not None else None
            )
            output: JsonObject | None = None
            if outcome is not None and outcome.output_json is not None:
                output = _JSON_OBJECT.validate_json(outcome.output_json)
            items.append(
                {
                    "attempt_id": attempt_id,
                    "node_id": attempt.node_id,
                    "instance_key": attempt.instance_key,
                    "decision_fingerprint": attempt.decision_fingerprint,
                    "input_fingerprint": attempt.input_fingerprint,
                    "model_id": attempt.model_id,
                    "actor": attempt.actor,
                    "correlation_id": attempt.correlation_id,
                    "started_at": _iso(attempt.started_at),
                    "lease_expires_at": _iso(attempt.lease_expires_at),
                    "status": outcome.status if outcome is not None else "in_progress",
                    "output": output,
                    "output_fingerprint": (
                        outcome.output_fingerprint if outcome is not None else None
                    ),
                    "failure_code": (
                        outcome.failure_code if outcome is not None else None
                    ),
                    "failure_message": (
                        outcome.failure_message if outcome is not None else None
                    ),
                }
            )
        return _success(
            {
                "project_id": project_id,
                "node_id": node_id,
                "instance_key": instance_key,
                "items": items,
                "count": len(items),
            }
        )

    def story_show(self, *, story_id: int) -> JsonObject:
        """Return one durable Story record."""
        with Session(self._engine) as session:
            story = session.get(UserStory, story_id)
        if story is None:
            return _error(
                "STORY_NOT_FOUND",
                f"Story {story_id} was not found.",
                story_id=story_id,
            )
        return _success(
            {
                "story_id": story_id,
                "project_id": story.product_id,
                "title": story.title,
                "description": story.story_description,
                "acceptance_criteria": story.acceptance_criteria,
                "status": _enum_value(story.status),
                "story_points": story.story_points,
                "rank": story.rank,
                "source_requirement": story.source_requirement,
                "is_refined": story.is_refined,
                "is_superseded": story.is_superseded,
                "updated_at": _iso(story.updated_at),
            }
        )

    def story_pending(self, *, project_id: int) -> JsonObject:
        """List accepted Backlog requirements and their durable Story coverage."""
        snapshot_or_error = self._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            return snapshot_or_error
        snapshot = snapshot_or_error
        story_artifacts = {
            item.requirement_id: item
            for item in sorted(
                (
                    item
                    for item in snapshot.planning_artifacts
                    if item.artifact_type == "story"
                    and item.requirement_id is not None
                    and item.status != "superseded"
                ),
                key=lambda item: item.artifact_id,
            )
        }
        items: list[JsonValue] = []
        pending_count = 0
        for requirement in snapshot.backlog_requirements:
            artifact = story_artifacts.get(requirement.requirement_id)
            status = artifact.status if artifact is not None else "pending"
            if status != "accepted":
                pending_count += 1
            items.append(
                {
                    "requirement_id": requirement.requirement_id,
                    "requirement": requirement.requirement,
                    "rank": requirement.rank,
                    "status": status,
                    "story_artifact_id": (
                        artifact.artifact_id if artifact is not None else None
                    ),
                    "story_ids": (
                        list(artifact.story_ids) if artifact is not None else []
                    ),
                }
            )
        return _success(
            {
                "project_id": project_id,
                "items": items,
                "count": len(items),
                "pending_count": pending_count,
            }
        )

    def story_dependencies_inspect(self, *, project_id: int) -> JsonObject:
        """Return durable dependency edges and reviewed sets."""
        snapshot_or_error = self._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            return snapshot_or_error
        snapshot = snapshot_or_error
        return _success(
            {
                "project_id": project_id,
                "edges": [
                    _validated(item.model_dump(mode="json"))
                    for item in snapshot.story_dependencies
                ],
                "reviews": [
                    _validated(item.model_dump(mode="json"))
                    for item in snapshot.story_dependency_reviews
                ],
            }
        )

    def sprint_candidates(self, *, project_id: int) -> JsonObject:
        """Return Story facts currently eligible for Sprint planning."""
        snapshot_or_error = self._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            return snapshot_or_error
        items: list[JsonValue] = [
            _validated(item.model_dump(mode="json"))
            for item in snapshot_or_error.stories
            if item.sprint_candidate
        ]
        return _success({"project_id": project_id, "items": items, "count": len(items)})

    def sprint_history(self, *, project_id: int) -> JsonObject:
        """Combine durable Sprint-plan attempts with execution lifecycle facts."""
        attempts = self.artifact_history(
            project_id=project_id,
            node_id="planning.sprint.plan",
        )
        if attempts.get("ok") is not True:
            return attempts
        snapshot_or_error = self._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            return snapshot_or_error
        return _success(
            {
                "project_id": project_id,
                "attempts": _result_data(attempts).get("items", []),
                "sprints": [
                    _validated(item.model_dump(mode="json"))
                    for item in snapshot_or_error.sprints
                ],
            }
        )

    def sprint_metrics(self, *, project_id: int) -> JsonObject:
        """Return deterministic counts from durable Sprint execution facts."""
        snapshot_or_error = self._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            return snapshot_or_error
        snapshot = snapshot_or_error
        completed_ids = {
            item.sprint_id for item in snapshot.sprints if item.status == "completed"
        }
        return _success(
            {
                "project_id": project_id,
                "sprint_count": len(snapshot.sprints),
                "completed_sprint_count": len(completed_ids),
                "task_count": len(snapshot.tasks),
                "completed_task_count": len(snapshot.task_completions),
                "story_completion_count": len(snapshot.story_completions),
            }
        )

    def sprint_status(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return one selected Sprint and its durable execution facts."""
        snapshot_or_error = self._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            return snapshot_or_error
        snapshot = snapshot_or_error
        sprint = self._select_sprint(snapshot.sprints, sprint_id)
        if sprint is None:
            return _error(
                "SPRINT_NOT_FOUND",
                "No matching Sprint was found.",
                project_id=project_id,
                sprint_id=sprint_id,
            )
        selected_id = sprint.sprint_id
        return _success(
            {
                "project_id": project_id,
                "sprint": _validated(sprint.model_dump(mode="json")),
                "start": next(
                    (
                        _validated(item.model_dump(mode="json"))
                        for item in snapshot.sprint_starts
                        if item.sprint_id == selected_id
                    ),
                    None,
                ),
                "tasks": [
                    _validated(item.model_dump(mode="json"))
                    for item in snapshot.tasks
                    if item.sprint_id == selected_id
                ],
                "review": next(
                    (
                        _validated(item.model_dump(mode="json"))
                        for item in snapshot.sprint_reviews
                        if item.sprint_id == selected_id
                    ),
                    None,
                ),
                "closure": next(
                    (
                        _validated(item.model_dump(mode="json"))
                        for item in snapshot.sprint_closures
                        if item.sprint_id == selected_id
                    ),
                    None,
                ),
            }
        )

    def sprint_tasks(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return task tickets for one selected Sprint."""
        status = self.sprint_status(project_id=project_id, sprint_id=sprint_id)
        if status.get("ok") is not True:
            return status
        data = _result_data(status)
        sprint = data.get("sprint")
        selected_id = sprint.get("sprint_id") if isinstance(sprint, dict) else None
        tasks = data.get("tasks")
        items = tasks if isinstance(tasks, list) else []
        return _success(
            {
                "project_id": project_id,
                "sprint_id": selected_id,
                "items": items,
                "count": len(items),
            }
        )

    def sprint_task_show(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return one durable task ticket and completion evidence."""
        snapshot_or_error = self._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            return snapshot_or_error
        snapshot = snapshot_or_error
        task = next(
            (
                item
                for item in snapshot.tasks
                if item.task_id == task_id
                and (sprint_id is None or item.sprint_id == sprint_id)
            ),
            None,
        )
        if task is None:
            return _error(
                "TASK_NOT_FOUND",
                f"Task {task_id} was not found in the selected Sprint.",
                project_id=project_id,
                sprint_id=sprint_id,
                task_id=task_id,
            )
        completion = next(
            (item for item in snapshot.task_completions if item.task_id == task_id),
            None,
        )
        return _success(
            {
                "project_id": project_id,
                "task": _validated(task.model_dump(mode="json")),
                "completion": (
                    _validated(completion.model_dump(mode="json"))
                    if completion is not None
                    else None
                ),
            }
        )

    def sprint_task_history(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return retained task logs plus graph completion evidence."""
        detail = self.sprint_task_show(
            project_id=project_id,
            task_id=task_id,
            sprint_id=sprint_id,
        )
        if detail.get("ok") is not True:
            return detail
        with Session(self._engine) as session:
            statement = select(TaskExecutionLog).where(
                col(TaskExecutionLog.task_id) == task_id
            )
            if sprint_id is not None:
                statement = statement.where(
                    col(TaskExecutionLog.sprint_id) == sprint_id
                )
            logs = session.exec(
                statement.order_by(col(TaskExecutionLog.changed_at).desc())
            ).all()
        items: list[JsonValue] = [
            {
                "log_id": item.log_id,
                "task_id": item.task_id,
                "sprint_id": item.sprint_id,
                "old_status": _enum_value(item.old_status),
                "new_status": _enum_value(item.new_status),
                "outcome_summary": item.outcome_summary,
                "artifact_refs_json": item.artifact_refs_json,
                "acceptance_result": _enum_value(item.acceptance_result),
                "notes": item.notes,
                "changed_by": item.changed_by,
                "changed_at": _iso(item.changed_at),
            }
            for item in logs
        ]
        return _success(
            {
                "project_id": project_id,
                "task": _result_data(detail).get("task"),
                "completion": _result_data(detail).get("completion"),
                "items": items,
                "count": len(items),
            }
        )

    def sprint_review(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return review, closure, and triage facts for one Sprint."""
        status = self.sprint_status(project_id=project_id, sprint_id=sprint_id)
        if status.get("ok") is not True:
            return status
        data = _result_data(status)
        sprint = data.get("sprint")
        selected_id = sprint.get("sprint_id") if isinstance(sprint, dict) else None
        snapshot_or_error = self._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            return snapshot_or_error
        triage: list[JsonValue] = [
            _validated(item.model_dump(mode="json"))
            for item in snapshot_or_error.post_sprint_triage
            if item.sprint_id == selected_id
        ]
        return _success(
            {
                "project_id": project_id,
                "sprint": sprint,
                "review": data.get("review"),
                "closure": data.get("closure"),
                "triage": triage,
            }
        )

    def task_packet(
        self,
        *,
        project_id: int,
        sprint_id: int,
        task_id: int,
        flavor: str | None = None,
    ) -> JsonObject:
        """Return a bounded task execution packet from durable facts."""
        detail = self.sprint_task_show(
            project_id=project_id,
            sprint_id=sprint_id,
            task_id=task_id,
        )
        if detail.get("ok") is not True:
            return detail
        task = _result_data(detail).get("task")
        story_id = task.get("story_id") if isinstance(task, dict) else None
        story = self._story_record(story_id) if isinstance(story_id, int) else None
        return _success(
            {
                "schema_version": "agileforge.task_packet.v1",
                "flavor": flavor or "default",
                "project_id": project_id,
                "sprint_id": sprint_id,
                "task": task,
                "story": story,
                "completion": _result_data(detail).get("completion"),
            }
        )

    def story_packet(
        self,
        *,
        project_id: int,
        sprint_id: int,
        story_id: int,
        flavor: str | None = None,
    ) -> JsonObject:
        """Return a bounded Story packet from durable records and task facts."""
        story = self._story_record(story_id)
        if story is None or story.get("project_id") != project_id:
            return _error(
                "STORY_NOT_FOUND",
                f"Story {story_id} was not found for Project {project_id}.",
                project_id=project_id,
                story_id=story_id,
            )
        snapshot_or_error = self._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            return snapshot_or_error
        tasks: list[JsonValue] = [
            _validated(item.model_dump(mode="json"))
            for item in snapshot_or_error.tasks
            if item.sprint_id == sprint_id and item.story_id == story_id
        ]
        return _success(
            {
                "schema_version": "agileforge.story_packet.v1",
                "flavor": flavor or "default",
                "project_id": project_id,
                "sprint_id": sprint_id,
                "story": story,
                "tasks": tasks,
            }
        )

    def context_pack(self, *, project_id: int, phase: str) -> JsonObject:
        """Return bounded non-routing context for retained automation readers."""
        project = self.project_show(project_id=project_id)
        if project.get("ok") is not True:
            return project
        authority = self.authority_status(project_id=project_id)
        return _success(
            {
                "schema_version": "agileforge.context_pack.v1",
                "phase": phase,
                "project": _result_data(project),
                "authority": _result_data(authority),
            }
        )

    def status(self, *, project_id: int) -> JsonObject:
        """Return non-routing Project orientation for operators."""
        project = self.project_show(project_id=project_id)
        if project.get("ok") is not True:
            return project
        authority = self.authority_status(project_id=project_id)
        sprint = self.sprint_status(project_id=project_id)
        return _success(
            {
                "project": _result_data(project),
                "authority": _result_data(authority),
                "sprint": _result_data(sprint) if sprint.get("ok") is True else None,
            }
        )

    def _snapshot(self, project_id: int) -> WorkflowFactSnapshot | JsonObject:
        with Session(self._engine) as session:
            try:
                return WorkflowFactRepository(session).load(project_id)
            except WorkflowFactLoadError as error:
                return _error(
                    "PROJECT_FACTS_UNAVAILABLE",
                    str(error),
                    project_id=project_id,
                )

    @staticmethod
    def _select_sprint(
        sprints: Iterable[SprintFact],
        sprint_id: int | None,
    ) -> SprintFact | None:
        items = tuple(sprints)
        if sprint_id is not None:
            return next((item for item in items if item.sprint_id == sprint_id), None)
        priorities = {"active": 0, "planned": 1, "completed": 2}
        return min(
            items,
            key=lambda item: (priorities[item.status], -item.sprint_id),
            default=None,
        )

    def _story_record(self, story_id: int) -> JsonObject | None:
        with Session(self._engine) as session:
            story = session.get(UserStory, story_id)
        if story is None:
            return None
        return {
            "story_id": story_id,
            "project_id": story.product_id,
            "title": story.title,
            "description": story.story_description,
            "acceptance_criteria": story.acceptance_criteria,
            "status": _enum_value(story.status),
            "story_points": story.story_points,
            "source_requirement": story.source_requirement,
        }


__all__ = ["DurableReadProjectionService"]
