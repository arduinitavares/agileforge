"""Render packet payloads as human briefs or strict agent prompts."""

from __future__ import annotations

import html
from typing import Any, Final, cast

TASK_SCHEMA_VERSION: Final[str] = "task_packet.v2"
STORY_SCHEMA_VERSION: Final[str] = "story_packet.v1"

_TASK_AGENT_INTRO: Final[str] = (
    "You are an expert full-stack developer. Your objective is to "
    "explicitly implement the provided `<task>` without hallucinating "
    "out-of-scope features or ignoring constraints.\n"
)
_SESSION_BOOTSTRAP_NOTE: Final[str] = (
    "This prompt assumes the session was already initialized with the "
    "parent story prompt. If not, restart with Copy Story Prompt.\n"
)
_COMPLETION_REPORT_GUIDANCE: Final[str] = (
    "Use this Markdown structure for your final completion report. "
    "Preserve normal human-readable text in the final report. Do not "
    "include XML/HTML escaping in the final report unless the escaped "
    "form is part of the intended literal content.\n"
)
_TASK_EXECUTION_PROTOCOL: Final[tuple[str, ...]] = (
    "1. Start by writing a brief, visible work plan before making any changes.",
    "2. Do not expose internal reasoning, hidden chain-of-thought, or self-talk.",
    (
        "3. Communicate only observable progress: plan, implementation "
        "summary, verification results, and blockers."
    ),
    (
        "4. Treat the packet as the single source of truth: task "
        "description, task checklist, task hard constraints, and story "
        "compliance boundaries."
    ),
    "5. Verify every task checklist item before claiming completion.",
    _SESSION_BOOTSTRAP_NOTE.strip(),
)
_STORY_EXECUTION_PROTOCOL: Final[tuple[str, ...]] = (
    "1. Start by writing a brief, visible work plan before making any changes.",
    "2. Do not expose internal reasoning, hidden chain-of-thought, or self-talk.",
    (
        "3. Communicate only observable progress: plan, implementation "
        "summary, verification results, and blockers."
    ),
    (
        "4. Treat the packet as the single source of truth: story "
        "description, story acceptance criteria, task plan reference, "
        "and story compliance boundaries."
    ),
    "5. Verify every story acceptance criteria item before claiming completion.",
    (
        "6. Use the task plan as reference only; it is not the done "
        "checklist for the story."
    ),
)


def _escape_md(text: object) -> str:
    """Escape HTML and Markdown metacharacters for human-facing briefs."""
    if not text:
        return ""
    safe_str = html.escape(str(text), quote=False)
    return safe_str.replace("#", "&#35;").replace("*", "&#42;")


def _escape_xml(text: object) -> str:
    """Escape XML entities for agent-prompt sections."""
    return html.escape(str(text or ""), quote=True) if text else ""


def _as_mapping(value: object) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _as_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _schema_version(packet: dict[str, Any]) -> str:
    return str(packet.get("schema_version") or "").strip().lower()


def _task_checklist_items(packet: dict[str, Any]) -> list[Any]:
    task = _as_mapping(packet.get("task"))
    return _as_list(task.get("checklist_items"))


def _story_acceptance_criteria_items(packet: dict[str, Any]) -> list[Any]:
    constraints = _as_mapping(packet.get("constraints"))
    return _as_list(constraints.get("story_acceptance_criteria_items"))


def _story_task_plan_items(packet: dict[str, Any]) -> list[dict[str, Any]]:
    task_plan = _as_mapping(packet.get("task_plan"))
    tasks = _as_list(task_plan.get("tasks"))
    return [task for task in tasks if isinstance(task, dict)]


def _story_metadata(packet: dict[str, Any]) -> dict[str, Any]:
    if _schema_version(packet) == STORY_SCHEMA_VERSION:
        return _as_mapping(packet.get("story"))
    context = _as_mapping(packet.get("context"))
    return _as_mapping(context.get("story"))


