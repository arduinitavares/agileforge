"""Condition-free CLI adapters for workflow graph positions and requests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

from workflow.contracts import (
    JsonObject,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    WorkflowPosition,
)
from workflow.requests import DecideAuthority, OpenProjectShell


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
    decision_fingerprint: str
    command: str


class WorkflowNextPayload(TypedDict):
    """Workflow-next CLI response."""

    project_id: int
    graph_version: str
    fact_fingerprint: str
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
    "abandon_project_shell": ("agileforge", "project", "abandon"),
    "abandon_scope_extension": (
        "agileforge",
        "scope",
        "extension",
        "abandon",
    ),
    "apply_story_dependencies": ("agileforge", "story", "dependencies", "apply"),
    "close_sprint": ("agileforge", "sprint", "close"),
    "close_story": ("agileforge", "story", "close"),
    "compile_authority": ("agileforge", "authority", "compile"),
    "complete_task": ("agileforge", "sprint", "task", "complete"),
    "decide_amendment_spec_draft": (
        "agileforge",
        "scope",
        "extension",
        "spec",
        "decide",
    ),
    "decide_authority": ("agileforge", "authority", "decide"),
    "decide_backlog": ("agileforge", "backlog", "decide"),
    "decide_brownfield_initial_spec": (
        "agileforge",
        "brownfield",
        "spec",
        "decide",
    ),
    "decide_extension_prd": (
        "agileforge",
        "scope",
        "extension",
        "prd",
        "decide",
    ),
    "decide_initial_spec_draft": (
        "agileforge",
        "discovery",
        "spec",
        "decide",
    ),
    "decide_prd": ("agileforge", "discovery", "prd", "decide"),
    "decide_roadmap": ("agileforge", "roadmap", "decide"),
    "decide_sprint_plan": ("agileforge", "sprint", "decide"),
    "decide_story": ("agileforge", "story", "decide"),
    "decide_vision": ("agileforge", "vision", "decide"),
    "reconcile_backlog": ("agileforge", "backlog", "reconcile"),
    "reconcile_scope_extension": (
        "agileforge",
        "scope",
        "extension",
        "reconcile",
    ),
    "record_amendment_spec_draft": (
        "agileforge",
        "scope",
        "extension",
        "spec",
        "record",
    ),
    "record_authority_feedback": ("agileforge", "authority", "feedback"),
    "record_backlog_draft": ("agileforge", "backlog", "generate"),
    "record_brownfield_spec_draft": (
        "agileforge",
        "brownfield",
        "curate",
    ),
    "record_challenge_artifact": (
        "agileforge",
        "discovery",
        "challenge",
        "record",
    ),
    "record_extension_challenge": (
        "agileforge",
        "scope",
        "extension",
        "challenge",
        "record",
    ),
    "record_extension_prd": (
        "agileforge",
        "scope",
        "extension",
        "prd",
        "record",
    ),
    "record_initial_spec_draft": (
        "agileforge",
        "discovery",
        "spec",
        "record",
    ),
    "record_post_sprint_triage": ("agileforge", "sprint", "triage"),
    "record_prd_version": ("agileforge", "discovery", "prd", "record"),
    "record_repository_baseline": (
        "agileforge",
        "brownfield",
        "baseline",
        "record",
    ),
    "record_repository_inventory": (
        "agileforge",
        "brownfield",
        "inventory",
        "record",
    ),
    "record_roadmap_draft": ("agileforge", "roadmap", "generate"),
    "record_sprint_plan": ("agileforge", "sprint", "generate"),
    "record_story_draft": ("agileforge", "story", "generate"),
    "record_vision_draft": ("agileforge", "vision", "generate"),
    "register_initial_scope": ("agileforge", "scope", "register"),
    "register_scope_extension": (
        "agileforge",
        "scope",
        "extension",
        "register",
    ),
    "repair_authority": ("agileforge", "authority", "repair"),
    "repair_story_readiness": ("agileforge", "story", "readiness", "repair"),
    "review_sprint": ("agileforge", "sprint", "review"),
    "start_scope_extension": ("agileforge", "scope", "extension", "start"),
    "start_sprint": ("agileforge", "sprint", "start"),
}

AGENTIC_REQUEST_KINDS = frozenset(
    {
        "record_brownfield_spec_draft",
        "compile_authority",
        "repair_authority",
        "record_vision_draft",
        "record_backlog_draft",
        "record_roadmap_draft",
        "record_story_draft",
        "record_sprint_plan",
    }
)


def _flag(name: str) -> str:
    return f"--{name.replace('_', '-')}"


def _render_command(
    prefix: tuple[str, ...],
    *,
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
            "--graph-version",
            position.graph_version,
            "--expected-fact-fingerprint",
            position.fact_fingerprint,
            "--expected-decision-fingerprint",
            decision.decision_fingerprint,
        ]
        if decision.instance_key is not None:
            command.extend(("--instance-key", decision.instance_key))
        agentic = request_kind in AGENTIC_REQUEST_KINDS
        payload_flag = "--input-file" if agentic else "--request-file"
        payload_name = "<input-file>" if agentic else "<request-file>"
        command.extend((payload_flag, payload_name))
        command.extend(
            (
                "--idempotency-key",
                "<idempotency-key>",
                "--changed-by",
                "<actor>",
            )
        )
        return tuple(command)

    return render


COMMAND_RENDERERS = CommandRendererRegistry(
    tuple(
        CommandRenderer(
            request_kind=kind,
            render=_render_command(prefix, request_kind=kind),
        )
        for kind, prefix in COMMAND_PREFIXES.items()
    )
)


@dataclass(frozen=True)
class ProjectShellArguments:
    """CLI fields for one Project Shell request."""

    name: str
    origin: Literal["greenfield", "brownfield"]
    idempotency_key: str
    changed_by: str
    correlation_id: str | None = None


@dataclass(frozen=True)
class AuthorityDecisionArguments:
    """CLI fields for one exact authority decision request."""

    project_id: int
    graph_version: str
    expected_fact_fingerprint: str
    expected_decision_fingerprint: str
    idempotency_key: str
    changed_by: str
    correlation_id: str | None
    pending_authority_id: int
    authority_fingerprint: str
    review_fingerprint: str
    decision: Literal["accepted", "rejected"]
    rationale: str


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
        "decision_fingerprint": decision.decision_fingerprint,
        "command": " ".join(COMMAND_RENDERERS.command_for(position, decision)),
    }


def render_workflow_next(position: WorkflowPosition) -> WorkflowNextPayload:
    """Render only available required and recovery decisions."""
    commands = [
        _decision_payload(position, decision)
        for decision in position.decisions
        if decision.category is NodeCategory.AVAILABLE
        and decision.recommendation_kind
        in {RecommendationKind.REQUIRED, RecommendationKind.RECOVERY}
    ]
    return {
        "project_id": position.project_id,
        "graph_version": position.graph_version,
        "fact_fingerprint": position.fact_fingerprint,
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


def build_open_project_shell_request(
    args: ProjectShellArguments,
) -> OpenProjectShell:
    """Build the pre-position Project Shell request."""
    return OpenProjectShell(
        name=args.name,
        origin=args.origin,
        idempotency_key=args.idempotency_key,
        actor=args.changed_by,
        correlation_id=args.correlation_id,
    )


def build_decide_authority_request(
    args: AuthorityDecisionArguments,
) -> DecideAuthority:
    """Build one exact guarded authority decision request."""
    return DecideAuthority(
        project_id=args.project_id,
        graph_version=args.graph_version,
        fact_fingerprint=args.expected_fact_fingerprint,
        decision_fingerprint=args.expected_decision_fingerprint,
        idempotency_key=args.idempotency_key,
        actor=args.changed_by,
        correlation_id=args.correlation_id,
        pending_authority_id=args.pending_authority_id,
        authority_fingerprint=args.authority_fingerprint,
        review_fingerprint=args.review_fingerprint,
        decision=args.decision,
        rationale=args.rationale,
    )


__all__ = [
    "AGENTIC_REQUEST_KINDS",
    "COMMAND_PREFIXES",
    "COMMAND_RENDERERS",
    "AuthorityDecisionArguments",
    "CommandRenderer",
    "CommandRendererRegistry",
    "ProjectShellArguments",
    "build_decide_authority_request",
    "build_open_project_shell_request",
    "render_workflow_next",
    "workflow_next",
    "workflow_position",
]
