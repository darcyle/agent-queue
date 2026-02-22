from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from src.chat_providers import ChatProvider, create_chat_provider
from src.config import AppConfig
from src.models import (
    Agent, Hook, Project, ProjectStatus, RepoConfig, RepoSourceType,
    Task, TaskStatus,
)
from src.orchestrator import Orchestrator


# Tools the LLM can call to manage the system
TOOLS = [
    {
        "name": "list_projects",
        "description": "List all projects in the system.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_project",
        "description": "Create a new project.",
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
        "description": "Edit a project's name, credit weight, or max concurrent agents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "name": {"type": "string", "description": "New project name (optional)"},
                "credit_weight": {"type": "number", "description": "New scheduling weight (optional)"},
                "max_concurrent_agents": {"type": "integer", "description": "New max concurrent agents (optional)"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "list_tasks",
        "description": "List tasks, optionally filtered by project or status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Filter by project ID (optional)",
                },
                "status": {
                    "type": "string",
                    "description": "Filter by status: DEFINED, READY, IN_PROGRESS, COMPLETED, etc.",
                },
            },
        },
    },
    {
        "name": "create_task",
        "description": "Create a new task. If no project_id is given, it goes into the 'quick-tasks' project automatically.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID (optional — omit for quick standalone tasks)",
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
                "repo_id": {
                    "type": "string",
                    "description": "Repository ID to work in (optional — agent gets an isolated checkout/worktree)",
                },
                "requires_approval": {
                    "type": "boolean",
                    "description": "If true, agent work creates a PR instead of auto-merging. Human must approve/merge the PR.",
                    "default": False,
                },
            },
            "required": ["title", "description"],
        },
    },
    {
        "name": "add_repo",
        "description": "Register a repository for a project. Source types: 'clone' (git URL), 'link' (existing directory on disk), 'init' (new empty repo).",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to add the repo to",
                },
                "source": {
                    "type": "string",
                    "enum": ["clone", "link", "init"],
                    "description": "How to set up the repo: clone (from URL), link (existing dir), init (new empty repo)",
                },
                "url": {
                    "type": "string",
                    "description": "Git URL (required for clone)",
                },
                "path": {
                    "type": "string",
                    "description": "Existing directory path (required for link)",
                },
                "name": {
                    "type": "string",
                    "description": "Repo name (optional — derived from URL or path)",
                },
                "default_branch": {
                    "type": "string",
                    "description": "Default branch name (default: main)",
                    "default": "main",
                },
            },
            "required": ["project_id", "source"],
        },
    },
    {
        "name": "list_repos",
        "description": "List registered repositories, optionally filtered by project.",
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
        "name": "list_agents",
        "description": "List all configured agents and their current state.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_agent",
        "description": "Register a new agent that can work on tasks. Optionally assign a repo so the agent works in that directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Agent display name"},
                "agent_type": {
                    "type": "string",
                    "description": "Agent type (claude, codex, cursor, aider)",
                    "default": "claude",
                },
                "repo_id": {
                    "type": "string",
                    "description": "Repo ID to assign as this agent's workspace (optional)",
                },
            },
            "required": ["name"],
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
        "description": "Edit a task's title, description, or priority.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "title": {"type": "string", "description": "New title (optional)"},
                "description": {"type": "string", "description": "New description (optional)"},
                "priority": {"type": "integer", "description": "New priority (optional)"},
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
        "description": "Delete a project and all associated data (tasks, repos, results, token ledger). Cannot delete if any task is IN_PROGRESS.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID to delete"},
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
        "description": "Update a hook's configuration (any field: enabled, trigger, context_steps, prompt_template, cooldown_seconds, llm_config).",
        "input_schema": {
            "type": "object",
            "properties": {
                "hook_id": {"type": "string", "description": "Hook ID"},
                "enabled": {"type": "boolean", "description": "Enable/disable the hook"},
                "trigger": {"type": "object", "description": "New trigger config"},
                "context_steps": {"type": "array", "description": "New context steps", "items": {"type": "object"}},
                "prompt_template": {"type": "string", "description": "New prompt template"},
                "cooldown_seconds": {"type": "integer", "description": "New cooldown"},
                "llm_config": {"type": "object", "description": "New LLM config override"},
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
        "description": "List all notes for a project. Notes are markdown documents stored in the project workspace.",
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
        "description": "Delete a project note.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "title": {"type": "string", "description": "Note title (as used when creating it)"},
            },
            "required": ["project_id", "title"],
        },
    },
]

