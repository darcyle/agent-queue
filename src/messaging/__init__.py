"""Messaging adapter abstraction layer.

Provides a platform-agnostic interface for the orchestrator to send
notifications and interact with users, whether via Discord, Telegram,
or any future messaging platform.
"""

from src.messaging.base import MessagingAdapter
from src.messaging.factory import create_messaging_adapter

__all__ = ["MessagingAdapter", "create_messaging_adapter"]
