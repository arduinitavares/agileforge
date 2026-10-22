# utils/generation_policy.py
"""One nonsecret policy for model-backed generation, independent of role budgets."""

from __future__ import annotations

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
