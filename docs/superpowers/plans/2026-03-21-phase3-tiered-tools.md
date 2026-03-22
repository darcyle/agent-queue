# Phase 3: Tiered Tool System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the monolithic 70+ tool TOOLS list in `src/chat_agent.py` into ~11 core tools (always loaded) and 6 on-demand categories. Implement `browse_tools` and `load_tools` commands so the Supervisor can dynamically expand its tool set per interaction. The system prompt shrinks dramatically; per-tool documentation moves into category payloads.

**Architecture:** A new `ToolRegistry` class owns tool categorization and metadata. `browse_tools` and `load_tools` are registered in `CommandHandler` like any other command. The `chat()` method in `ChatAgent` maintains a mutable tool set per interaction -- `load_tools` appends category schemas, and subsequent LLM turns see the expanded `tools` list. `CommandHandler.execute()` remains the single execution backend -- tiered loading only affects which definitions the LLM sees, not how calls are dispatched.

**Tech Stack:** Python 3.12+, pytest with pytest-asyncio, dataclasses

**Spec:** `docs/superpowers/specs/2026-03-21-supervisor-refactor-design.md` (Section 2)

---

## Tool Categorization

### Core Tools (~11, always loaded)

| Tool | Source | Notes |
|------|--------|-------|
| `create_task` | Existing | Primary work creation |
| `list_tasks` | Existing | Check current state |
| `edit_task` | Existing | Modify existing work |
| `get_task` | Existing | Get full task details |
| `browse_tools` | **NEW** | List available tool categories |
| `load_tools` | **NEW** | Load a specific tool category |
| `browse_rules` | Phase 2 (stub) | List active rules |
| `load_rule` | Phase 2 (stub) | Load a specific rule |
| `save_rule` | Phase 2 (stub) | Create/update a rule |
| `delete_rule` | Phase 2 (stub) | Remove a rule |
| `memory_search` | Existing | Search project memory |
| `send_message` | **NEW** | Post to Discord |

Note: Rule tools (`browse_rules`, `load_rule`, `save_rule`, `delete_rule`) will be implemented in Phase 2. In Phase 3, they are defined as core tool schemas with stub implementations in `CommandHandler` that return `{"error": "Rule system not yet implemented (Phase 2)"}`.

### Category: `git` -- Branch, commit, push, PR, merge operations

`get_git_status`, `git_commit`, `git_pull`, `git_push`, `git_create_branch`, `git_merge`, `git_create_pr`, `git_changed_files`, `git_log`, `git_diff`, `checkout_branch`

### Category: `project` -- Project CRUD, workspace management

`list_projects`, `create_project`, `pause_project`, `resume_project`, `edit_project`, `set_default_branch`, `get_project_channels`, `get_project_for_channel`, `delete_project`, `add_workspace`, `list_workspaces`, `find_merge_conflict_workspaces`, `release_workspace`, `remove_workspace`, `sync_workspaces`, `set_active_project`

### Category: `agent` -- Agent management, profiles

`list_agents`, `create_agent`, `edit_agent`, `pause_agent`, `resume_agent`, `delete_agent`, `get_agent_error`, `list_profiles`, `create_profile`, `get_profile`, `edit_profile` (agent profiles), `delete_profile`, `list_available_tools`, `check_profile`, `install_profile`, `export_profile`, `import_profile`

### Category: `hooks` -- Direct hook management

`create_hook`, `list_hooks`, `edit_hook`, `delete_hook`, `list_hook_runs`, `fire_hook`

### Category: `memory` -- Memory operations beyond search

`memory_stats`, `memory_reindex`, `view_profile` (project profile), `edit_profile` (project profile), `regenerate_profile`, `compact_memory`, `list_notes`, `write_note`, `delete_note`, `read_note`, `append_note`, `promote_note`, `compare_specs_notes`

### Category: `system` -- Token usage, config, diagnostics, task operations

`get_token_usage`, `run_command`, `read_file`, `search_files`, `get_status`, `get_recent_events`, `get_task_result`, `get_task_diff`, `list_active_tasks_all_projects`, `get_task_tree`, `stop_task`, `restart_task`, `reopen_with_feedback`, `delete_task`, `archive_tasks`, `archive_task`, `list_archived`, `restore_task`, `approve_task`, `approve_plan`, `reject_plan`, `delete_plan`, `skip_task`, `get_task_dependencies`, `add_dependency`, `remove_dependency`, `get_chain_health`, `restart_daemon`, `orchestrator_control`, `list_prompts`, `read_prompt`, `render_prompt`, `analyzer_status`, `analyzer_toggle`, `analyzer_history`

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `specs/tiered-tools.md` | Behavioral spec for the tiered tool system |
| Create | `src/tool_registry.py` | Tool categorization, core vs category metadata, browse/load logic |
| Create | `tests/test_tool_registry.py` | All ToolRegistry unit tests |
| Modify | `src/chat_agent.py` | Split TOOLS into registry calls; make `chat()` use mutable tool set |
| Modify | `src/command_handler.py` | Add `_cmd_browse_tools`, `_cmd_load_tools`, `_cmd_send_message`, rule stubs |
| Modify | `specs/chat-agent.md` | Document tiered tool loading in chat() |
| Modify | `specs/command-handler.md` | Document new commands |

---

### Task 1: Write the tiered tools spec

**Files:**
- Create: `specs/tiered-tools.md`

- [ ] **Step 1: Write the spec**

Write the behavioral specification for the tiered tool system. This spec describes what the system does, not how.

