"""Serve MCP within the existing API process and runtime ownership scope."""

from __future__ import annotations

import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import (
    CallToolResult,
    ClientNotification,
    ClientRequest,
    JSONRPCMessage,
    JSONRPCNotification,
    JSONRPCRequest,
    TextContent,
    Tool,
)
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from adapters.mcp.catalogue import build_catalogue

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from starlette.types import Message, Receive, Scope, Send

    from services.application import AgileForgeApplication

MCP_BEARER_ENV: str = "AGILEFORGE_MCP_BEARER_TOKEN"
MAX_REQUEST_BYTES: int = 4 * 1024 * 1024
MAX_TRANSPORT_PORT: int = 65535
logger: logging.Logger = logging.getLogger(name=__name__)


def _redact_secrets(value: object, secrets: tuple[str, ...]) -> object:
    """Redact runtime credentials from strings without changing stored facts."""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {
            _redact_secrets(key, secrets): _redact_secrets(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secrets(item, secrets) for item in value]
    return value


def _tool_result(payload: dict[str, object], *, failed: bool) -> CallToolResult:
    """Return one lossless structured result with a compatible text rendering."""
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))
        ],
        structuredContent=payload,
        isError=failed,
    )


def _matches_transport_identity(value: str, allowed: tuple[str, ...]) -> bool:
    """Match only complete identities or a previously validated numeric port."""
    return value in allowed or any(
        pattern.endswith(":*")
        and value.startswith(pattern[:-1])
        and value[len(pattern) - 1 :].isascii()
        and value[len(pattern) - 1 :].isdigit()
        for pattern in allowed
    )


def _valid_authority(value: str) -> bool:
    """Reject ambiguous authority syntax before SDK wildcard matching."""
    if not value.isascii() or any(char.isspace() for char in value):
        return False
    try:
        parsed = urlsplit(f"http://{value}")
        port = parsed.port
    except ValueError:
        return False
    return (
        bool(parsed.hostname)
        and parsed.netloc == value
        and parsed.username is None
        and parsed.password is None
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and not value.endswith(":")
        and (port is None or 0 < port <= MAX_TRANSPORT_PORT)
    )


