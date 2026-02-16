from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from src.models import ProjectStatus, TaskStatus


def setup_commands(bot: commands.Bot) -> None:
    """Register all slash commands on the bot."""

    @bot.tree.command(name="status", description="Show system status overview")
    async def status_command(interaction: discord.Interaction):
        projects = await bot.orchestrator.db.list_projects()
        agents = await bot.orchestrator.db.list_agents()
        tasks = await bot.orchestrator.db.list_tasks()

        active_tasks = [t for t in tasks if t.status == TaskStatus.IN_PROGRESS]
        ready_tasks = [t for t in tasks if t.status == TaskStatus.READY]

        lines = [
            f"**Projects:** {len(projects)}",
            f"**Agents:** {len(agents)}",
            f"**Tasks:** {len(tasks)} total, {len(active_tasks)} active, {len(ready_tasks)} ready",
        ]
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
        lines = []
        for t in tasks[:20]:  # limit output
            lines.append(f"• `{t.id}` **{t.title}** — {t.status.value}")
        if len(tasks) > 20:
            lines.append(f"_...and {len(tasks) - 20} more_")
        await interaction.response.send_message("\n".join(lines))

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
