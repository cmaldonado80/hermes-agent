"""A configured ``oauth.scope`` is what goes in the authorization request.

The SDK's 401 branch overwrites ``client_metadata.scope`` with the discovered scope
(WWW-Authenticate, then PRM ``scopes_supported``), so a server whose PRM lists every
scope it has always got the full grant, whatever ``oauth.scope`` said. These tests
drive the real flow through discovery and read the scope off the authorization URL.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("mcp.client.auth.oauth2", reason="MCP SDK 1.26.0+ required")

_ALL_SCOPES = ["sessions:read", "sessions:write", "devices:read", "repos:read", "org:read", "org:write", "offline_access"]


class _AuthorizationUrlSeen(Exception):
    pass


async def _authorization_scope(tmp_path, monkeypatch, scope: str | None) -> str | None:
    from tools.mcp_tool import sdk_httpx
    httpx = sdk_httpx()
    from mcp.shared.auth import OAuthClientMetadata
    from pydantic import AnyUrl

    from tools.mcp_oauth import HermesTokenStorage
    from tools.mcp_oauth_manager import _HERMES_PROVIDER_CLS, reset_manager_for_tests

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reset_manager_for_tests()
    seen: list[str] = []

    async def redirect(url: str) -> None:
        seen.append(url)
        raise _AuthorizationUrlSeen

    async def callback():
        return ("code", None)

    provider = _HERMES_PROVIDER_CLS(
        server_name="srv",
        server_url="https://example.com/mcp",
        client_metadata=OAuthClientMetadata(
            redirect_uris=[AnyUrl("http://127.0.0.1:12345/callback")], client_name="Hermes Agent",
            grant_types=["authorization_code", "refresh_token"], scope=scope),
        storage=HermesTokenStorage("srv"),
        redirect_handler=redirect,
        callback_handler=callback,
    )
    prm = {"resource": "https://example.com/mcp", "authorization_servers": ["https://example.com"],
           "scopes_supported": _ALL_SCOPES}
    asm = {"issuer": "https://example.com", "authorization_endpoint": "https://example.com/authorize",
           "token_endpoint": "https://example.com/token", "registration_endpoint": "https://example.com/register",
           "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"],
           "scopes_supported": _ALL_SCOPES}
    dcr = {"client_id": "c1", "redirect_uris": ["http://127.0.0.1:12345/callback"],
           "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
           "token_endpoint_auth_method": "none"}

    req = httpx.Request("POST", "https://example.com/mcp")
    flow = provider.async_auth_flow(req)
    outbound = await flow.__anext__()
    response = httpx.Response(401, request=outbound, headers={
        "www-authenticate": 'Bearer resource_metadata="https://example.com/.well-known/oauth-protected-resource/mcp"'})
    try:
        for _ in range(10):
            outbound = await flow.asend(response)
            path = urlsplit(str(outbound.url)).path
            body = prm if "oauth-protected-resource" in path else asm if ".well-known" in path else dcr
            response = httpx.Response(201 if path == "/register" else 200, request=outbound, json=body)
    except _AuthorizationUrlSeen:
        pass
    finally:
        await flow.aclose()
    assert seen, "flow never reached the authorization request"
    values = parse_qs(urlsplit(seen[0]).query).get("scope")
    return values[0] if values else None


@pytest.mark.asyncio
async def test_configured_scope_wins_over_discovered_scopes(tmp_path, monkeypatch):
    scope = await _authorization_scope(tmp_path, monkeypatch, "sessions:read devices:read")
    assert scope == "sessions:read devices:read"  # offline_access is not added silently


@pytest.mark.asyncio
async def test_configured_offline_access_is_kept(tmp_path, monkeypatch):
    scope = await _authorization_scope(tmp_path, monkeypatch, "sessions:read offline_access")
    assert scope == "sessions:read offline_access"


@pytest.mark.asyncio
async def test_without_configured_scope_discovery_still_decides(tmp_path, monkeypatch):
    scope = await _authorization_scope(tmp_path, monkeypatch, None)
    assert scope == " ".join(_ALL_SCOPES)
