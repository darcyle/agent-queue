"""Tests for ToolRegistry -- tool categorization and on-demand loading."""

import pytest

from src.tool_registry import ToolRegistry, _TOOL_CATEGORIES


def _make_tool(name: str) -> dict:
    """Create a minimal tool definition dict."""
    return {
        "name": name,
        "description": f"Tool: {name}",
        "input_schema": {"type": "object", "properties": {}},
    }


def _build_sample_tools() -> list[dict]:
    """Build a list covering all categorized tools + some core tools."""
    tools = []
    # Add all categorized tools
    for name in _TOOL_CATEGORIES:
        tools.append(_make_tool(name))
    # Add some core tools (not in _TOOL_CATEGORIES)
    for core_name in [
        "create_task", "list_tasks", "edit_task", "get_task",
        "memory_search",
    ]:
        tools.append(_make_tool(core_name))
    return tools


@pytest.fixture
def registry():
    """Registry initialized with sample tools (no chat_agent import)."""
    return ToolRegistry(tools=_build_sample_tools())


def test_registry_has_core_tools(registry):
    core = registry.get_core_tools()
    core_names = {t["name"] for t in core}
    assert "create_task" in core_names
    assert "list_tasks" in core_names
    assert "edit_task" in core_names
    assert "get_task" in core_names
    assert "browse_tools" in core_names
    assert "load_tools" in core_names
    assert "memory_search" in core_names
    assert "send_message" in core_names


def test_registry_has_categories(registry):
    categories = registry.get_categories()
    cat_names = {c["name"] for c in categories}
    assert cat_names == {"git", "project", "agent", "hooks", "memory", "system"}

    for cat in categories:
        assert "name" in cat
        assert "description" in cat
        assert "tool_count" in cat
        assert isinstance(cat["tool_count"], int)
        assert cat["tool_count"] > 0


def test_get_category_tools(registry):
    git_tools = registry.get_category_tools("git")
    assert len(git_tools) > 0
    git_names = {t["name"] for t in git_tools}
    assert "git_push" in git_names
    assert "git_create_pr" in git_names
    # Core tools should NOT appear in categories
    assert "create_task" not in git_names


def test_get_unknown_category_returns_none(registry):
    result = registry.get_category_tools("nonexistent")
    assert result is None


def test_all_tools_returns_everything(registry):
    all_tools = registry.get_all_tools()
    all_names = {t["name"] for t in all_tools}
    assert "create_task" in all_names  # core
    assert "git_push" in all_names     # git category
    assert "create_project" in all_names  # project category


def test_no_duplicate_tool_names(registry):
    all_tools = registry.get_all_tools()
    names = [t["name"] for t in all_tools]
    assert len(names) == len(set(names)), (
        f"Duplicate tool names found: "
        f"{[n for n in names if names.count(n) > 1]}"
    )


def test_category_tool_count_matches(registry):
    categories = registry.get_categories()
    for cat in categories:
        tools = registry.get_category_tools(cat["name"])
        assert len(tools) == cat["tool_count"], (
            f"Category {cat['name']}: metadata says "
            f"{cat['tool_count']} tools but get_category_tools "
            f"returned {len(tools)}"
        )


def test_get_category_tool_names(registry):
    names = registry.get_category_tool_names("git")
    assert isinstance(names, list)
    assert "git_push" in names