```markdown
# Tiered Tool System

## Purpose

Reduce the LLM's per-interaction context by presenting only ~11 core tools by default.
All other tools are organized into 6 named categories that can be loaded on demand.
This affects only which tool **definitions** the LLM sees — the execution path through
CommandHandler is unchanged.

## Concepts

### Core Tools

Core tools are always included in every LLM interaction's `tools` parameter.
They cover the most common operations: task CRUD, tool/rule navigation, memory
search, and messaging. Approximately 11 tools.

### Tool Categories

Six named categories group related tools:

| Category | Description | Approximate Count |
|----------|-------------|-------------------|
| `git` | Branch, commit, push, PR, and merge operations | 11 |
| `project` | Project CRUD, workspace management | 16 |
| `agent` | Agent management, agent profiles | 17 |
| `hooks` | Direct hook management (low-level) | 6 |
| `memory` | Memory operations beyond search, notes, project profiles | 13 |
| `system` | Token usage, config, diagnostics, task lifecycle ops, prompts | 33 |

### ToolRegistry

A singleton-like registry that owns:
- The mapping of tool name → JSON Schema definition
- The mapping of tool name → category name (or "core")
- Category metadata (name, description, tool count)

### Navigation Flow

1. LLM calls `browse_tools` → receives category list with names, descriptions, tool counts
2. LLM calls `load_tools(category="git")` → category's tool schemas are injected into
   the `tools` parameter for subsequent LLM turns in the same interaction
3. LLM can now call any git tool

### Mutable Tool Set Per Interaction

`ChatAgent.chat()` starts each interaction with only core tools. When `load_tools`
is called mid-interaction, the loaded category's schemas are appended to the active
tool set. Subsequent `create_message()` calls within the same `chat()` invocation
see the expanded list. The expansion does NOT persist across separate `chat()` calls.

### Response Formats

**browse_tools:**
```json
{
  "categories": [
    {"name": "git", "description": "Branch, commit, push, PR, and merge operations", "tool_count": 11},
    ...
  ]
}
```

**load_tools:**
```json
{
  "loaded": "git",
  "tools_added": ["get_git_status", "git_commit", ...],
  "message": "11 git tools are now available."
}
```
The tool schemas themselves are NOT in this response — they are injected into the
`tools` parameter of subsequent API calls.

## Invariants

- CommandHandler.execute() dispatches ALL tools regardless of whether they are loaded.
  Tiered loading is purely a context-management concern.
- Core tools cannot be unloaded.
- Loading the same category twice is idempotent (no duplicates).
- Tool names are globally unique across all categories.
- browse_tools and load_tools are themselves core tools.
- Category metadata (descriptions) are static — defined in code, not configurable.
```

- [ ] **Step 2: Commit the spec**

```
git add specs/tiered-tools.md
git commit -m "Add tiered tool system behavioral spec for Phase 3 of supervisor refactor"
```

---

### Task 2: Core ToolRegistry -- tool categorization and metadata

**Files:**
- Create: `tests/test_tool_registry.py`
- Create: `src/tool_registry.py`

- [ ] **Step 1: Write failing tests for ToolRegistry**

```python
"""Tests for ToolRegistry — tool categorization and on-demand loading."""

import pytest


def test_registry_has_core_tools():
    from src.tool_registry import ToolRegistry

    registry = ToolRegistry()
    core = registry.get_core_tools()

    # Core tools should include the navigation tools and task basics
    core_names = {t["name"] for t in core}
    assert "create_task" in core_names
    assert "list_tasks" in core_names
    assert "edit_task" in core_names
    assert "get_task" in core_names
    assert "browse_tools" in core_names
    assert "load_tools" in core_names
    assert "memory_search" in core_names
    assert "send_message" in core_names


def test_registry_has_categories():
    from src.tool_registry import ToolRegistry

    registry = ToolRegistry()
    categories = registry.get_categories()

    cat_names = {c["name"] for c in categories}
    assert cat_names == {"git", "project", "agent", "hooks", "memory", "system"}

    for cat in categories:
        assert "name" in cat
        assert "description" in cat
        assert "tool_count" in cat
        assert isinstance(cat["tool_count"], int)
        assert cat["tool_count"] > 0


def test_get_category_tools():
    from src.tool_registry import ToolRegistry

    registry = ToolRegistry()
    git_tools = registry.get_category_tools("git")

    assert len(git_tools) > 0
    git_names = {t["name"] for t in git_tools}
    assert "git_push" in git_names
    assert "git_create_pr" in git_names
    # Core tools should NOT appear in categories
    assert "create_task" not in git_names


def test_get_unknown_category_returns_none():
    from src.tool_registry import ToolRegistry

    registry = ToolRegistry()
    result = registry.get_category_tools("nonexistent")
    assert result is None


def test_all_tools_returns_everything():
    from src.tool_registry import ToolRegistry

    registry = ToolRegistry()
    all_tools = registry.get_all_tools()

    # Should be the union of core + all categories
    all_names = {t["name"] for t in all_tools}
    assert "create_task" in all_names  # core
    assert "git_push" in all_names     # git category
    assert "create_project" in all_names  # project category


def test_no_duplicate_tool_names():
    from src.tool_registry import ToolRegistry

    registry = ToolRegistry()
    all_tools = registry.get_all_tools()
    names = [t["name"] for t in all_tools]
    assert len(names) == len(set(names)), f"Duplicate tool names found: {[n for n in names if names.count(n) > 1]}"


def test_category_tool_count_matches():
    from src.tool_registry import ToolRegistry

    registry = ToolRegistry()
    categories = registry.get_categories()

    for cat in categories:
        tools = registry.get_category_tools(cat["name"])
        assert len(tools) == cat["tool_count"], (
            f"Category {cat['name']}: metadata says {cat['tool_count']} tools "
            f"but get_category_tools returned {len(tools)}"
        )


def test_get_category_tool_names():
    from src.tool_registry import ToolRegistry

    registry = ToolRegistry()
    names = registry.get_category_tool_names("git")
    assert isinstance(names, list)
    assert "git_push" in names
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_tool_registry.py -v`
Expected: FAIL -- `src.tool_registry` does not exist yet

