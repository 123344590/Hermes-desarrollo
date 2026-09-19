"""Per-profile identity/skills HTTP routes for CRM-style external integrations.

Extension module for ``APIServerAdapter`` (same pattern as ``api_server_room_grants.py``): plain
functions taking ``self`` + injected collaborators, wired to the adapter via delegate methods in
``api_server.py``. Every route here authenticates with the SAME per-profile Bearer token
(``API_SERVER_KEY``) already used by ``/v1/chat/completions`` and ``/api/jobs*`` — there is no
separate CRM credential. A profile's ``AgentPermissions.skills.policy`` (see
``hermes_cli/agent_permissions.py``) gates every WRITE here exactly as it gates
``tools.skill_manager_tool.skill_manage`` calls made by the agent's own turn: SOUL.md and the
personality overlay are identity content, so they are gated by the same policy dimension as
skills rather than inventing a fourth permission axis.

Reads (GET) are always allowed once authenticated — only creation/mutation is policy-gated,
matching the "read / read_write / read_write_create" cumulative levels already defined.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

try:
    from aiohttp import web
except ImportError:
    web = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# skills.policy required to write SOUL.md / the personality overlay: identity content is treated
# as "editing an existing skill" for policy purposes (read_write or higher), never "read" alone.
_IDENTITY_WRITE_POLICY = "read_write"
_POLICY_RANK = {"read": 0, "read_write": 1, "read_write_create": 2}


def _json_error(_openai_error, message: str, *, status: int, **error_kwargs) -> "web.Response":
    return web.json_response(_openai_error(message, **error_kwargs), status=status)


def _policy_denied_response(_openai_error, required: str, actual: str) -> "web.Response":
    return _json_error(
        _openai_error,
        f"This agent's skills policy ('{actual}') does not allow this write — it requires "
        f"'{required}' or higher. Ask an admin to raise this profile's skills.policy.",
        code="agent_policy_denied", status=403)


def _require_write_policy(required: str, *, _openai_error) -> Optional["web.Response"]:
    """None when the active profile's skills.policy meets *required*, else a 403 response."""
    from hermes_cli.agent_permissions import load_agent_permissions
    policy = load_agent_permissions().skills.policy
    if _POLICY_RANK.get(policy, 0) < _POLICY_RANK[required]:
        return _policy_denied_response(_openai_error, required, policy)
    return None


def _http_routes(self) -> list[tuple[str, str, Any]]:
    return [
        ("GET", "/v1/agent/soul", self._handle_agent_get_soul),
        ("PUT", "/v1/agent/soul", self._handle_agent_put_soul),
        ("GET", "/v1/agent/personality", self._handle_agent_get_personality),
        ("PUT", "/v1/agent/personality", self._handle_agent_put_personality),
        ("GET", "/v1/agent/skills", self._handle_agent_get_skills),
        ("POST", "/v1/agent/skills", self._handle_agent_post_skills)]


# -- SOUL.md ------------------------------------------------------------------------------------

async def _handle_agent_get_soul(self, request: "web.Request", *, _openai_error) -> "web.Response":
    """GET /v1/agent/soul — this profile's SOUL.md, or empty string when unset."""
    auth_err = self._check_auth(request)
    if auth_err:
        return auth_err
    from agent.prompt_builder import load_soul_md
    content = load_soul_md() or ""
    return web.json_response({"object": "hermes.agent.soul", "content": content})


async def _handle_agent_put_soul(self, request: "web.Request", *, _openai_error) -> "web.Response":
    """PUT /v1/agent/soul — replace this profile's SOUL.md. Body: {"content": "..."}."""
    auth_err = self._check_auth(request)
    if auth_err:
        return auth_err
    policy_err = _require_write_policy(_IDENTITY_WRITE_POLICY, _openai_error=_openai_error)
    if policy_err:
        return policy_err
    body, err = await self._read_json_body(request)
    if err:
        return err
    content = body.get("content")
    if not isinstance(content, str):
        return _json_error(_openai_error, "'content' must be a string", status=400)
    try:
        from agent.prompt_builder import save_soul_md
        save_soul_md(content)
    except Exception:
        logger.exception("PUT /v1/agent/soul failed")
        return _json_error(_openai_error, "Failed to write SOUL.md", err_type="server_error", status=500)
    return web.json_response({"object": "hermes.agent.soul", "content": content})


# -- Personality overlay --------------------------------------------------------------------------

# Only these config.yaml keys are readable/writable here — never the full config surface the
# dashboard's PUT /api/config exposes.
_PERSONALITY_KEYS = ("display_personality", "system_prompt", "personalities")

# Serializes read-modify-write of the 3 personality keys against concurrent PUTs on this route.
# Deliberately NOT the dashboard's web_server-owned _CONFIG_MUTATION_LOCK: that would pull the
# whole dashboard app module into the gateway process just for a lock, for a race window
# (two concurrent PUTs on the SAME profile) that is rare enough not to justify the coupling.
# save_config() itself still serializes the actual file write via its own internal _CONFIG_LOCK.
_PERSONALITY_WRITE_LOCK = threading.Lock()


def _personality_snapshot() -> Dict[str, Any]:
    from hermes_cli.config import read_raw_config
    raw = read_raw_config()
    display = raw.get("display") if isinstance(raw.get("display"), dict) else {}
    agent_cfg = raw.get("agent") if isinstance(raw.get("agent"), dict) else {}
    return {
        "display_personality": display.get("personality", ""),
        "system_prompt": agent_cfg.get("system_prompt", ""),
        "personalities": agent_cfg.get("personalities", {}) or {},
    }


