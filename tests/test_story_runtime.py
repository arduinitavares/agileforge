"""Tests for the direct-Specification Story runtime."""

from __future__ import annotations

import hashlib
import json
import sys
import types
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from services import story_runtime
from services.contracts.story import UserStoryWriterInput, UserStoryWriterOutput
from utils import failure_artifacts
from utils.agileforge_spec_profile_v2 import SpecificationPayload

GOLD_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "issue_210"
    / "gold"
    / "canonical-specification.json"
)
GOLD_HASH = "sha256:4f39ae394d3910bc52d73256eddc11edd66e57074025e1ec7f037e8e69a33025"
GOLD_ITEM_IDS = {
    "ASSUMPTION.001",
    "CONSTRAINT.001",
    "CONSTRAINT.002",
    "DATA.001",
    "DATA.002",
    "DECISION.001",
    "DECISION.002",
    "DECISION.003",
    "EXAMPLE.001",
    "GOAL.001",
    "GOAL.002",
    "INTERFACE.001",
    "INTERFACE.002",
    "NON_GOAL.001",
    "NON_GOAL.002",
    "NON_GOAL.003",
    "NON_GOAL.004",
    "OPEN_QUESTION.001",
    "QUALITY.001",
    "REQ.001",
    "REQ.002",
    "REQ.003",
    "REQ.004",
    "REQ.005",
    "REQ.006",
    "REQ.007",
    "REQ.008",
    "REQ.009",
    "REQ.010",
    "REQ.011",
    "REQ.012",
    "REQ.013",
    "REQ.014",
    "REQ.015",
    "RISK.001",
    "RISK.002",
    "RISK.003",
}
EXPECTED_REPAIR_CALL_COUNT = 2
TARGET_STORY_ID = 42


def _state() -> dict[str, Any]:
    return {
        "accepted_specification_version_id": 11,
        "accepted_specification_hash": GOLD_HASH,
        "accepted_specification_json": GOLD_PATH.read_text(encoding="utf-8"),
        "parent_backlog_item_id": "PBI-000001",
        "parent_backlog_spec_item_ids": ["DATA.001", "REQ.001"],
        "roadmap_context": json.dumps(
            {
                "release_name": "Release 1",
                "backlog_item_ids": ["PBI-000001"],
            },
            sort_keys=True,
        ),
    }


def _invest_assessment() -> dict[str, Any]:
    return {
        "independent": {
            "result": "pass",
            "rationale": "Delivers self-contained increment.",
            "evidence": "No external unbuilt dependencies.",
        },
        "negotiable": {
            "result": "pass",
            "rationale": "Implementation details open to refinement.",
            "evidence": "Focuses on user outcome.",
        },
        "valuable": {
            "result": "pass",
            "rationale": "Directly delivers user calculation capability.",
            "evidence": "Satisfies REQ.001 directly.",
        },
        "estimable": {
            "result": "pass",
            "rationale": "Scope is clear and bounded.",
            "evidence": "Discrete acceptance criteria.",
        },
        "small": {
            "result": "pass",
            "rationale": "Sized for single iteration.",
            "evidence": "Effort is S.",
        },
        "testable": {
            "result": "pass",
            "rationale": "Verifiable pass/fail criteria.",
            "evidence": "Observable verification steps against DATA.001.",
        },
    }


def _story_item(title: str = "Implement the accepted operation") -> dict[str, Any]:
    return {
        "story_title": title,
        "statement": (
            "As a calculator user, I want the accepted operation, so that I can "
            "obtain the specified result."
        ),
        "acceptance_criteria": ["Verify the result against DATA.001."],
        "spec_item_ids": ["DATA.001", "REQ.001"],
        "invest_assessment": _invest_assessment(),
        "estimated_effort": "S",
        "effort_rationale": "Single straightforward calculation operation.",
        "order_rationale": "First priority calculation.",
        "produced_artifacts": [],
        "research_caveats": [],
        "dependency_candidates": [],
    }


def _valid_output(*, story_count: int = 1) -> str:
    return json.dumps(
        {
            "user_stories": [
                _story_item(f"Accepted operation {ordinal}")
                for ordinal in range(1, story_count + 1)
            ],
            "is_complete": True,
            "clarifying_questions": [],
        }
    )


def _placeholder_output() -> str:
    """Return the exact three-Story malformed correction shape from issue #229."""
    payload = json.loads(_valid_output(story_count=3))
    third_story = payload["user_stories"][2]
    third_story["story_title"] = "placeholder"
    for dimension in third_story["invest_assessment"].values():
        dimension["rationale"] = "placeholder"
        dimension["evidence"] = "placeholder"
    return json.dumps(payload)


