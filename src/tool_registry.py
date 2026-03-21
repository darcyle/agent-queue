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
        description=(
            "Branch, commit, push, PR, and merge operations "
            "for project repositories"
        ),
    ),
    "project": CategoryMeta(
        name="project",
        description=(
            "Project CRUD, workspace management, channel configuration"
        ),
    ),
    "agent": CategoryMeta(
        name="agent",
        description=(
            "Agent management, agent profiles, profile import/export"
        ),
    ),
    "hooks": CategoryMeta(
        name="hooks",
        description=(
            "Direct hook management -- create, edit, list, delete, "
            "fire hooks"
        ),
    ),
    "memory": CategoryMeta(
        name="memory",
        description=(
            "Memory operations beyond search -- notes, project profiles, "
            "compaction, reindexing"
        ),
    ),
    "system": CategoryMeta(
        name="system",
        description=(
            "Token usage, config, diagnostics, advanced task operations "
            "(archive, approve, dependencies), prompt management, "
            "daemon control"
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
    "edit_profile": "agent",
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
        self._all_tools: dict[str, dict] = {t["name"]: t for t in tools}
        # Add new tools that don't exist in the legacy TOOLS list
        self._ensure_navigation_tools()

    def _ensure_navigation_tools(self) -> None:
        """Add browse_tools, load_tools, send_message, and rule stubs
        if not already present."""
        if "browse_tools" not in self._all_tools:
            self._all_tools["browse_tools"] = {
                "name": "browse_tools",
                "description": (
                    "List available tool categories. Returns category "
                    "names, descriptions, and tool counts. Use this to "
                    "discover what tools are available, then call "
                    "load_tools to load a category."
                ),
                "input_schema": {"type": "object", "properties": {}},
            }
        if "load_tools" not in self._all_tools:
            self._all_tools["load_tools"] = {
                "name": "load_tools",
                "description": (
                    "Load all tools from a specific category, making "
                    "them available for the remainder of this "
                    "interaction. Call browse_tools first to see "
                    "available categories."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "description": (
                                "Category name to load "
                                "(e.g. 'git', 'project')"
                            ),
                        },
                    },
                    "required": ["category"],
                },
            }
        if "send_message" not in self._all_tools:
            self._all_tools["send_message"] = {
                "name": "send_message",
                "description": (
                    "Post a message to a Discord channel. Use this to "
                    "notify users, post updates, or communicate outside "
                    "the current conversation thread."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "channel_id": {
                            "type": "string",
                            "description": (
                                "Discord channel ID to post to"
                            ),
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
                "description": (
                    "List active rules for current project and globals."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": (
                                "Project ID (optional, defaults to "
                                "active project)"
                            ),
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
                        "id": {
                            "type": "string",
                            "description": "Rule ID",
                        },
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
                        "id": {
                            "type": "string",
                            "description": (
                                "Rule ID (auto-generated if omitted)"
                            ),
                        },
                        "project_id": {
                            "type": "string",
                            "description": (
                                "Project ID (null = global)"
                            ),
                        },
                        "type": {
                            "type": "string",
                            "enum": ["active", "passive"],
                            "description": "Rule type",
                        },
                        "content": {
                            "type": "string",
                            "description": "Rule content (markdown)",
                        },
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
                        "id": {
                            "type": "string",
                            "description": "Rule ID",
                        },
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
        """Return all tool definitions for a category, or None if
        unknown."""
        if category not in CATEGORIES:
            return None
        return [
            self._all_tools[name]
            for name, cat in _TOOL_CATEGORIES.items()
            if cat == category and name in self._all_tools
        ]

    def get_category_tool_names(
        self, category: str
    ) -> list[str] | None:
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
