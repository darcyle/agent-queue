# Multi-Channel Discord Support — Implementation Plan

**Date:** 2026-02-23
**Branch:** `eager-journey/plan-multi-channel-support-implementation`
**Status:** Planned

## Overview

Currently, all Discord notifications and interactions for every project flow through two global channels: `#control` (commands and chat) and `#notifications` (task updates and threads). As the number of projects grows, this single-channel model creates noise — unrelated project updates intermingle, making it hard to follow specific projects.

This plan adds **per-project Discord channel support**, enabling each project to have its own dedicated notification channel (and optionally its own control channel). Task threads, status updates, error reports, and completion notices will be routed to the correct project channel automatically.

---

## 1. Current Architecture Review

### 1.1 Discord Configuration (`src/config.py`)

The `DiscordConfig` dataclass defines a single, global set of channels:

```python
@dataclass
class DiscordConfig:
    bot_token: str = ""
    guild_id: str = ""
    channels: dict[str, str] = {
        "control": "control",
        "notifications": "notifications",
        "agent_questions": "agent-questions",   # configured but unused
    }
    authorized_users: list[str] = []
```

**Limitation:** There is no per-project channel mapping. All projects share the same channels.

### 1.2 Bot Channel Resolution (`src/discord/bot.py`)

At startup (`on_ready`), the bot resolves channel names to `discord.TextChannel` objects and caches them:

```python
self._control_channel: discord.TextChannel | None = None
self._notifications_channel: discord.TextChannel | None = None
```

These are wired into the Orchestrator via three callbacks:
- `set_notify_callback(self._send_notification)` — sends to `self._notifications_channel`
- `set_control_callback(self._send_control_message)` — sends to `self._control_channel`
- `set_create_thread_callback(self._create_task_thread)` — creates threads in `self._notifications_channel`

**Limitation:** All three callbacks reference a single, fixed channel. There is no routing by project.

### 1.3 Orchestrator Notification Flow (`src/orchestrator.py`)

The orchestrator sends notifications via:
- `_notify_channel(message)` — calls `self._notify` callback (single notifications channel)
- `_control_channel_post(message)` — calls `self._control_notify` callback (single control channel)
- `_create_thread(thread_name, initial_message)` — creates a thread in the notifications channel

All ~30+ notification call sites in the orchestrator are project-agnostic — they send to the same channel regardless of which project the task belongs to.

### 1.4 Data Model (`src/models.py`, `src/database.py`)

The `Project` model has no Discord-related fields:

```python
@dataclass
class Project:
    id: str
    name: str
    credit_weight: float = 1.0
    max_concurrent_agents: int = 2
    status: ProjectStatus = ProjectStatus.ACTIVE
    total_tokens_used: int = 0
    budget_limit: int | None = None
    workspace_path: str | None = None
    # No channel_id or discord fields
```

---

## 2. Design Decisions

### 2.1 Channel Mapping Strategy

Each project can optionally have its own Discord channel for notifications. Projects without a dedicated channel fall back to the global `#notifications` channel. This allows gradual adoption — you don't need to create channels for every project at once.

The mapping is stored in the `projects` database table (new columns), not in `config.yaml`, because:
- Projects are created/deleted dynamically at runtime
- Channel associations should be manageable via Discord commands (not config file edits)
- The config file retains the global defaults

### 2.2 Control Channel Approach

Two modes are supported for control/management:

1. **Shared control channel** (default) — the global `#control` channel serves all projects. This is simpler and recommended for most setups.
2. **Per-project control channel** (opt-in) — a project can designate its own control channel. Messages in that channel are scoped to the project's context by default.

### 2.3 Channel Discovery

Channels are resolved by **name** (matching the current approach) or by **Discord channel ID** (more robust). The implementation stores the channel ID once resolved, ensuring renames don't break routing.

---

## 3. Implementation Steps

### Step 1: Extend the Project Data Model

