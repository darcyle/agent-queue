"""Discord slash commands for AgentQueue.

All commands delegate their business logic to the shared CommandHandler,
ensuring feature parity with the chat agent LLM tools. Discord commands
are thin formatting wrappers that translate between Discord interactions
and the unified command interface.
"""
from __future__ import annotations

import io

import discord
from discord import app_commands
from discord.ext import commands

from src.models import TaskStatus


def setup_commands(bot: commands.Bot) -> None:
    """Register all slash commands on the bot."""

    # Shortcut — the shared command handler is owned by the ChatAgent.
    # Every slash command calls `handler.execute(name, args)` for its
    # business logic and only handles Discord-specific formatting here.
    handler = bot.agent.handler

    # ---------------------------------------------------------------------------
    # Channel→project resolution helper
    # ---------------------------------------------------------------------------

    async def _resolve_project_from_context(
        interaction: discord.Interaction,
        project_id: str | None,
    ) -> str | None:
        """Resolve *project_id*, falling back to the channel→project mapping.

        If *project_id* was explicitly supplied by the user it is returned
        unchanged.  Otherwise the interaction's channel is checked against
        the bot's reverse channel-to-project lookup so that commands run
        inside a project-specific channel automatically inherit that project.
        """
        if project_id is not None:
            return project_id
        return bot.get_project_for_channel(interaction.channel_id)

    _NO_PROJECT_MSG = (
        "Could not determine project — please provide `project_id` "
        "or run this command from a project channel."
    )

    # ---------------------------------------------------------------------------
    # Shared task-detail helper
    # ---------------------------------------------------------------------------

    async def _format_task_detail(task_id: str) -> str | None:
        """Fetch and format full task details. Returns None if task not found."""
        result = await handler.execute("get_task", {"task_id": task_id})
        if "error" in result:
            return None
        lines = [
            f"## Task `{result['id']}`",
            f"**Title:** {result['title']}",
            f"**Status:** {result['status']}",
            f"**Project:** `{result['project_id']}`",
            f"**Priority:** {result['priority']}",
        ]
        if result.get("assigned_agent"):
            lines.append(f"**Agent:** {result['assigned_agent']}")
        if result.get("retry_count"):
            lines.append(f"**Retries:** {result['retry_count']} / {result['max_retries']}")
        desc = result.get("description", "")
        if desc:
            desc = desc if len(desc) <= 800 else desc[:800] + "..."
            lines.append(f"\n**Description:**\n{desc}")
        return "\n".join(lines)

    # ---------------------------------------------------------------------------
    # Interactive UI components for task lists
    # ---------------------------------------------------------------------------

    _STATUS_ORDER = [
        "IN_PROGRESS", "ASSIGNED", "READY", "DEFINED",
        "PAUSED", "WAITING_INPUT", "AWAITING_APPROVAL", "VERIFYING",
        "FAILED", "BLOCKED", "COMPLETED",
    ]
    _STATUS_DISPLAY: dict[str, str] = {
        "DEFINED": "Defined", "READY": "Ready", "ASSIGNED": "Assigned",
        "IN_PROGRESS": "In Progress", "VERIFYING": "Verifying",
        "COMPLETED": "Completed", "PAUSED": "Paused",
        "WAITING_INPUT": "Waiting Input", "FAILED": "Failed",
        "BLOCKED": "Blocked", "AWAITING_APPROVAL": "Awaiting Approval",
    }
    # Sections expanded by default (active/actionable states)
    _DEFAULT_EXPANDED = {
        "IN_PROGRESS", "ASSIGNED", "READY",
        "FAILED", "BLOCKED", "PAUSED", "WAITING_INPUT", "AWAITING_APPROVAL",
    }
    _MAX_TASKS_PER_SECTION = 15

    class StatusToggleButton(discord.ui.Button):
        """Toggles a status section between expanded and collapsed."""

        def __init__(self, status: str, count: int, is_expanded: bool) -> None:
            emoji = _STATUS_EMOJIS.get(status, "⚪")
            display = _STATUS_DISPLAY.get(status, status)
            label = f"{display} ({count})"
            style = (
                discord.ButtonStyle.primary if is_expanded
                else discord.ButtonStyle.secondary
            )
            super().__init__(style=style, label=label, emoji=emoji)
            self.status = status

        async def callback(self, interaction: discord.Interaction) -> None:
            view: TaskReportView = self.view
            if self.status in view.expanded:
                view.expanded.discard(self.status)
            else:
                view.expanded.add(self.status)
            view._rebuild_components()
            content = view.build_content()
            await interaction.response.edit_message(content=content, view=view)

    class TaskDetailSelect(discord.ui.Select):
        """Dropdown to pick a task and view its full details."""

        def __init__(self, options: list[discord.SelectOption]) -> None:
            super().__init__(
                placeholder="Select a task for details...",
                options=options,
            )

        async def callback(self, interaction: discord.Interaction) -> None:
            task_id = self.values[0]
            await interaction.response.defer(ephemeral=True)
            detail = await _format_task_detail(task_id)
            if detail is None:
                await interaction.followup.send(
                    f"Task `{task_id}` not found.", ephemeral=True
                )
            else:
                await interaction.followup.send(detail, ephemeral=True)

    class TaskReportView(discord.ui.View):
        """Grouped task report with collapsible status sections and detail select."""

        def __init__(self, tasks_by_status: dict[str, list], total: int) -> None:
            super().__init__(timeout=600)
            self.tasks_by_status = tasks_by_status
            self.total = total
            self.expanded: set[str] = set()
            for status in _DEFAULT_EXPANDED:
                if status in tasks_by_status:
                    self.expanded.add(status)
            # If nothing expanded, expand the first non-empty section
            if not self.expanded:
                for status in _STATUS_ORDER:
                    if status in tasks_by_status:
                        self.expanded.add(status)
                        break
            self._rebuild_components()

        def _rebuild_components(self) -> None:
            self.clear_items()
            # Toggle buttons for each status that has tasks
            for status in _STATUS_ORDER:
                if status not in self.tasks_by_status:
                    continue
                count = len(self.tasks_by_status[status])
                is_expanded = status in self.expanded
                self.add_item(StatusToggleButton(status, count, is_expanded))
            # Select dropdown with tasks from expanded sections
            options: list[discord.SelectOption] = []
            for status in _STATUS_ORDER:
                if status not in self.expanded:
                    continue
                for t in self.tasks_by_status.get(status, []):
                    if len(options) >= 25:
                        break
                    title = t["title"]
                    if len(title) > 95:
                        title = title[:92] + "..."
                    options.append(discord.SelectOption(
                        label=title,
                        value=t["id"],
                        description=t["id"],
                    ))
                if len(options) >= 25:
                    break
            if options:
                self.add_item(TaskDetailSelect(options))

        def build_content(self) -> str:
            lines: list[str] = []
            for status in _STATUS_ORDER:
                if status not in self.tasks_by_status:
                    continue
                tasks = self.tasks_by_status[status]
                emoji = _STATUS_EMOJIS.get(status, "⚪")
                display = _STATUS_DISPLAY.get(status, status)
                count = len(tasks)
                if status in self.expanded:
                    lines.append(f"### {emoji} {display} ({count})")
                    shown = tasks[:_MAX_TASKS_PER_SECTION]
                    for t in shown:
                        lines.append(f"**{t['title']}** `{t['id']}`")
                    if count > _MAX_TASKS_PER_SECTION:
                        lines.append(
                            f"_...and {count - _MAX_TASKS_PER_SECTION} more_"
                        )
                    lines.append("")
                else:
                    lines.append(f"{emoji} **{display}** ({count})")
            content = "\n".join(lines)
            # Trim if over Discord's 2000-char message limit
            if len(content) > 1950:
                lines = []
                for status in _STATUS_ORDER:
                    if status not in self.tasks_by_status:
                        continue
                    tasks = self.tasks_by_status[status]
                    emoji = _STATUS_EMOJIS.get(status, "⚪")
                    display = _STATUS_DISPLAY.get(status, status)
                    count = len(tasks)
                    if status in self.expanded:
                        cap = 8
                        lines.append(f"### {emoji} {display} ({count})")
                        for t in tasks[:cap]:
                            lines.append(f"**{t['title']}** `{t['id']}`")
                        if count > cap:
                            lines.append(f"_...and {count - cap} more_")
                        lines.append("")
                    else:
                        lines.append(f"{emoji} **{display}** ({count})")
                content = "\n".join(lines)
            return content

    # ---------------------------------------------------------------------------
    # Shared formatting helpers
    # ---------------------------------------------------------------------------

    _STATUS_COLORS: dict[str, int] = {
        TaskStatus.DEFINED.value:            0x95a5a6,
        TaskStatus.READY.value:              0x3498db,
        TaskStatus.ASSIGNED.value:           0x9b59b6,
        TaskStatus.IN_PROGRESS.value:        0xf39c12,
        TaskStatus.WAITING_INPUT.value:      0x1abc9c,
        TaskStatus.PAUSED.value:             0x7f8c8d,
        TaskStatus.VERIFYING.value:          0x2980b9,
        TaskStatus.AWAITING_APPROVAL.value:  0xe67e22,
        TaskStatus.COMPLETED.value:          0x2ecc71,
        TaskStatus.FAILED.value:             0xe74c3c,
        TaskStatus.BLOCKED.value:            0x992d22,
    }
    _STATUS_EMOJIS: dict[str, str] = {
        TaskStatus.DEFINED.value:            "⚪",
        TaskStatus.READY.value:              "🔵",
        TaskStatus.ASSIGNED.value:           "📋",
        TaskStatus.IN_PROGRESS.value:        "🟡",
        TaskStatus.WAITING_INPUT.value:      "💬",
        TaskStatus.PAUSED.value:             "⏸️",
        TaskStatus.VERIFYING.value:          "🔍",
        TaskStatus.AWAITING_APPROVAL.value:  "⏳",
        TaskStatus.COMPLETED.value:          "🟢",
        TaskStatus.FAILED.value:             "🔴",
        TaskStatus.BLOCKED.value:            "⛔",
    }

    async def _send_long(interaction, text: str, *, followup: bool = False):
        """Send a potentially long response, splitting or attaching as file."""
        send = interaction.followup.send if followup else interaction.response.send_message
        if len(text) <= 2000:
            await send(text)
        elif len(text) <= 6000:
            chunks, current = [], ""
            for line in text.split("\n"):
                candidate = current + ("\n" if current else "") + line
                if len(candidate) > 2000:
                    if current:
                        chunks.append(current)
                    current = line
                else:
                    current = candidate
            if current:
                chunks.append(current)
            for chunk in chunks:
                await interaction.followup.send(chunk)
        else:
            file = discord.File(
                fp=io.BytesIO(text.encode("utf-8")),
                filename="response.md",
            )
            preview = text[:300].rstrip() + "\n\n*Full output attached.*"
            await send(preview, file=file)

    # ===================================================================
    # SYSTEM / STATUS COMMANDS
    # ===================================================================

    @bot.tree.command(name="status", description="Show system status overview")
    async def status_command(interaction: discord.Interaction):
        result = await handler.execute("get_status", {})

        tasks = result["tasks"]
        by_status = tasks.get("by_status", {})
        lines = []
        if result.get("orchestrator_paused"):
            lines.append("⏸ **Orchestrator is PAUSED** — scheduling suspended")
            lines.append("")
        lines += [
            "## System Status",
            f"**Tasks:** {tasks['total']} total — "
            f"{by_status.get('IN_PROGRESS', 0)} active, "
            f"{by_status.get('READY', 0)} ready, "
            f"{by_status.get('COMPLETED', 0)} completed, "
            f"{by_status.get('FAILED', 0)} failed, "
            f"{by_status.get('PAUSED', 0)} paused",
            "",
        ]

        # Agent details
        agents = result.get("agents", [])
        if agents:
            lines.append("**Agents:**")
            for a in agents:
                working_on = a.get("working_on")
                if working_on:
                    lines.append(
                        f"• **{a['name']}** ({a['state']}) → "
                        f"working on `{working_on['task_id']}` — {working_on['title']}"
                    )
                else:
                    lines.append(f"• **{a['name']}** ({a['state']})")
        else:
            lines.append("**Agents:** none registered")

        # Ready tasks
        ready = tasks.get("ready_to_work", [])
        if ready:
            lines.append("")
            lines.append(f"**Queued ({len(ready)}):**")
            for t in ready[:5]:
                lines.append(f"• `{t['id']}` {t['title']}")
            if len(ready) > 5:
                lines.append(f"_...and {len(ready) - 5} more_")

        await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(name="projects", description="List all projects")
    async def projects_command(interaction: discord.Interaction):
        result = await handler.execute("list_projects", {})
        projects = result.get("projects", [])
        if not projects:
            await interaction.response.send_message("No projects configured.")
            return
        lines = []
        for p in projects:
            line = f"• **{p['name']}** (`{p['id']}`) — {p['status']}, weight={p['credit_weight']}"
            channel_parts = []
            if p.get("discord_channel_id"):
                channel_parts.append(f"<#{p['discord_channel_id']}>")
            if p.get("discord_control_channel_id"):
                channel_parts.append(f"ctrl: <#{p['discord_control_channel_id']}>")
            if channel_parts:
                line += f" | {', '.join(channel_parts)}"
            lines.append(line)
        await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(name="agents", description="List all agents")
    async def agents_command(interaction: discord.Interaction):
        result = await handler.execute("list_agents", {})
        agents = result.get("agents", [])
        if not agents:
            await interaction.response.send_message("No agents configured.")
            return
        lines = []
        for a in agents:
            task_info = f" → `{a['current_task']}`" if a.get("current_task") else ""
            lines.append(f"• **{a['name']}** (`{a['id']}`) — {a['state']}{task_info}")
        await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(name="budget", description="Show token budget usage")
    async def budget_command(interaction: discord.Interaction):
        result = await handler.execute("get_token_usage", {})
        breakdown = result.get("breakdown", [])
        if not breakdown:
            await interaction.response.send_message("No token usage recorded.")
            return
        lines = []
        for entry in breakdown:
            pid = entry.get("project_id", "unknown")
            tokens = entry.get("tokens", 0)
            lines.append(f"• **{pid}**: {tokens:,} tokens")
        await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(name="events", description="Show recent system events")
    @app_commands.describe(limit="Number of events to show (default 10)")
    async def events_command(interaction: discord.Interaction, limit: int = 10):
        result = await handler.execute("get_recent_events", {"limit": limit})
        events = result.get("events", [])
        if not events:
            await interaction.response.send_message("No recent events.")
            return
        lines = ["## Recent Events"]
        for evt in events:
            ts = evt.get("timestamp", "")
            etype = evt.get("event_type", "unknown")
            project = evt.get("project_id", "")
            task = evt.get("task_id", "")
            parts = [f"**{etype}**"]
            if project:
                parts.append(f"project=`{project}`")
            if task:
                parts.append(f"task=`{task}`")
            if ts:
                parts.append(f"at {ts}")
            lines.append(f"• {' — '.join(parts)}")
        msg = "\n".join(lines)
        if len(msg) > 2000:
            msg = msg[:1997] + "..."
        await interaction.response.send_message(msg)

    # ===================================================================
    # PROJECT COMMANDS
    # ===================================================================

    @bot.tree.command(name="create-project", description="Create a new project")
    @app_commands.describe(
        name="Project name",
        credit_weight="Scheduling weight (default 1.0)",
        max_concurrent_agents="Max agents working simultaneously (default 2)",
    )
    async def create_project_command(
        interaction: discord.Interaction,
        name: str,
        credit_weight: float = 1.0,
        max_concurrent_agents: int = 2,
    ):
        result = await handler.execute("create_project", {
            "name": name,
            "credit_weight": credit_weight,
            "max_concurrent_agents": max_concurrent_agents,
        })
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        embed = discord.Embed(title="✅ Project Created", color=0x2ecc71)
        embed.add_field(name="Name", value=name, inline=True)
        embed.add_field(name="ID", value=f"`{result['created']}`", inline=True)
        embed.add_field(name="Weight", value=str(credit_weight), inline=True)
        embed.add_field(name="Max Agents", value=str(max_concurrent_agents), inline=True)
        embed.add_field(name="Workspace", value=f"`{result.get('workspace', '')}`", inline=False)
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="edit-project", description="Edit a project's settings")
    @app_commands.describe(
        project_id="Project ID",
        name="New name (optional)",
        credit_weight="New scheduling weight (optional)",
        max_concurrent_agents="New max agents (optional)",
    )
    async def edit_project_command(
        interaction: discord.Interaction,
        project_id: str,
        name: str | None = None,
        credit_weight: float | None = None,
        max_concurrent_agents: int | None = None,
    ):
        args: dict = {"project_id": project_id}
        if name is not None:
            args["name"] = name
        if credit_weight is not None:
            args["credit_weight"] = credit_weight
        if max_concurrent_agents is not None:
            args["max_concurrent_agents"] = max_concurrent_agents
        result = await handler.execute("edit_project", args)
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        fields = ", ".join(result.get("fields", []))
        await interaction.response.send_message(
            f"✅ Project `{project_id}` updated: {fields}"
        )

    @bot.tree.command(name="delete-project", description="Delete a project and all its data")
    @app_commands.describe(
        project_id="Project ID to delete",
        archive_channels="Archive the project's Discord channels instead of leaving them (default: False)",
    )
    @app_commands.choices(archive_channels=[
        app_commands.Choice(name="Yes – archive channels", value=1),
        app_commands.Choice(name="No – leave channels as-is", value=0),
    ])
    async def delete_project_command(
        interaction: discord.Interaction,
        project_id: str,
        archive_channels: app_commands.Choice[int] | None = None,
    ):
        do_archive = archive_channels is not None and archive_channels.value == 1
        result = await handler.execute(
            "delete_project",
            {"project_id": project_id, "archive_channels": do_archive},
        )
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return

        # The command handler's _on_project_deleted callback already cleared
        # the bot's in-memory caches.  Now handle optional channel archival.
        archived: list[str] = []
        if do_archive and result.get("channel_ids"):
            guild = interaction.guild
            if guild:
                for ch_type, ch_id in result["channel_ids"].items():
                    channel = guild.get_channel(int(ch_id))
                    if channel and isinstance(channel, discord.TextChannel):
                        try:
                            # Archive by moving to read-only: deny Send Messages
                            # for @everyone while preserving history.
                            overwrite = channel.overwrites_for(guild.default_role)
                            overwrite.send_messages = False
                            await channel.set_permissions(
                                guild.default_role, overwrite=overwrite,
                                reason=f"Archived: project {project_id} deleted",
                            )
                            await channel.edit(
                                name=f"archived-{channel.name}",
                                reason=f"Archived: project {project_id} deleted",
                            )
                            archived.append(f"#{channel.name} ({ch_type})")
                        except discord.Forbidden:
                            archived.append(f"#{channel.name} ({ch_type}) — no permission to archive")

        msg = f"🗑️ Project **{result.get('name', project_id)}** (`{project_id}`) deleted."
        if archived:
            msg += "\n📦 Archived channels: " + ", ".join(archived)
        elif do_archive:
            msg += "\n*(No linked channels to archive.)*"
        await interaction.response.send_message(msg)

    # -------------------------------------------------------------------
    # CHANNEL MANAGEMENT COMMANDS
    # -------------------------------------------------------------------

    @bot.tree.command(
        name="set-channel",
        description="Link a Discord channel to a project for notifications or control",
    )
    @app_commands.describe(
        project_id="Project ID",
        channel="The Discord channel to link",
        channel_type="Channel purpose: notifications (default) or control",
    )
    @app_commands.choices(channel_type=[
        app_commands.Choice(name="notifications", value="notifications"),
        app_commands.Choice(name="control", value="control"),
    ])
    async def set_channel_command(
        interaction: discord.Interaction,
        project_id: str,
        channel: discord.TextChannel,
        channel_type: app_commands.Choice[str] | None = None,
    ):
        ch_type = channel_type.value if channel_type else "notifications"
        result = await handler.execute("set_project_channel", {
            "project_id": project_id,
            "channel_id": str(channel.id),
            "channel_type": ch_type,
        })
        if "error" in result:
            await interaction.response.send_message(
                f"Error: {result['error']}", ephemeral=True
            )
            return
        # Update the bot's channel cache immediately
        bot.update_project_channel(project_id, channel, ch_type)
        await interaction.response.send_message(
            f"✅ Project `{project_id}` {ch_type} channel set to {channel.mention}"
        )

    @bot.tree.command(
        name="set-control-interface",
        description="Set a project's control channel by name",
    )
    @app_commands.describe(
        project_id="Project ID",
        channel_name="Name of the Discord channel to use as the control interface",
    )
    async def set_control_interface_command(
        interaction: discord.Interaction,
        project_id: str,
        channel_name: str,
    ):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "Error: Not in a guild.", ephemeral=True
            )
            return

        # Strip leading '#' if the user included one.
        channel_name = channel_name.lstrip("#").strip()

        # Look up the channel by name in the guild.
        matched_channel: discord.TextChannel | None = None
        for ch in guild.text_channels:
            if ch.name == channel_name:
                matched_channel = ch
                break

        if not matched_channel:
            await interaction.response.send_message(
                f"Error: No text channel named `{channel_name}` found in this server.",
                ephemeral=True,
            )
            return

        result = await handler.execute("set_control_interface", {
            "project_id": project_id,
            "channel_name": channel_name,
            "_resolved_channel_id": str(matched_channel.id),
        })
        if "error" in result:
            await interaction.response.send_message(
                f"Error: {result['error']}", ephemeral=True
            )
            return
        # Update the bot's channel cache immediately
        bot.update_project_channel(project_id, matched_channel, "control")
        await interaction.response.send_message(
            f"✅ Project `{project_id}` control channel set to {matched_channel.mention}"
        )

    @bot.tree.command(
        name="create-channel",
        description="Create a new Discord channel for a project",
    )
    @app_commands.describe(
        project_id="Project ID",
        channel_name="Name for the new channel (defaults to project ID)",
        channel_type="Channel purpose: notifications (default) or control",
        category="Category to create the channel in (optional)",
    )
    @app_commands.choices(channel_type=[
        app_commands.Choice(name="notifications", value="notifications"),
        app_commands.Choice(name="control", value="control"),
    ])
    async def create_channel_command(
        interaction: discord.Interaction,
        project_id: str,
        channel_name: str | None = None,
        channel_type: app_commands.Choice[str] | None = None,
        category: discord.CategoryChannel | None = None,
    ):
        await interaction.response.defer()
        ch_type = channel_type.value if channel_type else "notifications"

        # Validate project exists (direct lookup)
        project_check = await handler.execute("get_project_channels", {"project_id": project_id})
        if "error" in project_check:
            await interaction.followup.send(
                f"Error: Project `{project_id}` not found.", ephemeral=True
            )
            return

        name = channel_name or project_id
        # Create the Discord channel
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Error: Not in a guild.", ephemeral=True)
            return

        if ch_type == "notifications":
            topic = f"Agent Queue notifications for project: {project_id}"
        else:
            topic = f"Agent Queue control channel for project: {project_id}"

        try:
            new_channel = await guild.create_text_channel(
                name=name,
                category=category,
                topic=topic,
                reason=f"AgentQueue: {ch_type} channel for project {project_id}",
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

        # Link it to the project in the database
        result = await handler.execute("set_project_channel", {
            "project_id": project_id,
            "channel_id": str(new_channel.id),
            "channel_type": ch_type,
        })
        if "error" in result:
            await interaction.followup.send(
                f"Channel created but linking failed: {result['error']}", ephemeral=True
            )
            return

        # Update the bot's channel cache immediately
        bot.update_project_channel(project_id, new_channel, ch_type)
        await interaction.followup.send(
            f"✅ Created {new_channel.mention} as {ch_type} channel for project `{project_id}`"
        )

    @bot.tree.command(
        name="create-channel-for-project",
        description="Create (or reuse) a dedicated Discord channel for a project (idempotent)",
    )
    @app_commands.describe(
        project_id="Project ID",
        channel_name="Name for the channel (defaults to project ID)",
        channel_type="Channel purpose: notifications (default) or control",
        category="Category to create the channel in (optional)",
    )
    @app_commands.choices(channel_type=[
        app_commands.Choice(name="notifications", value="notifications"),
        app_commands.Choice(name="control", value="control"),
    ])
    async def create_channel_for_project_command(
        interaction: discord.Interaction,
        project_id: str,
        channel_name: str | None = None,
        channel_type: app_commands.Choice[str] | None = None,
        category: discord.CategoryChannel | None = None,
    ):
        await interaction.response.defer()
        ch_type = channel_type.value if channel_type else "notifications"
        name = channel_name or project_id

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("Error: Not in a guild.", ephemeral=True)
            return

        # Build guild_channels list for idempotency lookup
        guild_channels = [
            {"id": ch.id, "name": ch.name}
            for ch in guild.text_channels
        ]

        # Check if a channel with this name already exists
        existing_channel: discord.TextChannel | None = None
        for ch in guild.text_channels:
            if ch.name == name:
                existing_channel = ch
                break

        created_channel_id: str | None = None
        if not existing_channel:
            # No matching channel — create one
            if ch_type == "notifications":
                topic = f"Agent Queue notifications for project: {project_id}"
            else:
                topic = f"Agent Queue control channel for project: {project_id}"
            try:
                existing_channel = await guild.create_text_channel(
                    name=name,
                    category=category,
                    topic=topic,
                    reason=f"AgentQueue: {ch_type} channel for project {project_id}",
                )
                created_channel_id = str(existing_channel.id)
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

        # Delegate to the command handler for project validation + linking
        result = await handler.execute("create_channel_for_project", {
            "project_id": project_id,
            "channel_name": name,
            "channel_type": ch_type,
            "guild_channels": guild_channels,
            "_created_channel_id": created_channel_id,
        })

        if "error" in result:
            await interaction.followup.send(
                f"Error: {result['error']}", ephemeral=True
            )
            return

        # Update the bot's channel cache immediately
        bot.update_project_channel(project_id, existing_channel, ch_type)

        action = result.get("action", "linked")
        if action == "linked_existing":
            await interaction.followup.send(
                f"✅ Channel {existing_channel.mention} already exists — "
                f"linked as {ch_type} channel for project `{project_id}`"
            )
        else:
            await interaction.followup.send(
                f"✅ Created {existing_channel.mention} as {ch_type} channel "
                f"for project `{project_id}`"
            )

    @bot.tree.command(
        name="channel-map",
        description="Show all project-to-channel mappings",
    )
    async def channel_map_command(interaction: discord.Interaction):
        result = await handler.execute("list_projects", {})
        projects = result.get("projects", [])
        if not projects:
            await interaction.response.send_message("No projects configured.")
            return

        # Build a channel-centric view: collect all channel assignments
        lines = ["**Channel Map**\n"]
        assigned = []
        unassigned = []

        for p in projects:
            notif_id = p.get("discord_channel_id")
            ctrl_id = p.get("discord_control_channel_id")
            if notif_id or ctrl_id:
                parts = [f"**{p['name']}** (`{p['id']}`)"]
                if notif_id:
                    parts.append(f"  notifications: <#{notif_id}>")
                if ctrl_id:
                    parts.append(f"  control: <#{ctrl_id}>")
                assigned.append("\n".join(parts))
            else:
                unassigned.append(f"`{p['id']}`")

        if assigned:
            lines.append("**Projects with dedicated channels:**")
            for entry in assigned:
                lines.append(f"• {entry}")
        else:
            lines.append("_No projects have dedicated channels yet._")

        if unassigned:
            lines.append(f"\n**Using global channels:** {', '.join(unassigned)}")

        lines.append(
            f"\n_Use `/set-channel` or `/create-channel` to assign project channels._"
        )
        await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(name="pause", description="Pause a project")
    @app_commands.describe(project_id="Project ID to pause (auto-detected from channel)")
    async def pause_command(interaction: discord.Interaction, project_id: str | None = None):
        project_id = await _resolve_project_from_context(interaction, project_id)
        if not project_id:
            await interaction.response.send_message(f"Error: {_NO_PROJECT_MSG}", ephemeral=True)
            return
        result = await handler.execute("pause_project", {"project_id": project_id})
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"⏸ Project **{result.get('name', project_id)}** paused."
        )

    @bot.tree.command(name="resume", description="Resume a paused project")
    @app_commands.describe(project_id="Project ID to resume (auto-detected from channel)")
    async def resume_command(interaction: discord.Interaction, project_id: str | None = None):
        project_id = await _resolve_project_from_context(interaction, project_id)
        if not project_id:
            await interaction.response.send_message(f"Error: {_NO_PROJECT_MSG}", ephemeral=True)
            return
        result = await handler.execute("resume_project", {"project_id": project_id})
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"▶ Project **{result.get('name', project_id)}** resumed."
        )

    @bot.tree.command(name="set-project", description="Set or clear the active project for the chat agent")
    @app_commands.describe(project_id="Project ID to set as active (leave empty to clear)")
    async def set_project_command(interaction: discord.Interaction, project_id: str | None = None):
        args = {"project_id": project_id} if project_id else {}
        result = await handler.execute("set_active_project", args)
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        if result.get("active_project"):
            await interaction.response.send_message(
                f"📌 Active project set to **{result.get('name', '')}** (`{result['active_project']}`)"
            )
        else:
            await interaction.response.send_message("📌 Active project cleared.")

    # ===================================================================
    # TASK COMMANDS
    # ===================================================================

    @bot.tree.command(name="tasks", description="List tasks for a project")
    @app_commands.describe(project_id="Project ID to filter by (auto-detected from channel)")
    async def tasks_command(interaction: discord.Interaction, project_id: str | None = None):
        project_id = await _resolve_project_from_context(interaction, project_id)
        args = {}
        if project_id:
            args["project_id"] = project_id
        result = await handler.execute("list_tasks", args)
        tasks = result.get("tasks", [])
        if not tasks:
            await interaction.response.send_message("No tasks found.")
            return
        # Group tasks by status
        tasks_by_status: dict[str, list] = {}
        for t in tasks:
            tasks_by_status.setdefault(t["status"], []).append(t)
        total = result.get("total", len(tasks))
        view = TaskReportView(tasks_by_status, total)
        content = view.build_content()
        await interaction.response.send_message(content, view=view)

    @bot.tree.command(name="task", description="Show full details of a task")
    @app_commands.describe(task_id="Task ID")
    async def task_command(interaction: discord.Interaction, task_id: str):
        detail = await _format_task_detail(task_id)
        if detail is None:
            await interaction.response.send_message(f"Task `{task_id}` not found.", ephemeral=True)
            return
        await interaction.response.send_message(detail)

    @bot.tree.command(name="add-task", description="Add a task manually to the active project")
    @app_commands.describe(description="What the task should do")
    async def add_task_command(interaction: discord.Interaction, description: str):
        project_id = (
            bot.get_project_for_channel(interaction.channel_id)
            or handler._active_project_id
            or "quick-tasks"
        )
        title = description[:100] if len(description) > 100 else description
        result = await handler.execute("create_task", {
            "project_id": project_id,
            "title": title,
            "description": description,
        })
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        embed = discord.Embed(title="✅ Task Added", color=0x2ecc71)
        embed.add_field(name="ID", value=f"`{result['created']}`", inline=True)
        embed.add_field(name="Project", value=f"`{result['project_id']}`", inline=True)
        embed.add_field(name="Status", value="🔵 READY", inline=True)
        desc_preview = description if len(description) <= 1024 else description[:1021] + "..."
        embed.add_field(name="Description", value=desc_preview, inline=False)
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="edit-task", description="Edit a task's title, description, or priority")
    @app_commands.describe(
        task_id="Task ID",
        title="New title (optional)",
        description="New description (optional)",
        priority="New priority (optional)",
    )
    async def edit_task_command(
        interaction: discord.Interaction,
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        priority: int | None = None,
    ):
        args: dict = {"task_id": task_id}
        if title is not None:
            args["title"] = title
        if description is not None:
            args["description"] = description
        if priority is not None:
            args["priority"] = priority
        result = await handler.execute("edit_task", args)
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        fields = ", ".join(result.get("fields", []))
        await interaction.response.send_message(
            f"✅ Task `{task_id}` updated: {fields}"
        )

    @bot.tree.command(name="stop-task", description="Stop a task that is currently in progress")
    @app_commands.describe(task_id="Task ID to stop")
    async def stop_task_command(interaction: discord.Interaction, task_id: str):
        result = await handler.execute("stop_task", {"task_id": task_id})
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        await interaction.response.send_message(f"⏹ Task `{task_id}` stopped.")

    @bot.tree.command(name="restart-task", description="Reset a task back to READY for re-execution")
    @app_commands.describe(task_id="Task ID to restart")
    async def restart_task_command(interaction: discord.Interaction, task_id: str):
        result = await handler.execute("restart_task", {"task_id": task_id})
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🔄 Task `{task_id}` restarted ({result.get('previous_status', '?')} → READY)"
        )

    @bot.tree.command(name="delete-task", description="Delete a task")
    @app_commands.describe(task_id="Task ID to delete")
    async def delete_task_command(interaction: discord.Interaction, task_id: str):
        result = await handler.execute("delete_task", {"task_id": task_id})
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🗑️ Task `{task_id}` ({result.get('title', '')}) deleted."
        )

    @bot.tree.command(name="approve-task", description="Approve a task that is awaiting approval")
    @app_commands.describe(task_id="Task ID to approve")
    async def approve_task_command(interaction: discord.Interaction, task_id: str):
        result = await handler.execute("approve_task", {"task_id": task_id})
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ Task `{task_id}` ({result.get('title', '')}) approved and completed."
        )

    @bot.tree.command(
        name="skip-task",
        description="Skip a blocked/failed task to unblock its dependency chain",
    )
    @app_commands.describe(task_id="Task ID to skip")
    async def skip_task_command(interaction: discord.Interaction, task_id: str):
        result = await handler.execute("skip_task", {"task_id": task_id})
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        unblocked_count = result.get("unblocked_count", 0)
        msg = f"⏭️ Task `{task_id}` skipped (marked COMPLETED)."
        if unblocked_count:
            unblocked_list = ", ".join(
                f"`{t['id']}`" for t in result.get("unblocked", [])
            )
            msg += f"\n{unblocked_count} task(s) unblocked: {unblocked_list}"
        await interaction.response.send_message(msg)

    @bot.tree.command(
        name="chain-health",
        description="Check dependency chain health for stuck tasks",
    )
    @app_commands.describe(
        task_id="(Optional) Check a specific blocked task",
        project_id="(Optional) Check all blocked chains in a project (auto-detected from channel)",
    )
    async def chain_health_command(
        interaction: discord.Interaction,
        task_id: str | None = None,
        project_id: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project_id)
        args = {}
        if task_id:
            args["task_id"] = task_id
        if project_id:
            args["project_id"] = project_id
        result = await handler.execute("get_chain_health", args)
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return

        if "task_id" in result:
            stuck = result.get("stuck_downstream", [])
            if not stuck:
                await interaction.response.send_message(
                    f"✅ No stuck downstream tasks for `{result['task_id']}`."
                )
            else:
                lines = [
                    f"⛓️ **Stuck Chain:** `{result['task_id']}` — {result.get('title', '')}",
                    f"{len(stuck)} downstream task(s) stuck:",
                ]
                for t in stuck[:15]:
                    lines.append(f"  • `{t['id']}` — {t['title']} ({t['status']})")
                if len(stuck) > 15:
                    lines.append(f"  … and {len(stuck) - 15} more")
                await interaction.response.send_message("\n".join(lines))
        else:
            chains = result.get("stuck_chains", [])
            if not chains:
                await interaction.response.send_message(
                    f"✅ No stuck dependency chains"
                    + (f" in project `{result.get('project_id')}`" if result.get("project_id") else "")
                    + "."
                )
            else:
                lines = [f"⛓️ **{len(chains)} stuck chain(s):**"]
                for chain in chains[:10]:
                    bt = chain["blocked_task"]
                    lines.append(
                        f"  • `{bt['id']}` — {bt['title']} "
                        f"→ {chain['stuck_count']} stuck task(s)"
                    )
                if len(chains) > 10:
                    lines.append(f"  … and {len(chains) - 10} more chains")
                await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(name="set-status", description="Change the status of a task")
    @app_commands.describe(
        task_id="Task ID to update",
        status="New status for the task",
    )
    @app_commands.choices(status=[
        app_commands.Choice(name="DEFINED",     value="DEFINED"),
        app_commands.Choice(name="READY",       value="READY"),
        app_commands.Choice(name="IN_PROGRESS", value="IN_PROGRESS"),
        app_commands.Choice(name="COMPLETED",   value="COMPLETED"),
        app_commands.Choice(name="FAILED",      value="FAILED"),
        app_commands.Choice(name="BLOCKED",     value="BLOCKED"),
    ])
    async def set_status_command(
        interaction: discord.Interaction,
        task_id: str,
        status: app_commands.Choice[str],
    ):
        result = await handler.execute("set_task_status", {
            "task_id": task_id,
            "status": status.value,
        })
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        old_status = result.get("old_status", "?")
        new_status = result.get("new_status", status.value)
        old_emoji = _STATUS_EMOJIS.get(old_status, "❓")
        new_emoji = _STATUS_EMOJIS.get(new_status, "❓")
        embed = discord.Embed(
            title="Task Status Updated",
            color=_STATUS_COLORS.get(new_status, 0x95a5a6),
        )
        embed.add_field(
            name="Task",
            value=f"`{task_id}` — {result.get('title', '')}",
            inline=False,
        )
        embed.add_field(
            name="Status Change",
            value=f"{old_emoji} **{old_status}** → {new_emoji} **{new_status}**",
            inline=False,
        )
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="task-result", description="Show the results/output of a completed task")
    @app_commands.describe(task_id="Task ID to inspect")
    async def task_result_command(interaction: discord.Interaction, task_id: str):
        await interaction.response.defer(ephemeral=True)
        result = await handler.execute("get_task_result", {"task_id": task_id})
        if "error" in result:
            await interaction.followup.send(f"Error: {result['error']}", ephemeral=True)
            return
        embed = discord.Embed(
            title=f"Task Result: {task_id}",
            color=0x2ecc71 if result.get("result") == "completed" else 0xe74c3c,
        )
        embed.add_field(name="Result", value=result.get("result", "unknown"), inline=True)
        summary = result.get("summary") or ""
        if summary:
            summary_text = summary[:1000] + ("…" if len(summary) > 1000 else "")
            embed.add_field(name="Summary", value=summary_text, inline=False)
        files = result.get("files_changed") or []
        if files:
            file_list = "\n".join(f"• `{f}`" for f in files[:20])
            if len(files) > 20:
                file_list += f"\n_...and {len(files) - 20} more_"
            embed.add_field(name="Files Changed", value=file_list, inline=False)
        tokens = result.get("tokens_used", 0)
        if tokens:
            embed.add_field(name="Tokens Used", value=f"{tokens:,}", inline=True)
        error_msg = result.get("error_message") or ""
        if error_msg:
            snippet = error_msg[:500] + ("…" if len(error_msg) > 500 else "")
            embed.add_field(name="Error", value=f"```\n{snippet}\n```", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @bot.tree.command(name="task-diff", description="Show the git diff for a task's branch")
    @app_commands.describe(task_id="Task ID")
    async def task_diff_command(interaction: discord.Interaction, task_id: str):
        await interaction.response.defer(ephemeral=True)
        result = await handler.execute("get_task_diff", {"task_id": task_id})
        if "error" in result:
            await interaction.followup.send(f"Error: {result['error']}", ephemeral=True)
            return
        diff = result.get("diff", "(no changes)")
        branch = result.get("branch", "?")
        header = f"**Branch:** `{branch}`\n"
        if len(diff) > 1800:
            file = discord.File(
                fp=io.BytesIO(diff.encode("utf-8")),
                filename=f"diff-{task_id}.patch",
            )
            await interaction.followup.send(
                f"{header}*Diff attached ({len(diff):,} chars)*",
                file=file, ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"{header}```diff\n{diff}\n```", ephemeral=True,
            )

    @bot.tree.command(
        name="agent-error",
        description="Show the last error recorded for a task",
    )
    @app_commands.describe(task_id="Task ID to inspect")
    async def agent_error_command(interaction: discord.Interaction, task_id: str):
        await interaction.response.defer(ephemeral=True)
        result = await handler.execute("get_agent_error", {"task_id": task_id})
        if "error" in result:
            await interaction.followup.send(f"Error: {result['error']}", ephemeral=True)
            return
        embed = discord.Embed(
            title=f"Agent Error Report: {task_id}",
            color=0xe74c3c,
        )
        embed.add_field(name="Task", value=result.get("title", ""), inline=False)
        embed.add_field(name="Status", value=result.get("status", ""), inline=True)
        embed.add_field(name="Retries", value=result.get("retries", ""), inline=True)

        if result.get("message"):
            embed.description = f"_{result['message']}_"
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed.add_field(name="Result", value=result.get("result", "unknown"), inline=True)
        embed.add_field(
            name="Error Type",
            value=f"**{result.get('error_type', 'unknown')}**",
            inline=False,
        )
        error_msg = result.get("error_message") or ""
        if error_msg:
            snippet = error_msg[:1000]
            if len(error_msg) > 1000:
                snippet += "\n… _(truncated)_"
            embed.add_field(name="Error Detail", value=f"```\n{snippet}\n```", inline=False)
        else:
            embed.add_field(name="Error Detail", value="_No error message recorded._", inline=False)
        embed.add_field(
            name="Suggested Fix",
            value=result.get("suggested_fix", "Review the logs"),
            inline=False,
        )
        summary = result.get("agent_summary") or ""
        if summary:
            embed.add_field(
                name="Agent Summary",
                value=summary[:500] + ("…" if len(summary) > 500 else ""),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ===================================================================
    # AGENT COMMANDS
    # ===================================================================

    @bot.tree.command(name="create-agent", description="Register a new agent")
    @app_commands.describe(
        name="Agent display name",
        agent_type="Agent type (claude, codex, cursor, aider)",
        repo_id="Repository ID to assign as workspace (optional)",
    )
    @app_commands.choices(agent_type=[
        app_commands.Choice(name="claude", value="claude"),
        app_commands.Choice(name="codex",  value="codex"),
        app_commands.Choice(name="cursor", value="cursor"),
        app_commands.Choice(name="aider",  value="aider"),
    ])
    async def create_agent_command(
        interaction: discord.Interaction,
        name: str,
        agent_type: app_commands.Choice[str] | None = None,
        repo_id: str | None = None,
    ):
        args: dict = {"name": name}
        if agent_type:
            args["agent_type"] = agent_type.value
        if repo_id:
            args["repo_id"] = repo_id
        result = await handler.execute("create_agent", args)
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        embed = discord.Embed(title="✅ Agent Registered", color=0x2ecc71)
        embed.add_field(name="Name", value=name, inline=True)
        embed.add_field(name="ID", value=f"`{result['created']}`", inline=True)
        embed.add_field(name="Type", value=args.get("agent_type", "claude"), inline=True)
        if repo_id:
            embed.add_field(name="Repo", value=f"`{repo_id}`", inline=True)
        await interaction.response.send_message(embed=embed)

    # ===================================================================
    # REPO COMMANDS
    # ===================================================================

    @bot.tree.command(name="repos", description="List registered repositories")
    @app_commands.describe(project_id="Filter by project ID (auto-detected from channel)")
    async def repos_command(interaction: discord.Interaction, project_id: str | None = None):
        project_id = await _resolve_project_from_context(interaction, project_id)
        args = {}
        if project_id:
            args["project_id"] = project_id
        result = await handler.execute("list_repos", args)
        repos = result.get("repos", [])
        if not repos:
            await interaction.response.send_message("No repositories registered.")
            return
        lines = ["## Repositories"]
        for r in repos:
            source_info = ""
            if r["source_type"] == "link" and r.get("source_path"):
                source_info = f" → `{r['source_path']}`"
            elif r["source_type"] == "clone" and r.get("url"):
                source_info = f" → {r['url']}"
            lines.append(
                f"• **{r['id']}** ({r['source_type']}){source_info} — "
                f"project: `{r['project_id']}`, branch: `{r['default_branch']}`"
            )
        msg = "\n".join(lines)
        if len(msg) > 2000:
            msg = msg[:1997] + "..."
        await interaction.response.send_message(msg)

    @bot.tree.command(name="add-repo", description="Register a repository for a project")
    @app_commands.describe(
        project_id="Project ID (auto-detected from channel)",
        source="How to set up the repo",
        url="Git URL (for clone)",
        path="Existing directory path (for link)",
        name="Repo name (optional)",
        default_branch="Default branch (default: main)",
    )
    @app_commands.choices(source=[
        app_commands.Choice(name="clone", value="clone"),
        app_commands.Choice(name="link",  value="link"),
        app_commands.Choice(name="init",  value="init"),
    ])
    async def add_repo_command(
        interaction: discord.Interaction,
        source: app_commands.Choice[str],
        project_id: str | None = None,
        url: str | None = None,
        path: str | None = None,
        name: str | None = None,
        default_branch: str = "main",
    ):
        project_id = await _resolve_project_from_context(interaction, project_id)
        if not project_id:
            await interaction.response.send_message(f"Error: {_NO_PROJECT_MSG}", ephemeral=True)
            return
        args: dict = {
            "project_id": project_id,
            "source": source.value,
            "default_branch": default_branch,
        }
        if url:
            args["url"] = url
        if path:
            args["path"] = path
        if name:
            args["name"] = name
        result = await handler.execute("add_repo", args)
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        embed = discord.Embed(title="✅ Repo Registered", color=0x2ecc71)
        embed.add_field(name="ID", value=f"`{result['created']}`", inline=True)
        embed.add_field(name="Source", value=source.value, inline=True)
        embed.add_field(name="Project", value=f"`{project_id}`", inline=True)
        await interaction.response.send_message(embed=embed)

    # ===================================================================
    # GIT COMMANDS
    # ===================================================================

    @bot.tree.command(
        name="git-status",
        description="Show the git status of a project's repository",
    )
    @app_commands.describe(project_id="Project ID to check git status for (auto-detected from channel)")
    async def git_status_command(interaction: discord.Interaction, project_id: str | None = None):
        project_id = await _resolve_project_from_context(interaction, project_id)
        if not project_id:
            await interaction.response.send_message(f"Error: {_NO_PROJECT_MSG}", ephemeral=True)
            return
        await interaction.response.defer()
        result = await handler.execute("get_git_status", {"project_id": project_id})
        if "error" in result:
            await interaction.followup.send(f"Error: {result['error']}")
            return

        project_name = result.get("project_name", project_id)
        repo_statuses = result.get("repos", [])
        sections: list[str] = []

        for rs in repo_statuses:
            if "error" in rs:
                sections.append(
                    f"### Repo: `{rs['repo_id']}`\n⚠️ {rs['error']}"
                )
                continue
            lines = [f"### Repo: `{rs['repo_id']}`"]
            if rs.get("path"):
                lines.append(f"**Path:** `{rs['path']}`")
            if rs.get("branch"):
                lines.append(f"**Branch:** `{rs['branch']}`")
            status_output = rs.get("status", "(clean)")
            lines.append(f"\n**Status:**\n```\n{status_output}\n```")
            if rs.get("recent_commits"):
                lines.append(f"**Recent commits:**\n```\n{rs['recent_commits']}\n```")
            sections.append("\n".join(lines))

        header = f"## Git Status: {project_name} (`{project_id}`)\n"
        full_message = header + "\n\n".join(sections)
        await _send_long(interaction, full_message, followup=True)

    @bot.tree.command(
        name="git-commit",
        description="Stage all changes and commit in a repository",
    )
    @app_commands.describe(
        repo_id="Repository ID",
        message="Commit message",
    )
    async def git_commit_command(
        interaction: discord.Interaction,
        repo_id: str,
        message: str,
    ):
        await interaction.response.defer()
        result = await handler.execute("git_commit", {
            "repo_id": repo_id,
            "message": message,
        })
        if "error" in result:
            await interaction.followup.send(f"Error: {result['error']}")
            return
        if result.get("committed"):
            await interaction.followup.send(
                f"✅ Committed in `{repo_id}`: {message}"
            )
        else:
            await interaction.followup.send(
                f"ℹ️ Nothing to commit in `{repo_id}` — working tree clean"
            )

    @bot.tree.command(
        name="git-push",
        description="Push a branch to remote origin",
    )
    @app_commands.describe(
        repo_id="Repository ID",
        branch="Branch name to push (defaults to current branch)",
    )
    async def git_push_command(
        interaction: discord.Interaction,
        repo_id: str,
        branch: str | None = None,
    ):
        await interaction.response.defer()
        args = {"repo_id": repo_id}
        if branch:
            args["branch"] = branch
        result = await handler.execute("git_push", args)
        if "error" in result:
            await interaction.followup.send(f"Error: {result['error']}")
            return
        await interaction.followup.send(
            f"✅ Pushed `{result['pushed']}` in `{repo_id}`"
        )

    @bot.tree.command(
        name="git-branch",
        description="Create and switch to a new git branch",
    )
    @app_commands.describe(
        repo_id="Repository ID",
        branch_name="Name for the new branch",
    )
    async def git_branch_command(
        interaction: discord.Interaction,
        repo_id: str,
        branch_name: str,
    ):
        await interaction.response.defer()
        result = await handler.execute("git_create_branch", {
            "repo_id": repo_id,
            "branch_name": branch_name,
        })
        if "error" in result:
            await interaction.followup.send(f"Error: {result['error']}")
            return
        await interaction.followup.send(
            f"✅ Created and switched to branch `{branch_name}` in `{repo_id}`"
        )

    @bot.tree.command(
        name="git-merge",
        description="Merge a branch into the default branch",
    )
    @app_commands.describe(
        repo_id="Repository ID",
        branch_name="Branch to merge",
        default_branch="Target branch (defaults to repo's default branch)",
    )
    async def git_merge_command(
        interaction: discord.Interaction,
        repo_id: str,
        branch_name: str,
        default_branch: str | None = None,
    ):
        await interaction.response.defer()
        args = {"repo_id": repo_id, "branch_name": branch_name}
        if default_branch:
            args["default_branch"] = default_branch
        result = await handler.execute("git_merge", args)
        if "error" in result:
            await interaction.followup.send(f"Error: {result['error']}")
            return
        if result.get("merged"):
            await interaction.followup.send(
                f"✅ Merged `{branch_name}` into `{result['into']}` in `{repo_id}`"
            )
        else:
            await interaction.followup.send(
                f"⚠️ Merge conflict — `{branch_name}` could not be merged into "
                f"`{result.get('into', 'default')}` in `{repo_id}`. Merge was aborted."
            )

    @bot.tree.command(
        name="git-pr",
        description="Create a GitHub pull request",
    )
    @app_commands.describe(
        repo_id="Repository ID",
        title="PR title",
        body="PR description (optional)",
        branch="Head branch (defaults to current)",
        base="Base branch (defaults to repo default)",
    )
    async def git_pr_command(
        interaction: discord.Interaction,
        repo_id: str,
        title: str,
        body: str = "",
        branch: str | None = None,
        base: str | None = None,
    ):
        await interaction.response.defer()
        args: dict = {"repo_id": repo_id, "title": title, "body": body}
        if branch:
            args["branch"] = branch
        if base:
            args["base"] = base
        result = await handler.execute("git_create_pr", args)
        if "error" in result:
            await interaction.followup.send(f"Error: {result['error']}")
            return
        pr_url = result.get("pr_url", "")
        await interaction.followup.send(
            f"✅ PR created: {pr_url}\n"
            f"**Branch:** `{result.get('branch', '?')}` → `{result.get('base', '?')}`"
        )

    @bot.tree.command(
        name="git-files",
        description="List files changed compared to a base branch",
    )
    @app_commands.describe(
        repo_id="Repository ID",
        base_branch="Branch to compare against (defaults to repo default)",
    )
    async def git_files_command(
        interaction: discord.Interaction,
        repo_id: str,
        base_branch: str | None = None,
    ):
        await interaction.response.defer()
        args = {"repo_id": repo_id}
        if base_branch:
            args["base_branch"] = base_branch
        result = await handler.execute("git_changed_files", args)
        if "error" in result:
            await interaction.followup.send(f"Error: {result['error']}")
            return
        files = result.get("files", [])
        count = result.get("count", 0)
        base = result.get("base_branch", "main")
        if not files:
            await interaction.followup.send(
                f"No files changed in `{repo_id}` vs `{base}`"
            )
            return
        file_list = "\n".join(f"• `{f}`" for f in files[:50])
        if count > 50:
            file_list += f"\n_...and {count - 50} more_"
        msg = (
            f"## Changed Files: `{repo_id}` vs `{base}`\n"
            f"**{count} file(s) changed:**\n{file_list}"
        )
        await _send_long(interaction, msg, followup=True)

    @bot.tree.command(
        name="git-log",
        description="Show recent commit log for a repository",
    )
    @app_commands.describe(
        repo_id="Repository ID",
        count="Number of commits to show (default 10)",
    )
    async def git_log_command(
        interaction: discord.Interaction,
        repo_id: str,
        count: int = 10,
    ):
        await interaction.response.defer()
        result = await handler.execute("git_log", {
            "repo_id": repo_id,
            "count": count,
        })
        if "error" in result:
            await interaction.followup.send(f"Error: {result['error']}")
            return
        branch = result.get("branch", "?")
        log = result.get("log", "(no commits)")
        msg = (
            f"## Git Log: `{repo_id}` (branch: `{branch}`)\n"
            f"```\n{log}\n```"
        )
        await _send_long(interaction, msg, followup=True)

    @bot.tree.command(
        name="git-diff",
        description="Show diff of working tree or against a base branch",
    )
    @app_commands.describe(
        project_id="Project ID (auto-detected from channel)",
        repo_id="Repository ID (optional, defaults to first repo in project)",
        base_branch="Branch to diff against (omit for unstaged working tree changes)",
    )
    async def git_diff_command(
        interaction: discord.Interaction,
        project_id: str | None = None,
        repo_id: str | None = None,
        base_branch: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project_id)
        if not project_id:
            await interaction.response.send_message(
                "Could not determine project. Please provide a project_id.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        args: dict = {"project_id": project_id}
        if repo_id:
            args["repo_id"] = repo_id
        if base_branch:
            args["base_branch"] = base_branch
        result = await handler.execute("git_diff", args)
        if "error" in result:
            await interaction.followup.send(
                f"Error: {result['error']}", ephemeral=True,
            )
            return
        diff = result.get("diff", "(no changes)")
        base_label = result.get("base_branch", "(working tree)")
        repo_label = result.get("repo_id", "?")
        header = f"**Repo:** `{repo_label}` · **Base:** `{base_label}`\n"
        if len(diff) > 1800:
            file = discord.File(
                fp=io.BytesIO(diff.encode("utf-8")),
                filename=f"diff-{project_id}.patch",
            )
            await interaction.followup.send(
                f"{header}*Diff attached ({len(diff):,} chars)*",
                file=file, ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"{header}```diff\n{diff}\n```", ephemeral=True,
            )

    # ===================================================================
    # HOOK COMMANDS
    # ===================================================================

    @bot.tree.command(name="hooks", description="List automation hooks")
    @app_commands.describe(project_id="Filter by project ID (auto-detected from channel)")
    async def hooks_command(interaction: discord.Interaction, project_id: str | None = None):
        project_id = await _resolve_project_from_context(interaction, project_id)
        args = {}
        if project_id:
            args["project_id"] = project_id
        result = await handler.execute("list_hooks", args)
        hooks = result.get("hooks", [])
        if not hooks:
            await interaction.response.send_message("No hooks configured.")
            return
        lines = ["## Hooks"]
        for h in hooks:
            status = "✅ enabled" if h.get("enabled") else "❌ disabled"
            trigger = h.get("trigger", {})
            trigger_desc = trigger.get("type", "unknown") if isinstance(trigger, dict) else "unknown"
            if trigger_desc == "periodic":
                interval = trigger.get("interval_seconds", 0) if isinstance(trigger, dict) else 0
                trigger_desc = f"periodic ({interval}s)"
            elif trigger_desc == "event":
                evt_type = trigger.get("event_type", "?") if isinstance(trigger, dict) else "?"
                trigger_desc = f"event: {evt_type}"
            lines.append(
                f"• **{h['name']}** (`{h['id']}`) — {status}, trigger: {trigger_desc}, "
                f"project: `{h['project_id']}`"
            )
        msg = "\n".join(lines)
        if len(msg) > 2000:
            msg = msg[:1997] + "..."
        await interaction.response.send_message(msg)

    @bot.tree.command(name="create-hook", description="Create an automation hook")
    @app_commands.describe(
        project_id="Project ID (auto-detected from channel)",
        name="Hook name",
        trigger_type="Trigger type",
        trigger_value="Interval seconds (periodic) or event type (event)",
        prompt_template="Prompt template with {{step_N}} and {{event}} placeholders",
        cooldown_seconds="Min seconds between runs (default 3600)",
    )
    @app_commands.choices(trigger_type=[
        app_commands.Choice(name="periodic", value="periodic"),
        app_commands.Choice(name="event",    value="event"),
    ])
    async def create_hook_command(
        interaction: discord.Interaction,
        name: str,
        trigger_type: app_commands.Choice[str],
        trigger_value: str,
        prompt_template: str,
        project_id: str | None = None,
        cooldown_seconds: int = 3600,
    ):
        project_id = await _resolve_project_from_context(interaction, project_id)
        if not project_id:
            await interaction.response.send_message(f"Error: {_NO_PROJECT_MSG}", ephemeral=True)
            return
        if trigger_type.value == "periodic":
            try:
                interval = int(trigger_value)
            except ValueError:
                await interaction.response.send_message(
                    "Error: trigger_value must be an integer (seconds) for periodic hooks.",
                    ephemeral=True,
                )
                return
            trigger = {"type": "periodic", "interval_seconds": interval}
        else:
            trigger = {"type": "event", "event_type": trigger_value}

        result = await handler.execute("create_hook", {
            "project_id": project_id,
            "name": name,
            "trigger": trigger,
            "prompt_template": prompt_template,
            "cooldown_seconds": cooldown_seconds,
        })
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ Hook **{name}** (`{result['created']}`) created in `{project_id}`"
        )

    @bot.tree.command(name="edit-hook", description="Edit an automation hook")
    @app_commands.describe(
        hook_id="Hook ID",
        enabled="Enable or disable the hook",
        prompt_template="New prompt template (optional)",
        cooldown_seconds="New cooldown in seconds (optional)",
    )
    async def edit_hook_command(
        interaction: discord.Interaction,
        hook_id: str,
        enabled: bool | None = None,
        prompt_template: str | None = None,
        cooldown_seconds: int | None = None,
    ):
        args: dict = {"hook_id": hook_id}
        if enabled is not None:
            args["enabled"] = enabled
        if prompt_template is not None:
            args["prompt_template"] = prompt_template
        if cooldown_seconds is not None:
            args["cooldown_seconds"] = cooldown_seconds
        result = await handler.execute("edit_hook", args)
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        fields = ", ".join(result.get("fields", []))
        await interaction.response.send_message(
            f"✅ Hook `{hook_id}` updated: {fields}"
        )

    @bot.tree.command(name="delete-hook", description="Delete an automation hook")
    @app_commands.describe(hook_id="Hook ID to delete")
    async def delete_hook_command(interaction: discord.Interaction, hook_id: str):
        result = await handler.execute("delete_hook", {"hook_id": hook_id})
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🗑️ Hook **{result.get('name', hook_id)}** (`{hook_id}`) deleted."
        )

    @bot.tree.command(name="hook-runs", description="Show recent execution history for a hook")
    @app_commands.describe(
        hook_id="Hook ID",
        limit="Number of runs to show (default 10)",
    )
    async def hook_runs_command(
        interaction: discord.Interaction, hook_id: str, limit: int = 10
    ):
        result = await handler.execute("list_hook_runs", {"hook_id": hook_id, "limit": limit})
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        runs = result.get("runs", [])
        hook_name = result.get("hook_name", hook_id)
        if not runs:
            await interaction.response.send_message(f"No runs found for hook **{hook_name}**.")
            return
        lines = [f"## Hook Runs: {hook_name}"]
        for r in runs:
            status_emoji = {"completed": "✅", "failed": "❌", "skipped": "⏭️"}.get(
                r.get("status", ""), "🔄"
            )
            line = f"• {status_emoji} {r.get('trigger_reason', '?')} — tokens: {r.get('tokens_used', 0):,}"
            if r.get("skipped_reason"):
                line += f" (skipped: {r['skipped_reason'][:50]})"
            lines.append(line)
        msg = "\n".join(lines)
        if len(msg) > 2000:
            msg = msg[:1997] + "..."
        await interaction.response.send_message(msg)

    @bot.tree.command(name="fire-hook", description="Manually trigger a hook immediately")
    @app_commands.describe(hook_id="Hook ID to fire")
    async def fire_hook_command(interaction: discord.Interaction, hook_id: str):
        result = await handler.execute("fire_hook", {"hook_id": hook_id})
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🔥 Hook `{hook_id}` fired — status: {result.get('status', 'running')}"
        )

    # ===================================================================
    # NOTES COMMANDS
    # ===================================================================

    @bot.tree.command(name="notes", description="View and manage notes for a project")
    @app_commands.describe(project_id="Project ID (auto-detected from channel)")
    async def notes_command(interaction: discord.Interaction, project_id: str | None = None):
        project_id = await _resolve_project_from_context(interaction, project_id)
        if not project_id:
            await interaction.response.send_message(f"Error: {_NO_PROJECT_MSG}", ephemeral=True)
            return
        result = await handler.execute("list_notes", {"project_id": project_id})
        if "error" in result:
            await interaction.response.send_message(
                f"Error: {result['error']}", ephemeral=True
            )
            return

        # Look up project name for the header
        projects_result = await handler.execute("list_projects", {})
        project_name = project_id
        for p in projects_result.get("projects", []):
            if p["id"] == project_id:
                project_name = p["name"]
                break

        notes = result.get("notes", [])
        lines = [f"## Notes: {project_name}"]
        if notes:
            for note in notes:
                size_kb = note["size_bytes"] / 1024
                lines.append(f"• **{note['title']}** (`{note['name']}`, {size_kb:.1f} KB)")
        else:
            lines.append("_No notes yet._")

        listing = "\n".join(lines)
        await interaction.response.send_message(listing)

        # Create a thread for interactive note management
        msg = await interaction.original_response()
        thread = await msg.create_thread(
            name=f"Notes: {project_name}"[:100],
            auto_archive_duration=1440,
        )
        await thread.send(
            f"Ask me to read, create, or edit notes for **{project_name}**."
        )
        bot.register_notes_thread(thread.id, project_id)

    @bot.tree.command(name="write-note", description="Create or update a project note")
    @app_commands.describe(
        project_id="Project ID (auto-detected from channel)",
        title="Note title",
        content="Note content (markdown)",
    )
    async def write_note_command(
        interaction: discord.Interaction,
        title: str,
        content: str,
        project_id: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project_id)
        if not project_id:
            await interaction.response.send_message(f"Error: {_NO_PROJECT_MSG}", ephemeral=True)
            return
        result = await handler.execute("write_note", {
            "project_id": project_id,
            "title": title,
            "content": content,
        })
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        status = result.get("status", "created")
        await interaction.response.send_message(
            f"📝 Note **{title}** {status} in `{project_id}`"
        )

    @bot.tree.command(name="delete-note", description="Delete a project note")
    @app_commands.describe(
        project_id="Project ID (auto-detected from channel)",
        title="Note title",
    )
    async def delete_note_command(
        interaction: discord.Interaction,
        title: str,
        project_id: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project_id)
        if not project_id:
            await interaction.response.send_message(f"Error: {_NO_PROJECT_MSG}", ephemeral=True)
            return
        result = await handler.execute("delete_note", {
            "project_id": project_id,
            "title": title,
        })
        if "error" in result:
            await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🗑️ Note **{title}** deleted from `{project_id}`"
        )

    # ===================================================================
    # SYSTEM CONTROL COMMANDS
    # ===================================================================

    @bot.tree.command(name="orchestrator", description="Pause or resume task scheduling")
    @app_commands.describe(action="pause | resume | status")
    @app_commands.choices(action=[
        app_commands.Choice(name="pause",  value="pause"),
        app_commands.Choice(name="resume", value="resume"),
        app_commands.Choice(name="status", value="status"),
    ])
    async def orchestrator_command(
        interaction: discord.Interaction, action: app_commands.Choice[str]
    ):
        result = await handler.execute("orchestrator_control", {"action": action.value})
        if action.value == "pause":
            await interaction.response.send_message(
                "⏸ Orchestrator paused — no new tasks will be scheduled."
            )
        elif action.value == "resume":
            await interaction.response.send_message("▶ Orchestrator resumed.")
        else:
            running = result.get("running_tasks", 0)
            state = "⏸ PAUSED" if result.get("status") == "paused" else "▶ RUNNING"
            await interaction.response.send_message(
                f"**Orchestrator status:** {state}\n"
                f"**Running tasks:** {running}"
            )

    @bot.tree.command(name="restart", description="Restart the agent-queue daemon")
    async def restart_command(interaction: discord.Interaction):
        await interaction.response.send_message("Restarting agent-queue...")
        bot._restart_requested = True
        await handler.execute("restart_daemon", {})
