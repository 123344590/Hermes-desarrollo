"""Regression guard: write_agent_permissions must stay unreachable from agent-turn code.

The whole point of hermes_cli/agent_permissions.py is that an agent can never grant itself
capabilities — see its module docstring. The only enforcement is that nothing importable from
inside an agent's own turn (tools/, agent/, gateway/) references the writer at all. This test
statically scans those trees for that import so a future change can't reintroduce it silently.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCANNED_ROOTS = ("tools", "agent", "gateway")
_FORBIDDEN_NAME = "write_agent_permissions"


def _iter_python_files():
    for root_name in _SCANNED_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        yield from root.rglob("*.py")


def _imports_forbidden_name(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == _FORBIDDEN_NAME for alias in node.names):
                return True
        elif isinstance(node, ast.Attribute) and node.attr == _FORBIDDEN_NAME:
            return True
        elif isinstance(node, ast.Name) and node.id == _FORBIDDEN_NAME:
            return True
    return False


def test_write_agent_permissions_not_imported_from_agent_turn_code():
    offenders = [str(p.relative_to(REPO_ROOT)) for p in _iter_python_files() if _imports_forbidden_name(p)]
    assert not offenders, (
        "write_agent_permissions must only be called from admin surfaces "
        "(hermes_cli/dashboard_auth/admin_routes.py or `hermes admin` CLI commands), "
        f"but found references in: {offenders}"
    )
