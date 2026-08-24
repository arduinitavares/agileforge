"""Closed human and agent presentation for direct-Spec execution packets."""

# ruff: noqa: EM101

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Literal, cast

from services.packets.canonical import CanonicalPacketError, validate_canonical_packet

if TYPE_CHECKING:
    from workflow.contracts import JsonObject, JsonValue

PacketFlavor = Literal["human", "agent"]


class PacketRenderError(RuntimeError):
    """Closed renderer failure with one public packet error code."""

    def __init__(self, code: str, message: str) -> None:
        """Retain the transport-safe renderer error classification."""
        super().__init__(message)
        self.code = code


def _mapping(value: JsonValue | None) -> JsonObject:
    return cast("JsonObject", value)


def _items(value: JsonValue | None) -> list[JsonValue]:
    return cast("list[JsonValue]", value)


def _text(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def _validate(packet: JsonObject, flavor: str) -> tuple[PacketFlavor, str]:
    if flavor not in {"human", "agent"}:
        message = "Packet flavor must be exactly 'human' or 'agent'."
        raise PacketRenderError("PACKET_FLAVOR_UNSUPPORTED", message)
    try:
        validate_canonical_packet(packet)
    except CanonicalPacketError as error:
        raise PacketRenderError(error.code, str(error)) from error
    return cast("PacketFlavor", flavor), cast("str", packet["packet_kind"])


def _human_specification(evidence: JsonObject) -> list[str]:
    specification = _mapping(evidence.get("specification"))
    lines = [f"Specification: {_text(specification.get('currentness'))}"]
    for item in _items(specification.get("items")):
        value = _mapping(item)
        lines.extend(
            [
                f"- {_text(value.get('title'))}: {_text(value.get('statement'))}",
                f"  Level: {_text(value.get('level'))}",
                "  Acceptance criteria:",
                *(
                    f"  - {_text(criterion)}"
                    for criterion in _items(value.get("acceptance_criteria"))
                ),
                f"  Verification: {_text(value.get('verification_method'))}",
            ]
        )
    return lines


def _human(packet: JsonObject, kind: str) -> str:
    context = _mapping(packet.get("context"))
    project = _mapping(context.get("project"))
    sprint = _mapping(context.get("sprint"))
    evidence = _mapping(packet.get("evidence"))
    work = _mapping(packet.get("work"))
    story = _mapping(work.get("story"))
    lines = [
        f"# {_text(project.get('name'))}",
        "",
        f"Sprint: {_text(sprint.get('goal'))}",
        f"Status: {_text(sprint.get('status'))}",
        f"Team: {_text(sprint.get('team_name'))}",
        "",
        f"## Story: {_text(story.get('title'))}",
        _text(story.get("statement")),
        "",
        "Story acceptance criteria:",
        *(f"- {_text(item)}" for item in _items(story.get("acceptance_criteria"))),
        "",
        "## Exact evidence",
        *_human_specification(evidence),
    ]
    backlog = _mapping(evidence.get("backlog_item"))
    release = _mapping(evidence.get("roadmap_release"))
    selected = _mapping(evidence.get("sprint_plan_story"))
    lines.extend(
        [
            f"Backlog requirement: {_text(backlog.get('requirement'))}",
            (
                f"Roadmap release: {_text(release.get('release_name'))} — "
                f"{_text(release.get('reasoning'))}"
            ),
            f"Selection reason: {_text(selected.get('reason_for_selection'))}",
            "",
        ]
    )
    if kind == "task":
        task = _mapping(work.get("task"))
        metadata = _mapping(task.get("metadata"))
        lines.extend(
            [
                "## Task",
                _text(task.get("description")),
                f"Status: {_text(task.get('status'))}",
                f"Assignee: {_text(task.get('assignee_name'))}",
                "Task checklist:",
                *(
                    f"- [ ] {_text(item)}"
                    for item in _items(metadata.get("checklist_items"))
                ),
            ]
        )
    else:
        lines.append("## Planned tasks")
        for task_value in _items(work.get("tasks")):
            task = _mapping(task_value)
            lines.append(f"- {_text(task.get('description'))}")
    return "\n".join(lines).strip() + "\n"


def _agent(packet: JsonObject, kind: str) -> str:
    context = _mapping(packet.get("context"))
    project = _mapping(context.get("project"))
    sprint = _mapping(context.get("sprint"))
    evidence = _mapping(packet.get("evidence"))
    specification = _mapping(evidence.get("specification"))
    work = _mapping(packet.get("work"))
    story = _mapping(work.get("story"))
    lines = [
        "<execution_packet>",
        f"<project>{_text(project.get('name'))}</project>",
        (
            f'<sprint goal="{html.escape(str(sprint.get("goal") or ""), quote=True)}" '
            f'status="{_text(sprint.get("status"))}" />'
        ),
        f'<specification currentness="{_text(specification.get("currentness"))}">',
    ]
    for item in _items(specification.get("items")):
        value = _mapping(item)
        title = html.escape(str(value.get("title") or ""), quote=True)
        level = html.escape(str(value.get("level") or "none"), quote=True)
        verification = html.escape(
            str(value.get("verification_method") or "none"),
            quote=True,
        )
        lines.extend(
            [
                (
                    f'<item id="{_text(value.get("spec_item_id"))}" '
                    f'title="{title}" level="{level}" '
                    f'verification_method="{verification}">'
                ),
                f"<statement>{_text(value.get('statement'))}</statement>",
                "<acceptance_criteria>",
                *(
                    f"<criterion>{_text(criterion)}</criterion>"
                    for criterion in _items(value.get("acceptance_criteria"))
                ),
                "</acceptance_criteria>",
                "</item>",
            ]
        )
    lines.extend(
        [
            "</specification>",
            f'<story title="{html.escape(str(story.get("title") or ""), quote=True)}">',
            f"<statement>{_text(story.get('statement'))}</statement>",
            "<acceptance_criteria>",
            *(
                f"<criterion>{_text(item)}</criterion>"
                for item in _items(story.get("acceptance_criteria"))
            ),
            "</acceptance_criteria>",
            "</story>",
        ]
    )
    task_values = [work.get("task")] if kind == "task" else _items(work.get("tasks"))
    lines.append("<tasks>")
    for task_value in task_values:
        task = _mapping(task_value)
        metadata = _mapping(task.get("metadata"))
        lines.extend(
            [
                f'<task kind="{_text(metadata.get("task_kind"))}">',
                f"<description>{_text(task.get('description'))}</description>",
                "<checklist>",
                *(
                    f"<item>{_text(item)}</item>"
                    for item in _items(metadata.get("checklist_items"))
                ),
                "</checklist>",
                "</task>",
            ]
        )
    lines.extend(["</tasks>", "</execution_packet>"])
    return "\n".join(lines) + "\n"


def render_packet(packet: JsonObject, flavor: str) -> str:
    """Render exactly one of the two closed packet flavors."""
    normalized, kind = _validate(packet, flavor)
    return _human(packet, kind) if normalized == "human" else _agent(packet, kind)


__all__ = [
    "PacketRenderError",
    "render_packet",
]
