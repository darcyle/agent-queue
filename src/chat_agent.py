"""LLM-powered chat interface for AgentQueue.

This module defines two things that together turn a language model into a
system operator:

1. **TOOLS** -- a list of tool definitions (JSON Schema) that the LLM can
   call.  Each tool maps 1:1 to a ``CommandHandler._cmd_*`` method; the
   schemas are what the LLM "sees" as its API surface.  Adding a new tool
   here automatically exposes the corresponding command to chat users.

2. **ChatAgent** -- the multi-turn conversation loop.  The ``chat()`` method
   sends the user message (plus history) to the LLM, checks if the response
   contains tool-use blocks, executes those tools via ``CommandHandler``,
   feeds the results back, and repeats until the LLM produces a final text
   response.

Design boundaries:
    - History management (compaction, summarization, per-channel storage)
      lives in the Discord bot layer, not here.  ChatAgent is stateless
      between calls -- the caller passes history in and gets text out.
    - SYSTEM_PROMPT_TEMPLATE shapes the LLM's persona and operating rules.
      It is NOT a code-worker prompt; it instructs the LLM to act as a
      dispatcher that plans and delegates to agents via the tool interface.
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from src.chat_providers import ChatProvider, LoggedChatProvider, create_chat_provider
from src.command_handler import CommandHandler
from src.config import AppConfig
from src.llm_logger import LLMLogger
from src.orchestrator import Orchestrator
from src.prompt_registry import registry as _prompt_registry


# ---------------------------------------------------------------------------
# Tool definitions -- the LLM's interface to the system.
#
# Each entry describes one operation the LLM can invoke during a conversation.
# The names match CommandHandler._cmd_* methods (e.g. "create_task" calls
# _cmd_create_task).  The input_schema tells the LLM what arguments are
# available; the description tells it *when* to use the tool.
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "list_projects",
        "description": "List all projects in the system.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_project",
        "description": (
            "Create a new project.  Optionally auto-create a dedicated Discord "
            "channel for the project.  When "
            "auto_create_channels is omitted the behaviour is determined by "
            "the per_project_channels.auto_create config flag."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name"},
                "credit_weight": {
                    "type": "number",
                    "description": "Scheduling weight (default 1.0)",
                    "default": 1.0,
                },
                "max_concurrent_agents": {
                    "type": "integer",
                    "description": "Max agents working on this project simultaneously",
                    "default": 2,
                },
                "repo_url": {
                    "type": "string",
                    "description": "Git repository URL for this project (optional)",
                },
                "default_branch": {
                    "type": "string",
                    "description": "Default branch name (default: main)",
                    "default": "main",
                },
                "auto_create_channels": {
                    "type": "boolean",
                    "description": (
                        "If true, auto-create dedicated Discord channels for "
                        "this project after creation.  If false, skip channel "
                        "creation.  When omitted, falls back to the global "
                        "per_project_channels.auto_create config setting."
                    ),
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "pause_project",
        "description": "Pause a project so no new tasks are scheduled.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID to pause"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "resume_project",
        "description": "Resume a paused project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID to resume"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "edit_project",
        "description": (
            "Edit a project's properties: name, credit_weight, max_concurrent_agents, "
            "budget_limit, discord_channel_id, default_profile_id, or repo_default_branch. "
            "Use this to rename projects, adjust scheduling weight, set token budgets, "
            "link Discord channels, set a default agent profile, or change the default git branch."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "name": {"type": "string", "description": "New project name (optional)"},
                "credit_weight": {"type": "number", "description": "New scheduling weight (optional)"},
                "max_concurrent_agents": {"type": "integer", "description": "New max concurrent agents (optional)"},
                "budget_limit": {"type": ["integer", "null"], "description": "Token budget limit (optional, null to clear)"},
                "discord_channel_id": {"type": ["string", "null"], "description": "Discord channel ID to link (optional, null to unlink)"},
                "default_profile_id": {"type": ["string", "null"], "description": "Default agent profile ID for tasks in this project (optional, null to clear)"},
                "repo_default_branch": {"type": "string", "description": "Default git branch for the project (e.g. main, dev, master)"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "set_default_branch",
        "description": (
            "Set (or change) the default git branch for a project. If the branch "
            "does not exist on the remote yet, it will be created automatically "
            "from the current default branch. Use this when a project should branch "
            "off of and merge into a branch other than 'main' (e.g. 'dev')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "branch": {"type": "string", "description": "Branch name to use as the new default (e.g. dev, main, master)"},
            },
            "required": ["project_id", "branch"],
        },
    },
    # Note: set_project_channel and set_control_interface have been removed.
    # Use edit_project with discord_channel_id instead.
    {
        "name": "get_project_channels",
        "description": "Get the Discord channel ID configured for a project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "get_project_for_channel",
        "description": (
            "Reverse lookup: given a Discord channel ID, find which project it belongs to. "
            "Returns the project ID, or null if no project is linked."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "Discord channel ID to look up",
                },
            },
            "required": ["channel_id"],
        },
    },
    {
        "name": "list_tasks",
        "description": (
            "List tasks, optionally filtered by project or status. "
            "IMPORTANT: By default, completed/failed/blocked tasks are HIDDEN "
            "and only active tasks are returned. When presenting results from "
            "the default filter, say 'N active tasks' (not just 'N tasks'). "
            "Use show_all=true or include_completed=true to include finished "
            "tasks. Use completed_only=true to see only finished tasks. "
            "An explicit status filter overrides all convenience flags. "
            "Supports three display modes: 'flat' (default list), 'tree' "
            "(hierarchical view with subtasks), and 'compact' (root tasks "
            "with progress bars). Tree and compact modes require project_id. "
            "Use show_dependencies=true to annotate each task with its upstream "
            "depends_on and downstream blocks relationships — useful for "
            "understanding blocking chains."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Filter by project ID (optional, but required for tree/compact modes)",
                },
                "status": {
                    "type": "string",
                    "description": (
                        "Filter by exact status: DEFINED, READY, IN_PROGRESS, "
                        "COMPLETED, etc. When provided, show_all, "
                        "include_completed, and completed_only are ignored."
                    ),
                },
                "show_all": {
                    "type": "boolean",
                    "description": (
                        "When true, return ALL tasks regardless of status "
                        "(active + completed + failed + blocked). "
                        "Default false (only active tasks are shown)."
                    ),
                },
                "include_completed": {
                    "type": "boolean",
                    "description": (
                        "When true, return all tasks including completed/failed/"
                        "blocked. Alias for show_all. Default false."
                    ),
                },
                "completed_only": {
                    "type": "boolean",
                    "description": (
                        "When true, return ONLY completed/failed/blocked tasks. "
                        "Default false."
                    ),
                },
                "display_mode": {
                    "type": "string",
                    "enum": ["flat", "tree", "compact"],
                    "description": (
                        "How to format the task list. 'flat' (default) returns "
                        "a JSON array of task objects. 'tree' returns a "
                        "hierarchical view using box-drawing characters showing "
                        "parent/subtask relationships — ideal for plans with "
                        "subtasks. 'compact' returns each root task with a "
                        "progress bar summarizing subtask completion. When "
                        "display_mode is 'tree' or 'compact', the response "
                        "includes a 'display' field with pre-formatted text."
                    ),
                },
                "show_dependencies": {
                    "type": "boolean",
                    "description": (
                        "When true, each task in the result includes "
                        "'depends_on' (list of upstream tasks with id and "
                        "status) and 'blocks' (list of downstream task IDs). "
                        "Also adds a 'dependency_display' field with "
                        "pre-formatted text showing dependency relationships. "
                        "Use this when the user asks about task dependencies, "
                        "blocking chains, or why a task is waiting. "
                        "Default false."
                    ),
                },
            },
        },
    },
    {
        "name": "list_active_tasks_all_projects",
        "description": (
            "List active tasks across ALL projects, grouped by project. "
            "Returns only non-terminal tasks (excludes COMPLETED, FAILED, "
            "BLOCKED) by default. Use this when the user wants a cross-project "
            "overview of everything that is queued, in-progress, or actionable. "
            "Response includes 'by_project' (grouped), 'tasks' (flat list), "
            "'total', 'project_count', and 'hidden_completed' (number of "
            "terminal tasks not shown). When presenting results, say "
            "'N active tasks across M projects'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "include_completed": {
                    "type": "boolean",
                    "description": (
                        "When true, include completed/failed/blocked tasks too. "
                        "Default false (active tasks only)."
                    ),
                },
            },
        },
    },
    {
        "name": "get_task_tree",
        "description": (
            "Get the subtask hierarchy for a specific parent task, rendered as "
            "a tree with box-drawing characters. Returns a 'display' field with "
            "pre-formatted text showing the full parent->child hierarchy, status "
            "emojis, and a progress summary. Use this when the user asks about "
            "subtasks of a specific task or wants to inspect a plan's structure. "
            "For a project-wide tree view, use list_tasks with "
            "display_mode='tree' instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The root/parent task ID whose subtree to display",
                },
                "compact": {
                    "type": "boolean",
                    "description": (
                        "When true, show only the root task with a subtask "
                        "count and progress bar instead of the full expanded "
                        "tree. Default false (full tree)."
                    ),
                },
                "max_depth": {
                    "type": "integer",
                    "description": (
                        "Maximum nesting depth to render (default 4). "
                        "Deeper subtasks are collapsed into a summary."
                    ),
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "create_task",
        "description": "Create a new task. If no project_id is given, it inherits from the active project; errors if none is set.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID (optional — inferred from active project)",
                },
                "title": {"type": "string", "description": "Short task title"},
                "description": {
                    "type": "string",
                    "description": "Detailed task description for the agent",
                },
                "priority": {
                    "type": "integer",
                    "description": "Priority (lower = higher priority, default 100)",
                    "default": 100,
                },
                "requires_approval": {
                    "type": "boolean",
                    "description": "If true, agent work creates a PR instead of auto-merging. Human must approve/merge the PR.",
                    "default": False,
                },
                "task_type": {
                    "type": "string",
                    "enum": ["feature", "bugfix", "refactor", "test", "docs", "chore", "research", "plan"],
                    "description": "Categorize the task type for display and filtering (optional)",
                },
                "profile_id": {
                    "type": "string",
                    "description": "Agent profile ID to configure the agent with specific tools/capabilities (optional)",
                },
                "preferred_workspace_id": {
                    "type": "string",
                    "description": (
                        "Workspace ID to prefer when assigning this task to an agent. "
                        "Use this when the task must run in a specific workspace (e.g. "
                        "one that contains a merge conflict). Get the ID from "
                        "find_merge_conflict_workspaces or list_workspaces."
                    ),
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "add_workspace",
        "description": (
            "Add a workspace directory for a project. Source types: 'clone' (auto-clones "
            "from the project's repo_url), 'link' (link an existing directory on disk). "
            "Workspaces are project-scoped and dynamically acquired by agents when assigned tasks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to add the workspace to",
                },
                "source": {
                    "type": "string",
                    "enum": ["clone", "link"],
                    "description": "How to set up the workspace: clone (from project repo_url), link (existing dir)",
                },
                "path": {
                    "type": "string",
                    "description": "Directory path (required for link, auto-generated for clone)",
                },
                "name": {
                    "type": "string",
                    "description": "Workspace name (optional)",
                },
            },
            "required": ["project_id", "source"],
        },
    },
    {
        "name": "list_workspaces",
        "description": "List workspaces, optionally filtered by project. Shows lock status (which agent/task holds each workspace).",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Filter by project ID (optional)",
                },
            },
        },
    },
    {
        "name": "find_merge_conflict_workspaces",
        "description": (
            "Scan project workspaces to find which ones have branches with merge conflicts "
            "against the default branch (main). Returns workspace IDs, conflicting branches, "
            "and file details. Use this BEFORE creating a merge-conflict resolution task so you "
            "can pass the correct preferred_workspace_id to create_task — ensuring the agent "
            "gets assigned the workspace that actually contains the conflict."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to scan workspaces for (optional if active project is set)",
                },
            },
        },
    },
    {
        "name": "release_workspace",
        "description": "Force-release a stuck workspace lock. Use when a workspace is locked by a dead agent or stale task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace ID to release"},
            },
            "required": ["workspace_id"],
        },
    },
    {
        "name": "remove_workspace",
        "description": (
            "Delete a workspace from a project. The workspace must not be locked by an agent. "
            "Accepts a workspace ID or name. Does not delete files on disk — only removes the "
            "workspace record from the database."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "Workspace ID or name to delete",
                },
                "project_id": {
                    "type": "string",
                    "description": "Project ID (used for name lookup; optional if active project is set)",
                },
            },
            "required": ["workspace_id"],
        },
    },
    {
        "name": "sync_workspaces",
        "description": (
            "Synchronize all project workspaces to the latest main branch. "
            "Fetches latest changes, pushes any unpushed local commits, and rebases "
            "feature branches onto the updated main. Locked workspaces (in use by agents) "
            "are skipped. Workspaces with unresolvable conflicts are reported for manual "
            "intervention. Returns a per-workspace status report."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to sync workspaces for (optional if active project is set)",
                },
                "skip_locked": {
                    "type": "boolean",
                    "description": "Skip workspaces locked by an agent (default: true). Set to false to sync all workspaces.",
                },
            },
        },
    },
    {
        "name": "list_agents",
        "description": "List all configured agents and their current state.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_agent",
        "description": (
            "Register a new agent. If no name is provided, a creative unique "
            "name is auto-generated. Agents start in IDLE state and immediately "
            "begin receiving tasks. Agents dynamically acquire workspace locks "
            "from available project workspaces when assigned tasks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Agent display name. Leave empty to auto-generate a creative name.",
                },
                "agent_type": {
                    "type": "string",
                    "description": "Agent type (claude, codex, cursor, aider)",
                    "default": "claude",
                },
            },
        },
    },
    {
        "name": "edit_agent",
        "description": (
            "Edit an agent's properties: name or agent_type. "
            "Use this to rename agents or change their type."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID"},
                "name": {"type": "string", "description": "New display name (optional)"},
                "agent_type": {
                    "type": "string",
                    "description": "New agent type: claude, codex, cursor, aider (optional)",
                },
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "pause_agent",
        "description": (
            "Pause an agent so it stops receiving new tasks. If the agent is "
            "currently BUSY, it will finish its current task then stay paused."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID to pause"},
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "resume_agent",
        "description": "Resume a paused agent so it can receive tasks again.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID to resume"},
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "delete_agent",
        "description": "Delete an agent and all its workspace mappings. Cannot delete a BUSY agent — stop its task first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID to delete"},
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "set_active_project",
        "description": "Set or clear the active project. When set, all commands default to this project without needing to specify project_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to set as active, or empty/null to clear",
                },
            },
        },
    },
    {
        "name": "get_task",
        "description": "Get full details of a specific task including its description.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "edit_task",
        "description": (
            "Edit a task's properties: title, description, priority, task_type, "
            "status, max_retries, verification_type, or profile_id. Use this to rename tasks, "
            "change priority, override status (admin), assign a profile, or adjust retry/verification settings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "title": {"type": "string", "description": "New title (optional)"},
                "description": {"type": "string", "description": "New description (optional)"},
                "priority": {"type": "integer", "description": "New priority (optional)"},
                "task_type": {
                    "type": ["string", "null"],
                    "enum": ["feature", "bugfix", "refactor", "test", "docs", "chore", "research", "plan", None],
                    "description": "New task type (optional, set to null to clear)",
                },
                "status": {
                    "type": "string",
                    "enum": ["DEFINED", "READY", "IN_PROGRESS", "COMPLETED", "FAILED", "BLOCKED"],
                    "description": "New status — admin override, bypasses state machine (optional)",
                },
                "max_retries": {"type": "integer", "description": "Max retry attempts (optional)"},
                "verification_type": {
                    "type": "string",
                    "enum": ["auto_test", "qa_agent", "human"],
                    "description": "How to verify task output (optional)",
                },
                "profile_id": {
                    "type": ["string", "null"],
                    "description": "Agent profile ID (optional, set to null to clear)",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "stop_task",
        "description": "Stop a task that is currently in progress. Cancels the agent working on it and marks the task as BLOCKED.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to stop"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "restart_task",
        "description": "Reset a completed, failed, or blocked task back to READY so it gets picked up by an agent again.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to restart"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "reopen_with_feedback",
        "description": (
            "Reopen a completed or failed task with feedback. Use this when a "
            "task needs rework — the feedback is appended to the task "
            "description and stored as a structured context entry so the agent "
            "sees it on re-execution. The task is reset to READY, retry count "
            "is cleared, and the PR URL is removed so a fresh PR can be created."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to reopen"},
                "feedback": {
                    "type": "string",
                    "description": (
                        "Feedback explaining what went wrong or what needs "
                        "to be fixed (appended to task description and stored "
                        "as a task context entry)"
                    ),
                },
            },
            "required": ["task_id", "feedback"],
        },
    },
    {
        "name": "delete_task",
        "description": "Delete a task. Cannot delete a task that is currently in progress.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to delete"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "archive_tasks",
        "description": (
            "Archive completed tasks to clear them from active task lists. "
            "Tasks are moved to the archived_tasks DB table (viewable with "
            "list_archived, restorable with restore_task) and a markdown "
            "reference note is written to the project workspace. Optionally "
            "also archive FAILED and BLOCKED tasks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to scope archiving to (optional — omit for all projects)",
                },
                "include_failed": {
                    "type": "boolean",
                    "description": (
                        "If true, also archive FAILED and BLOCKED tasks. "
                        "Default false."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "archive_task",
        "description": (
            "Archive a single task by ID. The task must be in a terminal status "
            "(COMPLETED, FAILED, or BLOCKED). Archived tasks are removed from "
            "active views but preserved for reference."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to archive"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "list_archived",
        "description": (
            "List archived tasks. Shows tasks that were previously completed and "
            "archived. Use this when the user wants to see old/finished tasks "
            "that have been cleared from the active view."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Filter by project (optional)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of tasks to return (default 50)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "restore_task",
        "description": (
            "Restore an archived task back into the active task list. The task "
            "is restored with DEFINED status so it re-enters the normal "
            "orchestrator lifecycle."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Archived task ID to restore",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "approve_task",
        "description": "Manually approve and complete a task that is AWAITING_APPROVAL. Use for tasks on LINK repos that don't have GitHub PRs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to approve"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "approve_plan",
        "description": "Approve a plan for a task in AWAITING_PLAN_APPROVAL status. Creates subtasks from the stored plan and marks the task completed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID whose plan to approve"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "reject_plan",
        "description": "Reject a plan for a task in AWAITING_PLAN_APPROVAL status with feedback. Reopens the task so the agent can revise the plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID whose plan to reject"},
                "feedback": {"type": "string", "description": "Feedback describing what changes are needed in the plan"},
            },
            "required": ["task_id", "feedback"],
        },
    },
    {
        "name": "delete_plan",
        "description": "Delete a plan for a task in AWAITING_PLAN_APPROVAL status. Completes the task without creating any subtasks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID whose plan to delete"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "skip_task",
        "description": "Skip a BLOCKED or FAILED task to unblock its dependency chain. Marks the task as COMPLETED so downstream dependents can proceed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to skip"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "get_task_dependencies",
        "description": (
            "Get the full dependency graph for a specific task: what it depends "
            "on (upstream) and what it blocks (downstream). Each entry includes "
            "the task's id, title, and current status. Use this when the user "
            "asks why a task is blocked, what depends on a task, or wants to "
            "understand the dependency chain for a specific task. "
            "Example: 'Task X is blocked because it depends on Y which is "
            "still IN_PROGRESS.'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task ID to inspect dependencies for",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "add_dependency",
        "description": (
            "Add a dependency between two tasks: task_id will depend on "
            "depends_on (i.e. task_id cannot start until depends_on is "
            "completed). Validates that both tasks exist and performs cycle "
            "detection to prevent circular dependency chains. Use this when "
            "the user wants to link tasks so one must finish before another "
            "can begin."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task that should wait (downstream task)",
                },
                "depends_on": {
                    "type": "string",
                    "description": "The task that must complete first (upstream task)",
                },
            },
            "required": ["task_id", "depends_on"],
        },
    },
    {
        "name": "remove_dependency",
        "description": (
            "Remove a dependency between two tasks: task_id will no longer "
            "depend on depends_on. Use this when the user wants to unlink "
            "tasks or remove a blocking relationship."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The downstream task to unlink",
                },
                "depends_on": {
                    "type": "string",
                    "description": "The upstream task to remove as a dependency",
                },
            },
            "required": ["task_id", "depends_on"],
        },
    },
    {
        "name": "get_chain_health",
        "description": "Check dependency chain health. Shows downstream tasks stuck because of blocked tasks. Pass task_id for a specific task, or project_id for all stuck chains in a project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "(Optional) Check a specific blocked task"},
                "project_id": {"type": "string", "description": "(Optional) Check all blocked chains in a project"},
            },
        },
    },
    {
        "name": "get_status",
        "description": "Get a high-level overview of the system: projects, agents, tasks counts.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_recent_events",
        "description": "Get recent system events (task completions, failures, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of events to return (default 10)",
                    "default": 10,
                },
            },
        },
    },
    {
        "name": "get_task_result",
        "description": "Retrieve a task's output: summary, files changed, error message, tokens used. Use this when the user asks about what a task did or its results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "get_task_diff",
        "description": "Show the git diff for a task's branch against the base branch. Use when the user asks to see code changes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file's contents from a workspace. Path can be absolute or relative to the workspaces root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (absolute or relative to workspaces root)"},
                "max_lines": {
                    "type": "integer",
                    "description": "Max lines to return (default 200)",
                    "default": 200,
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "run_command",
        "description": "Execute a shell command in a workspace directory. Use when the user asks to run tests, check status, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "working_dir": {
                    "type": "string",
                    "description": "Working directory (absolute path or project ID)",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30, max 120)",
                    "default": 30,
                },
            },
            "required": ["command", "working_dir"],
        },
    },
    {
        "name": "delete_project",
        "description": "Delete a project and all associated data (tasks, repos, results, token ledger). Cannot delete if any task is IN_PROGRESS. In-memory channel caches are automatically purged. Optionally archive the project's Discord channels.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID to delete"},
                "archive_channels": {
                    "type": "boolean",
                    "description": "If true, archive the project's Discord channels (rename + set read-only) instead of leaving them as-is. Default: false.",
                    "default": False,
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "search_files",
        "description": "Search for files or content in a workspace. Use 'grep' mode to search file contents, 'find' mode to search filenames.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Search pattern (regex for grep, glob for find)"},
                "path": {"type": "string", "description": "Directory to search in (absolute or relative to workspaces root)"},
                "mode": {
                    "type": "string",
                    "enum": ["grep", "find"],
                    "description": "Search mode: 'grep' for content, 'find' for filenames",
                    "default": "grep",
                },
            },
            "required": ["pattern", "path"],
        },
    },
    {
        "name": "get_token_usage",
        "description": "Get token usage breakdown by project or task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID (optional)"},
                "task_id": {"type": "string", "description": "Task ID (optional)"},
            },
        },
    },
    {
        "name": "create_hook",
        "description": (
            "Create a hook that automatically triggers actions. Hooks gather context "
            "(shell commands, file reads, HTTP checks, DB queries) and send a prompt "
            "to an LLM that has access to all system tools (create_task, list_tasks, etc.). "
            "Trigger types: 'periodic' (interval_seconds), 'event' (event_type). "
            "Context step types: 'shell' (command, timeout, skip_llm_if_exit_zero), "
            "'read_file' (path, max_lines), 'http' (url, skip_llm_if_status_ok), "
            "'db_query' (query name, params), 'git_diff' (workspace, base_branch). "
            "Use {{step_0}}, {{step_1}}, {{event}} in prompt_template."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "name": {"type": "string", "description": "Hook name (used as ID slug)"},
                "trigger": {
                    "type": "object",
                    "description": "Trigger config: {type: 'periodic', interval_seconds: N} or {type: 'event', event_type: '...'}",
                },
                "context_steps": {
                    "type": "array",
                    "description": "Array of context step configs",
                    "items": {"type": "object"},
                },
                "prompt_template": {
                    "type": "string",
                    "description": "Prompt template with {{step_N}} and {{event}} placeholders",
                },
                "cooldown_seconds": {
                    "type": "integer",
                    "description": "Min seconds between runs (default 3600)",
                    "default": 3600,
                },
                "llm_config": {
                    "type": "object",
                    "description": "Optional LLM config override: {provider, model, base_url}",
                },
            },
            "required": ["project_id", "name", "trigger", "prompt_template"],
        },
    },
    {
        "name": "list_hooks",
        "description": "List hooks, optionally filtered by project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Filter by project ID (optional)"},
            },
        },
    },
    {
        "name": "edit_hook",
        "description": (
            "Edit a hook's configuration: name, enabled, trigger, context_steps, "
            "prompt_template, cooldown_seconds, llm_config, or max_tokens_per_run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hook_id": {"type": "string", "description": "Hook ID"},
                "name": {"type": "string", "description": "New hook name (optional)"},
                "enabled": {"type": "boolean", "description": "Enable/disable the hook"},
                "trigger": {"type": "object", "description": "New trigger config"},
                "context_steps": {"type": "array", "description": "New context steps", "items": {"type": "object"}},
                "prompt_template": {"type": "string", "description": "New prompt template"},
                "cooldown_seconds": {"type": "integer", "description": "New cooldown"},
                "llm_config": {"type": "object", "description": "New LLM config override"},
                "max_tokens_per_run": {"type": ["integer", "null"], "description": "Max tokens per run (null to clear)"},
            },
            "required": ["hook_id"],
        },
    },
    {
        "name": "delete_hook",
        "description": "Delete a hook and its run history.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hook_id": {"type": "string", "description": "Hook ID to delete"},
            },
            "required": ["hook_id"],
        },
    },
    {
        "name": "list_hook_runs",
        "description": "Show recent execution history for a hook.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hook_id": {"type": "string", "description": "Hook ID"},
                "limit": {"type": "integer", "description": "Number of runs to show (default 10)", "default": 10},
            },
            "required": ["hook_id"],
        },
    },
    {
        "name": "fire_hook",
        "description": "Manually trigger a hook immediately, ignoring cooldown.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hook_id": {"type": "string", "description": "Hook ID to fire"},
            },
            "required": ["hook_id"],
        },
    },
    {
        "name": "list_notes",
        "description": "List all notes for a project. Returns name (filename), title, and size for each note. Use the 'name' field when calling read_note or delete_note.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "write_note",
        "description": "Create or overwrite a project note. Use to create new notes or to save edits (read with read_file first, modify, then write back).",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "title": {"type": "string", "description": "Note title (used as filename)"},
                "content": {"type": "string", "description": "Full markdown content"},
            },
            "required": ["project_id", "title", "content"],
        },
    },
    {
        "name": "delete_note",
        "description": (
            "Delete a project note by title. If the user provides the note name "
            "directly, call this tool immediately — no need to list notes first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "title": {
                    "type": "string",
                    "description": (
                        "Note filename from list_notes 'name' field (e.g. 'my-note.md'), "
                        "or the note title"
                    ),
                },
            },
            "required": ["project_id", "title"],
        },
    },
    {
        "name": "read_note",
        "description": (
            "Read a note's full contents. Returns the markdown content, path, and size. "
            "Use the 'name' field from list_notes (e.g. 'my-note.md') as the title parameter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "title": {
                    "type": "string",
                    "description": (
                        "Note filename from list_notes 'name' field (e.g. 'my-note.md'), "
                        "or the note title"
                    ),
                },
            },
            "required": ["project_id", "title"],
        },
    },
    {
        "name": "append_note",
        "description": (
            "Append content to an existing note, or create a new note if it doesn't exist. "
            "Ideal for stream-of-consciousness input — appends with a blank line separator "
            "without needing to read and rewrite the entire note."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "title": {"type": "string", "description": "Note title (used as filename)"},
                "content": {
                    "type": "string",
                    "description": "Content to append (or initial content if creating)",
                },
            },
            "required": ["project_id", "title", "content"],
        },
    },
    {
        "name": "promote_note",
        "description": (
            "Explicitly incorporate a note's content into the project profile. "
            "Uses an LLM to integrate the note's knowledge into the living profile "
            "rather than simply appending. Use when a note contains important knowledge "
            "that should be part of the project's core understanding."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "title": {
                    "type": "string",
                    "description": (
                        "Note filename from list_notes 'name' field (e.g. 'my-note.md'), "
                        "or the note title"
                    ),
                },
            },
            "required": ["project_id", "title"],
        },
    },
    {
        "name": "compare_specs_notes",
        "description": (
            "List all spec files and note files for a project side by side. "
            "Returns raw file listings (names, titles, sizes) for gap analysis. "
            "Use this when the user asks to compare specs with notes or find "
            "what's missing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "specs_path": {
                    "type": "string",
                    "description": "Override path to specs directory (optional)",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "list_prompts",
        "description": (
            "List all prompt templates for a project. Returns name, description, "
            "category, tags, and variable schemas for each template. "
            "Optionally filter by category (system, task, hooks, custom) or tag."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "category": {
                    "type": "string",
                    "description": (
                        "Filter by category: system, task, hooks, or custom (optional)"
                    ),
                },
                "tag": {
                    "type": "string",
                    "description": "Filter by tag (optional)",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "read_prompt",
        "description": (
            "Read a prompt template's full content and metadata. Returns the "
            "template body, variable definitions, tags, and category. "
            "Use the 'name' field from list_prompts as the name parameter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "name": {
                    "type": "string",
                    "description": (
                        "Template name from list_prompts (e.g. 'plan-generation'), "
                        "or the filename (e.g. 'plan-generation.md')"
                    ),
                },
            },
            "required": ["project_id", "name"],
        },
    },
    {
        "name": "render_prompt",
        "description": (
            "Render a prompt template with variable substitution. Replaces "
            "{{variable}} placeholders with provided values. Returns the "
            "fully rendered prompt text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "name": {
                    "type": "string",
                    "description": "Template name to render",
                },
                "variables": {
                    "type": "object",
                    "description": (
                        "Key-value pairs for template variables "
                        "(e.g. {\"task_title\": \"Fix login bug\"})"
                    ),
                },
            },
            "required": ["project_id", "name"],
        },
    },
    {
        "name": "get_git_status",
        "description": (
            "Get the git status of a project's repository. Shows current branch, "
            "working tree status, and recent commits. Reports status for all workspaces "
            "registered to the project, or falls back to the project workspace path. "
            "Operates on the active project's repository."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID (optional — inferred from active project)",
                },
            },
        },
    },
    {
        "name": "git_commit",
        "description": (
            "Stage all changes and create a commit in a repository. "
            "Operates on the active project's repository. "
            "Use the workspace parameter to target a specific workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Commit message"},
                "project_id": {"type": "string", "description": "Project ID (optional — inferred from active project)"},
                "workspace": {"type": "string", "description": "Workspace ID or name to operate on (optional — defaults to first workspace)"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "git_pull",
        "description": (
            "Pull (fetch + merge) a branch from the remote origin. Defaults to the current branch if not specified. "
            "Operates on the active project's repository. "
            "Use the workspace parameter to target a specific workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID (optional — inferred from active project)"},
                "branch": {"type": "string", "description": "Branch name to pull (defaults to current branch)"},
                "workspace": {"type": "string", "description": "Workspace ID or name to operate on (optional — defaults to first workspace)"},
            },
        },
    },
    {
        "name": "git_push",
        "description": (
            "Push a branch to the remote origin. Defaults to the current branch if not specified. "
            "Operates on the active project's repository. "
            "Use the workspace parameter to target a specific workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID (optional — inferred from active project)"},
                "branch": {"type": "string", "description": "Branch name to push (defaults to current branch)"},
                "workspace": {"type": "string", "description": "Workspace ID or name to operate on (optional — defaults to first workspace)"},
            },
        },
    },
    {
        "name": "git_create_branch",
        "description": (
            "Create and switch to a new git branch in a repository. "
            "Operates on the active project's repository. "
            "Use the workspace parameter to target a specific workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch_name": {"type": "string", "description": "Name for the new branch"},
                "project_id": {"type": "string", "description": "Project ID (optional — inferred from active project)"},
                "workspace": {"type": "string", "description": "Workspace ID or name to operate on (optional — defaults to first workspace)"},
            },
            "required": ["branch_name"],
        },
    },
    {
        "name": "git_merge",
        "description": (
            "Merge a branch into the default branch. Returns whether the merge "
            "succeeded or had conflicts (conflicts are automatically aborted). "
            "Operates on the active project's repository. "
            "Use the workspace parameter to target a specific workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch_name": {"type": "string", "description": "Branch to merge"},
                "project_id": {"type": "string", "description": "Project ID (optional — inferred from active project)"},
                "default_branch": {
                    "type": "string",
                    "description": "Target branch to merge into (defaults to repo's default branch)",
                },
                "workspace": {"type": "string", "description": "Workspace ID or name to operate on (optional — defaults to first workspace)"},
            },
            "required": ["branch_name"],
        },
    },
    {
        "name": "git_create_pr",
        "description": (
            "Create a GitHub pull request using the gh CLI. Requires gh to be authenticated. "
            "Operates on the active project's repository. "
            "Use the workspace parameter to target a specific workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "PR title"},
                "body": {"type": "string", "description": "PR description body (optional)", "default": ""},
                "branch": {"type": "string", "description": "Head branch (defaults to current branch)"},
                "base": {"type": "string", "description": "Base branch (defaults to repo's default branch)"},
                "project_id": {"type": "string", "description": "Project ID (optional — inferred from active project)"},
                "workspace": {"type": "string", "description": "Workspace ID or name to operate on (optional — defaults to first workspace)"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "git_changed_files",
        "description": (
            "List files changed compared to a base branch. Lighter than a full diff. "
            "Operates on the active project's repository. "
            "Use the workspace parameter to target a specific workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID (optional — inferred from active project)"},
                "base_branch": {
                    "type": "string",
                    "description": "Branch to compare against (defaults to repo's default branch)",
                },
                "workspace": {"type": "string", "description": "Workspace ID or name to operate on (optional — defaults to first workspace)"},
            },
        },
    },
    {
        "name": "git_log",
        "description": (
            "Show recent git commits for a project's repository. "
            "Operates on the active project's repository. "
            "Use the workspace parameter to target a specific workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID (optional — inferred from active project)"},
                "count": {"type": "integer", "description": "Number of commits to show (default 10)", "default": 10},
                "workspace": {"type": "string", "description": "Workspace ID or name to operate on (optional — defaults to first workspace)"},
            },
        },
    },
    {
        "name": "git_diff",
        "description": (
            "Show the git diff for a project's repository. Without base_branch shows working tree changes; "
            "with base_branch shows diff against that branch. "
            "Operates on the active project's repository. "
            "Use the workspace parameter to target a specific workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID (optional — inferred from active project)"},
                "base_branch": {"type": "string", "description": "Base branch to diff against (optional — shows working tree diff if omitted)"},
                "workspace": {"type": "string", "description": "Workspace ID or name to operate on (optional — defaults to first workspace)"},
            },
        },
    },
    {
        "name": "checkout_branch",
        "description": (
            "Switch to an existing git branch in a project's repository. "
            "Operates on the active project's repository. "
            "Use the workspace parameter to target a specific workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID (optional — inferred from active project)"},
                "branch_name": {"type": "string", "description": "Branch name to check out"},
                "workspace": {"type": "string", "description": "Workspace ID or name to operate on (optional — defaults to first workspace)"},
            },
            "required": ["branch_name"],
        },
    },
    {
        "name": "restart_daemon",
        "description": "Restart the agent-queue daemon process. The bot will disconnect briefly and reconnect. A reason is required.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the restart is being requested",
                },
            },
            "required": ["reason"],
        },
    },
    {
        "name": "orchestrator_control",
        "description": "Pause, resume, or check the status of the orchestrator (task scheduler). When paused, no new tasks will be assigned to agents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["pause", "resume", "status"],
                    "description": "Action to perform: pause, resume, or status",
                },
            },
            "required": ["action"],
        },
    },
    # Note: set_task_status has been removed. Use edit_task with status instead.
    {
        "name": "get_agent_error",
        "description": "Get the last error recorded for a task, including error classification, suggested fix, and agent summary.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to inspect"},
            },
            "required": ["task_id"],
        },
    },
    # --- Agent Profile tools ---
    {
        "name": "list_profiles",
        "description": "List all agent profiles. Profiles are capability bundles that configure agents with specific tools, MCP servers, and system prompts.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_profile",
        "description": (
            "Create a new agent profile. Profiles configure agents with specific tools, "
            "MCP servers, model overrides, and system prompt additions. Assign profiles "
            "to tasks (profile_id) or set as project defaults (default_profile_id)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Profile slug ID (e.g. 'reviewer', 'web-developer')",
                },
                "name": {
                    "type": "string",
                    "description": "Human-readable display name",
                },
                "description": {
                    "type": "string",
                    "description": "What this profile is for (optional)",
                },
                "model": {
                    "type": "string",
                    "description": "Model override (optional, empty = use default)",
                },
                "permission_mode": {
                    "type": "string",
                    "description": "Permission mode override (optional)",
                },
                "allowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tool whitelist (e.g. ['Read', 'Glob', 'Grep', 'Bash'])",
                },
                "mcp_servers": {
                    "type": "object",
                    "description": "MCP server configurations (name -> {command, args})",
                },
                "system_prompt_suffix": {
                    "type": "string",
                    "description": "Text appended to the agent's system prompt (optional)",
                },
            },
            "required": ["id", "name"],
        },
    },
    {
        "name": "get_profile",
        "description": "Get details of a specific agent profile.",
        "input_schema": {
            "type": "object",
            "properties": {
                "profile_id": {"type": "string", "description": "Profile ID to look up"},
            },
            "required": ["profile_id"],
        },
    },
    {
        "name": "edit_profile",
        "description": "Edit an existing agent profile's properties.",
        "input_schema": {
            "type": "object",
            "properties": {
                "profile_id": {"type": "string", "description": "Profile ID to edit"},
                "name": {"type": "string", "description": "New display name (optional)"},
                "description": {"type": "string", "description": "New description (optional)"},
                "model": {"type": "string", "description": "New model override (optional)"},
                "permission_mode": {"type": "string", "description": "New permission mode (optional)"},
                "allowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "New tool whitelist (optional)",
                },
                "mcp_servers": {
                    "type": "object",
                    "description": "New MCP server configurations (optional)",
                },
                "system_prompt_suffix": {
                    "type": "string",
                    "description": "New system prompt suffix (optional)",
                },
            },
            "required": ["profile_id"],
        },
    },
    {
        "name": "delete_profile",
        "description": "Delete an agent profile. Any tasks or projects referencing it will have their profile cleared.",
        "input_schema": {
            "type": "object",
            "properties": {
                "profile_id": {"type": "string", "description": "Profile ID to delete"},
            },
            "required": ["profile_id"],
        },
    },
    {
        "name": "list_available_tools",
        "description": (
            "Discover available Claude Code tools and well-known MCP servers "
            "for use in agent profiles."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_profile",
        "description": (
            "Validate an agent profile's install dependencies. Checks that "
            "required commands, npm packages, and pip packages are available."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "profile_id": {"type": "string", "description": "Profile ID to check"},
            },
            "required": ["profile_id"],
        },
    },
    {
        "name": "install_profile",
        "description": (
            "Install missing npm/pip dependencies for a profile's install manifest. "
            "System commands that are missing are reported for manual installation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "profile_id": {"type": "string", "description": "Profile ID to install deps for"},
            },
            "required": ["profile_id"],
        },
    },
    {
        "name": "export_profile",
        "description": (
            "Export an agent profile as YAML. Optionally create a public GitHub gist."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "profile_id": {"type": "string", "description": "Profile ID to export"},
                "create_gist": {
                    "type": "boolean",
                    "description": "If true, create a public GitHub gist and return the URL",
                },
            },
            "required": ["profile_id"],
        },
    },
    {
        "name": "import_profile",
        "description": (
            "Import an agent profile from YAML text or a GitHub gist URL. "
            "If the profile has an install manifest, dependencies are auto-installed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "YAML text or gist URL to import from",
                },
                "id": {
                    "type": "string",
                    "description": "Override the profile ID (optional)",
                },
                "name": {
                    "type": "string",
                    "description": "Override the profile name (optional)",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "If true, overwrite existing profile with same ID",
                },
            },
            "required": ["source"],
        },
    },
    # --- Memory tools ---
    {
        "name": "memory_search",
        "description": (
            "Search project memory for relevant context. Returns semantically "
            "similar past task results, notes, and knowledge-base entries. "
            "Use this when the user asks about past work, wants to find related "
            "context, or says 'search memory', 'what do we know about', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to search memory for",
                },
                "query": {
                    "type": "string",
                    "description": "Semantic search query",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results to return (default 10)",
                    "default": 10,
                },
            },
            "required": ["project_id", "query"],
        },
    },
    {
        "name": "memory_stats",
        "description": (
            "Get memory index statistics for a project. Shows whether memory "
            "is enabled, the collection name, embedding provider, and "
            "auto-recall/auto-remember settings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to get memory stats for",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "memory_reindex",
        "description": (
            "Force a full reindex of a project's memory. Re-scans all markdown "
            "files in memory/ and notes/ directories, re-embeds changed content, "
            "and updates the vector index. Use when memory seems stale or after "
            "bulk file changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to reindex memory for",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "view_profile",
        "description": (
            "View the project profile — a synthesized understanding of the project's "
            "architecture, conventions, key decisions, common patterns, and pitfalls. "
            "The profile evolves automatically as tasks complete."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to view profile for",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "edit_profile",
        "description": (
            "Replace the project profile with new content. Use this to manually "
            "correct or enhance the project's synthesized understanding."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to edit profile for",
                },
                "content": {
                    "type": "string",
                    "description": "New profile content (markdown)",
                },
            },
            "required": ["project_id", "content"],
        },
    },
    {
        "name": "regenerate_profile",
        "description": (
            "Force LLM regeneration of the project profile from the full task "
            "history. Use this when the profile has drifted or you want a fresh "
            "synthesis of everything the project has learned."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to regenerate profile for",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "compact_memory",
        "description": (
            "Trigger memory compaction for a project. Groups task memories "
            "by age: recent (kept as-is), medium (LLM-summarized into weekly "
            "digests), old (deleted after digesting). Returns stats on tasks "
            "inspected, digests created, and files removed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to compact memory for",
                },
            },
            "required": ["project_id"],
        },
    },
]

# ---------------------------------------------------------------------------
# System prompt -- shapes the LLM's behavior and persona.
#
# This tells the LLM it is a *dispatcher*, not a code worker.  It should
# understand user intent, translate it into tool calls, and present results
# conversationally.  The prompt also documents every tool's purpose so the
# LLM knows which one to reach for (LLMs often ignore JSON schema details
# but read prose descriptions carefully).
#
# The template has one placeholder: {workspace_dir}, filled at runtime.
# An ACTIVE PROJECT addendum is appended dynamically when the user is
# chatting in a project-specific Discord channel.
# ---------------------------------------------------------------------------
# The chat agent system prompt now lives in src/prompts/chat_agent_system.md.
# SYSTEM_PROMPT_TEMPLATE is kept for backward compatibility but _build_system_prompt()
# uses the registry.
SYSTEM_PROMPT_TEMPLATE = """\
You are AgentQueue, a Discord bot that manages an AI agent task queue. You help \
users manage projects, tasks, and agents through natural conversation.