SYSTEM_PROMPT_TEMPLATE = """\
You are AgentQueue, a Discord bot that manages an AI agent task queue. You help \
users manage projects, tasks, and agents through natural conversation.

System info:
- Workspaces root: {workspace_dir}
- Each project gets its own folder under the workspaces root (e.g., {workspace_dir}/my-project/)

You can directly (using your tools):
- Create and manage projects (groups of related tasks)
- Create, view, edit, delete, and restart tasks
- Create quick standalone tasks without specifying a project (they go into "Quick Tasks" automatically)
- Register and list agents
- Register repositories with `add_repo` and list them with `list_repos`
- Monitor agent status, task progress, and recent events
- Pause/resume projects
- Retrieve task results (summary, files changed, errors, tokens) with `get_task_result`
- Show git diffs for completed tasks with `get_task_diff`
- Read files from workspaces with `read_file`
- Run shell commands in workspaces with `run_command`
- Search file contents or filenames with `search_files`
- Get token usage breakdowns with `get_token_usage`
- Delete entire projects (cascading) with `delete_project`
- Create, read, edit, and delete project notes with `list_notes`, `write_note`, `delete_note`, and `read_file`
- Create and manage hooks for automated self-improvement with `create_hook`, `list_hooks`, \
`edit_hook`, `delete_hook`, `list_hook_runs`, and `fire_hook`

Repository management — use the `add_repo` tool to connect repos to projects:
- **clone**: Clone a git repo by URL. Agents get their own checkout. Use for remote repos.
- **link**: Link an existing directory on disk. Agents work directly in that directory, \
preserving the existing environment (.env, venv, node_modules, etc.). Use when \
the user says to "link", "connect", "use", or "point to" an existing directory/repo.
- **init**: Create a new empty git repo. Use when starting from scratch.

Agent workspaces — agents can be assigned a repo as their permanent workspace:
- Use `create_agent` with `repo_id` to assign a linked repo to an agent.
- When a task doesn't specify a repo_id, the agent uses its assigned repo automatically.
- For parallel work, link multiple checkouts of the same project as separate repos, then \
assign each agent to its own checkout. Each agent works in a fully-configured directory \
with its own .env, packages, etc.
- Example setup: link ~/project as "checkout-1", link ~/project-2 as "checkout-2", \
create Agent-1 with repo checkout-1, create Agent-2 with repo checkout-2.
- When creating tasks, you don't need to specify repo_id — each agent uses its assigned workspace.

Notes management — use notes to build up project knowledge:
- Use `list_notes` to see what notes exist for a project
- Use `read_file` to read a note's contents (path: <workspace>/notes/<name>.md)
- Use `write_note` to create or update a note (read with `read_file` first, edit content, \
then write back with `write_note`)
- Use `delete_note` to remove a note
- When a user asks to "turn a note into tasks" or "create tasks from the spec", \
read the note, propose a list of tasks with titles and descriptions, and wait \
for the user to approve before calling `create_task` for each one.
- When creating a brainstorming task for an agent, include the notes path in the \
task description so the agent writes its output to `<workspace>/notes/<name>.md`.

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

IMPORTANT — You are a dispatcher, not a worker. You CANNOT write code, edit files, \
run commands, or do technical work yourself. When a user asks you to DO something \
technical (fix a bug, write code, run a script, etc.), create a task for a Claude Code \
agent to handle it. But when a user asks to link a directory, add a repo, create a \
project, register an agent, or any other management action — use your tools directly. \
Never create a task for something you can do with a tool.

CRITICAL — When creating tasks, the description MUST be completely self-contained. \
The agent working on the task has NO access to this chat. Include ALL relevant context \
from the user's message: file paths, directory names, repo URLs, specific requirements, \
expected behavior, error messages, and any other details. The description should contain \
everything an engineer needs to complete the work without asking follow-up questions. \
Always include the project's workspace path so the agent knows where to work. \
If the user's request is vague, ask for clarification BEFORE creating the task.

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
(e.g., "my-web-app" for a project named "My Web App").\
"""


def _count_by(items, key_fn) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        k = key_fn(item)
        counts[k] = counts.get(k, 0) + 1
    return counts


