from __future__ import annotations

import json
import os

from src.chat_providers import ChatProvider, create_chat_provider
from src.command_handler import CommandHandler
from src.config import AppConfig
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
        "name": "set_project_channel",
        "description": (
            "Link a Discord channel to a project for per-project notifications or control. "
            "When set, task updates and threads for this project will be routed to its "
            "dedicated channel instead of the global notifications channel."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "channel_id": {
                    "type": "string",
                    "description": "Discord channel ID to link",
                },
                "channel_type": {
                    "type": "string",
                    "enum": ["notifications", "control"],
                    "description": "Channel purpose: 'notifications' (task updates, threads) or 'control' (commands, chat)",
                    "default": "notifications",
                },
            },
            "required": ["project_id", "channel_id"],
        },
    },
    {
        "name": "get_project_channels",
        "description": "Get the Discord channel IDs configured for a project (notifications and control).",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
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
    {
        "name": "get_git_status",
        "description": (
            "Get the git status of a project's repository. Shows current branch, "
            "working tree status, and recent commits. Reports status for all repos "
            "registered to the project, or falls back to the project workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to check git status for",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "restart_daemon",
        "description": "Restart the agent-queue daemon process. The bot will disconnect briefly and reconnect.",
        "input_schema": {"type": "object", "properties": {}},
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
    {
        "name": "set_task_status",
        "description": "Manually override the status of a task. Bypasses the state machine — use to unstick tasks or force a status change.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to update"},
                "status": {
                    "type": "string",
                    "enum": ["DEFINED", "READY", "IN_PROGRESS", "COMPLETED", "FAILED", "BLOCKED"],
                    "description": "New status for the task",
                },
            },
            "required": ["task_id", "status"],
        },
    },
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
- Check the git status of a project's repos with `get_git_status`
- Read files from workspaces with `read_file`
- Run shell commands in workspaces with `run_command`
- Search file contents or filenames with `search_files`
- Get token usage breakdowns with `get_token_usage`
- Delete entire projects (cascading) with `delete_project`
- Create, read, edit, and delete project notes with `list_notes`, `write_note`, `delete_note`, and `read_file`
- Create and manage hooks for automated self-improvement with `create_hook`, `list_hooks`, \
`edit_hook`, `delete_hook`, `list_hook_runs`, and `fire_hook`
- Restart the daemon with `restart_daemon`
- Pause, resume, or check the orchestrator (task scheduler) with `orchestrator_control`
- Manually override a task's status with `set_task_status` (bypasses state machine)
- Inspect the last error for a task with `get_agent_error` (shows error classification and suggested fix)
- Configure per-project Discord channels with `set_project_channel` and `get_project_channels`

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

Per-project Discord channels — route notifications to dedicated channels:
- By default, all projects share the global #notifications and #control channels.
- Use `set_project_channel` to link a Discord channel to a project for notifications or control.
- When a project has a dedicated notifications channel, task threads, status updates, and \
completion notices for that project are routed there automatically.
- When a project has a dedicated control channel, the bot responds to messages in that \
channel with project context, similar to the global control channel.
- Use the `/set-channel` or `/create-channel` Discord commands to manage channels interactively.
- Use `get_project_channels` to see which channels are configured for a project.

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

CRITICAL — When you discuss or generate a plan with the user, and they approve it, \
you MUST include the FULL plan in the task description. The agent runs autonomously \
with NO plan mode — it cannot plan and wait for approval. The task description IS the \
plan. Include specific file paths, code changes, new files to create, and step-by-step \
implementation instructions. The more detailed the description, the better the agent \
will execute. Never create a task with just a summary — include the complete plan.

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


class ChatAgent:
    """Platform-agnostic LLM chat agent for managing the AgentQueue system.

    Owns the tool definitions, system prompt, LLM client, and multi-turn
    tool-use loop.  Callers (Discord bot, CLI, web API) are responsible for
    building message history and routing responses.

    Business logic is delegated to the shared CommandHandler so that Discord
    slash commands and the chat agent use the same code path.
    """

    def __init__(self, orchestrator: Orchestrator, config: AppConfig):
        self.orchestrator = orchestrator
        self.config = config
        self._provider: ChatProvider | None = None
        self.handler = CommandHandler(orchestrator, config)

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
        self.handler.set_active_project(project_id)

    @property
    def _active_project_id(self) -> str | None:
        return self.handler._active_project_id

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

    async def _execute_tool(self, name: str, input_data: dict) -> dict:
        """Execute a tool call via the shared CommandHandler."""
        return await self.handler.execute(name, input_data)