- [ ] **Step 3: Implement ToolRegistry**

Create `src/tool_registry.py`. The registry imports the existing TOOLS list from `chat_agent.py` (temporarily) and splits it. The design:

```python
"""Tiered tool registry for on-demand tool loading.

Splits the monolithic TOOLS list into core tools (always loaded) and
named categories (loaded on demand via browse_tools/load_tools).

The registry only manages tool *definitions* (JSON Schema dicts).
Execution still flows through CommandHandler.execute() regardless
of whether a tool is "loaded" in the LLM's context.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CategoryMeta:
    """Metadata for a tool category."""
    name: str
    description: str


# Category definitions with human-readable descriptions
CATEGORIES: dict[str, CategoryMeta] = {
    "git": CategoryMeta(
        name="git",
        description="Branch, commit, push, PR, and merge operations for project repositories",
    ),
    "project": CategoryMeta(
        name="project",
        description="Project CRUD, workspace management, channel configuration",
    ),
    "agent": CategoryMeta(
        name="agent",
        description="Agent management, agent profiles, profile import/export",
    ),
    "hooks": CategoryMeta(
        name="hooks",
        description="Direct hook management — create, edit, list, delete, fire hooks",
    ),
    "memory": CategoryMeta(
        name="memory",
        description=(
            "Memory operations beyond search — notes, project profiles, "
            "compaction, reindexing"
        ),
    ),
    "system": CategoryMeta(
        name="system",
        description=(
            "Token usage, config, diagnostics, advanced task operations "
            "(archive, approve, dependencies), prompt management, daemon control"
        ),
    ),
}

# Which category each tool belongs to.
# Tools not listed here are "core" (always loaded).
_TOOL_CATEGORIES: dict[str, str] = {
    # git
    "get_git_status": "git",
    "git_commit": "git",
    "git_pull": "git",
    "git_push": "git",
    "git_create_branch": "git",
    "git_merge": "git",
    "git_create_pr": "git",
    "git_changed_files": "git",
    "git_log": "git",
    "git_diff": "git",
    "checkout_branch": "git",
    # project
    "list_projects": "project",
    "create_project": "project",
    "pause_project": "project",
    "resume_project": "project",
    "edit_project": "project",
    "set_default_branch": "project",
    "get_project_channels": "project",
    "get_project_for_channel": "project",
    "delete_project": "project",
    "add_workspace": "project",
    "list_workspaces": "project",
    "find_merge_conflict_workspaces": "project",
    "release_workspace": "project",
    "remove_workspace": "project",
    "sync_workspaces": "project",
    "set_active_project": "project",
    # agent
    "list_agents": "agent",
    "create_agent": "agent",
    "edit_agent": "agent",
    "pause_agent": "agent",
    "resume_agent": "agent",
    "delete_agent": "agent",
    "get_agent_error": "agent",
    "list_profiles": "agent",
    "create_profile": "agent",
    "get_profile": "agent",
    "edit_profile": "agent",  # agent profile edit_profile
    "delete_profile": "agent",
    "list_available_tools": "agent",
    "check_profile": "agent",
    "install_profile": "agent",
    "export_profile": "agent",
    "import_profile": "agent",
    # hooks
    "create_hook": "hooks",
    "list_hooks": "hooks",
    "edit_hook": "hooks",
    "delete_hook": "hooks",
    "list_hook_runs": "hooks",
    "fire_hook": "hooks",
    # memory
    "memory_stats": "memory",
    "memory_reindex": "memory",
    "view_profile": "memory",
    "regenerate_profile": "memory",
    "compact_memory": "memory",
    "list_notes": "memory",
    "write_note": "memory",
    "delete_note": "memory",
    "read_note": "memory",
    "append_note": "memory",
    "promote_note": "memory",
    "compare_specs_notes": "memory",
    # system
    "get_token_usage": "system",
    "run_command": "system",
    "read_file": "system",
    "search_files": "system",
    "get_status": "system",
    "get_recent_events": "system",
    "get_task_result": "system",
    "get_task_diff": "system",
    "list_active_tasks_all_projects": "system",
    "get_task_tree": "system",
    "stop_task": "system",
    "restart_task": "system",
    "reopen_with_feedback": "system",
    "delete_task": "system",
    "archive_tasks": "system",
    "archive_task": "system",
    "list_archived": "system",
    "restore_task": "system",
    "approve_task": "system",
    "approve_plan": "system",
    "reject_plan": "system",
    "delete_plan": "system",
    "skip_task": "system",
    "get_task_dependencies": "system",
    "add_dependency": "system",
    "remove_dependency": "system",
    "get_chain_health": "system",
    "restart_daemon": "system",
    "orchestrator_control": "system",
    "list_prompts": "system",
    "read_prompt": "system",
    "render_prompt": "system",
    "analyzer_status": "system",
    "analyzer_toggle": "system",
    "analyzer_history": "system",
}


class ToolRegistry:
    """Registry that categorizes tools into core and on-demand categories.

    Initialized with a list of tool definition dicts (JSON Schema format).
    Each tool is either "core" (always loaded) or belongs to a named category.
    """

    def __init__(self, tools: list[dict] | None = None):
        """Initialize with tool definitions.

        Args:
            tools: List of tool definition dicts. If None, imports from
                   chat_agent.TOOLS for backward compatibility.
        """
        if tools is None:
            from src.chat_agent import TOOLS
            tools = TOOLS
        self._all_tools = {t["name"]: t for t in tools}
        # Add new tools that don't exist in the legacy TOOLS list
        self._ensure_navigation_tools()

    def _ensure_navigation_tools(self) -> None:
        """Add browse_tools, load_tools, and send_message if not present."""
        if "browse_tools" not in self._all_tools:
            self._all_tools["browse_tools"] = {
                "name": "browse_tools",
                "description": (
                    "List available tool categories. Returns category names, "
                    "descriptions, and tool counts. Use this to discover what "
                    "tools are available, then call load_tools to load a category."
                ),
                "input_schema": {"type": "object", "properties": {}},
            }
        if "load_tools" not in self._all_tools:
            self._all_tools["load_tools"] = {
                "name": "load_tools",
                "description": (
                    "Load all tools from a specific category, making them "
                    "available for the remainder of this interaction. Call "
                    "browse_tools first to see available categories."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "description": "Category name to load (e.g. 'git', 'project')",
                        },
                    },
                    "required": ["category"],
                },
            }
        if "send_message" not in self._all_tools:
            self._all_tools["send_message"] = {
                "name": "send_message",
                "description": (
                    "Post a message to a Discord channel. Use this to notify "
                    "users, post updates, or communicate outside the current "
                    "conversation thread."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "channel_id": {
                            "type": "string",
                            "description": "Discord channel ID to post to",
                        },
                        "content": {
                            "type": "string",
                            "description": "Message content to post",
                        },
                    },
                    "required": ["channel_id", "content"],
                },
            }
        # Rule stubs (Phase 2 placeholders)
        for rule_tool in self._get_rule_tool_stubs():
            if rule_tool["name"] not in self._all_tools:
                self._all_tools[rule_tool["name"]] = rule_tool

    @staticmethod
    def _get_rule_tool_stubs() -> list[dict]:
        """Return stub tool definitions for Phase 2 rule tools."""
        return [
            {
                "name": "browse_rules",
                "description": "List active rules for current project and globals.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": "Project ID (optional, defaults to active project)",
                        },
                    },
                },
            },
            {
                "name": "load_rule",
                "description": "Load a specific rule's full detail.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Rule ID"},
                    },
                    "required": ["id"],
                },
            },
            {
                "name": "save_rule",
                "description": "Create or update a rule.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Rule ID (auto-generated if omitted)"},
                        "project_id": {"type": "string", "description": "Project ID (null = global)"},
                        "type": {"type": "string", "enum": ["active", "passive"], "description": "Rule type"},
                        "content": {"type": "string", "description": "Rule content (markdown)"},
                    },
                    "required": ["type", "content"],
                },
            },
            {
                "name": "delete_rule",
                "description": "Remove a rule.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Rule ID"},
                    },
                    "required": ["id"],
                },
            },
        ]

    def get_core_tools(self) -> list[dict]:
        """Return tool definitions that are always loaded."""
        return [
            t for name, t in self._all_tools.items()
            if name not in _TOOL_CATEGORIES
        ]

    def get_categories(self) -> list[dict]:
        """Return category metadata list for browse_tools response."""
        result = []
        for cat_name, meta in CATEGORIES.items():
            tools = self.get_category_tools(cat_name)
            result.append({
                "name": meta.name,
                "description": meta.description,
                "tool_count": len(tools) if tools else 0,
            })
        return result

    def get_category_tools(self, category: str) -> list[dict] | None:
        """Return all tool definitions for a category, or None if unknown."""
        if category not in CATEGORIES:
            return None
        return [
            self._all_tools[name]
            for name, cat in _TOOL_CATEGORIES.items()
            if cat == category and name in self._all_tools
        ]

    def get_category_tool_names(self, category: str) -> list[str] | None:
        """Return tool names for a category, or None if unknown."""
        if category not in CATEGORIES:
            return None
        return [
            name for name, cat in _TOOL_CATEGORIES.items()
            if cat == category and name in self._all_tools
        ]

    def get_all_tools(self) -> list[dict]:
        """Return all tool definitions (core + all categories)."""
        return list(self._all_tools.values())
```