def _fake_failure(**kwargs: object) -> dict[str, object]:
    details = cast("story_runtime._FailureDetails", kwargs["details"])
    return {
        "success": False,
        "output_artifact": None,
        "failure_stage": kwargs["failure_stage"],
        "error": details.message,
    }


def _isolate_failure_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(failure_artifacts, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        failure_artifacts,
        "FAILURES_DIR",
        tmp_path / "logs" / "failures",
    )


def test_story_input_context_preserves_complete_root_and_parent_evidence() -> None:
    """Preserve complete gold bytes and the exact Backlog evidence boundary."""
    context = story_runtime.build_story_input_context(
        _state(), current_user_input="Use only the accepted parent."
    )
    parsed = UserStoryWriterInput.model_validate(context)
    canonical_json = GOLD_PATH.read_text(encoding="utf-8")
    payload = SpecificationPayload.model_validate_json(
        parsed.accepted_specification_json
    )

    assert parsed.accepted_specification_json == canonical_json
    assert "sha256:" + hashlib.sha256(canonical_json.encode()).hexdigest() == GOLD_HASH
    assert {item.id for item in payload.items} == GOLD_ITEM_IDS
    assert "DATA.001" in GOLD_ITEM_IDS
    assert parsed.parent_backlog_item_id == "PBI-000001"
    assert parsed.parent_backlog_spec_item_ids == ("DATA.001", "REQ.001")
    dumped = parsed.model_dump(mode="json")
    assert "technical_spec" not in dumped
    assert "invariants" not in dumped


@pytest.mark.asyncio
async def test_story_runtime_uses_one_provider_call_and_no_automatic_semantic_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run one writer call without triggering an implicit semantic review."""
    observed: list[UserStoryWriterInput] = []
    semantic_calls: list[str] = []

    async def fake_invoke(payload: UserStoryWriterInput) -> str:
        observed.append(payload)
        return _valid_output()

    semantic_module = types.ModuleType("services.story_validation")

    def forbidden_semantic_call(*_args: object, **_kwargs: object) -> None:
        semantic_calls.append("semantic")
        message = "semantic review must be explicit"
        raise AssertionError(message)

    semantic_module.__dict__["validate_story_semantics"] = forbidden_semantic_call
    monkeypatch.setitem(sys.modules, "services.story_validation", semantic_module)
    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)

    result = await story_runtime.run_story_agent_from_state(
        _state(), project_id=1, user_input=None
    )

    assert result["success"] is True
    assert len(observed) == 1
    assert observed[0].accepted_specification_json == GOLD_PATH.read_text(
        encoding="utf-8"
    )
    assert semantic_calls == []


@pytest.mark.asyncio
async def test_story_runtime_rejects_exact_placeholder_correction_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject issue #229 output before it can become reusable canonical content."""
    calls = 0

    async def fake_invoke(_payload: UserStoryWriterInput) -> str:
        nonlocal calls
        calls += 1
        return _placeholder_output()

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)
    monkeypatch.setattr(story_runtime, "_failure", _fake_failure)

    result = await story_runtime.run_story_agent_from_state(
        _state(), project_id=1, user_input="Correct the accepted Story set."
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert result["is_reusable"] is False
    assert result["output_artifact"] is None
    assert calls == story_runtime.MAX_STORY_SCHEMA_REPAIR_ATTEMPTS
    message = str(result["error"])
    expected_fields = {
        "story_items[2].story_title",
        *{
            f"story_items[2].invest_assessment.{dimension}.{field_name}"
            for dimension in (
                "independent",
                "negotiable",
                "valuable",
                "estimable",
                "small",
                "testable",
            )
            for field_name in ("rationale", "evidence")
        },
    }
    assert all(field in message for field in expected_fields)
    assert "placeholder" not in message.casefold()


@pytest.mark.asyncio
async def test_story_runtime_sentinel_failure_omits_raw_provider_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Persist safe field evidence without raw sentinel output or a preview."""

    async def fake_invoke(_payload: UserStoryWriterInput) -> str:
        return _placeholder_output()

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)
    _isolate_failure_artifacts(monkeypatch, tmp_path)

    result = await story_runtime.run_story_agent_from_state(
        _state(), project_id=1, user_input="Correct the accepted Story set."
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert result["raw_output_preview"] is None
    artifact_id = cast("str", result["failure_artifact_id"])
    artifact = failure_artifacts.read_failure_artifact(artifact_id)
    assert artifact is not None
    assert artifact["raw_output"] is None
    assert artifact["raw_output_length"] == 0
    extra = cast("dict[str, object]", artifact["extra"])
    invalid_fields = cast("list[str]", extra["invalid_fields"])
    assert invalid_fields[0] == "story_items[2].story_title"
    assert len(invalid_fields) == 13  # noqa: PLR2004


@pytest.mark.asyncio
async def test_story_runtime_reference_failure_uses_paths_without_provider_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Keep invalid provider references out of repair and failure evidence."""
    private_reference = "PRIVATE.PROVIDER.229"
    payload = json.loads(_valid_output())
    payload["user_stories"][0]["spec_item_ids"] = [
        "DATA.001",
        private_reference,
    ]
    attempt_inputs: list[UserStoryWriterInput] = []

    async def fake_invoke(attempt_payload: UserStoryWriterInput) -> str:
        attempt_inputs.append(attempt_payload)
        return json.dumps(payload)

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)
    _isolate_failure_artifacts(monkeypatch, tmp_path)

    result = await story_runtime.run_story_agent_from_state(
        _state(), project_id=1, user_input=None
    )

    assert len(attempt_inputs) == EXPECTED_REPAIR_CALL_COUNT
    assert result["failure_stage"] == "output_validation"
    assert result["raw_output_preview"] is None
    repair_input = attempt_inputs[1].user_input or ""
    assert "story_items[0].spec_item_ids" in repair_input
    assert private_reference not in repair_input
    exposed = f"{result!s}\n{caplog.text}"
    assert private_reference not in exposed
    artifact = failure_artifacts.read_failure_artifact(
        cast("str", result["failure_artifact_id"])
    )
    assert artifact is not None
    assert private_reference not in str(artifact)
    assert artifact["raw_output"] is None
    assert artifact["validation_errors"] == [
        {
            "path": "story_items[0].spec_item_ids",
            "type": "specification_reference",
        }
    ]
    assert artifact["exception_type"] is None
    assert artifact["exception_message"] is None
    assert artifact["traceback"] is None
    assert artifact["extra"] == {
        "invalid_fields": ["story_items[0].spec_item_ids"]
    }


