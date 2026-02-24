# Multi-Channel Discord Support — Implementation Plan

Enable each project to have its own dedicated Discord channel(s) for notifications and management, with automatic channel creation, channel-context-aware project resolution, and updated setup wizard support.

---

## 1. Review & Audit Current Bot Configuration

**Goal:** Validate existing infrastructure and identify gaps.

**Current State (already in place):**
- `DiscordConfig` in `src/config.py` defines global channels: `control`, `notifications`, `agent_questions`.
- `Project` model in `src/models.py` already has `discord_channel_id` and `discord_control_channel_id` fields.
- `projects` table in `src/database.py` already has `discord_channel_id` and `discord_control_channel_id` columns.
- `AgentQueueBot` in `src/discord/bot.py` already maintains per-project channel caches (`_project_channels`, `_project_control_channels`) and routing methods (`_get_notification_channel`, `_get_control_channel`).
- Existing `/set-channel` and `/create-channel` slash commands in `src/discord/commands.py` link channels to projects.
- `_resolve_project_channels()` loads per-project channels from DB on startup.
- Notification routing in `orchestrator.py` already passes `project_id` to `_notify_channel()`.

**Gaps to address:**
- No reverse lookup from channel → project (needed for channel-context project resolution).
- No `set_control_interface` command that takes a channel *name* instead of a channel object.
- No `create_channel_for_project` command that auto-creates only if the channel doesn't already exist.
- Setup wizard doesn't guide users through per-project channel configuration.
- Commands that require `project_id` don't auto-infer it from the channel context.

**Files involved:** `src/config.py`, `src/models.py`, `src/database.py`, `src/discord/bot.py`, `src/discord/commands.py`, `src/command_handler.py`, `setup_wizard.py`

**No code changes needed in this step** — this is an audit/review step.

---

## 2. Add Channel-to-Project Reverse Lookup to Bot

**Goal:** Build and maintain a reverse mapping from `channel_id → project_id` so the bot can infer which project a command is being issued from based on the channel context.

### Changes to `src/discord/bot.py`:

1. **Add a new reverse-lookup dictionary** to `AgentQueueBot.__init__`:
   ```python
   self._channel_to_project: dict[int, str] = {}  # channel_id -> project_id
   ```

2. **Populate the reverse lookup in `_resolve_project_channels()`**:
   After caching per-project channels, also populate the reverse map:
   ```python
   # In _resolve_project_channels(), after caching each channel:
   if ch and isinstance(ch, discord.TextChannel):
       self._project_channels[project.id] = ch
       self._channel_to_project[ch.id] = project.id  # NEW
   # Same for control channels
   if ch and isinstance(ch, discord.TextChannel):
       self._project_control_channels[project.id] = ch
       self._channel_to_project[ch.id] = project.id  # NEW
   ```

3. **Update `update_project_channel()` to maintain reverse lookup**:
   ```python
   def update_project_channel(self, project_id, channel, channel_type="notifications"):
       if channel_type == "control":
           self._project_control_channels[project_id] = channel
       else:
           self._project_channels[project_id] = channel
       self._channel_to_project[channel.id] = project_id  # NEW
   ```

4. **Add a public lookup method**:
   ```python
   def get_project_for_channel(self, channel_id: int) -> str | None:
       """Return the project_id associated with a channel, or None."""
       return self._channel_to_project.get(channel_id)
   ```

**Tests:** Unit test for `get_project_for_channel` returning correct project or None.

---

## 3. Implement `set_control_interface` Command

**Goal:** Add a new command `set_control_interface <project_name> <channel_name>` that sets a project's control channel by *name* (string), creating a friendly alias for the existing `/set-channel` functionality.

### Changes to `src/command_handler.py`:

Add `_cmd_set_control_interface`:
```python
async def _cmd_set_control_interface(self, args: dict) -> dict:
    """Set the control interface channel for a project by channel name."""
    project_id = args["project_id"]
    channel_name = args["channel_name"]

    project = await self.db.get_project(project_id)
    if not project:
        return {"error": f"Project '{project_id}' not found"}

    # The actual channel resolution (name → ID) must happen at the Discord layer.
    # Store the channel name as a signal; the Discord command will resolve it.
    return {
        "project_id": project_id,
        "channel_name": channel_name,
        "action": "set_control_interface",
    }
```

### Changes to `src/discord/commands.py`:

Add a new slash command `/set-control-interface`:
```python
@bot.tree.command(
    name="set-control-interface",
    description="Set the control channel for a project by channel name",
)
@app_commands.describe(
    project_id="Project ID",
    channel_name="Name of the Discord channel to use as control interface",
)
async def set_control_interface_command(
    interaction: discord.Interaction,
    project_id: str,
    channel_name: str,
):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("Error: Not in a guild.", ephemeral=True)
        return

    # Find the channel by name in the guild
    channel = discord.utils.get(guild.text_channels, name=channel_name)
    if not channel:
        await interaction.response.send_message(
            f"Error: Channel `{channel_name}` not found in this server.",
            ephemeral=True,
        )
        return

    result = await handler.execute("set_project_channel", {
        "project_id": project_id,
        "channel_id": str(channel.id),
        "channel_type": "control",
    })
    if "error" in result:
        await interaction.response.send_message(
            f"Error: {result['error']}", ephemeral=True
        )
        return

    bot.update_project_channel(project_id, channel, "control")
    await interaction.response.send_message(
        f"✅ Project `{project_id}` control interface set to {channel.mention}"
    )
```

### Chat Agent Tool Definition:

Also expose this as a chat agent tool so the NL interface can use it. Add the tool definition to `src/chat_agent.py` in the tools list:
```python
{
    "name": "set_control_interface",
    "description": "Set the control interface channel for a project by name",
    "parameters": {
        "project_id": {"type": "string", "description": "Project ID"},
        "channel_name": {"type": "string", "description": "Discord channel name"},
    },
    "required": ["project_id", "channel_name"],
}
```

**Tests:** Test the slash command with a valid project and channel name, test with a missing channel, test with a missing project.

---

## 4. Implement `create_channel_for_project` Command

**Goal:** Add a new command `create_channel_for_project <project_name>` that creates a dedicated Discord channel for a project **only if one doesn't already exist**, then links it.

### Changes to `src/discord/commands.py`:

Add a new slash command `/create-project-channel`:
```python
@bot.tree.command(
    name="create-project-channel",
    description="Create a dedicated Discord channel for a project (if it doesn't already exist)",
)
@app_commands.describe(
    project_id="Project ID",
    channel_type="Channel purpose: notifications (default) or control",
    category="Category to create the channel in (optional)",
)
@app_commands.choices(channel_type=[
    app_commands.Choice(name="notifications", value="notifications"),
    app_commands.Choice(name="control", value="control"),
    app_commands.Choice(name="both", value="both"),
])
async def create_project_channel_command(
    interaction: discord.Interaction,
    project_id: str,
    channel_type: app_commands.Choice[str] | None = None,
    category: discord.CategoryChannel | None = None,
):
    await interaction.response.defer()
    ch_type = channel_type.value if channel_type else "notifications"
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("Error: Not in a guild.", ephemeral=True)
        return

    # Validate project exists
    project_result = await handler.execute("list_projects", {})
    project_ids = [p["id"] for p in project_result.get("projects", [])]
    if project_id not in project_ids:
        await interaction.followup.send(
            f"Error: Project `{project_id}` not found.", ephemeral=True
        )
        return

    types_to_create = ["notifications", "control"] if ch_type == "both" else [ch_type]
    created_channels = []

    for ctype in types_to_create:
        # Determine channel name
        suffix = "" if ctype == "notifications" else "-control"
        channel_name = f"{project_id}{suffix}"

        # Check if a channel with this name already exists
        existing = discord.utils.get(guild.text_channels, name=channel_name)
        if existing:
            # Channel exists — just link it if not already linked
            result = await handler.execute("set_project_channel", {
                "project_id": project_id,
                "channel_id": str(existing.id),
                "channel_type": ctype,
            })
            bot.update_project_channel(project_id, existing, ctype)
            created_channels.append(
                f"Linked existing {existing.mention} as {ctype} channel"
            )
            continue

        # Create the channel
        try:
            new_channel = await guild.create_text_channel(
                name=channel_name,
                category=category,
                reason=f"AgentQueue: {ctype} channel for project {project_id}",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Error: Bot lacks permission to create channels.", ephemeral=True
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"Error creating channel: {e}", ephemeral=True
            )
            return

        # Link to project
        result = await handler.execute("set_project_channel", {
            "project_id": project_id,
            "channel_id": str(new_channel.id),
            "channel_type": ctype,
        })
        bot.update_project_channel(project_id, new_channel, ctype)
        created_channels.append(
            f"Created {new_channel.mention} as {ctype} channel"
        )

    summary = "\n".join(f"• {c}" for c in created_channels)
    await interaction.followup.send(
        f"✅ Project `{project_id}` channel setup complete:\n{summary}"
    )
```

