"""Contract tests for condition-free workflow command rendering."""

import json
import shlex
from pathlib import Path

from cli.main import build_parser
from cli.workflow_commands import COMMAND_PREFIXES, render_workflow_next
from workflow.contracts import WorkflowPosition

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "workflow_position.json"
_PLACEHOLDERS = {
    "<input-file>": "input.json",
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
    assert "--graph-version agileforge.workflow.v1" in serialized
    assert "--expected-fact-fingerprint facts-41" in serialized
    assert "--expected-decision-fingerprint decision-compile" in serialized
    assert "--idempotency-key <idempotency-key>" in serialized
    assert "--changed-by <actor>" in serialized
    assert "--expected-state" not in serialized
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
        "FSM" + "Controller",
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

    for item in render_workflow_next(position_fixture())["commands"]:
        argv = shlex.split(item["command"])[1:]
        argv = [_PLACEHOLDERS.get(argument, argument) for argument in argv]
        parsed = parser.parse_args(argv)
        assert parsed.project_id == position_fixture().project_id
