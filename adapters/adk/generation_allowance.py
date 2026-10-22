# adapters/adk/generation_allowance.py
"""Resolve published endpoint capacities without pretending to count the prompt."""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import UTC, datetime
from typing import Literal, cast

import httpx

from adapters.adk.errors import GenerationProviderError
from services.contracts.generation_policy import GenerationAllowance
from utils.model_config import get_model_token_limit_args
from workflow.contracts import WorkflowErrorCode

_MODEL_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*/[A-Za-z0-9][A-Za-z0-9_.:-]*")
_METADATA_TIMEOUT_SECONDS: float = 30.0


def _unavailable() -> GenerationProviderError:
    code = WorkflowErrorCode.PROVIDER_CAPACITY_UNAVAILABLE.value
    return GenerationProviderError(
        code=code,
        message=(
            "Published provider completion capacity could not be resolved. "
            "No generation was sent. Check the selected model's OpenRouter "
            "endpoint metadata and retry with a new idempotency key. "
            "The same key replays this failure; actual-prompt allocation "
            "remains unverified."
        ),
        diagnostic={"code": code, "actual_prompt_allowance": "unverified"},
    )


def _positive_count(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise _unavailable()
    return value


def _endpoint_capacities(endpoints: list[object]) -> tuple[list[int], list[int], int]:
    capacities: list[int] = []
    contexts: list[int] = []
    unknown = 0
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            raise _unavailable()
        fields = cast("dict[str, object]", endpoint)
        context = fields.get("context_length")
        if context is not None:
            context = _positive_count(context)
            contexts.append(context)
        capacity = fields.get("max_completion_tokens")
        if capacity is None:
            unknown += 1
            continue
        capacity = _positive_count(capacity)
        if context is not None and capacity > context:
            raise _unavailable()
        capacities.append(capacity)
    if not capacities:
        raise _unavailable()
    return capacities, contexts, unknown


def request_rejection(
    allowance: GenerationAllowance, status: int, *, context_limit: bool
) -> GenerationProviderError:
    """Project owned rejection evidence without retaining provider prose or input."""
    code = (
        "PROVIDER_CONTEXT_LIMIT_EXCEEDED"
        if context_limit
        else "PROVIDER_REQUEST_REJECTED"
    )
    explanation = (
        "Provider reported a context-limit failure. "
        if context_limit
        else "Provider rejected the request; a context-limit cause was not confirmed. "
    )
    return GenerationProviderError(
        code=code,
        message=(
            explanation + f"HTTP {status}; published completion ceiling "
            f"{allowance.completion_tokens}; actual prompt allowance "
            "and route eligibility remain unverified. Review the model context and "
            "request constraints, then retry with a new idempotency key. "
            "The same key replays this failure. Selected model and effort "
            "were preserved."
        ),
        diagnostic={
            "code": code,
            "http_status": status,
            "generation_allowance": allowance.model_dump(mode="json"),
        },
    )


async def resolve_generation_allowance(
    model_id: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> GenerationAllowance:
    """Select the largest known published ceiling, leaving context/routing explicit."""
    slug = model_id.removeprefix("openrouter/")
    if not model_id.startswith("openrouter/") or not _MODEL_SLUG.fullmatch(slug):
        raise _unavailable()
    source = f"https://openrouter.ai/api/v1/models/{slug}/endpoints"
    try:
        async with (
            asyncio.timeout(_METADATA_TIMEOUT_SECONDS),
            httpx.AsyncClient(
                transport=transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(
                    _METADATA_TIMEOUT_SECONDS, connect=10.0, pool=10.0
                ),
            ) as client,
        ):
            response = await client.get(source, headers={"Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
        data = payload["data"]
        # This endpoint resolves the requested slug; do not switch the send's model.
        if data["id"] != slug:
            raise _unavailable()
        endpoints = data["endpoints"]
        if not isinstance(endpoints, list) or not endpoints:
            raise _unavailable()
        capacities, contexts, unknown = _endpoint_capacities(endpoints)
        maximum = max(capacities)
        parameter = next(iter(get_model_token_limit_args(model_id, maximum)))
        return GenerationAllowance(
            model_id=model_id,
            completion_tokens=maximum,
            maximum_published_context=max(contexts) if contexts else None,
            known_capacity_endpoints=len(capacities),
            unknown_capacity_endpoints=unknown,
            request_parameter=cast(
                'Literal["max_tokens", "max_completion_tokens"]', parameter
            ),
            source_url=source,
            snapshot_sha256="sha256:" + hashlib.sha256(response.content).hexdigest(),
            retrieved_at=datetime.now(UTC),
        )
    except GenerationProviderError:
        raise
    except (
        httpx.HTTPError,
        TimeoutError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ):
        raise _unavailable() from None
