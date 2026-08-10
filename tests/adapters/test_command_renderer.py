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


def test_renderer_covers_every_graph_request_kind() -> None:
    """Keep the command registry complete for every graph-authored decision."""
    definitions = Path(__file__).parents[2] / "workflow" / "definitions"
    request_kinds = {
        match.split('"', maxsplit=2)[1]
        for path in definitions.glob("*.py")
        for match in path.read_text().splitlines()
        if "request_kind=" in match
    }

    assert set(COMMAND_PREFIXES) == request_kinds


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