System info:
- Workspaces root: {workspace_dir}
- Each project gets its own folder under the workspaces root (e.g., {workspace_dir}/my-project/)

You can directly (using your tools):
- Create and manage projects (groups of related tasks)
- Create, view, edit, delete, and restart tasks
- View task hierarchies with `get_task_tree` or `list_tasks` with display_mode='tree'
- Inspect task dependency graphs with `get_task_dependencies` (upstream depends_on + downstream blocks)
- Add dependencies between tasks with `add_dependency` (with automatic cycle detection)
- Remove dependencies with `remove_dependency`
- List tasks with dependency annotations using `list_tasks` with show_dependencies=true
- Create tasks in the active project without explicitly specifying a project_id
- Register and list agents
- Manage project workspaces with `add_workspace`, `list_workspaces`, `remove_workspace`, and `release_workspace`
- Monitor agent status, task progress, and recent events
- Pause/resume projects
- Retrieve task results (summary, files changed, errors, tokens) with `get_task_result`
- Show git diffs for completed tasks with `get_task_diff`
- Check the git status of a project's repos with `get_git_status`
- Git operations: `git_commit`, `git_push`, `git_create_branch`, `git_merge`, \
`checkout_branch`, `git_create_pr`, `git_changed_files`, `git_log`, `git_diff`
- All git commands automatically infer the repository from the active project — \
you do NOT need to specify project_id when an active project is set
- Read files from workspaces with `read_file`
- Run shell commands in workspaces with `run_command`
- Search file contents or filenames with `search_files`
- Get token usage breakdowns with `get_token_usage`
- Delete entire projects (cascading) with `delete_project`
- Create, read, edit, and delete project notes with `list_notes`, `write_note`, `delete_note`, and `read_file`
- Browse prompt templates with `list_prompts`, `read_prompt`, and `render_prompt`
- Create and manage hooks for automated self-improvement with `create_hook`, `list_hooks`, \
`edit_hook`, `delete_hook`, `list_hook_runs`, and `fire_hook`
- Restart the daemon with `restart_daemon`
- Pause, resume, or check the orchestrator (task scheduler) with `orchestrator_control`
- Override a task's status with `edit_task` (set the `status` field to bypass the state machine)
- Inspect the last error for a task with `get_agent_error` (shows error classification and suggested fix)
- Configure per-project Discord channels with `edit_project` (discord_channel_id), `get_project_channels`, and `get_project_for_channel`
- Search project memory with `memory_search` (semantic search over past task results, notes, knowledge)
- View memory index stats with `memory_stats`
- Force a full memory reindex with `memory_reindex`
- View project profile with `view_profile` (synthesized project understanding that evolves with tasks)
- Edit project profile with `edit_profile` (manually correct or enhance project understanding)
- Regenerate project profile with `regenerate_profile` (force LLM regeneration from full task history)
- Compact memory with `compact_memory` (age-based compaction: digests medium-age tasks, removes old files)

