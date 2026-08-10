"""Condition-free CLI adapters for workflow graph positions and requests."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypedDict

from workflow.contracts import (
    JsonObject,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    WorkflowPosition,
)


class WorkflowPositionApplication(Protocol):
    """Application query exposed to workflow CLI handlers."""

    def position(self, *, project_id: int) -> WorkflowPosition:
        """Return the current durable workflow position."""
        ...


class RenderedCommand(TypedDict):
    """One rendered CLI action for a graph decision."""

    node_id: str
    instance_key: str | None
    child_graph_id: str
    request_kind: str
    recommendation_kind: str
    reason_code: str
    command: str


class WorkflowNextPayload(TypedDict):
    """Workflow-next CLI response."""

    project_id: int
    evaluated_at: str
    commands: list[RenderedCommand]
    terminal: bool
    waiting_nodes: list[str]
    blocked_nodes: list[str]
    invalid_nodes: list[str]


type CommandRender = Callable[[WorkflowPosition, NodeDecision], tuple[str, ...]]


@dataclass(frozen=True)
class CommandRenderer:
    """Bind one closed request kind to transport spelling only."""

    request_kind: str
    render: CommandRender


class CommandRendererRegistry:
    """Render commands without evaluating workflow conditions."""

    def __init__(self, renderers: tuple[CommandRenderer, ...]) -> None:
        """Index unique request-kind renderers."""
        self._renderers = {item.request_kind: item for item in renderers}
        if len(self._renderers) != len(renderers):
            msg = "Command renderer request kinds must be unique."
            raise ValueError(msg)

    def command_for(
        self,
        position: WorkflowPosition,
        decision: NodeDecision,
    ) -> tuple[str, ...]:
        """Render one already-selected decision."""
        try:
            renderer = self._renderers[decision.request_kind]
        except KeyError as exc:
            msg = f"No command renderer for request kind {decision.request_kind!r}."
            raise LookupError(msg) from exc
        return renderer.render(position, decision)


COMMAND_PREFIXES: dict[str, tuple[str, ...]] = {
    "abandon_product_goal": ("agileforge", "goal", "abandon"),
    "apply_story_dependencies": ("agileforge", "story", "dependencies", "apply"),
    "begin_vision_revision": ("agileforge", "vision", "revision"),
    "close_sprint": ("agileforge", "sprint", "close"),
    "close_story": ("agileforge", "story", "close"),
    "compile_authority": ("agileforge", "authority", "compile"),
    "complete_task": ("agileforge", "sprint", "task", "complete"),
    "decide_authority": ("agileforge", "authority", "decide"),
    "decide_backlog": ("agileforge", "backlog", "decide"),
    "decide_product_goal_review": ("agileforge", "goal", "review"),
    "decide_roadmap": ("agileforge", "roadmap", "decide"),
    "decide_specification": ("agileforge", "specification", "review"),
    "decide_sprint_plan": ("agileforge", "sprint", "decide"),
    "decide_story": ("agileforge", "story", "decide"),
    "decide_vision_review": ("agileforge", "vision", "review"),
    "fulfill_product_goal": ("agileforge", "goal", "complete"),
    "reconcile_backlog": ("agileforge", "backlog", "reconcile"),
    "record_backlog_draft": ("agileforge", "backlog", "generate"),
    "record_discovery_artifact": ("agileforge", "discovery", "record"),
    "record_post_sprint_triage": ("agileforge", "sprint", "triage"),
    "record_product_goal_interview_turn": ("agileforge", "goal", "respond"),
    "record_roadmap_draft": ("agileforge", "roadmap", "generate"),
    "record_specification_candidate": ("agileforge", "specification", "record"),
    "record_sprint_plan": ("agileforge", "sprint", "generate"),
    "record_story_draft": ("agileforge", "story", "generate"),
    "record_vision_interview_turn": ("agileforge", "vision", "respond"),
    "repair_authority": ("agileforge", "authority", "repair"),
    "repair_story_readiness": ("agileforge", "story", "readiness", "repair"),
    "review_sprint": ("agileforge", "sprint", "review"),
    "start_sprint": ("agileforge", "sprint", "start"),
}


def _render_command(
    prefix: tuple[str, ...],
) -> CommandRender:
    def render(
        position: WorkflowPosition,
        decision: NodeDecision,
    ) -> tuple[str, ...]:
        command = [
            *prefix,
            "--project-id",
            str(position.project_id),
        ]
        if decision.instance_key is not None:
            command.extend(("--instance-key", decision.instance_key))
        command.extend(("--request-file", "<request-file>"))
        command.extend(
            (
                "--idempotency-key",
                "<idempotency-key>",
                "--actor",
                "<actor>",
            )
        )
        return tuple(command)

    return render


_SEMANTIC_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "abandon_product_goal": ("--rationale", "<rationale>"),
    "begin_vision_revision": ("--reason", "<reason>"),
    "compile_authority": (),
    "decide_authority": (
        "--decision",
        "<decision>",
        "--rationale",
        "<rationale>",
    ),
    "decide_product_goal_review": (
        "--decision",
        "<decision>",
        "--rationale",
        "<rationale>",
    ),
    "decide_specification": (
        "--decision",
        "<decision>",
        "--rationale",
        "<rationale>",
    ),
    "decide_vision_review": (
        "--decision",
        "<decision>",
        "--rationale",
        "<rationale>",
    ),
    "fulfill_product_goal": ("--rationale", "<rationale>"),
    "record_discovery_artifact": ("--file", "<file>"),
    "record_backlog_draft": (),
    "record_product_goal_interview_turn": ("--text", "<text>"),
    "record_roadmap_draft": (),
    "record_specification_candidate": ("--file", "<file>"),
    "record_sprint_plan": (
        "--max-story-points",
        "<max-story-points>",
        "--team-name",
        "<team-name>",
    ),
    "record_story_draft": (),
    "record_vision_interview_turn": ("--text", "<text>"),
    "repair_authority": (),
}

_DELIVERY_REQUEST_KINDS = frozenset(
    {
        "record_backlog_draft",
        "record_roadmap_draft",
        "record_story_draft",
    }
)


def _render_semantic_command(
    prefix: tuple[str, ...],
    request_kind: str,
) -> CommandRender:
    def render(
        position: WorkflowPosition,
        decision: NodeDecision,
    ) -> tuple[str, ...]:
        command = [
            *prefix,
            "--project-id",
            str(position.project_id),
        ]
        if (
            request_kind in _DELIVERY_REQUEST_KINDS
            and decision.instance_key is not None
        ):
            command.extend(("--instance-key", decision.instance_key))
        command.extend(
            (
                *_SEMANTIC_ARGUMENTS[request_kind],
                "--idempotency-key",
                "<idempotency-key>",
                "--actor",
                "<actor>",
            )
        )
        return tuple(command)

    return render


COMMAND_RENDERERS = CommandRendererRegistry(
    tuple(
        CommandRenderer(
            request_kind=kind,
            render=(
                _render_semantic_command(prefix, kind)
                if kind in _SEMANTIC_ARGUMENTS
                else _render_command(prefix)
            ),
        )
        for kind, prefix in COMMAND_PREFIXES.items()
    )
)


def _decision_payload(
    position: WorkflowPosition,
    decision: NodeDecision,
) -> RenderedCommand:
    return {
        "node_id": decision.node_id,
        "instance_key": decision.instance_key,
        "child_graph_id": decision.child_graph_id,
        "request_kind": decision.request_kind,
        "recommendation_kind": decision.recommendation_kind.value,
        "reason_code": decision.reason_code,
        "command": " ".join(COMMAND_RENDERERS.command_for(position, decision)),
    }


def render_workflow_next(position: WorkflowPosition) -> WorkflowNextPayload:
    """Render only available required and recovery decisions."""
    candidates = tuple(
        decision
        for decision in position.decisions
        if decision.request_kind in COMMAND_PREFIXES
        and (
            decision.category is NodeCategory.AVAILABLE
            or (
                decision.category is NodeCategory.WAITING
                and decision.request_kind
                in {
                    "decide_authority",
                    "decide_product_goal_review",
                    "decide_specification",
                    "decide_vision_review",
                }
            )
        )
        and decision.recommendation_kind
        in {RecommendationKind.REQUIRED, RecommendationKind.RECOVERY}
    )
    semantic_counts = Counter(
        decision.request_kind
        for decision in candidates
        if decision.request_kind in _SEMANTIC_ARGUMENTS
        and decision.request_kind not in _DELIVERY_REQUEST_KINDS
    )
    commands = [
        _decision_payload(position, decision)
        for decision in candidates
        if (
            decision.request_kind not in _SEMANTIC_ARGUMENTS
            or decision.request_kind in _DELIVERY_REQUEST_KINDS
            or semantic_counts[decision.request_kind] == 1
        )
    ]
    return {
        "project_id": position.project_id,
        "evaluated_at": position.evaluated_at.isoformat(),
        "commands": commands,
        "terminal": position.terminal,
        "waiting_nodes": list(position.waiting_nodes),
        "blocked_nodes": list(position.blocked_nodes),
        "invalid_nodes": list(position.invalid_nodes),
    }


def workflow_next(
    *,
    application: WorkflowPositionApplication,
    project_id: int,
) -> WorkflowNextPayload:
    """Read one position and render its immediate required work."""
    return render_workflow_next(application.position(project_id=project_id))


def workflow_position(
    *,
    application: WorkflowPositionApplication,
    project_id: int,
    include_optional: bool,
) -> JsonObject:
    """Serialize a typed position, optionally retaining optional re-entry."""
    position = application.position(project_id=project_id)
    payload = position.model_dump(mode="json")
    if not include_optional:
        payload["decisions"] = [
            decision
            for decision in payload["decisions"]
            if decision["recommendation_kind"] != RecommendationKind.OPTIONAL_REENTRY
        ]
    return payload


__all__ = [
    "COMMAND_PREFIXES",
    "COMMAND_RENDERERS",
    "CommandRenderer",
    "CommandRendererRegistry",
    "render_workflow_next",
    "workflow_next",
    "workflow_position",
]
