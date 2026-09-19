"""Tests for the admin-granted skills.allowed enforcement in
agent/skill_utils.py::get_disabled_skill_names (see hermes_cli/agent_permissions.py)."""

from __future__ import annotations

from agent import skill_utils
from agent.skill_utils import get_disabled_skill_names
from hermes_cli.agent_permissions import AgentPermissions, SkillPermissions, write_agent_permissions


def _make_skill(skills_dir, name):
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test\n---\n\nBody.\n", encoding="utf-8")


def _reset_caches():
    skill_utils._raw_config_cache_clear()
    from hermes_cli.agent_permissions import clear_agent_permissions_cache
    clear_agent_permissions_cache()


def test_no_allowlist_leaves_disabled_set_unchanged(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    skills_dir = hermes_home / "skills"
    _make_skill(skills_dir, "crm-tags")
    _make_skill(skills_dir, "unrelated-skill")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _reset_caches()

    assert get_disabled_skill_names() == set()


def test_allowlist_hides_everything_not_listed(tmp_path, monkeypatch):
    profile_dir = tmp_path / "profiles" / "crm-support"
    skills_dir = profile_dir / "skills"
    _make_skill(skills_dir, "crm-tags")
    _make_skill(skills_dir, "crm-conversations")
    _make_skill(skills_dir, "unrelated-skill")
    write_agent_permissions(profile_dir, AgentPermissions(skills=SkillPermissions(allowed=("crm-tags",))))
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))
    _reset_caches()

    disabled = get_disabled_skill_names()
    assert "crm-conversations" in disabled
    assert "unrelated-skill" in disabled
    assert "crm-tags" not in disabled


def test_essential_skills_never_disabled_by_allowlist(tmp_path, monkeypatch):
    from agent.skill_utils import ESSENTIAL_SKILLS

    profile_dir = tmp_path / "profiles" / "crm-support"
    skills_dir = profile_dir / "skills"
    for essential in ESSENTIAL_SKILLS:
        _make_skill(skills_dir, essential)
    _make_skill(skills_dir, "crm-tags")
    write_agent_permissions(profile_dir, AgentPermissions(skills=SkillPermissions(allowed=("crm-tags",))))
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))
    _reset_caches()

    disabled = get_disabled_skill_names()
    assert disabled.isdisjoint(ESSENTIAL_SKILLS)