### Changes to `src/command_handler.py`:

Add a `_cmd_create_channel_for_project` method that handles the non-Discord (DB-only) side:
```python
async def _cmd_create_channel_for_project(self, args: dict) -> dict:
    """Validate project exists and return channel setup info.

    Actual Discord channel creation is handled by the Discord command layer.
    """
    project_id = args["project_id"]
    project = await self.db.get_project(project_id)
    if not project:
        return {"error": f"Project '{project_id}' not found"}
    return {
        "project_id": project_id,
        "project_name": project.name,
        "existing_notifications_channel": project.discord_channel_id,
        "existing_control_channel": project.discord_control_channel_id,
    }
```

**Key behavior:** The command checks if a channel named `<project_id>` (for notifications) or `<project_id>-control` (for control) already exists in the guild. If it does, it links the existing channel rather than creating a duplicate. If it doesn't exist, it creates and links it.

**Tests:** Test with a project that has no channels, test with a project that already has channels linked, test with a non-existent project.

---

## 5. Adjust Discord Commands to Infer Project from Channel Context

**Goal:** Commands that require a `project_id` should auto-infer it from the channel the command is issued in, making `project_id` optional when used from a project-specific channel.

### Create a shared helper in `src/discord/commands.py`:

```python
def _resolve_project_id(
    interaction: discord.Interaction,
    explicit_project_id: str | None,
    bot: commands.Bot,
) -> str | None:
    """Resolve project_id: use explicit value if given, otherwise infer from channel."""
    if explicit_project_id:
        return explicit_project_id
    # Try to infer from channel context
    return bot.get_project_for_channel(interaction.channel_id)
```

### Update the following commands to make `project_id` optional:

These commands currently require `project_id` as a mandatory parameter. Change them to optional with channel-context fallback:

1. **`/pause`** (`pause_command`) — Line 456
2. **`/resume`** (`resume_command`) — Line 467
3. **`/tasks`** (`tasks_command`) — Line 497 (already optional)
4. **`/repos`** (`repos_command`) — Line 817 (already optional)
5. **`/git-status`** (`git_status_command`) — Line 895
6. **`/notes`** (`notes_command`) — Line 1100
7. **`/write-note`** — Line 1147
8. **`/delete-note`** — Line 1171
9. **`/edit-project`** (`edit_project_command`) — Line 306
10. **`/delete-project`** (`delete_project_command`) — Line 329
11. **`/hooks`** (`hooks_command`) — Line 933 (already optional)

### Example transformation for `/pause`:

**Before:**
```python
@bot.tree.command(name="pause", description="Pause a project")
@app_commands.describe(project_id="Project ID to pause")
async def pause_command(interaction: discord.Interaction, project_id: str):
    result = await handler.execute("pause_project", {"project_id": project_id})
```

**After:**
```python
@bot.tree.command(name="pause", description="Pause a project")
@app_commands.describe(project_id="Project ID to pause (auto-detected from channel)")
async def pause_command(interaction: discord.Interaction, project_id: str | None = None):
    resolved = _resolve_project_id(interaction, project_id, bot)
    if not resolved:
        await interaction.response.send_message(
            "Error: No project specified and couldn't infer from channel context. "
            "Use this command in a project channel or provide a project_id.",
            ephemeral=True,
        )
        return
    result = await handler.execute("pause_project", {"project_id": resolved})
```

### Pattern for all updated commands:

Each command follows this pattern:
1. Change `project_id: str` to `project_id: str | None = None`.
2. Add `resolved = _resolve_project_id(interaction, project_id, bot)` at the top.
3. Add a guard clause if `resolved` is None (with helpful error message).
4. Use `resolved` instead of `project_id` in the rest of the function.

### Commands to keep as-is (project_id stays required):

- **`/create-project`** — Creating a project needs an explicit name.
- **`/set-channel`** — Needs explicit project_id and channel.
- **`/create-channel`** — Needs explicit project_id.
- **`/set-control-interface`** — Needs explicit project_id and channel_name.

**Tests:** Test commands in a project channel (should auto-resolve), test in global control channel (should require explicit project_id), test with explicit override.

---

## 6. Update Task Notifications for Per-Channel Routing

