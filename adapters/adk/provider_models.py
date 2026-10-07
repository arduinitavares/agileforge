# adapters/adk/provider_models.py
"""Shared model construction; retry policy is captured by each host action."""

import litellm
from google.adk.models.lite_llm import LiteLlm

from adapters.adk.provider_retry import RetryingOpenRouterClient

setattr(litellm, "suppress_debug_info", True)  # noqa: B010


def create_openrouter_model(*, model_id: str, **completion_kwargs: object) -> LiteLlm:
    """Preserve generation settings and install the only supported provider client."""
    if "llm_client" in completion_kwargs:
        message = "The provider model factory owns its completion client."
        raise ValueError(message)
    return LiteLlm(
        model=model_id,
        llm_client=RetryingOpenRouterClient(),
        **(completion_kwargs | {"num_retries": 0, "max_retries": 0}),
    )
