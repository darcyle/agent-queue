"""Telegram messaging integration for AgentQueue.

Provides:
- ``TelegramBot`` — core bot that connects to the Telegram Bot API,
  routes messages, and manages per-chat conversations.
- ``TelegramMessagingAdapter`` — implements ``MessagingAdapter`` ABC
  so the orchestrator and ``main.py`` can use Telegram transparently.
"""

from src.telegram.adapter import TelegramMessagingAdapter
from src.telegram.bot import TelegramBot

__all__ = ["TelegramBot", "TelegramMessagingAdapter"]
