"""Per-profile admin-granted permissions for agent profiles.

A profile's ``permissions.yaml`` says what that agent is allowed to do: manage webhooks, connect
platform channels, read/edit/create skills. It is fail-closed by design — a profile with no
``permissions.yaml`` yet (freshly created, before an admin configures it) resolves to every
capability denied, except the ``default`` profile, which is the one the admin account itself
operates and always resolves unrestricted.

Only :func:`write_agent_permissions` may create or modify this file, and it is called ONLY from
admin surfaces (``hermes_cli/dashboard_auth/admin_routes.py``, ``hermes admin`` CLI commands) that
have already checked the caller is the admin. Nothing under ``tools/``, ``agent/`` or ``gateway/``
(code that can run inside an agent's own turn) may import it — an agent must never be able to grant
itself capabilities. See ``tests/hermes_cli/test_agent_permissions_not_agent_writable.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional, Tuple

from utils import atomic_yaml_write, file_signature

SkillsPolicy = Literal["read", "read_write", "read_write_create"]

_PERMISSIONS_FILENAME = "permissions.yaml"
_PERMISSIONS_FILE_MODE = 0o640

# Profiles that always resolve unrestricted regardless of permissions.yaml: the admin operates
# hermes through the "default" profile, so it must behave exactly as it did before this feature.
_UNRESTRICTED_PROFILES = frozenset({"default"})


@dataclass(frozen=True)
class WebhookPermissions:
    can_manage: bool = False
    max: int = 0


@dataclass(frozen=True)
class ChannelPermissions:
    max: int = 0
    allowed_platforms: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillPermissions:
    policy: SkillsPolicy = "read"
    allowed: Tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentPermissions:
    """Resolved capabilities for one profile. All defaults are the fail-closed values."""
    webhooks: WebhookPermissions = field(default_factory=WebhookPermissions)
    channels: ChannelPermissions = field(default_factory=ChannelPermissions)
    skills: SkillPermissions = field(default_factory=SkillPermissions)


def _unrestricted() -> AgentPermissions:
    return AgentPermissions(
        webhooks=WebhookPermissions(can_manage=True, max=2**31 - 1),
        channels=ChannelPermissions(max=2**31 - 1, allowed_platforms=()),
        skills=SkillPermissions(policy="read_write_create", allowed=()),
    )


def permissions_path(profile_dir: Path) -> Path:
    """Path to *profile_dir*'s ``permissions.yaml`` (analogous to ``webhook._subscriptions_path``
    but per-profile: each profile directory owns exactly one permissions file)."""
    return Path(profile_dir) / _PERMISSIONS_FILENAME


_PERMISSIONS_CACHE: dict[Tuple[str, int, int, int, int], AgentPermissions] = {}


def _cache_key(path: Path) -> Optional[Tuple[str, int, int, int, int]]:
    try:
        return (str(path), *file_signature(path.stat()))
    except OSError:
        return None


def _coerce_str_tuple(value: Any) -> Tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(v) for v in value)
    return (str(value),)


def _parse_permissions(raw: dict) -> AgentPermissions:
    webhooks_raw = raw.get("webhooks") or {}
    channels_raw = raw.get("channels") or {}
    skills_raw = raw.get("skills") or {}

    policy = skills_raw.get("policy", "read")
    if policy not in ("read", "read_write", "read_write_create"):
        policy = "read"

    return AgentPermissions(
        webhooks=WebhookPermissions(
            can_manage=bool(webhooks_raw.get("can_manage", False)),
            max=max(0, int(webhooks_raw.get("max", 0) or 0)),
        ),
        channels=ChannelPermissions(
            max=max(0, int(channels_raw.get("max", 0) or 0)),
            allowed_platforms=_coerce_str_tuple(channels_raw.get("allowed_platforms")),
        ),
        skills=SkillPermissions(
            policy=policy,
            allowed=_coerce_str_tuple(skills_raw.get("allowed")),
        ),
    )


def load_agent_permissions(profile_dir: Optional[Path] = None, *, profile_name: Optional[str] = None) -> AgentPermissions:
    """Load the resolved :class:`AgentPermissions` for a profile.

    *profile_dir* defaults to the active profile's home (``get_hermes_home()``), so agent-side
    call sites (skill resolution, webhook subscribe, channel bring-up) can call this with no
    arguments from inside their own already-scoped process. *profile_name* lets admin surfaces
    that already know the profile's canonical name skip a redundant lookup for the
    unrestricted-profile check; when omitted it is inferred from *profile_dir*'s name.

    Fail-closed: a missing/unreadable/malformed file yields every capability denied, EXCEPT for
    the ``default`` profile (the admin's own profile), which is always unrestricted.
    """
    if profile_dir is None:
        from hermes_constants import get_hermes_home
        profile_dir = get_hermes_home()
    profile_dir = Path(profile_dir)

    if profile_name is None:
        # Identity, not path shape: a name derived from profile_dir.name would treat any tmp
        # dir/rename as a fresh anonymous profile. get_active_profile_name() resolves off the
        # SAME HERMES_HOME-override mechanism as everything else in this codebase, so this
        # agrees with what get_hermes_home()/profiles.py already consider "the default profile"
        # for this process — including a HERMES_HOME that is not literally named "default" but
        # resolves to the platform default home (e.g. a --profile-less bare-metal install).
        try:
            from hermes_cli.profiles import get_active_profile_name
            profile_name = get_active_profile_name()
        except Exception:
            profile_name = profile_dir.name
    if profile_name in _UNRESTRICTED_PROFILES:
        return _unrestricted()

    path = permissions_path(profile_dir)
    if not path.exists():
        return AgentPermissions()

    cache_key = _cache_key(path)
    cached = _PERMISSIONS_CACHE.get(cache_key) if cache_key is not None else None
    if cached is not None:
        return cached

    try:
        from agent.skill_utils import yaml_load
        parsed = yaml_load(path.read_text(encoding="utf-8"))
    except Exception:
        return AgentPermissions()
    if not isinstance(parsed, dict):
        return AgentPermissions()

    perms = _parse_permissions(parsed)
    if cache_key is not None:
        _PERMISSIONS_CACHE.clear()
        _PERMISSIONS_CACHE[cache_key] = perms
    return perms


def _to_raw(perms: AgentPermissions) -> dict:
    return {
        "webhooks": {"can_manage": perms.webhooks.can_manage, "max": perms.webhooks.max},
        "channels": {"max": perms.channels.max, "allowed_platforms": list(perms.channels.allowed_platforms)},
        "skills": {"policy": perms.skills.policy, "allowed": list(perms.skills.allowed)},
    }


def write_agent_permissions(profile_dir: Path, perms: AgentPermissions) -> None:
    """Persist *perms* for the profile at *profile_dir*. The ONLY writer of permissions.yaml.

    Written 0640 (owner read/write, group read, no world access) so that once a profile also gets
    a dedicated OS user, that user can read its own permissions but never modify them — only the
    admin/owning account can. Callers MUST be an already-authenticated admin surface; this
    function performs no authorization check itself, by design (see module docstring: the
    guarantee is that nothing reachable from an agent's own turn imports this function at all).
    """
    path = permissions_path(Path(profile_dir))
    atomic_yaml_write(path, _to_raw(perms), create_mode=_PERMISSIONS_FILE_MODE)
    cache_key = _cache_key(path)
    if cache_key is not None:
        _PERMISSIONS_CACHE.pop(cache_key, None)
    _PERMISSIONS_CACHE.clear()


def clear_agent_permissions_cache() -> None:
    """Test hook — drop the shared permissions cache."""
    _PERMISSIONS_CACHE.clear()
