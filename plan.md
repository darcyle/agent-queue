# Multi-Channel Infrastructure: Validation & Gap Analysis

This document validates the existing per-project channel infrastructure and identifies specific gaps that need to be addressed to fully implement multi-channel support with automatic channel creation, channel-context-aware project resolution, and updated setup wizard support.

---

## What Already Exists (Validated ✅)

### Database Layer
- **Schema**: `projects` table has `discord_channel_id` (notifications) and `discord_control_channel_id` (control) columns — both in `CREATE TABLE` and in migration `ALTER TABLE` statements for existing databases (`src/database.py:24-25`, `src/database.py:200-201`)
- **CRUD**: `create_project()`, `update_project()`, and `_row_to_project()` all handle both channel ID fields (`src/database.py:215-277`)
- **Migrations**: Backward-compatible `ALTER TABLE` migrations exist for both columns (`src/database.py:200-201`)

### Data Model
- **Project dataclass**: Includes `discord_channel_id: str | None` and `discord_control_channel_id: str | None` (`src/models.py:94-95`)

### Bot Channel Resolution & Routing
- **Per-project caches**: `_project_channels: dict[str, discord.TextChannel]` and `_project_control_channels: dict[str, discord.TextChannel]` on `AgentQueueBot` (`src/discord/bot.py:32-33`)
- **Startup resolution**: `_resolve_project_channels()` reads all projects from DB, looks up Discord channels by ID, caches them, and logs warnings for missing channels (`src/discord/bot.py:197-229`)
- **Fallback routing**: `_get_notification_channel(project_id)` returns project channel if set, falls back to global; same for `_get_control_channel()` (`src/discord/bot.py:231-245`)
- **Notification routing**: `_send_notification()`, `_send_control_message()`, and `_create_task_thread()` all accept `project_id` and route through the resolution methods (`src/discord/bot.py:247-307`)
- **Runtime cache update**: `update_project_channel()` method updates caches immediately after `/set-channel` or `/create-channel` without requiring restart (`src/discord/bot.py:67-78`)

### Orchestrator Integration
- **Callbacks pass project_id**: `_notify()`, `_control_notify()`, and `_create_thread()` all receive and forward `project_id` (`src/orchestrator.py:124, 135, 667`)

### Message Listener (Natural Language)
- **Per-project control channel listening**: `on_message()` iterates `_project_control_channels` to detect messages in project control channels and injects project context into the LLM prompt (`src/discord/bot.py:328-383`)
- **Context injection**: When a message arrives in a project control channel, the bot prepends `[Context: this is the control channel for project '{pid}'...]` to the user text, so the LLM defaults to that project for scoped commands (`src/discord/bot.py:370-376`)

### Slash Commands for Channel Management
- **`/set-channel`**: Links an existing Discord channel to a project (notifications or control), updates DB and bot cache (`src/discord/commands.py:342-376`)
- **`/create-channel`**: Creates a new Discord text channel, links it to a project, updates DB and bot cache (`src/discord/commands.py:378-452`)
- **`/projects`**: Displays per-project channel info inline (shows `<#channel_id>` mentions) (`src/discord/commands.py:191-209`)

### Chat Agent (LLM) Tools
- **`set_project_channel`**: Tool definition accepts `project_id`, `channel_id`, `channel_type` (`src/chat_agent.py:77-100`)
- **`get_project_channels`**: Tool definition returns notifications and control channel IDs for a project (`src/chat_agent.py:102-110`)

### Command Handler
- **`_cmd_set_project_channel`**: Validates project, stores channel_id in DB (`src/command_handler.py:197-219`)
- **`_cmd_get_project_channels`**: Returns both channel IDs from project record (`src/command_handler.py:221-231`)
- **`_cmd_list_projects`**: Includes channel IDs in response when set (`src/command_handler.py:131-148`)

### System Prompt Documentation
- **Per-project channels section**: System prompt documents the feature, including fallback behavior, `/set-channel` and `/create-channel` commands, and `get_project_channels` tool (`src/chat_agent.py:715-722`)

---

## Identified Gaps

### Gap 1: No Reverse Channel→Project Lookup

**Current state**: The bot can route _from_ project → channel (via `_project_channels` and `_project_control_channels` dicts), but there is no efficient _reverse_ lookup from channel_id → project_id.

**Where it matters**: When a slash command like `/status`, `/tasks`, or `/add-task` is invoked inside a per-project control channel, the bot has no way to automatically infer the project_id from the channel context. Users must always explicitly specify `project_id`.

**Current workaround**: The `on_message()` handler does a linear scan of `_project_control_channels.items()` to find the project_id (`src/discord/bot.py:333-337`). This works for natural language but slash commands don't go through `on_message()`.

**What's needed**: A reverse-lookup dict (`_channel_to_project: dict[int, str]`) populated alongside the forward caches during `_resolve_project_channels()` and updated in `update_project_channel()`. Slash commands would then call a helper like `_infer_project_from_channel(interaction.channel_id)` when no explicit `project_id` is provided.

**Affected files**: `src/discord/bot.py`, `src/discord/commands.py`

### Gap 2: Slash Commands Don't Auto-Infer Project from Channel Context

**Current state**: Slash commands that accept `project_id` (e.g., `/tasks`, `/status`, `/add-task`, `/pause`, `/resume`) always require the user to type it explicitly, even when the command is issued in a project's dedicated control channel.

