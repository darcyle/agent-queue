"""Platform-agnostic messaging abstraction layer.

Provides the ``MessagingPort`` ABC that both Discord and Telegram transports
implement, plus the ``RichNotification`` / ``NotificationAction`` types used
to describe rich messages without coupling to any specific chat platform.

Typical usage::

    from src.messaging import MessagingPort, RichNotification, NotificationAction

See ``src/messaging/port.py`` for the full transport contract.
"""

from src.messaging.port import MessagingPort, RichNotification, NotificationAction
from src.messaging.types import (
    NotifyCallback,
    ThreadSendCallback,
    CreateThreadCallback,
)

__all__ = [
    "MessagingPort",
    "RichNotification",
    "NotificationAction",
    "NotifyCallback",
    "ThreadSendCallback",
    "CreateThreadCallback",
]
