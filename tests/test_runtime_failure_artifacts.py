"""Tests for runtime failure artifacts."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from typing import Any, Never

import pytest

from services import (
    backlog_runtime,
    roadmap_runtime,
    sprint_input,
    sprint_runtime,
    story_runtime,
    vision_runtime,
)
from utils import failure_artifacts
from utils.failure_artifacts import AgentInvocationError

RuntimeCase = dict[str, Any]


def _story_state() -> dict[str, Any]:
    return {
        "pending_spec_content": "SPEC",
        "compiled_authority_cached": '{"ok": true}',
    }


def _vision_state() -> dict[str, Any]:
    return {
        "pending_spec_content": "SPEC",
        "compiled_authority_cached": '{"ok": true}',
    }


def _backlog_state() -> dict[str, Any]:
    return {
        "pending_spec_content": "SPEC",
        "compiled_authority_cached": '{"ok": true}',
        "product_vision_assessment": {
            "product_vision_statement": "Vision statement",
        },
    }


def _roadmap_state() -> dict[str, Any]:
    return {
        "pending_spec_content": "SPEC",
        "compiled_authority_cached": '{"ok": true}',
        "product_vision_assessment": {
            "product_vision_statement": "Vision statement",
        },
        "backlog_items": [],
    }


RUNTIME_CASES: list[RuntimeCase] = [
    {
        "phase": "story",
        "module": story_runtime,
        "invoke_name": "_invoke_story_agent",
        "run": story_runtime.run_story_agent_from_state,
        "state_factory": _story_state,
        "kwargs_factory": lambda: {
            "project_id": 1,
            "parent_requirement": "Requirement A",
            "user_input": None,
        },
    },
    {
        "phase": "vision",
        "module": vision_runtime,
        "invoke_name": "_invoke_vision_agent",
        "run": vision_runtime.run_vision_agent_from_state,
        "state_factory": _vision_state,
        "kwargs_factory": lambda: {
            "project_id": 1,
            "user_input": "",
        },
    },
    {
        "phase": "backlog",
        "module": backlog_runtime,
        "invoke_name": "_invoke_backlog_agent",
        "run": backlog_runtime.run_backlog_agent_from_state,
        "state_factory": _backlog_state,
        "kwargs_factory": lambda: {
            "project_id": 1,
            "user_input": "",
        },
    },
    {
        "phase": "roadmap",
        "module": roadmap_runtime,
        "invoke_name": "_invoke_roadmap_agent",
        "run": roadmap_runtime.run_roadmap_agent_from_state,
        "state_factory": _roadmap_state,
        "kwargs_factory": lambda: {
            "project_id": 1,
            "user_input": "",
        },
    },
]


def _patch_failure_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(failure_artifacts, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        failure_artifacts, "FAILURES_DIR", tmp_path / "logs" / "failures"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", RUNTIME_CASES, ids=lambda case: case["phase"])
async def test_runtime_invalid_json_writes_full_failure_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: RuntimeCase
) -> None:
    """Verify runtime invalid json writes full failure artifact."""
    _patch_failure_dir(monkeypatch, tmp_path)

    async def fake_invoke(_payload: object) -> str:
        return '{"broken": '

    monkeypatch.setattr(case["module"], case["invoke_name"], fake_invoke)

    result = await case["run"](case["state_factory"](), **case["kwargs_factory"]())
    assert result["success"] is False
    assert result["failure_stage"] == "invalid_json"
    assert result["failure_artifact_id"] is not None
    assert result["has_full_artifact"] is True

    artifact = failure_artifacts.read_failure_artifact(result["failure_artifact_id"])
    assert artifact is not None
    assert artifact["phase"] == case["phase"]
    assert artifact["raw_output"] == '{"broken": '


@pytest.mark.asyncio
async def test_backlog_runtime_forces_incomplete_when_questions_remain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backlog runtime must not accept complete artifacts with open questions."""

    async def fake_invoke(_payload: object) -> str:
        return (
            '{"backlog_items":[{"priority":1,"requirement":"Team selection",'
            '"value_driver":"Customer Satisfaction","justification":"Core value",'
            '"estimated_effort":"M"}],"is_complete":true,'
            '"clarifying_questions":["Which risk threshold should block a pick?"]}'
        )

    monkeypatch.setattr(backlog_runtime, "_invoke_backlog_agent", fake_invoke)

    result = await backlog_runtime.run_backlog_agent_from_state(
        _backlog_state(),
        project_id=1,
        user_input="",
    )

    assert result["success"] is True
    assert result["is_complete"] is False
    assert result["output_artifact"]["is_complete"] is False
    assert result["output_artifact"]["clarifying_questions"] == [
        "Which risk threshold should block a pick?"
    ]


