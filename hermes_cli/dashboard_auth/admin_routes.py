"""HTTP routes for agent-profile administration.

  GET  /api/admin/agents                      list profiles + their permissions
  POST /api/admin/agents                      create a new profile (fail-closed permissions,
                                               auto-provisioned CRM URL + Bearer token)
  GET  /api/admin/agents/{name}/permissions    read one profile's AgentPermissions
  PUT  /api/admin/agents/{name}/permissions    replace one profile's AgentPermissions
  POST /api/admin/agents/{name}/rotate-token   regenerate the profile's API_SERVER_KEY

Every route opens with ``_require_admin`` (see ``routes.py``) — the dashboard's single operator
account is the only identity that ever reaches these; agent profiles never hold a dashboard
session (see ``Session.role`` docstring in ``base.py``). Mounted alongside the auth router by
``web_server.py``, NOT in ``PUBLIC_API_PATHS``, so the auth gate covers it by default.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from hermes_cli.agent_permissions import (
    AgentPermissions, ChannelPermissions, NetworkPermissions, SkillPermissions, WebhookPermissions,
    load_agent_permissions, write_agent_permissions)
from hermes_cli.dashboard_auth.routes import _require_admin

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin")

# Same generation pattern as hermes_cli/webhook.py's per-route secrets (secrets.token_urlsafe(32));
# comfortably above the api_server startup guard's has_usable_secret(min_length=16) floor.
_API_KEY_BYTES = 32


def _http(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail)


def _agent_base_url() -> str:
    """Base ``http://host:port`` a caller OUTSIDE this host can use to reach the api_server
    platform (the default profile's config — under multiplex it is the single shared listener
    every profile's ``/p/<name>/v1`` is served through). Mirrors
    ``hermes_cli/webhook.py::_get_webhook_base_url`` exactly, including its ``extra.public_host``
    override: ``extra.host`` is the BIND address (``0.0.0.0``/``::`` in any real deployment,
    which is not reachable from outside the host/container), so a CRM given a bare "localhost"
    URL for such a deployment can never actually reach the agent. ``extra.public_host`` lets the
    operator state the externally-reachable hostname/IP explicitly (same shape as
    ``dashboard.public_url`` in config_defaults.py); unset falls back to the previous heuristic."""
    from hermes_cli.config import cfg_get, load_config
    cfg = load_config()
    extra = cfg_get(cfg, "platforms", "api_server", "extra", default={}) or {}
    public_host = extra.get("public_host")
    if public_host:
        display_host = public_host
    else:
        host = extra.get("host")
        display_host = "localhost" if not host or host in {"0.0.0.0", "::"} else host
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    port = cfg_get(cfg, "platforms", "api_server", "extra", "port", default=None) or extra.get("port", 8642)
    return f"http://{display_host}:{port}"


def _agent_url(name: str) -> str:
    return f"{_agent_base_url()}/p/{name}/v1"


def _ensure_multiplex_enabled() -> None:
    """Turn on ``gateway.multiplex_profiles`` (default profile) if it isn't already — a named
    profile's ``/p/<name>/v1`` only serves over the shared listener when this is on. Idempotent;
    never downgrades an operator's own separate-gateway choice back off once explicitly set True.

    ``preserve_keys`` is required here, not optional: ``multiplex_profiles``'s schema default is
    already ``True`` (see ``config_defaults.py`` — "an UNSET key is a request, not a verdict"),
    so a bare ``save_config`` would strip the value we just wrote right back out as
    "matches the default", leaving the key unset on disk. Unset and explicit-``True`` are NOT the
    same thing to the gateway's own startup preflight (unset lets it decide; explicit ``True``
    forces multiplex outright) — without ``preserve_keys`` this call would silently no-op and a
    newly created agent's ``/p/<name>/v1`` would never actually come up.
    """
    from hermes_cli.config import cfg_get, read_raw_config, save_config
    existing = read_raw_config()
    if cfg_get(existing, "gateway", "multiplex_profiles", default=False):
        return
    patch = {"gateway": {"multiplex_profiles": True}}
    from hermes_cli.config import _deep_merge
    save_config(_deep_merge(existing, patch), merge_existing=True, preserve_keys={("gateway", "multiplex_profiles")})


def _issue_api_server_key(profile_name: str) -> str:
    """Generate and persist a fresh API_SERVER_KEY into *profile_name*'s .env, returning it.
    Runs under that profile's HERMES_HOME scope so the write lands in ITS .env, never the
    admin's — same save_env_value() primitive the dashboard's own credential UI uses."""
    from hermes_cli.config import save_env_value
    from hermes_cli.web_server_profiles import _profile_scope
    token = secrets.token_urlsafe(_API_KEY_BYTES)
    with _profile_scope(profile_name):
        save_env_value("API_SERVER_KEY", token)
    return token


def _permissions_to_json(perms: AgentPermissions) -> dict:
    return {
        "webhooks": {"can_manage": perms.webhooks.can_manage, "max": perms.webhooks.max},
        "channels": {"max": perms.channels.max, "allowed_platforms": list(perms.channels.allowed_platforms)},
        "skills": {"policy": perms.skills.policy, "allowed": list(perms.skills.allowed)},
        "network": {"allowed_ips": list(perms.network.allowed_ips)},
    }


def _has_api_key(profile_name: str) -> bool:
    from hermes_cli.web_server_profiles import _profile_scope
    with _profile_scope(profile_name if profile_name != "default" else None):
        from hermes_cli.auth import has_usable_secret
        from hermes_cli.config import load_env
        return has_usable_secret(load_env().get("API_SERVER_KEY", ""), min_length=16)


def _resolve_named_profile_dir(name: str):
    from hermes_cli.profiles import get_profile_dir, profile_exists
    if name == "default" or not profile_exists(name):
        raise _http(404, f"Profile '{name}' does not exist")
    return get_profile_dir(name)


@router.get("/agents", name="admin_list_agents")
async def api_admin_list_agents(request: Request):
    _require_admin(request)
    from hermes_cli.profiles import list_profiles
    agents = []
    for info in list_profiles():
        perms = load_agent_permissions(info.path, profile_name=info.name)
        agents.append({
            "name": info.name, "is_default": info.is_default,
            "gateway_running": info.gateway_running, "permissions": _permissions_to_json(perms),
            "crm": {"url": _agent_url(info.name), "has_api_key": _has_api_key(info.name)}})
    return {"agents": agents}


class _CreateAgentBody(BaseModel):
    name: str
    description: str = ""


@router.post("/agents", name="admin_create_agent")
async def api_admin_create_agent(request: Request, body: _CreateAgentBody):
    _require_admin(request)
    from hermes_cli.profiles import create_profile
    try:
        profile_dir = create_profile(body.name, description=body.description or None)
    except (ValueError, FileExistsError) as exc:
        raise _http(400, str(exc))
    # New agents are born fail-closed: no capability until the admin explicitly grants one.
    write_agent_permissions(profile_dir, AgentPermissions())
    _ensure_multiplex_enabled()
    token = _issue_api_server_key(body.name)
    return {
        "name": body.name, "path": str(profile_dir), "permissions": _permissions_to_json(AgentPermissions()),
        # Shown once, at creation — paste straight into the CRM's Name/URL/Token fields.
        # GET /api/admin/agents never re-exposes it (only the has_api_key boolean).
        "crm": {"name": body.name, "url": _agent_url(body.name), "token": token}}


@router.post("/agents/{name}/rotate-token", name="admin_rotate_agent_token")
async def api_admin_rotate_agent_token(request: Request, name: str):
    _require_admin(request)
    _resolve_named_profile_dir(name)
    token = _issue_api_server_key(name)
    return {"name": name, "url": _agent_url(name), "token": token}


class _PermissionsBody(BaseModel):
    webhooks_can_manage: bool = False
    webhooks_max: int = 0
    channels_max: int = 0
    channels_allowed_platforms: list[str] = []
    skills_policy: str = "read"
    skills_allowed: list[str] = []
    network_allowed_ips: list[str] = []


def _validate_ip_allowlist(raw_ips: list[str]) -> tuple[str, ...]:
    """Reject the whole write on any unparseable entry — unlike the tolerant read-path parser in
    ``agent_permissions._coerce_ip_tuple`` (which drops bad entries from an already-written file
    rather than crash every read), a write is the one place a typo SHOULD surface immediately to
    the admin instead of being silently dropped."""
    import ipaddress
    for raw in raw_ips:
        try:
            ipaddress.ip_network(raw, strict=False)
        except ValueError:
            raise _http(400, f"'{raw}' is not a valid IP address or CIDR range")
    return tuple(raw_ips)


@router.get("/agents/{name}/permissions", name="admin_get_agent_permissions")
async def api_admin_get_agent_permissions(request: Request, name: str):
    _require_admin(request)
    profile_dir = _resolve_named_profile_dir(name)
    perms = load_agent_permissions(profile_dir, profile_name=name)
    return _permissions_to_json(perms)


@router.put("/agents/{name}/permissions", name="admin_put_agent_permissions")
async def api_admin_put_agent_permissions(request: Request, name: str, body: _PermissionsBody):
    _require_admin(request)
    profile_dir = _resolve_named_profile_dir(name)
    if body.skills_policy not in ("read", "read_write", "read_write_create"):
        raise _http(400, "skills_policy must be one of: read, read_write, read_write_create")
    allowed_ips = _validate_ip_allowlist(body.network_allowed_ips)
    perms = AgentPermissions(
        webhooks=WebhookPermissions(can_manage=body.webhooks_can_manage, max=max(0, body.webhooks_max)),
        channels=ChannelPermissions(
            max=max(0, body.channels_max), allowed_platforms=tuple(body.channels_allowed_platforms)),
        skills=SkillPermissions(policy=body.skills_policy, allowed=tuple(body.skills_allowed)),
        network=NetworkPermissions(allowed_ips=allowed_ips),
    )
    write_agent_permissions(profile_dir, perms)
    sess = _require_admin(request)
    _log.info("admin %s updated permissions for profile '%s'", sess.user_id, name)
    return _permissions_to_json(perms)
