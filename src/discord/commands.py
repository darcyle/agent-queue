from __future__ import annotations

import os
import random
import signal
import uuid

import discord
from discord import app_commands
from discord.ext import commands

from src.models import Project, ProjectStatus, Task, TaskStatus
from src.discord.notifications import classify_error


def setup_commands(bot: commands.Bot) -> None:
    """Register all slash commands on the bot."""

    # ---------------------------------------------------------------------------
    # Shared task-detail helper
    # ---------------------------------------------------------------------------

    async def _format_task_detail(task_id: str) -> str | None:
        """Fetch and format full task details. Returns None if task not found."""
        task = await bot.orchestrator.db.get_task(task_id)
        if not task:
            return None
        lines = [
            f"## Task `{task.id}`",
            f"**Title:** {task.title}",
            f"**Status:** {task.status.value}",
            f"**Project:** `{task.project_id}`",
            f"**Priority:** {task.priority}",
        ]
        if task.assigned_agent_id:
            lines.append(f"**Agent:** {task.assigned_agent_id}")
        if task.retry_count:
            lines.append(f"**Retries:** {task.retry_count} / {task.max_retries}")
        if task.description:
            desc = (
                task.description
                if len(task.description) <= 800
                else task.description[:800] + "..."
            )
            lines.append(f"\n**Description:**\n{desc}")
        return "\n".join(lines)

    # ---------------------------------------------------------------------------
    # Interactive UI components for task lists
    # ---------------------------------------------------------------------------

    # Discord hard limits: 5 rows × 5 buttons = 25 buttons per message.
    _MAX_TASK_BUTTONS = 25

    class TaskDetailButton(discord.ui.Button):
        """Button that fetches and displays full task details when clicked."""

        def __init__(self, task_id: str) -> None:
            # custom_id capped at 100 chars (Discord limit)
            custom_id = f"task_detail:{task_id}"[:100]
            super().__init__(
                style=discord.ButtonStyle.secondary,
                label="Details",
                custom_id=custom_id,
            )
            self.task_id = task_id

        async def callback(self, interaction: discord.Interaction) -> None:
            # Defer immediately to satisfy Discord's 3-second acknowledgement window.
            await interaction.response.defer(ephemeral=True)
            detail = await _format_task_detail(self.task_id)
            if detail is None:
                await interaction.followup.send(
                    f"Task `{self.task_id}` not found.", ephemeral=True
                )
            else:
                await interaction.followup.send(detail, ephemeral=True)

    class TaskListView(discord.ui.View):
        """Attaches one Details button per task (up to the Discord 25-button max)."""

        def __init__(self, tasks: list) -> None:
            # 5-minute window; buttons silently stop responding after this.
            super().__init__(timeout=300)
            for task in tasks[:_MAX_TASK_BUTTONS]:
                self.add_item(TaskDetailButton(task.id))

    # ---------------------------------------------------------------------------

    @bot.tree.command(name="status", description="Show system status overview")
    async def status_command(interaction: discord.Interaction):
        db = bot.orchestrator.db
        projects = await db.list_projects()
        agents = await db.list_agents()
        tasks = await db.list_tasks()

        active_tasks = [t for t in tasks if t.status == TaskStatus.IN_PROGRESS]
        ready_tasks = [t for t in tasks if t.status == TaskStatus.READY]
        paused_tasks = [t for t in tasks if t.status == TaskStatus.PAUSED]

        lines = []
        if bot.orchestrator._paused:
            lines.append("⏸ **Orchestrator is PAUSED** — scheduling suspended")
            lines.append("")
        lines += [
            "## System Status",
            f"**Projects:** {len(projects)}",
            f"**Tasks:** {len(tasks)} total — "
            f"{len(active_tasks)} active, {len(ready_tasks)} ready, {len(paused_tasks)} paused",
            "",
        ]

        # Agent details
        if agents:
            lines.append("**Agents:**")
            for a in agents:
                if a.current_task_id:
                    task = await db.get_task(a.current_task_id)
                    task_desc = f"working on `{task.id}` — {task.title}" if task else f"task `{a.current_task_id}`"
                    lines.append(f"• **{a.name}** ({a.state.value}) → {task_desc}")
                else:
                    lines.append(f"• **{a.name}** ({a.state.value})")
        else:
            lines.append("**Agents:** none registered")

        # Ready tasks waiting for an agent
        if ready_tasks:
            lines.append("")
            lines.append(f"**Queued ({len(ready_tasks)}):**")
            for t in ready_tasks[:5]:
                lines.append(f"• `{t.id}` {t.title}")
            if len(ready_tasks) > 5:
                lines.append(f"_...and {len(ready_tasks) - 5} more_")

        await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(name="projects", description="List all projects")
    async def projects_command(interaction: discord.Interaction):
        projects = await bot.orchestrator.db.list_projects()
        if not projects:
            await interaction.response.send_message("No projects configured.")
            return
        lines = []
        for p in projects:
            lines.append(f"• **{p.name}** (`{p.id}`) — {p.status.value}, weight={p.credit_weight}")
        await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(name="tasks", description="List tasks for a project")
    @app_commands.describe(project_id="Project ID to filter by")
    async def tasks_command(interaction: discord.Interaction, project_id: str | None = None):
        tasks = await bot.orchestrator.db.list_tasks(project_id=project_id)
        if not tasks:
            await interaction.response.send_message("No tasks found.")
            return
        # Slice once so both the text lines and the button view use the same tasks.
        shown_tasks = tasks[:_MAX_TASK_BUTTONS]  # up to 25 (Discord's hard limit)
        lines = []
        for t in shown_tasks:
            lines.append(f"• `{t.id}` **{t.title}** — {t.status.value}")
        if len(tasks) > len(shown_tasks):
            lines.append(f"_...and {len(tasks) - len(shown_tasks)} more_")
        # Attach a "Details" button for each shown task so users can click through
        # to the full task detail without typing /task <id>.
        view = TaskListView(shown_tasks)
        await interaction.response.send_message("\n".join(lines), view=view)

    @bot.tree.command(name="agents", description="List all agents")
    async def agents_command(interaction: discord.Interaction):
        agents = await bot.orchestrator.db.list_agents()
        if not agents:
            await interaction.response.send_message("No agents configured.")
            return
        lines = []
        for a in agents:
            task_info = f" → `{a.current_task_id}`" if a.current_task_id else ""
            lines.append(f"• **{a.name}** (`{a.id}`) — {a.state.value}{task_info}")
        await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(name="budget", description="Show token budget usage")
    async def budget_command(interaction: discord.Interaction):
        projects = await bot.orchestrator.db.list_projects()
        lines = []
        for p in projects:
            usage = await bot.orchestrator.db.get_project_token_usage(p.id)
            limit_str = f"/ {p.budget_limit:,}" if p.budget_limit else "/ unlimited"
            lines.append(f"• **{p.name}**: {usage:,} tokens {limit_str}")
        if not lines:
            await interaction.response.send_message("No projects configured.")
            return
        await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(name="pause", description="Pause a project")
    @app_commands.describe(project_id="Project ID to pause")
    async def pause_command(interaction: discord.Interaction, project_id: str):
        project = await bot.orchestrator.db.get_project(project_id)
        if not project:
            await interaction.response.send_message(f"Project `{project_id}` not found.")
            return
        await bot.orchestrator.db.update_project(project_id, status=ProjectStatus.PAUSED)
        await interaction.response.send_message(f"Project **{project.name}** paused.")

    @bot.tree.command(name="resume", description="Resume a paused project")
    @app_commands.describe(project_id="Project ID to resume")
    async def resume_command(interaction: discord.Interaction, project_id: str):
        project = await bot.orchestrator.db.get_project(project_id)
        if not project:
            await interaction.response.send_message(f"Project `{project_id}` not found.")
            return
        await bot.orchestrator.db.update_project(project_id, status=ProjectStatus.ACTIVE)
        await interaction.response.send_message(f"Project **{project.name}** resumed.")

    @bot.tree.command(name="restart", description="Restart the agent-queue daemon")
    async def restart_command(interaction: discord.Interaction):
        await interaction.response.send_message("Restarting agent-queue...")
        bot._restart_requested = True
        os.kill(os.getpid(), signal.SIGTERM)

    @bot.tree.command(name="orchestrator", description="Pause or resume task scheduling")
    @app_commands.describe(action="pause | resume | status")
    @app_commands.choices(action=[
        app_commands.Choice(name="pause",  value="pause"),
        app_commands.Choice(name="resume", value="resume"),
        app_commands.Choice(name="status", value="status"),
    ])
    async def orchestrator_command(interaction: discord.Interaction, action: app_commands.Choice[str]):
        orch = bot.orchestrator
        if action.value == "pause":
            orch.pause()
            await interaction.response.send_message(
                "⏸ Orchestrator paused — no new tasks will be scheduled."
            )
        elif action.value == "resume":
            orch.resume()
            await interaction.response.send_message("▶ Orchestrator resumed.")
        else:  # status
            running = len(orch._running_tasks)
            state = "⏸ PAUSED" if orch._paused else "▶ RUNNING"
            await interaction.response.send_message(
                f"**Orchestrator status:** {state}\n"
                f"**Running tasks:** {running}"
            )

    @bot.tree.command(name="notes", description="View and manage notes for a project")
    @app_commands.describe(project_id="Project ID")
    async def notes_command(interaction: discord.Interaction, project_id: str):
        project = await bot.orchestrator.db.get_project(project_id)
        if not project:
            await interaction.response.send_message(
                f"Project `{project_id}` not found.", ephemeral=True
            )
            return

        # Build notes listing using the existing tool implementation
        notes_result = await bot._execute_tool("list_notes", {"project_id": project_id})
        if "error" in notes_result:
            await interaction.response.send_message(
                f"Error: {notes_result['error']}", ephemeral=True
            )
            return

        notes = notes_result.get("notes", [])
        lines = [f"## Notes: {project.name}"]
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
            name=f"Notes: {project.name}"[:100],
            auto_archive_duration=1440,
        )
        await thread.send(
            f"Ask me to read, create, or edit notes for **{project.name}**."
        )
        bot.register_notes_thread(thread.id, project_id)

    @bot.tree.command(name="task", description="Show full details of a task")
    @app_commands.describe(task_id="Task ID")
    async def task_command(interaction: discord.Interaction, task_id: str):
        task = await bot.orchestrator.db.get_task(task_id)
        if not task:
            await interaction.response.send_message(f"Task `{task_id}` not found.", ephemeral=True)
            return
        lines = [
            f"## Task `{task.id}`",
            f"**Title:** {task.title}",
            f"**Status:** {task.status.value}",
            f"**Project:** `{task.project_id}`",
            f"**Priority:** {task.priority}",
        ]
        if task.assigned_agent_id:
            lines.append(f"**Agent:** {task.assigned_agent_id}")
        if task.retry_count:
            lines.append(f"**Retries:** {task.retry_count} / {task.max_retries}")
        if task.description:
            desc = task.description if len(task.description) <= 800 else task.description[:800] + "..."
            lines.append(f"\n**Description:**\n{desc}")
        await interaction.response.send_message("\n".join(lines))

    # ---------------------------------------------------------------------------
    # /set-status — manually override the status of any task
    # ---------------------------------------------------------------------------

    # Embed color and emoji per status value (used by set-status response)
    _STATUS_COLORS: dict[str, int] = {
        TaskStatus.DEFINED.value:     0x95a5a6,  # grey   — not yet actionable
        TaskStatus.READY.value:       0x3498db,  # blue   — queued for an agent
        TaskStatus.IN_PROGRESS.value: 0xf39c12,  # amber  — actively running
        TaskStatus.COMPLETED.value:   0x2ecc71,  # green  — done successfully
        TaskStatus.FAILED.value:      0xe74c3c,  # red    — execution failed
        TaskStatus.BLOCKED.value:     0x992d22,  # dark-red — max retries / manual stop
    }
    _STATUS_EMOJIS: dict[str, str] = {
        TaskStatus.DEFINED.value:     "⚪",
        TaskStatus.READY.value:       "🔵",
        TaskStatus.IN_PROGRESS.value: "🟡",
        TaskStatus.COMPLETED.value:   "🟢",
        TaskStatus.FAILED.value:      "🔴",
        TaskStatus.BLOCKED.value:     "⛔",
    }

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
    ) -> None:
        db = bot.orchestrator.db
        task = await db.get_task(task_id)
        if not task:
            await interaction.response.send_message(
                f"Task `{task_id}` not found.", ephemeral=True
            )
            return

        old_status = task.status.value
        new_status = status.value

        # Write the new status directly — this is an intentional manual override
        # that bypasses the state-machine so operators can unstick tasks.
        await db.update_task(task_id, status=TaskStatus(new_status))

        old_emoji = _STATUS_EMOJIS.get(old_status, "❓")
        new_emoji = _STATUS_EMOJIS.get(new_status, "❓")

        embed = discord.Embed(
            title="Task Status Updated",
            color=_STATUS_COLORS.get(new_status, 0x95a5a6),
        )
        embed.add_field(
            name="Task",
            value=f"`{task.id}` — {task.title}",
            inline=False,
        )
        embed.add_field(
            name="Status Change",
            value=f"{old_emoji} **{old_status}** → {new_emoji} **{new_status}**",
            inline=False,
        )
        embed.add_field(name="Project", value=f"`{task.project_id}`", inline=True)
        await interaction.response.send_message(embed=embed)

    # ---------------------------------------------------------------------------
    # /agent-error — inspect the last error output for a task
    # ---------------------------------------------------------------------------

    @bot.tree.command(
        name="agent-error",
        description="Show the last error recorded for a task",
    )
    @app_commands.describe(task_id="Task ID to inspect")
    async def agent_error_command(
        interaction: discord.Interaction, task_id: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        db = bot.orchestrator.db

        task = await db.get_task(task_id)
        if not task:
            await interaction.followup.send(
                f"Task `{task_id}` not found.", ephemeral=True
            )
            return

        result = await db.get_task_result(task_id)

        embed = discord.Embed(
            title=f"Agent Error Report: {task_id}",
            color=0xe74c3c,  # red
        )
        embed.add_field(
            name="Task",
            value=f"{task.title}",
            inline=False,
        )
        embed.add_field(
            name="Status",
            value=task.status.value,
            inline=True,
        )
        embed.add_field(
            name="Retries",
            value=f"{task.retry_count} / {task.max_retries}",
            inline=True,
        )

        if not result:
            embed.description = "_No result recorded yet for this task._"
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        result_value = result.get("result", "unknown")
        error_msg = result.get("error_message") or ""
        error_type, suggestion = classify_error(error_msg)

        embed.add_field(
            name="Result",
            value=result_value,
            inline=True,
        )
        embed.add_field(
            name="Error Type",
            value=f"**{error_type}**",
            inline=False,
        )

        if error_msg:
            # Show up to 1000 chars — Discord embed field limit is 1024
            snippet = error_msg[:1000]
            if len(error_msg) > 1000:
                snippet += "\n… _(truncated)_"
            embed.add_field(
                name="Error Detail",
                value=f"```\n{snippet}\n```",
                inline=False,
            )
        else:
            embed.add_field(
                name="Error Detail",
                value="_No error message recorded._",
                inline=False,
            )

        embed.add_field(
            name="Suggested Fix",
            value=suggestion,
            inline=False,
        )

        # Also show summary if available
        summary = result.get("summary") or ""
        if summary:
            embed.add_field(
                name="Agent Summary",
                value=summary[:500] + ("…" if len(summary) > 500 else ""),
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------------------------------------------------------------------------
    # /add-task — manually add a task to the active/current project
    # ---------------------------------------------------------------------------

    _TASK_ADJECTIVES = [
        "swift", "bright", "calm", "bold", "keen", "wise", "fair",
        "sharp", "clear", "eager", "fresh", "grand", "prime", "quick",
        "smart", "sound", "solid", "stark", "steady", "noble", "crisp",
        "fleet", "nimble", "brisk", "vivid", "agile", "amber", "azure",
    ]
    _TASK_NOUNS = [
        "falcon", "horizon", "cascade", "ember", "summit", "ridge",
        "beacon", "current", "delta", "forge", "glacier", "harbor",
        "impact", "journey", "lantern", "meadow", "nexus", "orbit",
        "pinnacle", "quest", "rapids", "stone", "torrent", "vault",
        "willow", "zenith", "apex", "bridge", "crest", "dune",
        "flare", "grove", "inlet", "jetty", "knoll", "lunar",
    ]

    def _generate_task_name() -> str:
        """Return a random adjective-noun task title (e.g. 'swift-falcon')."""
        adj = random.choice(_TASK_ADJECTIVES)
        noun = random.choice(_TASK_NOUNS)
        return f"{adj}-{noun}"

    @bot.tree.command(name="add-task", description="Add a task manually to the active project")
    @app_commands.describe(description="What the task should do")
    async def add_task_command(interaction: discord.Interaction, description: str) -> None:
        db = bot.orchestrator.db

        # Resolve the target project: use the active project, or fall back to "quick-tasks"
        project_id = bot.agent._active_project_id or "quick-tasks"
        project = await db.get_project(project_id)

        # Auto-create "quick-tasks" if it doesn't exist yet
        if not project:
            workspace = os.path.join(bot.config.workspace_dir, project_id)
            os.makedirs(workspace, exist_ok=True)
            project = Project(
                id=project_id,
                name="Quick Tasks",
                credit_weight=0.5,
                max_concurrent_agents=1,
                workspace_path=workspace,
            )
            await db.create_project(project)

        # Build task with a random name and a short unique ID
        task_id = str(uuid.uuid4())[:8]
        title = _generate_task_name()
        task = Task(
            id=task_id,
            project_id=project_id,
            title=title,
            description=description,
            status=TaskStatus.READY,
        )
        await db.create_task(task)

        embed = discord.Embed(title="✅ Task Added", color=0x2ecc71)
        embed.add_field(name="ID", value=f"`{task_id}`", inline=True)
        embed.add_field(name="Name", value=title, inline=True)
        embed.add_field(name="Project", value=f"`{project_id}`", inline=True)
        embed.add_field(name="Status", value="🔵 READY", inline=True)
        # Cap description at 1024 chars (Discord embed field limit)
        desc_preview = description if len(description) <= 1024 else description[:1021] + "..."
        embed.add_field(name="Description", value=desc_preview, inline=False)
        await interaction.response.send_message(embed=embed)