@pytest.mark.asyncio
async def test_roadmap_runtime_forces_incomplete_when_questions_remain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Roadmap runtime must not accept complete artifacts with open questions."""

    async def fake_invoke(_payload: object) -> str:
        return (
            '{"roadmap_releases":[{"release_name":"Milestone 1",'
            '"theme":"Foundation","focus_area":"Technical Foundation",'
            '"items":["Team selection"],"reasoning":"Start here"}],'
            '"roadmap_summary":"Draft roadmap","is_complete":true,'
            '"clarifying_questions":["Which release must own simulations?"]}'
        )

    monkeypatch.setattr(roadmap_runtime, "_invoke_roadmap_agent", fake_invoke)

    result = await roadmap_runtime.run_roadmap_agent_from_state(
        {
            **_roadmap_state(),
            "backlog_items": [
                {
                    "priority": 1,
                    "requirement": "Team selection",
                    "value_driver": "Strategic",
                    "justification": "Core value",
                    "estimated_effort": "M",
                }
            ],
        },
        project_id=1,
        user_input="",
    )

    assert result["success"] is True
    assert result["is_complete"] is False
    assert result["output_artifact"]["is_complete"] is False
    assert result["output_artifact"]["clarifying_questions"] == [
        "Which release must own simulations?"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", RUNTIME_CASES, ids=lambda case: case["phase"])
async def test_runtime_output_validation_writes_full_failure_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: RuntimeCase
) -> None:
    """Verify runtime output validation writes full failure artifact."""
    _patch_failure_dir(monkeypatch, tmp_path)

    async def fake_invoke(_payload: object) -> str:
        return "{}"

    monkeypatch.setattr(case["module"], case["invoke_name"], fake_invoke)

    result = await case["run"](case["state_factory"](), **case["kwargs_factory"]())
    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert result["failure_artifact_id"] is not None

    artifact = failure_artifacts.read_failure_artifact(result["failure_artifact_id"])
    assert artifact is not None
    assert artifact["phase"] == case["phase"]
    assert artifact["raw_output"] == "{}"
    assert artifact["validation_errors"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", RUNTIME_CASES, ids=lambda case: case["phase"])
async def test_runtime_invocation_exception_persists_partial_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: RuntimeCase
) -> None:
    """Verify runtime invocation exception persists partial output."""
    _patch_failure_dir(monkeypatch, tmp_path)

    async def fake_invoke(_payload: object) -> Never:
        msg = "provider timeout"
        raise AgentInvocationError(
            msg,
            partial_output='{"partial": true}',
            event_count=2,
        )

    monkeypatch.setattr(case["module"], case["invoke_name"], fake_invoke)

    result = await case["run"](case["state_factory"](), **case["kwargs_factory"]())
    assert result["success"] is False
    assert result["failure_stage"] == "invocation_exception"
    assert result["failure_artifact_id"] is not None
    assert result["raw_output_preview"] == '{"partial": true}'
    if case["phase"] == "vision":
        assert result["model_info"]["agent_name"] == "product_vision_tool"
        assert result["model_info"]["model_id"]

    artifact = failure_artifacts.read_failure_artifact(result["failure_artifact_id"])
    assert artifact is not None
    assert artifact["phase"] == case["phase"]
    assert artifact["raw_output"] == '{"partial": true}'


@pytest.mark.asyncio
async def test_sprint_failure_artifact_keeps_structured_validation_details(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify sprint failure artifact keeps structured validation details."""
    _patch_failure_dir(monkeypatch, tmp_path)

    def fake_fetch_sprint_candidates(*, product_id: int) -> object:
        assert product_id == 7  # noqa: PLR2004
        return {
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 2,
                    "story_points": 3,
                    "evaluated_invariant_ids": [],
                }
            ],
        }

    async def fake_invoke(_payload: object) -> Never:
        msg = "ADK validation failed"
        raise AgentInvocationError(
            msg,
            validation_errors=[
                {
                    "type": "literal_error",
                    "loc": ["selected_stories", 0, "tasks", 0, "task_kind"],
                    "msg": "Input should be one of the supported task kinds.",
                    "input": "approval",
                }
            ],
        )

    monkeypatch.setattr(
        sprint_input, "fetch_sprint_candidates", fake_fetch_sprint_candidates
    )
    monkeypatch.setattr(sprint_runtime, "_invoke_sprint_agent", fake_invoke)

    result = await sprint_runtime.run_sprint_agent_from_state(
        {},
        project_id=7,
        max_story_points=13,
        include_task_decomposition=True,
        selected_story_ids=[12],
        user_input=None,
        capacity_points=13,
        capacity_source="user_override",
        capacity_basis="13 points",
    )

    assert result["success"] is False
    assert result["failure_stage"] == "invocation_exception"
    assert result["output_artifact"]["validation_errors"] == [
        "Unsupported task_kind 'approval'. Use one of: analysis, design, implementation, testing, documentation, refactor."  # noqa: E501
    ]

    artifact = failure_artifacts.read_failure_artifact(result["failure_artifact_id"])
    assert artifact is not None
    assert artifact["phase"] == "sprint"
    assert artifact["validation_errors"] == [
        {
            "type": "literal_error",
            "loc": ["selected_stories", 0, "tasks", 0, "task_kind"],
            "msg": "Input should be one of the supported task kinds.",
            "input": "approval",
        }
    ]