def _render_invariants_markdown(invariants: list[Any]) -> str:
    if not invariants:
        return "* No pinned specification invariants found.\n"

    lines: list[str] = []
    for inv in invariants:
        if not isinstance(inv, dict):
            continue
        inv_type = _escape_md(inv.get("type", "RULE"))
        excerpt = _escape_md(inv.get("source_excerpt", ""))
        lines.append(f"- **[{inv_type}]** {excerpt}")
    return "\n".join(lines) + "\n"


def _task_plan_xml_line(task: dict[str, Any]) -> str:
    task_id = task.get("id", "unknown")
    description = _escape_xml(task.get("description", ""))
    return f"  - [{task_id}] {description}"


def _task_plan_md_line(task: dict[str, Any]) -> str:
    task_id = task.get("id", "unknown")
    description = _escape_md(task.get("description", ""))
    return f"- [{task_id}] {description}"


def _append_xml_bullets(
    parts: list[str],
    items: list[object],
    *,
    prefix: str,
) -> None:
    parts.extend(f"{prefix}{_escape_xml(item)}" for item in items)


def _append_md_checklist(parts: list[str], items: list[object]) -> None:
    parts.extend(f"- [ ] {_escape_md(item)}" for item in items)


def _append_xml_checklist(parts: list[str], items: list[object]) -> None:
    parts.extend(f"- [ ] {_escape_xml(item)}" for item in items)


def _append_invariant_xml_section(
    parts: list[str],
    *,
    tag: str,
    invariants: list[Any],
    empty_line: str,
) -> None:
    parts.append(f"<{tag}>")
    valid_invariants = [inv for inv in invariants if isinstance(inv, dict)]
    if valid_invariants:
        parts.extend(
            (
                f"  - [{_escape_xml(inv.get('type', 'RULE'))}] "
                f"{_escape_xml(inv.get('source_excerpt', ''))}"
            )
            for inv in valid_invariants
        )
    else:
        parts.append(empty_line)
    parts.append(f"</{tag}>\n")


def _append_execution_protocol(parts: list[str], steps: tuple[str, ...]) -> None:
    parts.append("<execution_protocol>")
    parts.extend(steps)
    parts.append("</execution_protocol>\n")


def _append_task_completion_report(
    parts: list[str],
    *,
    task: dict[str, Any],
    checklist_items: list[Any],
) -> None:
    task_label = _escape_xml(task.get("label", task.get("description", "Task")))
    parts.append("<completion_report>")
    parts.append(_COMPLETION_REPORT_GUIDANCE)
    parts.append("## Completion Report")
    parts.append(f"**Task**: {task_label}")
    parts.append("**Status**: DONE | PARTIAL | BLOCKED\n")
    parts.append("### Brief Plan")
    parts.append("- ...\n")
    parts.append("### Task Checklist")
    if checklist_items:
        _append_xml_checklist(parts, checklist_items)
    else:
        parts.append("- No task checklist items were provided in the packet.")
    parts.append("")
    parts.append("### Changes Made")
    parts.append("- [file or area]: [what changed and why]\n")
    parts.append("### Verification")
    parts.append("- [command, check, or review performed]\n")
    parts.append("### Blockers / Follow-ups")
    parts.append("- None")
    parts.append("</completion_report>")


def _append_story_completion_report(
    parts: list[str],
    *,
    story: dict[str, Any],
    ac_items: list[Any],
    task_plan_items: list[dict[str, Any]],
) -> None:
    story_label = _escape_xml(story.get("title", "Story"))
    parts.append("<completion_report>")
    parts.append(_COMPLETION_REPORT_GUIDANCE)
    parts.append("## Completion Report")
    parts.append(f"**Story**: {story_label}")
    parts.append("**Status**: DONE | PARTIAL | BLOCKED\n")
    parts.append("### Brief Plan")
    parts.append("- ...\n")
    parts.append("### Story Acceptance Criteria")
    if ac_items:
        _append_xml_checklist(parts, ac_items)
    else:
        parts.append("- No story acceptance criteria were provided in the packet.")
    parts.append("")
    parts.append("### Task Plan Reference")
    if task_plan_items:
        parts.extend(_task_plan_xml_line(task) for task in task_plan_items)
    else:
        parts.append("- No task plan reference was provided.")
    parts.append("")
    parts.append("### Changes Made")
    parts.append("- [file or area]: [what changed and why]\n")
    parts.append("### Verification")
    parts.append("- [command, check, or review performed]\n")
    parts.append("### Blockers / Follow-ups")
    parts.append("- None")
    parts.append("</completion_report>")