**Files:** `src/models.py`, `src/database.py`

Add two optional fields to the `Project` dataclass:

```python
@dataclass
class Project:
    # ... existing fields ...
    discord_channel_id: str | None = None       # Notifications channel ID
    discord_control_channel_id: str | None = None  # Optional control channel ID
```

Add a database migration in `Database.initialize()`:

```python
# In the migrations list:
"ALTER TABLE projects ADD COLUMN discord_channel_id TEXT",
"ALTER TABLE projects ADD COLUMN discord_control_channel_id TEXT",
```

Update `_row_to_project()` to include the new fields:

```python
def _row_to_project(self, row) -> Project:
    return Project(
        # ... existing fields ...
        discord_channel_id=row["discord_channel_id"] if "discord_channel_id" in row.keys() else None,
        discord_control_channel_id=row["discord_control_channel_id"] if "discord_control_channel_id" in row.keys() else None,
    )
```

### Step 2: Create Project-Aware Notification Routing in the Bot

**File:** `src/discord/bot.py`

Replace the single-channel notification approach with project-aware routing.

#### 2a. Add a Channel Cache

```python
class AgentQueueBot(commands.Bot):
    def __init__(self, config, orchestrator):
        # ... existing init ...
        self._project_channels: dict[str, discord.TextChannel] = {}  # project_id -> channel
        self._project_control_channels: dict[str, discord.TextChannel] = {}  # project_id -> channel
```

#### 2b. Add Channel Resolution Methods

```python
async def _resolve_project_channels(self) -> None:
    """Resolve per-project channels from database. Called on_ready and after channel changes."""
    guild = self.get_guild(int(self.config.discord.guild_id))
    if not guild:
        return

    projects = await self.orchestrator.db.list_projects()
    channel_cache = {ch.id: ch for ch in guild.text_channels}

    for project in projects:
        if project.discord_channel_id:
            ch = channel_cache.get(int(project.discord_channel_id))
            if ch:
                self._project_channels[project.id] = ch
            else:
                print(f"Warning: channel ID {project.discord_channel_id} not found for project {project.id}")

        if project.discord_control_channel_id:
            ch = channel_cache.get(int(project.discord_control_channel_id))
            if ch:
                self._project_control_channels[project.id] = ch
```

#### 2c. Add Project-Aware Send Methods

```python
def _get_notification_channel(self, project_id: str | None = None) -> discord.TextChannel | None:
    """Get the notification channel for a project, falling back to global."""
    if project_id and project_id in self._project_channels:
        return self._project_channels[project_id]
    return self._notifications_channel

def _get_control_channel(self, project_id: str | None = None) -> discord.TextChannel | None:
    """Get the control channel for a project, falling back to global."""
    if project_id and project_id in self._project_control_channels:
        return self._project_control_channels[project_id]
    return self._control_channel

async def _send_project_notification(self, text: str, project_id: str | None = None) -> None:
    """Send a notification to the appropriate project channel."""
    channel = self._get_notification_channel(project_id)
    if channel:
        await self._send_long_message(channel, text)

async def _send_project_control_message(self, text: str, project_id: str | None = None) -> None:
    """Send a control message to the appropriate project channel."""
    channel = self._get_control_channel(project_id)
    if channel:
        await self._send_long_message(channel, text)
```

#### 2d. Update Thread Creation to be Project-Aware

```python
async def _create_project_task_thread(
    self, thread_name: str, initial_message: str, project_id: str | None = None
):
    """Create a thread in the appropriate project notification channel."""
    channel = self._get_notification_channel(project_id)
    if not channel:
        print(f"Cannot create thread: no channel for project {project_id}")
        return None

    msg = await channel.send(f"**Agent working:** {thread_name}")
    thread = await msg.create_thread(name=thread_name)
    await thread.send(initial_message)

    async def send_to_thread(text: str) -> None:
        try:
            await self._send_long_message(thread, text)
        except Exception as e:
            print(f"Thread send error: {e}")

    async def notify_main_channel(text: str) -> None:
        try:
            await msg.reply(text)
        except Exception as e:
            print(f"Main channel notify error: {e}")
            try:
                await channel.send(text)
            except Exception as e2:
                print(f"Fallback notify error: {e2}")

    return send_to_thread, notify_main_channel
```

