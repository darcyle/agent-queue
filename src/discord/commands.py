"""Discord slash commands for AgentQueue.

All commands delegate their business logic to the shared ``CommandHandler``,
ensuring feature parity with the chat agent LLM tools.  This file is
intentionally a thin formatting layer: each slash command calls
``handler.execute(name, args)`` and only handles Discord-specific presentation
(ephemeral replies, embeds, file attachments, autocomplete).  No business
logic lives here -- see ``src/command_handler.py`` for that.
"""
from __future__ import annotations

import io
import re
import subprocess
import traceback

import discord
from discord import app_commands
from discord.ext import commands

from src.discord.embeds import (
    STATUS_COLORS, STATUS_EMOJIS, TYPE_TAGS,
    success_embed, error_embed, warning_embed, info_embed, status_embed,
    truncate, progress_bar, format_tree_task,
    TREE_PIPE, TREE_SPACE, LIMIT_FIELD_VALUE,
)
from src.models import TaskStatus

_NOTES_PER_PAGE = 20  # Max note buttons (4 rows × 5 buttons)


class _NoteDismissButton(discord.ui.Button):
    """Dismiss button that deletes the note-viewing message."""

    def __init__(self, project_id: str, note_slug: str, bot) -> None:
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Dismiss",
            custom_id=f"notes:{project_id}:dismiss:{note_slug}",
        )
        self._bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = self._bot
        if bot:
            thread_id = interaction.channel_id
            note_viewers = getattr(bot, "_note_viewers", {})
            if thread_id in note_viewers:
                fname = f"{self.custom_id.split(':')[3]}.md"
                note_viewers[thread_id].pop(fname, None)
        await interaction.message.delete()


class _NoteDeleteButton(discord.ui.Button):
    """Delete button that deletes the actual note file and removes the message."""

    def __init__(self, project_id: str, note_slug: str, handler, bot) -> None:
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Delete",
            custom_id=f"notes:{project_id}:delete:{note_slug}",
        )
        self._project_id = project_id
        self._slug = note_slug
        self._handler = handler
        self._bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        handler = self._handler
        if not handler:
            await interaction.response.send_message("Not available.", ephemeral=True)
            return

        title = self._slug.replace("-", " ").title()
        await interaction.response.defer(ephemeral=True)

        result = await handler.execute("delete_note", {
            "project_id": self._project_id,
            "title": title,
        })
        if "error" in result:
            await _send_error(interaction, result['error'], followup=True)
            return

        # Clean up note_viewers tracking
        bot = self._bot
        fname = f"{self._slug}.md"
        if bot:
            thread_id = interaction.channel_id
            note_viewers = getattr(bot, "_note_viewers", {})
            if thread_id in note_viewers:
                note_viewers[thread_id].pop(fname, None)

        # Delete the note-viewing message
        try:
            await interaction.message.delete()
        except Exception:
            pass

        # Send ephemeral confirmation
        await _send_success(
            interaction, "Note Deleted",
            description=f"Note **{title}** deleted.",
            followup=True, ephemeral=True,
        )

        # Auto-refresh notes TOC if available
        if bot:
            await self._refresh_toc(bot, interaction.channel_id)

    async def _refresh_toc(self, bot, thread_id: int) -> None:
        """Try to refresh the notes TOC message after deletion."""
        toc_messages = getattr(bot, "_notes_toc_messages", {})
        toc_msg_id = toc_messages.get(thread_id)
        if not toc_msg_id:
            return
        try:
            handler = self._handler
            result = await handler.execute("list_notes", {
                "project_id": self._project_id,
            })
            if "error" in result:
                return
            notes = result.get("notes", [])
            thread = bot.get_channel(thread_id)
            if not thread:
                return
            toc_msg = await thread.fetch_message(toc_msg_id)
            view = NotesView(
                self._project_id, notes,
                handler=handler, bot=bot,
            )
            await toc_msg.edit(
                content=view.build_content(), view=view,
            )
        except Exception:
            pass


class _NotePlanButton(discord.ui.Button):
    """Button that creates a task to plan/implement what's described in the note."""

    def __init__(self, project_id: str, note_slug: str, handler, bot) -> None:
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Plan Implementation",
            custom_id=f"notes:{project_id}:plan:{note_slug}",
        )
        self._project_id = project_id
        self._slug = note_slug
        self._handler = handler
        self._bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        handler = self._handler
        if not handler:
            await interaction.response.send_message("Not available.", ephemeral=True)
            return

        title = self._slug.replace("-", " ").title()
        await interaction.response.defer(ephemeral=True)

        # Read the note content
        result = await handler.execute("read_note", {
            "project_id": self._project_id,
            "title": title,
        })
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        note_content = result["content"]
        note_title = result.get("title", title)

        # Create a task to plan/implement what's described in the note
        task_title = f"Plan implementation: {note_title}"
        task_description = (
            f"Plan and implement what is described in the following note.\n\n"
            f"## Note: {note_title}\n\n{note_content}"
        )

        task_result = await handler.execute("create_task", {
            "project_id": self._project_id,
            "title": task_title,
            "description": task_description,
        })
        if "error" in task_result:
            await _send_error(interaction, task_result["error"], followup=True)
            return

        task_id = task_result["created"]
        await _send_success(
            interaction, "Task Created",
            description=(
                f"Created task **{task_id}** to plan implementation "
                f"of note **{note_title}**."
            ),
            followup=True, ephemeral=True,
        )


class NoteContentView(discord.ui.View):
    """View attached to a note content message with Dismiss, Delete, and Plan buttons."""

    def __init__(self, project_id: str, note_slug: str, handler=None, bot=None) -> None:
        super().__init__(timeout=None)
        self.add_item(_NotePlanButton(project_id, note_slug, handler, bot))
        self.add_item(_NoteDismissButton(project_id, note_slug, bot))
        self.add_item(_NoteDeleteButton(project_id, note_slug, handler, bot))


class _NoteViewButton(discord.ui.Button):
    """Button that displays a note's content when clicked."""

    def __init__(self, project_id: str, slug: str, label: str, handler, bot) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=label,
            custom_id=f"notes:{project_id}:view:{slug}",
        )
        self._project_id = project_id
        self._slug = slug
        self._handler = handler
        self._bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        handler = self._handler
        bot = self._bot
        if not handler:
            await interaction.response.send_message("Not available.", ephemeral=True)
            return
        title = self._slug.replace("-", " ").title()
        await interaction.response.defer()
        result = await handler.execute("read_note", {
            "project_id": self._project_id,
            "title": title,
        })
        if "error" in result:
            await _send_error(interaction, result['error'], followup=True)
            return
        content = result["content"]
        fname = f"{self._slug}.md"
        thread_id = interaction.channel_id

        # Delete previous view of the same note if any
        if bot:
            note_viewers = getattr(bot, "_note_viewers", {})
            if thread_id in note_viewers and fname in note_viewers[thread_id]:
                try:
                    old_msg = await interaction.channel.fetch_message(
                        note_viewers[thread_id][fname]
                    )
                    await old_msg.delete()
                except Exception:
                    pass

        # Send note content with dismiss and delete buttons
        dismiss_view = NoteContentView(self._project_id, self._slug, handler=handler, bot=bot)
        if len(content) <= 1900:
            msg = await interaction.followup.send(
                f"### 📄 {result['title']}\n```md\n{content}\n```",
                view=dismiss_view,
                wait=True,
            )
        else:
            file = discord.File(
                fp=io.BytesIO(content.encode("utf-8")),
                filename=fname,
            )
            preview = content[:300].rstrip()
            msg = await interaction.followup.send(
                f"### 📄 {result['title']}\n{preview}\n\n"
                f"*Full content attached ({len(content):,} chars)*",
                file=file,
                view=dismiss_view,
                wait=True,
            )

        # Track the viewing message
        if bot:
            note_viewers = getattr(bot, "_note_viewers", {})
            note_viewers.setdefault(thread_id, {})[fname] = msg.id


class _NotesRefreshButton(discord.ui.Button):
    """Refreshes the notes TOC by re-fetching the note list."""

    def __init__(self, project_id: str, handler, bot) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Refresh",
            custom_id=f"notes:{project_id}:refresh",
            row=4,
        )
        self._project_id = project_id
        self._handler = handler
        self._bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        handler = self._handler
        if not handler:
            await interaction.response.send_message("Not available.", ephemeral=True)
            return
        await interaction.response.defer()
        result = await handler.execute("list_notes", {
            "project_id": self._project_id,
        })
        if "error" not in result:
            parent_view: NotesView = self.view
            parent_view.notes = result.get("notes", [])
            parent_view.total_pages = max(
                1, (len(parent_view.notes) + _NOTES_PER_PAGE - 1) // _NOTES_PER_PAGE
            )
            if parent_view.page >= parent_view.total_pages:
                parent_view.page = parent_view.total_pages - 1
            parent_view._rebuild_components()
            await interaction.edit_original_response(
                content=parent_view.build_content(), view=parent_view,
            )


class _NotesCloseButton(discord.ui.Button):
    """Archives the notes thread and cleans up tracking state."""

    def __init__(self, project_id: str, bot) -> None:
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Close Thread",
            custom_id=f"notes:{project_id}:close",
            row=4,
        )
        self._project_id = project_id
        self._bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = self._bot
        if not bot:
            await interaction.response.send_message("Not available.", ephemeral=True)
            return
        await interaction.response.defer()

        # Find the thread for this project — it may not be interaction.channel
        # (the TOC message lives in the parent channel, not the thread).
        thread = None
        if isinstance(interaction.channel, discord.Thread):
            thread = interaction.channel
        else:
            # Look up the thread from bot tracking
            for tid, pid in list(bot._notes_threads.items()):
                if pid == self._project_id:
                    thread = bot.get_channel(tid)
                    if isinstance(thread, discord.Thread):
                        break
                    thread = None

        if thread and isinstance(thread, discord.Thread):
            # Clean up tracking
            note_viewers = getattr(bot, "_note_viewers", {})
            note_viewers.pop(thread.id, None)
            toc_messages = getattr(bot, "_notes_toc_messages", {})
            toc_messages.pop(thread.id, None)
            # Unregister and archive
            bot._notes_threads.pop(thread.id, None)
            bot._save_notes_threads()
            await thread.edit(archived=True)
            # Delete the TOC message (the /notes response with buttons)
            try:
                await interaction.message.delete()
            except Exception:
                pass


class _NotesPageButton(discord.ui.Button):
    """Paginates the notes TOC forward or backward."""

    def __init__(self, project_id: str, direction: str, disabled: bool) -> None:
        label = "◀ Prev" if direction == "prev" else "Next ▶"
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=label,
            custom_id=f"notes:{project_id}:page:{direction}",
            disabled=disabled,
            row=4,
        )
        self._direction = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        parent_view: NotesView = self.view
        if self._direction == "prev" and parent_view.page > 0:
            parent_view.page -= 1
        elif self._direction == "next" and parent_view.page < parent_view.total_pages - 1:
            parent_view.page += 1
        parent_view._rebuild_components()
        await interaction.response.edit_message(
            content=parent_view.build_content(), view=parent_view,
        )