Workspace management — use `add_workspace` to add workspace directories to projects:
- **clone**: Auto-clones from the project's `repo_url`. Path is auto-generated under the workspace root.
- **link**: Link an existing directory on disk. Agents work directly in that directory, \
preserving the existing environment (.env, venv, node_modules, etc.). Use when \
the user says to "link", "connect", "use", or "point to" an existing directory/repo.
- Each project can have multiple workspaces for parallel agent execution.
- Agents dynamically acquire a workspace lock when assigned a task and release it on completion.
- Use `list_workspaces` to see workspace status and lock information.
- Use `remove_workspace` to delete a workspace from a project (must not be locked). Only removes the DB record, not files on disk.
- Use `release_workspace` to force-release a stuck lock (e.g., dead agent, stale task).
- Use `sync_workspaces` to pull and push all workspaces to the latest main branch. This fetches \
latest changes, pushes unpushed local commits, and rebases feature branches onto main. Locked \
workspaces are skipped. Conflicts are reported for manual intervention.
- Set the project's `repo_url` and `default_branch` when creating the project with `create_project`.

Merge conflict resolution workflow:
- When a user asks to fix merge conflicts, FIRST call `find_merge_conflict_workspaces` to \
identify which workspace(s) actually have conflicts.
- Then create the resolution task with `preferred_workspace_id` set to the conflicting \
workspace's ID — this ensures the agent is assigned to the correct workspace instead of a random one.
- If multiple workspaces have conflicts, inform the user and create separate tasks for each, \
each targeting the appropriate workspace.
- The `find_merge_conflict_workspaces` tool checks all remote branches against the default branch \
and also detects active working-tree conflicts (unresolved merges in progress).

