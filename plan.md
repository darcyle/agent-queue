# Audit: Current Bot Configuration for Multi-Channel Support

## Summary

This document validates the existing per-project channel infrastructure and identifies specific gaps that need to be addressed to fully implement multi-channel support with automatic channel creation, channel-context-aware project resolution, and updated setup wizard support.

---

## Existing Infrastructure (Validated)

### 1. Config Layer (`src/config.py`)
- **`DiscordConfig`** defines three global channel names: `control`, `notifications`, `agent_questions` (lines 14-18).
- These are string channel *names* (not IDs) resolved at bot startup by matching against guild text channels.
- No per-project channel configuration exists at the config file level (by design -- per-project channels are stored in the database).

### 2. Data Model (`src/models.py`)
- **`Project` dataclass** (line 85) already includes:
  - `discord_channel_id: str | None` -- per-project notifications channel (line 94)
  - `discord_control_channel_id: str | None` -- per-project control channel (line 95)
- These store Discord channel IDs as strings (converted to `int` for Discord API calls).

### 3. Database Layer (`src/database.py`)
- **`projects` table** schema (line 15) includes `discord_channel_id TEXT` and `discord_control_channel_id TEXT` columns.
- **Migration support** (lines 200-201): `ALTER TABLE` migrations ensure existing databases get these columns.
- **`create_project()`** (line 215) persists both channel IDs.
- **`_row_to_project()`** (line 264) reads both channel IDs with safe key-existence checks.
- **`update_project()`** (line 250) is generic and supports updating any project field including channel IDs.

### 4. Bot Channel Resolution (`src/discord/bot.py`)
- **Per-project channel caches** (lines 32-33):
  - `_project_channels: dict[str, discord.TextChannel]` -- project_id -> notifications channel
  - `_project_control_channels: dict[str, discord.TextChannel]` -- project_id -> control channel
- **`_resolve_project_channels()`** (line 197): On startup, loads all projects from DB, resolves channel IDs to `discord.TextChannel` objects, and populates caches.
- **`_get_notification_channel(project_id)`** (line 231): Returns project-specific channel or falls back to global.
- **`_get_control_channel(project_id)`** (line 239): Returns project-specific channel or falls back to global.
- **`update_project_channel()`** (line 67): Runtime cache update called after `/set-channel` or `/create-channel`.
- **`_send_notification()`** and **`_send_control_message()`** (lines 247, 253): Both accept `project_id` and route accordingly.
- **`_create_task_thread()`** (line 259): Creates threads in the project-specific notifications channel.

### 5. Bot Message Handling (`src/discord/bot.py` `on_message`)
- **Global control channel detection** (line 328-331): Checks if message is in the global `_control_channel`.
- **Per-project control channel detection** (lines 333-338): Iterates `_project_control_channels` to find matching `project_control_id`.
- **Context injection** (lines 369-376): When a message comes from a project control channel, prepends context like `[Context: this is the control channel for project 'foo'...]` to the user message so the LLM defaults to that project.
- **Notes thread detection** (lines 341-342): Uses `_notes_threads` dict for thread-to-project mapping.

### 6. Orchestrator Notification Routing (`src/orchestrator.py`)
- **`_notify_channel(message, project_id)`** (line 115): Passes `project_id` to the callback, enabling per-project routing.
- **`_control_channel_post(message, project_id)`** (line 128): Same pattern for control channel messages.
- **All notification call sites** consistently pass `project_id=action.project_id` (verified: ~30+ call sites).

### 7. Slash Commands (`src/discord/commands.py`)
- **`/set-channel`** (line 342): Links an existing Discord channel to a project. Takes `project_id`, `channel` (TextChannel picker), and `channel_type` (notifications/control). Updates DB and bot cache.
- **`/create-channel`** (line 378): Creates a new Discord channel and links it. Takes `project_id`, optional `channel_name`, `channel_type`, and `category`. Creates the channel via Discord API, then calls `set_project_channel` handler and updates bot cache.

### 8. Command Handler (`src/command_handler.py`)
- **`_cmd_set_project_channel()`** (line 197): Unified handler for linking channels. Validates project exists, validates channel_type, updates DB.
- **`_cmd_get_project_channels()`** (line 221): Returns configured channel IDs for a project.

### 9. Chat Agent Tools (`src/chat_agent.py`)
- **`set_project_channel`** tool (line 77): LLM tool definition for linking channels. Requires `project_id` and `channel_id`.
- **`get_project_channels`** tool (line 102): LLM tool to query project channel configuration.
- **System prompt** (lines 715-723): Documents per-project channel behavior for the LLM.

### 10. Setup Wizard (`setup_wizard.py`)
- **Step 2 (Discord)** (line 220): Configures global bot token, guild ID, and channel names.
- Tests connectivity and verifies global channels exist.
- Configures authorized users.
- **No per-project channel setup** -- only handles global `control`, `notifications`, and `agent_questions` channels.

