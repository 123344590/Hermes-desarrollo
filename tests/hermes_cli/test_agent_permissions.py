"""Tests for hermes_cli/agent_permissions.py — per-profile admin-granted permissions."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli.agent_permissions import (
    AgentPermissions,
    ChannelPermissions,
    SkillPermissions,
    WebhookPermissions,
    clear_agent_permissions_cache,
    load_agent_permissions,
    permissions_path,
    write_agent_permissions,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    clear_agent_permissions_cache()
    yield
    clear_agent_permissions_cache()


def test_missing_file_is_fail_closed_for_named_profile(tmp_path):
    perms = load_agent_permissions(tmp_path, profile_name="crm-support-1")
    assert perms.webhooks.can_manage is False
    assert perms.webhooks.max == 0
    assert perms.channels.max == 0
    assert perms.channels.allowed_platforms == ()
    assert perms.skills.policy == "read"
    assert perms.skills.allowed == ()


def test_default_profile_is_unrestricted_even_without_file(tmp_path):
    perms = load_agent_permissions(tmp_path, profile_name="default")
    assert perms.webhooks.can_manage is True
    assert perms.webhooks.max > 0
    assert perms.skills.policy == "read_write_create"


def test_active_profile_resolution_treats_bare_hermes_home_as_default(tmp_path):
    # No profile_name given: load_agent_permissions must infer via get_active_profile_name(),
    # which treats a HERMES_HOME that isn't a named profiles/<name> dir as "default".
    perms = load_agent_permissions(tmp_path)
    assert perms.webhooks.can_manage is True


def test_round_trip_write_then_load(tmp_path):
    perms = AgentPermissions(
        webhooks=WebhookPermissions(can_manage=True, max=3),
        channels=ChannelPermissions(max=2, allowed_platforms=("telegram", "whatsapp")),
        skills=SkillPermissions(policy="read_write", allowed=("crm-tags", "crm-conversations")),
    )
    write_agent_permissions(tmp_path, perms)
    loaded = load_agent_permissions(tmp_path, profile_name="crm-support-1")
    assert loaded.webhooks.can_manage is True
    assert loaded.webhooks.max == 3
    assert loaded.channels.max == 2
    assert loaded.channels.allowed_platforms == ("telegram", "whatsapp")
    assert loaded.skills.policy == "read_write"
    assert set(loaded.skills.allowed) == {"crm-tags", "crm-conversations"}


def test_write_sets_restrictive_file_mode(tmp_path):
    write_agent_permissions(tmp_path, AgentPermissions())
    path = permissions_path(tmp_path)
    mode = path.stat().st_mode & 0o777
    assert mode == 0o640, f"expected 0640, got {oct(mode)}"


def test_cache_invalidates_on_rewrite(tmp_path):
    write_agent_permissions(tmp_path, AgentPermissions(webhooks=WebhookPermissions(can_manage=False, max=0)))
    first = load_agent_permissions(tmp_path, profile_name="crm-support-1")
    assert first.webhooks.can_manage is False

    write_agent_permissions(tmp_path, AgentPermissions(webhooks=WebhookPermissions(can_manage=True, max=5)))
    second = load_agent_permissions(tmp_path, profile_name="crm-support-1")
    assert second.webhooks.can_manage is True
    assert second.webhooks.max == 5


def test_malformed_yaml_fails_closed(tmp_path):
    permissions_path(tmp_path).write_text("not: valid: yaml: [", encoding="utf-8")
    perms = load_agent_permissions(tmp_path, profile_name="crm-support-1")
    assert perms.webhooks.can_manage is False


def test_unknown_skills_policy_value_falls_back_to_read(tmp_path):
    permissions_path(tmp_path).write_text("skills:\n  policy: everything\n", encoding="utf-8")
    perms = load_agent_permissions(tmp_path, profile_name="crm-support-1")
    assert perms.skills.policy == "read"
