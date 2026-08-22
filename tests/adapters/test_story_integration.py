"""Integration replay test for capturing raw story-agent output.

Run with:
    pytest -m integration tests/test_user_story_writer_integration.py -s -v
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import ValidationError

from adapters.adk.agents.story import create_user_story_writer_agent
from services.contracts.story import (
    UserStoryWriterInput,
    UserStoryWriterOutput,
)
from utils.adk_runner import extract_final_response_text, parse_json_payload
from utils.runtime_config import STORY_RUNNER_IDENTITY

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse

REPO_ROOT = Path(__file__).resolve().parents[1]
INPUT_FIXTURE_PATH = REPO_ROOT / "input_for_test.txt"


def _load_story_payload() -> tuple[UserStoryWriterInput, str]:
    payload_text = INPUT_FIXTURE_PATH.read_text(encoding="utf-8")
    payload = UserStoryWriterInput.model_validate_json(payload_text)
    return payload, payload_text


def _describe_parse_failure(raw_text: str) -> str:
    candidate = (raw_text or "").strip()
    if not candidate:
        return "No response text was produced."

    fenced = re.search(
        r"```(?:json)?\s*(.*?)\s*```",
        candidate,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced:
        candidate = fenced.group(1).strip()

    try:
        parsed = json.loads(candidate)
        return (
            f"Response parsed as {type(parsed).__name__}, but a top-level JSON object "
            "was required."
        )
    except json.JSONDecodeError as exc:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or end < start:
            return f"json.loads failed on the raw response: {exc}"

        sliced = candidate[start : end + 1]
        try:
            parsed = json.loads(sliced)
            return (
                "Direct json.loads failed, but the extracted object candidate parsed "
                f"as {type(parsed).__name__} instead of dict."
            )
        except json.JSONDecodeError as inner_exc:
            return (
                f"json.loads failed on raw response: {exc}; "
                f"extracted object candidate also failed: {inner_exc}"
            )


@dataclass(frozen=True)
class _DiagnosticArtifact:
    """Values persisted when the integration replay fails."""

    artifact_path: Path
    payload_text: str
    raw_output: str | None
    parsed_json: dict[str, Any] | None
    parse_error: str | None
    validation_errors: object


def _write_diagnostic_artifact(diagnostic: _DiagnosticArtifact) -> None:
    """Persist a replay diagnostic artifact."""
    artifact = {
        "source_payload_path": str(INPUT_FIXTURE_PATH),
        "instruction_resource": "adapters.adk.prompts/story.txt",
        "target_schema": UserStoryWriterOutput.__name__,
        "payload_length": len(diagnostic.payload_text),
        "raw_output": diagnostic.raw_output,
        "raw_output_length": (
            len(diagnostic.raw_output) if diagnostic.raw_output is not None else 0
        ),
        "parsed_json": diagnostic.parsed_json,
        "parse_error": diagnostic.parse_error,
        "validation_errors": diagnostic.validation_errors,
    }
    diagnostic.artifact_path.write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("OPEN_ROUTER_API_KEY"), reason="No API Key")
async def test_story_agent_replay_captures_raw_output_from_input_fixture(
    tmp_path: Path,
) -> None:
    """Verify story agent replay captures raw output from input fixture."""
    payload, payload_text = _load_story_payload()

    agent = create_user_story_writer_agent()
    original_output_schema = agent.output_schema
    assert original_output_schema is UserStoryWriterOutput

    async def _preserve_request_schema(
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> LlmResponse | None:
        del callback_context
        llm_request.set_output_schema(original_output_schema)
        return None

    agent.output_schema = None
    agent.output_key = None
    agent.before_model_callback = _preserve_request_schema

    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name=STORY_RUNNER_IDENTITY.app_name,
        session_service=session_service,
    )
    session = await session_service.create_session(
        app_name=STORY_RUNNER_IDENTITY.app_name,
        user_id="integration_story_replay",
    )

    message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=payload.model_dump_json())],
    )

    events: list[object] = [
        event
        async for event in runner.run_async(
            user_id="integration_story_replay",
            session_id=session.id,
            new_message=message,
        )
    ]

    raw_output = extract_final_response_text(events) or None
    parsed_json = parse_json_payload(raw_output or "")
    artifact_path = tmp_path / "story_integration_diagnostic.json"

    if not raw_output:
        _write_diagnostic_artifact(
            _DiagnosticArtifact(
                artifact_path=artifact_path,
                payload_text=payload_text,
                raw_output=raw_output,
                parsed_json=None,
                parse_error="No final text response extracted from runner events.",
                validation_errors=None,
            )
        )
        pytest.fail(
            f"Story replay produced no final text. Diagnostic saved to {artifact_path}"
        )

    if parsed_json is None:
        _write_diagnostic_artifact(
            _DiagnosticArtifact(
                artifact_path=artifact_path,
                payload_text=payload_text,
                raw_output=raw_output,
                parsed_json=None,
                parse_error=_describe_parse_failure(raw_output),
                validation_errors=None,
            )
        )
        pytest.fail(
            f"Story replay returned invalid JSON. Diagnostic saved to {artifact_path}"
        )

    try:
        validated = UserStoryWriterOutput.model_validate(parsed_json)
    except ValidationError as exc:
        _write_diagnostic_artifact(
            _DiagnosticArtifact(
                artifact_path=artifact_path,
                payload_text=payload_text,
                raw_output=raw_output,
                parsed_json=parsed_json,
                parse_error=None,
                validation_errors=exc.errors(),
            )
        )
        pytest.fail(
            "Story replay returned schema-invalid JSON. Diagnostic saved to "
            f"{artifact_path}"
        )

    assert raw_output.strip(), "Expected a non-empty raw model response"
    assert validated.user_stories, "Expected at least one generated user story"