class NotesView(discord.ui.View):
    """Interactive table-of-contents for project notes with per-note buttons."""

    def __init__(
        self,
        project_id: str,
        notes: list[dict],
        page: int = 0,
        handler=None,
        bot=None,
    ) -> None:
        super().__init__(timeout=None)
        self.project_id = project_id
        self.notes = notes
        self.page = page
        self._handler = handler
        self._bot = bot
        self.total_pages = max(1, (len(notes) + _NOTES_PER_PAGE - 1) // _NOTES_PER_PAGE)
        self._rebuild_components()

    def _rebuild_components(self) -> None:
        self.clear_items()
        start = self.page * _NOTES_PER_PAGE
        page_notes = self.notes[start:start + _NOTES_PER_PAGE]

        # Rows 1-4: note buttons (up to 5 per row, 4 rows = 20 max)
        for note in page_notes:
            slug = note["name"][:-3] if note["name"].endswith(".md") else note["name"]
            label = note["title"]
            if len(label) > 72:
                label = label[:69] + "..."
            self.add_item(_NoteViewButton(
                self.project_id, slug, label, self._handler, self._bot,
            ))

        # Row 5: control buttons
        if self.total_pages > 1:
            self.add_item(_NotesPageButton(
                self.project_id, "prev", disabled=(self.page == 0),
            ))
            self.add_item(_NotesPageButton(
                self.project_id, "next",
                disabled=(self.page >= self.total_pages - 1),
            ))
        self.add_item(_NotesRefreshButton(self.project_id, self._handler, self._bot))
        self.add_item(_NotesCloseButton(self.project_id, self._bot))

    def build_content(self) -> str:
        if not self.notes:
            return "## 📝 Notes\n_No notes yet._ Use the chat thread to create some!"
        lines = ["## 📝 Notes"]
        start = self.page * _NOTES_PER_PAGE
        for i, note in enumerate(self.notes[start:start + _NOTES_PER_PAGE], start=1):
            size_kb = note["size_bytes"] / 1024
            lines.append(f"**{i}.** {note['title']} (`{note['name']}`, {size_kb:.1f} KB)")
        if self.total_pages > 1:
            lines.append(f"\n_Page {self.page + 1}/{self.total_pages}_")
        return "\n".join(lines)


def _split_display_text(text: str, limit: int) -> list[str]:
    """Split *text* into chunks of at most *limit* characters.

    Splits on line boundaries so individual task lines are never broken
    mid-line.  If the text starts or ends with a code-block fence (````` ```
    `````) each chunk is wrapped in its own fence pair so Discord renders
    them correctly.

    Returns a list with at least one element.
    """
    is_code_block = text.lstrip().startswith("```")
    # Determine the fence marker (e.g. "```\n" / "\n```")
    fence_open = ""
    fence_close = ""
    inner = text
    if is_code_block:
        # Extract the opening fence (e.g. "```\n" or "```py\n")
        first_nl = text.index("\n")
        fence_open = text[: first_nl + 1]
        # The closing fence is always "```" possibly preceded by a newline
        fence_close = "\n```"
        # Strip outer fences so we can split the inner content
        inner = text[first_nl + 1 :]
        if inner.rstrip().endswith("```"):
            inner = inner.rstrip()[: -3].rstrip("\n")

    # Budget per chunk: subtract fence overhead
    overhead = len(fence_open) + len(fence_close)
    chunk_limit = limit - overhead

    lines = inner.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in lines:
        # +1 for the newline character
        line_cost = len(line) + 1
        if current and current_len + line_cost > chunk_limit:
            body = "\n".join(current)
            chunks.append(f"{fence_open}{body}{fence_close}" if is_code_block else body)
            current = []
            current_len = 0
        current.append(line)
        current_len += line_cost

    if current:
        body = "\n".join(current)
        chunks.append(f"{fence_open}{body}{fence_close}" if is_code_block else body)

    return chunks or [text]


# ---------------------------------------------------------------------------
# Interactive menu view — persistent control panel
# ---------------------------------------------------------------------------


class _AddTaskMenuModal(discord.ui.Modal, title="Add New Task"):
    """Modal dialog for adding a task via the interactive menu."""

    task_description = discord.ui.TextInput(
        label="Task description",
        style=discord.TextStyle.long,
        placeholder="Describe what the task should do…",
        required=True,
        max_length=2000,
    )

    def __init__(self, project_id: str, handler) -> None:
        super().__init__()
        self._project_id = project_id
        self._handler = handler

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        description = self.task_description.value
        title = description[:100]
        result = await self._handler.execute("create_task", {
            "project_id": self._project_id,
            "title": title,
            "description": description,
        })
        if "error" in result:
            await interaction.followup.send(
                f"Could not create task: {result['error']}", ephemeral=True
            )
        else:
            task_id = result.get("created") or result.get("task_id", "?")
            await interaction.followup.send(
                f"✅ Task `{task_id}` created in project `{self._project_id}`.",
                ephemeral=True,
            )


class MenuView(discord.ui.View):
    """Persistent interactive control panel for agent-queue.

    Provides clickable buttons for common actions — useful for mobile usage
    and remote control without typing slash commands.  The view never times
    out so the message stays interactive as long as the bot is running.
    """

    def __init__(self, handler, bot) -> None:
        super().__init__(timeout=None)
        self._handler = handler
        self._bot = bot

    # --- Row 1: Status & Task Info ---

    @discord.ui.button(
        label="Status",
        style=discord.ButtonStyle.primary,
        emoji="📊",
        row=0,
    )
    async def status_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute("get_status", {})
        tasks = result.get("tasks", {})
        by_status = tasks.get("by_status", {})
        total = tasks.get("total", 0)
        completed = by_status.get("COMPLETED", 0)
        in_progress = by_status.get("IN_PROGRESS", 0)
        failed = by_status.get("FAILED", 0)
        assigned = by_status.get("ASSIGNED", 0)
        active = in_progress + assigned
        ready = by_status.get("READY", 0)
        pending = by_status.get("DEFINED", 0)
        blocked = by_status.get("BLOCKED", 0)

        lines = []
        if result.get("orchestrator_paused"):
            lines.append("⏸ **Orchestrator is PAUSED**")
        lines.append("## System Status")

        if total > 0:
            bar = progress_bar(completed, total, width=12)
            lines.append(f"**Progress:** {bar}")

        parts = []
        if active:
            parts.append(f"{active} active")
        if ready:
            parts.append(f"{ready} ready")
        if pending:
            parts.append(f"{pending} pending")
        if completed:
            parts.append(f"{completed} completed")
        if failed:
            parts.append(f"{failed} failed")
        if blocked:
            parts.append(f"{blocked} blocked")
        lines.append(f"**Tasks:** {total} total — " + ", ".join(parts))

        agents = result.get("agents", [])
        if agents:
            lines.append("\n**Agents:**")
            for a in agents:
                working_on = a.get("working_on")
                if working_on:
                    lines.append(
                        f"• **{a['name']}** ({a['state']}) → "
                        f"`{working_on['task_id']}`"
                    )
                else:
                    lines.append(f"• **{a['name']}** ({a['state']})")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @discord.ui.button(
        label="Active Tasks",
        style=discord.ButtonStyle.primary,
        emoji="📋",
        row=0,
    )
    async def active_tasks_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute(
            "list_active_tasks_all_projects",
            {"include_completed": False},
        )
        by_project = result.get("by_project", {})
        total = result.get("total", 0)

        if total == 0:
            await interaction.followup.send(
                "No active tasks across any project.", ephemeral=True
            )
            return

        lines = [f"**{total} active tasks:**"]
        for project_id in sorted(by_project.keys()):
            project_tasks = by_project[project_id]
            lines.append(f"\n`{project_id}` ({len(project_tasks)}):")
            for t in project_tasks[:8]:
                emoji = STATUS_EMOJIS.get(t["status"], "⚪")
                title = t["title"][:55] + "…" if len(t["title"]) > 55 else t["title"]
                agent = f" ← `{t['assigned_agent']}`" if t.get("assigned_agent") else ""
                lines.append(f"{emoji} **{title}** `{t['id']}`{agent}")
            if len(project_tasks) > 8:
                lines.append(f"_...and {len(project_tasks) - 8} more_")

        msg = "\n".join(lines)
        if len(msg) > 2000:
            msg = msg[:1997] + "…"
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(
        label="Projects",
        style=discord.ButtonStyle.secondary,
        emoji="📁",
        row=0,
    )
    async def projects_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute("list_projects", {})
        projects = result.get("projects", [])
        if not projects:
            await interaction.followup.send("No projects configured.", ephemeral=True)
            return
        lines = ["**Projects:**"]
        for p in projects:
            status = p.get("status", "?")
            lines.append(f"• **{p['name']}** (`{p['id']}`) — {status}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @discord.ui.button(
        label="Agents",
        style=discord.ButtonStyle.secondary,
        emoji="🤖",
        row=0,
    )
    async def agents_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute("list_agents", {})
        agents = result.get("agents", [])
        if not agents:
            await interaction.followup.send("No agents configured.", ephemeral=True)
            return
        lines = ["**Agents:**"]
        for a in agents:
            task_info = f" → `{a['current_task']}`" if a.get("current_task") else ""
            lines.append(f"• **{a['name']}** (`{a['id']}`) — {a['state']}{task_info}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # --- Row 2: Actions ---

    @discord.ui.button(
        label="Notes",
        style=discord.ButtonStyle.secondary,
        emoji="📝",
        row=1,
    )
    async def notes_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        project_id = self._bot.get_project_for_channel(interaction.channel_id)
        if not project_id:
            project_id = getattr(self._handler, "_active_project_id", None)
        if not project_id:
            await interaction.response.send_message(
                "No project context — use this from a project channel.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute("list_notes", {"project_id": project_id})
        if "error" in result:
            await interaction.followup.send(
                f"Could not list notes: {result['error']}", ephemeral=True
            )
            return
        notes = result.get("notes", [])
        if not notes:
            await interaction.followup.send(
                f"No notes in project `{project_id}`.", ephemeral=True
            )
            return
        lines = [f"**Notes for `{project_id}`:**"]
        for n in notes[:20]:
            lines.append(f"• {n.get('title', n.get('filename', '?'))}")
        if len(notes) > 20:
            lines.append(f"_...and {len(notes) - 20} more_")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @discord.ui.button(
        label="Add Task",
        style=discord.ButtonStyle.success,
        emoji="➕",
        row=1,
    )
    async def add_task_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        project_id = (
            self._bot.get_project_for_channel(interaction.channel_id)
            or getattr(self._handler, "_active_project_id", None)
        )
        if not project_id:
            await interaction.response.send_message(
                "No project context — use this from a project channel.",
                ephemeral=True,
            )
            return
        modal = _AddTaskMenuModal(project_id, self._handler)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Restart Task",
        style=discord.ButtonStyle.secondary,
        emoji="🔄",
        row=1,
    )
    async def restart_task_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Show failed/blocked tasks and let user pick one to restart."""
        await interaction.response.defer(ephemeral=True)
        result = await self._handler.execute(
            "list_active_tasks_all_projects",
            {"include_completed": False},
        )
        by_project = result.get("by_project", {})
        restartable = []
        for tasks in by_project.values():
            for t in tasks:
                if t["status"] in ("FAILED", "BLOCKED", "PAUSED"):
                    restartable.append(t)

        if not restartable:
            await interaction.followup.send(
                "No failed/blocked/paused tasks to restart.", ephemeral=True
            )
            return

        lines = ["**Restartable tasks** (use `/restart-task <id>`):"]
        for t in restartable[:15]:
            emoji = STATUS_EMOJIS.get(t["status"], "⚪")
            lines.append(f"{emoji} `{t['id']}` — {t['title'][:60]}")
        if len(restartable) > 15:
            lines.append(f"_...and {len(restartable) - 15} more_")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @discord.ui.button(
        label="Toggle Orchestrator",
        style=discord.ButtonStyle.secondary,
        emoji="⏯️",
        row=1,
    )
    async def toggle_orchestrator_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        # Check current state
        status_result = await self._handler.execute(
            "orchestrator_control", {"action": "status"}
        )
        is_paused = status_result.get("status") == "paused"
        # Toggle
        action = "resume" if is_paused else "pause"
        await self._handler.execute("orchestrator_control", {"action": action})
        if action == "pause":
            await interaction.followup.send(
                "⏸ Orchestrator **paused** — no new tasks will be scheduled.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "▶ Orchestrator **resumed** — task scheduling is active.",
                ephemeral=True,
            )


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

        # Build type tag prefix
        tags = []
        if result.get("is_plan_subtask"):
            tags.append(TYPE_TAGS["plan_subtask"])
        if result.get("subtasks"):
            tags.append(TYPE_TAGS["has_subtasks"])
        if result.get("pr_url"):
            tags.append(TYPE_TAGS["has_pr"])
        if result.get("requires_approval"):
            tags.append(TYPE_TAGS["approval_required"])
        tag_str = " ".join(tags) + " " if tags else ""

        status = result['status']
        emoji = STATUS_EMOJIS.get(status, "⚪")

        lines = [
            f"## {tag_str}Task `{result['id']}`",
            f"**Title:** {result['title']}",
            f"**Status:** {emoji} {status}",
            f"**Project:** `{result['project_id']}`",
            f"**Priority:** {result['priority']}",
        ]
        if result.get("assigned_agent"):
            lines.append(f"**Agent:** {result['assigned_agent']}")
        if result.get("retry_count"):
            lines.append(f"**Retries:** {result['retry_count']} / {result['max_retries']}")
        if result.get("parent_task_id"):
            lines.append(f"**Parent Task:** `{result['parent_task_id']}`")
        if result.get("pr_url"):
            lines.append(f"**PR:** {result['pr_url']}")

        # Dependency visualization
        depends_on = result.get("depends_on", [])
        if depends_on:
            lines.append("\n**Depends On:**")
            for dep in depends_on:
                dep_emoji = STATUS_EMOJIS.get(dep["status"], "⚪")
                lines.append(f"  {dep_emoji} `{dep['id']}` — {dep['title']} ({dep['status']})")

        blocks = result.get("blocks", [])
        if blocks:
            lines.append("\n**Blocks:**")
            for blk in blocks:
                blk_emoji = STATUS_EMOJIS.get(blk["status"], "⚪")
                lines.append(f"  {blk_emoji} `{blk['id']}` — {blk['title']} ({blk['status']})")

        # Subtask tree view
        subtasks = result.get("subtasks", [])
        if subtasks:
            completed = sum(1 for st in subtasks if st["status"] == "COMPLETED")
            bar = progress_bar(completed, len(subtasks), width=8)
            lines.append(f"\n**Subtasks:** {bar}")
            for i, st in enumerate(subtasks):
                is_last = (i == len(subtasks) - 1)
                st_emoji = STATUS_EMOJIS.get(st["status"], "⚪")
                connector = "└── " if is_last else "├── "
                lines.append(f"  {connector}{st_emoji} `{st['id']}` — {st['title']}")

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
        """Grouped task report with collapsible status sections and detail select.

        Displays tasks grouped by status with tree-view for parent/subtask
        relationships and type tags for quick identification.
        """

        def __init__(
            self,
            tasks_by_status: dict[str, list],
            total: int,
            *,
            all_tasks: list | None = None,
        ) -> None:
            super().__init__(timeout=600)
            self.tasks_by_status = tasks_by_status
            self.total = total
            # Build parent→subtask lookup for tree view
            self._all_tasks = all_tasks or []
            self._subtask_ids: set[str] = set()
            self._subtask_map: dict[str, list] = {}  # parent_id → [child tasks]
            for t in self._all_tasks:
                pid = t.get("parent_task_id")
                if pid:
                    self._subtask_ids.add(t["id"])
                    self._subtask_map.setdefault(pid, []).append(t)
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

        def _get_type_tag(self, task: dict) -> str:
            """Return a type tag emoji based on task properties."""
            tags = []
            if task.get("is_plan_subtask"):
                tags.append(TYPE_TAGS["plan_subtask"])
            if task["id"] in self._subtask_map:
                tags.append(TYPE_TAGS["has_subtasks"])
            if task.get("pr_url"):
                tags.append(TYPE_TAGS["has_pr"])
            return "".join(tags)

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
                    tag = self._get_type_tag(t)
                    if tag:
                        title = f"{tag} {title}"
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

        def _format_task_line(self, t: dict, *, show_children: bool = True) -> list[str]:
            """Format a task with optional tree-view subtasks."""
            tag = self._get_type_tag(t)
            tag_str = f"{tag} " if tag else ""
            lines = [f"{tag_str}**{t['title']}** `{t['id']}`"]

            # Show inline subtask count if parent has children
            children = self._subtask_map.get(t["id"], [])
            if children and show_children:
                completed = sum(
                    1 for c in children if c.get("status") == "COMPLETED"
                )
                total = len(children)
                if total <= 4:
                    # Show individual subtasks in tree view
                    for i, child in enumerate(children):
                        is_last = (i == total - 1)
                        child_emoji = _STATUS_EMOJIS.get(child.get("status", ""), "⚪")
                        connector = "└── " if is_last else "├── "
                        child_tag = TYPE_TAGS["plan_subtask"] + " " if child.get("is_plan_subtask") else ""
                        lines.append(
                            f"  {connector}{child_emoji} {child_tag}{child['title']} `{child['id']}`"
                        )
                else:
                    # Compact subtask summary
                    bar = progress_bar(completed, total, width=6)
                    lines.append(f"  └── {total} subtasks: {bar}")
            return lines

        def build_content(self) -> str:
            lines: list[str] = []
            # Add progress summary at top — use _all_tasks for accurate totals
            # since tasks_by_status may be filtered (e.g. completed hidden).
            if self._all_tasks:
                all_count = len(self._all_tasks)
                completed_count = sum(
                    1 for t in self._all_tasks if t.get("status") == "COMPLETED"
                )
            else:
                all_count = sum(len(v) for v in self.tasks_by_status.values())
                completed_count = len(self.tasks_by_status.get("COMPLETED", []))
            active_statuses = {"IN_PROGRESS", "ASSIGNED", "VERIFYING"}
            active_count = sum(
                len(self.tasks_by_status.get(s, []))
                for s in active_statuses
            )
            if all_count > 0:
                bar = progress_bar(completed_count, all_count, width=10)
                lines.append(f"**Progress:** {bar}")
                if active_count:
                    lines.append(f"**Active:** {active_count} task(s) running")
                lines.append("")

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
                        # Skip subtasks that are shown under their parent
                        if t["id"] in self._subtask_ids:
                            # Only skip if parent is in the same expanded section
                            parent_id = t.get("parent_task_id")
                            parent_in_section = any(
                                pt["id"] == parent_id
                                for pt in self.tasks_by_status.get(status, [])
                            )
                            if parent_in_section:
                                continue
                        task_lines = self._format_task_line(t)
                        lines.extend(task_lines)
                    if count > _MAX_TASKS_PER_SECTION:
                        lines.append(
                            f"_...and {count - _MAX_TASKS_PER_SECTION} more_"
                        )
                    lines.append("")
                else:
                    lines.append(f"{emoji} **{display}** ({count})")
            content = "\n".join(lines)
            # Trim if over Discord's 2000-char message limit.
            # Progressively reduce the per-section cap until content fits.
            if len(content) > 1950:
                for cap in (8, 4, 2):
                    lines = []
                    for status in _STATUS_ORDER:
                        if status not in self.tasks_by_status:
                            continue
                        tasks = self.tasks_by_status[status]
                        emoji = _STATUS_EMOJIS.get(status, "⚪")
                        display = _STATUS_DISPLAY.get(status, status)
                        count = len(tasks)
                        if status in self.expanded:
                            lines.append(f"### {emoji} {display} ({count})")
                            for t in tasks[:cap]:
                                tag = self._get_type_tag(t)
                                tag_str = f"{tag} " if tag else ""
                                lines.append(f"{tag_str}**{t['title']}** `{t['id']}`")
                            if count > cap:
                                lines.append(f"_...and {count - cap} more_")
                            lines.append("")
                        else:
                            lines.append(f"{emoji} **{display}** ({count})")
                    content = "\n".join(lines)
                    if len(content) <= 1950:
                        break
                # Final safety net: hard-truncate if still over limit
                if len(content) > 1950:
                    content = content[:1947] + "..."
            return content

    # ---------------------------------------------------------------------------
    # Shared formatting helpers
    # ---------------------------------------------------------------------------

    # Status visual mappings -- canonical definitions live in
    # src/discord/embeds.py; local aliases for backward compatibility
    # with the many references inside this function's nested closures.
    _STATUS_COLORS = STATUS_COLORS
    _STATUS_EMOJIS = STATUS_EMOJIS

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

    async def _send_error(
        interaction,
        message: str,
        *,
        followup: bool = False,
        ephemeral: bool = True,
    ):
        """Send an error response as a rich embed.

        Replaces the plain-text ``f"Error: {msg}"`` pattern with a consistent
        red embed, improving visual clarity in the Discord channel.
        """
        embed = error_embed("Error", description=message)
        if followup:
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

    async def _send_success(
        interaction,
        title: str,
        *,
        description: str | None = None,
        fields: list[tuple[str, str, bool]] | None = None,
        followup: bool = False,
        ephemeral: bool = False,
        result: dict | None = None,
    ):
        """Send a success response as a rich green embed.

        If *result* contains a ``"warning"`` key the warning text is appended
        to the embed *description* so it remains visible.
        """
        if result and result.get("warning"):
            warning_text = f"⚠️ {result['warning']}"
            description = f"{description}\n\n{warning_text}" if description else warning_text
        embed = success_embed(title, description=description, fields=fields)
        if followup:
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

    async def _send_info(
        interaction,
        title: str,
        *,
        description: str | None = None,
        fields: list[tuple[str, str, bool]] | None = None,
        followup: bool = False,
        ephemeral: bool = False,
    ):
        """Send an informational response as a rich blue embed."""
        embed = info_embed(title, description=description, fields=fields)
        if followup:
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

    async def _send_warning(
        interaction,
        title: str,
        *,
        description: str | None = None,
        fields: list[tuple[str, str, bool]] | None = None,
        followup: bool = False,
        ephemeral: bool = False,
    ):
        """Send a warning response as a rich amber embed."""
        embed = warning_embed(title, description=description, fields=fields)
        if followup:
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

    def _with_warning(msg: str, result: dict) -> str:
        """Append an in-progress task warning to *msg* if present in *result*.

        .. deprecated::
            Prefer passing ``result=result`` to ``_send_success()`` instead.
        """
        warning = result.get("warning")
        if warning:
            return f"{msg}\n\n⚠️ {warning}"
        return msg

    # ===================================================================
    # SYSTEM / STATUS COMMANDS
    # ===================================================================

    @bot.tree.command(name="status", description="Show system status overview")
    async def status_command(interaction: discord.Interaction):
        result = await handler.execute("get_status", {})

        tasks = result["tasks"]
        by_status = tasks.get("by_status", {})
        total = tasks.get("total", 0)
        completed = by_status.get("COMPLETED", 0)
        in_progress = by_status.get("IN_PROGRESS", 0)
        failed = by_status.get("FAILED", 0)

        lines = []
        if result.get("orchestrator_paused"):
            lines.append("⏸ **Orchestrator is PAUSED** — scheduling suspended")
            lines.append("")
        lines.append("## System Status")

        # Progress bar for overall completion
        if total > 0:
            bar = progress_bar(completed, total, width=12)
            lines.append(f"**Progress:** {bar}")
        # Build task breakdown — include all statuses that have nonzero counts
        # so the numbers always add up to the total.
        pending = by_status.get('DEFINED', 0)
        ready = by_status.get('READY', 0)
        assigned = by_status.get('ASSIGNED', 0)
        active = in_progress + assigned
        waiting = by_status.get('WAITING_INPUT', 0)
        paused = by_status.get('PAUSED', 0)
        verifying = by_status.get('VERIFYING', 0)
        awaiting = by_status.get('AWAITING_APPROVAL', 0)
        blocked = by_status.get('BLOCKED', 0)

        parts = []
        if pending:
            parts.append(f"{pending} pending")
        if active:
            parts.append(f"{active} active")
        if ready:
            parts.append(f"{ready} ready")
        if waiting:
            parts.append(f"{waiting} waiting input")
        if paused:
            parts.append(f"{paused} paused")
        if verifying:
            parts.append(f"{verifying} verifying")
        if awaiting:
            parts.append(f"{awaiting} awaiting approval")
        if completed:
            parts.append(f"{completed} completed")
        if failed:
            parts.append(f"{failed} failed")
        if blocked:
            parts.append(f"{blocked} blocked")

        lines.append(f"**Tasks:** {total} total — " + ", ".join(parts))
        lines.append("")

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
            await _send_info(interaction, "No Projects", description="No projects configured.")
            return
        lines = []
        for p in projects:
            line = f"• **{p['name']}** (`{p['id']}`) — {p['status']}, weight={p['credit_weight']}"
            if p.get("discord_channel_id"):
                line += f" | <#{p['discord_channel_id']}>"
            lines.append(line)
        await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(name="agents", description="List all agents")
    async def agents_command(interaction: discord.Interaction):
        result = await handler.execute("list_agents", {})
        agents = result.get("agents", [])
        if not agents:
            await _send_info(interaction, "No Agents", description="No agents configured.")
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
            await _send_info(interaction, "No Usage", description="No token usage recorded.")
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
            await _send_info(interaction, "No Events", description="No recent events.")
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

    async def _auto_create_project_channels(
        guild: discord.Guild,
        cmd_handler,
        project_id: str,
        ppc,
    ) -> list[dict]:
        """Auto-create a per-project Discord channel based on config.

        Idempotent: reuses existing channels with matching names.

        Returns a list of result dicts with keys:
            channel_name, display (for embed formatting).
        """
        results: list[dict] = []
        guild_channels = [{"id": ch.id, "name": ch.name} for ch in guild.text_channels]

        # Resolve category (create if needed)
        category: discord.CategoryChannel | None = None
        if ppc.category_name:
            for cat in guild.categories:
                if cat.name.lower() == ppc.category_name.lower():
                    category = cat
                    break
            if category is None:
                try:
                    cat_overwrites = {}
                    if ppc.private:
                        cat_overwrites[guild.default_role] = discord.PermissionOverwrite(
                            view_channel=False
                        )
                        cat_overwrites[guild.me] = discord.PermissionOverwrite(
                            view_channel=True, send_messages=True,
                            create_public_threads=True,
                            send_messages_in_threads=True,
                        )
                    category = await guild.create_category(
                        ppc.category_name,
                        overwrites=cat_overwrites or discord.utils.MISSING,
                        reason="AgentQueue: auto-created category for project channels",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass  # Fall through — channels will be created without a category

        channel_name = ppc.naming_convention.format(project_id=project_id)

        # Check if channel already exists
        existing_id = None
        for ch in guild_channels:
            if ch["name"] == channel_name:
                existing_id = str(ch["id"])
                break

        created_channel_id = None
        if not existing_id:
            # Create the channel in Discord
            try:
                topic = f"AgentQueue channel for project: {project_id}"
                overwrites = {}
                if ppc.private:
                    overwrites[guild.default_role] = discord.PermissionOverwrite(
                        view_channel=False
                    )
                    overwrites[guild.me] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True,
                        create_public_threads=True,
                        send_messages_in_threads=True,
                    )
                new_ch = await guild.create_text_channel(
                    name=channel_name,
                    category=category,
                    topic=topic,
                    reason=f"AgentQueue: auto-created channel for {project_id}",
                    overwrites=overwrites or discord.utils.MISSING,
                )
                created_channel_id = str(new_ch.id)
                guild_channels.append({"id": new_ch.id, "name": new_ch.name})
            except (discord.Forbidden, discord.HTTPException):
                results.append({
                    "channel_name": channel_name,
                    "display": f"⚠️ Failed to create #{channel_name}",
                })
                return results

        # Delegate to command handler for project linking
        channel_id = created_channel_id or existing_id
        link_result = await cmd_handler.execute("set_project_channel", {
            "project_id": project_id,
            "channel_id": channel_id,
        })

        if "error" in link_result:
            results.append({
                "channel_name": channel_name,
                "display": f"⚠️ #{channel_name} ({link_result['error']})",
            })
        else:
            ch_id = link_result.get("channel_id", "")
            mention = f"<#{ch_id}>" if ch_id else f"#{channel_name}"
            label = "created" if created_channel_id else "linked"
            results.append({
                "channel_name": channel_name,
                "display": f"{mention} ({label})",
            })

        # Refresh the bot's channel cache after creating new channels
        if hasattr(bot, '_resolve_project_channels'):
            try:
                await bot._resolve_project_channels()
            except Exception:
                pass

        return results

    @bot.tree.command(name="create-project", description="Create a new project")
    @app_commands.describe(
        name="Project name",
        credit_weight="Scheduling weight (default 1.0)",
        max_concurrent_agents="Max agents working simultaneously (default 2)",
        repo_url="Git repository URL (optional)",
        default_branch="Default branch name (default: main)",
        auto_create_channels="Auto-create Discord channels for this project (overrides config)",
    )
    async def create_project_command(
        interaction: discord.Interaction,
        name: str,
        credit_weight: float = 1.0,
        max_concurrent_agents: int = 2,
        repo_url: str | None = None,
        default_branch: str = "main",
        auto_create_channels: bool | None = None,
    ):
        # Build args for the command handler.  An explicit
        # ``auto_create_channels`` parameter overrides the global config.
        create_args: dict = {
            "name": name,
            "credit_weight": credit_weight,
            "max_concurrent_agents": max_concurrent_agents,
        }
        if repo_url:
            create_args["repo_url"] = repo_url
        if default_branch != "main":
            create_args["default_branch"] = default_branch
        if auto_create_channels is not None:
            create_args["auto_create_channels"] = auto_create_channels

        # Determine up-front whether auto-channel creation will happen so
        # we can defer the response (Discord requires early deferral for
        # operations that may take longer than 3 seconds).
        ppc = bot.config.discord.per_project_channels
        if auto_create_channels is not None:
            will_auto_create = auto_create_channels and interaction.guild is not None
        else:
            will_auto_create = ppc.auto_create and interaction.guild is not None
        if will_auto_create:
            await interaction.response.defer()

        result = await handler.execute("create_project", create_args)
        if "error" in result:
            if will_auto_create:
                await _send_error(interaction, result['error'], followup=True)
            else:
                await _send_error(interaction, result['error'])
            return

        project_id = result["created"]
        embed = success_embed(
            "Project Created",
            fields=[
                ("Name", name, True),
                ("ID", f"`{project_id}`", True),
                ("Weight", str(credit_weight), True),
                ("Max Agents", str(max_concurrent_agents), True),
                ("Workspace", f"`{result.get('workspace', '')}`", False),
            ],
        )

        # Auto-create per-project channels when the handler says so.
        # The handler resolves the explicit param vs config flag for us.
        if result.get("auto_create_channels") and interaction.guild is not None:
            channel_results = await _auto_create_project_channels(
                interaction.guild, handler, project_id, ppc,
            )
            for cr in channel_results:
                embed.add_field(
                    name="📢 Channel",
                    value=cr["display"],
                    inline=True,
                )

        if will_auto_create:
            await interaction.followup.send(embed=embed)
        else:
            await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="edit-project", description="Edit a project's settings")
    @app_commands.describe(
        project_id="Project ID",
        name="New name (optional)",
        credit_weight="New scheduling weight (optional)",
        max_concurrent_agents="New max agents (optional)",
        budget_limit="Token budget limit (optional, 0 to clear)",
        channel="Discord channel to link to this project (optional)",
    )
    async def edit_project_command(
        interaction: discord.Interaction,
        project_id: str,
        name: str | None = None,
        credit_weight: float | None = None,
        max_concurrent_agents: int | None = None,
        budget_limit: int | None = None,
        channel: discord.TextChannel | None = None,
    ):
        args: dict = {"project_id": project_id}
        if name is not None:
            args["name"] = name
        if credit_weight is not None:
            args["credit_weight"] = credit_weight
        if max_concurrent_agents is not None:
            args["max_concurrent_agents"] = max_concurrent_agents
        if budget_limit is not None:
            args["budget_limit"] = budget_limit if budget_limit > 0 else None
        if channel is not None:
            args["discord_channel_id"] = str(channel.id)
        result = await handler.execute("edit_project", args)
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        fields = ", ".join(result.get("fields", []))
        desc = f"Project `{project_id}` updated: {fields}"
        if channel is not None:
            desc += f"\nChannel: {channel.mention}"
            bot.update_project_channel(project_id, channel)
        await _send_success(
            interaction, "Project Updated",
            description=desc,
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
            await _send_error(interaction, result['error'])
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

        desc = f"Project **{result.get('name', project_id)}** (`{project_id}`) deleted."
        if archived:
            desc += "\n📦 Archived channels: " + ", ".join(archived)
        elif do_archive:
            desc += "\n*(No linked channels to archive.)*"
        await _send_success(interaction, "Project Deleted", description=desc)

    # -------------------------------------------------------------------
    # CHANNEL MANAGEMENT COMMANDS
    # -------------------------------------------------------------------
    # Note: /set-channel and /set-control-interface have been removed.
    # Use /edit-project with the channel parameter instead.

    @bot.tree.command(
        name="create-channel",
        description="Create a new Discord channel for a project",
    )
    @app_commands.describe(
        project_id="Project ID",
        channel_name="Name for the new channel (defaults to project ID)",
        category="Category to create the channel in (optional)",
    )
    async def create_channel_command(
        interaction: discord.Interaction,
        project_id: str,
        channel_name: str | None = None,
        category: discord.CategoryChannel | None = None,
    ):
        await interaction.response.defer()

        # Validate project exists (direct lookup)
        project_check = await handler.execute("get_project_channels", {"project_id": project_id})
        if "error" in project_check:
            await _send_error(interaction, f"Project `{project_id}` not found.", followup=True)
            return

        name = channel_name or project_id
        # Create the Discord channel
        guild = interaction.guild
        if not guild:
            await _send_error(interaction, "Not in a guild.", followup=True)
            return

        topic = f"Agent Queue channel for project: {project_id}"

        try:
            new_channel = await guild.create_text_channel(
                name=name,
                category=category,
                topic=topic,
                reason=f"AgentQueue: channel for project {project_id}",
            )
        except discord.Forbidden:
            await _send_error(interaction, "Bot lacks permission to create channels.", followup=True)
            return
        except discord.HTTPException as e:
            await _send_error(interaction, f"Error creating channel: {e}", followup=True)
            return

        # Link it to the project in the database
        result = await handler.execute("set_project_channel", {
            "project_id": project_id,
            "channel_id": str(new_channel.id),
        })
        if "error" in result:
            await _send_error(interaction, f"Channel created but linking failed: {result['error']}", followup=True)
            return

        # Update the bot's channel cache immediately
        bot.update_project_channel(project_id, new_channel)
        await _send_success(
            interaction, "Channel Created",
            description=f"Created {new_channel.mention} as channel for project `{project_id}`",
            followup=True,
        )

    @bot.tree.command(
        name="channel-map",
        description="Show all project-to-channel mappings",
    )
    async def channel_map_command(interaction: discord.Interaction):
        result = await handler.execute("list_projects", {})
        projects = result.get("projects", [])
        if not projects:
            await _send_info(interaction, "No Projects", description="No projects configured.")
            return

        # Build a channel-centric view: collect all channel assignments
        lines = ["**Channel Map**\n"]
        assigned = []
        unassigned = []

        for p in projects:
            channel_id = p.get("discord_channel_id")
            if channel_id:
                assigned.append(
                    f"**{p['name']}** (`{p['id']}`) → <#{channel_id}>"
                )
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
    async def pause_command(interaction: discord.Interaction):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        result = await handler.execute("pause_project", {"project_id": project_id})
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        await _send_info(
            interaction, "Project Paused",
            description=f"Project **{result.get('name', project_id)}** is now paused.",
        )

    @bot.tree.command(name="resume", description="Resume a paused project")
    async def resume_command(interaction: discord.Interaction):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        result = await handler.execute("resume_project", {"project_id": project_id})
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        await _send_success(
            interaction, "Project Resumed",
            description=f"Project **{result.get('name', project_id)}** is now active.",
        )

    @bot.tree.command(name="set-project", description="Set or clear the active project for the chat agent")
    @app_commands.describe(project_id="Project ID to set as active (leave empty to clear)")
    async def set_project_command(interaction: discord.Interaction, project_id: str | None = None):
        args = {"project_id": project_id} if project_id else {}
        result = await handler.execute("set_active_project", args)
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        if result.get("active_project"):
            await _send_success(
                interaction, "Active Project Set",
                description=f"Active project set to **{result.get('name', '')}** (`{result['active_project']}`)",
            )
        else:
            await _send_info(
                interaction, "Active Project Cleared",
                description="No active project is set for the chat agent.",
            )

    # ===================================================================
    # TASK COMMANDS
    # ===================================================================

    @bot.tree.command(name="tasks", description="List tasks for a project")
    @app_commands.describe(
        show_completed="Include completed tasks (default: hide completed)",
        view="Display format: list (default interactive), tree (hierarchy), compact (summary)",
    )
    @app_commands.choices(view=[
        app_commands.Choice(name="list",    value="list"),
        app_commands.Choice(name="tree",    value="tree"),
        app_commands.Choice(name="compact", value="compact"),
    ])
    async def tasks_command(
        interaction: discord.Interaction,
        show_completed: bool = False,
        view: app_commands.Choice[str] | None = None,
    ):
        await interaction.response.defer()
        try:
            view_mode = view.value if view else "list"
            project_id = await _resolve_project_from_context(interaction, None)

            # Map slash-command view names to command handler display_mode values
            _VIEW_TO_DISPLAY_MODE = {"list": "flat", "tree": "tree", "compact": "compact"}
            display_mode = _VIEW_TO_DISPLAY_MODE[view_mode]

            args: dict = {
                "include_completed": show_completed,
                "display_mode": display_mode,
            }
            if project_id:
                args["project_id"] = project_id

            # tree/compact modes require a project_id; warn if missing
            if display_mode in ("tree", "compact") and not project_id:
                await interaction.followup.send(
                    embed=error_embed(
                        "Project Required",
                        description=(
                            "Tree and compact views require a project context. "
                            "Set an active project with `/set-project` first."
                        ),
                    ),
                )
                return

            result = await handler.execute("list_tasks", args)

            if "error" in result:
                await interaction.followup.send(
                    embed=error_embed("Error", description=result["error"]),
                )
                return

            # ── Tree / Compact display modes ──────────────────────────
            if display_mode in ("tree", "compact"):
                await _send_tree_or_compact(
                    interaction, result, show_completed, display_mode,
                )
                return

            # ── Flat / list display mode (default) ────────────────────
            tasks = result.get("tasks", [])
            if not tasks:
                if not show_completed:
                    # Check if there are completed/terminal tasks being hidden
                    check_args = {**args, "include_completed": True}
                    # Always use flat mode for the hidden-tasks check
                    check_args.pop("display_mode", None)
                    check_result = await handler.execute("list_tasks", check_args)
                    total_count = check_result.get("total", 0)
                    if total_count > 0:
                        await interaction.followup.send(
                            embed=success_embed(
                                "All Tasks Completed",
                                description=(
                                    f"All {total_count} task(s) are completed. "
                                    f"Use `/tasks show_completed:True` to view them."
                                ),
                            ),
                        )
                        return
                desc = "No tasks found"
                if project_id:
                    desc += f" for project `{project_id}`"
                    # Hint about cross-project view
                    desc += ". Check `/status` for a cross-project overview."
                else:
                    desc += "."
                await interaction.followup.send(
                    embed=info_embed("No Tasks", description=desc),
                )
                return

            # ----- Default list view (interactive grouped report) ---------------
            # Group tasks by status
            tasks_by_status: dict[str, list] = {}
            for t in tasks:
                tasks_by_status.setdefault(t["status"], []).append(t)
            total = result.get("total", len(tasks))
            # Fetch all tasks (including completed) for the full-view report widget
            if not show_completed:
                all_result = await handler.execute(
                    "list_tasks",
                    {**args, "display_mode": "flat", "include_completed": True},
                )
                all_tasks = all_result.get("tasks", [])
            else:
                all_tasks = tasks
            view_widget = TaskReportView(tasks_by_status, total, all_tasks=all_tasks)
            content = view_widget.build_content()
            await interaction.followup.send(content, view=view_widget)
        except Exception as e:
            print(f"ERROR in /tasks: {e!r}\n{traceback.format_exc()}")
            try:
                await interaction.followup.send(
                    embed=error_embed("Error", description=f"Failed to list tasks: {e}"),
                )
            except Exception:
                pass  # interaction may have expired

    async def _send_tree_or_compact(
        interaction: discord.Interaction,
        result: dict,
        show_completed: bool,
        display_mode: str,
    ):
        """Render tree or compact task list with pagination for Discord limits.

        The command handler returns pre-formatted text for each root task
        tree.  This helper stitches them together into Discord messages,
        splitting across multiple messages when the output exceeds the
        2,000-char message limit or using an embed for longer content.
        """
        trees = result.get("trees", [])
        total_root = result.get("total_root_tasks", 0)
        total_tasks = result.get("total_tasks", 0)

        if not trees:
            # No tasks — check for hidden completed tasks
            if not show_completed:
                hint = " Use `/tasks show_completed:True` to include completed."
            else:
                hint = ""
            await interaction.followup.send(
                embed=info_embed(
                    "No Tasks",
                    description=f"No tasks found for this project.{hint}",
                ),
            )
            return

        mode_label = "Tree View" if display_mode == "tree" else "Compact View"

        # Build header line
        header = f"**{mode_label}** — {total_root} root task(s), {total_tasks} total"

        # Assemble all formatted tree blocks
        blocks: list[str] = []
        for entry in trees:
            formatted = entry.get("formatted", "")
            # In compact mode, append the progress bar inline if available
            bar = entry.get("progress_bar")
            if bar and display_mode == "compact":
                formatted += f"\n  {bar}"
            blocks.append(formatted)

        # Tree mode uses a code block for monospace alignment of
        # box-drawing characters; compact mode uses regular markdown.
        if display_mode == "tree":
            body = "\n\n".join(blocks)
            # Wrap in a code block for monospace alignment
            full_text = f"{header}\n```\n{body}\n```"
        else:
            # Compact mode — regular markdown
            body = "\n".join(blocks)
            full_text = f"{header}\n\n{body}"

        # ── Pagination: split into chunks respecting Discord limits ──
        # Discord message limit is 2,000 chars.  If the full text fits,
        # send as a single message.  Otherwise split into pages.
        if len(full_text) <= 2000:
            await interaction.followup.send(full_text)
            return

        # Strategy: send header + as many tree blocks as fit per message,
        # then continue with followup messages for the rest.
        _MSG_LIMIT = 1950  # leave margin for safety
        pages: list[str] = []
        current_page = header

        for i, block in enumerate(blocks):
            if display_mode == "tree":
                # Each page wraps its content in a code block
                # Check if adding this block (with code fences) fits
                candidate = current_page + ("\n\n" if i > 0 or current_page != header else "\n```\n") + block
                test_text = candidate + "\n```" if "```" in candidate else candidate
                if len(test_text) > _MSG_LIMIT and current_page != header:
                    # Close current page and start new one
                    if "```\n" in current_page and not current_page.endswith("```"):
                        current_page += "\n```"
                    pages.append(current_page)
                    current_page = f"```\n{block}"
                else:
                    if current_page == header:
                        current_page += f"\n```\n{block}"
                    else:
                        current_page += f"\n\n{block}"
            else:
                # Compact mode — plain text
                candidate = current_page + "\n" + block
                if len(candidate) > _MSG_LIMIT and current_page != header:
                    pages.append(current_page)
                    current_page = block
                else:
                    current_page = candidate

        # Close final code block for tree mode
        if display_mode == "tree" and "```\n" in current_page and not current_page.endswith("```"):
            current_page += "\n```"
        pages.append(current_page)

        # Send first page as the interaction response, rest as followups
        await interaction.followup.send(pages[0])
        for page in pages[1:]:
            await interaction.followup.send(page)

        # If total output is very large (>6000), also attach a text file
        total_len = sum(len(p) for p in pages)
        if total_len > 6000:
            raw = header + "\n\n" + "\n\n".join(blocks)
            file = discord.File(
                fp=io.BytesIO(raw.encode("utf-8")),
                filename="task_tree.txt",
            )
            await interaction.followup.send(
                "_Full task tree attached below._", file=file,
            )

    @bot.tree.command(
        name="active-tasks",
        description="List active tasks across ALL projects",
    )
    @app_commands.describe(
        show_completed="Include completed/failed/blocked tasks (default: hide)",
    )
    async def active_tasks_command(
        interaction: discord.Interaction,
        show_completed: bool = False,
    ):
        await interaction.response.defer()
        result = await handler.execute(
            "list_active_tasks_all_projects",
            {"include_completed": show_completed},
        )

        by_project: dict[str, list] = result.get("by_project", {})
        total = result.get("total", 0)
        hidden = result.get("hidden_completed", 0)

        if total == 0:
            desc = (
                "No active tasks across any project."
                if not show_completed
                else "No tasks found across any project."
            )
            if hidden > 0:
                desc += (
                    f"\n\n_{hidden} completed/failed task(s) hidden — "
                    f"use `/active-tasks show_completed:True` to view._"
                )
            await interaction.followup.send(
                embed=info_embed("No Tasks", description=desc),
            )
            return

        # Build embed with one field per project (up to 25 field limit).
        label = "tasks" if show_completed else "active tasks"
        description = f"**{total} {label}** across **{len(by_project)} project(s)**"
        if hidden > 0:
            description += f"\n_{hidden} completed/failed task(s) hidden_"

        # Build per-project fields. Each project gets one embed field.
        # Cap tasks per project to fit within 1024-char field value limit.
        fields: list[tuple[str, str, bool]] = []
        max_tasks_per_field = 12  # keep field values well under 1024 chars
        for project_id in sorted(by_project.keys()):
            project_tasks = by_project[project_id]
            task_lines: list[str] = []
            for t in project_tasks[:max_tasks_per_field]:
                emoji = STATUS_EMOJIS.get(t["status"], "\u26AA")
                title = t["title"]
                if len(title) > 60:
                    title = title[:57] + "..."
                agent_info = ""
                if t.get("assigned_agent"):
                    agent_info = f" \u2190 `{t['assigned_agent']}`"
                task_lines.append(f"{emoji} **{title}** `{t['id']}`{agent_info}")
            if len(project_tasks) > max_tasks_per_field:
                task_lines.append(
                    f"_...and {len(project_tasks) - max_tasks_per_field} more_"
                )
            field_value = "\n".join(task_lines)
            # Truncate field value to Discord's limit as a safety net
            field_value = truncate(field_value, LIMIT_FIELD_VALUE)
            fields.append((
                f"`{project_id}` ({len(project_tasks)})",
                field_value,
                False,  # not inline — each project gets a full-width field
            ))

        embed = info_embed(
            "Active Tasks — All Projects",
            description=description,
            fields=fields,
        )
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="task", description="Show full details of a task")
    @app_commands.describe(task_id="Task ID")
    async def task_command(interaction: discord.Interaction, task_id: str):
        detail = await _format_task_detail(task_id)
        if detail is None:
            await interaction.response.send_message(
                embed=error_embed("Task Not Found", description=f"Task `{task_id}` was not found."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(detail)

    @bot.tree.command(name="add-task", description="Add a task manually to the active project")
    @app_commands.describe(description="What the task should do")
    async def add_task_command(interaction: discord.Interaction, description: str):
        project_id = (
            bot.get_project_for_channel(interaction.channel_id)
            or handler._active_project_id
        )
        if not project_id:
            await interaction.response.send_message(
                "No project context — use this in a project channel or set an active project.",
                ephemeral=True,
            )
            return
        title = description[:100] if len(description) > 100 else description
        result = await handler.execute("create_task", {
            "project_id": project_id,
            "title": title,
            "description": description,
        })
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        desc_preview = truncate(description, LIMIT_FIELD_VALUE)
        embed = success_embed(
            "Task Added",
            fields=[
                ("ID", f"`{result['created']}`", True),
                ("Project", f"`{result['project_id']}`", True),
                ("Status", "🔵 READY", True),
                ("Description", desc_preview, False),
            ],
        )
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="edit-task", description="Edit a task's properties")
    @app_commands.describe(
        task_id="Task ID",
        title="New title (optional)",
        description="New description (optional)",
        priority="New priority (optional)",
        task_type="New task type (optional)",
        status="New status — admin override (optional)",
        max_retries="Max retry attempts (optional)",
        verification_type="How to verify task output (optional)",
    )
    @app_commands.choices(
        task_type=[
            app_commands.Choice(name="feature",  value="feature"),
            app_commands.Choice(name="bugfix",   value="bugfix"),
            app_commands.Choice(name="refactor", value="refactor"),
            app_commands.Choice(name="test",     value="test"),
            app_commands.Choice(name="docs",     value="docs"),
            app_commands.Choice(name="chore",    value="chore"),
            app_commands.Choice(name="research", value="research"),
            app_commands.Choice(name="plan",     value="plan"),
        ],
        status=[
            app_commands.Choice(name="DEFINED",     value="DEFINED"),
            app_commands.Choice(name="READY",       value="READY"),
            app_commands.Choice(name="IN_PROGRESS", value="IN_PROGRESS"),
            app_commands.Choice(name="COMPLETED",   value="COMPLETED"),
            app_commands.Choice(name="FAILED",      value="FAILED"),
            app_commands.Choice(name="BLOCKED",     value="BLOCKED"),
        ],
        verification_type=[
            app_commands.Choice(name="auto_test", value="auto_test"),
            app_commands.Choice(name="qa_agent",  value="qa_agent"),
            app_commands.Choice(name="human",     value="human"),
        ],
    )
    async def edit_task_command(
        interaction: discord.Interaction,
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        priority: int | None = None,
        task_type: app_commands.Choice[str] | None = None,
        status: app_commands.Choice[str] | None = None,
        max_retries: int | None = None,
        verification_type: app_commands.Choice[str] | None = None,
    ):
        args: dict = {"task_id": task_id}
        if title is not None:
            args["title"] = title
        if description is not None:
            args["description"] = description
        if priority is not None:
            args["priority"] = priority
        if task_type is not None:
            args["task_type"] = task_type.value
        if status is not None:
            args["status"] = status.value
        if max_retries is not None:
            args["max_retries"] = max_retries
        if verification_type is not None:
            args["verification_type"] = verification_type.value
        result = await handler.execute("edit_task", args)
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        fields = ", ".join(result.get("fields", []))
        desc = f"Task `{task_id}` updated: {fields}"
        if result.get("old_status"):
            from src.discord.embeds import STATUS_EMOJIS
            old_emoji = STATUS_EMOJIS.get(result["old_status"], "")
            new_emoji = STATUS_EMOJIS.get(result["new_status"], "")
            desc += f"\n{old_emoji} **{result['old_status']}** → {new_emoji} **{result['new_status']}**"
        await _send_success(
            interaction, "Task Updated",
            description=desc,
        )

    @bot.tree.command(name="stop-task", description="Stop a task that is currently in progress")
    @app_commands.describe(task_id="Task ID to stop")
    async def stop_task_command(interaction: discord.Interaction, task_id: str):
        result = await handler.execute("stop_task", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        await _send_success(interaction, "Task Stopped", description=f"Task `{task_id}` has been stopped.")

    @bot.tree.command(name="restart-task", description="Reset a task back to READY for re-execution")
    @app_commands.describe(task_id="Task ID to restart")
    async def restart_task_command(interaction: discord.Interaction, task_id: str):
        result = await handler.execute("restart_task", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        await _send_success(
            interaction, "Task Restarted",
            description=f"Task `{task_id}` restarted ({result.get('previous_status', '?')} → READY)",
        )

    @bot.tree.command(
        name="reopen-with-feedback",
        description="Reopen a task with QA feedback appended to its description",
    )
    @app_commands.describe(
        task_id="Task ID to reopen",
        feedback="QA feedback explaining what went wrong or needs fixing",
    )
    async def reopen_with_feedback_command(
        interaction: discord.Interaction, task_id: str, feedback: str,
    ):
        result = await handler.execute(
            "reopen_with_feedback", {"task_id": task_id, "feedback": feedback},
        )
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        prev = result.get('previous_status', '?')
        title = result.get('title', '')
        await _send_success(
            interaction, "Task Reopened with Feedback",
            description=(
                f"Task `{task_id}` ({title}) reopened ({prev} → READY).\n\n"
                f"**Feedback added:**\n{feedback[:500]}"
            ),
        )

    @bot.tree.command(name="delete-task", description="Delete a task")
    @app_commands.describe(task_id="Task ID to delete")
    async def delete_task_command(interaction: discord.Interaction, task_id: str):
        result = await handler.execute("delete_task", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        await _send_success(
            interaction, "Task Deleted",
            description=f"Task `{task_id}` ({result.get('title', '')}) deleted.",
        )

    @bot.tree.command(
        name="archive-tasks",
        description="Archive completed tasks (DB + markdown notes in workspace)",
    )
    @app_commands.describe(
        project_id="Project to archive completed tasks from (optional — omit for all projects)",
        include_failed="Also archive FAILED and BLOCKED tasks (default: false)",
    )
    async def archive_tasks_command(
        interaction: discord.Interaction,
        project_id: str | None = None,
        include_failed: bool = False,
    ):
        args: dict = {"include_failed": include_failed}
        if project_id:
            args["project_id"] = project_id
        await interaction.response.defer()
        result = await handler.execute("archive_tasks", args)
        if "error" in result:
            await _send_error(interaction, result['error'], followup=True)
            return
        if "message" in result:
            await _send_info(
                interaction, "Nothing to Archive",
                description=result["message"], followup=True,
            )
            return
        count = result.get("archived_count", 0)
        scope = f" from `{project_id}`" if project_id else ""
        archive_dir = result.get("archive_dir")
        desc = f"Archived **{count}** task{'s' if count != 1 else ''}{scope}."
        if archive_dir:
            desc += f"\nNotes written to `{archive_dir}`"
        await _send_success(
            interaction, "Tasks Archived", description=desc, followup=True,
        )

    @bot.tree.command(
        name="archive-task",
        description="Archive a single completed/failed/blocked task",
    )
    @app_commands.describe(task_id="Task ID to archive")
    async def archive_task_command(interaction: discord.Interaction, task_id: str):
        result = await handler.execute("archive_task", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        await _send_success(
            interaction, "Task Archived",
            description=(
                f"Task `{task_id}` ({result.get('title', '')}) archived "
                f"(was {result.get('status', '?')})."
            ),
        )

    @bot.tree.command(
        name="list-archived",
        description="View archived tasks",
    )
    @app_commands.describe(
        project_id="Filter by project (optional)",
        limit="Max tasks to show (default 25)",
    )
    async def list_archived_command(
        interaction: discord.Interaction,
        project_id: str | None = None,
        limit: int = 25,
    ):
        args: dict = {"limit": limit}
        if project_id:
            args["project_id"] = project_id
        result = await handler.execute("list_archived", args)
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        tasks = result.get("tasks", [])
        total = result.get("total", 0)
        if not tasks:
            scope = f" in project `{project_id}`" if project_id else ""
            await _send_info(
                interaction, "No Archived Tasks",
                description=f"No archived tasks found{scope}.",
            )
            return
        lines = []
        for t in tasks:
            status = t.get("status", "?")
            title = t.get("title", "")
            tid = t.get("id", "?")
            lines.append(f"• `{tid}` — {title} ({status})")
        body = "\n".join(lines)
        showing = f"Showing {len(tasks)} of {total}" if total > len(tasks) else f"{len(tasks)} task{'s' if len(tasks) != 1 else ''}"
        scope = f" in `{project_id}`" if project_id else ""
        await _send_info(
            interaction, f"Archived Tasks{scope}",
            description=f"{showing}\n\n{body}",
        )

    @bot.tree.command(
        name="restore-task",
        description="Restore an archived task back to active status",
    )
    @app_commands.describe(task_id="Archived task ID to restore")
    async def restore_task_command(interaction: discord.Interaction, task_id: str):
        result = await handler.execute("restore_task", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        await _send_success(
            interaction, "Task Restored",
            description=(
                f"Task `{task_id}` ({result.get('title', '')}) restored "
                f"with status DEFINED."
            ),
        )

    @bot.tree.command(
        name="archive-settings",
        description="View auto-archive configuration and status",
    )
    async def archive_settings_command(interaction: discord.Interaction):
        result = await handler.execute("archive_settings", {})
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        enabled = "✅ Enabled" if result["enabled"] else "❌ Disabled"
        hours = result["after_hours"]
        statuses = ", ".join(result["statuses"]) if result["statuses"] else "None"
        archived = result["archived_count"]
        eligible = result["eligible_count"]
        desc = (
            f"**Auto-Archive:** {enabled}\n"
            f"**Archive After:** {hours} hours\n"
            f"**Eligible Statuses:** {statuses}\n\n"
            f"**Currently Archived:** {archived} task{'s' if archived != 1 else ''}\n"
            f"**Eligible Now:** {eligible} task{'s' if eligible != 1 else ''} "
            f"ready to be auto-archived"
        )
        await _send_info(interaction, "Archive Settings", description=desc)

    @bot.tree.command(name="approve-task", description="Approve a task that is awaiting approval")
    @app_commands.describe(task_id="Task ID to approve")
    async def approve_task_command(interaction: discord.Interaction, task_id: str):
        result = await handler.execute("approve_task", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        await _send_success(
            interaction, "Task Approved",
            description=f"Task `{task_id}` ({result.get('title', '')}) approved and completed.",
        )

    @bot.tree.command(
        name="skip-task",
        description="Skip a blocked/failed task to unblock its dependency chain",
    )
    @app_commands.describe(task_id="Task ID to skip")
    async def skip_task_command(interaction: discord.Interaction, task_id: str):
        result = await handler.execute("skip_task", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        unblocked_count = result.get("unblocked_count", 0)
        desc = f"Task `{task_id}` skipped (marked COMPLETED)."
        if unblocked_count:
            unblocked_list = ", ".join(
                f"`{t['id']}`" for t in result.get("unblocked", [])
            )
            desc += f"\n{unblocked_count} task(s) unblocked: {unblocked_list}"
        await _send_success(interaction, "Task Skipped", description=desc)

    @bot.tree.command(
        name="chain-health",
        description="Check dependency chain health for stuck tasks",
    )
    @app_commands.describe(
        task_id="(Optional) Check a specific blocked task",
    )
    async def chain_health_command(
        interaction: discord.Interaction,
        task_id: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        args = {}
        if task_id:
            args["task_id"] = task_id
        if project_id:
            args["project_id"] = project_id
        result = await handler.execute("get_chain_health", args)
        if "error" in result:
            await _send_error(interaction, result['error'])
            return

        if "task_id" in result:
            stuck = result.get("stuck_downstream", [])
            if not stuck:
                await _send_success(
                    interaction, "Chain Healthy",
                    description=f"No stuck downstream tasks for `{result['task_id']}`.",
                )
            else:
                lines = [
                    f"**Stuck Chain:** `{result['task_id']}` — {result.get('title', '')}",
                    f"{len(stuck)} downstream task(s) stuck:",
                ]
                for t in stuck[:15]:
                    lines.append(f"  • `{t['id']}` — {t['title']} ({t['status']})")
                if len(stuck) > 15:
                    lines.append(f"  … and {len(stuck) - 15} more")
                await _send_warning(
                    interaction, "Stuck Chain Detected",
                    description="\n".join(lines),
                )
        else:
            chains = result.get("stuck_chains", [])
            if not chains:
                scope = f" in project `{result.get('project_id')}`" if result.get("project_id") else ""
                await _send_success(
                    interaction, "Chains Healthy",
                    description=f"No stuck dependency chains{scope}.",
                )
            else:
                lines = [f"**{len(chains)} stuck chain(s):**"]
                for chain in chains[:10]:
                    bt = chain["blocked_task"]
                    lines.append(
                        f"  • `{bt['id']}` — {bt['title']} "
                        f"→ {chain['stuck_count']} stuck task(s)"
                    )
                if len(chains) > 10:
                    lines.append(f"  … and {len(chains) - 10} more chains")
                await _send_warning(
                    interaction, "Stuck Chains Found",
                    description="\n".join(lines),
                )

    # Note: /set-status has been removed. Use /edit-task with the status
    # parameter instead.

    @bot.tree.command(name="task-result", description="Show the results/output of a completed task")
    @app_commands.describe(task_id="Task ID to inspect")
    async def task_result_command(interaction: discord.Interaction, task_id: str):
        await interaction.response.defer(ephemeral=True)
        result = await handler.execute("get_task_result", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result['error'], followup=True)
            return
        fields: list[tuple[str, str, bool]] = [
            ("Result", result.get("result", "unknown"), True),
        ]
        summary = result.get("summary") or ""
        if summary:
            fields.append(("Summary", truncate(summary, LIMIT_FIELD_VALUE), False))
        files = result.get("files_changed") or []
        if files:
            file_list = "\n".join(f"• `{f}`" for f in files[:20])
            if len(files) > 20:
                file_list += f"\n_...and {len(files) - 20} more_"
            fields.append(("Files Changed", truncate(file_list, LIMIT_FIELD_VALUE), False))
        tokens = result.get("tokens_used", 0)
        if tokens:
            fields.append(("Tokens Used", f"{tokens:,}", True))
        error_msg = result.get("error_message") or ""
        if error_msg:
            snippet = truncate(error_msg, 500)
            fields.append(("Error", f"```\n{snippet}\n```", False))
        if result.get("result") == "completed":
            embed = success_embed(f"Task Result: {task_id}", fields=fields)
        else:
            embed = error_embed(f"Task Result: {task_id}", fields=fields)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @bot.tree.command(name="task-diff", description="Show the git diff for a task's branch")
    @app_commands.describe(task_id="Task ID")
    async def task_diff_command(interaction: discord.Interaction, task_id: str):
        await interaction.response.defer(ephemeral=True)
        result = await handler.execute("get_task_diff", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result['error'], followup=True)
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
        name="task-deps",
        description="Show dependency graph for a task (what it needs and blocks)",
    )
    @app_commands.describe(task_id="Task ID to inspect")
    async def task_deps_command(interaction: discord.Interaction, task_id: str):
        result = await handler.execute("task_deps", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result['error'])
            return

        status = result["status"]
        title = result["title"]
        task_emoji = STATUS_EMOJIS.get(status, "⚪")

        depends_on: list[dict] = result.get("depends_on", [])
        blocks: list[dict] = result.get("blocks", [])

        # Build embed fields
        fields: list[tuple[str, str, bool]] = [
            ("Task", f"{task_emoji} `{task_id}` — {title}", False),
            ("Status", f"{task_emoji} {status}", True),
        ]

        # Upstream: what this task needs
        if depends_on:
            dep_lines = []
            for dep in depends_on:
                emoji = STATUS_EMOJIS.get(dep["status"], "⚪")
                dep_lines.append(f"{emoji} `{dep['id']}` — {dep['title']} ({dep['status']})")
            fields.append(("Depends On", truncate("\n".join(dep_lines), LIMIT_FIELD_VALUE), False))
        else:
            fields.append(("Depends On", "_No upstream dependencies_", False))

        # Downstream: what this task blocks
        if blocks:
            blk_lines = []
            for blk in blocks:
                emoji = STATUS_EMOJIS.get(blk["status"], "⚪")
                blk_lines.append(f"{emoji} `{blk['id']}` — {blk['title']} ({blk['status']})")
            fields.append(("Blocks", truncate("\n".join(blk_lines), LIMIT_FIELD_VALUE), False))
        else:
            fields.append(("Blocks", "_No downstream dependents_", False))

        embed = status_embed(
            status,
            f"Dependencies: {task_id}",
            fields=fields,
        )
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(
        name="agent-error",
        description="Show the last error recorded for a task",
    )
    @app_commands.describe(task_id="Task ID to inspect")
    async def agent_error_command(interaction: discord.Interaction, task_id: str):
        await interaction.response.defer(ephemeral=True)
        result = await handler.execute("get_agent_error", {"task_id": task_id})
        if "error" in result:
            await _send_error(interaction, result['error'], followup=True)
            return
        fields: list[tuple[str, str, bool]] = [
            ("Task", result.get("title", ""), False),
            ("Status", result.get("status", ""), True),
            ("Retries", result.get("retries", ""), True),
        ]

        if result.get("message"):
            embed = error_embed(
                f"Agent Error Report: {task_id}",
                description=f"_{result['message']}_",
                fields=fields,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        fields.append(("Result", result.get("result", "unknown"), True))
        fields.append(("Error Type", f"**{result.get('error_type', 'unknown')}**", False))
        error_msg = result.get("error_message") or ""
        if error_msg:
            snippet = truncate(error_msg, 990)
            fields.append(("Error Detail", f"```\n{snippet}\n```", False))
        else:
            fields.append(("Error Detail", "_No error message recorded._", False))
        fields.append(("Suggested Fix", result.get("suggested_fix", "Review the logs"), False))
        summary = result.get("agent_summary") or ""
        if summary:
            fields.append(("Agent Summary", truncate(summary, 500), False))
        embed = error_embed(f"Agent Error Report: {task_id}", fields=fields)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ===================================================================
    # AGENT COMMANDS
    # ===================================================================

    @bot.tree.command(name="create-agent", description="Register a new agent")
    @app_commands.describe(
        name="Agent display name (leave empty for auto-generated creative name)",
        agent_type="Agent type (claude, codex, cursor, aider)",
    )
    @app_commands.choices(agent_type=[
        app_commands.Choice(name="claude", value="claude"),
        app_commands.Choice(name="codex",  value="codex"),
        app_commands.Choice(name="cursor", value="cursor"),
        app_commands.Choice(name="aider",  value="aider"),
    ])
    async def create_agent_command(
        interaction: discord.Interaction,
        name: str | None = None,
        agent_type: app_commands.Choice[str] | None = None,
    ):
        args: dict = {}
        if name:
            args["name"] = name
        if agent_type:
            args["agent_type"] = agent_type.value
        result = await handler.execute("create_agent", args)
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        agent_fields: list[tuple[str, str, bool]] = [
            ("Name", result.get("name", name), True),
            ("ID", f"`{result['created']}`", True),
            ("Type", args.get("agent_type", "claude"), True),
            ("State", result.get("state", "IDLE"), True),
        ]
        embed = success_embed("Agent Registered", fields=agent_fields)
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(
        name="pause-agent",
        description="Pause an agent so it stops receiving new tasks",
    )
    @app_commands.describe(agent_id="Agent ID to pause")
    async def pause_agent_command(
        interaction: discord.Interaction,
        agent_id: str,
    ):
        result = await handler.execute("pause_agent", {"agent_id": agent_id})
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        desc = f"Agent `{agent_id}` is now paused."
        if result.get("note"):
            desc += f"\n{result['note']}"
        embed = success_embed("Agent Paused", description=desc)
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(
        name="resume-agent",
        description="Resume a paused agent so it can receive tasks again",
    )
    @app_commands.describe(agent_id="Agent ID to resume")
    async def resume_agent_command(
        interaction: discord.Interaction,
        agent_id: str,
    ):
        result = await handler.execute("resume_agent", {"agent_id": agent_id})
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        embed = success_embed(
            "Agent Resumed",
            description=f"Agent `{agent_id}` is now IDLE and ready to receive tasks.",
        )
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="edit-agent", description="Edit an agent's properties")
    @app_commands.describe(
        agent_id="Agent ID",
        name="New display name (optional)",
        agent_type="New agent type (optional)",
    )
    @app_commands.choices(agent_type=[
        app_commands.Choice(name="claude", value="claude"),
        app_commands.Choice(name="codex",  value="codex"),
        app_commands.Choice(name="cursor", value="cursor"),
        app_commands.Choice(name="aider",  value="aider"),
    ])
    async def edit_agent_command(
        interaction: discord.Interaction,
        agent_id: str,
        name: str | None = None,
        agent_type: app_commands.Choice[str] | None = None,
    ):
        args: dict = {"agent_id": agent_id}
        if name is not None:
            args["name"] = name
        if agent_type is not None:
            args["agent_type"] = agent_type.value
        result = await handler.execute("edit_agent", args)
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        fields = ", ".join(result.get("fields", []))
        await _send_success(
            interaction, "Agent Updated",
            description=f"Agent `{agent_id}` updated: {fields}",
        )

    @bot.tree.command(
        name="delete-agent",
        description="Delete an agent and its workspace mappings",
    )
    @app_commands.describe(agent_id="Agent ID to delete")
    async def delete_agent_command(
        interaction: discord.Interaction,
        agent_id: str,
    ):
        result = await handler.execute("delete_agent", {"agent_id": agent_id})
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        embed = success_embed(
            "Agent Deleted",
            description=f"Agent `{result['name']}` (`{agent_id}`) has been removed.",
        )
        await interaction.response.send_message(embed=embed)

    # ===================================================================
    # WORKSPACE COMMANDS
    # ===================================================================

    @bot.tree.command(name="workspaces", description="List project workspaces")
    async def workspaces_command(interaction: discord.Interaction):
        project_id = await _resolve_project_from_context(interaction, None)
        args = {}
        if project_id:
            args["project_id"] = project_id
        result = await handler.execute("list_workspaces", args)
        workspaces = result.get("workspaces", [])
        if not workspaces:
            await _send_info(
                interaction, "No Workspaces",
                description="No workspaces registered. Use `/add-workspace` to add one.",
            )
            return
        lines = ["## Workspaces"]
        for ws in workspaces:
            lock_info = ""
            if ws.get("locked_by_agent_id"):
                lock_info = f" 🔒 agent=`{ws['locked_by_agent_id']}`"
                if ws.get("locked_by_task_id"):
                    lock_info += f" task=`{ws['locked_by_task_id']}`"
            name_str = f" **{ws['name']}**" if ws.get("name") else ""
            lines.append(
                f"• `{ws['id']}`{name_str} ({ws['source_type']}) — "
                f"`{ws['workspace_path']}` project=`{ws['project_id']}`{lock_info}"
            )
        msg = "\n".join(lines)
        if len(msg) > 2000:
            msg = msg[:1997] + "..."
        await interaction.response.send_message(msg)

    @bot.tree.command(name="add-workspace", description="Add a workspace directory for a project")
    @app_commands.describe(
        source="How to set up the workspace",
        path="Directory path (required for link, auto-generated for clone)",
        name="Workspace name (optional)",
    )
    @app_commands.choices(source=[
        app_commands.Choice(name="clone", value="clone"),
        app_commands.Choice(name="link",  value="link"),
    ])
    async def add_workspace_command(
        interaction: discord.Interaction,
        source: app_commands.Choice[str],
        path: str | None = None,
        name: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        args: dict = {
            "project_id": project_id,
            "source": source.value,
        }
        if path:
            args["path"] = path
        if name:
            args["name"] = name
        result = await handler.execute("add_workspace", args)
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        embed = success_embed(
            "Workspace Added",
            fields=[
                ("ID", f"`{result['created']}`", True),
                ("Source", source.value, True),
                ("Project", f"`{project_id}`", True),
                ("Path", f"`{result.get('workspace_path', '')}`", False),
            ],
        )
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(
        name="release-workspace",
        description="Force-release a stuck workspace lock",
    )
    @app_commands.describe(workspace_id="Workspace ID to release")
    async def release_workspace_command(
        interaction: discord.Interaction,
        workspace_id: str,
    ):
        result = await handler.execute("release_workspace", {"workspace_id": workspace_id})
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        await _send_success(
            interaction, "Workspace Released",
            description=f"Workspace `{workspace_id}` lock has been released.",
        )

    # ===================================================================
    # GIT COMMANDS
    # ===================================================================

    @bot.tree.command(
        name="git-status",
        description="Show the git status of a project's repository",
    )
    async def git_status_command(interaction: discord.Interaction):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        await interaction.response.defer()
        result = await handler.execute("get_git_status", {"project_id": project_id})
        if "error" in result:
            await _send_error(interaction, result['error'], followup=True)
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

    # -------------------------------------------------------------------
    # GIT MANAGEMENT COMMANDS
    # -------------------------------------------------------------------
    # Project-based git commands that auto-detect the project from the
    # Discord channel.  These call the newer project-oriented handlers
    # (create_branch, checkout_branch, commit_changes, push_branch,
    # merge_branch, git_branch) which resolve repos via project_id.

    @bot.tree.command(
        name="git-branches",
        description="List branches or create a new branch in a project's repository",
    )
    @app_commands.describe(
        name="New branch name to create (omit to list branches)",
        repo_id="Repo ID (optional — uses first repo if omitted)",
    )
    async def git_branches_command(
        interaction: discord.Interaction,
        name: str | None = None,
        repo_id: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {"project_id": project_id}
        if repo_id:
            args["repo_id"] = repo_id
        if name:
            args["name"] = name
        result = await handler.execute("git_branch", args)
        if "error" in result:
            await _send_error(interaction, result['error'], followup=True)
            return

        if "created" in result:
            await _send_success(
                interaction, "Branch Created",
                description=f"Created and switched to branch `{result['created']}` on `{project_id}`",
                followup=True,
            )
        else:
            current = result.get("current_branch", "?")
            branches = result.get("branches", [])
            branch_list = "\n".join(branches) if branches else "(no branches)"
            text = (
                f"## Branches: `{project_id}`\n"
                f"**Current:** `{current}`\n```\n{branch_list}\n```"
            )
            await _send_long(interaction, text, followup=True)

    @bot.tree.command(
        name="git-checkout",
        description="Switch to an existing branch in a project's repository",
    )
    @app_commands.describe(
        branch_name="Branch name to switch to",
        repo_id="Repo ID (optional — uses first repo if omitted)",
    )
    async def git_checkout_command(
        interaction: discord.Interaction,
        branch_name: str,
        repo_id: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {"project_id": project_id, "branch_name": branch_name}
        if repo_id:
            args["repo_id"] = repo_id
        result = await handler.execute("checkout_branch", args)
        if "error" in result:
            await _send_error(interaction, result['error'], followup=True)
            return
        await _send_success(
            interaction, "Branch Switched",
            description=f"Switched to branch `{result['branch']}` on `{project_id}`",
            followup=True,
            result=result,
        )

    @bot.tree.command(
        name="project-commit",
        description="Stage all changes and commit in a project's repository",
    )
    @app_commands.describe(
        message="Commit message",
        repo_id="Repo ID (optional — uses first repo if omitted)",
    )
    async def project_commit_command(
        interaction: discord.Interaction,
        message: str,
        repo_id: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {"project_id": project_id, "message": message}
        if repo_id:
            args["repo_id"] = repo_id
        result = await handler.execute("commit_changes", args)
        if "error" in result:
            await _send_error(interaction, result['error'], followup=True)
            return
        if result.get("status") == "committed":
            repo_label = result.get("repo_id", "?")
            await _send_success(
                interaction, "Changes Committed",
                description=f"Committed in `{repo_label}` on `{project_id}`: {message}",
                followup=True,
                result=result,
            )
        else:
            await _send_info(
                interaction, "Nothing to Commit",
                description=f"Working tree clean on `{project_id}`.",
                followup=True,
            )

    @bot.tree.command(
        name="project-push",
        description="Push a branch to origin in a project's repository",
    )
    @app_commands.describe(
        branch_name="Branch to push (defaults to current branch)",
        repo_id="Repo ID (optional — uses first repo if omitted)",
    )
    async def project_push_command(
        interaction: discord.Interaction,
        branch_name: str | None = None,
        repo_id: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {"project_id": project_id}
        if branch_name:
            args["branch_name"] = branch_name
        if repo_id:
            args["repo_id"] = repo_id
        result = await handler.execute("push_branch", args)
        if "error" in result:
            await _send_error(interaction, result['error'], followup=True)
            return
        pushed_branch = result.get("branch", "?")
        await _send_success(
            interaction, "Branch Pushed",
            description=f"Pushed `{pushed_branch}` to origin on `{project_id}`",
            followup=True,
        )

    @bot.tree.command(
        name="project-merge",
        description="Merge a branch into the default branch in a project's repository",
    )
    @app_commands.describe(
        branch_name="Branch to merge",
        repo_id="Repo ID (optional — uses first repo if omitted)",
    )
    async def project_merge_command(
        interaction: discord.Interaction,
        branch_name: str,
        repo_id: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {"project_id": project_id, "branch_name": branch_name}
        if repo_id:
            args["repo_id"] = repo_id
        result = await handler.execute("merge_branch", args)
        if "error" in result:
            await _send_error(interaction, result['error'], followup=True)
            return
        target = result.get("target", "main")
        if result.get("status") == "conflict":
            await _send_warning(
                interaction, "Merge Conflict",
                description=(
                    f"`{branch_name}` could not be merged into `{target}` "
                    f"on `{project_id}`. Merge was aborted."
                ),
                followup=True,
            )
        else:
            await _send_success(
                interaction, "Branch Merged",
                description=f"Merged `{branch_name}` into `{target}` on `{project_id}`",
                followup=True,
                result=result,
            )

    @bot.tree.command(
        name="project-create-branch",
        description="Create and switch to a new branch in a project's repository",
    )
    @app_commands.describe(
        branch_name="Name for the new branch",
        repo_id="Repo ID (optional — uses first repo if omitted)",
    )
    async def project_create_branch_command(
        interaction: discord.Interaction,
        branch_name: str,
        repo_id: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {"project_id": project_id, "branch_name": branch_name}
        if repo_id:
            args["repo_id"] = repo_id
        result = await handler.execute("create_branch", args)
        if "error" in result:
            await _send_error(interaction, result['error'], followup=True)
            return
        await _send_success(
            interaction, "Branch Created",
            description=f"Created and switched to branch `{result['branch']}` on `{project_id}`",
            followup=True,
        )

    @bot.tree.command(
        name="create-branch",
        description="Create a new git branch in a project's repo",
    )
    @app_commands.describe(
        branch_name="Name for the new branch",
        repo_id="Specific repo ID (optional)",
    )
    async def create_branch_command(
        interaction: discord.Interaction,
        branch_name: str,
        repo_id: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        args: dict = {"project_id": project_id, "branch_name": branch_name}
        if repo_id:
            args["repo_id"] = repo_id
        result = await handler.execute("create_branch", args)
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        await _send_success(
            interaction, "Branch Created",
            description=f"Branch `{branch_name}` created in `{result.get('repo_id', project_id)}`",
        )

    @bot.tree.command(
        name="checkout-branch",
        description="Switch to an existing git branch",
    )
    @app_commands.describe(
        branch_name="Branch name to check out",
        repo_id="Specific repo ID (optional)",
    )
    async def checkout_branch_command(
        interaction: discord.Interaction,
        branch_name: str,
        repo_id: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        args: dict = {"project_id": project_id, "branch_name": branch_name}
        if repo_id:
            args["repo_id"] = repo_id
        result = await handler.execute("checkout_branch", args)
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        await _send_success(
            interaction, "Branch Switched",
            description=f"Switched to branch `{branch_name}` in `{result.get('repo_id', project_id)}`",
            result=result,
        )

    @bot.tree.command(
        name="commit",
        description="Stage all changes and commit",
    )
    @app_commands.describe(
        message="Commit message",
        repo_id="Specific repo ID (optional)",
    )
    async def commit_command(
        interaction: discord.Interaction,
        message: str,
        repo_id: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        args: dict = {"project_id": project_id, "message": message}
        if repo_id:
            args["repo_id"] = repo_id
        result = await handler.execute("commit_changes", args)
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        if result.get("status") == "nothing_to_commit":
            await _send_info(
                interaction, "Nothing to Commit",
                description=f"Working tree clean in `{result.get('repo_id', project_id)}`.",
            )
            return
        await _send_success(
            interaction, "Changes Committed",
            description=f"Committed in `{result.get('repo_id', project_id)}`: {message}",
            result=result,
        )

    @bot.tree.command(
        name="push",
        description="Push a branch to the remote",
    )
    @app_commands.describe(
        branch_name="Branch to push (optional — pushes current branch)",
        repo_id="Specific repo ID (optional)",
    )
    async def push_command(
        interaction: discord.Interaction,
        branch_name: str | None = None,
        repo_id: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        args: dict = {"project_id": project_id}
        if branch_name:
            args["branch_name"] = branch_name
        if repo_id:
            args["repo_id"] = repo_id
        result = await handler.execute("push_branch", args)
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        pushed_branch = result.get("branch", branch_name or "current")
        await _send_success(
            interaction, "Branch Pushed",
            description=f"Pushed `{pushed_branch}` in `{result.get('repo_id', project_id)}`",
        )

    @bot.tree.command(
        name="merge",
        description="Merge a branch into the default branch",
    )
    @app_commands.describe(
        branch_name="Branch to merge",
        repo_id="Specific repo ID (optional)",
    )
    async def merge_command(
        interaction: discord.Interaction,
        branch_name: str,
        repo_id: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        args: dict = {"project_id": project_id, "branch_name": branch_name}
        if repo_id:
            args["repo_id"] = repo_id
        result = await handler.execute("merge_branch", args)
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        if result.get("status") == "conflict":
            await _send_warning(
                interaction, "Merge Conflict",
                description=(
                    f"Merge conflict: `{branch_name}` → `{result.get('target', 'main')}` "
                    f"in `{result.get('repo_id', project_id)}`. Merge was aborted."
                ),
            )
            return
        await _send_success(
            interaction, "Branch Merged",
            description=f"Merged `{branch_name}` → `{result.get('target', 'main')}` in `{result.get('repo_id', project_id)}`",
            result=result,
        )

    @bot.tree.command(
        name="git-commit",
        description="Stage all changes and commit in a repository",
    )
    @app_commands.describe(
        message="Commit message",
        repo_id="Repository ID (optional — uses first repo if omitted)",
    )
    async def git_commit_command(
        interaction: discord.Interaction,
        message: str,
        repo_id: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        # Set active project from channel context so _resolve_repo_path can
        # infer the repository even when project_id is not in the args dict.
        if project_id:
            handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {"message": message}
        if project_id:
            args["project_id"] = project_id
        if repo_id:
            args["repo_id"] = repo_id
        result = await handler.execute("git_commit", args)
        if "error" in result:
            await _send_error(interaction, result['error'], followup=True)
            return
        label = result.get("repo_id", project_id or "repo")
        if result.get("committed"):
            await _send_success(
                interaction, "Changes Committed",
                description=f"Committed in `{label}`: {message}",
                followup=True,
            )
        else:
            await _send_info(
                interaction, "Nothing to Commit",
                description=f"Working tree clean in `{label}`.",
                followup=True,
            )

    @bot.tree.command(
        name="git-push",
        description="Push a branch to remote origin",
    )
    @app_commands.describe(
        repo_id="Repository ID (optional — uses first repo if omitted)",
        branch="Branch name to push (defaults to current branch)",
    )
    async def git_push_command(
        interaction: discord.Interaction,
        repo_id: str | None = None,
        branch: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if project_id:
            handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {}
        if project_id:
            args["project_id"] = project_id
        if repo_id:
            args["repo_id"] = repo_id
        if branch:
            args["branch"] = branch
        result = await handler.execute("git_push", args)
        if "error" in result:
            await _send_error(interaction, result['error'], followup=True)
            return
        label = result.get("repo_id", project_id or "repo")
        await _send_success(
            interaction, "Branch Pushed",
            description=f"Pushed `{result['pushed']}` in `{label}`",
            followup=True,
        )

    @bot.tree.command(
        name="git-branch",
        description="Create and switch to a new git branch",
    )
    @app_commands.describe(
        branch_name="Name for the new branch",
        repo_id="Repository ID (optional — uses first repo if omitted)",
    )
    async def git_branch_command(
        interaction: discord.Interaction,
        branch_name: str,
        repo_id: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if project_id:
            handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {"branch_name": branch_name}
        if project_id:
            args["project_id"] = project_id
        if repo_id:
            args["repo_id"] = repo_id
        result = await handler.execute("git_create_branch", args)
        if "error" in result:
            await _send_error(interaction, result['error'], followup=True)
            return
        label = result.get("repo_id", project_id or "repo")
        await _send_success(
            interaction, "Branch Created",
            description=f"Created and switched to branch `{branch_name}` in `{label}`",
            followup=True,
        )

    @bot.tree.command(
        name="git-merge",
        description="Merge a branch into the default branch",
    )
    @app_commands.describe(
        branch_name="Branch to merge",
        repo_id="Repository ID (optional — uses first repo if omitted)",
        default_branch="Target branch (defaults to repo's default branch)",
    )
    async def git_merge_command(
        interaction: discord.Interaction,
        branch_name: str,
        repo_id: str | None = None,
        default_branch: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if project_id:
            handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {"branch_name": branch_name}
        if project_id:
            args["project_id"] = project_id
        if repo_id:
            args["repo_id"] = repo_id
        if default_branch:
            args["default_branch"] = default_branch
        result = await handler.execute("git_merge", args)
        if "error" in result:
            await _send_error(interaction, result['error'], followup=True)
            return
        label = result.get("repo_id", project_id or "repo")
        if result.get("merged"):
            await _send_success(
                interaction, "Branch Merged",
                description=f"Merged `{branch_name}` into `{result['into']}` in `{label}`",
                followup=True,
            )
        else:
            await _send_warning(
                interaction, "Merge Conflict",
                description=(
                    f"`{branch_name}` could not be merged into "
                    f"`{result.get('into', 'default')}` in `{label}`. Merge was aborted."
                ),
                followup=True,
            )

    @bot.tree.command(
        name="git-pr",
        description="Create a GitHub pull request",
    )
    @app_commands.describe(
        title="PR title",
        repo_id="Repository ID (optional — uses first repo if omitted)",
        body="PR description (optional)",
        branch="Head branch (defaults to current)",
        base="Base branch (defaults to repo default)",
    )
    async def git_pr_command(
        interaction: discord.Interaction,
        title: str,
        repo_id: str | None = None,
        body: str = "",
        branch: str | None = None,
        base: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if project_id:
            handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {"title": title, "body": body}
        if project_id:
            args["project_id"] = project_id
        if repo_id:
            args["repo_id"] = repo_id
        if branch:
            args["branch"] = branch
        if base:
            args["base"] = base
        result = await handler.execute("git_create_pr", args)
        if "error" in result:
            await _send_error(interaction, result['error'], followup=True)
            return
        pr_url = result.get("pr_url", "")
        await _send_success(
            interaction, "Pull Request Created",
            description=(
                f"[View PR]({pr_url})\n"
                f"**Branch:** `{result.get('branch', '?')}` → `{result.get('base', '?')}`"
            ),
            followup=True,
        )

    @bot.tree.command(
        name="git-files",
        description="List files changed compared to a base branch",
    )
    @app_commands.describe(
        repo_id="Repository ID (optional — uses first repo if omitted)",
        base_branch="Branch to compare against (defaults to repo default)",
    )
    async def git_files_command(
        interaction: discord.Interaction,
        repo_id: str | None = None,
        base_branch: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if project_id:
            handler.set_active_project(project_id)
        await interaction.response.defer()
        args: dict = {}
        if project_id:
            args["project_id"] = project_id
        if repo_id:
            args["repo_id"] = repo_id
        if base_branch:
            args["base_branch"] = base_branch
        result = await handler.execute("git_changed_files", args)
        if "error" in result:
            await _send_error(interaction, result['error'], followup=True)
            return
        label = result.get("repo_id", project_id or "repo")
        files = result.get("files", [])
        count = result.get("count", 0)
        base = result.get("base_branch", "main")
        if not files:
            await _send_info(
                interaction, "No Changes",
                description=f"No files changed in `{label}` vs `{base}`",
                followup=True,
            )
            return
        file_list = "\n".join(f"• `{f}`" for f in files[:50])
        if count > 50:
            file_list += f"\n_...and {count - 50} more_"
        msg = (
            f"## Changed Files: `{label}` vs `{base}`\n"
            f"**{count} file(s) changed:**\n{file_list}"
        )
        await _send_long(interaction, msg, followup=True)

    @bot.tree.command(
        name="git-log",
        description="Show recent git commits",
    )
    @app_commands.describe(
        count="Number of commits to show (default 10)",
        repo_id="Specific repo ID (optional)",
    )
    async def git_log_command(
        interaction: discord.Interaction,
        count: int = 10,
        repo_id: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        args: dict = {"project_id": project_id, "count": count}
        if repo_id:
            args["repo_id"] = repo_id
        result = await handler.execute("git_log", args)
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        branch = result.get("branch", "?")
        log = result.get("log", "(no commits)")
        repo_label = result.get("repo_id", project_id)
        msg = f"## Git Log: `{repo_label}` (branch: `{branch}`)\n```\n{log}\n```"
        await _send_long(interaction, msg, followup=False)

    @bot.tree.command(
        name="git-diff",
        description="Show git diff for a project's repo",
    )
    @app_commands.describe(
        base_branch="Base branch to diff against (optional — shows working tree diff)",
        repo_id="Specific repo ID (optional)",
    )
    async def git_diff_command(
        interaction: discord.Interaction,
        base_branch: str | None = None,
        repo_id: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        handler.set_active_project(project_id)
        args: dict = {"project_id": project_id}
        if base_branch:
            args["base_branch"] = base_branch
        if repo_id:
            args["repo_id"] = repo_id
        result = await handler.execute("git_diff", args)
        if "error" in result:
            await _send_error(interaction, result['error'])
            return

        diff = result.get("diff", "(no changes)")
        base_label = result.get("base_branch", "working tree")
        repo_label = result.get("repo_id", project_id)
        header = f"**Repo:** `{repo_label}` | **Diff against:** `{base_label}`\n"

        if len(diff) > 1800:
            await interaction.response.defer()
            await interaction.followup.send(
                content=f"{header}*Diff attached ({len(diff):,} chars)*",
                file=discord.File(
                    fp=io.BytesIO(diff.encode("utf-8")),
                    filename=f"diff-{project_id}.patch",
                ),
            )
        else:
            await interaction.response.send_message(
                f"{header}```diff\n{diff}\n```"
            )

    # ===================================================================
    # HOOK COMMANDS
    # ===================================================================

    @bot.tree.command(name="hooks", description="List automation hooks")
    async def hooks_command(interaction: discord.Interaction):
        project_id = await _resolve_project_from_context(interaction, None)
        args = {}
        if project_id:
            args["project_id"] = project_id
        result = await handler.execute("list_hooks", args)
        hooks = result.get("hooks", [])
        if not hooks:
            await _send_info(interaction, "No Hooks", description="No hooks configured.")
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
        cooldown_seconds: int = 3600,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        if trigger_type.value == "periodic":
            try:
                interval = int(trigger_value)
            except ValueError:
                await _send_error(interaction, "trigger_value must be an integer (seconds) for periodic hooks.")
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
            await _send_error(interaction, result['error'])
            return
        await _send_success(
            interaction, "Hook Created",
            description=f"Hook **{name}** (`{result['created']}`) created in `{project_id}`",
        )

    @bot.tree.command(name="edit-hook", description="Edit an automation hook")
    @app_commands.describe(
        hook_id="Hook ID",
        name="New hook name (optional)",
        enabled="Enable or disable the hook",
        prompt_template="New prompt template (optional)",
        cooldown_seconds="New cooldown in seconds (optional)",
        max_tokens_per_run="Max tokens per run (optional, 0 to clear)",
    )
    async def edit_hook_command(
        interaction: discord.Interaction,
        hook_id: str,
        name: str | None = None,
        enabled: bool | None = None,
        prompt_template: str | None = None,
        cooldown_seconds: int | None = None,
        max_tokens_per_run: int | None = None,
    ):
        args: dict = {"hook_id": hook_id}
        if name is not None:
            args["name"] = name
        if enabled is not None:
            args["enabled"] = enabled
        if prompt_template is not None:
            args["prompt_template"] = prompt_template
        if cooldown_seconds is not None:
            args["cooldown_seconds"] = cooldown_seconds
        if max_tokens_per_run is not None:
            args["max_tokens_per_run"] = max_tokens_per_run if max_tokens_per_run > 0 else None
        result = await handler.execute("edit_hook", args)
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        fields = ", ".join(result.get("fields", []))
        await _send_success(
            interaction, "Hook Updated",
            description=f"Hook `{hook_id}` updated: {fields}",
        )

    @bot.tree.command(name="delete-hook", description="Delete an automation hook")
    @app_commands.describe(hook_id="Hook ID to delete")
    async def delete_hook_command(interaction: discord.Interaction, hook_id: str):
        result = await handler.execute("delete_hook", {"hook_id": hook_id})
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        await _send_success(
            interaction, "Hook Deleted",
            description=f"Hook **{result.get('name', hook_id)}** (`{hook_id}`) deleted.",
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
            await _send_error(interaction, result['error'])
            return
        runs = result.get("runs", [])
        hook_name = result.get("hook_name", hook_id)
        if not runs:
            await _send_info(
                interaction, "No Hook Runs",
                description=f"No runs found for hook **{hook_name}**.",
            )
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
            await _send_error(interaction, result['error'])
            return
        await _send_success(
            interaction, "Hook Fired",
            description=f"Hook `{hook_id}` fired — status: {result.get('status', 'running')}",
        )

    # ===================================================================
    # NOTES COMMANDS
    # ===================================================================

    @bot.tree.command(name="notes", description="View and manage notes for a project")
    async def notes_command(interaction: discord.Interaction):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        result = await handler.execute("list_notes", {"project_id": project_id})
        if "error" in result:
            await _send_error(interaction, result['error'])
            return

        # Look up project name for the header
        projects_result = await handler.execute("list_projects", {})
        project_name = project_id
        for p in projects_result.get("projects", []):
            if p["id"] == project_id:
                project_name = p["name"]
                break

        notes = result.get("notes", [])
        view = NotesView(project_id, notes, handler=handler, bot=bot)
        await interaction.response.send_message(
            view.build_content(), view=view,
        )

        # Create a thread for interactive note management
        msg = await interaction.original_response()
        thread = await msg.create_thread(
            name=f"Notes: {project_name}"[:100],
            auto_archive_duration=1440,
        )
        await thread.send(
            f"💬 Type ideas and I'll organize them into notes for **{project_name}**.\n"
            f"Click a note button above to view its content."
        )
        bot.register_notes_thread(thread.id, project_id)

        # Track TOC message for view persistence
        toc_messages = getattr(bot, "_notes_toc_messages", {})
        toc_messages[thread.id] = msg.id
        bot._notes_toc_messages = toc_messages
        bot._save_notes_threads()

    @bot.tree.command(name="write-note", description="Create or update a project note")
    @app_commands.describe(
        title="Note title",
        content="Note content (markdown)",
    )
    async def write_note_command(
        interaction: discord.Interaction,
        title: str,
        content: str,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        result = await handler.execute("write_note", {
            "project_id": project_id,
            "title": title,
            "content": content,
        })
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        status = result.get("status", "created")
        await _send_success(
            interaction, f"Note {status.title()}",
            description=f"Note **{title}** {status} in `{project_id}`",
        )

    @bot.tree.command(name="delete-note", description="Delete a project note")
    @app_commands.describe(
        title="Note title",
    )
    async def delete_note_command(
        interaction: discord.Interaction,
        title: str,
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return
        result = await handler.execute("delete_note", {
            "project_id": project_id,
            "title": title,
        })
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        await _send_success(
            interaction, "Note Deleted",
            description=f"Note **{title}** deleted from `{project_id}`",
        )

    # ===================================================================
    # MEMORY COMMANDS
    # ===================================================================

    @bot.tree.command(name="memory-stats", description="Show memory index statistics for a project")
    @app_commands.describe(
        project="Project ID (defaults to the project linked to this channel)",
    )
    async def memory_stats_command(
        interaction: discord.Interaction,
        project: str | None = None,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return

        result = await handler.execute("memory_stats", {"project_id": project_id})
        if "error" in result:
            await _send_error(interaction, result["error"])
            return

        enabled = result.get("enabled", False)
        available = result.get("available", False)

        if not enabled:
            await _send_info(
                interaction, "Memory Disabled",
                description=(
                    f"Memory is **not enabled** for `{project_id}`.\n"
                    f"memsearch installed: {'Yes' if available else 'No'}\n\n"
                    "Set `memory.enabled = true` in your config to enable."
                ),
            )
            return

        if not available:
            await _send_warning(
                interaction, "Memory Unavailable",
                description=(
                    f"Memory is enabled for `{project_id}` but the "
                    "`memsearch` package is not installed."
                ),
            )
            return

        fields = [
            ("Collection", f"`{result.get('collection', 'N/A')}`", True),
            ("Embedding Provider", f"`{result.get('embedding_provider', 'N/A')}`", True),
            ("Milvus URI", f"`{result.get('milvus_uri', 'N/A')}`", False),
            ("Auto Recall", "Enabled" if result.get("auto_recall") else "Disabled", True),
            ("Auto Remember", "Enabled" if result.get("auto_remember") else "Disabled", True),
            ("Recall Top-K", str(result.get("recall_top_k", "N/A")), True),
        ]

        await _send_success(
            interaction, f"Memory Stats — {project_id}",
            description="Memory subsystem is **active** and operational.",
            fields=fields,
        )

    @bot.tree.command(name="memory-search", description="Semantic search across project memory")
    @app_commands.describe(
        query="Semantic search query",
        project="Project ID (defaults to the project linked to this channel)",
        top_k="Number of results to return (default: 5)",
    )
    async def memory_search_command(
        interaction: discord.Interaction,
        query: str,
        project: str | None = None,
        top_k: int = 5,
    ):
        project_id = await _resolve_project_from_context(interaction, project)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
            return

        await interaction.response.defer()

        result = await handler.execute("memory_search", {
            "project_id": project_id,
            "query": query,
            "top_k": top_k,
        })
        if "error" in result:
            await _send_error(interaction, result["error"], followup=True)
            return

        results = result.get("results", [])
        count = result.get("count", 0)

        if count == 0:
            await _send_info(
                interaction, "No Results",
                description=f"No memories matched your query in `{project_id}`.\n\n**Query:** {query}",
                followup=True,
            )
            return

        # Build result entries for the embed description
        desc_parts = [f"**Query:** {query}", f"**Project:** `{project_id}`", ""]
        for r in results:
            score = r.get("score", 0)
            source = r.get("source", "unknown")
            heading = r.get("heading", "")
            content = r.get("content", "")

            # Truncate content preview
            preview = content.replace("\n", " ").strip()
            if len(preview) > 200:
                preview = preview[:197] + "..."

            # Format source — show just the filename
            source_short = source.rsplit("/", 1)[-1] if "/" in source else source

            rank = r.get("rank", "?")
            score_pct = f"{score * 100:.1f}%" if isinstance(score, (int, float)) else "N/A"
            entry = (
                f"**{rank}.** `{source_short}` — {score_pct}\n"
            )
            if heading:
                entry += f"> **{heading}**\n"
            entry += f"> {preview}"
            desc_parts.append(entry)

        description = "\n\n".join(desc_parts)
        # Truncate to Discord limit if needed
        if len(description) > 4000:
            description = description[:3997] + "..."

        embed = info_embed(
            f"Memory Search — {count} result{'s' if count != 1 else ''}",
            description=description,
        )
        await interaction.followup.send(embed=embed)

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
            await _send_info(
                interaction, "Orchestrator Paused",
                description="No new tasks will be scheduled.",
            )
        elif action.value == "resume":
            await _send_success(
                interaction, "Orchestrator Resumed",
                description="Task scheduling is now active.",
            )
        else:
            running = result.get("running_tasks", 0)
            is_paused = result.get("status") == "paused"
            state = "⏸ PAUSED" if is_paused else "▶ RUNNING"
            embed_fn = _send_info if is_paused else _send_success
            await embed_fn(
                interaction, f"Orchestrator {state}",
                description=f"**Running tasks:** {running}",
            )

    @bot.tree.command(name="restart", description="Restart the agent-queue daemon")
    async def restart_command(interaction: discord.Interaction):
        # Gather git info for the restart message
        git_info_parts: list[str] = []
        try:
            short_hash = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if short_hash:
                git_info_parts.append(f"commit `{short_hash}`")
        except Exception:
            pass
        try:
            subprocess.run(
                ["git", "fetch", "--quiet"],
                capture_output=True, text=True, timeout=15,
            )
            behind = subprocess.run(
                ["git", "rev-list", "--count", "HEAD..@{u}"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if behind and int(behind) > 0:
                git_info_parts.append(f"**{behind}** commit{'s' if int(behind) != 1 else ''} behind origin")
        except Exception:
            pass

        desc = "Agent-queue daemon is restarting…"
        if git_info_parts:
            desc += "\n" + " · ".join(git_info_parts)

        await _send_warning(interaction, "Restarting", description=desc)
        bot._restart_requested = True
        await handler.execute("restart_daemon", {})

    # ===================================================================
    # INTERACTIVE MENU
    # ===================================================================

    @bot.tree.command(
        name="menu",
        description="Show an interactive control panel with clickable buttons",
    )
    async def menu_command(interaction: discord.Interaction):
        # Build a quick status summary for the menu message
        result = await handler.execute("get_status", {})
        tasks = result.get("tasks", {})
        by_status = tasks.get("by_status", {})
        total = tasks.get("total", 0)
        completed = by_status.get("COMPLETED", 0)
        in_progress = (
            by_status.get("IN_PROGRESS", 0) + by_status.get("ASSIGNED", 0)
        )
        failed = by_status.get("FAILED", 0)
        blocked = by_status.get("BLOCKED", 0)
        ready = by_status.get("READY", 0)

        agents = result.get("agents", [])
        busy_count = sum(1 for a in agents if a.get("state") == "BUSY")

        lines = ["## 🎛️ Agent Queue — Control Panel"]
        if result.get("orchestrator_paused"):
            lines.append("⏸ **Orchestrator is PAUSED**")
        else:
            lines.append("▶ Orchestrator running")

        bar = progress_bar(completed, total, width=12) if total > 0 else "—"
        lines.append(f"**Progress:** {bar}")
        lines.append(
            f"📊 {total} tasks — {in_progress} active, {ready} ready, "
            f"{failed} failed, {blocked} blocked"
        )
        lines.append(
            f"🤖 {len(agents)} agents — {busy_count} busy"
        )
        lines.append(
            "\n_Use the buttons below to view details and take actions._"
        )

        view = MenuView(handler=handler, bot=bot)
        await interaction.response.send_message(
            "\n".join(lines), view=view
        )