### Step 3: Update Orchestrator Callback Signatures

**File:** `src/orchestrator.py`

The orchestrator's notification callbacks must accept a `project_id` parameter for routing.

#### 3a. Update Callback Type Definitions

```python
# Change from:
NotifyCallback = Callable[[str], Awaitable[None]]

# To:
NotifyCallback = Callable[[str], Awaitable[None]]
ProjectNotifyCallback = Callable[[str, str | None], Awaitable[None]]  # (message, project_id)

# For thread creation:
# Change from:
CreateThreadCallback = Callable[[str, str], Awaitable[...]]
# To:
CreateThreadCallback = Callable[[str, str, str | None], Awaitable[...]]  # (name, msg, project_id)
```

#### 3b. Update `_notify_channel` and `_control_channel_post`

```python
async def _notify_channel(self, message: str, project_id: str | None = None) -> None:
    """Send a notification to the appropriate channel for the project."""
    if self._notify:
        try:
            await self._notify(message, project_id)
        except Exception as e:
            print(f"Notification error: {e}")

async def _control_channel_post(self, message: str, project_id: str | None = None) -> None:
    """Post a message to the control channel for the project."""
    if self._control_notify:
        try:
            await self._control_notify(message, project_id)
        except Exception as e:
            print(f"Control channel notification error: {e}")
```

#### 3c. Update All Notification Call Sites

Every call to `_notify_channel()` and `_control_channel_post()` in the orchestrator must pass the relevant `project_id`. This involves updating ~30 call sites. Key examples:

```python
# In _execute_task():
await self._notify_channel(start_msg, project_id=action.project_id)

# Thread creation:
thread_result = await self._create_thread(thread_name, start_msg, action.project_id)

# Task completion:
await self._control_channel_post(brief, project_id=action.project_id)

# Task failure:
await self._notify_channel(format_task_failed(task, agent, output), project_id=task.project_id)

# In stop_task():
await self._notify_channel(
    f"**Task Stopped:** `{task_id}` — {task.title}",
    project_id=task.project_id
)

# In _check_awaiting_approval():
await self._notify_channel(
    f"**PR Merged:** ...", project_id=task.project_id
)
```

The complete list of call sites to update (all in `orchestrator.py`):

| Method | Approx Line | Context |
|--------|-------------|---------|
| `stop_task` | ~102 | Task stopped notification |
| `_execute_task_safe` | ~217 | Task timeout notification |
| `_execute_task_safe` | ~234 | Execution error notification |
| `_execute_task` | ~590 | No adapter configured error |
| `_execute_task` | ~614 | Workspace error |
| `_execute_task` | ~630 | Task started |
| `_execute_task` | ~638 | Thread creation |
| `_execute_task` | ~741 | Rate limit notice |
| `_execute_task` | ~747 | Rate limit cleared |
| `_execute_task` | ~796-852 | Task completed (multiple) |
| `_execute_task` | ~875-924 | Task failed/blocked (multiple) |
| `_execute_task` | ~940 | Task paused |
| `_check_awaiting_approval` | ~575 | PR merged |
| `_check_awaiting_approval` | ~583 | PR closed |
| `_prepare_workspace` | ~309 | Repo not found |
| `_prepare_workspace` | ~334 | Linked repo missing |
| `_merge_and_push` | ~395 | Merge conflict |
| `_merge_and_push` | ~405 | Push failed |
| `_create_pr_for_task` | ~415 | Approval required |
| `_create_pr_for_task` | ~425 | Push failed |
| `_create_pr_for_task` | ~441 | PR creation failed |

