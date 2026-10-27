"""Verify authentication before every MCP operation over the real API endpoint."""

from __future__ import annotations

import json
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