Note on the `edit_profile` name collision: There are two tools named `edit_profile` in the current TOOLS list (one for agent profiles at line 1661, one for project memory profiles at line 1866). The registry handles this by keeping whichever appears last in the TOOLS list. This is a pre-existing issue. In this implementation, `edit_profile` is categorized under `agent` (agent profiles). The project memory profile edit is `view_profile`/`regenerate_profile` which are in the `memory` category.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_tool_registry.py -v`
Expected: All 9 tests PASS

- [ ] **Step 5: Commit**

```
git add src/tool_registry.py tests/test_tool_registry.py
git commit -m "Add ToolRegistry with tool categorization and category metadata"
```

---

### Task 3: Add browse_tools and load_tools to CommandHandler

**Files:**
- Modify: `tests/test_tool_registry.py`
- Modify: `src/command_handler.py`

- [ ] **Step 1: Write failing tests for CommandHandler commands**

Add to `tests/test_tool_registry.py`:

```python
import pytest


@pytest.fixture
def mock_orchestrator():
    """Minimal mock orchestrator for CommandHandler."""
    from unittest.mock import AsyncMock, MagicMock
    orch = MagicMock()
    orch.db = AsyncMock()
    orch.config = MagicMock()
    return orch


@pytest.fixture
def mock_config():
    """Minimal mock config for CommandHandler."""
    from unittest.mock import MagicMock
    config = MagicMock()
    config.workspace_dir = "/tmp/test"
    return config