@pytest.mark.asyncio
async def test_story_runtime_redacts_statement_sentinel_before_persona_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pre-scan sentinel prose that Pydantic would otherwise echo as input."""
    payload = json.loads(_valid_output())
    payload["user_stories"][0]["statement"] = "[ `TBD` ]"

    async def fake_invoke(_payload: UserStoryWriterInput) -> str:
        return json.dumps(payload)

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)
    _isolate_failure_artifacts(monkeypatch, tmp_path)

    result = await story_runtime.run_story_agent_from_state(
        _state(), project_id=1, user_input=None
    )

    assert result["raw_output_preview"] is None
    assert "tbd" not in str(result["error"]).casefold()
    artifact = failure_artifacts.read_failure_artifact(
        cast("str", result["failure_artifact_id"])
    )
    assert artifact is not None
    assert artifact["raw_output"] is None
    assert artifact["validation_errors"] is None
    assert artifact["exception_type"] is None
    assert artifact["traceback"] is None
    assert artifact["extra"] == {"invalid_fields": ["story_items[0].statement"]}


@pytest.mark.asyncio
async def test_story_runtime_redacts_agent_validation_sentinel_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Sanitize actual item-level Pydantic errors when no partial text exists."""
    payload = json.loads(_valid_output())
    payload["user_stories"][0]["statement"] = "[ `TBD` ]"
    with pytest.raises(ValidationError) as validation:
        UserStoryWriterOutput.model_validate(payload)
    validation_errors = cast(
        "list[dict[str, object]]",
        validation.value.errors(),
    )
    assert validation_errors[0]["loc"] == ("user_stories", 0)
    assert isinstance(validation_errors[0]["input"], dict)
    error_message = "Story agent output validation failed"
    attempt_inputs: list[UserStoryWriterInput] = []

    async def fake_invoke(attempt_payload: UserStoryWriterInput) -> str:
        attempt_inputs.append(attempt_payload)
        raise failure_artifacts.AgentInvocationError(
            error_message,
            validation_errors=validation_errors,
        )

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)
    _isolate_failure_artifacts(monkeypatch, tmp_path)

    result = await story_runtime.run_story_agent_from_state(
        _state(), project_id=1, user_input=None
    )

    assert result["raw_output_preview"] is None
    assert "tbd" not in str(result["error"]).casefold()
    assert len(attempt_inputs) == EXPECTED_REPAIR_CALL_COUNT
    repair_input = attempt_inputs[1].user_input or ""
    assert "story_items[0].statement" in repair_input
    assert "tbd" not in repair_input.casefold()
    artifact = failure_artifacts.read_failure_artifact(
        cast("str", result["failure_artifact_id"])
    )
    assert artifact is not None
    assert artifact["raw_output"] is None
    assert artifact["validation_errors"] is None
    assert artifact["exception_type"] is None
    assert artifact["exception_message"] is None
    assert artifact["traceback"] is None
    assert artifact["extra"] == {"invalid_fields": ["story_items[0].statement"]}


