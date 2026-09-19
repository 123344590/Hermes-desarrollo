"""Tests for gateway/platforms/api_server_agent_admin.py — CRM-facing per-profile identity/skills
routes (SOUL.md, personality overlay, skills), gated by the same Bearer token as chat/cron and by
AgentPermissions.skills.policy for writes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent import secret_scope as ss
from gateway.config import PlatformConfig
from gateway.platforms import api_server
from gateway.platforms import api_server_agent_admin as agent_admin
from gateway.platforms.api_server import APIServerAdapter, _api_request_profile
from hermes_cli.agent_permissions import AgentPermissions, SkillPermissions, write_agent_permissions


def _make_adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True))


def _create_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_get("/v1/agent/soul", adapter._handle_agent_get_soul)
    app.router.add_put("/v1/agent/soul", adapter._handle_agent_put_soul)
    app.router.add_get("/v1/agent/personality", adapter._handle_agent_get_personality)
    app.router.add_put("/v1/agent/personality", adapter._handle_agent_put_personality)
    app.router.add_get("/v1/agent/skills", adapter._handle_agent_get_skills)
    app.router.add_post("/v1/agent/skills", adapter._handle_agent_post_skills)
    return app


@pytest.fixture(autouse=True)
def _reset_multiplex():
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


@pytest.fixture
def named_profile(tmp_path, monkeypatch):
    """A named profile with its own .env (API_SERVER_KEY) and HERMES_HOME."""
    profile_home = tmp_path / "profiles" / "crm-support"
    profile_home.mkdir(parents=True)
    key = "crm-support-api-key-0123456789"
    (profile_home / ".env").write_text(f"API_SERVER_KEY={key}\n", encoding="utf-8")
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: profile_home)
    ss.set_multiplex_active(True)
    return profile_home, key


@asynccontextmanager
async def _scoped_client(adapter: APIServerAdapter):
    """TestClient wired up exactly as the real profile_prefix_middleware scopes a
    /p/crm-support/v1/... request: _api_request_profile set + adapter._profile_scope entered
    (which hydrates HERMES_HOME + the secret scope from THIS profile's .env, same mechanism
    already covered by tests/gateway/test_api_server_multiplex_secret_scope.py)."""
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


class TestAuth:
    @pytest.mark.asyncio
    async def test_get_soul_requires_bearer(self, named_profile):
        _home, _key = named_profile
        adapter = _make_adapter()
        async with _scoped_client(adapter) as cli:
            resp = await cli.get("/v1/agent/soul")
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_get_soul_rejects_wrong_token(self, named_profile):
        _home, _key = named_profile
        adapter = _make_adapter()
        async with _scoped_client(adapter) as cli:
            resp = await cli.get("/v1/agent/soul", headers=_auth("not-the-right-key"))
            assert resp.status == 401


class TestSoul:
    @pytest.mark.asyncio
    async def test_get_soul_returns_content(self, named_profile):
        home, key = named_profile
        (home / "SOUL.md").write_text("You are a CRM support agent.\n", encoding="utf-8")
        adapter = _make_adapter()
        async with _scoped_client(adapter) as cli:
            resp = await cli.get("/v1/agent/soul", headers=_auth(key))
            assert resp.status == 200
            body = await resp.json()
            assert "CRM support agent" in body["content"]

    @pytest.mark.asyncio
    async def test_put_soul_blocked_at_read_policy(self, named_profile):
        home, key = named_profile
        write_agent_permissions(home, AgentPermissions(skills=SkillPermissions(policy="read")))
        adapter = _make_adapter()
        async with _scoped_client(adapter) as cli:
            resp = await cli.put("/v1/agent/soul", headers=_auth(key), json={"content": "new soul"})
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_put_soul_allowed_at_read_write_policy(self, named_profile):
        home, key = named_profile
        write_agent_permissions(home, AgentPermissions(skills=SkillPermissions(policy="read_write")))
        adapter = _make_adapter()
        async with _scoped_client(adapter) as cli:
            resp = await cli.put("/v1/agent/soul", headers=_auth(key), json={"content": "new soul"})
            assert resp.status == 200
        assert (home / "SOUL.md").read_text(encoding="utf-8") == "new soul"

    @pytest.mark.asyncio
    async def test_put_soul_rejects_non_string_content(self, named_profile):
        home, key = named_profile
        write_agent_permissions(home, AgentPermissions(skills=SkillPermissions(policy="read_write")))
        adapter = _make_adapter()
        async with _scoped_client(adapter) as cli:
            resp = await cli.put("/v1/agent/soul", headers=_auth(key), json={"content": 123})
            assert resp.status == 400


class TestPersonality:
    @pytest.mark.asyncio
    async def test_get_personality_defaults(self, named_profile):
        home, key = named_profile
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
        adapter = _make_adapter()
        async with _scoped_client(adapter) as cli:
            resp = await cli.get("/v1/agent/personality", headers=_auth(key))
            assert resp.status == 200
            body = await resp.json()
        assert body["display_personality"] == ""
        assert body["system_prompt"] == ""
        assert body["personalities"] == {}

    @pytest.mark.asyncio
    async def test_put_personality_blocked_at_read_policy(self, named_profile):
        home, key = named_profile
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
        write_agent_permissions(home, AgentPermissions(skills=SkillPermissions(policy="read")))
        adapter = _make_adapter()
        async with _scoped_client(adapter) as cli:
            resp = await cli.put(
                "/v1/agent/personality", headers=_auth(key), json={"system_prompt": "Be concise."})
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_put_personality_updates_only_named_fields(self, named_profile):
        home, key = named_profile
        (home / "config.yaml").write_text("display:\n  language: en\n", encoding="utf-8")
        write_agent_permissions(home, AgentPermissions(skills=SkillPermissions(policy="read_write")))
        adapter = _make_adapter()
        async with _scoped_client(adapter) as cli:
            resp = await cli.put(
                "/v1/agent/personality", headers=_auth(key),
                json={"system_prompt": "Be concise.", "display_personality": "concise"})
            assert resp.status == 200
            body = await resp.json()
        assert body["system_prompt"] == "Be concise."
        assert body["display_personality"] == "concise"
        raw = (home / "config.yaml").read_text(encoding="utf-8")
        assert "language: en" in raw

    @pytest.mark.asyncio
    async def test_put_personality_rejects_unknown_field(self, named_profile):
        home, key = named_profile
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
        write_agent_permissions(home, AgentPermissions(skills=SkillPermissions(policy="read_write")))
        adapter = _make_adapter()
        async with _scoped_client(adapter) as cli:
            resp = await cli.put("/v1/agent/personality", headers=_auth(key), json={"unknown_field": "x"})
            assert resp.status == 400


class TestSkills:
    @pytest.mark.asyncio
    async def test_get_skills_returns_list(self, named_profile, monkeypatch):
        _home, key = named_profile
        monkeypatch.setattr(
            "tools.skills_tool._find_all_skills",
            lambda **kw: [{"name": "crm-tags", "description": "tag conversations", "category": "crm"}])
        adapter = _make_adapter()
        async with _scoped_client(adapter) as cli:
            resp = await cli.get("/v1/agent/skills", headers=_auth(key))
            assert resp.status == 200
            body = await resp.json()
        assert body["data"][0]["name"] == "crm-tags"

    @pytest.mark.asyncio
    async def test_post_skills_rejects_unknown_action(self, named_profile):
        _home, key = named_profile
        adapter = _make_adapter()
        async with _scoped_client(adapter) as cli:
            resp = await cli.post("/v1/agent/skills", headers=_auth(key), json={"action": "nuke", "name": "x"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_post_skills_create_blocked_at_read_write_policy(self, named_profile):
        home, key = named_profile
        write_agent_permissions(home, AgentPermissions(skills=SkillPermissions(policy="read_write")))
        adapter = _make_adapter()
        async with _scoped_client(adapter) as cli:
            resp = await cli.post(
                "/v1/agent/skills", headers=_auth(key),
                json={"action": "create", "name": "new-skill", "content": "---\nname: new-skill\n---\nBody.\n"})
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_post_skills_create_allowed_and_applies_immediately(self, named_profile, monkeypatch):
        home, key = named_profile
        write_agent_permissions(home, AgentPermissions(skills=SkillPermissions(policy="read_write_create")))
        monkeypatch.setattr(
            "tools.skill_manager_guards._check_skill_policy", lambda action, name: None)
        monkeypatch.setattr(
            "tools.skill_manager_tool.skill_manage",
            lambda **kw: '{"success": true, "staged": false}')
        adapter = _make_adapter()
        async with _scoped_client(adapter) as cli:
            resp = await cli.post(
                "/v1/agent/skills", headers=_auth(key),
                json={"action": "create", "name": "new-skill", "content": "---\nname: new-skill\n---\nBody.\n"})
            assert resp.status == 200
            body = await resp.json()
        assert body["success"] is True
        assert body.get("staged") is not True  # applied directly, never left pending


class TestAdapterKeepsMethodsOnClass:
    def test_agent_admin_handlers_present_on_adapter_class(self):
        expected = {
            "_handle_agent_get_soul", "_handle_agent_put_soul",
            "_handle_agent_get_personality", "_handle_agent_put_personality",
            "_handle_agent_get_skills", "_handle_agent_post_skills"}
        assert expected <= api_server.APIServerAdapter.__dict__.keys()

    @pytest.mark.asyncio
    async def test_delegate_forwards_to_module_function(self, monkeypatch):
        adapter = api_server.APIServerAdapter.__new__(api_server.APIServerAdapter)
        request = object()
        expected = object()
        implementation = AsyncMock(return_value=expected)
        monkeypatch.setattr(agent_admin, "_handle_agent_get_soul", implementation)

        assert await adapter._handle_agent_get_soul(request) is expected
        implementation.assert_awaited_once_with(adapter, request, _openai_error=api_server._openai_error)