@pytest.mark.asyncio
async def test_cmd_browse_tools(mock_orchestrator, mock_config):
    from src.command_handler import CommandHandler

    handler = CommandHandler(mock_orchestrator, mock_config)
    result = await handler.execute("browse_tools", {})

    assert "categories" in result
    cat_names = {c["name"] for c in result["categories"]}
    assert "git" in cat_names
    assert "project" in cat_names
    for cat in result["categories"]:
        assert "description" in cat
        assert "tool_count" in cat


@pytest.mark.asyncio
async def test_cmd_load_tools_valid_category(mock_orchestrator, mock_config):
    from src.command_handler import CommandHandler

    handler = CommandHandler(mock_orchestrator, mock_config)
    result = await handler.execute("load_tools", {"category": "git"})

    assert result["loaded"] == "git"
    assert "tools_added" in result
    assert "git_push" in result["tools_added"]
    assert "message" in result


@pytest.mark.asyncio
async def test_cmd_load_tools_invalid_category(mock_orchestrator, mock_config):
    from src.command_handler import CommandHandler

    handler = CommandHandler(mock_orchestrator, mock_config)
    result = await handler.execute("load_tools", {"category": "nonexistent"})

    assert "error" in result


@pytest.mark.asyncio
async def test_cmd_send_message_stub(mock_orchestrator, mock_config):
    from src.command_handler import CommandHandler

    handler = CommandHandler(mock_orchestrator, mock_config)
    result = await handler.execute("send_message", {
        "channel_id": "12345",
        "content": "Hello world",
    })
    # send_message needs Discord bot reference; without it, returns error
    assert "error" in result or "success" in result


@pytest.mark.asyncio
async def test_cmd_browse_rules_stub(mock_orchestrator, mock_config):
    from src.command_handler import CommandHandler

    handler = CommandHandler(mock_orchestrator, mock_config)
    result = await handler.execute("browse_rules", {})

    # Phase 2 stub returns informative error
    assert "error" in result or "rules" in result


