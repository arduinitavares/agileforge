"""Focused unit tests for the packet renderer (prompt contract split)."""

from services.packet_renderer import render_human_brief, render_packet
from workflow.contracts import JsonObject, JsonValue


def _minimal_packet(**overrides: JsonValue) -> JsonObject:
    """Build the smallest valid packet dict for renderer testing."""
    schema_version = overrides.get("schema_version", "task_packet.v2")
    task_plan = overrides.get("task_plan")
    story_payload: JsonObject = {
        "story_id": 7,
        "title": overrides.get("story_title"),
        "story_description": overrides.get("story_description"),
    }
    context: JsonObject = {
        "sprint": {
            "sprint_id": 3,
            "goal": overrides.get("sprint_goal"),
        },
        "project": {
            "name": "Test Project",
        },
    }
    packet: JsonObject = {
        "schema_version": schema_version,
        "task": {
            "task_id": 1,
            "label": overrides.get("task_label", "Implement feature X"),
            "description": overrides.get("task_description", "Build the feature"),
            "status": "To Do",
            "task_kind": overrides.get("task_kind", "implementation"),
            "artifact_targets": overrides.get("artifact_targets") or [],
            "workstream_tags": overrides.get("workstream_tags") or [],
            "checklist_items": overrides.get("task_checklist_items") or [],
        },
        "context": context,
        "constraints": {
            "acceptance_criteria_items": overrides.get("ac_items") or [],
            "task_hard_constraints": (
                overrides.get("task_hard_constraints") or []
            ),
            "story_compliance_boundaries": (
                overrides.get("story_compliance_boundaries") or []
            ),
            "story_acceptance_criteria_items": overrides.get("ac_items") or [],
        },
    }
    if schema_version == "story_packet.v1":
        packet["story"] = story_payload
    else:
        context["story"] = story_payload
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
    expected_bootstrap_note = (
        "This prompt assumes the session was already initialized with the parent "
        "story prompt. If not, restart with Copy Story Prompt."
    )
    assert expected_bootstrap_note in output
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