---

## Identified Gaps

### Gap 1: No Reverse Lookup (Channel -> Project)
**Current state:** The bot maintains `project_id -> channel` mappings but has no `channel_id -> project_id` index.

**Where it matters:**
- In `on_message()` (bot.py line 333-338), the bot iterates ALL project control channels to find a match: `for pid, ch in self._project_control_channels.items()`. This is O(n) per message.
- There is no equivalent iteration for notification channels -- if a user sends a message in a project's *notification* channel, the bot won't recognize it at all.
- Slash commands that require `project_id` (like `/create-task`, `/list-tasks`, etc.) cannot auto-infer the project from the channel they're executed in.

**What's needed:** A reverse mapping `dict[int, str]` -- `channel_id -> project_id` -- covering both notification and control channels. This should be maintained alongside the existing caches and updated whenever `update_project_channel()` is called.

### Gap 2: No `set_control_interface` Command by Channel Name
**Current state:** The `/set-channel` command takes a Discord `TextChannel` object (channel picker). The `set_project_channel` chat agent tool takes a `channel_id` string.

**What's missing:** There is no command variant that accepts a channel *name* (string) instead of a channel object/ID. This would be useful for:
- Scripted/programmatic setup where you know the channel name but not the ID.
- The setup wizard or CLI tools that don't have access to Discord's channel picker.

**Where to add:** A new command handler method (or parameter on the existing one) and corresponding chat agent tool.

### Gap 3: No Idempotent `create_channel_for_project` Command
**Current state:** The `/create-channel` command (commands.py line 378) always creates a new channel. It does not check if a channel with the same name already exists.

**What's missing:** An idempotent variant that:
1. Checks if a channel with the given name already exists in the guild.
2. If it exists, links it to the project (like `/set-channel`).
3. If it doesn't exist, creates it and links it.

This is important for:
- Preventing duplicate channels when setup is re-run.
- Automation scripts that may be run multiple times.
- The enhanced setup wizard flow.

**Where to fix:** The `create_channel_command` function in `commands.py` (line 392) and the underlying handler logic.

### Gap 4: Setup Wizard Lacks Per-Project Channel Configuration
**Current state:** `setup_wizard.py` Step 2 (line 220, `step_discord()`) only configures:
- Bot token
- Guild ID
- Global channel names (`control`, `notifications`, `agent_questions`)
- Authorized users

**What's missing:** After project creation (or as a separate step), the wizard should offer:
1. Option to create/link per-project Discord channels.
2. Auto-creation of notification and control channels for each project.
3. Guidance on the per-project channel naming convention.

**Where to add:** A new step in the wizard (after the project-creation step) or an extension to the existing Discord step.

### Gap 5: Slash Commands Don't Auto-Infer `project_id` from Channel Context
**Current state:** Most slash commands (e.g., `/create-task`, `/list-tasks`, `/status`) require an explicit `project_id` parameter. When a user runs a command in a project-specific channel, they still need to type the project ID manually.

**Where it matters:** In `commands.py`, commands like:
- `/create-task` (requires `project_id`)
- `/list-tasks` (requires `project_id`)
- `/pause` / `/resume` (requires `project_id`)
- `/status` (optionally takes `project_id`)

**What's needed:** A helper function that resolves `project_id` from the interaction's channel. If the command is run in a channel mapped to a project (either notification or control), the project_id should be auto-filled when not explicitly provided by the user. This requires Gap 1 (reverse lookup) to be implemented first.

---

## Dependency Graph for Implementation

```
Gap 1 (Reverse Lookup)
  |-- Gap 5 (Auto-infer project_id) -- depends on Gap 1
  |-- Gap 3 (Idempotent create) -- partially depends on Gap 1 for enriched behavior

Gap 2 (set by name) -- independent
Gap 4 (Setup wizard) -- depends on Gap 3 (uses idempotent create)
```

**Recommended implementation order:**
1. **Gap 1** -- Add reverse lookup `_channel_to_project: dict[int, str]`
2. **Gap 3** -- Make `/create-channel` idempotent (check-before-create)
3. **Gap 2** -- Add name-based channel setting command
4. **Gap 5** -- Add auto-inference helper and integrate into slash commands
5. **Gap 4** -- Extend setup wizard with per-project channel step

---

## Files to Modify (by gap)

| Gap | Files |
|-----|-------|
| 1 - Reverse Lookup | `src/discord/bot.py` |
| 2 - Set by Name | `src/command_handler.py`, `src/chat_agent.py`, `src/discord/commands.py` |
| 3 - Idempotent Create | `src/discord/commands.py`, `src/command_handler.py` |
| 4 - Setup Wizard | `setup_wizard.py` |
| 5 - Auto-infer project_id | `src/discord/bot.py`, `src/discord/commands.py` |
