"""Factory for creating the appropriate messaging adapter based on config.

Uses lazy imports so that platform-specific dependencies (discord.py,
python-telegram-bot, etc.) are only loaded when actually needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import AppConfig
    from src.messaging.base import MessagingAdapter
    from src.orchestrator import Orchestrator


def create_messaging_adapter(
    config: "AppConfig", orchestrator: "Orchestrator"
) -> "MessagingAdapter":
    """Create a messaging adapter for the configured platform.

    Parameters
    ----------
    config:
        Application configuration — ``config.messaging_platform`` determines
        which adapter is instantiated.
    orchestrator:
        The orchestrator instance the adapter will integrate with.

    Returns
    -------
    A concrete ``MessagingAdapter`` implementation.

    Raises
    ------
    ValueError
        If the configured platform is not supported.
    """
    platform = getattr(config, "messaging_platform", "discord")

    if platform == "discord":
        from src.discord.adapter import DiscordMessagingAdapter

        return DiscordMessagingAdapter(config, orchestrator)
    elif platform == "telegram":
        raise NotImplementedError(
            "Telegram adapter is not yet implemented. "
            "Set messaging_platform to 'discord' in your config."
        )
    else:
        raise ValueError(
            f"Unsupported messaging platform: {platform!r}. "
            f"Supported platforms: discord, telegram"
        )
