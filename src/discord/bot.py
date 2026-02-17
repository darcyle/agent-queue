from __future__ import annotations

import asyncio
import json
import os
import subprocess
import traceback
import uuid

import discord
from discord.ext import commands

from src.config import AppConfig
from src.models import (
    Agent, AgentState, Project, ProjectStatus, RepoConfig, RepoSourceType,
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
        "description": "Register a new agent that can work on tasks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Agent display name"},
                "agent_type": {
                    "type": "string",
                    "description": "Agent type (claude, codex, cursor, aider)",
                    "default": "claude",
                },
            },
            "required": ["name"],
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

Repository management — use the `add_repo` tool to connect repos to projects:
- **clone**: Clone a git repo by URL. Agents get their own checkout. Use for remote repos.
- **link**: Link an existing directory on disk. Agents get isolated git worktrees. Use when \
the user says to "link", "connect", "use", or "point to" an existing directory/repo.
- **init**: Create a new empty git repo. Use when starting from scratch.
When creating tasks for a project with repos, pass the `repo_id` to `create_task` so the \
agent gets an isolated workspace with its own branch.

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

When creating projects or tasks, generate reasonable IDs from the name \
(e.g., "my-web-app" for a project named "My Web App").\
"""


def _create_llm_client():
    """Create an Anthropic client using whatever backend is available."""
    import anthropic

    # Try Vertex AI first
    project_id = (
        os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
    )
    if project_id:
        from anthropic import AnthropicVertex

        region = (
            os.environ.get("GOOGLE_CLOUD_LOCATION")
            or os.environ.get("CLOUD_ML_REGION")
            or "us-east5"
        )
        return AnthropicVertex(project_id=project_id, region=region), "claude-sonnet-4@20250514"

    # Try Bedrock
    if os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"):
        from anthropic import AnthropicBedrock

        return AnthropicBedrock(), "claude-sonnet-4-20250514"

    # Direct API
    return anthropic.Anthropic(), "claude-sonnet-4-20250514"


MAX_HISTORY_MESSAGES = 50  # Max messages to fetch from Discord
COMPACT_THRESHOLD = 20     # Compact older messages beyond this count
RECENT_KEEP = 14           # Keep this many recent messages as-is after compaction


class AgentQueueBot(commands.Bot):
    def __init__(self, config: AppConfig, orchestrator: Orchestrator):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.orchestrator = orchestrator
        self._control_channel: discord.TextChannel | None = None
        self._notifications_channel: discord.TextChannel | None = None
        self._llm_client = None
        self._llm_model = None
        self._processed_messages: set[int] = set()
        self._channel_summaries: dict[int, tuple[int, str]] = {}  # channel_id -> (up_to_message_id, summary)
        self._channel_locks: dict[int, asyncio.Lock] = {}  # prevent concurrent LLM calls per channel
        self._restart_requested = False
        self._boot_time: float | None = None

    async def setup_hook(self) -> None:
        from src.discord.commands import setup_commands
        setup_commands(self)
        if self.config.discord.guild_id:
            guild = discord.Object(id=int(self.config.discord.guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

    async def on_ready(self) -> None:
        print(f"Discord bot connected as {self.user} (guild: {self.config.discord.guild_id})")
        self._boot_time = discord.utils.utcnow().timestamp()

        # Cache channels
        if self.config.discord.guild_id:
            guild = self.get_guild(int(self.config.discord.guild_id))
            if guild:
                control_name = self.config.discord.channels.get("control", "control")
                notifications_name = self.config.discord.channels.get("notifications", "notifications")
                for ch in guild.text_channels:
                    if ch.name == control_name:
                        self._control_channel = ch
                    if ch.name == notifications_name:
                        self._notifications_channel = ch

                if self._notifications_channel:
                    print(f"Notifications channel: #{self._notifications_channel.name}")
                    # Wire up the orchestrator to post to the notifications channel
                    self.orchestrator.set_notify_callback(self._send_notification)
                else:
                    print(f"Warning: notifications channel '{notifications_name}' not found")

                if self._control_channel:
                    print(f"Control channel: #{self._control_channel.name}")
                    self.orchestrator.set_control_callback(self._send_control_message)
                else:
                    print(f"Warning: control channel '{control_name}' not found")

                # Wire up thread creation for task streaming
                if self._notifications_channel:
                    self.orchestrator.set_create_thread_callback(self._create_task_thread)

        # Initialize LLM client
        try:
            self._llm_client, self._llm_model = _create_llm_client()
            print(f"LLM client initialized (model: {self._llm_model})")
        except Exception as e:
            print(f"Warning: Could not initialize LLM client: {e}")

    @staticmethod
    async def _send_long_message(
        channel: discord.abc.Messageable,
        text: str,
        *,
        reply_to: discord.Message | None = None,
        filename: str = "response.md",
    ) -> None:
        """Send a message, handling Discord's 2000-char limit.

        Short messages are sent normally. Long messages are split at line
        boundaries when possible, falling back to a file attachment for
        very long content (>6000 chars).
        """
        if len(text) <= 2000:
            if reply_to:
                await reply_to.reply(text)
            else:
                await channel.send(text)
            return

        # Very long content → attach as file with a short preview
        if len(text) > 6000:
            # Find a reasonable preview (first paragraph or first 300 chars)
            preview_end = text.find("\n\n", 0, 500)
            if preview_end == -1:
                preview_end = min(300, len(text))
            preview = text[:preview_end].rstrip()

            file = discord.File(
                fp=__import__("io").BytesIO(text.encode("utf-8")),
                filename=filename,
            )
            msg = f"{preview}\n\n*Full response attached ({len(text):,} chars)*"
            if reply_to:
                await reply_to.reply(msg, file=file)
            else:
                await channel.send(msg, file=file)
            return

        # Medium-length content → split into multiple messages at line boundaries
        chunks: list[str] = []
        current = ""
        for line in text.split("\n"):
            candidate = current + ("\n" if current else "") + line
            if len(candidate) > 2000:
                if current:
                    chunks.append(current)
                # If a single line exceeds 2000, hard-split it
                while len(line) > 2000:
                    chunks.append(line[:2000])
                    line = line[2000:]
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)

        for i, chunk in enumerate(chunks):
            if i == 0 and reply_to:
                await reply_to.reply(chunk)
            else:
                await channel.send(chunk)

    async def _send_notification(self, text: str) -> None:
        """Send a message to the notifications channel."""
        if self._notifications_channel:
            await self._send_long_message(self._notifications_channel, text)

    async def _send_control_message(self, text: str) -> None:
        """Send a message to the control channel."""
        if self._control_channel:
            await self._send_long_message(self._control_channel, text)

    async def _create_task_thread(self, thread_name: str, initial_message: str):
        """Create a Discord thread for streaming agent output. Returns a send callback."""
        if not self._notifications_channel:
            return None

        # Create the thread with an initial message
        msg = await self._notifications_channel.send(
            f"**Agent working:** {thread_name}"
        )
        thread = await msg.create_thread(name=thread_name)
        await thread.send(initial_message)

        async def send_to_thread(text: str) -> None:
            try:
                await self._send_long_message(thread, text)
            except Exception as e:
                print(f"Thread send error: {e}")

        return send_to_thread

    async def _validate_path(self, path: str) -> str | None:
        """Validate that a path resolves within workspace_dir or a registered repo source_path.

        Returns the resolved real path, or None if the path is outside allowed directories.
        """
        real = os.path.realpath(path)
        workspace_real = os.path.realpath(self.config.workspace_dir)
        if real.startswith(workspace_real + os.sep) or real == workspace_real:
            return real
        # Check registered repo source_paths
        repos = await self.orchestrator.db.list_repos()
        for repo in repos:
            if repo.source_path:
                repo_real = os.path.realpath(repo.source_path)
                if real.startswith(repo_real + os.sep) or real == repo_real:
                    return real
        return None

    async def on_message(self, message: discord.Message) -> None:
        # Ignore own messages
        if message.author == self.user:
            return

        # Dedup guard — prevent processing the same message twice
        if message.id in self._processed_messages:
            return
        self._processed_messages.add(message.id)
        # Keep the set from growing unbounded
        if len(self._processed_messages) > 200:
            self._processed_messages = set(list(self._processed_messages)[-100:])

        # Skip messages created before the bot started (prevents reprocessing after restart)
        if self._boot_time and message.created_at.timestamp() < self._boot_time:
            return

        # Only respond in the control channel, or when mentioned
        is_control = (
            self._control_channel
            and message.channel.id == self._control_channel.id
        )
        is_mentioned = self.user in message.mentions

        if not is_control and not is_mentioned:
            return

        # Strip the bot mention from the message text
        text = message.content
        if self.user:
            text = text.replace(f"<@{self.user.id}>", "").strip()

        if not text:
            await message.reply("How can I help? Ask me about status, projects, or tasks.")
            return

        if not self._llm_client:
            await message.reply(
                "LLM not configured — I can only respond to slash commands. "
                "Check the daemon logs for details."
            )
            return

        # Serialize LLM processing per channel to avoid duplicate/concurrent responses
        lock = self._channel_locks.setdefault(message.channel.id, asyncio.Lock())
        async with lock:
            async with message.channel.typing():
                try:
                    response = await self._process_with_llm(
                        text, message.author.display_name, message
                    )
                    await self._send_long_message(
                        message.channel, response, reply_to=message
                    )
                except Exception as e:
                    print(f"LLM error: {e}\n{traceback.format_exc()}")
                    await message.reply(f"**LLM error:** {e}")

    async def _build_message_history(
        self, channel: discord.TextChannel, before: discord.Message
    ) -> list[dict]:
        """Fetch recent channel messages and build LLM message history.

        When history exceeds COMPACT_THRESHOLD, older messages are summarized
        into a compact description so the LLM retains context without consuming
        excessive tokens.
        """
        raw: list[discord.Message] = []
        async for msg in channel.history(
            limit=MAX_HISTORY_MESSAGES, before=before
        ):
            raw.append(msg)
        raw.reverse()  # oldest first

        if not raw:
            return []

        # Split into older (to compact) and recent (to keep verbatim)
        if len(raw) > COMPACT_THRESHOLD:
            older = raw[:-RECENT_KEEP]
            recent = raw[-RECENT_KEEP:]
        else:
            older = []
            recent = raw

        messages: list[dict] = []

        # Compact older messages into a summary
        if older:
            summary = await self._get_or_create_summary(channel.id, older)
            if summary:
                messages.append({
                    "role": "user",
                    "content": f"[CONVERSATION SUMMARY — earlier messages]\n{summary}",
                })
                # Need an assistant ack so message list alternates properly
                messages.append({
                    "role": "assistant",
                    "content": "Understood, I have the conversation context.",
                })

        # Add recent messages verbatim
        for msg in recent:
            if msg.author == self.user:
                # Bot's own messages become assistant turns
                messages.append({"role": "assistant", "content": msg.content})
            else:
                messages.append({
                    "role": "user",
                    "content": f"[from {msg.author.display_name}]: {msg.content}",
                })

        # Merge consecutive same-role messages (Anthropic API requirement)
        merged: list[dict] = []
        for m in messages:
            if merged and merged[-1]["role"] == m["role"]:
                merged[-1]["content"] += "\n" + m["content"]
            else:
                merged.append(m)

        return merged

    async def _get_or_create_summary(
        self, channel_id: int, older_messages: list[discord.Message]
    ) -> str | None:
        """Return a compact summary of older messages, caching per channel."""
        if not older_messages:
            return None

        last_id = older_messages[-1].id

        # Return cached summary if it covers these messages
        cached = self._channel_summaries.get(channel_id)
        if cached and cached[0] >= last_id:
            return cached[1]

        # Build a transcript to summarize
        lines = []
        for msg in older_messages:
            author = "AgentQueue" if msg.author == self.user else msg.author.display_name
            lines.append(f"{author}: {msg.content}")
        transcript = "\n".join(lines)

        try:
            resp = self._llm_client.messages.create(
                model=self._llm_model,
                max_tokens=512,
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
            )
            summary = resp.content[0].text
            self._channel_summaries[channel_id] = (last_id, summary)
            return summary
        except Exception as e:
            print(f"Summary generation failed: {e}")
            return None

    async def _process_with_llm(
        self, text: str, user_name: str, message: discord.Message
    ) -> str:
        """Send the user message to Claude with tools and conversation history."""
        # Build history from channel
        history = await self._build_message_history(message.channel, before=message)

        # Append current message
        current = {"role": "user", "content": f"[from {user_name}]: {text}"}
        if history and history[-1]["role"] == "user":
            # Merge with last user message to avoid consecutive user turns
            history[-1]["content"] += "\n" + current["content"]
            messages = history
        else:
            messages = history + [current]

        # Allow multiple rounds of tool use
        all_text_parts: list[str] = []
        tool_actions: list[str] = []  # Track what tools were called for fallback

        for _ in range(10):
            resp = self._llm_client.messages.create(
                model=self._llm_model,
                max_tokens=1024,
                system=SYSTEM_PROMPT_TEMPLATE.format(workspace_dir=self.config.workspace_dir),
                tools=TOOLS,
                messages=messages,
            )

            # Collect text and tool use blocks
            text_parts = []
            tool_uses = []
            for block in resp.content:
                if block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_uses.append(block)

            # If no tool use, return the text response
            if not tool_uses:
                response = "\n".join(text_parts).strip()
                if response:
                    return response
                # LLM returned empty text after tool use — summarize what happened
                if tool_actions:
                    return f"Done. Actions taken: {', '.join(tool_actions)}"
                return "Done."

            # Execute tool calls and build tool results
            messages.append({"role": "assistant", "content": resp.content})

            tool_results = []
            for tool_use in tool_uses:
                result = await self._execute_tool(tool_use.name, tool_use.input)
                tool_actions.append(tool_use.name)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": json.dumps(result),
                })

            messages.append({"role": "user", "content": tool_results})

            # Accumulate text from rounds that also had tool use
            all_text_parts.extend(text_parts)

        # Hit the loop limit — return the last text or a summary
        final_text = "\n".join(all_text_parts).strip()
        if final_text:
            return final_text
        if tool_actions:
            return f"Done. Actions taken: {', '.join(tool_actions)}"
        return "Done."

    async def _execute_tool(self, name: str, input_data: dict) -> dict:
        """Execute a tool call and return the result."""
        db = self.orchestrator.db

        try:
            if name == "get_status":
                projects = await db.list_projects()
                agents = await db.list_agents()
                tasks = await db.list_tasks()

                # Build detailed agent info including current task
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

                # Show active/stuck tasks
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
                    # Auto-create or reuse the quick-tasks project
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
                task = Task(
                    id=task_id,
                    project_id=project_id,
                    title=input_data["title"],
                    description=input_data["description"],
                    priority=input_data.get("priority", 100),
                    status=TaskStatus.READY,
                    repo_id=repo_id,
                )
                await db.create_task(task)
                result = {
                    "created": task_id,
                    "title": task.title,
                    "project_id": task.project_id,
                }
                if repo_id:
                    result["repo_id"] = repo_id
                return result

            elif name == "get_task":
                task = await db.get_task(input_data["task_id"])
                if not task:
                    return {"error": f"Task '{input_data['task_id']}' not found"}
                return {
                    "id": task.id,
                    "project_id": task.project_id,
                    "title": task.title,
                    "description": task.description,
                    "status": task.status.value,
                    "priority": task.priority,
                    "assigned_agent": task.assigned_agent_id,
                    "retry_count": task.retry_count,
                    "max_retries": task.max_retries,
                }

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
                agent = Agent(
                    id=agent_id,
                    name=input_data["name"],
                    agent_type=input_data.get("agent_type", "claude"),
                )
                await db.create_agent(agent)
                return {"created": agent_id, "name": agent.name}

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

                # Validation
                if source_type == RepoSourceType.CLONE and not url:
                    return {"error": "Clone repos require a 'url' parameter"}
                if source_type == RepoSourceType.LINK and not path:
                    return {"error": "Link repos require a 'path' parameter"}
                if source_type == RepoSourceType.LINK and not os.path.isdir(path):
                    return {"error": f"Path '{path}' does not exist or is not a directory"}

                # Derive name
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

                # Find the agent's checkout path
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
                # Resolve relative paths against workspace_dir
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

                # Resolve working_dir: could be a project ID or absolute path
                if not os.path.isabs(working_dir):
                    # Try as project ID
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
                # Block if any task is in progress
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
                    # Try to extract title from first heading
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


def _count_by(items, key_fn) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        k = key_fn(item)
        counts[k] = counts.get(k, 0) + 1
    return counts