**Where it matters**: This defeats the UX purpose of per-project channels. A user in `#my-app-control` expects that `/tasks` would show tasks for `my-app` without having to specify `project_id=my-app`.

**What's needed**: For each slash command that accepts an optional `project_id`, add fallback logic:
1. If `project_id` is provided → use it (explicit always wins)
2. Else if the command was issued in a per-project control channel → infer from reverse lookup
3. Else if the command was issued in a per-project notifications channel → infer from reverse lookup
4. Else if `_active_project_id` is set → use it
5. Else → omit (global scope) or error

**Affected commands**: `/tasks`, `/add-task`, `/status` (if project-filtered), `/pause`, `/resume`, `/create-project` (auto-link?), `/task-result`, `/task-diff`

**Affected files**: `src/discord/commands.py`, `src/discord/bot.py` (for the inference helper)

### Gap 3: No Idempotent/Upsert Channel Creation

**Current state**: `/create-channel` always calls `guild.create_text_channel()` — it creates a new channel even if one with the same name already exists (`src/discord/commands.py:420-424`).

**Where it matters**: If a user runs `/create-channel my-app` twice, they get two `#my-app` channels. If the system is configured to auto-create channels on project creation, it must be safe to re-run. Idempotency is critical for automation (hooks, wizard re-runs).

**What's needed**: Before creating, check `guild.text_channels` for an existing channel with the target name (optionally in the same category). If found, reuse it; if not, create. Return the channel either way.

**Affected files**: `src/discord/commands.py` (the `create_channel_command` function)

### Gap 4: No Automatic Channel Creation on Project Creation

**Current state**: When a project is created via `/create-project` or the `create_project` LLM tool, no Discord channels are created. Channel setup is a separate manual step.

**Where it matters**: For a streamlined multi-project workflow, users expect that creating a project also sets up its dedicated Discord channels (at minimum a notifications channel, optionally a control channel).

**What's needed**: An option (configurable, not forced) on project creation to auto-create and link Discord channels. This could be:
- A flag on `/create-project` like `auto_channels: bool = True`
- A config setting `discord.auto_create_project_channels: true`
- The LLM tool `create_project` gaining a `create_channels` parameter

The auto-creation should use idempotent logic (see Gap 3) and optionally place channels in a configured category.

**Affected files**: `src/discord/commands.py`, `src/command_handler.py`, `src/config.py` (for the config flag)

### Gap 5: Setup Wizard Has No Per-Project Channel Configuration

**Current state**: The setup wizard (`setup_wizard.py`) only configures global channels: `control`, `notifications`, `agent_questions`. It has no concept of per-project channels.

**Where it matters**: New users running the wizard for the first time (or re-running it to add a project) have no guided path to set up per-project channels. They must know to use `/set-channel` or `/create-channel` after setup.

**What's needed**: An optional wizard step (after the Discord connectivity test) that:
1. Asks "Would you like to set up per-project channels?"
2. If yes, asks for project names and creates/links channels
3. Optionally lets users pick a Discord category for project channels
4. Saves per-project channel config (either by calling the DB directly or by generating commands to run after bot start)

Note: This is a lower priority since the interactive CLI wizard runs before the bot starts and thus cannot call the Discord API to create channels. It may be better addressed by adding a post-start `/setup-channels` command or by documenting the workflow in the wizard output.

**Affected files**: `setup_wizard.py`

### Gap 6: No `set_channel_by_name` Variant

**Current state**: The `/set-channel` slash command takes a `discord.TextChannel` object (Discord handles name resolution). The LLM tool `set_project_channel` requires a raw `channel_id` string. There's no way to set a channel by name via natural language (e.g., "set notifications to #my-app-tasks").

**Where it matters**: In the chat agent flow, the user says "set the notifications channel for my-app to #my-app-notifications", but the LLM must provide a numeric channel_id. The LLM has no tool to resolve a channel name to an ID.

**What's needed**: Either:
- A new LLM tool `resolve_channel_name` that maps `#channel-name` → `channel_id`
- Modify `set_project_channel` to accept either `channel_id` or `channel_name`, with the command handler resolving the name via the bot's guild
- The chat agent's context injection could include a list of available channel names/IDs

**Affected files**: `src/chat_agent.py`, `src/command_handler.py`, possibly `src/discord/bot.py`

---

## Summary Table

| # | Gap | Priority | Complexity | Affected Files |
|---|-----|----------|------------|----------------|
| 1 | No reverse channel→project lookup | High | Low | `bot.py` |
| 2 | Slash commands don't auto-infer project from channel | High | Medium | `commands.py`, `bot.py` |
| 3 | No idempotent channel creation | Medium | Low | `commands.py` |
| 4 | No auto-channel creation on project creation | Medium | Medium | `commands.py`, `command_handler.py`, `config.py` |
| 5 | Setup wizard lacks per-project channel config | Low | Medium | `setup_wizard.py` |
| 6 | No `set_channel_by_name` for LLM tool | Low | Low | `chat_agent.py`, `command_handler.py` |

---

## Recommended Implementation Order

1. **Gap 1** (reverse lookup) — foundational, required by Gap 2
2. **Gap 2** (auto-infer project from channel) — highest user-facing impact
3. **Gap 3** (idempotent creation) — required by Gap 4
4. **Gap 4** (auto-create channels on project creation) — major workflow improvement
5. **Gap 6** (channel name resolution for LLM) — quality-of-life
6. **Gap 5** (setup wizard update) — lowest priority, alternative is documentation
