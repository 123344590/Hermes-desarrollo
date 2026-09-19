"""Tests for the per-profile IP allowlist gate in gateway/platforms/api_server.py::_check_auth.

Regression coverage for a real bug caught in live security testing against a deployed instance:
the gate originally trusted ``X-Real-IP``/``X-Forwarded-For`` (attacker-controlled request
headers) ahead of the real TCP peer address, so any client could bypass the allowlist just by
setting a header to an allowed IP. Fixed to use ONLY ``remote``/``peer_ip`` (the actual socket
peer, which a client cannot forge) — this project has no "trusted reverse proxy" concept for the
api_server, so those headers are never a legitimate signal here. See
``APIServerAdapter._check_ip_allowlist``'s docstring for the full rationale.

Uses a fake request (same pattern as
tests/gateway/test_api_server_multiplex_secret_scope.py::TestProfileScopedApiAuthentication) —
constructing a real socket-backed aiohttp TestClient can't control what IP the connection appears
to come from (it's always loopback), which is exactly the property under test here."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_cli.agent_permissions import AgentPermissions, NetworkPermissions, write_agent_permissions


def _make_adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True))


def _fake_request(*, remote: str = "", headers: dict | None = None):
    """A minimal stand-in for aiohttp's Request, just enough for
    _request_audit_context/_check_ip_allowlist: .remote, .headers, .transport, .method, .path_qs."""
    return SimpleNamespace(
        remote=remote, headers=headers or {}, transport=None, method="GET", path_qs="/test")


@pytest.fixture
def named_profile(tmp_path, monkeypatch):
    profile_home = tmp_path / "profiles" / "crm-support"
    profile_home.mkdir(parents=True)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: profile_home)
    return profile_home


class TestIpAllowlist:
    def test_no_restriction_configured_allows_any_ip(self, named_profile):
        adapter = _make_adapter()
        assert adapter._check_ip_allowlist(_fake_request(remote="203.0.113.9")) is None

    def test_disallowed_ip_rejected(self, named_profile):
        write_agent_permissions(named_profile, AgentPermissions(network=NetworkPermissions(allowed_ips=("203.0.113.0/24",))))
        adapter = _make_adapter()
        resp = adapter._check_ip_allowlist(_fake_request(remote="198.51.100.7"))
        assert resp is not None
        assert resp.status == 403

    def test_allowed_single_ip_passes(self, named_profile):
        write_agent_permissions(named_profile, AgentPermissions(network=NetworkPermissions(allowed_ips=("198.51.100.7",))))
        adapter = _make_adapter()
        assert adapter._check_ip_allowlist(_fake_request(remote="198.51.100.7")) is None

    def test_cidr_range_matches(self, named_profile):
        write_agent_permissions(named_profile, AgentPermissions(network=NetworkPermissions(allowed_ips=("198.51.100.0/24",))))
        adapter = _make_adapter()
        assert adapter._check_ip_allowlist(_fake_request(remote="198.51.100.200")) is None

    def test_cidr_range_excludes_outside_ip(self, named_profile):
        write_agent_permissions(named_profile, AgentPermissions(network=NetworkPermissions(allowed_ips=("198.51.100.0/24",))))
        adapter = _make_adapter()
        resp = adapter._check_ip_allowlist(_fake_request(remote="203.0.113.9"))
        assert resp is not None
        assert resp.status == 403

    def test_no_resolvable_ip_fails_closed(self, named_profile):
        write_agent_permissions(named_profile, AgentPermissions(network=NetworkPermissions(allowed_ips=("198.51.100.0/24",))))
        adapter = _make_adapter()
        resp = adapter._check_ip_allowlist(_fake_request(remote=""))
        assert resp is not None
        assert resp.status == 403


class TestIpAllowlistIgnoresClientControlledHeaders:
    """Regression: X-Real-IP / X-Forwarded-For must NEVER be trusted — a client sets its own
    request headers, so honoring them would let anyone bypass the allowlist entirely."""

    def test_x_real_ip_spoof_does_not_bypass_a_disallowed_real_peer(self, named_profile):
        write_agent_permissions(named_profile, AgentPermissions(network=NetworkPermissions(allowed_ips=("203.0.113.0/24",))))
        adapter = _make_adapter()
        # Real socket peer (198.51.100.7) is NOT in the allowlist; the client claims via header
        # to be 203.0.113.5 (which IS in the allowlist). Must still be rejected.
        resp = adapter._check_ip_allowlist(_fake_request(
            remote="198.51.100.7", headers={"X-Real-IP": "203.0.113.5"}))
        assert resp is not None
        assert resp.status == 403

    def test_x_forwarded_for_spoof_does_not_bypass_a_disallowed_real_peer(self, named_profile):
        write_agent_permissions(named_profile, AgentPermissions(network=NetworkPermissions(allowed_ips=("203.0.113.0/24",))))
        adapter = _make_adapter()
        resp = adapter._check_ip_allowlist(_fake_request(
            remote="198.51.100.7", headers={"X-Forwarded-For": "203.0.113.5"}))
        assert resp is not None
        assert resp.status == 403

    def test_spoofed_header_cannot_grant_access_the_real_peer_lacks(self, named_profile):
        """Same scenario as the live-security-test finding: allowlist configured to reject the
        real client; request must fail even though the client sends a header claiming an
        allowed IP."""
        write_agent_permissions(named_profile, AgentPermissions(network=NetworkPermissions(allowed_ips=("198.51.100.7",))))
        adapter = _make_adapter()
        resp = adapter._check_ip_allowlist(_fake_request(
            remote="203.0.113.9", headers={"X-Real-IP": "198.51.100.7", "X-Forwarded-For": "198.51.100.7"}))
        assert resp is not None
        assert resp.status == 403
