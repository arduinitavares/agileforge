"""Contract tests for condition-free workflow command rendering."""

import json
import shlex
from datetime import UTC, datetime
from pathlib import Path

from cli.main import build_parser
from cli.workflow_commands import COMMAND_PREFIXES, render_workflow_next
from workflow.contracts import (
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    WorkflowPosition,
)

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "workflow_position.json"
EXPECTED_STORY_COMMAND_COUNT = 2
_PLACEHOLDERS = {
    "<input-file>": "input.json",
    "<request-file>": "request.json",
    "<file>": "artifact.json",
    "<text>": "response",
    "<decision>": "accepted",
    "<rationale>": "reviewed",
    "<reason>": "changed",
    "<idempotency-key>": "run-41",
    "<actor>": "cli-user",
}

_RETIRED_REQUEST_KINDS = {
    "abandon_project_shell",
    "abandon_scope_extension",
    "decide_amendment_spec_draft",
    "decide_brownfield_initial_spec",
    "decide_extension_prd",
    "decide_initial_spec_draft",
    "decide_prd",
    "decide_vision",
    "record_amendment_spec_draft",
    "record_brownfield_spec_draft",
    "record_challenge_artifact",
    "record_extension_challenge",
    "record_extension_prd",
    "record_initial_spec_draft",
    "record_prd_version",
    "record_repository_baseline",
    "record_repository_inventory",
    "record_vision_draft",
    "register_initial_scope",
    "register_scope_extension",
    "reconcile_scope_extension",
    "start_scope_extension",
}


def position_fixture() -> WorkflowPosition:
    """Load the transport-shared serialized position fixture."""
    return WorkflowPosition.model_validate_json(FIXTURE_PATH.read_text())


def test_workflow_next_renders_required_and_recovery_only() -> None:
    """Advertise each available required or recovery decision exactly once."""
    payload = render_workflow_next(position_fixture())

    assert [item["node_id"] for item in payload["commands"]] == [
        "authority.compile",
        "authority.repair",
    ]
    serialized = json.dumps(payload)
    assert "--graph-version" not in serialized
    assert "--expected-fact-fingerprint" not in serialized
    assert "--expected-decision-fingerprint" not in serialized
    assert "--idempotency-key <idempotency-key>" in serialized
    assert "--actor <actor>" in serialized
    assert "decision_fingerprint" not in serialized
    assert "--expected-" + "state" not in serialized
    assert "--expected-setup-status" not in serialized


def test_zero_command_position_explains_terminal_waiting_and_invalid() -> None:
    """Keep orientation details when no command is executable."""
    position = position_fixture().model_copy(
        update={
            "available_nodes": (),
            "terminal": True,
            "decisions": tuple(
                decision
                for decision in position_fixture().decisions
                if decision.category.value != "available"
            ),
        }
    )

    payload = render_workflow_next(position)

    assert payload["commands"] == []
    assert payload["terminal"] is True
    assert payload["waiting_nodes"] == ["vision.generate"]
    assert payload["invalid_nodes"] == ["planning.roadmap.generate"]


def test_renderer_module_has_no_routing_or_repository_policy() -> None:
    """Keep persistence and availability policy outside the renderer."""
    source = (Path(__file__).parents[2] / "cli" / "workflow_commands.py").read_text()

    for forbidden in (
        "repositories",
        "WorkflowService",
        "F" + "SMController",
        "fsm" + "_state",
        "setup_status",
    ):
        assert forbidden not in source


def test_renderer_excludes_retired_transport_request_kinds() -> None:
    """Do not render compatibility commands for graph modules deleted in Task 9."""
    assert _RETIRED_REQUEST_KINDS.isdisjoint(COMMAND_PREFIXES)


def test_rendered_commands_are_accepted_by_the_cli_parser() -> None:
    """Ensure advertised commands and the executable parser share one contract."""
    parser = build_parser()
    decisions = tuple(
        NodeDecision(
            node_id=f"test.{index}",
            instance_key=f"instance:{index}",
            child_graph_id="test",
            request_kind=request_kind,
            category=NodeCategory.AVAILABLE,
            recommendation_kind=RecommendationKind.REQUIRED,
            reason_code="TEST_AVAILABLE",
            decision_fingerprint=f"decision-{index}",
        )
        for index, request_kind in enumerate(COMMAND_PREFIXES)
    )
    position = WorkflowPosition(
        project_id=41,
        graph_version="agileforge.workflow.v2",
        fact_fingerprint="facts-41",
        evaluated_at=datetime(2026, 8, 9, tzinfo=UTC),
        available_nodes=tuple(item.node_id for item in decisions),
        waiting_nodes=(),
        blocked_nodes=(),
        invalid_nodes=(),
        terminal=False,
        decisions=decisions,
    )

    for item in render_workflow_next(position)["commands"]:
        argv = shlex.split(item["command"])[1:]
        argv = [_PLACEHOLDERS.get(argument, argument) for argument in argv]
        parsed = parser.parse_args(argv)
        assert parsed.project_id == position.project_id
        assert not hasattr(parsed, "graph_version")
        assert not hasattr(parsed, "expected_fact_fingerprint")
        assert not hasattr(parsed, "expected_decision_fingerprint")
        assert not hasattr(parsed, "changed_by")
        assert not hasattr(parsed, "input_file")
        assert not hasattr(parsed, "model_id")
        assert "--input-file" not in item["command"]
        assert "--model-id" not in item["command"]


