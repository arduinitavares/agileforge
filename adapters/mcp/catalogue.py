"""Define the complete typed allowlist for the AgileForge MCP adapter."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, cast

from mcp.types import Tool, ToolAnnotations
from pydantic import BaseModel, Field, StringConstraints

from cli.workflow_commands import render_workflow_next
from services.application import (
    AgileForgeApplication,
    CloseStoryRequest,
    CompleteTaskRequest,
    CreateProjectCommand,
    DeliveryActionRequest,
    ProductGoalResponseRequest,
    RepositoryAttachRequest,
    SpecificationSourceRegistrationRequest,
    SpecificationStructuringRequest,
    SprintPlanningRequest,
    VisionBootstrapRequest,
    VisionResponseRequest,
)
from workflow.contracts import FrozenModel, JsonObject

type PositiveIdentity = Annotated[int, Field(strict=True, gt=0)]
type SemanticText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1)
]
type ApplicationFactory = Callable[[], AgileForgeApplication]


class EmptyRequest(FrozenModel):
    """Accept no arguments for an unscoped read."""


class ProjectRequest(FrozenModel):
    """Select one existing Project explicitly."""

    project_id: PositiveIdentity


class StoryRequest(FrozenModel):
    """Select one durable Story explicitly."""

    story_id: PositiveIdentity


class SprintRequest(ProjectRequest):
    """Select a Sprint or the application's current Sprint."""

    sprint_id: PositiveIdentity | None = None


class TaskRequest(SprintRequest):
    """Select a task within the Project and optional Sprint."""

    task_id: PositiveIdentity


class ArtifactHistoryRequest(ProjectRequest):
    """Select durable attempts for one exact workflow node and instance."""

    node_id: SemanticText
    instance_key: SemanticText | None = None


class SourceRegistrationRequest(SpecificationSourceRegistrationRequest):
    """Require the same freshness inputs as the existing HTTP operation."""

    expected_decision_fingerprint: str = Field(min_length=1, exclude=True, repr=False)
    expected_source_fingerprint: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$",
        exclude=True,
        repr=False,
    )


class StructuringRequest(SpecificationStructuringRequest):
    """Require the caller's observed structuring decision."""

    expected_decision_fingerprint: str = Field(min_length=1, exclude=True, repr=False)


@dataclass(frozen=True)
class ToolDefinition:
    """Bind one advertised schema to one explicitly chosen application call."""

    name: str
    description: str
    request_model: type[BaseModel]
    read_only: bool
    execute: Callable[[dict[str, object]], JsonObject]

    def as_tool(self) -> Tool:
        """Publish typed input and structured object output without runtime access."""
        return Tool(
            name=self.name,
            description=self.description,
            inputSchema=self.request_model.model_json_schema(),
            outputSchema={"type": "object", "additionalProperties": True},
            annotations=ToolAnnotations(readOnlyHint=self.read_only),
        )


def _tool[Request: BaseModel](  # noqa: PLR0913 - fixed tool metadata and typed operation
    name: str,
    request_model: type[Request],
    operation: Callable[[AgileForgeApplication, Request], JsonObject | BaseModel],
    application_factory: ApplicationFactory,
    description: str,
    *,
    read_only: bool = True,
) -> ToolDefinition:
    """Validate a fixed typed request before obtaining the shared application."""

    def execute(arguments: dict[str, object]) -> JsonObject:
        request = request_model.model_validate_json(json.dumps(arguments), strict=True)
        result = operation(application_factory(), request)
        return (
            result.model_dump(mode="json") if isinstance(result, BaseModel) else result
        )

    return ToolDefinition(name, description, request_model, read_only, execute)


