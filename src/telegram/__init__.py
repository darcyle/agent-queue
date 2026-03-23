"""Telegram messaging integration for AgentQueue.

Provides:
- ``TelegramBot`` — core bot that connects to the Telegram Bot API,
  routes messages, and manages per-chat conversations.
- ``TelegramMessagingAdapter`` — implements ``MessagingAdapter`` ABC
  so the orchestrator and ``main.py`` can use Telegram transparently.
"""

from src.telegram.adapter import TelegramMessagingAdapter
from src.telegram.bot import TelegramBot
from src.telegram.views import (
    agent_question_keyboard,
    plan_approval_keyboard,
    task_approval_keyboard,
    task_blocked_keyboard,
    task_failed_keyboard,
    task_started_keyboard,
)

__all__ = [
    "TelegramBot",
    "TelegramMessagingAdapter",
    "agent_question_keyboard",
    "plan_approval_keyboard",
    "task_approval_keyboard",
    "task_blocked_keyboard",
    "task_failed_keyboard",
    "task_started_keyboard",
]