### Step 4: Wire Up the New Callbacks in `on_ready`

**File:** `src/discord/bot.py`

Update the `on_ready` method to:

```python
async def on_ready(self) -> None:
    # ... existing guild/channel resolution ...

    # Resolve per-project channels
    await self._resolve_project_channels()

    # Wire up project-aware callbacks
    if self._notifications_channel:
        self.orchestrator.set_notify_callback(self._send_project_notification)
        self.orchestrator.set_create_thread_callback(self._create_project_task_thread)
    if self._control_channel:
        self.orchestrator.set_control_callback(self._send_project_control_message)
```

### Step 5: Update `on_message` for Per-Project Control Channels

**File:** `src/discord/bot.py`

The message handler must recognize per-project control channels:

```python
async def on_message(self, message: discord.Message) -> None:
    # ... existing filters ...

    # Check if this is a project-specific control channel
    project_context_id = None
    for project_id, ch in self._project_control_channels.items():
        if message.channel.id == ch.id:
            is_control = True
            project_context_id = project_id
            break

    # When routing to ChatAgent, inject project context
    if project_context_id:
        user_text = (
            f"[Context: messages in this channel are scoped to project "
            f"`{project_context_id}`. Default project_id='{project_context_id}' "
            f"for all operations.]\n{text}"
        )
```

### Step 6: Add New Slash Commands

**File:** `src/discord/commands.py`

#### 6a. `/set-channel` Command

Associates a Discord channel with a project for notifications.

```python
@bot.tree.command(
    name="set-channel",
    description="Set the notification channel for a project"
)
@app_commands.describe(
    project_id="Project ID",
    channel="Discord channel to use for this project's notifications",
    channel_type="Which channel role to set"
)
@app_commands.choices(channel_type=[
    app_commands.Choice(name="notifications", value="notifications"),
    app_commands.Choice(name="control", value="control"),
])
async def set_channel_command(
    interaction: discord.Interaction,
    project_id: str,
    channel: discord.TextChannel,
    channel_type: app_commands.Choice[str] = None,
):
    db = bot.orchestrator.db
    project = await db.get_project(project_id)
    if not project:
        await interaction.response.send_message(
            f"Project `{project_id}` not found.", ephemeral=True
        )
        return

    ch_type = channel_type.value if channel_type else "notifications"

    if ch_type == "notifications":
        await db.update_project(project_id, discord_channel_id=str(channel.id))
        bot._project_channels[project_id] = channel
    else:
        await db.update_project(project_id, discord_control_channel_id=str(channel.id))
        bot._project_control_channels[project_id] = channel

    await interaction.response.send_message(
        f"Project **{project.name}** {ch_type} channel set to {channel.mention}."
    )
```

#### 6b. `/create-channel` Command

Creates a new Discord channel for a project and associates it automatically.

```python
@bot.tree.command(
    name="create-channel",
    description="Create a new Discord channel for a project"
)
@app_commands.describe(
    project_id="Project ID",
    channel_name="Channel name (default: project name)",
    category="Category to create the channel in (optional)",
)
async def create_channel_command(
    interaction: discord.Interaction,
    project_id: str,
    channel_name: str | None = None,
    category: discord.CategoryChannel | None = None,
):
    db = bot.orchestrator.db
    project = await db.get_project(project_id)
    if not project:
        await interaction.response.send_message(
            f"Project `{project_id}` not found.", ephemeral=True
        )
        return

    await interaction.response.defer()

    name = channel_name or f"aq-{project.id}"
    guild = interaction.guild

    # Create the channel
    channel = await guild.create_text_channel(
        name=name,
        category=category,
        topic=f"Agent Queue notifications for project: {project.name} ({project.id})",
    )

    # Associate it with the project
    await db.update_project(project_id, discord_channel_id=str(channel.id))
    bot._project_channels[project_id] = channel

    await interaction.followup.send(
        f"Created {channel.mention} and linked to project **{project.name}**.\n"
        f"All task notifications for this project will now appear here."
    )
```

