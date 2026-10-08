# adapters/adk/provider_transport.py
"""Owned HTTP transport without SDK connection resends or redirect following."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, cast

import httpx
import litellm
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

from adapters.adk.provider_retry import (
    _MAX_HTTP_STATUS,
    _OWNED_STATUS_ATTRIBUTE,
    ProviderAttemptStopped,
    ProviderDeadlineExceeded,
    _status_code,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from httpx._types import (
        HeaderTypes,
        QueryParamTypes,
        RequestContent,
        RequestData,
        RequestFiles,
    )
    from litellm import ModelResponse

_OPENROUTER_COMPLETION_URL: httpx.URL = httpx.URL(
    "https://openrouter.ai/api/v1/chat/completions"
)


def _response_status(response: httpx.Response) -> int:
    """Project only the current HTTP response or its bounded HTTP200 error code."""
    if response.status_code != HTTPStatus.OK:
        return response.status_code
    try:
        body = response.json()
    except ValueError:
        body = None
    provider_error = body.get("error") if isinstance(body, dict) else None
    code = (
        _status_code(provider_error.get("code"))
        if isinstance(provider_error, dict)
        else None
    )
    return (
        code
        if code is not None and HTTPStatus.CONTINUE <= code <= _MAX_HTTP_STATUS
        else response.status_code
    )


class OneSendAsyncHTTPHandler(AsyncHTTPHandler):
    """Keep the pinned typed client seam while replacing its implicit retry path."""

    def __init__(
        self, client: httpx.AsyncClient, on_dispatch: Callable[[], None] | None = None
    ) -> None:
        # Calling the SDK constructor would create an additional redirecting client.
        """Capture this instance's injected deterministic seams."""
        self.client = client
        self.timeout = client.timeout
        self.event_hooks = {}
        self.transport_error: httpx.TransportError | None = None
        self.response_headers: httpx.Headers | None = None
        self.response: httpx.Response | None = None
        self._on_dispatch = on_dispatch
        self.dispatch_error: (
            ProviderAttemptStopped | ProviderDeadlineExceeded | None
        ) = None
        self._dispatched = False

    async def post(  # noqa: PLR0913 - pinned SDK override signature
        self,
        url: str,
        data: object = None,
        json: object = None,
        params: object = None,
        headers: object = None,
        timeout: float | httpx.Timeout | None = None,  # noqa: ASYNC109
        stream: bool = False,
        logging_obj: object = None,
        files: object = None,
        content: object = None,
    ) -> httpx.Response:
        """Perform exactly one send; expose errors to real OpenRouter translation."""
        del logging_obj
        if isinstance(data, (str, bytes)) and content is None:
            content, data = data, None
        if stream:
            message = "Streaming transport is unsupported."
            raise ValueError(message)
        request = self.client.build_request(
            "POST",
            url,
            data=cast("RequestData | None", data),
            json=json,
            params=cast("QueryParamTypes | None", params),
            headers=cast("HeaderTypes | None", headers),
            timeout=self.timeout if timeout is None else timeout,
            files=cast("RequestFiles | None", files),
            content=cast("RequestContent | None", content),
        )
        try:
            self._notify_dispatch(request.url)
        except (ProviderAttemptStopped, ProviderDeadlineExceeded) as error:
            self.dispatch_error = error
            raise
        try:
            response = await self.client.send(
                request, stream=False, follow_redirects=False
            )
        except httpx.TransportError as error:
            self.transport_error = error
            raise
        self.response_headers = response.headers
        self.response = response
        response.raise_for_status()
        return response

    def _notify_dispatch(self, url: httpx.URL) -> None:
        """Reject an unsupported destination or second send before invoking HTTPX."""
        if url != _OPENROUTER_COMPLETION_URL or self._dispatched:
            raise ProviderAttemptStopped()
        if self._on_dispatch is not None:
            self._on_dispatch()
        self._dispatched = True


class OpenRouterOneSendCompletion:
    """Create and close resources on each try, safe across separate event loops."""

    owns_dispatch_notifications: bool = True

    def __init__(
        self, *, transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None
    ) -> None:
        """Capture this instance's injected deterministic seams."""
        self._transport_factory = transport_factory

    async def __call__(
        self, *, on_dispatch: Callable[[], None] | None = None, **kwargs: object
    ) -> ModelResponse:
        """Require owned response provenance, including setup and cleanup failures."""
        owned_errors: list[Exception] = []
        try:
            return await self._complete(owned_errors, on_dispatch=on_dispatch, **kwargs)
        except Exception as error:
            if not owned_errors or error is not owned_errors[0]:
                setattr(error, _OWNED_STATUS_ATTRIBUTE, None)
            raise

    async def _complete(
        self,
        owned_errors: list[Exception],
        *,
        on_dispatch: Callable[[], None] | None,
        **kwargs: object,
    ) -> ModelResponse:
        """Run pinned OpenRouter translation with one owned HTTP client."""
        transport = (
            self._transport_factory()
            if self._transport_factory is not None
            else httpx.AsyncHTTPTransport(retries=0)
        )
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            event_hooks={},
            timeout=cast("float", kwargs.get("timeout", 600.0)),
        ) as client:
            handler = OneSendAsyncHTTPHandler(client, on_dispatch)
            try:
                return await litellm.acompletion(**kwargs, client=handler)
            except Exception as error:
                projected = error
                if handler.dispatch_error is not None:
                    projected = handler.dispatch_error
                # The pinned mapper invents a 500 for statusless transport errors.
                # Keep actual transport provenance instead of treating it as a response.
                elif isinstance(handler.transport_error, httpx.TimeoutException):
                    projected = litellm.Timeout(
                        message="Provider request timed out.",
                        model=cast("str", kwargs["model"]),
                        llm_provider="openrouter",
                    )
                elif handler.transport_error is not None:
                    projected = handler.transport_error
                status: int | None = None
                if (
                    handler.dispatch_error is None
                    and handler.transport_error is None
                    and handler.response is not None
                ):
                    status = _response_status(handler.response)
                # None is authoritative: SDK preparation and historical causes
                # cannot establish a current provider response that did not exist.
                setattr(projected, _OWNED_STATUS_ATTRIBUTE, status)
                if handler.response_headers is not None:
                    guidance = handler.response_headers.get("retry-after")
                    if guidance is not None:
                        setattr(  # noqa: B010
                            projected,
                            "litellm_response_headers",
                            {"retry-after": guidance},
                        )
                owned_errors.append(projected)
                if (
                    handler.dispatch_error is not None
                    or handler.transport_error is not None
                ):
                    raise projected from None
                raise