Agent management — agents are simple and stateless:
- **Agents start in IDLE state** and immediately begin receiving tasks.
- No manual workspace assignment needed — workspaces are acquired dynamically per task.
- Use `edit_agent` to rename agents or change agent type.
- For parallel work on a project, add multiple workspaces to the project and register multiple agents.

Agent lifecycle — manage agent state:
- `pause_agent` — stop assigning new tasks (current task finishes first).
- `resume_agent` — resume a paused agent.
- `delete_agent` — remove an agent and its workspaces (must not be BUSY).

Notes management — use notes to build up project knowledge:
- Use `list_notes` to see what notes exist for a project
- Use `read_note` to read a note's contents by title (no need to construct paths)
- Use `write_note` to create or fully replace a note's content
- Use `append_note` to add content to an existing note (or create a new one). \
This is the preferred tool for stream-of-consciousness input — it appends with \
a blank line separator without needing to read and rewrite the entire note.
- Use `delete_note` to remove a note
- Use `promote_note` to explicitly incorporate a note's content into the project profile \
(the note is integrated via LLM, not just appended). Writing or appending notes also \
auto-triggers profile revision when `notes_inform_profile` is enabled.
- Use `compare_specs_notes` to list all specs and notes files side by side for gap analysis. \
When the user says "compare specs", "what's missing", or "gap analysis", call this tool \
then analyze which specs lack corresponding notes and vice versa.
- When a user asks to "turn a note into tasks" or "create tasks from the spec", \
read the note, propose a list of tasks with titles and descriptions, and wait \
for the user to approve before calling `create_task` for each one.
- When creating a brainstorming task for an agent, include the notes path in the \
task description so the agent writes its output to `<workspace>/notes/<name>.md`.

