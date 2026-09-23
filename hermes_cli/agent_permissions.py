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

from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional, Tuple

from utils import atomic_yaml_write, file_signature

SkillsPolicy = Literal["read", "read_write", "read_write_create"]

_PERMISSIONS_FILENAME = "permissions.yaml"
# World-readable on purpose: the agent must be able to read its own (non-secret) grants, or the
# fail-closed loader degrades every permission to "deny". Write protection comes from the
# out-of-home control directory, not from this mode. See write_agent_permissions().
_PERMISSIONS_FILE_MODE = 0o644
# Sibling of the profiles tree, never inside a profile home — see control_dir_for().
_CONTROL_DIRNAME = ".control"

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
class NetworkPermissions:
    # Empty = no IP restriction (backward compatible with every profile created before this
    # field existed) — same "empty means unrestricted" convention as ChannelPermissions.
    # allowed_platforms and SkillPermissions.allowed. Entries are individual IPs or CIDR ranges.
    #
    # INBOUND: who may call THIS agent's own CRM endpoint (/p/<name>/v1). Separate system from
    # both fields below — do not confuse the two either in code or in the admin UI.
    allowed_ips: Tuple[str, ...] = ()
    # OUTBOUND (allow_private_urls / allowed_private_ips, below) vs INBOUND (allowed_ips, above):
    # these two gate whether the agent's OWN network-facing tools (terminal, url fetch, browser)
    # may reach private/internal addresses — bridged by the admin write path into that profile's
    # own config.yaml as security.allow_private_urls / security.allowed_private_ips (see
    # tools/url_safety.py::_resolve_allow_private_urls / _resolve_allowed_private_ips). Fail-closed
    # default, same as every other field on this dataclass; a profile with no permissions.yaml yet
    # must not be able to reach internal/VPN network space.
    allow_private_urls: bool = False
    # Scoped OUTBOUND allowlist: when non-empty, the agent's outbound tools may reach ONLY private
    # IPs/CIDRs listed here, regardless of allow_private_urls — granularity for "this agent may
    # reach just this one internal IP" rather than all-or-nothing. Empty = no scoped allowlist,
    # falls back to the blunt allow_private_urls boolean (unchanged prior behavior). Entries are
    # individual IPs or CIDR ranges, same shape as allowed_ips above.
    allowed_private_ips: Tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentPermissions:
    """Resolved capabilities for one profile. All defaults are the fail-closed values."""
    webhooks: WebhookPermissions = field(default_factory=WebhookPermissions)
    channels: ChannelPermissions = field(default_factory=ChannelPermissions)
    skills: SkillPermissions = field(default_factory=SkillPermissions)
    network: NetworkPermissions = field(default_factory=NetworkPermissions)


def _unrestricted() -> AgentPermissions:
    return AgentPermissions(
        webhooks=WebhookPermissions(can_manage=True, max=2**31 - 1),
        channels=ChannelPermissions(max=2**31 - 1, allowed_platforms=()),
        skills=SkillPermissions(policy="read_write_create", allowed=()),
        # allowed_private_ips stays empty here on purpose: allow_private_urls=True already grants
        # unrestricted outbound private reach for this always-unrestricted profile, so a scoped
        # allowlist entry would be redundant (and, per the override rule, would actually NARROW
        # it — the opposite of what "unrestricted" means for this profile).
        network=NetworkPermissions(allowed_ips=(), allow_private_urls=True, allowed_private_ips=()),
    )


def control_dir_for(profile_dir: Path) -> Path:
    """The out-of-home control directory holding *profile_dir*'s permissions file.

    Deliberately OUTSIDE the profile home: the Landlock ruleset grants the agent's terminal
    read/WRITE over its whole home (``agent/landlock_sandbox.py``), so a permissions file stored
    inside it is writable by the very agent it constrains — a plain
    ``echo 'skills: {policy: read_write_create}' > $HOME/permissions.yaml`` from the terminal
    tool silently grants full rights. Landlock rules UNION rather than override, so a narrower
    read-only rule on the file cannot claw that back (verified against a live 6.x kernel); the
    file has to live somewhere the home-wide write rule does not cover. The sandbox adds this
    directory as READ-ONLY, so the agent can still read its own permissions but never rewrite
    them.

    Layout: ``<profiles_root>/.control/<profile name>/permissions.yaml``, i.e. a sibling of the
    ``profiles/`` tree rather than a child of any single profile.
    """
    profile_dir = Path(profile_dir)
    return profile_dir.parent / _CONTROL_DIRNAME / profile_dir.name


