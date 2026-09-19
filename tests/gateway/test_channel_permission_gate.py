"""Tests for GatewayRunner._channel_permitted / _create_adapter admin channel policy gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from hermes_cli.agent_permissions import AgentPermissions, ChannelPermissions, write_agent_permissions


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    root.mkdir()
    (root / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


def _runner() -> GatewayRunner:
    config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True)})
    return GatewayRunner(config)


def test_default_profile_unrestricted_allows_any_platform(_isolate):
    runner = _runner()
    assert runner._channel_permitted(Platform.TELEGRAM) is True


def test_named_profile_without_permissions_file_blocked(tmp_path):
    profile_dir = tmp_path / "profiles" / "crm-support"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
    import os
    os.environ["HERMES_HOME"] = str(profile_dir)
    try:
        runner = _runner()
        assert runner._channel_permitted(Platform.TELEGRAM) is False
    finally:
        del os.environ["HERMES_HOME"]


def test_disallowed_platform_blocked(tmp_path, monkeypatch):
    profile_dir = tmp_path / "profiles" / "crm-support"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
    write_agent_permissions(
        profile_dir,
        AgentPermissions(channels=ChannelPermissions(max=5, allowed_platforms=("whatsapp",))))
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))

    runner = _runner()
    assert runner._channel_permitted(Platform.TELEGRAM) is False


def test_allowed_platform_within_max_permitted(tmp_path, monkeypatch):
    profile_dir = tmp_path / "profiles" / "crm-support"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
    write_agent_permissions(
        profile_dir,
        AgentPermissions(channels=ChannelPermissions(max=5, allowed_platforms=("telegram",))))
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))

    runner = _runner()
    assert runner._channel_permitted(Platform.TELEGRAM) is True


def test_max_reached_blocks_new_channel_on_named_profile(tmp_path, monkeypatch):
    profile_dir = tmp_path / "profiles" / "crm-support"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
    write_agent_permissions(
        profile_dir, AgentPermissions(channels=ChannelPermissions(max=1, allowed_platforms=())))
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))

    runner = _runner()
    runner.adapters[Platform.TELEGRAM] = object()  # one already active on the shared/default map
    # Named (non-"default") profiles track their own adapters under _profile_adapters, not
    # runner.adapters — so this exercises the "no entry yet" branch (nothing counted, permitted).
    assert runner._channel_permitted(Platform.DISCORD) is True

    runner._profile_adapters = {"crm-support": {Platform.TELEGRAM: object()}}
    assert runner._channel_permitted(Platform.DISCORD) is False