async def _handle_agent_get_personality(self, request: "web.Request", *, _openai_error) -> "web.Response":
    """GET /v1/agent/personality — display.personality, agent.system_prompt, agent.personalities."""
    auth_err = self._check_auth(request)
    if auth_err:
        return auth_err
    return web.json_response({"object": "hermes.agent.personality", **_personality_snapshot()})


async def _handle_agent_put_personality(self, request: "web.Request", *, _openai_error) -> "web.Response":
    """PUT /v1/agent/personality — partial update of display_personality/system_prompt/personalities.
    Only these three config.yaml keys are ever touched; every other section is left untouched."""
    auth_err = self._check_auth(request)
    if auth_err:
        return auth_err
    policy_err = _require_write_policy(_IDENTITY_WRITE_POLICY, _openai_error=_openai_error)
    if policy_err:
        return policy_err
    body, err = await self._read_json_body(request)
    if err:
        return err
    unknown = set(body) - set(_PERSONALITY_KEYS)
    if unknown:
        return _json_error(_openai_error, f"Unknown field(s): {sorted(unknown)}", status=400)
    if "personalities" in body and not isinstance(body["personalities"], dict):
        return _json_error(_openai_error, "'personalities' must be an object", status=400)
    for key in ("display_personality", "system_prompt"):
        if key in body and not isinstance(body[key], str):
            return _json_error(_openai_error, f"'{key}' must be a string", status=400)

    try:
        from hermes_cli.config import _deep_merge, read_raw_config, save_config
        with _PERSONALITY_WRITE_LOCK:
            existing = read_raw_config()
            patch: Dict[str, Any] = {}
            if "display_personality" in body:
                patch["display"] = {"personality": body["display_personality"]}
            agent_patch: Dict[str, Any] = {}
            if "system_prompt" in body:
                agent_patch["system_prompt"] = body["system_prompt"]
            if "personalities" in body:
                agent_patch["personalities"] = body["personalities"]
            if agent_patch:
                patch["agent"] = agent_patch
            merged = _deep_merge(existing, patch)
            save_config(merged, merge_existing=True)
    except Exception:
        logger.exception("PUT /v1/agent/personality failed")
        return _json_error(_openai_error, "Failed to update personality", err_type="server_error", status=500)
    return web.json_response({"object": "hermes.agent.personality", **_personality_snapshot()})


# -- Skills ---------------------------------------------------------------------------------------

async def _handle_agent_get_skills(self, request: "web.Request", *, _openai_error) -> "web.Response":
    """GET /v1/agent/skills — authenticated skills listing (respects skills.allowed), unlike the
    unauthenticated /v1/skills."""
    auth_err = self._check_auth(request)
    if auth_err:
        return auth_err
    try:
        from tools.skills_tool import _find_all_skills, _sort_skills
        skills = _sort_skills(_find_all_skills())
    except Exception:
        logger.exception("GET /v1/agent/skills failed")
        return _json_error(_openai_error, "Failed to enumerate skills", err_type="server_error", status=500)
    return web.json_response({"object": "list", "data": skills})


_SKILL_WRITE_ACTIONS = {"create", "edit", "patch", "delete", "write_file", "remove_file"}


async def _handle_agent_post_skills(self, request: "web.Request", *, _openai_error) -> "web.Response":
    """POST /v1/agent/skills — create/edit/patch/delete a skill. Body mirrors skill_manage's flat
    shape: {"action", "name", "content"?, "category"?, "file_path"?, "old_string"?, "new_string"?,
    "replace_all"?, "absorbed_into"?}. Gated by skills.policy (read_write for edit/patch/delete/
    write_file/remove_file, read_write_create for create); applies immediately — no approval
    staging — since the policy gate IS the access control for this route."""
    auth_err = self._check_auth(request)
    if auth_err:
        return auth_err
    body, err = await self._read_json_body(request)
    if err:
        return err
    action = (body.get("action") or "").strip()
    name = (body.get("name") or "").strip()
    if action not in _SKILL_WRITE_ACTIONS:
        return _json_error(
            _openai_error, f"'action' must be one of: {sorted(_SKILL_WRITE_ACTIONS)}", status=400)
    if not name:
        return _json_error(_openai_error, "'name' is required", status=400)

    from tools.skill_manager_guards import _check_skill_policy
    policy_refusal = _check_skill_policy(action, name)
    if policy_refusal is not None:
        return _json_error(
            _openai_error, policy_refusal.get("error", "Denied by skills policy"),
            code="agent_policy_denied", status=403)

    try:
        from tools.skill_manager_tool import skill_manage, _skill_gate_bypass
        token = _skill_gate_bypass.set(True)  # policy already checked above; skip approval staging
        try:
            raw = skill_manage(
                action=action, name=name, content=body.get("content"), category=body.get("category"),
                file_path=body.get("file_path"), file_content=body.get("file_content"),
                old_string=body.get("old_string"), new_string=body.get("new_string"),
                replace_all=bool(body.get("replace_all", False)), absorbed_into=body.get("absorbed_into"))
        finally:
            _skill_gate_bypass.reset(token)
    except Exception:
        logger.exception("POST /v1/agent/skills failed")
        return _json_error(_openai_error, "Failed to apply skill write", err_type="server_error", status=500)

    import json
    try:
        result = json.loads(raw)
    except Exception:
        result = {"success": False, "error": "skill_manage returned a non-JSON result"}
    status = 200 if result.get("success") else 400
    return web.json_response(result, status=status)
