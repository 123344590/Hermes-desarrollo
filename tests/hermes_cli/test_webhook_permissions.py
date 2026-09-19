"""Tests for the admin-granted permission gate in hermes_cli/webhook.py::_cmd_subscribe."""

from __future__ import annotations

from argparse import Namespace

import pytest

from hermes_cli.agent_permissions import AgentPermissions, WebhookPermissions, write_agent_permissions
from hermes_cli.webhook import _load_subscriptions, webhook_command


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.webhook._is_webhook_enabled", lambda: True)


def _make_args(**kwargs):
    defaults = {
        "webhook_action": "subscribe", "name": "", "prompt": "", "events": "", "description": "",
        "skills": "", "deliver": "log", "deliver_chat_id": "", "secret": "", "route_profile": None,
        "payload": "", "script": "",
    }
    defaults.update(kwargs)
    return Namespace(**defaults)


def _named_profile(tmp_path, name, *, can_manage=True, max_webhooks=5):
    profile_dir = tmp_path / "profiles" / name
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
    write_agent_permissions(
        profile_dir, AgentPermissions(webhooks=WebhookPermissions(can_manage=can_manage, max=max_webhooks)))
    return profile_dir


def test_subscribe_blocked_without_can_manage(tmp_path, capsys):
    _named_profile(tmp_path, "crm-support", can_manage=False, max_webhooks=5)
    webhook_command(_make_args(name="s", route_profile="crm-support"))
    assert "not permitted to manage webhooks" in capsys.readouterr().out
    assert "s" not in _load_subscriptions()


def test_subscribe_allowed_with_can_manage(tmp_path):
    _named_profile(tmp_path, "crm-support", can_manage=True, max_webhooks=5)
    webhook_command(_make_args(name="s", route_profile="crm-support"))
    assert "s" in _load_subscriptions()


def test_subscribe_blocked_once_max_reached(tmp_path, capsys):
    _named_profile(tmp_path, "crm-support", can_manage=True, max_webhooks=1)
    webhook_command(_make_args(name="first", route_profile="crm-support"))
    assert "first" in _load_subscriptions()

    webhook_command(_make_args(name="second", route_profile="crm-support"))
    out = capsys.readouterr().out
    assert "reached its webhook limit" in out
    assert "second" not in _load_subscriptions()


def test_update_of_existing_subscription_not_blocked_by_max(tmp_path):
    _named_profile(tmp_path, "crm-support", can_manage=True, max_webhooks=1)
    webhook_command(_make_args(name="first", route_profile="crm-support"))
    assert "first" in _load_subscriptions()

    # Same name again: is_update=True, so the max check must not apply even though
    # the profile is already at its limit.
    webhook_command(_make_args(name="first", route_profile="crm-support", description="updated"))
    assert _load_subscriptions()["first"]["description"] == "updated"


def test_default_profile_unrestricted(tmp_path):
    # No route_profile: falls back to "default", which is always unrestricted.
    webhook_command(_make_args(name="s"))
    assert "s" in _load_subscriptions()