### Step 7: Add ChatAgent Tools for Channel Management

**File:** `src/chat_agent.py`

Add new tools to the `TOOLS` list and handle them in the tool execution method:

#### 7a. Tool Definitions

```python
{
    "name": "set_control_interface",
    "description": (
        "Set the Discord channel for a project's notifications or control interface. "
        "This makes task updates, thread creation, and notifications appear in "
        "the specified channel instead of the global default."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "Project ID to configure",
            },
            "channel_name": {
                "type": "string",
                "description": "Discord channel name to use (e.g. 'my-project-notifications')",
            },
            "channel_type": {
                "type": "string",
                "enum": ["notifications", "control"],
                "description": "Which channel role to set (default: notifications)",
                "default": "notifications",
            },
        },
        "required": ["project_id", "channel_name"],
    },
},
{
    "name": "create_channel_for_project",
    "description": (
        "Create a new Discord channel for a project and automatically link it "
        "as the project's notification channel. The channel will be created in "
        "the configured guild."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "Project ID to create a channel for",
            },
            "channel_name": {
                "type": "string",
                "description": "Name for the new channel (default: 'aq-{project_id}')",
            },
        },
        "required": ["project_id"],
    },
},
{
    "name": "get_project_channel",
    "description": "Get the Discord channel configuration for a project.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "Project ID to check",
            },
        },
        "required": ["project_id"],
    },
},
```

#### 7b. Tool Handlers

These tools require access to the Discord bot instance. The `ChatAgent` will need a reference to the bot:

```python
# In ChatAgent.__init__:
self._bot = None  # Set after bot initialization

# New method to set the bot reference:
def set_bot(self, bot) -> None:
    self._bot = bot
```

Tool handler implementations:

```python
elif name == "set_control_interface":
    pid = input_data["project_id"]
    channel_name = input_data["channel_name"]
    channel_type = input_data.get("channel_type", "notifications")

    project = await db.get_project(pid)
    if not project:
        return {"error": f"Project '{pid}' not found"}

    if not self._bot:
        return {"error": "Bot not initialized — cannot resolve channels"}

    guild = self._bot.get_guild(int(self._bot.config.discord.guild_id))
    if not guild:
        return {"error": "Guild not found"}

    # Find channel by name
    channel = discord.utils.get(guild.text_channels, name=channel_name)
    if not channel:
        return {"error": f"Channel '{channel_name}' not found in guild"}

    if channel_type == "notifications":
        await db.update_project(pid, discord_channel_id=str(channel.id))
        self._bot._project_channels[pid] = channel
    else:
        await db.update_project(pid, discord_control_channel_id=str(channel.id))
        self._bot._project_control_channels[pid] = channel

    return {
        "project": pid,
        "channel": channel_name,
        "channel_id": str(channel.id),
        "channel_type": channel_type,
        "status": "linked",
    }

elif name == "create_channel_for_project":
    pid = input_data["project_id"]
    project = await db.get_project(pid)
    if not project:
        return {"error": f"Project '{pid}' not found"}

    if not self._bot:
        return {"error": "Bot not initialized — cannot create channels"}

    guild = self._bot.get_guild(int(self._bot.config.discord.guild_id))
    if not guild:
        return {"error": "Guild not found"}

    channel_name = input_data.get("channel_name") or f"aq-{pid}"

    channel = await guild.create_text_channel(
        name=channel_name,
        topic=f"Agent Queue notifications for project: {project.name} ({pid})",
    )

    await db.update_project(pid, discord_channel_id=str(channel.id))
    self._bot._project_channels[pid] = channel

    return {
        "project": pid,
        "channel": channel.name,
        "channel_id": str(channel.id),
        "status": "created_and_linked",
    }

elif name == "get_project_channel":
    pid = input_data["project_id"]
    project = await db.get_project(pid)
    if not project:
        return {"error": f"Project '{pid}' not found"}

    result = {"project": pid, "project_name": project.name}
    if project.discord_channel_id:
        ch = self._bot._project_channels.get(pid) if self._bot else None
        result["notifications_channel"] = {
            "id": project.discord_channel_id,
            "name": ch.name if ch else "unknown",
        }
    else:
        result["notifications_channel"] = "global (default)"

    if project.discord_control_channel_id:
        ch = self._bot._project_control_channels.get(pid) if self._bot else None
        result["control_channel"] = {
            "id": project.discord_control_channel_id,
            "name": ch.name if ch else "unknown",
        }
    else:
        result["control_channel"] = "global (default)"

    return result
```

