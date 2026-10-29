"""Verify authentication before every MCP operation over the real API endpoint."""

from __future__ import annotations

import json
import logging
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient

import api

# Deliberate synthetic credential for authenticated transport checks.
TOKEN = "mcp-test-bearer-secret-canary-304"  # noqa: S105  # nosec B105
AUTH_ENV = "AGILEFORGE_MCP_BEARER_TOKEN"
ACCEPT = "application/json, text/event-stream"


def _message(method: str) -> dict[str, object]:
    params: dict[str, object] = {}
    if method == "initialize":
        params = {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "agileforge-test-client", "version": "1"},
        }
    elif method == "tools/call":
        params = {"name": "project_list", "arguments": {}}
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


@pytest.mark.parametrize("method", ["initialize", "tools/list", "tools/call"])
@pytest.mark.parametrize("authorization", [None, "Bearer incorrect-credential"])
def test_missing_and_wrong_bearer_reject_every_mcp_operation(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    authorization: str | None,
) -> None:
    """A missing auth guard must expose an HTTP failure instead of business data."""
    monkeypatch.setenv(AUTH_ENV, TOKEN)
    headers = {"Accept": ACCEPT}
    if authorization is not None:
        headers["Authorization"] = authorization
    with TestClient(api.app, base_url="http://localhost") as client:
        response = client.post("/mcp", json=_message(method), headers=headers)
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert TOKEN not in response.text