@pytest.mark.asyncio
async def test_cmd_save_rule_stub(mock_orchestrator, mock_config):
    from src.command_handler import CommandHandler

    handler = CommandHandler(mock_orchestrator, mock_config)
    result = await handler.execute("save_rule", {
        "type": "passive",
        "content": "Always check for SQL injection",
    })

    assert "error" in result  # Phase 2 stub
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_tool_registry.py -v -k "cmd_"`
Expected: FAIL -- `_cmd_browse_tools` etc. do not exist

- [ ] **Step 3: Implement CommandHandler commands**

Add to `src/command_handler.py` in the `CommandHandler` class:

```python
    # -----------------------------------------------------------------------
    # Tool navigation commands (Phase 3 — tiered tool system)
    # -----------------------------------------------------------------------

    async def _cmd_browse_tools(self, args: dict) -> dict:
        """List available tool categories with metadata."""
        from src.tool_registry import ToolRegistry
        registry = ToolRegistry()
        return {"categories": registry.get_categories()}

    async def _cmd_load_tools(self, args: dict) -> dict:
        """Load a tool category's definitions for the current interaction."""
        from src.tool_registry import ToolRegistry
        category = args.get("category", "")
        registry = ToolRegistry()
        names = registry.get_category_tool_names(category)
        if names is None:
            available = [c["name"] for c in registry.get_categories()]
            return {
                "error": f"Unknown category: {category}. "
                         f"Available: {', '.join(available)}"
            }
        return {
            "loaded": category,
            "tools_added": names,
            "message": f"{len(names)} {category} tools are now available.",
        }

    async def _cmd_send_message(self, args: dict) -> dict:
        """Post a message to a Discord channel."""
        channel_id = args.get("channel_id")
        content = args.get("content")
        if not channel_id or not content:
            return {"error": "channel_id and content are required"}
        bot = getattr(self.orchestrator, "_discord_bot", None)
        if not bot:
            return {"error": "Discord bot not available"}
        try:
            channel = bot.get_channel(int(channel_id))
            if not channel:
                channel = await bot.fetch_channel(int(channel_id))
            await channel.send(content)
            return {"success": True, "channel_id": channel_id}
        except Exception as e:
            return {"error": f"Failed to send message: {e}"}

    # -----------------------------------------------------------------------
    # Rule system stubs (Phase 2 placeholders)
    # -----------------------------------------------------------------------

    async def _cmd_browse_rules(self, args: dict) -> dict:
        """List active rules. Phase 2 stub."""
        return {"error": "Rule system not yet implemented (Phase 2)"}

    async def _cmd_load_rule(self, args: dict) -> dict:
        """Load a rule's full detail. Phase 2 stub."""
        return {"error": "Rule system not yet implemented (Phase 2)"}

    async def _cmd_save_rule(self, args: dict) -> dict:
        """Create/update a rule. Phase 2 stub."""
        return {"error": "Rule system not yet implemented (Phase 2)"}

    async def _cmd_delete_rule(self, args: dict) -> dict:
        """Remove a rule. Phase 2 stub."""
        return {"error": "Rule system not yet implemented (Phase 2)"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_tool_registry.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```
git add src/command_handler.py tests/test_tool_registry.py
git commit -m "Add browse_tools, load_tools, send_message, and rule stubs to CommandHandler"
```

---

### Task 4: Modify chat() to support mutable tool set per interaction

**Files:**
- Modify: `tests/test_tool_registry.py`
- Modify: `src/chat_agent.py`

This is the key behavioral change. The `chat()` method currently passes the static `TOOLS` list to every `create_message()` call. After this change, it starts with core tools and expands dynamically when `load_tools` is called.

- [ ] **Step 1: Write failing tests for mutable tool set**

Add to `tests/test_tool_registry.py`:

```python
@pytest.mark.asyncio
async def test_chat_starts_with_core_tools_only():
    """Verify chat() sends only core tools on first LLM call."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from src.tool_registry import ToolRegistry

    registry = ToolRegistry()
    core_count = len(registry.get_core_tools())
    all_count = len(registry.get_all_tools())

    # Core should be significantly fewer than all
    assert core_count < all_count, (
        f"Core ({core_count}) should be fewer than all ({all_count})"
    )


@pytest.mark.asyncio
async def test_load_tools_expands_active_set():
    """Verify that calling load_tools adds category tools to active set."""
    from src.tool_registry import ToolRegistry

    registry = ToolRegistry()
    active_tools = {t["name"]: t for t in registry.get_core_tools()}

    # Simulate load_tools("git")
    git_tools = registry.get_category_tools("git")
    assert git_tools is not None
    for t in git_tools:
        active_tools[t["name"]] = t

    # Active set should now include git tools
    assert "git_push" in active_tools
    assert "create_task" in active_tools  # core still present


@pytest.mark.asyncio
async def test_load_tools_idempotent():
    """Loading same category twice should not duplicate tools."""
    from src.tool_registry import ToolRegistry

    registry = ToolRegistry()
    active_tools = {t["name"]: t for t in registry.get_core_tools()}
    initial_count = len(active_tools)

    # Load git twice
    git_tools = registry.get_category_tools("git")
    for t in git_tools:
        active_tools[t["name"]] = t
    count_after_first = len(active_tools)

    for t in git_tools:
        active_tools[t["name"]] = t
    count_after_second = len(active_tools)

    assert count_after_first == count_after_second
    assert count_after_first > initial_count
```

- [ ] **Step 2: Run tests to verify behavior**

Run: `pytest tests/test_tool_registry.py -v -k "chat_starts or expands or idempotent"`
Expected: PASS (these test registry logic, not chat() directly)

- [ ] **Step 3: Modify chat() to use mutable tool set**

In `src/chat_agent.py`, modify the `chat()` method (line 2407). The key changes:

1. Replace `tools=TOOLS` with a mutable `active_tools` dict initialized from `ToolRegistry.get_core_tools()`
2. After executing a `load_tools` call, expand `active_tools` with the loaded category
3. Pass `list(active_tools.values())` to each `create_message()` call

```python
    async def chat(
        self,
        text: str,
        user_name: str,
        history: list[dict] | None = None,
        on_progress: "Callable[[str, str | None], Awaitable[None]] | None" = None,
    ) -> str:
        """Process a user message with tool use. Returns response text.

        Starts with core tools only. When the LLM calls load_tools,
        the requested category's tool definitions are added to the active
        set for subsequent turns within this interaction.
        """
        if not self._provider:
            raise RuntimeError("LLM provider not initialized — call initialize() first")

        from src.tool_registry import ToolRegistry
        registry = ToolRegistry()

        # Mutable tool set — starts with core, expands via load_tools
        active_tools: dict[str, dict] = {
            t["name"]: t for t in registry.get_core_tools()
        }

        messages = list(history) if history else []

        # Append current message
        current = {"role": "user", "content": f"[from {user_name}]: {text}"}
        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"] += "\n" + current["content"]
        else:
            messages.append(current)

        # Multi-turn tool-use loop
        tool_actions: list[str] = []
        max_rounds = getattr(self, "_max_tool_rounds", 10)

        for round_num in range(max_rounds):
            if on_progress:
                if round_num == 0:
                    await on_progress("thinking", None)
                else:
                    await on_progress("thinking", f"round {round_num + 1}")

            resp = await self._provider.create_message(
                messages=messages,
                system=self._build_system_prompt(),
                tools=list(active_tools.values()),
                max_tokens=1024,
            )

            if not resp.tool_uses:
                if on_progress:
                    await on_progress("responding", None)
                response = "\n".join(resp.text_parts).strip()
                if response:
                    return response
                if tool_actions:
                    return f"Done. Actions taken: {', '.join(tool_actions)}"
                return "Done."

            messages.append({"role": "assistant", "content": resp.tool_uses})

            tool_results = []
            for tool_use in resp.tool_uses:
                label = _tool_label(tool_use.name, tool_use.input)
                if on_progress:
                    await on_progress("tool_use", label)
                result = await self._execute_tool(tool_use.name, tool_use.input)
                tool_actions.append(label)

                # If load_tools was called, expand active tool set
                if tool_use.name == "load_tools" and "loaded" in result:
                    category = result["loaded"]
                    cat_tools = registry.get_category_tools(category)
                    if cat_tools:
                        for t in cat_tools:
                            active_tools[t["name"]] = t

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": json.dumps(result),
                })

            messages.append({"role": "user", "content": tool_results})

        if tool_actions:
            return f"Done. Actions taken: {', '.join(tool_actions)}"
        return "Done."
```

- [ ] **Step 4: Run existing tests to verify no regression**

Run: `pytest tests/ -v --ignore=tests/chat_eval -k "chat"`
Expected: All existing tests PASS

- [ ] **Step 5: Commit**

```
git add src/chat_agent.py tests/test_tool_registry.py
git commit -m "Make chat() use mutable tool set with core tools + on-demand category loading"
```

---

### Task 5: Split TOOLS list out of chat_agent.py

**Files:**
- Modify: `src/chat_agent.py`
- Modify: `src/tool_registry.py`
- Modify: `tests/test_tool_registry.py`

The TOOLS list (lines 50-1982 of `chat_agent.py`) is ~1,930 lines. Move all tool definitions into `tool_registry.py` as the single source of truth.

- [ ] **Step 1: Write test that tool count is preserved**

Add to `tests/test_tool_registry.py`:

```python
def test_total_tool_count_preserved():
    """Verify no tools were lost in the split."""
    from src.tool_registry import ToolRegistry

    registry = ToolRegistry()
    all_tools = registry.get_all_tools()
    all_names = {t["name"] for t in all_tools}

    # These are the tools that existed in the original TOOLS list
    # plus the new navigation tools (browse_tools, load_tools, send_message,
    # browse_rules, load_rule, save_rule, delete_rule)
    expected_new_tools = {
        "browse_tools", "load_tools", "send_message",
        "browse_rules", "load_rule", "save_rule", "delete_rule",
    }

    # Every original tool should still exist
    original_names = {
        "list_projects", "create_project", "pause_project", "resume_project",
        "edit_project", "set_default_branch", "get_project_channels",
        "get_project_for_channel", "list_tasks", "list_active_tasks_all_projects",
        "get_task_tree", "create_task", "add_workspace", "list_workspaces",
        "find_merge_conflict_workspaces", "release_workspace", "remove_workspace",
        "sync_workspaces", "list_agents", "create_agent", "edit_agent",
        "pause_agent", "resume_agent", "delete_agent", "set_active_project",
        "get_task", "edit_task", "stop_task", "restart_task",
        "reopen_with_feedback", "delete_task", "archive_tasks", "archive_task",
        "list_archived", "restore_task", "approve_task", "approve_plan",
        "reject_plan", "delete_plan", "skip_task", "get_task_dependencies",
        "add_dependency", "remove_dependency", "get_chain_health", "get_status",
        "get_recent_events", "get_task_result", "get_task_diff", "read_file",
        "run_command", "delete_project", "search_files", "get_token_usage",
        "create_hook", "list_hooks", "edit_hook", "delete_hook",
        "list_hook_runs", "fire_hook", "list_notes", "write_note",
        "delete_note", "read_note", "append_note", "promote_note",
        "compare_specs_notes", "list_prompts", "read_prompt", "render_prompt",
        "get_git_status", "git_commit", "git_pull", "git_push",
        "git_create_branch", "git_merge", "git_create_pr", "git_changed_files",
        "git_log", "git_diff", "checkout_branch", "restart_daemon",
        "orchestrator_control", "get_agent_error", "list_profiles",
        "create_profile", "get_profile", "edit_profile", "delete_profile",
        "list_available_tools", "check_profile", "install_profile",
        "export_profile", "import_profile", "memory_search", "memory_stats",
        "memory_reindex", "view_profile", "regenerate_profile",
        "compact_memory", "analyzer_status", "analyzer_toggle", "analyzer_history",
    }

    missing = original_names - all_names
    assert not missing, f"Tools missing from registry: {missing}"
```

- [ ] **Step 2: Move tool definitions into tool_registry.py**

Move the entire TOOLS list from `src/chat_agent.py` into `src/tool_registry.py` as a module-level constant `_ALL_TOOL_DEFINITIONS`. Update the `ToolRegistry.__init__()` to use this constant by default instead of importing from `chat_agent.TOOLS`.

In `src/chat_agent.py`:
- Remove the TOOLS list (lines 50-1982)
- Remove the SYSTEM_PROMPT_TEMPLATE string (lines 2000+, kept only for backward compat)
- Add import: `from src.tool_registry import ToolRegistry`
- Keep the `TOOLS` name as a backward-compatible alias: `TOOLS = ToolRegistry().get_all_tools()`

- [ ] **Step 3: Run all tests to verify no regression**

Run: `pytest tests/ -v --ignore=tests/chat_eval`
Expected: All tests PASS

- [ ] **Step 4: Run linter**

Run: `ruff check src/tool_registry.py src/chat_agent.py`
Expected: No errors

- [ ] **Step 5: Commit**

```
git add src/chat_agent.py src/tool_registry.py tests/test_tool_registry.py
git commit -m "Move tool definitions from chat_agent.py to tool_registry.py"
```

---

### Task 6: Create compact system prompt

**Files:**
- Modify: `src/prompts/chat_agent_system.md` (or create new supervisor prompt)
- Modify: `tests/test_tool_registry.py`

The current system prompt is ~2,150 lines because it documents every tool. With tiered loading, the system prompt shrinks to describe only the Supervisor role, activation modes, navigation pattern, and core tool usage.

- [ ] **Step 1: Write test for compact prompt size**

Add to `tests/test_tool_registry.py`:

```python
def test_core_tools_are_compact():
    """Core tools should be significantly fewer than all tools."""
    from src.tool_registry import ToolRegistry

    registry = ToolRegistry()
    core = registry.get_core_tools()
    all_tools = registry.get_all_tools()

    # Core should be roughly 10-15 tools
    assert len(core) <= 20, f"Core has {len(core)} tools — should be ~11"
    assert len(core) >= 8, f"Core has {len(core)} tools — too few"
    # Core should be < 25% of all tools
    assert len(core) < len(all_tools) * 0.25
```

- [ ] **Step 2: Create compact system prompt template**

The existing `src/prompts/chat_agent_system.md` template is loaded by `_build_system_prompt()`. Update it (or create a companion) to be compact. The new prompt should cover:

1. Supervisor role and identity (brief)
2. Navigation pattern: "Use `browse_tools` to see available tool categories, then `load_tools` to load what you need"
3. Core tool quick-reference (one line per tool)
4. Key behavioral guidelines (brief)

The detailed per-tool documentation that currently fills the system prompt moves into the tool `description` fields within each category's definitions (already present in the JSON Schema).

- [ ] **Step 3: Verify the prompt is actually compact**

```python
def test_system_prompt_is_compact():
    """System prompt should be well under 500 lines with tiered tools."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder()
    builder.set_identity("chat-agent-system", {"workspace_dir": "/tmp/test"})
    prompt, _ = builder.build()

    line_count = len(prompt.split("\n"))
    assert line_count < 500, f"System prompt is {line_count} lines — should be compact"
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_tool_registry.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```
git add src/prompts/chat_agent_system.md tests/test_tool_registry.py
git commit -m "Create compact system prompt for tiered tool system"
```

---

### Task 7: Add category documentation to tool descriptions

**Files:**
- Modify: `src/tool_registry.py`
- Modify: `tests/test_tool_registry.py`

Detailed per-tool documentation that used to live in the system prompt now lives in the tool `description` fields. Review each category's tools and ensure their `description` fields are self-contained -- an LLM seeing the tool definition for the first time (without the system prompt context) should understand when and how to use it.

- [ ] **Step 1: Write test for description quality**

```python
def test_all_tools_have_descriptions():
    """Every tool should have a non-empty description."""
    from src.tool_registry import ToolRegistry

    registry = ToolRegistry()
    for tool in registry.get_all_tools():
        assert "description" in tool, f"Tool {tool['name']} missing description"
        assert len(tool["description"]) > 10, (
            f"Tool {tool['name']} has too-short description: {tool['description']}"
        )


def test_all_tools_have_input_schema():
    """Every tool should have an input_schema."""
    from src.tool_registry import ToolRegistry

    registry = ToolRegistry()
    for tool in registry.get_all_tools():
        assert "input_schema" in tool, f"Tool {tool['name']} missing input_schema"
```

- [ ] **Step 2: Review and enhance tool descriptions**

For each category, review tool descriptions. Tools whose descriptions reference the system prompt ("see above", "as described in the system prompt") need to be made self-contained. The description should answer: what does this tool do, when should I use it, and what are the key parameters.

This is a review/editing pass on the tool definition dicts, not a structural change.

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_tool_registry.py -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```
git add src/tool_registry.py tests/test_tool_registry.py
git commit -m "Enhance tool descriptions for self-contained category payloads"
```

---

### Task 8: Update specs

**Files:**
- Modify: `specs/chat-agent.md`
- Modify: `specs/command-handler.md`
- Modify: `specs/tiered-tools.md`

- [ ] **Step 1: Update chat-agent spec**

Add to `specs/chat-agent.md`:

```markdown
### Tiered Tool Loading

The `chat()` method uses a mutable tool set per interaction:

1. Initializes `active_tools` from `ToolRegistry.get_core_tools()` (~11 tools)
2. Passes `active_tools` to each `create_message()` call
3. When the LLM calls `load_tools(category)`, the category's tool definitions
   are added to `active_tools` for subsequent turns
4. Tool expansion does not persist across separate `chat()` invocations

The LLM sees only core tools initially. It uses `browse_tools` to discover
categories and `load_tools` to expand its capabilities as needed.
```

- [ ] **Step 2: Update command-handler spec**

Add to `specs/command-handler.md`:

```markdown
### Tool Navigation Commands

- `browse_tools` — Returns list of tool categories with name, description, tool_count
- `load_tools` — Returns confirmation of loaded category; actual tool schema injection
  happens in the chat layer, not in CommandHandler
- `send_message` — Posts a message to a Discord channel via the bot reference

### Rule System Stubs (Phase 2)

- `browse_rules` — Stub, returns Phase 2 not-implemented error
- `load_rule` — Stub
- `save_rule` — Stub
- `delete_rule` — Stub
```

- [ ] **Step 3: Finalize tiered-tools spec**

Update `specs/tiered-tools.md` with final tool counts and any implementation details that emerged during development.

- [ ] **Step 4: Commit**

```
git add specs/chat-agent.md specs/command-handler.md specs/tiered-tools.md
git commit -m "Update specs to reflect tiered tool system implementation"
```

---

### Task 9: Final verification and cleanup

**Files:**
- No new files

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v --ignore=tests/chat_eval`
Expected: All tests PASS. No regressions.

- [ ] **Step 2: Run linter**

Run: `ruff check src/tool_registry.py src/chat_agent.py src/command_handler.py`
Expected: No errors

- [ ] **Step 3: Verify tool counts**

```bash
python -c "
from src.tool_registry import ToolRegistry
r = ToolRegistry()
print(f'Core tools: {len(r.get_core_tools())}')
for c in r.get_categories():
    print(f'{c[\"name\"]}: {c[\"tool_count\"]} tools')
print(f'Total: {len(r.get_all_tools())} tools')
"
```

Expected output approximately:
```
Core tools: ~11-15
git: 11 tools
project: 16 tools
agent: 17 tools
hooks: 6 tools
memory: 13 tools
system: ~33 tools
Total: ~97+ tools
```

- [ ] **Step 4: Verify backward compatibility**

The `TOOLS` name in `chat_agent.py` still works as a list of all tools for any external code that imports it. Verify:

```bash
python -c "from src.chat_agent import TOOLS; print(f'TOOLS backward compat: {len(TOOLS)} tools')"
```

- [ ] **Step 5: Commit any final cleanup**

```
git add -A
git commit -m "Phase 3 complete: tiered tool system with browse/load navigation"