def test_lifecycle_positions_render_semantic_commands() -> None:
    """Render each new lifecycle boundary with task language only."""
    expected = {
        "record_vision_interview_turn": "agileforge vision respond",
        "record_product_goal_interview_turn": "agileforge goal respond",
        "record_discovery_artifact": "agileforge discovery record",
        "record_specification_candidate": "agileforge specification record",
        "compile_authority": "agileforge authority compile",
        "fulfill_product_goal": "agileforge goal complete",
        "abandon_product_goal": "agileforge goal abandon",
    }
    for request_kind, command_prefix in expected.items():
        decision = NodeDecision(
            node_id=f"test.{request_kind}",
            child_graph_id="test",
            request_kind=request_kind,
            category=NodeCategory.AVAILABLE,
            recommendation_kind=RecommendationKind.REQUIRED,
            reason_code="TEST_AVAILABLE",
            decision_fingerprint=f"decision-{request_kind}",
        )
        position = position_fixture().model_copy(
            update={
                "graph_version": "agileforge.workflow.v2",
                "decisions": (decision,),
                "available_nodes": (decision.node_id,),
                "waiting_nodes": (),
                "blocked_nodes": (),
                "invalid_nodes": (),
            }
        )

        payload = render_workflow_next(position)

        assert len(payload["commands"]) == 1
        assert payload["commands"][0]["command"].startswith(command_prefix)


def test_sprint_generation_is_not_advertised_without_capacity_contract() -> None:
    """Keep Sprint planning visible but non-executable until capacity is durable."""
    decision = NodeDecision(
        node_id="planning.sprint.plan",
        child_graph_id="planning",
        request_kind="record_sprint_plan",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="SPRINT_PLANNING_REQUIRED",
        decision_fingerprint="decision-sprint",
    )
    position = position_fixture().model_copy(
        update={
            "decisions": (decision,),
            "available_nodes": (decision.node_id,),
            "waiting_nodes": (),
            "blocked_nodes": (),
            "invalid_nodes": (),
        }
    )

    payload = render_workflow_next(position)

    assert payload["commands"] == []
    assert payload["waiting_nodes"] == []


def test_story_generation_renders_each_exact_requirement_selector() -> None:
    """Keep parallel Story work reachable through parser-valid semantic commands."""
    parser = build_parser()
    decisions = tuple(
        NodeDecision(
            node_id="planning.story.generate",
            instance_key=f"requirement:req-{index}",
            child_graph_id="planning",
            request_kind="record_story_draft",
            category=NodeCategory.AVAILABLE,
            recommendation_kind=RecommendationKind.REQUIRED,
            reason_code="STORY_GENERATION_REQUIRED",
            decision_fingerprint=f"decision-story-{index}",
        )
        for index in range(EXPECTED_STORY_COMMAND_COUNT)
    )
    position = position_fixture().model_copy(
        update={
            "decisions": decisions,
            "available_nodes": ("planning.story.generate",),
            "waiting_nodes": (),
            "blocked_nodes": (),
            "invalid_nodes": (),
        }
    )

    commands = render_workflow_next(position)["commands"]

    assert len(commands) == EXPECTED_STORY_COMMAND_COUNT
    for index, item in enumerate(commands):
        argv = [
            _PLACEHOLDERS.get(argument, argument)
            for argument in shlex.split(item["command"])[1:]
        ]
        parsed = parser.parse_args(argv)
        assert parsed.instance_key == f"requirement:req-{index}"
        assert "--input-file" not in item["command"]
        assert "--model-id" not in item["command"]


def test_ambiguous_semantic_decisions_do_not_render_an_unusable_command() -> None:
    """Advertise a semantic action only when the parser needs no hidden selector."""
    parser = build_parser()
    decisions = tuple(
        NodeDecision(
            node_id="vision.interview",
            instance_key=f"after-turn:{index}",
            child_graph_id="vision",
            request_kind="record_vision_interview_turn",
            category=NodeCategory.AVAILABLE,
            recommendation_kind=RecommendationKind.REQUIRED,
            reason_code="VISION_INTERVIEW_REQUIRED",
            decision_fingerprint=f"decision-vision-{index}",
        )
        for index in range(2)
    )
    unique_position = position_fixture().model_copy(
        update={
            "decisions": decisions[:1],
            "available_nodes": ("vision.interview",),
            "waiting_nodes": (),
            "blocked_nodes": (),
            "invalid_nodes": (),
        }
    )
    unique_command = render_workflow_next(unique_position)["commands"][0]["command"]

    parsed = parser.parse_args(
        [
            _PLACEHOLDERS.get(argument, argument)
            for argument in shlex.split(unique_command)[1:]
        ]
    )
    ambiguous_position = unique_position.model_copy(
        update={
            "decisions": decisions,
            "blocked_nodes": ("vision.interview.ambiguity",),
        }
    )

    payload = render_workflow_next(ambiguous_position)

    assert parsed.project_id == unique_position.project_id
    assert payload["commands"] == []
    assert payload["blocked_nodes"] == ["vision.interview.ambiguity"]