def _valid_origin(value: str) -> bool:
    """Accept a serialized origin without userinfo, path, query, or fragment."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and value == f"{parsed.scheme}://{parsed.netloc}"
        and _valid_authority(parsed.netloc)
    )


@dataclass(frozen=True)
class McpSettings:
    """Keep the runtime credential private and restrict transport identities."""

    bearer_token: str | None = field(default=None, repr=False)
    allowed_hosts: tuple[str, ...] = (
        "localhost",
        "localhost:*",
        "127.0.0.1",
        "127.0.0.1:*",
        "[::1]",
        "[::1]:*",
    )
    allowed_origins: tuple[str, ...] = (
        "http://localhost",
        "http://localhost:*",
        "http://127.0.0.1",
        "http://127.0.0.1:*",
        "http://[::1]",
        "http://[::1]:*",
    )

    @classmethod
    def from_environment(cls) -> McpSettings:
        """Read credentials at runtime without enabling an anonymous fallback."""
        token = os.environ.get(MCP_BEARER_ENV)
        return cls(bearer_token=token if token and token.strip() else None)


class McpEndpoint:
    """Own the protocol lifetime without owning another application runtime."""

    def __init__(
        self,
        application_factory: Callable[[], AgileForgeApplication],
        identity_factory: Callable[[], dict[str, object]],
        settings: McpSettings | None = None,
    ) -> None:
        """Retain lazy suppliers for the API's existing authority."""
        self._application_factory = application_factory
        self._identity_factory = identity_factory
        self._configured_settings = settings
        self._settings = McpSettings()
        self._manager: StreamableHTTPSessionManager | None = None
        self._tool_names: frozenset[str] = frozenset()
        self._secret_values: tuple[str, ...] = ()

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Start fresh transport state inside the API's lifetime lock."""
        self._settings = self._configured_settings or McpSettings.from_environment()
        self._secret_values = tuple(
            value
            for value in (
                self._settings.bearer_token,
                os.environ.get("OPEN_ROUTER_API_KEY"),
            )
            if value
        )
        server: Server = Server("AgileForge")
        catalogue = build_catalogue(self._application_factory, self._identity_factory)
        tools = {definition.name: definition for definition in catalogue}
        self._tool_names = frozenset(tools)

        @server.list_tools()
        async def list_tools() -> list[Tool]:
            return [definition.as_tool() for definition in catalogue]

        @server.call_tool(validate_input=False)
        async def call_tool(name: str, arguments: dict[str, object]) -> CallToolResult:
            definition = tools.get(name)
            if definition is None:
                return _tool_result(
                    {"error": {"code": "UNKNOWN_TOOL", "message": "Unknown tool."}},
                    failed=True,
                )
            try:
                payload = await run_in_threadpool(definition.execute, arguments)
            except ValidationError:
                return _tool_result(
                    {
                        "error": {
                            "code": "INVALID_ARGUMENTS",
                            "message": "Tool arguments failed validation.",
                        }
                    },
                    failed=True,
                )
            except Exception as error:  # noqa: BLE001 - sanitize the transport boundary
                # Exception text and tracebacks can contain credential-bearing input.
                logger.error("MCP operation failed (%s)", type(error).__name__)  # noqa: TRY400
                return _tool_result(
                    {
                        "error": {
                            "code": "INTERNAL_ERROR",
                            "message": "Operation failed.",
                        }
                    },
                    failed=True,
                )
            return _tool_result(
                cast(
                    "dict[str, object]", _redact_secrets(payload, self._secret_values)
                ),
                failed=payload.get("ok") is False,
            )

        manager = StreamableHTTPSessionManager(
            server,
            stateless=True,
            json_response=True,
            security_settings=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=list(self._settings.allowed_hosts),
                allowed_origins=list(self._settings.allowed_origins),
            ),
        )
        async with manager.run():
            self._manager = manager
            try:
                yield
            finally:
                self._manager = None
                self._settings = McpSettings()
                self._tool_names = frozenset()
                self._secret_values = ()

    def _protocol_error(self, body: bytes) -> JSONResponse | None:
        """Validate envelopes before SDK diagnostics can echo untrusted values."""
        try:
            message = JSONRPCMessage.model_validate_json(body).root
        except ValidationError:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32700,
                        "message": "Invalid JSON-RPC message.",
                    },
                },
                status_code=400,
            )
        wire = message.model_dump(mode="json")
        params = wire.get("params")
        identifiers = (
            wire.get("id"),
            params.get("requestId") if isinstance(params, dict) else None,
        )
        if any(
            isinstance(identifier, str) and secret in identifier
            for identifier in identifiers
            for secret in self._secret_values
        ):
            return JSONResponse(
                {"error": "Invalid request identifier"}, status_code=400
            )
        try:
            if isinstance(message, JSONRPCRequest):
                ClientRequest.model_validate(wire)
            elif isinstance(message, JSONRPCNotification):
                ClientNotification.model_validate(wire)
            else:
                return None
        except ValidationError:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": wire.get("id"),
                    "error": {
                        "code": -32602,
                        "message": "Invalid MCP request parameters.",
                    },
                },
                status_code=400,
            )
        if (
            isinstance(message, JSONRPCRequest)
            and message.method == "tools/call"
            and (
                message.params is None
                or message.params.get("name") not in self._tool_names
            )
        ):
            result = _tool_result(
                {"error": {"code": "UNKNOWN_TOOL", "message": "Unknown tool."}},
                failed=True,
            )
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": message.id,
                    "result": result.model_dump(mode="json"),
                }
            )
        return None

    async def _validated_body(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> bytes | None:
        """Bound buffering and reject malformed envelopes without logging input."""
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return None
            body.extend(event.get("body", b""))
            if len(body) > MAX_REQUEST_BYTES:
                await JSONResponse({"error": "Request too large"}, status_code=413)(
                    scope, receive, send
                )
                return None
            if not event.get("more_body", False):
                break
        raw = bytes(body)
        error = self._protocol_error(raw)
        if error is not None:
            await error(scope, receive, send)
            return None
        return raw

    def _authorized(self, scope: Scope) -> bool:
        """Require one unambiguous runtime bearer using constant-time comparison."""
        authorizations = [
            value
            for name, value in scope["headers"]
            if name.lower() == b"authorization"
        ]
        token = self._settings.bearer_token
        if token and len(authorizations) == 1:
            scheme, _, credential = authorizations[0].partition(b" ")
            return scheme.lower() == b"bearer" and hmac.compare_digest(
                credential,
                token.encode("utf-8"),
            )
        return False

    def _transport_error(self, scope: Scope) -> JSONResponse | None:
        """Reject ambiguous identities and content types without logging values."""
        hosts = [value for name, value in scope["headers"] if name.lower() == b"host"]
        host = hosts[0].decode("latin-1").lower() if len(hosts) == 1 else ""
        if not _valid_authority(host) or not _matches_transport_identity(
            host,
            self._settings.allowed_hosts,
        ):
            return JSONResponse({"error": "Invalid Host header"}, status_code=421)
        origins = [
            value for name, value in scope["headers"] if name.lower() == b"origin"
        ]
        origin = origins[0].decode("latin-1").lower() if len(origins) == 1 else ""
        if origins and (
            len(origins) != 1
            or not _valid_origin(origin)
            or not _matches_transport_identity(origin, self._settings.allowed_origins)
        ):
            return JSONResponse({"error": "Invalid Origin header"}, status_code=403)
        if scope["method"] == "POST":
            content_types = [
                value
                for name, value in scope["headers"]
                if name.lower() == b"content-type"
            ]
            if len(content_types) != 1 or (
                content_types[0].split(b";", 1)[0].strip() != b"application/json"
            ):
                return JSONResponse(
                    {"error": "Invalid Content-Type header"},
                    status_code=400,
                )
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Authenticate every method before handing a request to the SDK."""
        if not self._authorized(scope):
            await JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return
        error = self._transport_error(scope)
        if error is not None:
            await error(scope, receive, send)
            return
        # The SDK retains its own DNS-rebinding checks. Give it only validated,
        # normalized identities so its rejection logs cannot echo raw headers.
        scope = {
            **scope,
            "headers": [
                (name, value.lower() if name.lower() in {b"host", b"origin"} else value)
                for name, value in scope["headers"]
            ],
        }
        if self._manager is None:
            await JSONResponse({"error": "Service unavailable"}, status_code=503)(
                scope,
                receive,
                send,
            )
            return
        if scope["method"] == "POST":
            body = await self._validated_body(scope, receive, send)
            if body is None:
                return
            original_receive = receive
            delivered = False

            async def receive_body() -> Message:
                nonlocal delivered
                if delivered:
                    return await original_receive()
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}

            receive = receive_body
        await self._manager.handle_request(scope, receive, send)


def create_mcp_endpoint(
    application_factory: Callable[[], AgileForgeApplication],
    identity_factory: Callable[[], dict[str, object]],
    settings: McpSettings | None = None,
) -> McpEndpoint:
    """Create a lazy endpoint for the selected API application and profile."""
    return McpEndpoint(application_factory, identity_factory, settings)