def permissions_path(profile_dir: Path) -> Path:
    """Path to *profile_dir*'s ``permissions.yaml``.

    Resolves to the out-of-home control directory (see :func:`control_dir_for`). A legacy
    in-home file is still honoured for reading when no control-dir file exists yet, so an
    existing deployment keeps working until :func:`write_agent_permissions` migrates it on the
    next admin write.
    """
    profile_dir = Path(profile_dir)
    controlled = control_dir_for(profile_dir) / _PERMISSIONS_FILENAME
    if controlled.exists():
        return controlled
    legacy = profile_dir / _PERMISSIONS_FILENAME
    if legacy.exists():
        return legacy
    return controlled


def legacy_permissions_path(profile_dir: Path) -> Path:
    """The pre-migration in-home location, kept only so writers can clean it up."""
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


def _coerce_ip_tuple(value: Any) -> Tuple[str, ...]:
    """Like :func:`_coerce_str_tuple`, but silently drops entries that are not a valid IP address
    or CIDR range — a hand-edited permissions.yaml with a typo'd entry should not either crash
    every permission read or silently become "no IP restriction" (empty tuple would mean
    unrestricted); dropping just the bad entry keeps whatever entries WERE valid enforced.
    Strict validation (reject the whole write on any invalid entry) belongs in the admin write
    path (``hermes_cli/dashboard_auth/admin_routes.py``), not here."""
    import ipaddress
    result = []
    for raw in _coerce_str_tuple(value):
        try:
            ipaddress.ip_network(raw, strict=False)
        except ValueError:
            continue
        result.append(raw)
    return tuple(result)


def _parse_permissions(raw: dict) -> AgentPermissions:
    webhooks_raw = raw.get("webhooks") or {}
    channels_raw = raw.get("channels") or {}
    skills_raw = raw.get("skills") or {}
    network_raw = raw.get("network") or {}

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
        network=NetworkPermissions(
            allowed_ips=_coerce_ip_tuple(network_raw.get("allowed_ips")),
            allow_private_urls=bool(network_raw.get("allow_private_urls", False)),
            allowed_private_ips=_coerce_ip_tuple(network_raw.get("allowed_private_ips")),
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
        "network": {
            "allowed_ips": list(perms.network.allowed_ips),
            "allow_private_urls": perms.network.allow_private_urls,
            "allowed_private_ips": list(perms.network.allowed_private_ips),
        },
    }


def write_agent_permissions(profile_dir: Path, perms: AgentPermissions) -> None:
    """Persist *perms* for the profile at *profile_dir*. The ONLY writer of permissions.yaml.

    Stored in the out-of-home control directory (:func:`control_dir_for`), which is what actually
    keeps an agent from rewriting its own grants: the file mode cannot, since the admin process
    and the agent may run as the same OS user, and a file's owner can always rewrite it.

    Written world-READABLE (0644) on purpose. The agent MUST be able to read the permissions that
    constrain it — ``load_agent_permissions`` is fail-closed, so a file the agent cannot read
    silently degrades every grant to "deny" and the admin's settings stop taking effect. The file
    holds no secrets (policy levels, counts, platform names, IP ranges), and write protection
    comes from the directory, not the mode.

    Callers MUST be an already-authenticated admin surface; this function performs no
    authorization check itself, by design (see module docstring: the guarantee is that nothing
    reachable from an agent's own turn imports this function at all).
    """
    profile_dir = Path(profile_dir)
    control_dir = control_dir_for(profile_dir)
    control_dir.mkdir(parents=True, exist_ok=True)
    path = control_dir / _PERMISSIONS_FILENAME
    atomic_yaml_write(path, _to_raw(perms), create_mode=_PERMISSIONS_FILE_MODE)

    # Migration: a pre-existing in-home file would still be readable (and agent-writable), so it
    # must not survive as a shadow copy once the authoritative one lives out of reach.
    legacy = legacy_permissions_path(profile_dir)
    if legacy.exists():
        with suppress(OSError):
            legacy.unlink()

    cache_key = _cache_key(path)
    if cache_key is not None:
        _PERMISSIONS_CACHE.pop(cache_key, None)
    _PERMISSIONS_CACHE.clear()


def clear_agent_permissions_cache() -> None:
    """Test hook — drop the shared permissions cache."""
    _PERMISSIONS_CACHE.clear()
