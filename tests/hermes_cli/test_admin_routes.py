"""Tests for hermes_cli/dashboard_auth/admin_routes.py — admin-only agent-profile management."""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from hermes_cli.agent_permissions import AgentPermissions, WebhookPermissions, write_agent_permissions
from hermes_cli.dashboard_auth.admin_routes import _agent_base_url, router as admin_router
from hermes_cli.dashboard_auth.base import Session


def _session(role: str) -> Session:
    now = int(time.time())
    return Session(
        user_id="u1", email="u1@example.test", display_name="U1", org_id="", provider="stub",
        expires_at=now + 3600, access_token="AT", refresh_token="RT", role=role)


def _build_app(session: "Session | None"):
    app = FastAPI()

    @app.middleware("http")
    async def _inject_session(request: Request, call_next):
        request.state.session = session
        return await call_next(request)

    app.include_router(admin_router)
    return app


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    root.mkdir()
    (root / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))


def test_list_agents_requires_session():
    client = TestClient(_build_app(None))
    r = client.get("/api/admin/agents")
    assert r.status_code == 401


def test_list_agents_forbidden_for_non_admin_role():
    client = TestClient(_build_app(_session("agent")))
    r = client.get("/api/admin/agents")
    assert r.status_code == 403


def test_list_agents_allowed_for_admin():
    client = TestClient(_build_app(_session("admin")))
    r = client.get("/api/admin/agents")
    assert r.status_code == 200
    assert "agents" in r.json()


def test_create_agent_is_fail_closed(tmp_path):
    client = TestClient(_build_app(_session("admin")))
    r = client.post("/api/admin/agents", json={"name": "crm-support", "description": "CRM support bot"})
    assert r.status_code == 200
    body = r.json()
    assert body["permissions"]["webhooks"]["can_manage"] is False
    assert body["permissions"]["webhooks"]["max"] == 0
    assert body["permissions"]["skills"]["policy"] == "read"


def test_create_agent_provisions_crm_url_and_token(tmp_path):
    client = TestClient(_build_app(_session("admin")))
    r = client.post("/api/admin/agents", json={"name": "crm-support"})
    assert r.status_code == 200
    crm = r.json()["crm"]
    assert crm["name"] == "crm-support"
    assert crm["url"] == "http://localhost:8642/p/crm-support/v1"
    assert len(crm["token"]) >= 32

    # Persisted into the new profile's OWN .env, not the admin's.
    profile_env = tmp_path / ".hermes" / "profiles" / "crm-support" / ".env"
    assert profile_env.exists()
    assert crm["token"] in profile_env.read_text(encoding="utf-8")

    # multiplex_profiles turned on as a side effect so /p/<name>/v1 is actually served.
    root_config = (tmp_path / ".hermes" / "config.yaml").read_text(encoding="utf-8")
    assert "multiplex_profiles: true" in root_config


def test_list_agents_never_reexposes_raw_token(tmp_path):
    client = TestClient(_build_app(_session("admin")))
    created = client.post("/api/admin/agents", json={"name": "crm-support"})
    token = created.json()["crm"]["token"]

    listed = client.get("/api/admin/agents")
    assert listed.status_code == 200
    raw = listed.text
    assert token not in raw
    entry = next(a for a in listed.json()["agents"] if a["name"] == "crm-support")
    assert entry["crm"]["has_api_key"] is True
    assert "token" not in entry["crm"]


def test_rotate_token_issues_a_new_token(tmp_path):
    client = TestClient(_build_app(_session("admin")))
    created = client.post("/api/admin/agents", json={"name": "crm-support"})
    first_token = created.json()["crm"]["token"]

    rotated = client.post("/api/admin/agents/crm-support/rotate-token")
    assert rotated.status_code == 200
    second_token = rotated.json()["token"]
    assert second_token != first_token

    profile_env = tmp_path / ".hermes" / "profiles" / "crm-support" / ".env"
    env_text = profile_env.read_text(encoding="utf-8")
    assert second_token in env_text
    assert first_token not in env_text


def test_rotate_token_rejects_unknown_profile():
    client = TestClient(_build_app(_session("admin")))
    r = client.post("/api/admin/agents/does-not-exist/rotate-token")
    assert r.status_code == 404


def test_put_permissions_updates_and_get_reflects_it(tmp_path):
    client = TestClient(_build_app(_session("admin")))
    create = client.post("/api/admin/agents", json={"name": "crm-sales"})
    assert create.status_code == 200

    put = client.put(
        "/api/admin/agents/crm-sales/permissions",
        json={
            "webhooks_can_manage": True, "webhooks_max": 2,
            "channels_max": 1, "channels_allowed_platforms": ["whatsapp"],
            "skills_policy": "read_write", "skills_allowed": ["crm-tags"],
        },
    )
    assert put.status_code == 200
    assert put.json()["webhooks"]["can_manage"] is True

    got = client.get("/api/admin/agents/crm-sales/permissions")
    assert got.status_code == 200
    assert got.json()["webhooks"]["max"] == 2
    assert got.json()["channels"]["allowed_platforms"] == ["whatsapp"]


