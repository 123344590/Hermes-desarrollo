"""Tests for the per-profile IP allowlist gate in gateway/platforms/api_server.py::_check_auth.

Uses the same real-request harness as test_api_server_agent_admin.py — a real aiohttp TestClient
against a route registered on the real adapter, with a real Bearer token and real
AgentPermissions.network.allowed_ips, not a mocked auth layer."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent import secret_scope as ss
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, _api_request_profile
from hermes_cli.agent_permissions import AgentPermissions, NetworkPermissions, write_agent_permissions


def _make_adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True))


def _create_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_get("/v1/agent/soul", adapter._handle_agent_get_soul)
    return app


@pytest.fixture(autouse=True)
def _reset_multiplex():
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


@pytest.fixture
def named_profile(tmp_path, monkeypatch):
    profile_home = tmp_path / "profiles" / "crm-support"
    profile_home.mkdir(parents=True)
    key = "crm-support-api-key-0123456789"
    (profile_home / ".env").write_text(f"API_SERVER_KEY={key}\n", encoding="utf-8")
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: profile_home)
    ss.set_multiplex_active(True)
    return profile_home, key


@asynccontextmanager
async def _scoped_client(adapter: APIServerAdapter):
    app = _create_app(adapter)
    token = _api_request_profile.set("crm-support")
    try:
        with adapter._profile_scope("crm-support"):
            async with TestClient(TestServer(app)) as cli:
                yield cli
    finally:
        _api_request_profile.reset(token)


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


class TestIpAllowlist:
    @pytest.mark.asyncio
    async def test_no_restriction_configured_allows_any_ip(self, named_profile):
        _home, key = named_profile
        adapter = _make_adapter()
        async with _scoped_client(adapter) as cli:
            resp = await cli.get("/v1/agent/soul", headers=_auth(key))
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_disallowed_ip_rejected_with_valid_token(self, named_profile):
        home, key = named_profile
        write_agent_permissions(home, AgentPermissions(network=NetworkPermissions(allowed_ips=("203.0.113.0/24",))))
        adapter = _make_adapter()
        async with _scoped_client(adapter) as cli:
            # X-Real-IP is the highest-priority source _check_ip_allowlist reads.
            resp = await cli.get("/v1/agent/soul", headers={**_auth(key), "X-Real-IP": "198.51.100.7"})
            assert resp.status == 403
            body = await resp.json()
            assert body["error"]["code"] == "gateway_ip_not_allowed"

    @pytest.mark.asyncio
    async def test_allowed_single_ip_passes(self, named_profile):
        home, key = named_profile
        write_agent_permissions(home, AgentPermissions(network=NetworkPermissions(allowed_ips=("198.51.100.7",))))
        adapter = _make_adapter()
        async with _scoped_client(adapter) as cli:
            resp = await cli.get("/v1/agent/soul", headers={**_auth(key), "X-Real-IP": "198.51.100.7"})
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_cidr_range_matches(self, named_profile):
        home, key = named_profile
        write_agent_permissions(home, AgentPermissions(network=NetworkPermissions(allowed_ips=("198.51.100.0/24",))))
        adapter = _make_adapter()
        async with _scoped_client(adapter) as cli:
            resp = await cli.get("/v1/agent/soul", headers={**_auth(key), "X-Real-IP": "198.51.100.200"})
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_cidr_range_excludes_outside_ip(self, named_profile):
        home, key = named_profile
        write_agent_permissions(home, AgentPermissions(network=NetworkPermissions(allowed_ips=("198.51.100.0/24",))))
        adapter = _make_adapter()
        async with _scoped_client(adapter) as cli:
            resp = await cli.get("/v1/agent/soul", headers={**_auth(key), "X-Real-IP": "203.0.113.9"})
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_invalid_token_still_401s_before_ip_check(self, named_profile):
        """A bad token must not leak whether the IP restriction would have been the blocker —
        always the same 401, regardless of AgentPermissions.network."""
        home, _key = named_profile
        write_agent_permissions(home, AgentPermissions(network=NetworkPermissions(allowed_ips=("203.0.113.0/24",))))
        adapter = _make_adapter()
        async with _scoped_client(adapter) as cli:
            resp = await cli.get("/v1/agent/soul", headers={**_auth("wrong-token"), "X-Real-IP": "198.51.100.7"})
            assert resp.status == 401
