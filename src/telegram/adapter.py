"""Telegram messaging adapter — wraps TelegramBot to implement MessagingAdapter.

This is the thin adapter layer that the orchestrator and ``main.py`` interact
with.  All Telegram-specific logic lives in ``TelegramBot``; this class simply
delegates the seven ``MessagingAdapter`` methods to the underlying bot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.messaging.base import MessagingAdapter

if TYPE_CHECKING:
    from src.config import AppConfig
    from src.messaging.types import ThreadSendCallback
    from src.orchestrator import Orchestrator


class TelegramMessagingAdapter(MessagingAdapter):
    """Adapter that wraps ``TelegramBot`` to implement ``MessagingAdapter``.

    Usage::

        adapter = TelegramMessagingAdapter(config, orchestrator)
        await adapter.start()
        await adapter.wait_until_ready()
        # ... orchestrator runs ...
        await adapter.close()
    """

    def __init__(self, config: "AppConfig", orchestrator: "Orchestrator") -> None:
        from src.telegram.bot import TelegramBot

        self._bot = TelegramBot(config, orchestrator)
        self._config = config

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------

    async def start(self) -> None:
        """Connect to Telegram and begin polling for updates."""
        await self._bot.start()

    async def wait_until_ready(self) -> None:
        """Block until the Telegram connection is established."""
        await self._bot.wait_until_ready()

    async def close(self) -> None:
        """Disconnect from Telegram gracefully."""
        await self._bot.stop()

    # -------------------------------------------------------------------
    # Messaging
    # -------------------------------------------------------------------

    async def send_message(
        self,
        text: str,
        project_id: str | None = None,
        *,
        embed: Any = None,
        view: Any = None,
    ) -> None:
        """Send a notification to the appropriate Telegram chat."""
        await self._bot.send_notification(text, project_id, embed=embed, view=view)

    async def create_task_thread(
        self,
        task: Any,
        project: Any,
    ) -> tuple["ThreadSendCallback", "ThreadSendCallback"]:
        """Create a forum topic or reply chain for task output.

        Returns ``(send_to_thread, notify_main_channel)`` callback pair.
        """
        task_title = getattr(task, "title", None) or getattr(task, "id", "task")
        project_id = getattr(project, "id", None)
        task_id = getattr(task, "id", None)
        thread_name = str(task_title)[:128]
        initial_message = f"Agent working on: {task_title}"

        result = await self._bot.create_task_topic(
            thread_name, initial_message, project_id, task_id
        )
        if result is None:
            # Return no-op callbacks if topic creation failed
            async def noop(text: str) -> None:
                pass
            return noop, noop
        return result

    # -------------------------------------------------------------------
    # Component access
    # -------------------------------------------------------------------

    def get_command_handler(self) -> Any:
        """Return the CommandHandler wired to the Telegram bot."""
        return self._bot.handler

    def get_supervisor(self) -> Any:
        """Return the Supervisor wired to the Telegram bot."""
        return self._bot.supervisor