def test_put_permissions_rejects_unknown_profile():
    client = TestClient(_build_app(_session("admin")))
    r = client.put(
        "/api/admin/agents/does-not-exist/permissions",
        json={"webhooks_can_manage": True, "webhooks_max": 1, "channels_max": 1,
              "channels_allowed_platforms": [], "skills_policy": "read", "skills_allowed": []},
    )
    assert r.status_code == 404


def test_agent_base_url_prefers_explicit_public_host_over_bind_address(tmp_path):
    # extra.host is the BIND address (0.0.0.0/:: in any real deployment) — never a URL an
    # external CRM can dial. extra.public_host is the operator's explicit override and must win.
    config_path = tmp_path / ".hermes" / "config.yaml"
    config_path.write_text(
        "platforms:\n"
        "  api_server:\n"
        "    extra:\n"
        "      host: 0.0.0.0\n"
        "      port: 8642\n"
        "      public_host: crm.example.com\n",
        encoding="utf-8",
    )
    assert _agent_base_url() == "http://crm.example.com:8642"


def test_put_permissions_rejects_invalid_policy_value(tmp_path):
    client = TestClient(_build_app(_session("admin")))
    client.post("/api/admin/agents", json={"name": "crm-ops"})
    r = client.put(
        "/api/admin/agents/crm-ops/permissions",
        json={"webhooks_can_manage": False, "webhooks_max": 0, "channels_max": 0,
              "channels_allowed_platforms": [], "skills_policy": "godmode", "skills_allowed": []},
    )
    assert r.status_code == 400


def test_put_permissions_outbound_private_ips_round_trip_into_permissions_and_config(tmp_path):
    """network_allow_private_urls / network_allowed_private_ips must land in BOTH the
    AgentPermissions control file (via the API's own read-back) AND the profile's OWN
    config.yaml as security.allow_private_urls / security.allowed_private_ips — the bridge
    tools/url_safety.py actually reads at runtime. Mirrors the existing
    test_create_agent_provisions_crm_url_and_token config.yaml-content assertion style."""
    client = TestClient(_build_app(_session("admin")))
    client.post("/api/admin/agents", json={"name": "crm-outbound"})

    put = client.put(
        "/api/admin/agents/crm-outbound/permissions",
        json={
            "webhooks_can_manage": False, "webhooks_max": 0,
            "channels_max": 0, "channels_allowed_platforms": [],
            "skills_policy": "read", "skills_allowed": [],
            "network_allowed_ips": ["203.0.113.0/24"],
            "network_allow_private_urls": True,
            "network_allowed_private_ips": ["10.20.30.40/32", "192.168.1.0/24"],
        },
    )
    assert put.status_code == 200
    body = put.json()
    assert body["network"]["allow_private_urls"] is True
    assert body["network"]["allowed_private_ips"] == ["10.20.30.40/32", "192.168.1.0/24"]
    assert body["network"]["allowed_ips"] == ["203.0.113.0/24"]

    # GET reflects the same, proving the AgentPermissions control file round-trips it.
    got = client.get("/api/admin/agents/crm-outbound/permissions")
    assert got.status_code == 200
    assert got.json()["network"]["allow_private_urls"] is True
    assert got.json()["network"]["allowed_private_ips"] == ["10.20.30.40/32", "192.168.1.0/24"]

    # Bridged into the PROFILE'S OWN config.yaml (not the admin's default-profile one) —
    # this is what tools/url_safety.py actually reads at runtime for that profile's process.
    profile_config = (tmp_path / ".hermes" / "profiles" / "crm-outbound" / "config.yaml").read_text(
        encoding="utf-8")
    assert "allow_private_urls: true" in profile_config
    assert "10.20.30.40/32" in profile_config
    assert "192.168.1.0/24" in profile_config


def test_put_permissions_rejects_invalid_outbound_private_ip_entry(tmp_path):
    client = TestClient(_build_app(_session("admin")))
    client.post("/api/admin/agents", json={"name": "crm-badip"})
    r = client.put(
        "/api/admin/agents/crm-badip/permissions",
        json={
            "webhooks_can_manage": False, "webhooks_max": 0,
            "channels_max": 0, "channels_allowed_platforms": [],
            "skills_policy": "read", "skills_allowed": [],
            "network_allowed_private_ips": ["not-an-ip"],
        },
    )
    assert r.status_code == 400


def test_put_permissions_outbound_defaults_stay_fail_closed(tmp_path):
    """A PUT that omits the new outbound fields must not accidentally grant outbound private
    reach — the Pydantic defaults (False / []) must be the fail-closed values, and the bridge
    write must persist that explicit False rather than leaving it unset (see
    _sync_outbound_network_permissions's preserve_keys docstring)."""
    client = TestClient(_build_app(_session("admin")))
    client.post("/api/admin/agents", json={"name": "crm-default"})
    put = client.put(
        "/api/admin/agents/crm-default/permissions",
        json={"webhooks_can_manage": False, "webhooks_max": 0, "channels_max": 0,
              "channels_allowed_platforms": [], "skills_policy": "read", "skills_allowed": []},
    )
    assert put.status_code == 200
    assert put.json()["network"]["allow_private_urls"] is False
    assert put.json()["network"]["allowed_private_ips"] == []

    profile_config = (tmp_path / ".hermes" / "profiles" / "crm-default" / "config.yaml").read_text(
        encoding="utf-8")
    assert "allow_private_urls: false" in profile_config
