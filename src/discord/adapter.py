"""Discord implementation of the MessagingAdapter interface.

This is a thin wrapper around the existing ``AgentQueueBot`` — all Discord-specific
logic remains in ``bot.py``.  The adapter simply delegates to the bot's methods,
providing a platform-agnostic interface for ``main.py`` and the orchestrator.
"""

from __future__ import annotations

from typing import Any

from src.config import AppConfig
from src.discord.bot import AgentQueueBot
from src.messaging.base import MessagingAdapter
from src.orchestrator import Orchestrator


class DiscordMessagingAdapter(MessagingAdapter):
    """Wraps ``AgentQueueBot`` to implement the ``MessagingAdapter`` ABC.

    The bot retains all its existing behavior — this adapter just exposes it
    through the standard interface so ``main.py`` can treat all messaging
    platforms uniformly.
    """

    def __init__(self, config: AppConfig, orchestrator: Orchestrator) -> None:
        self._bot = AgentQueueBot(config, orchestrator)
        self._config = config

    @property
    def bot(self) -> AgentQueueBot:
        """Direct access to the underlying bot for Discord-specific needs."""
        return self._bot

    async def start(self) -> None:
        """Connect the Discord bot to the gateway."""
        await self._bot.start(self._config.discord.bot_token)

    async def wait_until_ready(self) -> None:
        """Wait until the Discord bot has connected and cached guilds."""
        await self._bot.wait_until_ready()

    async def close(self) -> None:
        """Disconnect from Discord gracefully."""
        await self._bot.close()

    async def send_message(
        self,
        text: str,
        project_id: str | None = None,
        *,
        embed: Any = None,
        view: Any = None,
    ) -> Any:
        """Send a message via the Discord bot."""
        return await self._bot._send_message(text, project_id, embed=embed, view=view)

    async def create_task_thread(
        self,
        thread_name: str,
        initial_message: str,
        project_id: str | None = None,
        task_id: str | None = None,
    ) -> Any:
        """Create a Discord thread for task output streaming."""
        return await self._bot._create_task_thread(
            thread_name, initial_message, project_id=project_id, task_id=task_id
        )

    def get_command_handler(self) -> Any:
        """Return the command handler from the bot's Supervisor."""
        return self._bot.agent.handler

    def get_supervisor(self) -> Any:
        """Return the bot's Supervisor instance."""
        return self._bot.agent