### Step 8: Update the `/status` and `/projects` Commands

**File:** `src/discord/commands.py`

Update `/projects` to show channel info:

```python
@bot.tree.command(name="projects", description="List all projects")
async def projects_command(interaction: discord.Interaction):
    projects = await bot.orchestrator.db.list_projects()
    if not projects:
        await interaction.response.send_message("No projects configured.")
        return
    lines = []
    for p in projects:
        ch_info = ""
        if p.discord_channel_id:
            ch_info = f" | channel: <#{p.discord_channel_id}>"
        lines.append(
            f"- **{p.name}** (`{p.id}`) — {p.status.value}, "
            f"weight={p.credit_weight}{ch_info}"
        )
    await interaction.response.send_message("\n".join(lines))
```

### Step 9: Update the `create_project` ChatAgent Tool

**File:** `src/chat_agent.py`

When creating a project through the chat interface, optionally auto-create a channel:

```python
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

    result = {"created": project_id, "name": project.name, "workspace": workspace}

    # Auto-create channel if requested
    if input_data.get("create_channel", False) and self._bot:
        try:
            guild = self._bot.get_guild(int(self._bot.config.discord.guild_id))
            if guild:
                channel = await guild.create_text_channel(
                    name=f"aq-{project_id}",
                    topic=f"Agent Queue notifications for: {project.name}",
                )
                await db.update_project(project_id, discord_channel_id=str(channel.id))
                self._bot._project_channels[project_id] = channel
                result["channel"] = channel.name
                result["channel_id"] = str(channel.id)
        except Exception as e:
            result["channel_error"] = str(e)

    return result
```

Add `create_channel` to the `create_project` tool schema:

```python
"create_channel": {
    "type": "boolean",
    "description": "Auto-create a Discord channel for this project",
    "default": False,
},
```

### Step 10: Update Tests

**File:** `tests/test_discord_commands.py` (and new test files)

#### 10a. Unit Tests for Channel Routing

```python
# tests/test_channel_routing.py

async def test_project_notification_routing():
    """Notifications for a project with a channel go to that channel."""
    ...

async def test_fallback_to_global_channel():
    """Projects without a dedicated channel fall back to global."""
    ...

async def test_thread_creation_in_project_channel():
    """Task threads are created in the project's channel, not global."""
    ...

async def test_set_channel_command():
    """The /set-channel command links a channel to a project."""
    ...

async def test_create_channel_command():
    """The /create-channel command creates and links a channel."""
    ...
```

#### 10b. Update Existing Orchestrator Tests

Update `tests/test_orchestrator.py` to pass `project_id` in mock notification callbacks:

```python
# Mock callbacks need to accept project_id parameter
async def mock_notify(msg, project_id=None):
    notifications.append((msg, project_id))
```

---

## 4. Migration & Backwards Compatibility

### 4.1 Database Migration

The migration is non-destructive — new nullable columns are added:

```sql
ALTER TABLE projects ADD COLUMN discord_channel_id TEXT;
ALTER TABLE projects ADD COLUMN discord_control_channel_id TEXT;
```

