"""Centralized Discord embed factory for AgentQueue.

This module is the single source of truth for all embed creation, semantic
colors, task-status visual mappings, and text-safety utilities.  Every embed
sent by the bot -- whether from a slash command, a lifecycle notification, or
the chat agent -- should be built through the helpers defined here.

Design principles
-----------------
* **Auto-truncation** -- every text property is silently capped to its Discord
  API limit so callers never have to worry about ``HTTPException: 400``.
* **Consistent branding** -- all embeds carry an "AgentQueue" footer and a UTC
  timestamp unless explicitly suppressed.
* **Pure functions** -- nothing in this module touches the Discord gateway or
  requires a bot instance, making everything trivially unit-testable.
* **Semantic styling** -- callers pick an ``EmbedStyle`` (SUCCESS, ERROR, ...)
  and the factory resolves it to a color + icon.  Status-specific embeds use
  the task ``STATUS_COLORS`` / ``STATUS_EMOJIS`` dicts instead.

Discord API embed limits (enforced by ``check_embed_size``):

    ============  ================
    Property      Character limit
    ============  ================
    Title         256
    Description   4 096
    Field name    256
    Field value   1 024
    Footer text   2 048
    Author name   256
    Fields/embed  25
    **Total**     **6 000**
    ============  ================
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from enum import Enum
from typing import Sequence

import discord

from src.models import TaskStatus

# ---------------------------------------------------------------------------
# Discord API hard limits
# ---------------------------------------------------------------------------

LIMIT_TITLE = 256
LIMIT_DESCRIPTION = 4096
LIMIT_FIELD_NAME = 256
LIMIT_FIELD_VALUE = 1024
LIMIT_FOOTER_TEXT = 2048
LIMIT_AUTHOR_NAME = 256
LIMIT_FIELDS_PER_EMBED = 25
LIMIT_TOTAL_CHARS = 6000

# ---------------------------------------------------------------------------
# Semantic embed styles
# ---------------------------------------------------------------------------


class EmbedStyle(Enum):
    """High-level visual styles with associated color and icon."""

    SUCCESS  = (0x2ECC71, "\u2705")   # Green  / white check mark
    ERROR    = (0xE74C3C, "\u274C")   # Red    / cross mark
    WARNING  = (0xF39C12, "\u26A0\uFE0F")  # Amber  / warning sign
    INFO     = (0x3498DB, "\u2139\uFE0F")   # Blue   / information
    CRITICAL = (0x992D22, "\U0001F6A8")     # Dark red / rotating light

    def __init__(self, color: int, icon: str) -> None:
        self.color = color
        self.icon = icon


# ---------------------------------------------------------------------------
# Task-status visual mappings (previously scoped inside setup_commands())
# ---------------------------------------------------------------------------

STATUS_COLORS: dict[str, int] = {
    TaskStatus.DEFINED.value:            0x95A5A6,  # Gray
    TaskStatus.READY.value:              0x3498DB,  # Blue
    TaskStatus.ASSIGNED.value:           0x9B59B6,  # Purple
    TaskStatus.IN_PROGRESS.value:        0xF39C12,  # Amber
    TaskStatus.WAITING_INPUT.value:      0x1ABC9C,  # Teal
    TaskStatus.PAUSED.value:             0x7F8C8D,  # Dark gray
    TaskStatus.VERIFYING.value:          0x2980B9,  # Dark blue
    TaskStatus.AWAITING_APPROVAL.value:  0xE67E22,  # Orange
    TaskStatus.COMPLETED.value:          0x2ECC71,  # Green
    TaskStatus.FAILED.value:             0xE74C3C,  # Red
    TaskStatus.BLOCKED.value:            0x992D22,  # Dark red
}

STATUS_EMOJIS: dict[str, str] = {
    TaskStatus.DEFINED.value:            "\u26AA",       # white circle
    TaskStatus.READY.value:              "\U0001F535",    # blue circle
    TaskStatus.ASSIGNED.value:           "\U0001F4CB",    # clipboard
    TaskStatus.IN_PROGRESS.value:        "\U0001F7E1",    # yellow circle
    TaskStatus.WAITING_INPUT.value:      "\U0001F4AC",    # speech balloon
    TaskStatus.PAUSED.value:             "\u23F8\uFE0F",  # pause button
    TaskStatus.VERIFYING.value:          "\U0001F50D",    # magnifying glass
    TaskStatus.AWAITING_APPROVAL.value:  "\u231B",        # hourglass
    TaskStatus.COMPLETED.value:          "\U0001F7E2",    # green circle
    TaskStatus.FAILED.value:             "\U0001F534",    # red circle
    TaskStatus.BLOCKED.value:            "\u26D4",        # no entry
}

_DEFAULT_FOOTER = "AgentQueue"
_ELLIPSIS = "\u2026"

# ---------------------------------------------------------------------------
# Text-safety utilities
# ---------------------------------------------------------------------------


def truncate(text: str | None, max_len: int, *, suffix: str = _ELLIPSIS) -> str:
    """Safely truncate *text* to at most *max_len* characters.

    If *text* is ``None`` or empty the empty string is returned.  When
    truncation is necessary, *suffix* (default ``"\\u2026"``) is appended
    so the total length equals *max_len*.

    >>> truncate("hello world", 5)
    'hell\\u2026'
    >>> truncate(None, 100)
    ''
    """
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    if max_len <= len(suffix):
        return suffix[:max_len]
    return text[: max_len - len(suffix)] + suffix


def unix_timestamp(dt: datetime | None = None, style: str = "R") -> str:
    """Format *dt* as a Discord dynamic timestamp string.

    Parameters
    ----------
    dt:
        The datetime to format.  Defaults to ``datetime.now(tz=timezone.utc)``.
    style:
        One of the Discord timestamp styles:
        ``t`` (short time), ``T`` (long time), ``d`` (short date),
        ``D`` (long date), ``f`` (short date/time), ``F`` (long date/time),
        ``R`` (relative -- the default).

    Returns
    -------
    str
        e.g. ``<t:1700000000:R>``
    """
    if dt is None:
        dt = datetime.now(tz=timezone.utc)
    return f"<t:{math.floor(dt.timestamp())}:{style}>"


# ---------------------------------------------------------------------------
# Embed size guard
# ---------------------------------------------------------------------------


def _embed_char_count(embed: discord.Embed) -> int:
    """Return the total character count Discord uses to enforce the 6 000 limit."""
    total = 0
    if embed.title:
        total += len(embed.title)
    if embed.description:
        total += len(embed.description)
    if embed.footer and embed.footer.text:
        total += len(embed.footer.text)
    if embed.author and embed.author.name:
        total += len(embed.author.name)
    for field in embed.fields:
        total += len(field.name or "")
        total += len(field.value or "")
    return total


def check_embed_size(embed: discord.Embed) -> tuple[bool, int]:
    """Validate that *embed* respects the 6 000 total-character limit.

    Returns
    -------
    tuple[bool, int]
        ``(is_valid, total_chars)`` where *is_valid* is ``True`` when
        the embed is within limits.
    """
    total = _embed_char_count(embed)
    return (total <= LIMIT_TOTAL_CHARS, total)


# ---------------------------------------------------------------------------
# Core factory
# ---------------------------------------------------------------------------


def make_embed(
    style: EmbedStyle,
    title: str,
    *,
    description: str | None = None,
    fields: Sequence[tuple[str, str, bool]] | None = None,
    footer: str | None = _DEFAULT_FOOTER,
    timestamp: datetime | None | bool = True,
    color_override: int | None = None,
    url: str | None = None,
) -> discord.Embed:
    """Build a ``discord.Embed`` with automatic truncation and branding.

    Parameters
    ----------
    style:
        Semantic style that determines the embed color and title icon.
    title:
        Embed title (auto-truncated to 256 chars).  The style's icon is
        prepended automatically.
    description:
        Optional body text (auto-truncated to 4 096 chars).
    fields:
        Optional sequence of ``(name, value, inline)`` tuples.  Each name
        and value is auto-truncated to its Discord limit.  At most 25
        fields are kept.
    footer:
        Footer text.  Defaults to ``"AgentQueue"``.  Pass ``None`` to omit.
    timestamp:
        * ``True`` (default) -- use ``datetime.now(tz=timezone.utc)``
        * A ``datetime`` instance -- use that value
        * ``None`` or ``False`` -- omit the timestamp
    color_override:
        If provided, overrides the color from *style*.
    url:
        Optional URL attached to the embed title.

    Returns
    -------
    discord.Embed
    """
    icon = style.icon
    color = color_override if color_override is not None else style.color

    full_title = f"{icon} {truncate(title, LIMIT_TITLE - len(icon) - 1)}"

    embed = discord.Embed(
        title=full_title,
        description=truncate(description, LIMIT_DESCRIPTION) if description else None,
        color=color,
        url=url,
    )

    # Timestamp
    if timestamp is True:
        embed.timestamp = datetime.now(tz=timezone.utc)
    elif isinstance(timestamp, datetime):
        embed.timestamp = timestamp
    # else: omit

    # Footer
    if footer is not None:
        embed.set_footer(text=truncate(footer, LIMIT_FOOTER_TEXT))

    # Fields
    if fields:
        for name, value, inline in fields[:LIMIT_FIELDS_PER_EMBED]:
            embed.add_field(
                name=truncate(name, LIMIT_FIELD_NAME),
                value=truncate(value, LIMIT_FIELD_VALUE),
                inline=inline,
            )

    return embed


# ---------------------------------------------------------------------------
# Convenience builders
# ---------------------------------------------------------------------------


def success_embed(
    title: str,
    *,
    description: str | None = None,
    fields: Sequence[tuple[str, str, bool]] | None = None,
    **kwargs,
) -> discord.Embed:
    """Green success embed."""
    return make_embed(EmbedStyle.SUCCESS, title, description=description, fields=fields, **kwargs)


def error_embed(
    title: str,
    *,
    description: str | None = None,
    fields: Sequence[tuple[str, str, bool]] | None = None,
    **kwargs,
) -> discord.Embed:
    """Red error embed."""
    return make_embed(EmbedStyle.ERROR, title, description=description, fields=fields, **kwargs)


def warning_embed(
    title: str,
    *,
    description: str | None = None,
    fields: Sequence[tuple[str, str, bool]] | None = None,
    **kwargs,
) -> discord.Embed:
    """Amber warning embed."""
    return make_embed(EmbedStyle.WARNING, title, description=description, fields=fields, **kwargs)


def info_embed(
    title: str,
    *,
    description: str | None = None,
    fields: Sequence[tuple[str, str, bool]] | None = None,
    **kwargs,
) -> discord.Embed:
    """Blue info embed."""
    return make_embed(EmbedStyle.INFO, title, description=description, fields=fields, **kwargs)


def critical_embed(
    title: str,
    *,
    description: str | None = None,
    fields: Sequence[tuple[str, str, bool]] | None = None,
    **kwargs,
) -> discord.Embed:
    """Dark-red critical embed."""
    return make_embed(EmbedStyle.CRITICAL, title, description=description, fields=fields, **kwargs)


def status_embed(
    status: str,
    title: str,
    *,
    description: str | None = None,
    fields: Sequence[tuple[str, str, bool]] | None = None,
    **kwargs,
) -> discord.Embed:
    """Build an embed whose color and title icon match a task *status*.

    *status* should be one of the ``TaskStatus`` ``.value`` strings (e.g.
    ``"IN_PROGRESS"``).  Unknown statuses fall back to gray / white-circle.

    Example::

        embed = status_embed("COMPLETED", "Task Finished", description="All done.")
    """
    color = STATUS_COLORS.get(status, 0x95A5A6)
    emoji = STATUS_EMOJIS.get(status, "\u26AA")

    full_title = f"{emoji} {truncate(title, LIMIT_TITLE - 3)}"

    embed = discord.Embed(
        title=full_title,
        description=truncate(description, LIMIT_DESCRIPTION) if description else None,
        color=color,
    )

    # Timestamp -- same logic as make_embed
    ts = kwargs.pop("timestamp", True)
    if ts is True:
        embed.timestamp = datetime.now(tz=timezone.utc)
    elif isinstance(ts, datetime):
        embed.timestamp = ts

    # Footer
    footer = kwargs.pop("footer", _DEFAULT_FOOTER)
    if footer is not None:
        embed.set_footer(text=truncate(footer, LIMIT_FOOTER_TEXT))

    # URL
    url = kwargs.pop("url", None)
    if url is not None:
        embed.url = url

    # Fields
    if fields:
        for name, value, inline in fields[:LIMIT_FIELDS_PER_EMBED]:
            embed.add_field(
                name=truncate(name, LIMIT_FIELD_NAME),
                value=truncate(value, LIMIT_FIELD_VALUE),
                inline=inline,
            )

    return embed
