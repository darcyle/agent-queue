"""Service layer for chat analyzer suggestion lifecycle.

Encapsulates the logic for posting, accepting, and dismissing chat analyzer
suggestions. The Discord bot delegates to this service when handling
suggestion interactions.

The service bridges the ``ChatAnalyzer`` (which produces suggestions) and the
Discord UI (``ChatAnalyzerSuggestionView`` buttons), keeping interaction logic
out of both the bot and the analyzer.
"""
from __future__ import annotations

import logging
from typing import Any

import discord

from src.database import Database
from src.discord.notifications import (
    ChatAnalyzerSuggestionView,
    format_analyzer_suggestion_embed,
    _SUGGESTION_TYPE_COLORS,
    truncate,
    LIMIT_DESCRIPTION,
)

logger = logging.getLogger(__name__)


class ChatAnalyzerService:
    """Manages the suggestion lifecycle: post → accept/dismiss.

    Parameters
    ----------
    db:
        Database instance for reading/writing suggestion records.
    handler:
        ``CommandHandler`` instance for executing actions (create_task, etc.).
    bot:
        The Discord bot instance, used to resolve channels by ID.
    """

    def __init__(self, db: Database, handler: Any = None, bot: Any = None) -> None:
        self._db = db
        self._handler = handler
        self._bot = bot

    async def post_suggestion(
        self,
        channel_id: int,
        project_id: str,
        suggestion_id: int,
        suggestion_type: str,
        suggestion_text: str,
        task_title: str = "",
        confidence: float = 0.0,
    ) -> discord.Message | None:
        """Post a suggestion embed with Accept/Dismiss buttons to a channel.

        Creates the embed via ``format_analyzer_suggestion_embed()``, attaches
        a ``ChatAnalyzerSuggestionView`` for user interaction, and sends the
        message. The suggestion record should already be inserted in the DB
        by the ``ChatAnalyzer`` before calling this method.

        Returns the sent message, or None if the channel couldn't be resolved.
        """
        if not self._bot:
            logger.warning("ChatAnalyzerService: bot not set, cannot post suggestion")
            return None

        channel = self._bot.get_channel(channel_id)
        if channel is None:
            logger.warning(
                "ChatAnalyzerService: channel %d not found", channel_id
            )
            return None

        embed = format_analyzer_suggestion_embed(
            suggestion_type=suggestion_type,
            suggestion_text=suggestion_text,
            project_id=project_id,
            confidence=confidence,
        )

        view = ChatAnalyzerSuggestionView(
            suggestion_id=suggestion_id,
            suggestion_type=suggestion_type,
            suggestion_text=suggestion_text,
            project_id=project_id,
            task_title=task_title,
            handler=self._handler,
            db=self._db,
        )

        try:
            msg = await channel.send(embed=embed, view=view)
            logger.info(
                "ChatAnalyzerService: posted %s suggestion #%d to channel %d",
                suggestion_type, suggestion_id, channel_id,
            )
            return msg
        except Exception as e:
            logger.error(
                "ChatAnalyzerService: failed to send suggestion: %s", e
            )
            return None

    async def on_accept(
        self,
        suggestion_id: int,
        interaction: discord.Interaction,
        suggestion_type: str,
        suggestion_text: str,
        project_id: str,
        task_title: str = "",
    ) -> None:
        """Handle acceptance of a suggestion.

        Behaviour depends on the suggestion type:
        - ``answer``: post the suggestion text as a regular bot message
        - ``task``: create a task via ``CommandHandler``
        - ``context``: post an informational embed with the relevant context
        - ``warning``: acknowledge and offer to create a task

        Updates the DB status to ``"accepted"``.
        """
        # Record acceptance
        try:
            await self._db.resolve_chat_analyzer_suggestion(
                suggestion_id, "accepted"
            )
        except Exception as e:
            logger.error("Could not record suggestion acceptance: %s", e)

        if suggestion_type == "answer":
            await interaction.channel.send(
                f"\U0001f4a1 **Suggested answer:**\n{suggestion_text}"
            )
            await interaction.followup.send("Suggestion accepted!", ephemeral=True)

        elif suggestion_type == "task" and self._handler:
            title = task_title or suggestion_text[:80]
            result = await self._handler.execute("create_task", {
                "project_id": project_id,
                "title": title,
                "description": suggestion_text,
            })
            if "error" in result:
                await interaction.followup.send(
                    f"Could not create task: {result['error']}", ephemeral=True
                )
            else:
                task_id = result.get("task_id", "?")
                await interaction.followup.send(
                    f"\U0001f4cb Task `{task_id}` created: {title}",
                    ephemeral=True,
                )

        elif suggestion_type == "context":
            context_embed = discord.Embed(
                title="\U0001f4ce Relevant Context",
                description=truncate(suggestion_text, LIMIT_DESCRIPTION),
                color=_SUGGESTION_TYPE_COLORS.get("context", 0xF1C40F),
            )
            context_embed.set_footer(text=f"Project: {project_id}")
            await interaction.channel.send(embed=context_embed)
            await interaction.followup.send("Context posted!", ephemeral=True)

        elif suggestion_type == "warning":
            await interaction.channel.send(
                f"\u26a0\ufe0f **Heads up:**\n{suggestion_text}"
            )
            await interaction.followup.send(
                "Warning acknowledged! Use `/create-task` if you'd like to "
                "create a task to address this.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send("Suggestion accepted.", ephemeral=True)

    async def on_dismiss(
        self,
        suggestion_id: int,
        interaction: discord.Interaction,
    ) -> None:
        """Handle dismissal of a suggestion.

        Updates the DB status to ``"dismissed"`` (which records the timestamp
        for cooldown logic) and edits the original message to show a greyed-out
        "Suggestion dismissed" state.
        """
        # Record dismissal — the resolved_at timestamp is used by the
        # ChatAnalyzer's cooldown logic to avoid re-suggesting too soon
        try:
            await self._db.resolve_chat_analyzer_suggestion(
                suggestion_id, "dismissed"
            )
        except Exception as e:
            logger.error("Could not record suggestion dismissal: %s", e)

        await interaction.followup.send("Suggestion dismissed.", ephemeral=True)

    async def rehydrate_view(
        self,
        suggestion_id: int,
    ) -> ChatAnalyzerSuggestionView | None:
        """Recreate a suggestion view from the database for persistence.

        Called on bot startup to re-register persistent views for pending
        suggestions. Returns None if the suggestion is already resolved or
        not found.
        """
        suggestion = await self._db.get_suggestion(suggestion_id)
        if suggestion is None:
            return None
        if suggestion.get("status") != "pending":
            return None

        return ChatAnalyzerSuggestionView(
            suggestion_id=suggestion_id,
            suggestion_type=suggestion.get("suggestion_type", "context"),
            suggestion_text=suggestion.get("suggestion_text", ""),
            project_id=suggestion.get("project_id", ""),
            task_title="",
            handler=self._handler,
            db=self._db,
        )