def build_catalogue(
    application_factory: ApplicationFactory,
    identity_factory: Callable[[], dict[str, object]],
) -> tuple[ToolDefinition, ...]:
    """Expose only the approved read, setup, draft, and submission operations."""

    def identity(arguments: dict[str, object]) -> JsonObject:
        EmptyRequest.model_validate(arguments)
        return cast("JsonObject", identity_factory())

    return (
        ToolDefinition(
            "service_identity",
            "Read this runtime's service and selected-state identity.",
            EmptyRequest,
            True,
            identity,
        ),
        _tool(
            "project_list",
            EmptyRequest,
            lambda app, _req: app.reads.project_list(),
            application_factory,
            "List durable Projects without generation.",
        ),
        _tool(
            "project_show",
            ProjectRequest,
            lambda app, req: app.reads.project_show(project_id=req.project_id),
            application_factory,
            "Read one Project and its durable identity.",
        ),
        _tool(
            "repository_status",
            ProjectRequest,
            lambda app, req: app.reads.repository_status(project_id=req.project_id),
            application_factory,
            "Read the Project's repository provenance.",
        ),
        _tool(
            "vision_status",
            ProjectRequest,
            lambda app, req: app.reads.vision_status(project_id=req.project_id),
            application_factory,
            "Read accepted and current Vision state.",
        ),
        _tool(
            "product_goal_status",
            ProjectRequest,
            lambda app, req: app.reads.product_goal_status(project_id=req.project_id),
            application_factory,
            "Read the active and accepted Product Goal state.",
        ),
        _tool(
            "specification_status",
            ProjectRequest,
            lambda app, req: app.reads.specification_status(project_id=req.project_id),
            application_factory,
            "Read current source and accepted Specification state.",
        ),
        _tool(
            "specification_review",
            ProjectRequest,
            lambda app, req: app.reads.specification_review(project_id=req.project_id),
            application_factory,
            "Read pending Specification review; humans decide in the UI.",
        ),
        _tool(
            "backlog_review",
            ProjectRequest,
            lambda app, req: app.backlog_review(req.project_id),
            application_factory,
            "Read pending Backlog review and exact candidate identity.",
        ),
        _tool(
            "roadmap_review",
            ProjectRequest,
            lambda app, req: app.roadmap_review(req.project_id),
            application_factory,
            "Read pending Roadmap review and exact candidate identity.",
        ),
        _tool(
            "story_reviews",
            ProjectRequest,
            lambda app, req: app.story_reviews(req.project_id),
            application_factory,
            "Read current Story reviews without deciding them.",
        ),
        _tool(
            "sprint_plan_review",
            ProjectRequest,
            lambda app, req: app.sprint_plan_review(req.project_id),
            application_factory,
            "Read the current Sprint Plan review and binding.",
        ),
        _tool(
            "accepted_roadmap",
            ProjectRequest,
            lambda app, req: app.reads.accepted_roadmap(project_id=req.project_id),
            application_factory,
            "Read the current accepted Roadmap.",
        ),
        _tool(
            "artifact_history",
            ArtifactHistoryRequest,
            lambda app, req: app.reads.artifact_history(
                project_id=req.project_id,
                node_id=req.node_id,
                instance_key=req.instance_key,
            ),
            application_factory,
            "Read durable attempt, outcome, and artifact lineage.",
        ),
        _tool(
            "story_pending",
            ProjectRequest,
            lambda app, req: app.reads.story_pending(project_id=req.project_id),
            application_factory,
            "Read pending Story coverage and readiness.",
        ),
        _tool(
            "story_show",
            StoryRequest,
            lambda app, req: app.reads.story_show(story_id=req.story_id),
            application_factory,
            "Read one Story and its canonical readiness facts.",
        ),
        _tool(
            "sprint_status",
            SprintRequest,
            lambda app, req: app.reads.sprint_status(
                project_id=req.project_id,
                sprint_id=req.sprint_id,
            ),
            application_factory,
            "Read planning and execution state for a Sprint.",
        ),
        _tool(
            "sprint_history",
            ProjectRequest,
            lambda app, req: app.reads.sprint_history(project_id=req.project_id),
            application_factory,
            "Read retained Sprint history.",
        ),
        _tool(
            "sprint_tasks",
            SprintRequest,
            lambda app, req: app.reads.sprint_tasks(
                project_id=req.project_id,
                sprint_id=req.sprint_id,
            ),
            application_factory,
            "Read task tickets for the selected Sprint.",
        ),
        _tool(
            "sprint_task_show",
            TaskRequest,
            lambda app, req: app.reads.sprint_task_show(
                project_id=req.project_id,
                task_id=req.task_id,
                sprint_id=req.sprint_id,
            ),
            application_factory,
            "Read a task and its completion evidence.",
        ),
        _tool(
            "sprint_task_history",
            TaskRequest,
            lambda app, req: app.reads.sprint_task_history(
                project_id=req.project_id,
                task_id=req.task_id,
                sprint_id=req.sprint_id,
            ),
            application_factory,
            "Read retained task logs and completion evidence.",
        ),
        _tool(
            "workflow_position",
            ProjectRequest,
            lambda app, req: app.position(project_id=req.project_id),
            application_factory,
            "Read canonical decisions, reasons, and current fingerprints.",
        ),
        _tool(
            "workflow_next",
            ProjectRequest,
            lambda app, req: cast(
                "JsonObject",
                render_workflow_next(
                    app.position(project_id=req.project_id),
                    application=app,
                ),
            ),
            application_factory,
            "Read canonical next work, waits, and required human actions.",
        ),
        _tool(
            "create_project",
            CreateProjectCommand,
            lambda app, req: app.create_project(req),
            application_factory,
            "Create a Project with caller-owned actor and idempotency key.",
            read_only=False,
        ),
        _tool(
            "attach_repository",
            RepositoryAttachRequest,
            lambda app, req: app.attach_repository(req),
            application_factory,
            "Attach repository provenance through the existing Project operation.",
            read_only=False,
        ),
        _tool(
            "bootstrap_vision",
            VisionBootstrapRequest,
            lambda app, req: app.bootstrap_vision(req),
            application_factory,
            "Generate a Vision draft from the Project's verified repository.",
            read_only=False,
        ),
        _tool(
            "respond_to_vision",
            VisionResponseRequest,
            lambda app, req: app.respond_to_vision(req),
            application_factory,
            "Submit a Vision interview response for draft generation.",
            read_only=False,
        ),
        _tool(
            "respond_to_product_goal",
            ProductGoalResponseRequest,
            lambda app, req: app.respond_to_product_goal(req),
            application_factory,
            "Submit a Product Goal interview response for draft generation.",
            read_only=False,
        ),
        _tool(
            "register_specification_source",
            SourceRegistrationRequest,
            lambda app, req: app.register_specification_source(req),
            application_factory,
            "Register repository-bound Specification sources with observed freshness.",
            read_only=False,
        ),
        _tool(
            "structure_specification",
            StructuringRequest,
            lambda app, req: app.structure_specification(req),
            application_factory,
            "Generate a Specification draft from the observed current source.",
            read_only=False,
        ),
        _tool(
            "generate_backlog",
            DeliveryActionRequest,
            lambda app, req: app.generate_backlog(req),
            application_factory,
            "Generate a Backlog draft for existing human review.",
            read_only=False,
        ),
        _tool(
            "generate_roadmap",
            DeliveryActionRequest,
            lambda app, req: app.generate_roadmap(req),
            application_factory,
            "Generate a Roadmap draft for existing human review.",
            read_only=False,
        ),
        _tool(
            "generate_story",
            DeliveryActionRequest,
            lambda app, req: app.generate_story(req),
            application_factory,
            "Generate the exact selected Story draft for human review.",
            read_only=False,
        ),
        _tool(
            "generate_sprint",
            SprintPlanningRequest,
            lambda app, req: app.generate_sprint(req),
            application_factory,
            "Generate a Sprint Plan draft for existing human review.",
            read_only=False,
        ),
        _tool(
            "complete_task",
            CompleteTaskRequest,
            lambda app, req: app.complete_task(req),
            application_factory,
            "Submit work and exact checklist evidence for an identified task.",
            read_only=False,
        ),
        _tool(
            "close_story",
            CloseStoryRequest,
            lambda app, req: app.close_story(req),
            application_factory,
            "Submit Story completion evidence through the guarded application.",
            read_only=False,
        ),
    )