def _task_agent_context_lines(
    *,
    story: dict[str, Any],
    sprint: dict[str, Any],
) -> list[str]:
    lines = ["<context>"]
    if sprint.get("goal"):
        lines.append(f"  <sprint_goal>{_escape_xml(sprint.get('goal'))}</sprint_goal>")
    if story.get("title"):
        lines.append(f"  <parent_story>{_escape_xml(story.get('title'))}")
        if story.get("story_description"):
            lines.append(f"    {_escape_xml(story.get('story_description'))}")
        lines.append("  </parent_story>")
    lines.append("</context>\n")
    return lines


def _task_agent_task_lines(task: dict[str, Any]) -> list[str]:
    description = _escape_xml(task.get("description", "Unknown task"))
    return ["<task>", f"  {description}", "</task>\n"]


def _task_agent_task_context_lines(task: dict[str, Any]) -> list[str]:
    artifact_targets = _as_list(task.get("artifact_targets"))
    workstream_tags = _as_list(task.get("workstream_tags"))
    lines = [
        "<task_context>",
        f"  <task_kind>{_escape_xml(task.get('task_kind', 'other'))}</task_kind>",
        "  <artifact_targets>",
    ]
    if artifact_targets:
        _append_xml_bullets(lines, artifact_targets, prefix="    - ")
    else:
        lines.append("    (None specified)")
    lines.extend(["  </artifact_targets>", "  <workstream_tags>"])
    if workstream_tags:
        _append_xml_bullets(lines, workstream_tags, prefix="    - ")
    else:
        lines.append("    (None specified)")
    lines.extend(["  </workstream_tags>", "</task_context>\n"])
    return lines


def _task_agent_checklist_lines(checklist_items: list[Any]) -> list[str]:
    lines = ["<task_checklist>"]
    if checklist_items:
        _append_xml_bullets(lines, checklist_items, prefix="  - ")
    else:
        lines.append("  (No task checklist items provided)")
    lines.append("</task_checklist>\n")
    return lines


def _render_task_agent_prompt(packet: dict[str, Any]) -> str:
    task = _as_mapping(packet.get("task"))
    context = _as_mapping(packet.get("context"))
    constraints = _as_mapping(packet.get("constraints"))
    story = _as_mapping(context.get("story"))
    sprint = _as_mapping(context.get("sprint"))
    checklist_items = _task_checklist_items(packet)

    parts = [_TASK_AGENT_INTRO]
    parts.extend(_task_agent_context_lines(story=story, sprint=sprint))
    parts.append(_SESSION_BOOTSTRAP_NOTE)
    parts.extend(_task_agent_task_lines(task))
    parts.extend(_task_agent_task_context_lines(task))
    parts.extend(_task_agent_checklist_lines(checklist_items))

    story_boundaries = _as_list(constraints.get("story_compliance_boundaries"))
    if story_boundaries:
        _append_invariant_xml_section(
            parts,
            tag="story_compliance_boundaries",
            invariants=story_boundaries,
            empty_line="  (No story compliance boundaries identified)",
        )

    _append_invariant_xml_section(
        parts,
        tag="hard_constraints",
        invariants=_as_list(constraints.get("task_hard_constraints")),
        empty_line="  (No task-local hard constraints identified)",
    )
    _append_execution_protocol(parts, _TASK_EXECUTION_PROTOCOL)
    _append_task_completion_report(parts, task=task, checklist_items=checklist_items)
    return "\n".join(parts)


