"""Abstract base class for messaging platform adapters.

Each supported messaging platform (Discord, Telegram, etc.) implements this
interface so the orchestrator and main entry point can work with any platform
without knowing platform-specific details.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class MessagingAdapter(ABC):
    """Platform-agnostic interface for messaging integrations.

    The orchestrator interacts with the messaging layer exclusively through
    this interface.  Concrete implementations wrap platform-specific SDKs
    (e.g. discord.py, python-telegram-bot).
    """

    @abstractmethod
    async def start(self) -> None:
        """Connect to the messaging platform and begin processing events."""

    @abstractmethod
    async def wait_until_ready(self) -> None:
        """Block until the adapter is fully connected and ready to send messages."""

    @abstractmethod
    async def close(self) -> None:
        """Gracefully disconnect from the messaging platform."""

    @abstractmethod
    async def send_message(
        self,
        text: str,
        project_id: str | None = None,
        *,
        embed: Any = None,
        view: Any = None,
    ) -> Any:
        """Send a notification message, optionally scoped to a project.

        Parameters
        ----------
        text:
            Plain-text message content.
        project_id:
            If provided, route the message to the project's channel/chat.
        embed:
            Platform-specific rich embed object (e.g. ``discord.Embed``).
        view:
            Platform-specific interactive component (e.g. ``discord.ui.View``).

        Returns
        -------
        The platform-specific message object, or ``None`` if no channel was
        available.
        """

    @abstractmethod
    async def create_task_thread(
        self,
        thread_name: str,
        initial_message: str,
        project_id: str | None = None,
        task_id: str | None = None,
    ) -> Any:
        """Create a thread/topic for streaming task output.

        Returns platform-specific callback(s) for sending messages into the
        thread, or ``None`` if no channel is available.
        """

    @abstractmethod
    def get_command_handler(self) -> Any:
        """Return the command handler for interactive views (buttons, etc.)."""

    @abstractmethod
    def get_supervisor(self) -> Any:
        """Return the supervisor/chat agent for post-task delegation."""