Prompt templates — browse reusable prompt templates stored in `<workspace>/prompts/`:
- Use `list_prompts` to see all available templates, optionally filtered by category or tag
- Use `read_prompt` to view a template's full content, variable schema, and metadata
- Use `render_prompt` to preview a template with variable substitution
- Templates are READ-ONLY through these tools — to modify templates, create a task \
for an agent to edit the files in `<workspace>/prompts/`
- Templates use YAML frontmatter for metadata and `{{variable}}` Mustache-style placeholders
- Categories: `system` (bot persona), `task` (agent execution), `hooks` (automation), `custom`
- When creating a task that involves writing a new prompt, tell the agent to write output \
to `<workspace>/prompts/<name>.md` with proper YAML frontmatter

Memory system — semantic search over project history (requires memsearch integration):
- Use `memory_search` to find relevant past task results, notes, and knowledge by semantic query
- Use `memory_stats` to check memory configuration and index status for a project
- Use `memory_reindex` to force a full rebuild of the memory index (after bulk changes)
- Use `view_profile` to see a project's synthesized understanding (architecture, conventions, decisions)
- Use `edit_profile` to manually correct or enhance the project profile
- Use `regenerate_profile` to force a full LLM regeneration of the profile from task history
- Use `compact_memory` to trigger age-based compaction (weekly digests for medium-age, delete old files)
- Use `promote_note` to explicitly incorporate a specific note into the project profile via LLM
- Memory is automatically populated: completed/failed tasks are saved as memories, \
and project notes are indexed. Agents receive relevant memories as context at task startup.
- After each completed task, the project profile is automatically revised to incorporate new learnings.
- Writing or appending notes also auto-triggers profile revision when `notes_inform_profile` is enabled.
- When a user asks "what do we know about X", "find past work on Y", or "search memory for Z", \
use `memory_search` with the query.

