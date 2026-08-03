"""Focused unit tests for the packet renderer (prompt contract split)."""

from typing import Any

from services.packet_renderer import render_human_brief, render_packet


def _minimal_packet(  # noqa: PLR0913
    *,
    schema_version: object = "task_packet.v2",
    task_label: object = "Implement feature X",
    task_description: object = "Build the feature",
    ac_items: object = None,
    task_checklist_items: object = None,
    sprint_goal: object = None,
    story_title: object = None,
    story_description: object = None,
    task_kind: object = "implementation",
    artifact_targets: object = None,
    workstream_tags: object = None,
    task_hard_constraints: object = None,
    story_compliance_boundaries: object = None,
    task_plan: object = None,
) -> dict[str, Any]:
    """Build the smallest valid packet dict for renderer testing."""
    story_payload = {
        "story_id": 7,
        "title": story_title,
        "story_description": story_description,
    }
    packet = {
        "schema_version": schema_version,
        "task": {
            "task_id": 1,
            "label": task_label,
            "description": task_description,
            "status": "To Do",
            "task_kind": task_kind,
            "artifact_targets": artifact_targets or [],
            "workstream_tags": workstream_tags or [],
            "checklist_items": task_checklist_items or [],
        },
        "context": {
            "sprint": {
                "sprint_id": 3,
                "goal": sprint_goal,
            },
            "product": {
                "name": "Test Project",
            },
        },
        "constraints": {
            "acceptance_criteria_items": ac_items or [],
            "task_hard_constraints": task_hard_constraints or [],
            "story_compliance_boundaries": story_compliance_boundaries or [],
            "story_acceptance_criteria_items": ac_items or [],
        },
    }
    if schema_version == "story_packet.v1":
        packet["story"] = story_payload
    else:
        packet["context"]["story"] = story_payload
    if task_plan is not None:
        packet["task_plan"] = {"tasks": task_plan}
    return packet


# ------------------------------------------------------------------
# Execution Protocol
# ------------------------------------------------------------------


def test_render_packet_uses_task_checklist_for_task_packets() -> None:
    """Verify render packet uses task checklist for task packets."""
    packet = _minimal_packet(
        schema_version="task_packet.v2",
        story_title="Parent Story",
        story_description="Bootstrap the execution session.",
        task_checklist_items=["Confirm request shape", "Add request tests"],
        ac_items=["Story AC should stay out of task prompts"],
    )
    output = render_packet(packet, "cursor")

    assert "Task Checklist" in output
    assert "Verify every task checklist item before claiming completion." in output
    assert (
        "This prompt assumes the session was already initialized with the parent story prompt. If not, restart with Copy Story Prompt."  # noqa: E501
        in output
    )
    assert "- [ ] Confirm request shape" in output
    assert "- [ ] Add request tests" in output
    assert "Acceptance Criteria Checklist" not in output
    assert "Story AC should stay out of task prompts" not in output


def test_render_packet_uses_story_acceptance_criteria_for_story_packets() -> None:
    """Verify render packet uses story acceptance criteria for story packets."""
    packet = _minimal_packet(
        schema_version="story_packet.v1",
        story_title="Parent Story",
        story_description="Bootstrap the execution session.",
        ac_items=["include user_id", "reject invalid payloads"],
        task_plan=[
            {
                "id": 12,
                "description": "Implement request validation",
                "status": "To Do",
                "task_kind": "implementation",
                "artifact_targets": ["validator"],
                "workstream_tags": ["backend"],
                "checklist_items": ["Confirm request shape"],
                "is_executable": True,
            }
        ],
    )
    output = render_packet(packet, "cursor")

    assert "Story Acceptance Criteria" in output
    assert "- [ ] include user_id" in output
    assert "- [ ] reject invalid payloads" in output
    assert "Task Checklist" not in output
    assert "Task Plan Reference" in output
    assert "Implement request validation" in output


def test_story_packet_human_brief_uses_top_level_story_shape() -> None:
    """Verify story packet human brief uses top level story shape."""
    packet = _minimal_packet(
        schema_version="story_packet.v1",
        story_title="Top-level Story Title",
        story_description="Top-level story description.",
        ac_items=["include user_id"],
        task_plan=[
            {
                "id": 12,
                "description": "Implement request validation",
                "status": "To Do",
                "task_kind": "implementation",
                "artifact_targets": ["validator"],
                "workstream_tags": ["backend"],
                "checklist_items": ["Confirm request shape"],
                "is_executable": True,
            }
        ],
    )

    output = render_human_brief(packet)

    assert "# Story: Top-level Story Title" in output
    assert "Top-level story description." in output
    assert "## Story Acceptance Criteria" in output
    assert "## Task Plan Reference" in output
    assert "Confirm request shape" not in output
    assert "## Task Checklist" not in output


def test_human_brief_has_no_execution_contract() -> None:
    """Verify human brief has no execution contract."""
    packet = _minimal_packet(
        ac_items=["some AC"], task_checklist_items=["some checklist"]
    )
    output = render_human_brief(packet)
    assert "<execution_protocol>" not in output
    assert "<completion_report>" not in output
    assert "## Completion Report" not in output
