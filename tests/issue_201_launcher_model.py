# tests/issue_201_launcher_model.py
"""Provider-free model used only by the issue #201 launcher regression."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from openai import AuthenticationError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from google.adk.models.llm_request import LlmRequest


def _profile_root() -> Path:
    database_url = os.environ["AGILEFORGE_DB_URL"]
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        message = "issue #201 fixture requires a checkout-local SQLite profile"
        raise RuntimeError(message)
    return Path(database_url.removeprefix(prefix)).parent


class Issue201LauncherModel(BaseLlm):
    """Raise or truncate deterministically while recording the product child PID."""

    async def generate_content_async(
        self,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        """Return the selected network-free terminal provider behavior."""
        del llm_request, stream
        logs = _profile_root() / "logs"
        failure_mode = (
            (logs / "issue-201-failure-mode").read_text(encoding="utf-8").strip()
        )
        with (logs / "issue-201-provider-calls").open(
            "a",
            encoding="utf-8",
        ) as calls:
            calls.write(f"{os.getpid()}\n")
        if failure_mode == "authentication":
            request = httpx.Request("POST", "https://provider.invalid/v1/chat")
            response = httpx.Response(status_code=401, request=request)
            message = "fixture provider rejected authentication"
            raise AuthenticationError(
                message,
                response=response,
                body=None,
            )
        if failure_mode != "incomplete":
            message = f"unknown issue #201 failure mode: {failure_mode!r}"
            raise RuntimeError(message)
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text='{"payload":')],
            ),
            finish_reason=types.FinishReason.MAX_TOKENS,
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=100,
                candidates_token_count=64,
                total_token_count=164,
            ),
        )


__all__ = ["Issue201LauncherModel"]