@pytest.mark.parametrize("method", ["initialize", "tools/list", "tools/call"])
def test_unconfigured_mcp_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    """An absent runtime secret must never enable an anonymous MCP endpoint."""
    monkeypatch.delenv(AUTH_ENV, raising=False)
    with TestClient(api.app, base_url="http://localhost") as client:
        response = client.post(
            "/mcp",
            json=_message(method),
            headers={"Accept": ACCEPT, "Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert TOKEN not in response.text


def test_authenticated_client_initializes_supported_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A correct bearer must reach the supported protocol in this API process."""
    monkeypatch.setenv(AUTH_ENV, TOKEN)
    with TestClient(api.app, base_url="http://localhost") as client:
        response = client.post(
            "/mcp",
            json=_message("initialize"),
            headers={"Accept": ACCEPT, "Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == HTTPStatus.OK
    result = response.json()["result"]
    assert result["protocolVersion"] == "2025-11-25"
    assert result["serverInfo"]["name"] == "AgileForge"
    assert "tools" in result["capabilities"]
    assert TOKEN not in response.text


@pytest.mark.parametrize("method", ["POST", "GET", "DELETE", "OPTIONS", "HEAD"])
def test_unsupported_protocol_version_does_not_echo_credentials(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    method: str,
) -> None:
    """Rejected protocol metadata must not enter SDK responses or raw logs."""
    monkeypatch.setenv(AUTH_ENV, TOKEN)
    caplog.set_level(logging.DEBUG, logger="mcp")
    with TestClient(api.app, base_url="http://localhost") as client:
        response = client.request(
            method,
            "/mcp",
            json=_message("tools/list"),
            headers={
                "Accept": ACCEPT,
                "Authorization": f"Bearer {TOKEN}",
                "MCP-Protocol-Version": TOKEN,
            },
        )
    assert TOKEN not in response.text + caplog.text
    assert all(TOKEN not in record.getMessage() for record in caplog.records)
    assert response.status_code == HTTPStatus.BAD_REQUEST


@pytest.mark.parametrize("second_version", ["2025-11-25", TOKEN])
def test_duplicate_protocol_versions_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    second_version: str,
) -> None:
    """A proxy and the SDK must not choose different protocol versions."""
    monkeypatch.setenv(AUTH_ENV, TOKEN)
    caplog.set_level(logging.DEBUG, logger="mcp")
    with TestClient(api.app, base_url="http://localhost") as client:
        response = client.post(
            "/mcp",
            json=_message("tools/list"),
            headers=[
                ("Accept", ACCEPT),
                ("Authorization", f"Bearer {TOKEN}"),
                ("MCP-Protocol-Version", "2025-11-25"),
                ("MCP-Protocol-Version", second_version),
            ],
        )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert TOKEN not in response.text + caplog.text
    assert all(TOKEN not in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    "version",
    ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"],
)
def test_supported_protocol_versions_preserve_discovery(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
) -> None:
    """The metadata guard must preserve the SDK's supported client versions."""
    monkeypatch.setenv(AUTH_ENV, TOKEN)
    with TestClient(api.app, base_url="http://localhost") as client:
        response = client.post(
            "/mcp",
            json=_message("tools/list"),
            headers={
                "Accept": ACCEPT,
                "Authorization": f"Bearer {TOKEN}",
                "MCP-Protocol-Version": version,
            },
        )
    assert response.status_code == HTTPStatus.OK
    assert "service_identity" in {
        tool["name"] for tool in response.json()["result"]["tools"]
    }


@pytest.mark.parametrize(
    ("header", "value", "status"),
    [
        ("Origin", "https://attacker.example", 403),
        ("Origin", "null", 403),
        ("Host", "attacker.example", 421),
        ("Host", "localhost.attacker.example", 421),
    ],
)
def test_origin_and_host_rejections_hide_credentials(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    header: str,
    value: str,
    status: int,
) -> None:
    """Authenticated cross-origin or rebound requests must fail before tool work."""
    monkeypatch.setenv(AUTH_ENV, TOKEN)
    headers = {"Accept": ACCEPT, "Authorization": f"Bearer {TOKEN}", header: value}
    with TestClient(api.app, base_url="http://localhost") as client:
        response = client.post("/mcp", json=_message("initialize"), headers=headers)
    assert response.status_code == status
    assert TOKEN not in response.text + caplog.text


def test_duplicate_authorization_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambiguous credentials must not depend on the proxy's header selection."""
    monkeypatch.setenv(AUTH_ENV, TOKEN)
    headers = [
        ("Accept", ACCEPT),
        ("Authorization", f"Bearer {TOKEN}"),
        ("Authorization", "Bearer incorrect-credential"),
    ]
    with TestClient(api.app, base_url="http://localhost") as client:
        response = client.post("/mcp", json=_message("initialize"), headers=headers)
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert TOKEN not in response.text


@pytest.mark.parametrize(
    ("header", "value", "status"),
    [
        ("Host", "localhost:8765@attacker.example", 421),
        ("Host", "localhost:invalid", 421),
        ("Origin", "http://localhost:8765@attacker.example", 403),
        ("Origin", "http://localhost:8765/path", 403),
        ("Host", f"{TOKEN}.attacker.example", 421),
        ("Origin", f"https://{TOKEN}.attacker.example", 403),
        ("Content-Type", f"text/{TOKEN}", 400),
    ],
    ids=[
        "host-userinfo",
        "invalid-port",
        "origin-userinfo",
        "origin-path",
        "host-canary",
        "origin-canary",
        "content-type-canary",
    ],
)
def test_invalid_transport_headers_do_not_bypass_checks_or_leak_values(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    header: str,
    value: str,
    status: int,
) -> None:
    """A port prefix must not authorize userinfo or disclose rejected headers."""
    monkeypatch.setenv(AUTH_ENV, TOKEN)
    with TestClient(api.app, base_url="http://localhost") as client:
        response = client.post(
            "/mcp",
            json=_message("initialize"),
            headers={
                "Accept": ACCEPT,
                "Authorization": f"Bearer {TOKEN}",
                header: value,
            },
        )
    assert response.status_code == status
    assert TOKEN not in response.text + caplog.text


@pytest.mark.parametrize(("header", "status"), [("Host", 421), ("Origin", 403)])
def test_duplicate_transport_identities_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    header: str,
    status: int,
) -> None:
    """Proxies must not select a different identity from the MCP process."""
    monkeypatch.setenv(AUTH_ENV, TOKEN)
    value = "localhost" if header == "Host" else "http://localhost"
    with TestClient(api.app, base_url="http://localhost") as client:
        response = client.post(
            "/mcp",
            json=_message("initialize"),
            headers=[
                ("Accept", ACCEPT),
                ("Authorization", f"Bearer {TOKEN}"),
                (header, value),
                (header, value),
            ],
        )
    assert response.status_code == status


@pytest.mark.parametrize("method", ["GET", "DELETE", "OPTIONS", "HEAD"])
def test_non_post_methods_require_the_runtime_bearer(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    """Transport negotiation must not create an anonymous endpoint variant."""
    monkeypatch.setenv(AUTH_ENV, TOKEN)
    with TestClient(api.app, base_url="http://localhost") as client:
        response = client.request(method, "/mcp")
    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_authenticated_loopback_origin_and_port_are_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local client must retain access through the published loopback port."""
    monkeypatch.setenv(AUTH_ENV, TOKEN)
    with TestClient(api.app, base_url="http://127.0.0.1:32020") as client:
        response = client.post(
            "/mcp",
            json=_message("initialize"),
            headers={
                "Accept": ACCEPT,
                "Authorization": f"Bearer {TOKEN}",
                "Origin": "http://127.0.0.1:32020",
            },
        )
    assert response.status_code == HTTPStatus.OK


def test_transport_identity_normalization_preserves_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK must receive the normalized identity accepted by our guard."""
    monkeypatch.setenv(AUTH_ENV, TOKEN)
    with TestClient(api.app, base_url="http://localhost") as client:
        response = client.post(
            "/mcp",
            json=_message("initialize"),
            headers={
                "Accept": ACCEPT,
                "Authorization": f"Bearer {TOKEN}",
                "Host": "LOCALHOST:8765",
                "Origin": "HTTP://LOCALHOST:8765",
            },
        )
    assert response.status_code == HTTPStatus.OK


@pytest.mark.parametrize(
    "payload",
    [
        f'{{"jsonrpc":"2.0", "id":1, "method":"{TOKEN}"',
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "project_list",
                "arguments": TOKEN,
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": TOKEN,
                "arguments": {},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": TOKEN,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {
                "requestId": {"invalid": TOKEN},
            },
        },
    ],
    ids=[
        "malformed-json",
        "invalid-call-parameters",
        "unknown-tool",
        "credential-bearing-id",
        "invalid-notification",
    ],
)
def test_protocol_errors_do_not_echo_credentials(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    payload: str | dict[str, object],
) -> None:
    """Malformed MCP envelopes must not put input values in diagnostics."""
    monkeypatch.setenv(AUTH_ENV, TOKEN)
    with TestClient(api.app, base_url="http://localhost") as client:
        response = client.post(
            "/mcp",
            content=payload if isinstance(payload, str) else json.dumps(payload),
            headers={
                "Accept": ACCEPT,
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
            },
        )
    assert response.status_code in {200, 400}
    assert TOKEN not in response.text + caplog.text


