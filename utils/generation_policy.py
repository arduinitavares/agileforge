# utils/generation_policy.py
"""One nonsecret policy for model-backed generation, independent of role budgets."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from pydantic import JsonValue

type JsonObject = dict[str, JsonValue]

RETIRED_GENERATION_SETTINGS: tuple[str, ...] = (
    "VISION_INTERVIEWER_MAX_TOKENS",
    "SPECIFICATION_STRUCTURER_MAX_TOKENS",
    "SPEC_VALIDATOR_MAX_TOKENS",
    "BACKLOG_PRIMER_MAX_TOKENS",
    "ROADMAP_BUILDER_MAX_TOKENS",
    "STORY_WRITER_MAX_TOKENS",
    "SPRINT_PLANNER_MAX_TOKENS",
)
RETIRED_GENERATION_ARGUMENTS: frozenset[str] = frozenset(
    {
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "timeout",
        "request_timeout",
    }
)
_RETIRED_REASONING_ARGUMENTS: frozenset[str] = frozenset(
    {"max_tokens", "max_completion_tokens", "max_output_tokens", "budget_tokens"}
)


def _clean_generation_container(
    arguments: Mapping[str, object],
) -> Mapping[str, object]:
    """Copy a supported container only when a retired argument changes it."""
    cleaned = {
        key: value
        for key, value in arguments.items()
        if key not in RETIRED_GENERATION_ARGUMENTS
    }
    reasoning = arguments.get("reasoning")
    if isinstance(reasoning, Mapping):
        controls = {
            key: value
            for key, value in reasoning.items()
            if key not in _RETIRED_REASONING_ARGUMENTS
        }
        if len(controls) != len(reasoning):
            if controls:
                cleaned["reasoning"] = controls
            else:
                cleaned.pop("reasoning", None)
    if len(cleaned) == len(arguments) and cleaned.get("reasoning") is reasoning:
        return arguments
    return cleaned


def remove_generation_limits(arguments: Mapping[str, object]) -> dict[str, object]:
    """Remove legacy caps without mutating payloads or masking selected controls."""
    normalized = dict(_clean_generation_container(arguments))
    body = arguments.get("extra_body")
    if isinstance(body, Mapping):
        cleaned_body = _clean_generation_container(cast("Mapping[str, object]", body))
        root_reasoning = normalized.get("reasoning")
        body_reasoning = cleaned_body.get("reasoning")
        if isinstance(root_reasoning, Mapping) and isinstance(body_reasoning, Mapping):
            if root_reasoning and not body_reasoning:
                cleaned_body = dict(cleaned_body)
                cleaned_body.pop("reasoning")
            elif body_reasoning and not root_reasoning:
                normalized.pop("reasoning")
        if cleaned_body is not body:
            normalized["extra_body"] = cleaned_body
    return normalized


def get_generation_policy() -> JsonObject:
    """Describe requested behavior; resolved provider evidence belongs to each call."""
    return {
        "schema_version": "agileforge.generation-policy.v1",
        "application_output_cap": None,
        "application_reasoning_cap": None,
        "generation_timeout_seconds": None,
        "provider_capacity_source": "openrouter_published_endpoint_capacities",
        "actual_prompt_allowance": "unverified",
        "route_eligibility": "provider_enforced_unverified",
        "resolved_request_evidence": "provider_try_audit",
        "deprecated_settings_ignored": list(RETIRED_GENERATION_SETTINGS),
    }
