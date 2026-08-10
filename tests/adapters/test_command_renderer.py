"""Contract tests for condition-free workflow command rendering."""

import json
import shlex
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cli.main import build_parser
from cli.workflow_commands import COMMAND_PREFIXES, render_workflow_next
from workflow.contracts import (
    FactReference,
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
    "<feedback>": "Narrow the identity invariant.",
    "<rationale>": "reviewed",
    "<reason>": "changed",
    "<story-id>": "7",
    "<dependency>": "7:8:Story 7 requires Story 8.",
    "<repair>": "7:3:1.1",
    "<outcome-summary>": "Implemented semantic execution.",
    "<artifact-ref>": "services/application.py",
    "<acceptance-result>": "fully_met",
    "<checklist-item>": "Focused tests=passed",
    "<resolution>": "Completed",
    "<delivered>": "Semantic execution transport.",
    "<evidence>": "Focused tests pass.",
    "<known-gaps>": "None.",
    "<impact>": "none",
    "<max-story-points>": "3",
    "<team-name>": "Platform",
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
        "record_authority_feedback": "agileforge authority feedback",
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


def test_delivery_reviews_render_fingerprint_free_semantic_commands() -> None:
    """Render all four waiting reviews without request files or artifact guards."""
    decisions = (
        NodeDecision(
            node_id="backlog.review",
            child_graph_id="backlog",
            request_kind="decide_backlog",
            category=NodeCategory.WAITING,
            recommendation_kind=RecommendationKind.REQUIRED,
            reason_code="BACKLOG_REVIEW_REQUIRED",
            decision_fingerprint="decision-backlog",
        ),
        NodeDecision(
            node_id="planning.roadmap.review",
            child_graph_id="planning",
            request_kind="decide_roadmap",
            category=NodeCategory.WAITING,
            recommendation_kind=RecommendationKind.REQUIRED,
            reason_code="ROADMAP_REVIEW_REQUIRED",
            decision_fingerprint="decision-roadmap",
        ),
        NodeDecision(
            node_id="planning.story.review",
            instance_key="requirement:req-7",
            child_graph_id="planning",
            request_kind="decide_story",
            category=NodeCategory.WAITING,
            recommendation_kind=RecommendationKind.REQUIRED,
            reason_code="STORY_REVIEW_REQUIRED",
            decision_fingerprint="decision-story",
        ),
        NodeDecision(
            node_id="planning.sprint.review",
            child_graph_id="planning",
            request_kind="decide_sprint_plan",
            category=NodeCategory.WAITING,
            recommendation_kind=RecommendationKind.REQUIRED,
            reason_code="SPRINT_REVIEW_REQUIRED",
            decision_fingerprint="decision-sprint",
        ),
    )
    position = position_fixture().model_copy(
        update={
            "decisions": decisions,
            "available_nodes": (),
            "waiting_nodes": tuple(item.node_id for item in decisions),
            "blocked_nodes": (),
            "invalid_nodes": (),
        }
    )

    commands = render_workflow_next(position)["commands"]

    assert len(commands) == len(decisions)
    for item in commands:
        command = item["command"]
        assert "--decision <decision>" in command
        assert "--rationale <rationale>" in command
        assert "--request-file" not in command
        assert "fingerprint" not in command
        parsed = build_parser().parse_args(
            [
                _PLACEHOLDERS.get(argument, argument)
                for argument in shlex.split(command)[1:]
            ]
        )
        assert parsed.decision == "accepted"
    story_command = next(
        item["command"] for item in commands if item["request_kind"] == "decide_story"
    )
    assert "--instance-key requirement:req-7" in story_command


@pytest.mark.parametrize(
    ("request_kind", "expected_flags", "fact_references"),
    [
        (
            "reconcile_backlog",
            (),
            (
                FactReference(
                    fact_type="authority",
                    fact_id="17",
                    fingerprint="authority-17",
                ),
                FactReference(
                    fact_type="backlog",
                    fact_id="23",
                    fingerprint="backlog-23",
                ),
            ),
        ),
        (
            "apply_story_dependencies",
            ("--story-id", "--dependency"),
            (
                FactReference(
                    fact_type="story_dependency_source",
                    fact_id="41",
                    fingerprint="dependency-source-41",
                ),
            ),
        ),
        (
            "repair_story_readiness",
            ("--repair",),
            (
                FactReference(
                    fact_type="story_readiness",
                    fact_id="41",
                    fingerprint="readiness-41",
                ),
            ),
        ),
        (
            "start_sprint",
            (),
            (
                FactReference(
                    fact_type="sprint_plan",
                    fact_id="29",
                    fingerprint="plan-29",
                ),
                FactReference(
                    fact_type="candidate_set",
                    fact_id="41",
                    fingerprint="candidates-41",
                ),
                FactReference(
                    fact_type="sprint_plan_tasks",
                    fact_id="31",
                    fingerprint="tasks-31",
                ),
            ),
        ),
    ],
)
def test_planning_actions_render_task_specific_semantic_commands(
    request_kind: str,
    expected_flags: tuple[str, ...],
    fact_references: tuple[FactReference, ...],
) -> None:
    """Advertise only parser-valid operator semantics for planning actions."""
    decision = NodeDecision(
        node_id=f"test.{request_kind}",
        child_graph_id="planning",
        request_kind=request_kind,
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="TEST_AVAILABLE",
        decision_fingerprint=f"decision-{request_kind}",
        fact_references=fact_references,
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

    commands = render_workflow_next(position)["commands"]

    assert len(commands) == 1
    command = commands[0]["command"]
    assert "--request-file" not in command
    assert "fingerprint" not in command
    for flag in expected_flags:
        assert flag in command
    parsed = build_parser().parse_args(
        [_PLACEHOLDERS.get(argument, argument) for argument in shlex.split(command)[1:]]
    )
    assert parsed.project_id == position.project_id


@pytest.mark.parametrize(
    "request_kind",
    [
        "reconcile_backlog",
        "apply_story_dependencies",
        "repair_story_readiness",
        "start_sprint",
    ],
)
def test_planning_actions_with_malformed_decisions_are_not_advertised(
    request_kind: str,
) -> None:
    """Suppress semantic commands when required graph references are absent."""
    decision = NodeDecision(
        node_id=f"test.{request_kind}",
        child_graph_id="planning",
        request_kind=request_kind,
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="TEST_AVAILABLE",
        decision_fingerprint=f"decision-{request_kind}",
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

    assert render_workflow_next(position)["commands"] == []


@pytest.mark.parametrize(
    ("request_kind", "instance_key", "expected_flags", "fact_references"),
    [
        (
            "complete_task",
            "task:7",
            (
                "--instance-key",
                "--outcome-summary",
                "--artifact-ref",
                "--acceptance-result",
                "--checklist-item",
            ),
            (
                FactReference(
                    fact_type="task",
                    fact_id="7",
                    fingerprint="task-7",
                ),
            ),
        ),
        (
            "close_story",
            "story:9",
            (
                "--instance-key",
                "--resolution",
                "--delivered",
                "--evidence",
                "--known-gaps",
            ),
            (
                FactReference(
                    fact_type="story_completion",
                    fact_id="9",
                    fingerprint="story-completion-9",
                ),
            ),
        ),
        (
            "review_sprint",
            "sprint:31",
            ("--instance-key",),
            (
                FactReference(
                    fact_type="sprint_review",
                    fact_id="31",
                    fingerprint="sprint-review-31",
                ),
            ),
        ),
        (
            "close_sprint",
            "sprint:31",
            ("--instance-key",),
            (
                FactReference(
                    fact_type="sprint",
                    fact_id="31",
                    fingerprint="sprint-31",
                ),
                FactReference(
                    fact_type="sprint_review",
                    fact_id="31",
                    fingerprint="sprint-review-31",
                ),
                FactReference(
                    fact_type="sprint_close",
                    fact_id="31",
                    fingerprint="sprint-close-31",
                ),
            ),
        ),
        (
            "record_post_sprint_triage",
            "sprint:31",
            ("--instance-key", "--impact", "--file"),
            (
                FactReference(
                    fact_type="sprint_closure",
                    fact_id="31",
                    fingerprint="sprint-close-31",
                ),
            ),
        ),
    ],
)
def test_execution_actions_render_parser_valid_semantic_commands(
    request_kind: str,
    instance_key: str,
    expected_flags: tuple[str, ...],
    fact_references: tuple[FactReference, ...],
) -> None:
    """Advertise only strict execution semantics and required exact selectors."""
    decision = NodeDecision(
        node_id=f"test.{request_kind}",
        instance_key=instance_key,
        child_graph_id="execution",
        request_kind=request_kind,
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="TEST_AVAILABLE",
        decision_fingerprint=f"decision-{request_kind}",
        fact_references=fact_references,
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

    commands = render_workflow_next(position)["commands"]

    assert len(commands) == 1
    command = commands[0]["command"]
    assert "--request-file" not in command
    assert "fingerprint" not in command
    for flag in expected_flags:
        assert flag in command
    parsed = build_parser().parse_args(
        [_PLACEHOLDERS.get(argument, argument) for argument in shlex.split(command)[1:]]
    )
    assert parsed.project_id == position.project_id

    malformed = decision.model_copy(
        update={
            "fact_references": (
                *fact_references,
                FactReference(
                    fact_type="unexpected_guard",
                    fact_id="99",
                    fingerprint="caller-owned",
                ),
            )
        }
    )
    malformed_position = position.model_copy(update={"decisions": (malformed,)})
    assert render_workflow_next(malformed_position)["commands"] == []


@pytest.mark.parametrize(
    "request_kind",
    [
        "complete_task",
        "close_story",
        "review_sprint",
        "close_sprint",
        "record_post_sprint_triage",
    ],
)
def test_execution_actions_with_malformed_decisions_are_not_advertised(
    request_kind: str,
) -> None:
    """Suppress execution recommendations without exact selectors and references."""
    decision = NodeDecision(
        node_id=f"test.{request_kind}",
        child_graph_id="execution",
        request_kind=request_kind,
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="TEST_AVAILABLE",
        decision_fingerprint=f"decision-{request_kind}",
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

    assert render_workflow_next(position)["commands"] == []


def test_generic_workflow_command_renderer_is_removed() -> None:
    """Keep every public recommendation on an explicit semantic renderer."""
    source = (Path(__file__).parents[2] / "cli" / "workflow_commands.py").read_text()

    assert "def _render_command(" not in source
    assert "<request-file>" not in source


def test_sprint_generation_advertises_parser_valid_capacity_remediation() -> None:
    """Emit one callable semantic command with an explicit capacity placeholder."""
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

    assert len(payload["commands"]) == 1
    command = payload["commands"][0]["command"]
    assert "agileforge sprint generate" in command
    assert "--max-story-points <max-story-points>" in command
    assert "--team-name <team-name>" in command
    assert "--input-file" not in command
    assert "--model-id" not in command
    argv = [
        _PLACEHOLDERS.get(argument, argument) for argument in shlex.split(command)[1:]
    ]
    parsed = build_parser().parse_args(argv)
    assert parsed.max_story_points == int(_PLACEHOLDERS["<max-story-points>"])
    assert parsed.team_name == "Platform"


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


def test_duplicate_story_review_selectors_are_ambiguous() -> None:
    """Suppress duplicate Story review selectors but keep distinct ones."""
    decisions = tuple(
        NodeDecision(
            node_id="planning.story.review",
            instance_key=instance_key,
            child_graph_id="planning",
            request_kind="decide_story",
            category=NodeCategory.WAITING,
            recommendation_kind=RecommendationKind.REQUIRED,
            reason_code="STORY_REVIEW_REQUIRED",
            decision_fingerprint=f"decision-story-{index}",
        )
        for index, instance_key in enumerate(
            (
                "requirement:req-1",
                "requirement:req-1",
                "requirement:req-2",
                "requirement:req-3",
            )
        )
    )
    position = position_fixture().model_copy(
        update={
            "decisions": decisions,
            "available_nodes": (),
            "waiting_nodes": tuple(item.node_id for item in decisions),
            "blocked_nodes": (),
            "invalid_nodes": (),
        }
    )

    commands = render_workflow_next(position)["commands"]

    assert [item["command"] for item in commands] == [
        (
            "agileforge story decide --project-id 41 "
            "--instance-key requirement:req-2 --decision <decision> "
            "--rationale <rationale> --idempotency-key <idempotency-key> "
            "--actor <actor>"
        ),
        (
            "agileforge story decide --project-id 41 "
            "--instance-key requirement:req-3 --decision <decision> "
            "--rationale <rationale> --idempotency-key <idempotency-key> "
            "--actor <actor>"
        ),
    ]


def test_duplicate_selectorless_story_reviews_are_ambiguous() -> None:
    """Suppress repeated Story review decisions without selectors."""
    decisions = tuple(
        NodeDecision(
            node_id="planning.story.review",
            child_graph_id="planning",
            request_kind="decide_story",
            category=NodeCategory.WAITING,
            recommendation_kind=RecommendationKind.REQUIRED,
            reason_code="STORY_REVIEW_REQUIRED",
            decision_fingerprint=f"decision-story-{index}",
        )
        for index in range(2)
    )
    position = position_fixture().model_copy(
        update={
            "decisions": decisions,
            "available_nodes": (),
            "waiting_nodes": ("planning.story.review",),
            "blocked_nodes": (),
            "invalid_nodes": (),
        }
    )

    assert render_workflow_next(position)["commands"] == []


def test_selectorless_story_review_is_not_executable() -> None:
    """Suppress even one Story review decision without its required selector."""
    decision = NodeDecision(
        node_id="planning.story.review",
        child_graph_id="planning",
        request_kind="decide_story",
        category=NodeCategory.WAITING,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="STORY_REVIEW_REQUIRED",
        decision_fingerprint="decision-story",
    )
    position = position_fixture().model_copy(
        update={
            "decisions": (decision,),
            "available_nodes": (),
            "waiting_nodes": (decision.node_id,),
            "blocked_nodes": (),
            "invalid_nodes": (),
        }
    )

    assert render_workflow_next(position)["commands"] == []


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
