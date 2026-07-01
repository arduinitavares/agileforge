"""Utilities for running ADK agents and extracting text/JSON responses."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Protocol, cast

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from utils.failure_artifacts import AgentInvocationError

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Iterator


class RunnerIdentityLike(Protocol):
    """Minimal runner identity required to create an ADK session."""

    app_name: str
    user_id: str


def _iter_exception_chain(exc: BaseException) -> Iterable[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _extract_validation_errors(exc: BaseException) -> list[dict[str, Any]] | None:
    for candidate in _iter_exception_chain(exc):
        errors = getattr(candidate, "errors", None)
        if not callable(errors):
            continue
        try:
            raw_errors = errors()
        except TypeError:
            continue
        if isinstance(raw_errors, list):
            return raw_errors
    return None


def extract_final_response_text(events: list[object]) -> str:
    """Return the last non-empty text payload emitted by a runner event stream."""
    for event in reversed(events):
        content = getattr(event, "content", None)
        if not content:
            continue
        parts = getattr(content, "parts", None) or []
        text_parts = [
            getattr(part, "text", "") for part in parts if getattr(part, "text", "")
        ]
        merged = "\n".join(text_parts).strip()
        if merged:
            return merged
    return ""


def extract_partial_response_text(events: list[object]) -> str:
    """Return any text fragments collected before a runner failed."""
    final = extract_final_response_text(events)
    if final:
        return final

    fragments: list[str] = []
    for event in events:
        content = getattr(event, "content", None)
        if not content:
            continue
        for part in getattr(content, "parts", None) or []:
            text = getattr(part, "text", "")
            if text:
                fragments.append(text)
    return "\n".join(fragments).strip()


def _parse_json_dict(candidate: str) -> dict[str, Any] | None:
    parsed = json.loads(candidate)
    return cast("dict[str, Any]", parsed) if isinstance(parsed, dict) else None


def _iter_json_dict_candidates(raw_text: str) -> Iterator[dict[str, Any]]:
    decoder = json.JSONDecoder()
    cursor = 0
    while cursor < len(raw_text):
        start = raw_text.find("{", cursor)
        if start == -1:
            return

        try:
            parsed, end = decoder.raw_decode(raw_text, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue

        if isinstance(parsed, dict):
            yield cast("dict[str, Any]", parsed)
        cursor = max(end, start + 1)


def _json_dict_matches_required_keys(
    payload: dict[str, Any],
    required_keys: Collection[str] | None,
) -> bool:
    if required_keys is None:
        return True
    return all(key in payload for key in required_keys)


def _parse_json_payload_without_required_keys(candidate: str) -> dict[str, Any] | None:
    try:
        return _parse_json_dict(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or end < start:
            return None

        try:
            return _parse_json_dict(candidate[start : end + 1])
        except json.JSONDecodeError:
            return None


def _parse_json_payload_with_required_keys(
    candidate: str,
    required_keys: Collection[str],
) -> dict[str, Any] | None:
    try:
        return _parse_json_dict(candidate)
    except json.JSONDecodeError:
        pass

    for parsed in _iter_json_dict_candidates(candidate):
        if _json_dict_matches_required_keys(parsed, required_keys):
            return parsed
    return None


def parse_json_payload(
    raw_text: str,
    *,
    required_keys: Collection[str] | None = None,
) -> dict[str, Any] | None:
    """Parse a JSON object from raw model text or a fenced JSON block."""
    candidate = (raw_text or "").strip()
    if not candidate:
        return None

    fenced = re.search(
        r"```(?:json)?\s*(.*?)\s*```",
        candidate,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced:
        candidate = fenced.group(1).strip()

    if required_keys is None:
        return _parse_json_payload_without_required_keys(candidate)

    return _parse_json_payload_with_required_keys(candidate, required_keys)


def get_agent_model_info(agent: object) -> dict[str, Any]:
    """Return lightweight model metadata for failure artifacts and diagnostics."""
    model = getattr(agent, "model", None)
    return {
        "agent_name": getattr(agent, "name", None),
        "model_class": type(model).__name__ if model is not None else None,
        "model_id": getattr(model, "model", None),
        "extra_body": getattr(model, "extra_body", None),
    }


async def invoke_agent_to_text(
    *,
    agent: object,
    runner_identity: RunnerIdentityLike,
    payload_json: str,
    no_text_error: str,
) -> str:
    """Run an ADK agent with a JSON payload and return the final text response."""
    session_service = InMemorySessionService()
    runner = Runner(
        agent=cast("Any", agent),
        app_name=runner_identity.app_name,
        session_service=session_service,
    )
    session = await session_service.create_session(
        app_name=runner_identity.app_name,
        user_id=runner_identity.user_id,
    )

    events: list[Any] = []
    message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=payload_json)],
    )

    try:
        events.extend(
            [
                event
                async for event in runner.run_async(
                    user_id=runner_identity.user_id,
                    session_id=session.id,
                    new_message=message,
                )
            ]
        )
    except Exception as exc:  # pylint: disable=broad-except
        partial_output = extract_partial_response_text(events) or None
        raise AgentInvocationError(
            str(exc),
            partial_output=partial_output,
            event_count=len(events),
            validation_errors=_extract_validation_errors(exc),
        ) from exc

    response_text = extract_final_response_text(events)
    if not response_text:
        raise ValueError(no_text_error)
    return response_text