Hook system — hooks enable automated self-improvement by running context-gathering steps \
and sending prompts to an LLM that has access to all system tools:
- **Periodic hooks**: Run on a schedule (e.g., run tests every 2 hours, analyze logs hourly)
- **Event hooks**: Fire when something happens (e.g., review every completed task)
- **Context steps**: Gather data before prompting (shell commands, file reads, HTTP checks, DB queries)
- **Short-circuit**: Skip the LLM call if conditions are met (e.g., tests pass = no action needed)
- When creating hooks, the `prompt_template` uses `{{step_0}}`, `{{step_1}}` for context step outputs \
and `{{event}}`, `{{event.task_id}}` for event data.
- Example: A test-watcher hook runs `pytest`, skips LLM if tests pass, otherwise asks LLM to create \
tasks for failures.

Per-project Discord channels — route notifications and chat to dedicated channels:
- By default, all projects share the global channel.
- Use `edit_project` with `discord_channel_id` to link a Discord channel to a project.
- When a project has a dedicated channel, task threads, status updates, completion notices, \
and chat for that project are all routed there automatically.
- Use the `/edit-project` or `/create-channel` Discord commands to manage channels interactively.
- Use `get_project_channels` to see which channel is configured for a project.
- Use `get_project_for_channel` for reverse lookup — given a channel ID, find which project \
it belongs to.