@pytest.mark.parametrize(
    "response_body",
    [
        {"jsonrpc": "2.0", "id": 3, "result": {"marker": TOKEN}},
        {"jsonrpc": "2.0", "id": 4, "error": {"code": -1, "message": TOKEN}},
    ],
    ids=["unsolicited-result", "unsolicited-error"],
)
def test_unsolicited_responses_do_not_reach_sdk_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    response_body: dict[str, object],
) -> None:
    """A stateless tools-only server has no client request awaiting a response."""
    monkeypatch.setenv(AUTH_ENV, TOKEN)
    caplog.set_level(logging.DEBUG, logger="mcp")
    with TestClient(api.app, base_url="http://localhost") as client:
        response = client.post(
            "/mcp",
            json=response_body,
            headers={"Accept": ACCEPT, "Authorization": f"Bearer {TOKEN}"},
        )
    assert TOKEN not in response.text + caplog.text
    assert all(TOKEN not in record.getMessage() for record in caplog.records)
    assert response.status_code == HTTPStatus.BAD_REQUEST


@pytest.mark.parametrize(
    "notification",
    [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 1, "reason": "Client cancelled"},
        },
        {
            "jsonrpc": "2.0",
            "method": "notifications/roots/list_changed",
            "params": {"_meta": {"marker": "Client state changed"}},
        },
        {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {
                "progressToken": "progress-1",
                "progress": 0,
                "message": "Working",
            },
        },
    ],
    ids=["initialized", "cancelled", "roots-changed", "progress"],
)
def test_response_guard_preserves_client_notifications(
    monkeypatch: pytest.MonkeyPatch,
    notification: dict[str, object],
) -> None:
    """Rejecting the opposite message direction must preserve client notices."""
    monkeypatch.setenv(AUTH_ENV, TOKEN)
    with TestClient(api.app, base_url="http://localhost") as client:
        response = client.post(
            "/mcp",
            json=notification,
            headers={"Accept": ACCEPT, "Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == HTTPStatus.ACCEPTED


@pytest.mark.parametrize(
    "notification",
    [
        {
            "jsonrpc": "2.0",
            "method": "notifications/roots/list_changed",
            "params": {"_meta": {"marker": TOKEN}},
        },
        {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {"progressToken": "progress-1", "progress": 0, "message": TOKEN},
        },
    ],
    ids=["roots-metadata", "progress-message"],
)
def test_credential_bearing_notifications_do_not_enter_sdk_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    notification: dict[str, object],
) -> None:
    """Control metadata must not copy runtime credentials into SDK diagnostics."""
    monkeypatch.setenv(AUTH_ENV, TOKEN)
    caplog.set_level(logging.DEBUG, logger="mcp")
    with TestClient(api.app, base_url="http://localhost") as client:
        response = client.post(
            "/mcp",
            json=notification,
            headers={"Accept": ACCEPT, "Authorization": f"Bearer {TOKEN}"},
        )
    assert TOKEN not in response.text + caplog.text
    assert all(TOKEN not in record.getMessage() for record in caplog.records)
    assert response.status_code == HTTPStatus.BAD_REQUEST


def test_content_type_whitespace_does_not_enter_raw_sdk_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The SDK must receive the same normalized media type the guard accepts."""
    monkeypatch.setenv(AUTH_ENV, TOKEN)
    caplog.set_level(logging.DEBUG, logger="mcp")
    with TestClient(api.app, base_url="http://localhost") as client:
        response = client.post(
            "/mcp",
            json=_message("tools/list"),
            headers={
                "Accept": ACCEPT,
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": f" application/json; marker={TOKEN}",
            },
        )
    assert TOKEN not in response.text + caplog.text
    assert all(TOKEN not in record.getMessage() for record in caplog.records)
    assert response.status_code == HTTPStatus.OK
    assert response.json()["result"]["tools"]
