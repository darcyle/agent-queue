from __future__ import annotations

import os
import signal

import discord
from discord import app_commands
from discord.ext import commands

from src.models import Agent, AgentState, Project, ProjectStatus, RepoSourceType, Task, TaskStatus
from src.discord.notifications import classify_error
from src.task_names import generate_task_id


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
        completed_tasks = [t for t in tasks if t.status == TaskStatus.COMPLETED]
        failed_tasks = [t for t in tasks if t.status == TaskStatus.FAILED]

        lines = []
        if bot.orchestrator._paused:
            lines.append("⏸ **Orchestrator is PAUSED** — scheduling suspended")
            lines.append("")
        lines += [
            "## System Status",
            f"**Tasks:** {len(tasks)} total — "
            f"{len(active_tasks)} active, {len(ready_tasks)} ready, "
            f"{len(completed_tasks)} completed, {len(failed_tasks)} failed, "
            f"{len(paused_tasks)} paused",
            "",
        ]

        # Per-project breakdown
        if projects:
            lines.append("**Projects:**")
            # Group tasks by project
            tasks_by_project: dict[str, list] = {}
            for t in tasks:
                tasks_by_project.setdefault(t.project_id, []).append(t)

            for p in projects:
                ptasks = tasks_by_project.get(p.id, [])
                p_active = sum(1 for t in ptasks if t.status == TaskStatus.IN_PROGRESS)
                p_ready = sum(1 for t in ptasks if t.status == TaskStatus.READY)
                p_completed = sum(1 for t in ptasks if t.status == TaskStatus.COMPLETED)
                p_failed = sum(1 for t in ptasks if t.status == TaskStatus.FAILED)

                status_parts = []
                if p_active:
                    status_parts.append(f"{p_active} active")
                if p_ready:
                    status_parts.append(f"{p_ready} ready")
                if p_completed:
                    status_parts.append(f"{p_completed} completed")
                if p_failed:
                    status_parts.append(f"{p_failed} failed")

                stats = ", ".join(status_parts) if status_parts else "no tasks"
                lines.append(f"• **{p.name}** (`{p.id}`) — {len(ptasks)} tasks: {stats}")
            lines.append("")

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

        # Build task with a generated name as the ID
        task_id = await generate_task_id(db)
        title = description[:100] if len(description) > 100 else description
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
        embed.add_field(name="Project", value=f"`{project_id}`", inline=True)
        embed.add_field(name="Status", value="🔵 READY", inline=True)
        # Cap description at 1024 chars (Discord embed field limit)
        desc_preview = description if len(description) <= 1024 else description[:1021] + "..."
        embed.add_field(name="Description", value=desc_preview, inline=False)
        await interaction.response.send_message(embed=embed)

    # ---------------------------------------------------------------------------
    # /git-status — report the git status of a project's repository
    # ---------------------------------------------------------------------------

    @bot.tree.command(
        name="git-status",
        description="Show the git status of a project's repository",
    )
    @app_commands.describe(project_id="Project ID to check git status for")
    async def git_status_command(
        interaction: discord.Interaction, project_id: str
    ) -> None:
        await interaction.response.defer()
        db = bot.orchestrator.db
        git = bot.orchestrator.git

        project = await db.get_project(project_id)
        if not project:
            await interaction.followup.send(
                f"Project `{project_id}` not found.", ephemeral=True
            )
            return

        # Collect git status from all repos registered to this project,
        # and fall back to the project workspace if no repos are registered.
        repos = await db.list_repos(project_id=project_id)
        sections: list[str] = []

        if repos:
            for repo in repos:
                # Determine the working directory for this repo
                if repo.source_type == RepoSourceType.LINK and repo.source_path:
                    repo_path = repo.source_path
                elif repo.source_type == RepoSourceType.CLONE and repo.checkout_base_path:
                    # For clone repos, use the checkout base path directly
                    # (agents may have sub-checkouts, but show the base)
                    repo_path = repo.checkout_base_path
                else:
                    continue

                if not os.path.isdir(repo_path):
                    sections.append(
                        f"### Repo: `{repo.id}`\n"
                        f"⚠️ Path not found: `{repo_path}`"
                    )
                    continue

                if not git.validate_checkout(repo_path):
                    sections.append(
                        f"### Repo: `{repo.id}`\n"
                        f"⚠️ Not a valid git repository: `{repo_path}`"
                    )
                    continue

                branch = git.get_current_branch(repo_path)
                status_output = git.get_status(repo_path)
                recent_commits = git.get_recent_commits(repo_path, count=5)

                lines = [f"### Repo: `{repo.id}`"]
                lines.append(f"**Path:** `{repo_path}`")
                if branch:
                    lines.append(f"**Branch:** `{branch}`")
                lines.append(f"\n**Status:**\n```\n{status_output or '(clean)'}\n```")
                if recent_commits:
                    lines.append(f"**Recent commits:**\n```\n{recent_commits}\n```")

                sections.append("\n".join(lines))
        else:
            # No repos registered — try using the project workspace directly
            workspace = project.workspace_path
            if not workspace or not os.path.isdir(workspace):
                await interaction.followup.send(
                    f"Project `{project_id}` has no repos and no valid workspace path."
                )
                return

            if not git.validate_checkout(workspace):
                await interaction.followup.send(
                    f"Project workspace `{workspace}` is not a git repository."
                )
                return

            branch = git.get_current_branch(workspace)
            status_output = git.get_status(workspace)
            recent_commits = git.get_recent_commits(workspace, count=5)

            lines = [f"### Workspace: `{workspace}`"]
            if branch:
                lines.append(f"**Branch:** `{branch}`")
            lines.append(f"\n**Status:**\n```\n{status_output or '(clean)'}\n```")
            if recent_commits:
                lines.append(f"**Recent commits:**\n```\n{recent_commits}\n```")
            sections.append("\n".join(lines))

        header = f"## Git Status: {project.name} (`{project_id}`)\n"
        full_message = header + "\n\n".join(sections)

        # Handle Discord's 2000-char limit
        if len(full_message) <= 2000:
            await interaction.followup.send(full_message)
        elif len(full_message) <= 6000:
            # Split into multiple messages
            chunks: list[str] = []
            current = header
            for section in sections:
                candidate = current + "\n\n" + section
                if len(candidate) > 2000:
                    if current.strip():
                        chunks.append(current)
                    current = section
                else:
                    current = candidate
            if current.strip():
                chunks.append(current)
            for chunk in chunks:
                await interaction.followup.send(chunk)
        else:
            # Very long — send as file attachment
            import io
            file = discord.File(
                fp=io.BytesIO(full_message.encode("utf-8")),
                filename="git-status.md",
            )
            preview = full_message[:300].rstrip() + "\n\n*Full output attached.*"
            await interaction.followup.send(preview, file=file)

    # ---------------------------------------------------------------------------
    # /create-project — create a new project
    # ---------------------------------------------------------------------------

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
    ) -> None:
        db = bot.orchestrator.db
        project_id = name.lower().replace(" ", "-")
        workspace = os.path.join(bot.config.workspace_dir, project_id)
        os.makedirs(workspace, exist_ok=True)
        project = Project(
            id=project_id,
            name=name,
            credit_weight=credit_weight,
            max_concurrent_agents=max_concurrent_agents,
            workspace_path=workspace,
        )
        await db.create_project(project)

        embed = discord.Embed(title="✅ Project Created", color=0x2ecc71)
        embed.add_field(name="Name", value=name, inline=True)
        embed.add_field(name="ID", value=f"`{project_id}`", inline=True)
        embed.add_field(name="Weight", value=str(credit_weight), inline=True)
        embed.add_field(name="Max Agents", value=str(max_concurrent_agents), inline=True)
        embed.add_field(name="Workspace", value=f"`{workspace}`", inline=False)
        await interaction.response.send_message(embed=embed)

    # ---------------------------------------------------------------------------
    # /delete-project — delete a project and all associated data
    # ---------------------------------------------------------------------------

    @bot.tree.command(name="delete-project", description="Delete a project and all its data")
    @app_commands.describe(project_id="Project ID to delete")
    async def delete_project_command(
        interaction: discord.Interaction, project_id: str
    ) -> None:
        db = bot.orchestrator.db
        project = await db.get_project(project_id)
        if not project:
            await interaction.response.send_message(
                f"Project `{project_id}` not found.", ephemeral=True
            )
            return
        tasks = await db.list_tasks(project_id=project_id, status=TaskStatus.IN_PROGRESS)
        if tasks:
            await interaction.response.send_message(
                f"Cannot delete: {len(tasks)} task(s) currently IN_PROGRESS. Stop them first.",
                ephemeral=True,
            )
            return
        await db.delete_project(project_id)
        await interaction.response.send_message(
            f"🗑️ Project **{project.name}** (`{project_id}`) deleted."
        )

    # ---------------------------------------------------------------------------
    # /stop-task — stop a running task
    # ---------------------------------------------------------------------------

    @bot.tree.command(name="stop-task", description="Stop a task that is currently in progress")
    @app_commands.describe(task_id="Task ID to stop")
    async def stop_task_command(
        interaction: discord.Interaction, task_id: str
    ) -> None:
        error = await bot.orchestrator.stop_task(task_id)
        if error:
            await interaction.response.send_message(
                f"Error: {error}", ephemeral=True
            )
            return
        await interaction.response.send_message(f"⏹ Task `{task_id}` stopped.")

    # ---------------------------------------------------------------------------
    # /restart-task — restart a failed/completed/blocked task
    # ---------------------------------------------------------------------------

    @bot.tree.command(name="restart-task", description="Reset a task back to READY for re-execution")
    @app_commands.describe(task_id="Task ID to restart")
    async def restart_task_command(
        interaction: discord.Interaction, task_id: str
    ) -> None:
        db = bot.orchestrator.db
        task = await db.get_task(task_id)
        if not task:
            await interaction.response.send_message(
                f"Task `{task_id}` not found.", ephemeral=True
            )
            return
        if task.status == TaskStatus.IN_PROGRESS:
            await interaction.response.send_message(
                "Task is currently in progress. Stop it first.", ephemeral=True
            )
            return
        old_status = task.status.value
        await db.update_task(
            task_id,
            status=TaskStatus.READY.value,
            retry_count=0,
            assigned_agent_id=None,
        )
        await interaction.response.send_message(
            f"🔄 Task `{task_id}` restarted ({old_status} → READY)"
        )

    # ---------------------------------------------------------------------------
    # /delete-task — delete a task
    # ---------------------------------------------------------------------------

    @bot.tree.command(name="delete-task", description="Delete a task")
    @app_commands.describe(task_id="Task ID to delete")
    async def delete_task_command(
        interaction: discord.Interaction, task_id: str
    ) -> None:
        db = bot.orchestrator.db
        task = await db.get_task(task_id)
        if not task:
            await interaction.response.send_message(
                f"Task `{task_id}` not found.", ephemeral=True
            )
            return
        if task.status == TaskStatus.IN_PROGRESS:
            error = await bot.orchestrator.stop_task(task_id)
            if error:
                await interaction.response.send_message(
                    f"Could not stop task before deleting: {error}", ephemeral=True
                )
                return
        await db.delete_task(task_id)
        await interaction.response.send_message(
            f"🗑️ Task `{task_id}` ({task.title}) deleted."
        )

    # ---------------------------------------------------------------------------
    # /approve-task — approve a task awaiting approval
    # ---------------------------------------------------------------------------

    @bot.tree.command(name="approve-task", description="Approve a task that is awaiting approval")
    @app_commands.describe(task_id="Task ID to approve")
    async def approve_task_command(
        interaction: discord.Interaction, task_id: str
    ) -> None:
        db = bot.orchestrator.db
        task = await db.get_task(task_id)
        if not task:
            await interaction.response.send_message(
                f"Task `{task_id}` not found.", ephemeral=True
            )
            return
        if task.status != TaskStatus.AWAITING_APPROVAL:
            await interaction.response.send_message(
                f"Task is not awaiting approval (status: {task.status.value}).",
                ephemeral=True,
            )
            return
        await db.update_task(task_id, status=TaskStatus.COMPLETED.value)
        await db.log_event(
            "task_completed",
            project_id=task.project_id,
            task_id=task.id,
        )
        await interaction.response.send_message(
            f"✅ Task `{task_id}` ({task.title}) approved and completed."
        )

    # ---------------------------------------------------------------------------
    # /task-result — show the output/results of a task
    # ---------------------------------------------------------------------------

    @bot.tree.command(name="task-result", description="Show the results/output of a completed task")
    @app_commands.describe(task_id="Task ID to inspect")
    async def task_result_command(
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
        if not result:
            await interaction.followup.send(
                f"No results found for task `{task_id}`.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"Task Result: {task_id}",
            color=0x2ecc71 if result.get("result") == "completed" else 0xe74c3c,
        )
        embed.add_field(name="Task", value=task.title, inline=False)
        embed.add_field(name="Status", value=task.status.value, inline=True)
        embed.add_field(
            name="Result",
            value=result.get("result", "unknown"),
            inline=True,
        )

        summary = result.get("summary") or ""
        if summary:
            summary_text = summary[:1000] + ("…" if len(summary) > 1000 else "")
            embed.add_field(
                name="Summary", value=summary_text, inline=False
            )

        files = result.get("files_changed") or []
        if files:
            file_list = "\n".join(f"• `{f}`" for f in files[:20])
            if len(files) > 20:
                file_list += f"\n_...and {len(files) - 20} more_"
            embed.add_field(name="Files Changed", value=file_list, inline=False)

        tokens = result.get("tokens_used", 0)
        if tokens:
            embed.add_field(
                name="Tokens Used", value=f"{tokens:,}", inline=True
            )

        error_msg = result.get("error_message") or ""
        if error_msg:
            snippet = error_msg[:500] + ("…" if len(error_msg) > 500 else "")
            embed.add_field(
                name="Error", value=f"```\n{snippet}\n```", inline=False
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------------------------------------------------------------------------
    # /events — show recent system events
    # ---------------------------------------------------------------------------

    @bot.tree.command(name="events", description="Show recent system events")
    @app_commands.describe(limit="Number of events to show (default 10)")
    async def events_command(
        interaction: discord.Interaction, limit: int = 10
    ) -> None:
        db = bot.orchestrator.db
        events = await db.get_recent_events(limit=limit)
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

    # ---------------------------------------------------------------------------
    # /create-agent — register a new agent
    # ---------------------------------------------------------------------------

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
    ) -> None:
        db = bot.orchestrator.db
        agent_id = name.lower().replace(" ", "-")
        atype = agent_type.value if agent_type else "claude"

        if repo_id:
            repo = await db.get_repo(repo_id)
            if not repo:
                await interaction.response.send_message(
                    f"Repo `{repo_id}` not found.", ephemeral=True
                )
                return

        agent = Agent(
            id=agent_id,
            name=name,
            agent_type=atype,
            repo_id=repo_id,
        )
        await db.create_agent(agent)

        embed = discord.Embed(title="✅ Agent Registered", color=0x2ecc71)
        embed.add_field(name="Name", value=name, inline=True)
        embed.add_field(name="ID", value=f"`{agent_id}`", inline=True)
        embed.add_field(name="Type", value=atype, inline=True)
        if repo_id:
            embed.add_field(name="Repo", value=f"`{repo_id}`", inline=True)
        await interaction.response.send_message(embed=embed)

    # ---------------------------------------------------------------------------
    # /repos — list registered repositories
    # ---------------------------------------------------------------------------

    @bot.tree.command(name="repos", description="List registered repositories")
    @app_commands.describe(project_id="Filter by project ID (optional)")
    async def repos_command(
        interaction: discord.Interaction, project_id: str | None = None
    ) -> None:
        db = bot.orchestrator.db
        repos = await db.list_repos(project_id=project_id)
        if not repos:
            await interaction.response.send_message("No repositories registered.")
            return

        lines = ["## Repositories"]
        for r in repos:
            source_info = ""
            if r.source_type == RepoSourceType.LINK and r.source_path:
                source_info = f" → `{r.source_path}`"
            elif r.source_type == RepoSourceType.CLONE and r.url:
                source_info = f" → {r.url}"
            lines.append(
                f"• **{r.id}** ({r.source_type.value}){source_info} — "
                f"project: `{r.project_id}`, branch: `{r.default_branch}`"
            )

        msg = "\n".join(lines)
        if len(msg) > 2000:
            msg = msg[:1997] + "..."
        await interaction.response.send_message(msg)

    # ---------------------------------------------------------------------------
    # /hooks — list hooks
    # ---------------------------------------------------------------------------

    @bot.tree.command(name="hooks", description="List automation hooks")
    @app_commands.describe(project_id="Filter by project ID (optional)")
    async def hooks_command(
        interaction: discord.Interaction, project_id: str | None = None
    ) -> None:
        db = bot.orchestrator.db
        hooks = await db.list_hooks(project_id=project_id)
        if not hooks:
            await interaction.response.send_message("No hooks configured.")
            return

        import json as _json
        lines = ["## Hooks"]
        for h in hooks:
            status = "✅ enabled" if h.enabled else "❌ disabled"
            try:
                trigger = _json.loads(h.trigger)
                trigger_desc = trigger.get("type", "unknown")
                if trigger_desc == "periodic":
                    interval = trigger.get("interval_seconds", 0)
                    trigger_desc = f"periodic ({interval}s)"
                elif trigger_desc == "event":
                    trigger_desc = f"event: {trigger.get('event_type', '?')}"
            except Exception:
                trigger_desc = "unknown"
            lines.append(
                f"• **{h.name}** (`{h.id}`) — {status}, trigger: {trigger_desc}, "
                f"project: `{h.project_id}`"
            )

        msg = "\n".join(lines)
        if len(msg) > 2000:
            msg = msg[:1997] + "..."
        await interaction.response.send_message(msg)

    # ---------------------------------------------------------------------------
    # /set-project — set or clear the active project
    # ---------------------------------------------------------------------------

    @bot.tree.command(name="set-project", description="Set or clear the active project for the chat agent")
    @app_commands.describe(project_id="Project ID to set as active (leave empty to clear)")
    async def set_project_command(
        interaction: discord.Interaction, project_id: str | None = None
    ) -> None:
        if project_id:
            project = await bot.orchestrator.db.get_project(project_id)
            if not project:
                await interaction.response.send_message(
                    f"Project `{project_id}` not found.", ephemeral=True
                )
                return
            bot.agent.set_active_project(project_id)
            await interaction.response.send_message(
                f"📌 Active project set to **{project.name}** (`{project_id}`)"
            )
        else:
            bot.agent.set_active_project(None)
            await interaction.response.send_message("📌 Active project cleared.")