Existing projects will have `NULL` values, meaning they use the global channels.

### 4.2 Callback Signature Compatibility

The updated callback signatures add an optional `project_id` parameter. The old single-channel behavior is preserved as the default when `project_id` is `None`.

### 4.3 Config File

No changes to `config.yaml` are required. The global `channels` config continues to work as before. Per-project channels are managed entirely via Discord commands and the database.

---

## 5. File Change Summary

| File | Change Type | Description |
|------|------------|-------------|
| `src/models.py` | Modify | Add `discord_channel_id` and `discord_control_channel_id` to `Project` |
| `src/database.py` | Modify | Add migration, update `_row_to_project()` |
| `src/discord/bot.py` | Modify | Add project channel cache, project-aware routing methods, update `on_ready`, `on_message`, thread creation |
| `src/discord/commands.py` | Modify | Add `/set-channel`, `/create-channel`, update `/projects` |
| `src/orchestrator.py` | Modify | Update callback type defs, update `_notify_channel`, `_control_channel_post`, update ~30 notification call sites to pass `project_id` |
| `src/chat_agent.py` | Modify | Add `set_control_interface`, `create_channel_for_project`, `get_project_channel` tools; add `_bot` reference; update `create_project` tool |
| `tests/test_channel_routing.py` | New | Unit tests for multi-channel routing |
| `tests/test_discord_commands.py` | Modify | Update for new commands and callback signatures |
| `tests/test_orchestrator.py` | Modify | Update mock callbacks to accept `project_id` |

---

## 6. User Workflow Examples

### Example 1: Set Up a Channel for an Existing Project

```
User: @AgentQueue set the channel for project "my-web-app" to #my-web-app-tasks
Bot:  (calls set_control_interface tool)
Bot:  Project "my-web-app" notifications channel set to #my-web-app-tasks.
      All task updates for this project will now appear there.
```

### Example 2: Create a New Project with a Channel

```
User: @AgentQueue create a project called "mobile-app" with its own channel
Bot:  (calls create_project with create_channel=True)
Bot:  Created project "mobile-app" with channel #aq-mobile-app.
```

### Example 3: Using Slash Commands

```
/create-channel project_id:my-web-app
→ Created #aq-my-web-app and linked to project my-web-app.

/set-channel project_id:my-web-app channel:#existing-channel channel_type:notifications
→ Project my-web-app notifications channel set to #existing-channel.
```

### Example 4: Task Notifications Route Correctly

After setup, when a task for "my-web-app" starts:
- Thread created in `#aq-my-web-app` (not `#notifications`)
- Task started/completed/failed messages appear in `#aq-my-web-app`
- Control summaries go to the project's control channel (or global `#control`)

---

## 7. Implementation Order

The recommended implementation order to minimize risk:

1. **Step 1** — Data model changes (low risk, additive)
2. **Step 3** — Orchestrator callback signatures (foundational)
3. **Step 2** — Bot channel routing (depends on Step 3)
4. **Step 4** — Wire callbacks in `on_ready`
5. **Step 5** — `on_message` updates for control channels
6. **Step 6** — New slash commands
7. **Step 7** — ChatAgent tools
8. **Step 8** — `/status` and `/projects` display updates
9. **Step 9** — Auto-channel creation in `create_project`
10. **Step 10** — Tests

---

## 8. Future Enhancements (Out of Scope)

These are not part of this implementation but could be added later:

- **Per-project agent-questions channel** — Route agent questions to a project-specific channel
- **Channel categories** — Auto-create a category per project with sub-channels (notifications, control, questions)
- **Webhook fallback** — Support posting to external webhooks for projects not on the same Discord server
- **Channel permissions** — Auto-set Discord permissions so only relevant team members see project channels
- **Cross-guild support** — Allow projects to notify channels in different Discord guilds