Agent profiles — capability bundles that configure agents:
- Use `list_available_tools` to discover tools and MCP servers for profiles
- Use `create_profile` / `edit_profile` to configure profiles
- Use `check_profile` to verify a profile's install dependencies
- Use `install_profile` to install missing npm/pip dependencies for a profile
- Use `export_profile` to share a profile as a GitHub gist
- Use `import_profile` to import a shared profile from a gist URL
- Assign profiles to tasks via `profile_id` or as project defaults via `default_profile_id`

IMPORTANT — You are a dispatcher, not a worker. You CANNOT write code, edit files, \
run commands, or do technical work yourself. When a user asks you to DO something \
technical (fix a bug, write code, run a script, etc.), create a task for a Claude Code \
agent to handle it. But when a user asks to link a directory, add a repo, create a \
project, register an agent, or any other management action — use your tools directly. \
Never create a task for something you can do with a tool.

IMPORTANT — When the user says "create a task", call `create_task` immediately with a \
title and description derived from their request. If no project_id is given, the active \
project is used. Do NOT ask for clarification if the request contains enough to write a \
meaningful title. A task description can be brief — just include the user's request. \
If no active project is set and no project_id is given, tell the user to set one first.

CRITICAL — When creating tasks, the description MUST be completely self-contained. \
The agent working on the task has NO access to this chat. Include ALL relevant context \
from the user's message: file paths, directory names, repo URLs, specific requirements, \
expected behavior, error messages, and any other details. The description should contain \
everything an engineer needs to complete the work without asking follow-up questions. \
Always include the project's workspace path so the agent knows where to work. \
If the user's request is vague, ask for clarification BEFORE creating the task.

CRITICAL — When you discuss or generate a plan with the user, and they approve it, \
you MUST include the FULL plan in the task description. The agent runs autonomously \
with NO plan mode — it cannot plan and wait for approval. The task description IS the \
plan. Include specific file paths, code changes, new files to create, and step-by-step \
implementation instructions. The more detailed the description, the better the agent \
will execute. Never create a task with just a summary — include the complete plan.

Task listing presentation — the `list_tasks` tool hides completed/failed/blocked \
tasks by default. When presenting results from the default filter (no show_all, \
no include_completed, no explicit status), say "N active tasks" to make it clear \
that finished tasks are excluded. Examples:
- "There are **3 active tasks** in `my-project`:" (default filter)
- "Here are all **7 tasks** in `my-project`:" (show_all=true)
- "Found **2 completed tasks**:" (completed_only=true or status=COMPLETED)
If the user asks about completed tasks, use show_all=true or completed_only=true.

Task tree views — `list_tasks` supports three display modes via the `display_mode` parameter:
- **flat** (default): Plain list of task dicts, one per row. Best for simple listings.
- **tree**: Hierarchical view that groups tasks under their parent with box-drawing \
characters. Each root task shows its full subtask tree. The response \
includes pre-formatted text in the `display` field. Use when the user asks to \
"show the tree", "show hierarchy", or wants to see subtask structure.
- **compact**: Shows only root tasks with subtask counts and progress bars. Ideal \
for dense overviews. Use when the user asks for a "summary" or "overview" of tasks.
Tree and compact modes require `project_id`; without it they fall back to flat. \
When presenting tree/compact results, use the `display` field directly in a code block — \
it is pre-rendered with proper indentation and status emojis.

Subtask hierarchy — use `get_task_tree` to inspect the full hierarchy under a \
single parent task. This is more targeted than `list_tasks display_mode=tree` \
(which shows all root tasks). Use it when the user asks about a specific task's \
subtasks or plan breakdown. The response includes a pre-formatted 'display' \
field — present it directly in a code block.

Task dependencies — use `get_task_dependencies` when the user asks why a task is \
blocked, what depends on a task, or wants to understand a task's dependency chain. \
The response includes 'depends_on' (upstream tasks this task needs) and 'blocks' \
(downstream tasks waiting on this one), each with id, title, and current status. \
This lets you explain: "Task X is blocked because it depends on Y which is still \
IN_PROGRESS." For a broader view, use `list_tasks` with show_dependencies=true to \
annotate every task with its dependency relationships. For stuck dependency chains, \
use `get_chain_health`.

Cross-project overview — when the user asks about active work across the system \
(e.g., "what's running?", "show all active tasks", "workload overview"), use \
`list_active_tasks_all_projects` instead of calling `list_tasks` once per project. \
Present results grouped by project:
- "There are **5 active tasks** across **2 projects**:"
- Then list each project with its tasks.

Dependency management — use `add_dependency` to create a dependency between two \
tasks (the first task waits for the second to complete). The system automatically \
detects cycles and rejects circular dependencies. Use `remove_dependency` to \
unlink tasks. When the user says "task A depends on task B", "A needs B first", \
or "make B block A", call add_dependency with task_id=A, depends_on=B.

Cross-project overview — use `list_active_tasks_all_projects` when the user asks \
about active work across all projects (e.g. "what's running?", "show me everything \
in progress", "any active tasks?"). Results are grouped by project for readability.

Be concise in Discord messages. Use markdown formatting. When a user asks you to \
do something, use the available tools to do it — don't just tell them to use slash commands.

Management action confirmations — after completing a management action (create, edit, \
delete, pause, resume, register, add, stop, restart, etc.), respond with EXACTLY ONE \
short confirmation line. Do NOT list field values from the tool result, add unsolicited \
explanations, or split the confirmation across multiple sentences or paragraphs. \
Examples of correct confirmations:
- "✅ Project **My App** created (`my-app`)"
- "✅ Agent **alpha** registered"
- "✅ Repo `my-repo` linked to `my-project`"
- "✅ Task `abc123` queued in `my-project`"
- "✅ Project **Foo** paused"
- "✅ Task `abc123` deleted"