class ChatAgent:
    """Platform-agnostic LLM chat agent for managing the AgentQueue system.

    Owns the tool definitions, system prompt, LLM client, and multi-turn
    tool-use loop.  Callers (Discord bot, CLI, web API) are responsible for
    building message history and routing responses.
    """

    def __init__(self, orchestrator: Orchestrator, config: AppConfig):
        self.orchestrator = orchestrator
        self.config = config
        self._provider: ChatProvider | None = None
        self._active_project_id: str | None = None

    def initialize(self) -> bool:
        """Create LLM provider. Returns True if provider is ready."""
        self._provider = create_chat_provider(self.config.chat_provider)
        return self._provider is not None

    @property
    def is_ready(self) -> bool:
        return self._provider is not None

    @property
    def model(self) -> str | None:
        return self._provider.model_name if self._provider else None

    def set_active_project(self, project_id: str | None) -> None:
        self._active_project_id = project_id

    def reload_credentials(self) -> bool:
        """Re-create the LLM provider (e.g. after token refresh). Returns True on success."""
        return self.initialize()

    def _build_system_prompt(self) -> str:
        prompt = SYSTEM_PROMPT_TEMPLATE.format(workspace_dir=self.config.workspace_dir)
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
    ) -> str:
        """Process a user message with tool use. Returns response text.

        ``history`` is a list of {"role": "user"|"assistant", "content": ...}
        dicts.  The caller is responsible for building history from whatever
        source it uses (Discord channel, CLI readline, HTTP session, etc.).
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

        for _ in range(10):
            resp = await self._provider.create_message(
                messages=messages,
                system=self._build_system_prompt(),
                tools=TOOLS,
                max_tokens=1024,
            )

            if not resp.tool_uses:
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
                result = await self._execute_tool(tool_use.name, tool_use.input)
                tool_actions.append(tool_use.name)
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

    async def _validate_path(self, path: str) -> str | None:
        """Validate that a path resolves within workspace_dir or a registered repo source_path."""
        real = os.path.realpath(path)
        workspace_real = os.path.realpath(self.config.workspace_dir)
        if real.startswith(workspace_real + os.sep) or real == workspace_real:
            return real
        repos = await self.orchestrator.db.list_repos()
        for repo in repos:
            if repo.source_path:
                repo_real = os.path.realpath(repo.source_path)
                if real.startswith(repo_real + os.sep) or real == repo_real:
                    return real
        return None

    async def _execute_tool(self, name: str, input_data: dict) -> dict:
        """Execute a tool call and return the result."""
        db = self.orchestrator.db

        try:
            if name == "get_status":
                projects = await db.list_projects()
                agents = await db.list_agents()
                tasks = await db.list_tasks()

                agent_details = []
                for a in agents:
                    info = {
                        "id": a.id,
                        "name": a.name,
                        "state": a.state.value,
                    }
                    if a.current_task_id:
                        current_task = await db.get_task(a.current_task_id)
                        if current_task:
                            info["working_on"] = {
                                "task_id": current_task.id,
                                "title": current_task.title,
                                "project_id": current_task.project_id,
                                "status": current_task.status.value,
                            }
                    agent_details.append(info)

                in_progress = [
                    {"id": t.id, "title": t.title, "project_id": t.project_id,
                     "assigned_agent": t.assigned_agent_id}
                    for t in tasks if t.status == TaskStatus.IN_PROGRESS
                ]
                ready = [
                    {"id": t.id, "title": t.title, "project_id": t.project_id}
                    for t in tasks if t.status == TaskStatus.READY
                ]

                return {
                    "projects": len(projects),
                    "agents": agent_details,
                    "tasks": {
                        "total": len(tasks),
                        "by_status": _count_by(tasks, lambda t: t.status.value),
                        "in_progress": in_progress,
                        "ready_to_work": ready,
                    },
                }

            elif name == "list_projects":
                projects = await db.list_projects()
                return {
                    "projects": [
                        {
                            "id": p.id,
                            "name": p.name,
                            "status": p.status.value,
                            "credit_weight": p.credit_weight,
                            "max_concurrent_agents": p.max_concurrent_agents,
                            "workspace": p.workspace_path,
                        }
                        for p in projects
                    ]
                }

            elif name == "create_project":
                project_id = input_data["name"].lower().replace(" ", "-")
                workspace = os.path.join(self.config.workspace_dir, project_id)
                os.makedirs(workspace, exist_ok=True)
                project = Project(
                    id=project_id,
                    name=input_data["name"],
                    credit_weight=input_data.get("credit_weight", 1.0),
                    max_concurrent_agents=input_data.get("max_concurrent_agents", 2),
                    workspace_path=workspace,
                )
                await db.create_project(project)
                return {"created": project_id, "name": project.name, "workspace": workspace}

            elif name == "pause_project":
                pid = input_data["project_id"]
                project = await db.get_project(pid)
                if not project:
                    return {"error": f"Project '{pid}' not found"}
                await db.update_project(pid, status=ProjectStatus.PAUSED)
                return {"paused": pid}

            elif name == "resume_project":
                pid = input_data["project_id"]
                project = await db.get_project(pid)
                if not project:
                    return {"error": f"Project '{pid}' not found"}
                await db.update_project(pid, status=ProjectStatus.ACTIVE)
                return {"resumed": pid}

            elif name == "edit_project":
                pid = input_data["project_id"]
                project = await db.get_project(pid)
                if not project:
                    return {"error": f"Project '{pid}' not found"}
                updates = {}
                if "name" in input_data:
                    updates["name"] = input_data["name"]
                if "credit_weight" in input_data:
                    updates["credit_weight"] = input_data["credit_weight"]
                if "max_concurrent_agents" in input_data:
                    updates["max_concurrent_agents"] = input_data["max_concurrent_agents"]
                if not updates:
                    return {"error": "No fields to update. Provide name, credit_weight, or max_concurrent_agents."}
                await db.update_project(pid, **updates)
                return {"updated": pid, "fields": list(updates.keys())}

            elif name == "list_tasks":
                kwargs = {}
                if "project_id" in input_data:
                    kwargs["project_id"] = input_data["project_id"]
                if "status" in input_data:
                    kwargs["status"] = TaskStatus(input_data["status"])
                tasks = await db.list_tasks(**kwargs)
                return {
                    "tasks": [
                        {
                            "id": t.id,
                            "project_id": t.project_id,
                            "title": t.title,
                            "status": t.status.value,
                            "priority": t.priority,
                            "assigned_agent": t.assigned_agent_id,
                        }
                        for t in tasks[:25]
                    ],
                    "total": len(tasks),
                }

            elif name == "create_task":
                project_id = input_data.get("project_id")
                if not project_id:
                    project_id = "quick-tasks"
                    existing = await db.get_project(project_id)
                    if not existing:
                        workspace = os.path.join(self.config.workspace_dir, project_id)
                        os.makedirs(workspace, exist_ok=True)
                        await db.create_project(Project(
                            id=project_id,
                            name="Quick Tasks",
                            credit_weight=0.5,
                            max_concurrent_agents=1,
                            workspace_path=workspace,
                        ))
                task_id = str(uuid.uuid4())[:8]
                repo_id = input_data.get("repo_id")
                requires_approval = input_data.get("requires_approval", False)
                task = Task(
                    id=task_id,
                    project_id=project_id,
                    title=input_data["title"],
                    description=input_data["description"],
                    priority=input_data.get("priority", 100),
                    status=TaskStatus.READY,
                    repo_id=repo_id,
                    requires_approval=requires_approval,
                )
                await db.create_task(task)
                result = {
                    "created": task_id,
                    "title": task.title,
                    "project_id": task.project_id,
                }
                if repo_id:
                    result["repo_id"] = repo_id
                if requires_approval:
                    result["requires_approval"] = True
                return result

            elif name == "get_task":
                task = await db.get_task(input_data["task_id"])
                if not task:
                    return {"error": f"Task '{input_data['task_id']}' not found"}
                info = {
                    "id": task.id,
                    "project_id": task.project_id,
                    "title": task.title,
                    "description": task.description,
                    "status": task.status.value,
                    "priority": task.priority,
                    "assigned_agent": task.assigned_agent_id,
                    "retry_count": task.retry_count,
                    "max_retries": task.max_retries,
                    "requires_approval": task.requires_approval,
                }
                if task.pr_url:
                    info["pr_url"] = task.pr_url
                return info

            elif name == "edit_task":
                task = await db.get_task(input_data["task_id"])
                if not task:
                    return {"error": f"Task '{input_data['task_id']}' not found"}
                updates = {}
                if "title" in input_data:
                    updates["title"] = input_data["title"]
                if "description" in input_data:
                    updates["description"] = input_data["description"]
                if "priority" in input_data:
                    updates["priority"] = input_data["priority"]
                if not updates:
                    return {"error": "No fields to update. Provide title, description, or priority."}
                await db.update_task(input_data["task_id"], **updates)
                return {"updated": input_data["task_id"], "fields": list(updates.keys())}

            elif name == "stop_task":
                error = await self.orchestrator.stop_task(input_data["task_id"])
                if error:
                    return {"error": error}
                return {"stopped": input_data["task_id"]}

            elif name == "restart_task":
                task = await db.get_task(input_data["task_id"])
                if not task:
                    return {"error": f"Task '{input_data['task_id']}' not found"}
                if task.status == TaskStatus.IN_PROGRESS:
                    return {"error": "Task is currently in progress. Stop it first."}
                await db.update_task(
                    input_data["task_id"],
                    status=TaskStatus.READY.value,
                    retry_count=0,
                    assigned_agent_id=None,
                )
                return {
                    "restarted": input_data["task_id"],
                    "title": task.title,
                    "previous_status": task.status.value,
                }

            elif name == "delete_task":
                task = await db.get_task(input_data["task_id"])
                if not task:
                    return {"error": f"Task '{input_data['task_id']}' not found"}
                if task.status == TaskStatus.IN_PROGRESS:
                    error = await self.orchestrator.stop_task(input_data["task_id"])
                    if error:
                        return {"error": f"Could not stop task before deleting: {error}"}
                await db.delete_task(input_data["task_id"])
                return {"deleted": input_data["task_id"], "title": task.title}

            elif name == "approve_task":
                task = await db.get_task(input_data["task_id"])
                if not task:
                    return {"error": f"Task '{input_data['task_id']}' not found"}
                if task.status != TaskStatus.AWAITING_APPROVAL:
                    return {"error": f"Task is not awaiting approval (status: {task.status.value})"}
                await db.update_task(
                    input_data["task_id"],
                    status=TaskStatus.COMPLETED.value,
                )
                await db.log_event(
                    "task_completed",
                    project_id=task.project_id,
                    task_id=task.id,
                )
                return {"approved": input_data["task_id"], "title": task.title}

            elif name == "list_agents":
                agents = await db.list_agents()
                return {
                    "agents": [
                        {
                            "id": a.id,
                            "name": a.name,
                            "type": a.agent_type,
                            "state": a.state.value,
                            "current_task": a.current_task_id,
                        }
                        for a in agents
                    ]
                }

            elif name == "create_agent":
                agent_id = input_data["name"].lower().replace(" ", "-")
                repo_id = input_data.get("repo_id")
                if repo_id:
                    repo = await db.get_repo(repo_id)
                    if not repo:
                        return {"error": f"Repo '{repo_id}' not found"}
                agent = Agent(
                    id=agent_id,
                    name=input_data["name"],
                    agent_type=input_data.get("agent_type", "claude"),
                    repo_id=repo_id,
                )
                await db.create_agent(agent)
                result = {"created": agent_id, "name": agent.name}
                if repo_id:
                    result["repo_id"] = repo_id
                return result

            elif name == "set_active_project":
                pid = input_data.get("project_id")
                if pid:
                    project = await db.get_project(pid)
                    if not project:
                        return {"error": f"Project '{pid}' not found"}
                    self._active_project_id = pid
                    return {"active_project": pid, "name": project.name}
                else:
                    self._active_project_id = None
                    return {"active_project": None, "message": "Active project cleared"}

            elif name == "add_repo":
                project_id = input_data["project_id"]
                project = await db.get_project(project_id)
                if not project:
                    return {"error": f"Project '{project_id}' not found"}

                source = input_data["source"]
                source_type = RepoSourceType(source)
                url = input_data.get("url", "")
                path = input_data.get("path", "")
                default_branch = input_data.get("default_branch", "main")

                if source_type == RepoSourceType.CLONE and not url:
                    return {"error": "Clone repos require a 'url' parameter"}
                if source_type == RepoSourceType.LINK and not path:
                    return {"error": "Link repos require a 'path' parameter"}
                if source_type == RepoSourceType.LINK and not os.path.isdir(path):
                    return {"error": f"Path '{path}' does not exist or is not a directory"}

                repo_name = input_data.get("name")
                if not repo_name:
                    if url:
                        repo_name = url.rstrip("/").split("/")[-1].replace(".git", "")
                    elif path:
                        repo_name = os.path.basename(path.rstrip("/"))
                    else:
                        repo_name = f"{project_id}-repo"

                repo_id = repo_name.lower().replace(" ", "-")
                checkout_base = os.path.join(
                    project.workspace_path or self.config.workspace_dir, "repos", repo_name
                )

                repo = RepoConfig(
                    id=repo_id,
                    project_id=project_id,
                    source_type=source_type,
                    url=url,
                    source_path=path,
                    default_branch=default_branch,
                    checkout_base_path=checkout_base,
                )
                await db.create_repo(repo)
                return {
                    "created": repo_id,
                    "name": repo_name,
                    "source_type": source,
                    "checkout_base_path": checkout_base,
                }

            elif name == "list_repos":
                project_id = input_data.get("project_id")
                repos = await db.list_repos(project_id=project_id)
                return {
                    "repos": [
                        {
                            "id": r.id,
                            "project_id": r.project_id,
                            "source_type": r.source_type.value,
                            "url": r.url,
                            "source_path": r.source_path,
                            "default_branch": r.default_branch,
                            "checkout_base_path": r.checkout_base_path,
                        }
                        for r in repos
                    ]
                }

            elif name == "get_recent_events":
                limit = input_data.get("limit", 10)
                events = await db.get_recent_events(limit=limit)
                return {"events": events}

            elif name == "get_task_result":
                result = await db.get_task_result(input_data["task_id"])
                if not result:
                    return {"error": f"No results found for task '{input_data['task_id']}'"}
                return result

            elif name == "get_task_diff":
                task = await db.get_task(input_data["task_id"])
                if not task:
                    return {"error": f"Task '{input_data['task_id']}' not found"}
                if not task.repo_id:
                    return {"error": "Task has no associated repository"}
                repo = await db.get_repo(task.repo_id)
                if not repo:
                    return {"error": f"Repository '{task.repo_id}' not found"}
                if not task.branch_name:
                    return {"error": "Task has no branch name"}

                checkout_path = None
                if task.assigned_agent_id:
                    agent = await db.get_agent(task.assigned_agent_id)
                    if agent and agent.checkout_path:
                        checkout_path = agent.checkout_path
                if not checkout_path and repo.source_path:
                    checkout_path = repo.source_path
                if not checkout_path:
                    return {"error": "Could not determine checkout path for diff"}

                diff = self.orchestrator.git.get_diff(checkout_path, repo.default_branch)
                if not diff:
                    return {"diff": "(no changes)", "branch": task.branch_name}
                return {"diff": diff, "branch": task.branch_name}

            elif name == "read_file":
                path = input_data["path"]
                max_lines = input_data.get("max_lines", 200)
                if not os.path.isabs(path):
                    path = os.path.join(self.config.workspace_dir, path)
                validated = await self._validate_path(path)
                if not validated:
                    return {"error": "Access denied: path is outside allowed directories"}
                if not os.path.isfile(validated):
                    return {"error": f"File not found: {path}"}
                try:
                    with open(validated, "r") as f:
                        lines = []
                        for i, line in enumerate(f):
                            if i >= max_lines:
                                lines.append(f"\n... truncated at {max_lines} lines ({i} total)")
                                break
                            lines.append(line.rstrip("\n"))
                    return {"content": "\n".join(lines), "path": validated}
                except UnicodeDecodeError:
                    return {"error": "Binary file — cannot display contents"}

            elif name == "run_command":
                command = input_data["command"]
                working_dir = input_data["working_dir"]
                timeout = min(input_data.get("timeout", 30), 120)

                if not os.path.isabs(working_dir):
                    project = await db.get_project(working_dir)
                    if project and project.workspace_path:
                        working_dir = project.workspace_path
                    else:
                        working_dir = os.path.join(self.config.workspace_dir, working_dir)

                validated = await self._validate_path(working_dir)
                if not validated:
                    return {"error": "Access denied: working directory is outside allowed directories"}
                if not os.path.isdir(validated):
                    return {"error": f"Directory not found: {working_dir}"}

                try:
                    result = await asyncio.to_thread(
                        subprocess.run,
                        command,
                        shell=True,
                        cwd=validated,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                    )
                    stdout = result.stdout[:4000] if result.stdout else ""
                    stderr = result.stderr[:2000] if result.stderr else ""
                    return {
                        "returncode": result.returncode,
                        "stdout": stdout,
                        "stderr": stderr,
                    }
                except subprocess.TimeoutExpired:
                    return {"error": f"Command timed out after {timeout}s"}

            elif name == "delete_project":
                pid = input_data["project_id"]
                project = await db.get_project(pid)
                if not project:
                    return {"error": f"Project '{pid}' not found"}
                tasks = await db.list_tasks(project_id=pid, status=TaskStatus.IN_PROGRESS)
                if tasks:
                    return {
                        "error": f"Cannot delete: {len(tasks)} task(s) currently IN_PROGRESS. "
                                 "Stop them first."
                    }
                await db.delete_project(pid)
                return {"deleted": pid, "name": project.name}

            elif name == "search_files":
                pattern = input_data["pattern"]
                path = input_data["path"]
                mode = input_data.get("mode", "grep")

                if not os.path.isabs(path):
                    path = os.path.join(self.config.workspace_dir, path)
                validated = await self._validate_path(path)
                if not validated:
                    return {"error": "Access denied: path is outside allowed directories"}
                if not os.path.isdir(validated):
                    return {"error": f"Directory not found: {path}"}

                try:
                    if mode == "grep":
                        result = await asyncio.to_thread(
                            subprocess.run,
                            ["grep", "-rn", "--include=*", "-m", "50", pattern, validated],
                            capture_output=True, text=True, timeout=30,
                        )
                    else:
                        result = await asyncio.to_thread(
                            subprocess.run,
                            ["find", validated, "-name", pattern, "-type", "f"],
                            capture_output=True, text=True, timeout=30,
                        )
                    output = result.stdout[:4000] if result.stdout else "(no matches)"
                    return {"results": output, "mode": mode}
                except subprocess.TimeoutExpired:
                    return {"error": "Search timed out"}

            elif name == "get_token_usage":
                project_id = input_data.get("project_id")
                task_id = input_data.get("task_id")

                if task_id:
                    cursor = await db._db.execute(
                        "SELECT agent_id, SUM(tokens_used) as total, COUNT(*) as entries "
                        "FROM token_ledger WHERE task_id = ? GROUP BY agent_id",
                        (task_id,),
                    )
                    rows = await cursor.fetchall()
                    return {
                        "task_id": task_id,
                        "breakdown": [
                            {"agent_id": r["agent_id"], "tokens": r["total"], "entries": r["entries"]}
                            for r in rows
                        ],
                        "total": sum(r["total"] for r in rows),
                    }
                elif project_id:
                    cursor = await db._db.execute(
                        "SELECT task_id, agent_id, SUM(tokens_used) as total "
                        "FROM token_ledger WHERE project_id = ? "
                        "GROUP BY task_id, agent_id ORDER BY total DESC",
                        (project_id,),
                    )
                    rows = await cursor.fetchall()
                    return {
                        "project_id": project_id,
                        "breakdown": [
                            {"task_id": r["task_id"], "agent_id": r["agent_id"], "tokens": r["total"]}
                            for r in rows
                        ],
                        "total": sum(r["total"] for r in rows),
                    }
                else:
                    cursor = await db._db.execute(
                        "SELECT project_id, SUM(tokens_used) as total "
                        "FROM token_ledger GROUP BY project_id ORDER BY total DESC",
                    )
                    rows = await cursor.fetchall()
                    return {
                        "breakdown": [
                            {"project_id": r["project_id"], "tokens": r["total"]}
                            for r in rows
                        ],
                        "total": sum(r["total"] for r in rows),
                    }

            elif name == "create_hook":
                project_id = input_data["project_id"]
                project = await db.get_project(project_id)
                if not project:
                    return {"error": f"Project '{project_id}' not found"}
                hook_id = input_data["name"].lower().replace(" ", "-")
                hook = Hook(
                    id=hook_id,
                    project_id=project_id,
                    name=input_data["name"],
                    trigger=json.dumps(input_data["trigger"]),
                    context_steps=json.dumps(input_data.get("context_steps", [])),
                    prompt_template=input_data["prompt_template"],
                    cooldown_seconds=input_data.get("cooldown_seconds", 3600),
                    llm_config=json.dumps(input_data["llm_config"]) if input_data.get("llm_config") else None,
                )
                await db.create_hook(hook)
                return {"created": hook_id, "name": hook.name, "project_id": project_id}

            elif name == "list_hooks":
                project_id = input_data.get("project_id")
                hooks = await db.list_hooks(project_id=project_id)
                return {
                    "hooks": [
                        {
                            "id": h.id,
                            "project_id": h.project_id,
                            "name": h.name,
                            "enabled": h.enabled,
                            "trigger": json.loads(h.trigger),
                            "cooldown_seconds": h.cooldown_seconds,
                        }
                        for h in hooks
                    ]
                }

            elif name == "edit_hook":
                hook_id = input_data["hook_id"]
                hook = await db.get_hook(hook_id)
                if not hook:
                    return {"error": f"Hook '{hook_id}' not found"}
                updates = {}
                if "enabled" in input_data:
                    updates["enabled"] = input_data["enabled"]
                if "trigger" in input_data:
                    updates["trigger"] = json.dumps(input_data["trigger"])
                if "context_steps" in input_data:
                    updates["context_steps"] = json.dumps(input_data["context_steps"])
                if "prompt_template" in input_data:
                    updates["prompt_template"] = input_data["prompt_template"]
                if "cooldown_seconds" in input_data:
                    updates["cooldown_seconds"] = input_data["cooldown_seconds"]
                if "llm_config" in input_data:
                    updates["llm_config"] = json.dumps(input_data["llm_config"])
                if not updates:
                    return {"error": "No fields to update"}
                await db.update_hook(hook_id, **updates)
                return {"updated": hook_id, "fields": list(updates.keys())}

            elif name == "delete_hook":
                hook_id = input_data["hook_id"]
                hook = await db.get_hook(hook_id)
                if not hook:
                    return {"error": f"Hook '{hook_id}' not found"}
                await db.delete_hook(hook_id)
                return {"deleted": hook_id, "name": hook.name}

            elif name == "list_hook_runs":
                hook_id = input_data["hook_id"]
                hook = await db.get_hook(hook_id)
                if not hook:
                    return {"error": f"Hook '{hook_id}' not found"}
                limit = input_data.get("limit", 10)
                runs = await db.list_hook_runs(hook_id, limit=limit)
                return {
                    "hook_id": hook_id,
                    "hook_name": hook.name,
                    "runs": [
                        {
                            "id": r.id,
                            "trigger_reason": r.trigger_reason,
                            "status": r.status,
                            "tokens_used": r.tokens_used,
                            "skipped_reason": r.skipped_reason,
                            "started_at": r.started_at,
                            "completed_at": r.completed_at,
                        }
                        for r in runs
                    ],
                }

            elif name == "fire_hook":
                hook_id = input_data["hook_id"]
                hooks_engine = self.orchestrator.hooks
                if not hooks_engine:
                    return {"error": "Hook engine is not enabled"}
                try:
                    await hooks_engine.fire_hook(hook_id)
                    return {"fired": hook_id, "status": "running"}
                except ValueError as e:
                    return {"error": str(e)}

            elif name == "list_notes":
                project = await db.get_project(input_data["project_id"])
                if not project:
                    return {"error": f"Project '{input_data['project_id']}' not found"}
                workspace = project.workspace_path or os.path.join(
                    self.config.workspace_dir, input_data["project_id"]
                )
                notes_dir = os.path.join(workspace, "notes")
                if not os.path.isdir(notes_dir):
                    return {"project_id": input_data["project_id"], "notes": []}
                notes = []
                for fname in sorted(os.listdir(notes_dir)):
                    if not fname.endswith(".md"):
                        continue
                    fpath = os.path.join(notes_dir, fname)
                    stat = os.stat(fpath)
                    title = fname[:-3].replace("-", " ").title()
                    try:
                        with open(fpath, "r") as f:
                            first_line = f.readline().strip()
                        if first_line.startswith("# "):
                            title = first_line[2:].strip()
                    except Exception:
                        pass
                    notes.append({
                        "name": fname,
                        "title": title,
                        "size_bytes": stat.st_size,
                        "modified": stat.st_mtime,
                        "path": fpath,
                    })
                return {"project_id": input_data["project_id"], "notes": notes}

            elif name == "write_note":
                project = await db.get_project(input_data["project_id"])
                if not project:
                    return {"error": f"Project '{input_data['project_id']}' not found"}
                workspace = project.workspace_path or os.path.join(
                    self.config.workspace_dir, input_data["project_id"]
                )
                notes_dir = os.path.join(workspace, "notes")
                os.makedirs(notes_dir, exist_ok=True)
                slug = self.orchestrator.git.slugify(input_data["title"])
                if not slug:
                    return {"error": "Title produces an empty filename"}
                fpath = os.path.join(notes_dir, f"{slug}.md")
                existed = os.path.isfile(fpath)
                with open(fpath, "w") as f:
                    f.write(input_data["content"])
                return {
                    "path": fpath,
                    "title": input_data["title"],
                    "status": "updated" if existed else "created",
                }

            elif name == "delete_note":
                project = await db.get_project(input_data["project_id"])
                if not project:
                    return {"error": f"Project '{input_data['project_id']}' not found"}
                workspace = project.workspace_path or os.path.join(
                    self.config.workspace_dir, input_data["project_id"]
                )
                slug = self.orchestrator.git.slugify(input_data["title"])
                fpath = os.path.join(workspace, "notes", f"{slug}.md")
                if not os.path.isfile(fpath):
                    return {"error": f"Note '{input_data['title']}' not found"}
                os.remove(fpath)
                return {"deleted": fpath, "title": input_data["title"]}

            else:
                return {"error": f"Unknown tool: {name}"}

        except Exception as e:
            return {"error": str(e)}