@pytest.mark.asyncio
async def test_story_runtime_redacts_sentinel_from_wrapped_partial_agent_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pre-scan an incomplete embedded object before retaining partial output."""
    raw_output = (
        'prefix {"user_stories":[{"story_title":"placeholder"}]} suffix'
    )
    error_message = "Story agent invocation failed after partial output"

    async def fake_invoke(_payload: UserStoryWriterInput) -> str:
        raise failure_artifacts.AgentInvocationError(
            error_message,
            partial_output=raw_output,
        )

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)
    _isolate_failure_artifacts(monkeypatch, tmp_path)

    result = await story_runtime.run_story_agent_from_state(
        _state(), project_id=1, user_input=None
    )

    assert result["failure_stage"] == "output_validation"
    assert result["raw_output_preview"] is None
    assert "placeholder" not in str(result["error"]).casefold()
    artifact = failure_artifacts.read_failure_artifact(
        cast("str", result["failure_artifact_id"])
    )
    assert artifact is not None
    assert artifact["raw_output"] is None
    assert artifact["validation_errors"] is None
    assert artifact["exception_type"] is None
    assert artifact["exception_message"] is None
    assert artifact["traceback"] is None
    assert artifact["extra"] == {"invalid_fields": ["story_items[0].story_title"]}


@pytest.mark.parametrize(
    ("raw_output", "private_marker"),
    [
        (
            "prefix {\"user_stories\": []} actual "
            "{\"user_stories\":[{\"story_title\":\"placeholder\","
            "\"statement\":\"PRIVATE_RUNTIME_229\"}]}",
            "private_runtime_229",
        ),
        (
            "{\"user_stories\":[{\"story_title\":\"placeholder\","
            "\"statement\":\"PRIVATE_RUNTIME_TRUNCATED_229\"}]",
            "private_runtime_truncated_229",
        ),
    ],
)
@pytest.mark.asyncio
async def test_story_runtime_invalid_json_never_persists_provider_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raw_output: str,
    private_marker: str,
) -> None:
    """Omit unclassifiable Story output instead of retaining provider values."""

    async def fake_invoke(_payload: UserStoryWriterInput) -> str:
        return raw_output

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)
    _isolate_failure_artifacts(monkeypatch, tmp_path)

    result = await story_runtime.run_story_agent_from_state(
        _state(), project_id=1, user_input=None
    )

    assert result["failure_stage"] == "invalid_json"
    assert result["raw_output_preview"] is None
    result_text = str(result).casefold()
    assert "placeholder" not in result_text
    assert private_marker not in result_text
    artifact = failure_artifacts.read_failure_artifact(
        cast("str", result["failure_artifact_id"])
    )
    assert artifact is not None
    assert artifact["raw_output"] is None
    assert artifact["validation_errors"] is None
    assert artifact["exception_type"] is None
    assert artifact["traceback"] is None


@pytest.mark.asyncio
async def test_story_runtime_agent_error_omits_unclassified_partial_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Never retain partial provider text or exception detail from agent errors."""
    raw_output = (
        '{"user_stories":[{"story_title":"placeholder",'
        '"statement":"PRIVATE_PARTIAL_229"}]'
    )
    private_error = "PRIVATE_AGENT_EXCEPTION_229"
    calls = 0

    async def fake_invoke(_payload: UserStoryWriterInput) -> str:
        nonlocal calls
        calls += 1
        raise failure_artifacts.AgentInvocationError(
            private_error,
            partial_output=raw_output,
        )

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)
    _isolate_failure_artifacts(monkeypatch, tmp_path)

    result = await story_runtime.run_story_agent_from_state(
        _state(), project_id=1, user_input=None
    )

    assert calls == 1
    assert result["failure_stage"] == "invocation_exception"
    assert result["raw_output_preview"] is None
    exposed = f"{result!s}\n{caplog.text}".casefold()
    assert "placeholder" not in exposed
    assert "private_partial_229" not in exposed
    assert "private_agent_exception_229" not in exposed
    artifact = failure_artifacts.read_failure_artifact(
        cast("str", result["failure_artifact_id"])
    )
    assert artifact is not None
    assert artifact["raw_output"] is None
    assert artifact["validation_errors"] is None
    assert artifact["exception_type"] is None
    assert artifact["exception_message"] is None
    assert artifact["traceback"] is None