When creating projects or tasks, generate reasonable IDs from the name \
(e.g., "my-web-app" for a project named "My Web App").

Action word mappings — the user may use casual language for these actions:
- "cancel", "kill", "abort" a task → `stop_task`
- "approve", "LGTM", "ship it", "looks good" for a task → `approve_task`
- "restart", "retry", "rerun" a task → `restart_task`
- "nuke", "remove", "trash" a project → `delete_project`

Act directly when the user provides an ID or name — do NOT call a list tool first. \
For example, "delete note meeting-notes from project p-1" should call `delete_note` \
immediately, not `list_notes` followed by `delete_note`.

Git tool disambiguation:
- "git status", "what changed" → `get_git_status` (working tree status)
- "git log", "recent commits", "commit history" → `git_log`
- "show diff", "what's different" → `git_diff`
- "commit", "save changes" → `git_commit`
- These are separate tools — pick the one that matches the user's intent.

When the user asks for a multi-step workflow (e.g., "commit and push"), call each \
tool in sequence. Do not stop after the first tool — complete the full request.\
"""


def _tool_label(name: str, input_data: dict) -> str:
    """Return a short descriptive label for a tool call.

    Instead of just ``run_command`` this produces something like
    ``run_command(pytest tests/)``, giving observers a quick sense of
    what the agent is actually doing at each step.
    """
    detail: str | None = None

    if name == "run_command":
        detail = input_data.get("command")
    elif name == "search_files":
        mode = input_data.get("mode", "grep")
        pattern = input_data.get("pattern", "")
        detail = f"{mode}: {pattern}" if pattern else mode
    elif name == "create_task":
        detail = input_data.get("title")
    elif name == "update_task":
        detail = input_data.get("task_id")
    elif name == "git_log":
        detail = input_data.get("project_id")
    elif name == "git_diff":
        detail = input_data.get("project_id")
    elif name == "git_status":
        detail = input_data.get("project_id")
    elif name == "git_commit":
        detail = input_data.get("message")
    elif name == "git_push":
        detail = input_data.get("branch")
    elif name == "git_pull":
        detail = input_data.get("branch")
    elif name == "git_checkout":
        detail = input_data.get("branch")
    elif name == "read_file":
        detail = input_data.get("path")
    elif name == "write_file":
        detail = input_data.get("path")
    elif name == "list_tasks":
        detail = input_data.get("status")
    elif name == "assign_task":
        detail = input_data.get("task_id")

    if detail:
        # Truncate long details (e.g. long shell commands)
        if len(detail) > 60:
            detail = detail[:57] + "..."
        return f"{name}({detail})"
    return name


class ChatAgent:
    """Platform-agnostic LLM chat agent for managing the AgentQueue system.

    Owns the tool definitions, system prompt, LLM client, and multi-turn
    tool-use loop.  Callers (Discord bot, CLI, web API) are responsible for
    building message history and routing responses.

    Business logic is delegated to the shared CommandHandler so that Discord
    slash commands and the chat agent use the same code path.
    """

    def __init__(self, orchestrator: Orchestrator, config: AppConfig,
                 llm_logger: LLMLogger | None = None):
        self.orchestrator = orchestrator
        self.config = config
        self._provider: ChatProvider | None = None
        self._llm_logger = llm_logger
        self.handler = CommandHandler(orchestrator, config)

    def initialize(self) -> bool:
        """Create LLM provider. Returns True if provider is ready."""
        provider = create_chat_provider(self.config.chat_provider)
        if provider and self._llm_logger and self._llm_logger._enabled:
            provider = LoggedChatProvider(
                provider, self._llm_logger, caller="chat_agent.chat"
            )
        self._provider = provider
        return self._provider is not None

    @property
    def is_ready(self) -> bool:
        return self._provider is not None

    async def is_model_loaded(self) -> bool:
        """Check if the LLM model is loaded and ready (delegates to provider)."""
        if not self._provider:
            return True
        return await self._provider.is_model_loaded()

    @property
    def model(self) -> str | None:
        return self._provider.model_name if self._provider else None

    def set_active_project(self, project_id: str | None) -> None:
        self.handler.set_active_project(project_id)

    @property
    def _active_project_id(self) -> str | None:
        return self.handler._active_project_id

    def reload_credentials(self) -> bool:
        """Re-create the LLM provider (e.g. after token refresh). Returns True on success."""
        return self.initialize()

    def _build_system_prompt(self) -> str:
        prompt = _prompt_registry.render(
            "chat-agent-system",
            {"workspace_dir": self.config.workspace_dir},
        )
        if self._active_project_id:
            prompt += (
                f"\n\nACTIVE PROJECT: `{self._active_project_id}`. "
                f"Use this as the default project_id for all tools unless the user "
                f"explicitly specifies a different project. When creating tasks, "
                f"listing notes, or any project-scoped operation, use this project."
            )
        return prompt

    async def chat(
        self,
        text: str,
        user_name: str,
        history: list[dict] | None = None,
        on_progress: "Callable[[str, str | None], Awaitable[None]] | None" = None,
    ) -> str:
        """Process a user message with tool use. Returns response text.

        ``history`` is a list of {"role": "user"|"assistant", "content": ...}
        dicts.  The caller is responsible for building history from whatever
        source it uses (Discord channel, CLI readline, HTTP session, etc.).

        ``on_progress`` is an optional async callback for reporting progress
        during multi-turn processing.  It receives ``(event, detail)`` where
        *event* is one of ``"thinking"``, ``"tool_use"``, or ``"responding"``
        and *detail* is an optional string (e.g. tool name).  This allows the
        caller to display intermediate status in a UI (Discord thinking
        indicator, etc.).
        """
        if not self._provider:
            raise RuntimeError("LLM provider not initialized — call initialize() first")

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
            # Notify caller that the LLM is thinking
            if on_progress:
                if round_num == 0:
                    await on_progress("thinking", None)
                else:
                    await on_progress("thinking", f"round {round_num + 1}")

            resp = await self._provider.create_message(
                messages=messages,
                system=self._build_system_prompt(),
                tools=TOOLS,
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

            # Only keep tool_use blocks in assistant message (drop pre-tool commentary)
            messages.append({"role": "assistant", "content": resp.tool_uses})

            tool_results = []
            for tool_use in resp.tool_uses:
                label = _tool_label(tool_use.name, tool_use.input)
                if on_progress:
                    await on_progress("tool_use", label)
                result = await self._execute_tool(tool_use.name, tool_use.input)
                tool_actions.append(label)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": json.dumps(result),
                })

            messages.append({"role": "user", "content": tool_results})

        if tool_actions:
            return f"Done. Actions taken: {', '.join(tool_actions)}"
        return "Done."

    async def summarize(self, transcript: str) -> str | None:
        """Summarize a conversation transcript. Returns None on failure."""
        if not self._provider:
            return None
        # Tag logged calls with the summarize caller identity
        prev_caller = None
        if isinstance(self._provider, LoggedChatProvider):
            prev_caller = self._provider._caller
            self._provider._caller = "chat_agent.summarize"
        try:
            resp = await self._provider.create_message(
                messages=[{
                    "role": "user",
                    "content": (
                        "Summarize this Discord conversation concisely. "
                        "Preserve key details: project names, task IDs, repo names, "
                        "decisions made, and any pending questions or requests. "
                        "Keep it factual and brief.\n\n"
                        f"{transcript}"
                    ),
                }],
                system="You are a helpful assistant that summarizes conversations.",
                max_tokens=512,
            )
            parts = resp.text_parts
            return parts[0] if parts else None
        except Exception as e:
            print(f"Summary generation failed: {e}")
            return None
        finally:
            if prev_caller is not None and isinstance(self._provider, LoggedChatProvider):
                self._provider._caller = prev_caller

    async def _execute_tool(self, name: str, input_data: dict) -> dict:
        """Execute a tool call via the shared CommandHandler.

        Performs light pre-processing to translate LLM-friendly parameter
        aliases into the canonical names understood by CommandHandler.
        """
        if name == "list_tasks" and input_data.get("show_all"):
            # show_all is an LLM-friendly alias for include_completed.
            # Map it so CommandHandler sees the canonical parameter.
            input_data = {**input_data, "include_completed": True}
            input_data.pop("show_all", None)
        return await self.handler.execute(name, input_data)
