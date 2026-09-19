"""Tests for agent/landlock_sandbox.py — kernel-enforced per-agent filesystem confinement.

Linux-only (Landlock is a Linux LSM); skipped everywhere else. Runs the syscalls for real against
a real subprocess — no mocks — because the whole point of this module is a kernel-enforced
boundary, which a mocked ``libc.syscall`` cannot meaningfully verify.
"""
from __future__ import annotations

import functools
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Landlock is Linux-only")


def _landlock_actually_supported() -> bool:
    from agent.landlock_sandbox import landlock_supported
    return landlock_supported()


requires_landlock = pytest.mark.skipif(
    sys.platform == "linux" and not _landlock_actually_supported(),
    reason="Landlock unsupported on this kernel (needs Linux >= 5.13)")


class TestLandlockSupported:
    def test_returns_bool_and_is_cached(self):
        from agent.landlock_sandbox import landlock_supported
        first = landlock_supported()
        assert isinstance(first, bool)
        assert landlock_supported() is first  # cached, same object identity for a bool singleton


@requires_landlock
class TestRestrictToProfileHome:
    def test_confines_subprocess_to_profile_home(self, tmp_path):
        from agent.landlock_sandbox import restrict_to_profile_home_or_warn

        profile_home = tmp_path / "profile"
        profile_home.mkdir()
        (profile_home / "own_file.txt").write_text("mine", encoding="utf-8")
        outside = tmp_path / "outside.txt"
        outside.write_text("not mine", encoding="utf-8")

        # Read inside profile_home: allowed.
        proc = subprocess.Popen(
            ["cat", str(profile_home / "own_file.txt")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            preexec_fn=functools.partial(restrict_to_profile_home_or_warn, profile_home))
        out, _ = proc.communicate(timeout=10)
        assert proc.returncode == 0
        assert out.strip() == "mine"

    def test_blocks_read_outside_profile_home(self, tmp_path):
        from agent.landlock_sandbox import restrict_to_profile_home_or_warn

        profile_home = tmp_path / "profile"
        profile_home.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("not mine", encoding="utf-8")

        proc = subprocess.Popen(
            ["cat", str(outside)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            preexec_fn=functools.partial(restrict_to_profile_home_or_warn, profile_home))
        out, _ = proc.communicate(timeout=10)
        assert proc.returncode != 0
        assert "Permission denied" in out

    def test_blocks_write_outside_profile_home(self, tmp_path):
        from agent.landlock_sandbox import restrict_to_profile_home_or_warn

        profile_home = tmp_path / "profile"
        profile_home.mkdir()
        target = tmp_path / "pwned.txt"

        proc = subprocess.Popen(
            ["bash", "-c", f"echo pwned > {target}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            preexec_fn=functools.partial(restrict_to_profile_home_or_warn, profile_home))
        proc.communicate(timeout=10)
        assert proc.returncode != 0
        assert not target.exists()

    def test_allows_write_inside_profile_home(self, tmp_path):
        from agent.landlock_sandbox import restrict_to_profile_home_or_warn

        profile_home = tmp_path / "profile"
        profile_home.mkdir()
        target = profile_home / "new_file.txt"

        proc = subprocess.Popen(
            ["bash", "-c", f"echo hello > {target}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            preexec_fn=functools.partial(restrict_to_profile_home_or_warn, profile_home))
        proc.communicate(timeout=10)
        assert proc.returncode == 0
        assert target.read_text(encoding="utf-8").strip() == "hello"

    def test_blocks_listing_a_sibling_profile_dir(self, tmp_path):
        """Simulates two agent profiles under the same profiles/ root — one must not be able to
        list or read the other's directory, matching the real deployment layout
        (<HERMES_HOME>/profiles/<name>)."""
        from agent.landlock_sandbox import restrict_to_profile_home_or_warn

        profiles_root = tmp_path / "profiles"
        profile_a = profiles_root / "agent-a"
        profile_b = profiles_root / "agent-b"
        profile_a.mkdir(parents=True)
        profile_b.mkdir(parents=True)
        (profile_b / "secret.txt").write_text("agent-b's secret", encoding="utf-8")

        proc = subprocess.Popen(
            ["bash", "-c", f"ls {profile_b}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            preexec_fn=functools.partial(restrict_to_profile_home_or_warn, profile_a))
        out, _ = proc.communicate(timeout=10)
        assert proc.returncode != 0
        assert "Permission denied" in out

    def test_shared_system_tmp_is_not_a_cross_agent_covert_channel(self, tmp_path):
        """Regression: the shared system /tmp used to be granted read-write to every sandboxed
        process (all agents run as the same OS uid), so one agent's process could write a file
        under /tmp and a DIFFERENT agent's sandboxed process could read it straight back —
        confirmed live against a real deployment. Each profile now gets its OWN tmp (under its
        own profile_home, already covered by that directory's read-write rule) instead of the
        shared /tmp, and /tmp itself is no longer writable at all from inside the sandbox."""
        from agent.landlock_sandbox import restrict_to_profile_home_or_warn

        profile_a = tmp_path / "profiles" / "agent-a"
        profile_b = tmp_path / "profiles" / "agent-b"
        profile_a.mkdir(parents=True)
        profile_b.mkdir(parents=True)

        # Agent A's process must not be able to write to the shared system /tmp at all.
        marker = f"/tmp/hermes_landlock_test_{os.getpid()}.txt"
        proc = subprocess.Popen(
            ["bash", "-c", f"echo leaked > {marker}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            preexec_fn=functools.partial(restrict_to_profile_home_or_warn, profile_a))
        proc.communicate(timeout=10)
        assert proc.returncode != 0
        assert not Path(marker).exists()

        # Agent A CAN use its own scoped tmp (TMPDIR points there; mktemp respects it).
        proc = subprocess.Popen(
            ["bash", "-c", "echo TMPDIR=$TMPDIR && mktemp"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            preexec_fn=functools.partial(restrict_to_profile_home_or_warn, profile_a))
        out, _ = proc.communicate(timeout=10)
        assert proc.returncode == 0
        assert str(profile_a / "cache" / "terminal") in out


class TestLocalEnvironmentIntegration:
    """Exercises the real preexec_fn wiring in tools/environments/local.py, not just the
    landlock_sandbox module in isolation."""

    def test_default_profile_is_never_confined(self, monkeypatch):
        """The admin's own 'default' profile must get preexec_fn=None — same exemption
        AgentPermissions already uses (_UNRESTRICTED_PROFILES)."""
        from tools.environments.local import _agent_landlock_preexec_fn
        monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
        assert _agent_landlock_preexec_fn() is None

    @requires_landlock
    def test_named_profile_gets_a_preexec_fn(self, monkeypatch, tmp_path):
        from tools.environments.local import _agent_landlock_preexec_fn
        monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "crm-support")
        monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
        fn = _agent_landlock_preexec_fn()
        assert fn is not None
        assert callable(fn)