def _story_agent_context_lines(
    *,
    story: dict[str, Any],
    sprint: dict[str, Any],
    product: dict[str, Any],
) -> list[str]:
    lines = ["<context>"]
    if sprint.get("goal"):
        lines.append(f"  <sprint_goal>{_escape_xml(sprint.get('goal'))}</sprint_goal>")
    if story.get("title"):
        lines.append(f"  <story_title>{_escape_xml(story.get('title'))}</story_title>")
    if story.get("story_description"):
        description = _escape_xml(story.get("story_description"))
        lines.append(f"  <story_description>{description}</story_description>")
    if product.get("vision_excerpt"):
        vision_excerpt = _escape_xml(product.get("vision_excerpt"))
        lines.append(f"  <product_vision>{vision_excerpt}</product_vision>")
    lines.append("</context>\n")
    return lines


def _story_agent_acceptance_lines(ac_items: list[Any]) -> list[str]:
    lines = ["<story_acceptance_criteria>"]
    if ac_items:
        _append_xml_bullets(lines, ac_items, prefix="  - ")
    else:
        lines.append("  (No story acceptance criteria provided)")
    lines.append("</story_acceptance_criteria>\n")
    return lines


def _story_task_plan_reference_lines(
    task_plan_items: list[dict[str, Any]],
) -> list[str]:
    lines = ["<task_plan_reference>"]
    if task_plan_items:
        lines.extend(_task_plan_xml_line(task) for task in task_plan_items)
    else:
        lines.append("  (No task plan reference provided)")
    lines.append("</task_plan_reference>\n")
    return lines


def _render_story_agent_prompt(packet: dict[str, Any]) -> str:
    story = _as_mapping(packet.get("story"))
    context = _as_mapping(packet.get("context"))
    constraints = _as_mapping(packet.get("constraints"))
    sprint = _as_mapping(context.get("sprint"))
    product = _as_mapping(context.get("product"))
    ac_items = _story_acceptance_criteria_items(packet)
    task_plan_items = _story_task_plan_items(packet)

    parts = [
        (
            "You are bootstrapping a fresh story session. Use the story "
            "acceptance criteria to define the completion contract.\n"
        )
    ]
    parts.extend(
        _story_agent_context_lines(story=story, sprint=sprint, product=product)
    )
    parts.extend(_story_agent_acceptance_lines(ac_items))
    parts.extend(_story_task_plan_reference_lines(task_plan_items))

    story_boundaries = _as_list(constraints.get("story_compliance_boundaries"))
    if story_boundaries:
        _append_invariant_xml_section(
            parts,
            tag="story_compliance_boundaries",
            invariants=story_boundaries,
            empty_line="  (No story compliance boundaries identified)",
        )

    _append_execution_protocol(parts, _STORY_EXECUTION_PROTOCOL)
    _append_story_completion_report(
        parts,
        story=story,
        ac_items=ac_items,
        task_plan_items=task_plan_items,
    )
    return "\n".join(parts)


def _render_story_human_brief(packet: dict[str, Any]) -> str:
    story = _story_metadata(packet)
    context = _as_mapping(packet.get("context"))
    constraints = _as_mapping(packet.get("constraints"))
    sprint = _as_mapping(context.get("sprint"))
    product = _as_mapping(context.get("product"))
    ac_items = _story_acceptance_criteria_items(packet)
    task_plan_items = _story_task_plan_items(packet)

    parts = [f"# Story: {_escape_md(story.get('title', 'Story'))}"]
    if story.get("story_description"):
        parts.append(f"{_escape_md(story.get('story_description'))}\n")
    parts.append("## Story Brief")
    if sprint.get("goal"):
        parts.append(f"**Sprint Goal**: {_escape_md(sprint.get('goal'))}")
    if product.get("vision_excerpt"):
        vision_excerpt = _escape_md(product.get("vision_excerpt"))
        parts.append(f"**product vision**: {vision_excerpt}\n")

    parts.append("## Story Acceptance Criteria")
    if ac_items:
        _append_md_checklist(parts, ac_items)
    else:
        parts.append("* No story acceptance criteria provided.")
    parts.append("")

    parts.append("## Task Plan Reference")
    if task_plan_items:
        parts.extend(_task_plan_md_line(task) for task in task_plan_items)
    else:
        parts.append("* No task plan reference provided.")
    parts.append("")

    story_boundaries = _as_list(constraints.get("story_compliance_boundaries"))
    if story_boundaries:
        parts.append("## Story Compliance Boundaries")
        parts.append(_render_invariants_markdown(story_boundaries))
    return "\n".join(parts)


