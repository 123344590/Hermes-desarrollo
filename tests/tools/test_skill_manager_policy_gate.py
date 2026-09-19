"""Tests for the admin-granted skills.policy gate in tools/skill_manager_tool.py::skill_manage."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli.agent_permissions import AgentPermissions, SkillPermissions, write_agent_permissions
from tools.skill_manager_tool import _create_skill, skill_manage

VALID_SKILL_CONTENT = """\
---
name: test-skill
description: A test skill for unit testing.
---

# Test Skill

Step 1: Do the thing.
"""


@contextmanager
def _skill_dir(tmp_path):
    with patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]):
        yield


def _set_policy(monkeypatch, policy: str, profile_dir: Path):
    write_agent_permissions(profile_dir, AgentPermissions(skills=SkillPermissions(policy=policy)))
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))


@pytest.fixture
def named_profile(tmp_path):
    profile_dir = tmp_path / "profiles" / "crm-support"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
    return profile_dir


class TestReadOnlyPolicy:
    def test_create_blocked(self, tmp_path, monkeypatch, named_profile):
        _set_policy(monkeypatch, "read", named_profile)
        with _skill_dir(tmp_path):
            result = json.loads(skill_manage(action="create", name="new-skill", content=VALID_SKILL_CONTENT))
        assert result["success"] is False
        assert "does not allow 'create'" in result["error"]

    def test_edit_blocked(self, tmp_path, monkeypatch, named_profile):
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT, category=None)
            _set_policy(monkeypatch, "read", named_profile)
            result = json.loads(skill_manage(action="edit", name="my-skill", content=VALID_SKILL_CONTENT))
        assert result["success"] is False
        assert "does not allow 'edit'" in result["error"]

    def test_delete_blocked(self, tmp_path, monkeypatch, named_profile):
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT, category=None)
            _set_policy(monkeypatch, "read", named_profile)
            result = json.loads(skill_manage(action="delete", name="my-skill"))
        assert result["success"] is False


class TestReadWritePolicy:
    def test_edit_allowed_create_blocked(self, tmp_path, monkeypatch, named_profile):
        with _skill_dir(tmp_path):
            _create_skill("my-skill", VALID_SKILL_CONTENT, category=None)
            _set_policy(monkeypatch, "read_write", named_profile)

            create_result = json.loads(
                skill_manage(action="create", name="another-skill", content=VALID_SKILL_CONTENT))
            assert create_result["success"] is False
            assert "does not allow 'create'" in create_result["error"]

            edit_result = json.loads(skill_manage(action="edit", name="my-skill", content=VALID_SKILL_CONTENT))
        assert edit_result["success"] is True


class TestReadWriteCreatePolicy:
    def test_create_and_edit_both_allowed(self, tmp_path, monkeypatch, named_profile):
        _set_policy(monkeypatch, "read_write_create", named_profile)
        with _skill_dir(tmp_path):
            create_result = json.loads(
                skill_manage(action="create", name="new-skill", content=VALID_SKILL_CONTENT))
            assert create_result["success"] is True
            edit_result = json.loads(skill_manage(action="edit", name="new-skill", content=VALID_SKILL_CONTENT))
        assert edit_result["success"] is True


def test_default_profile_unrestricted(tmp_path):
    # No HERMES_HOME override beyond the pytest session sandbox: resolves to "default", which
    # is always unrestricted regardless of permissions.yaml.
    with _skill_dir(tmp_path):
        result = json.loads(skill_manage(action="create", name="new-skill", content=VALID_SKILL_CONTENT))
    assert result["success"] is True
