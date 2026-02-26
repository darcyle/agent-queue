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


class NoteContentView(discord.ui.View):
    """View attached to a note content message with Dismiss and Delete buttons."""

    def __init__(self, project_id: str, note_slug: str, handler=None, bot=None) -> None:
        super().__init__(timeout=None)
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
            self._children: dict[str, list] = {}  # parent_id → [child tasks]
            for t in self._all_tasks:
                pid = t.get("parent_task_id")
                if pid:
                    self._subtask_ids.add(t["id"])
                    self._children.setdefault(pid, []).append(t)
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
            if task["id"] in self._children:
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
            children = self._children.get(t["id"], [])
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
            # Add progress summary at top
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
                            tag = self._get_type_tag(t)
                            tag_str = f"{tag} " if tag else ""
                            lines.append(f"{tag_str}**{t['title']}** `{t['id']}`")
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
        lines.append(
            f"**Tasks:** {total} total — "
            f"{by_status.get('DEFINED', 0)} pending, "
            f"{in_progress} active, "
            f"{by_status.get('READY', 0)} ready, "
            f"{completed} completed, "
            f"{failed} failed, "
            f"{by_status.get('PAUSED', 0)} paused"
        )
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

        Uses the ``create_channel_for_project`` command handler for the actual
        creation, which is idempotent (reuses existing channels with matching
        names).

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

        # Delegate to command handler for project linking (idempotent)
        link_result = await cmd_handler.execute("create_channel_for_project", {
            "project_id": project_id,
            "channel_name": channel_name,
            "guild_channels": guild_channels,
            "_created_channel_id": created_channel_id,
        })

        if "error" in link_result:
            results.append({
                "channel_name": channel_name,
                "display": f"⚠️ #{channel_name} ({link_result['error']})",
            })
        else:
            action = link_result.get("action", "linked")
            ch_id = link_result.get("channel_id", "")
            mention = f"<#{ch_id}>" if ch_id else f"#{channel_name}"
            label = "linked" if action == "linked_existing" else "created"
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
        auto_create_channels="Auto-create Discord channels for this project (overrides config)",
    )
    async def create_project_command(
        interaction: discord.Interaction,
        name: str,
        credit_weight: float = 1.0,
        max_concurrent_agents: int = 2,
        auto_create_channels: bool | None = None,
    ):
        # Build args for the command handler.  An explicit
        # ``auto_create_channels`` parameter overrides the global config.
        create_args: dict = {
            "name": name,
            "credit_weight": credit_weight,
            "max_concurrent_agents": max_concurrent_agents,
        }
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
            await _send_error(interaction, result['error'])
            return
        fields = ", ".join(result.get("fields", []))
        await _send_success(
            interaction, "Project Updated",
            description=f"Project `{project_id}` updated: {fields}",
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

    @bot.tree.command(
        name="set-channel",
        description="Link a Discord channel to a project",
    )
    @app_commands.describe(
        project_id="Project ID",
        channel="The Discord channel to link",
    )
    async def set_channel_command(
        interaction: discord.Interaction,
        project_id: str,
        channel: discord.TextChannel,
    ):
        result = await handler.execute("set_project_channel", {
            "project_id": project_id,
            "channel_id": str(channel.id),
        })
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        # Update the bot's channel cache immediately
        bot.update_project_channel(project_id, channel)
        await _send_success(
            interaction, "Channel Linked",
            description=f"Project `{project_id}` channel set to {channel.mention}",
        )

    @bot.tree.command(
        name="set-control-interface",
        description="Set a project's channel by name (alias for /set-channel)",
    )
    @app_commands.describe(
        project_id="Project ID",
        channel_name="Name of the Discord channel to link",
    )
    async def set_control_interface_command(
        interaction: discord.Interaction,
        project_id: str,
        channel_name: str,
    ):
        guild = interaction.guild
        if not guild:
            await _send_error(interaction, "Not in a guild.")
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
            await _send_error(interaction, f"No text channel named `{channel_name}` found in this server.")
            return

        result = await handler.execute("set_control_interface", {
            "project_id": project_id,
            "channel_name": channel_name,
            "_resolved_channel_id": str(matched_channel.id),
        })
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        # Update the bot's channel cache immediately
        bot.update_project_channel(project_id, matched_channel)
        await _send_success(
            interaction, "Channel Linked",
            description=f"Project `{project_id}` channel set to {matched_channel.mention}",
        )

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
        name="create-channel-for-project",
        description="Create (or reuse) a dedicated Discord channel for a project (idempotent)",
    )
    @app_commands.describe(
        project_id="Project ID",
        channel_name="Name for the channel (defaults to project ID)",
        category="Category to create the channel in (optional)",
    )
    async def create_channel_for_project_command(
        interaction: discord.Interaction,
        project_id: str,
        channel_name: str | None = None,
        category: discord.CategoryChannel | None = None,
    ):
        await interaction.response.defer()
        name = channel_name or project_id

        guild = interaction.guild
        if not guild:
            await _send_error(interaction, "Not in a guild.", followup=True)
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
            topic = f"Agent Queue channel for project: {project_id}"
            ppc = bot.config.discord.per_project_channels
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
            try:
                existing_channel = await guild.create_text_channel(
                    name=name,
                    category=category,
                    topic=topic,
                    reason=f"AgentQueue: channel for project {project_id}",
                    overwrites=overwrites or discord.utils.MISSING,
                )
                created_channel_id = str(existing_channel.id)
            except discord.Forbidden:
                await _send_error(interaction, "Bot lacks permission to create channels.", followup=True)
                return
            except discord.HTTPException as e:
                await _send_error(interaction, f"Error creating channel: {e}", followup=True)
                return

        # Delegate to the command handler for project validation + linking
        result = await handler.execute("create_channel_for_project", {
            "project_id": project_id,
            "channel_name": name,
            "guild_channels": guild_channels,
            "_created_channel_id": created_channel_id,
        })

        if "error" in result:
            await _send_error(interaction, result['error'], followup=True)
            return

        # Update the bot's channel cache immediately
        bot.update_project_channel(project_id, existing_channel)

        action = result.get("action", "linked")
        if action == "linked_existing":
            await _send_success(
                interaction, "Channel Linked",
                description=(
                    f"Channel {existing_channel.mention} already exists — "
                    f"linked to project `{project_id}`"
                ),
                followup=True,
            )
        else:
            await _send_success(
                interaction, "Channel Created",
                description=(
                    f"Created {existing_channel.mention} as channel "
                    f"for project `{project_id}`"
                ),
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
        view_mode = view.value if view else "list"
        project_id = await _resolve_project_from_context(interaction, None)
        args: dict = {"include_completed": show_completed}
        if project_id:
            args["project_id"] = project_id
        # Delegate active/completed filtering to CommandHandler
        args["include_completed"] = show_completed
        # Map the view choice to the command handler display_mode parameter.
        # "list" uses "flat" (the default interactive grouped view handled here).
        if view_mode in ("tree", "compact"):
            args["display_mode"] = view_mode
        result = await handler.execute("list_tasks", args)
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
            await interaction.followup.send(
                embed=info_embed("No Tasks", description="No tasks found for this project."),
            )
            return

        # ----- Tree / compact display modes --------------------------------
        if view_mode in ("tree", "compact"):
            display_text = result.get("display", "")
            total = result.get("total", len(tasks))
            title = "Task Tree" if view_mode == "tree" else "Tasks (Compact)"

            # Tree mode uses a code block for monospace alignment;
            # compact mode uses regular markdown.
            if view_mode == "tree":
                # Strip Discord markdown (bold **) for monospace code blocks
                # since they don't render inside ```.
                clean = re.sub(r"\*\*(.+?)\*\*", r"\1", display_text)
                formatted = f"```\n{clean}\n```"
            else:
                formatted = display_text

            # Paginate: if the output exceeds Discord's 4096-char embed
            # description limit, split into multiple embeds.
            _EMBED_DESC_LIMIT = 4000  # leave headroom below the 4096 hard cap

            if len(formatted) <= _EMBED_DESC_LIMIT:
                embed = info_embed(
                    title,
                    description=f"{formatted}\n\n**Total:** {total} task(s)",
                )
                await interaction.followup.send(embed=embed)
            else:
                # Split the display text into chunks, respecting line
                # boundaries so we don't break mid-task.
                chunks = _split_display_text(formatted, _EMBED_DESC_LIMIT)
                # First chunk goes as the initial response
                first_embed = info_embed(
                    title,
                    description=chunks[0],
                )
                await interaction.followup.send(embed=first_embed)
                # Remaining chunks sent as followup messages
                for i, chunk in enumerate(chunks[1:], start=2):
                    followup_embed = info_embed(
                        f"{title} (page {i}/{len(chunks)})",
                        description=chunk,
                    )
                    await interaction.followup.send(embed=followup_embed)
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
                {**args, "include_completed": True},
            )
            all_tasks = all_result.get("tasks", [])
        else:
            all_tasks = tasks
        view_widget = TaskReportView(tasks_by_status, total, all_tasks=all_tasks)
        content = view_widget.build_content()
        await interaction.followup.send(content, view=view_widget)

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
            or "quick-tasks"
        )
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
            await _send_error(interaction, result['error'])
            return
        fields = ", ".join(result.get("fields", []))
        await _send_success(
            interaction, "Task Updated",
            description=f"Task `{task_id}` updated: {fields}",
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
            await _send_error(interaction, result['error'])
            return
        old_status = result.get("old_status", "?")
        new_status = result.get("new_status", status.value)
        old_emoji = STATUS_EMOJIS.get(old_status, "❓")
        new_emoji = STATUS_EMOJIS.get(new_status, "❓")
        embed = status_embed(
            new_status,
            "Task Status Updated",
            fields=[
                ("Task", f"`{task_id}` — {result.get('title', '')}", False),
                ("Status Change", f"{old_emoji} **{old_status}** → {new_emoji} **{new_status}**", False),
            ],
        )
        await interaction.response.send_message(embed=embed)

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
        repo_id="Repository ID to assign as workspace (optional)",
        project_id="Project to set workspace for (activates agent immediately)",
        workspace_path="Workspace directory path (requires project_id)",
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
        repo_id: str | None = None,
        project_id: str | None = None,
        workspace_path: str | None = None,
    ):
        args: dict = {}
        if name:
            args["name"] = name
        if agent_type:
            args["agent_type"] = agent_type.value
        if repo_id:
            args["repo_id"] = repo_id
        if project_id:
            args["project_id"] = project_id
        if workspace_path:
            args["workspace_path"] = workspace_path
        result = await handler.execute("create_agent", args)
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        agent_fields: list[tuple[str, str, bool]] = [
            ("Name", result.get("name", name), True),
            ("ID", f"`{result['created']}`", True),
            ("Type", args.get("agent_type", "claude"), True),
            ("State", result.get("state", "STARTING"), True),
        ]
        if repo_id:
            agent_fields.append(("Repo", f"`{repo_id}`", True))
        if result.get("workspace_path"):
            agent_fields.append(("Workspace", f"`{result['workspace_path']}`", False))
        embed = success_embed("Agent Registered", fields=agent_fields)
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(
        name="activate-agent",
        description="Activate a STARTING agent so it can receive tasks",
    )
    @app_commands.describe(agent_id="Agent ID to activate")
    async def activate_agent_command(
        interaction: discord.Interaction,
        agent_id: str,
    ):
        result = await handler.execute("activate_agent", {"agent_id": agent_id})
        if "error" in result:
            await _send_error(interaction, result['error'])
            return
        embed = success_embed(
            "Agent Activated",
            description=f"Agent `{agent_id}` is now IDLE and ready to receive tasks.",
        )
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
    # REPO COMMANDS
    # ===================================================================

    @bot.tree.command(name="repos", description="List registered repositories")
    async def repos_command(interaction: discord.Interaction):
        project_id = await _resolve_project_from_context(interaction, None)
        args = {}
        if project_id:
            args["project_id"] = project_id
        result = await handler.execute("list_repos", args)
        repos = result.get("repos", [])
        if not repos:
            await _send_info(interaction, "No Repositories", description="No repositories registered.")
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
        url: str | None = None,
        path: str | None = None,
        name: str | None = None,
        default_branch: str = "main",
    ):
        project_id = await _resolve_project_from_context(interaction, None)
        if not project_id:
            await _send_error(interaction, _NO_PROJECT_MSG)
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
            await _send_error(interaction, result['error'])
            return
        embed = success_embed(
            "Repo Registered",
            fields=[
                ("ID", f"`{result['created']}`", True),
                ("Source", source.value, True),
                ("Project", f"`{project_id}`", True),
            ],
        )
        await interaction.response.send_message(embed=embed)

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
        await _send_warning(interaction, "Restarting", description="Agent-queue daemon is restarting…")
        bot._restart_requested = True
        await handler.execute("restart_daemon", {})