def _render_task_human_brief(packet: dict[str, Any]) -> str:
    task = _as_mapping(packet.get("task"))
    context = _as_mapping(packet.get("context"))
    constraints = _as_mapping(packet.get("constraints"))
    story = _story_metadata(packet)
    sprint = _as_mapping(context.get("sprint"))
    product = _as_mapping(context.get("product"))
    checklist_items = _task_checklist_items(packet)

    parts = [
        f"# Task: {_escape_md(task.get('label', 'Task'))}",
        f"{_escape_md(task.get('description', ''))}\n",
        "## Task Profile",
        f"**Task Kind**: {_escape_md(task.get('task_kind', 'other'))}",
    ]
    parts.extend(_task_profile_lines(task))
    parts.extend(_parent_story_orientation_lines(story, sprint, product))

    parts.append("## Task Checklist")
    if checklist_items:
        _append_md_checklist(parts, checklist_items)
    else:
        parts.append("* No task checklist items provided.")
    parts.append("")

    parts.append("## Task-Local Hard Constraints")
    task_constraints = _as_list(constraints.get("task_hard_constraints"))
    if task_constraints:
        parts.append(_render_invariants_markdown(task_constraints))
    else:
        parts.append("* (No task-local hard constraints identified)\n")

    story_boundaries = _as_list(constraints.get("story_compliance_boundaries"))
    if story_boundaries:
        parts.append("## Story Compliance Boundaries")
        parts.append(_render_invariants_markdown(story_boundaries))
    return "\n".join(parts)


def _task_profile_lines(task: dict[str, Any]) -> list[str]:
    artifact_targets = _as_list(task.get("artifact_targets"))
    workstream_tags = _as_list(task.get("workstream_tags"))
    lines: list[str] = []

    if artifact_targets:
        joined_targets = ", ".join(str(item) for item in artifact_targets)
        lines.append(f"**Artifact Targets**: {_escape_md(joined_targets)}")
    else:
        lines.append("**Artifact Targets**: None specified")

    if workstream_tags:
        joined_tags = ", ".join(str(item) for item in workstream_tags)
        lines.append(f"**Workstream Tags**: {_escape_md(joined_tags)}\n")
    else:
        lines.append("**Workstream Tags**: None specified\n")

    return lines


def _parent_story_orientation_lines(
    story: dict[str, Any],
    sprint: dict[str, Any],
    product: dict[str, Any],
) -> list[str]:
    lines = ["## Parent Story Orientation"]
    if story.get("title"):
        lines.append(f"**Parent Story**: {_escape_md(story.get('title'))}")
        if story.get("story_description"):
            lines.append(f"> {_escape_md(story.get('story_description'))}\n")
    if sprint.get("goal"):
        lines.append(f"**Sprint Goal**: {_escape_md(sprint.get('goal'))}\n")
    if product.get("vision_excerpt"):
        vision_excerpt = _escape_md(product.get("vision_excerpt"))
        lines.append(f"**product vision**: {vision_excerpt}\n")
    return lines


def render_human_brief(packet: dict[str, Any]) -> str:
    """Render the canonical packet into a clean Markdown brief for developers."""
    if _schema_version(packet) == STORY_SCHEMA_VERSION:
        return _render_story_human_brief(packet)
    return _render_task_human_brief(packet)


def render_agent_prompt(packet: dict[str, Any]) -> str:
    """Render the canonical packet into a strict XML-tagged agent prompt."""
    if _schema_version(packet) == STORY_SCHEMA_VERSION:
        return _render_story_agent_prompt(packet)
    return _render_task_agent_prompt(packet)


def render_packet(packet: dict[str, Any], flavor: str) -> str:
    """Dispatch packet rendering across supported output flavors."""
    normalized = (flavor or "").strip().lower()
    if normalized in ("human", "markdown", "brief"):
        return render_human_brief(packet)
    if normalized in ("cursor", "copilot", "agent", "xml"):
        return render_agent_prompt(packet)
    return render_agent_prompt(packet)