@pytest.mark.asyncio
async def test_story_runtime_allows_substantive_placeholder_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep ordinary valid generation unchanged when prose names placeholders."""
    payload = json.loads(_valid_output())
    story = payload["user_stories"][0]
    story["story_title"] = "Replace placeholder tokens in generated templates"
    story["invest_assessment"]["testable"] = {
        "result": "pass",
        "rationale": "Placeholder replacement has deterministic outcomes.",
        "evidence": "Tests prove each placeholder token is replaced.",
    }

    async def fake_invoke(_payload: UserStoryWriterInput) -> str:
        return json.dumps(payload)

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)

    result = await story_runtime.run_story_agent_from_state(
        _state(), project_id=1, user_input=None
    )

    assert result["success"] is True
    assert result["is_reusable"] is True


@pytest.mark.asyncio
async def test_story_schema_repair_reuses_exact_root_without_semantic_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reuse the exact root during bounded schema repair and avoid semantic work."""
    observed: list[UserStoryWriterInput] = []

    async def fake_invoke(payload: UserStoryWriterInput) -> str:
        observed.append(payload)
        return "not-json" if len(observed) == 1 else _valid_output()

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", fake_invoke)

    result = await story_runtime.run_story_agent_from_state(
        _state(), project_id=1, user_input="Keep the reviewed boundary."
    )

    assert result["success"] is True
    assert len(observed) == EXPECTED_REPAIR_CALL_COUNT
    assert {item.accepted_specification_json for item in observed} == {
        GOLD_PATH.read_text(encoding="utf-8")
    }
    assert {item.accepted_specification_hash for item in observed} == {GOLD_HASH}
    assert "SYSTEM_FEEDBACK" in (observed[1].user_input or "")


@pytest.mark.asyncio
async def test_targeted_correction_uses_writer_output_and_keeps_story_id_host_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use one writer item while retaining the selected Story ID in the host."""
    observed: list[UserStoryWriterInput] = []

    async def fake_invoke(payload: UserStoryWriterInput) -> str:
        observed.append(payload)
        return _valid_output()

    monkeypatch.setattr(story_runtime, "_invoke_story_patch_agent", fake_invoke)

    result = await story_runtime.run_story_agent_from_state(
        _state(),
        project_id=1,
        user_input="Replace only the host-selected Story item.",
        target_story_id=TARGET_STORY_ID,
    )

    assert result["success"] is True
    assert result["draft_kind"] == "story_correction"
    assert result["target_story_id"] == TARGET_STORY_ID
    assert len(result["output_artifact"]["user_stories"]) == 1
    provider_payload = observed[0].model_dump(mode="json")
    assert "story_id" not in provider_payload
    assert "target_refinement_slot" not in provider_payload
    assert "parent_requirement" not in provider_payload


@pytest.mark.asyncio
async def test_targeted_correction_rejects_multiple_replacement_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject a targeted correction that returns more than one provider item."""
    calls = 0

    async def fake_invoke(_payload: UserStoryWriterInput) -> str:
        nonlocal calls
        calls += 1
        return _valid_output(story_count=2)

    monkeypatch.setattr(story_runtime, "_invoke_story_patch_agent", fake_invoke)
    monkeypatch.setattr(story_runtime, "_failure", _fake_failure)

    result = await story_runtime.run_story_agent_from_state(
        _state(), project_id=1, user_input=None, target_story_id=TARGET_STORY_ID
    )

    assert result["success"] is False
    assert result["failure_stage"] == "output_validation"
    assert "exactly one" in str(result["error"])
    assert calls == story_runtime.MAX_STORY_SCHEMA_REPAIR_ATTEMPTS


@pytest.mark.asyncio
async def test_story_input_failure_stops_before_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop an invalid parent evidence reference before provider execution."""
    state = _state()
    state["parent_backlog_spec_item_ids"] = ["REQ.missing"]
    calls: list[str] = []

    async def forbidden_invoke(_payload: UserStoryWriterInput) -> str:
        calls.append("provider")
        return _valid_output()

    monkeypatch.setattr(story_runtime, "_invoke_story_agent", forbidden_invoke)
    monkeypatch.setattr(story_runtime, "_failure", _fake_failure)

    result = await story_runtime.run_story_agent_from_state(
        state, project_id=1, user_input=None
    )

    assert result["success"] is False
    assert result["failure_stage"] == "input_validation"
    assert calls == []