**Goal:** Ensure all task lifecycle notifications are properly routed to their project's dedicated channel.

### Current State (already working):

The notification flow already supports per-project routing:
- `orchestrator._notify_channel(msg, project_id=...)` → `bot._send_notification(msg, project_id)` → `bot._get_notification_channel(project_id)` → per-project or global channel.
- Thread creation (`_create_task_thread`) already routes to per-project channels.
- `_send_control_message` already routes to per-project control channels.

### Enhancements needed:

1. **Add a welcome/intro message when a project channel is first configured:**

   In `src/discord/commands.py`, after linking a channel to a project (in both `set_channel_command`, `create_channel_command`, `set_control_interface_command`, and `create_project_channel_command`), send a welcome message to the new channel:
   ```python
   # After successfully linking/creating the channel:
   try:
       await channel.send(
           f"📋 **Channel linked to project `{project_id}`**\n"
           f"This channel will receive {ch_type} messages for this project.\n"
           f"Use slash commands here — project will be auto-detected."
       )
   except discord.Forbidden:
       pass  # Bot may not have send permission in the channel yet
   ```

2. **Ensure `agent_questions` channel also supports per-project routing:**

   Currently, the `agent_questions` channel is global. Add per-project agent questions channel support:
   - Add `discord_agent_questions_channel_id` to the `Project` model (optional, future enhancement — **defer to a follow-up task**).
   - For now, agent questions continue to use the global channel but mention the project name prominently.

3. **Verify all notification call sites pass `project_id`:**

   Audit all calls to `_notify_channel` and `_control_channel_post` in `orchestrator.py` to ensure they pass `project_id`. Current audit shows they all do — no changes needed.

**Files to modify:** `src/discord/commands.py` (welcome message)

**Tests:** Verify that notifications for different projects land in their respective channels (integration test).

---

## 7. Update the Setup Wizard for Multi-Channel Support

**Goal:** Extend `setup_wizard.py` to guide users through per-project channel configuration during initial setup or re-configuration.

### Changes to `setup_wizard.py`:

1. **Add a new Step 6 (or extend existing flow) — "Per-Project Channels":**

   After the base Discord setup (Step 2), add an optional section:
   ```python
   def step_project_channels(existing: dict, discord_cfg: dict) -> dict:
       """Optional: configure per-project Discord channels."""
       step_header(6, "Per-Project Channels (Optional)")

       print(f"""
     AgentQueue supports dedicated channels per project.
     Each project can have its own notifications and control channel.
     You can configure this now or later using Discord commands:
       /create-project-channel <project_id>
       /set-control-interface <project_id> <channel_name>
       """)

       if not prompt_yes_no("Configure per-project channels now?", default=False):
           info("Skipped — you can set up project channels anytime via Discord commands.")
           return {}

       # This step just documents the capability.
       # Actual channel creation requires the bot to be running and connected.
       info("Per-project channels are configured at runtime via Discord commands.")
       info("Once the bot is running, use these commands:")
       info("  /create-project-channel <project_id>       — auto-create + link channels")
       info("  /set-channel <project_id> <channel>        — link existing channel")
       info("  /set-control-interface <project_id> <name> — set control channel by name")
       success("Per-project channel support is built in — no additional config needed.")
       return {}
   ```

2. **Update the "Test Connectivity" step to mention multi-channel support:**

   In `step_test_connectivity()`, add a note:
   ```python
   info("Per-project channels: configure at runtime via /create-project-channel")
   ```

3. **Update the channel verification in `_test_discord()`:**

   Currently, the wizard only checks for global channels (`control`, `notifications`, `agent_questions`). Add informational output about existing per-project channels if the database already has projects configured. Since the wizard runs before the DB is fully initialized, this would be a soft check only.

4. **Add documentation text to the written config file:**

   In `step_write_config()`, add a comment to the YAML output:
   ```yaml
   discord:
     # Per-project channels are configured at runtime via Discord commands:
     #   /create-project-channel <project_id>
     #   /set-channel <project_id> <channel>
     #   /set-control-interface <project_id> <channel_name>
     channels:
       control: control
       notifications: notifications
       agent_questions: agent-questions
   ```

**Files to modify:** `setup_wizard.py`

**Tests:** Run the wizard and verify it displays the new step and doesn't break existing flow.

---

## 8. Add `create_project` Integration with Auto-Channel Creation

**Goal:** Optionally auto-create Discord channels when a new project is created via the `/create-project` command.

### Changes to `src/discord/commands.py`:

Update the `/create-project` command to add an optional `create_channels` parameter:

```python
@bot.tree.command(name="create-project", description="Create a new project")
@app_commands.describe(
    name="Project name",
    credit_weight="Token budget weight (default: 1.0)",
    max_agents="Max concurrent agents (default: 2)",
    create_channels="Auto-create dedicated Discord channels for this project",
    category="Category to create channels in (optional)",
)
async def create_project_command(
    interaction: discord.Interaction,
    name: str,
    credit_weight: float = 1.0,
    max_agents: int = 2,
    create_channels: bool = False,
    category: discord.CategoryChannel | None = None,
):
```

When `create_channels=True`:
1. Create the project via the command handler (as today).
2. Auto-create a notifications channel named `<project_id>` and a control channel named `<project_id>-control`.
3. Link both channels to the project.
4. Send welcome messages in the new channels.

This reuses the logic from `create_project_channel_command` (Step 4).

**Files to modify:** `src/discord/commands.py`

**Tests:** Test project creation with and without `create_channels` flag.

---

## 9. Update Chat Agent Tool Definitions

**Goal:** Ensure the NL chat agent (used via `@mention` or control channel) has tool definitions for the new commands.

### Changes to `src/chat_agent.py`:

Add tool definitions:
1. `set_control_interface` — Set control channel by name.
2. `create_channel_for_project` — Create/link project channel (returns info; actual channel creation happens via Discord API in the callback).

Since the chat agent routes through `CommandHandler.execute()`, the tools just need proper definitions. The actual Discord channel creation for `create_channel_for_project` cannot be done purely from the chat agent (needs Discord API access), so the tool should return instructions for the user to use the slash command instead, or delegate to the bot if a reference is available.

**Practical approach:** For chat agent tools, `set_control_interface` can resolve channel names via the bot's guild cache. Add a bot reference or guild accessor to the chat agent's context. `create_channel_for_project` should suggest using the slash command.

**Files to modify:** `src/chat_agent.py`

---

## 10. Write Tests

**Goal:** Add test coverage for the new functionality.

### New test files / additions:

1. **`tests/test_channel_routing.py`** — Test the reverse channel→project lookup:
   - Test `get_project_for_channel()` returns correct project_id.
   - Test reverse map is updated when `update_project_channel()` is called.
   - Test reverse map is populated from DB on startup.

2. **`tests/test_commands.py`** (update existing) — Test new commands:
   - Test `set_control_interface` with valid/invalid project and channel names.
   - Test `create_channel_for_project` with existing/non-existing channels.
   - Test project_id inference from channel context in updated commands.

3. **`tests/test_command_handler.py`** (update existing) — Test handler:
   - Test `_cmd_set_control_interface` validation.
   - Test `_cmd_create_channel_for_project` validation.

**Files to create/modify:** `tests/test_channel_routing.py`, `tests/test_commands.py`, `tests/test_command_handler.py`

---

## Summary of All File Changes

| File | Changes |
|------|---------|
| `src/discord/bot.py` | Add `_channel_to_project` reverse map, `get_project_for_channel()` method, update `_resolve_project_channels()` and `update_project_channel()` |
| `src/discord/commands.py` | Add `/set-control-interface`, `/create-project-channel` commands; add `_resolve_project_id()` helper; make `project_id` optional in ~9 commands; add `create_channels` option to `/create-project`; add welcome messages |
| `src/command_handler.py` | Add `_cmd_set_control_interface`, `_cmd_create_channel_for_project` handlers |
| `src/chat_agent.py` | Add tool definitions for new commands |
| `setup_wizard.py` | Add per-project channels step, update config comments, update test-connectivity info |
| `tests/test_channel_routing.py` | New file — test reverse lookup and channel routing |
| `tests/test_commands.py` | Test new slash commands |
| `tests/test_command_handler.py` | Test new command handler methods |

## Implementation Order

1. **Step 2** — Reverse lookup (foundation for everything else)
2. **Step 3** — `set_control_interface` command
3. **Step 4** — `create_channel_for_project` command
4. **Step 5** — Channel-context project inference (depends on Step 2)
5. **Step 6** — Notification routing verification & welcome messages
6. **Step 8** — Auto-channel on project creation (depends on Step 4)
7. **Step 7** — Setup wizard updates
8. **Step 9** — Chat agent tool definitions
9. **Step 10** — Tests (can be written alongside each step)
