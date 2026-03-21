"""ChatObserver — passive observation and batching of Discord chat messages.

The ChatObserver implements Stage 1 filtering (keyword-based) to identify
potentially relevant messages, buffers them by channel, and provides batch
readiness detection for Stage 2 LLM analysis.

Stage 1 Filter Logic:
    - Reject bot messages
    - Reject messages from unknown projects
    - Pass if message length >= 200 chars (detailed discussion)
    - Pass if message contains project-specific term (from project profile)
    - Pass if message contains action word (deploy, fix, bug, etc.)
    - Pass if message contains custom keyword (from config)
    - Otherwise reject (trivial messages like "ok", "thanks")
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import ObservationConfig

# Action words that indicate work-relevant messages
_ACTION_WORDS = {
    "deploy", "hotfix", "fix", "bug", "crash", "error", "issue", "problem",
    "broken", "failing", "failed", "investigate", "debug", "urgent",
    "blocker", "stuck", "help", "review", "merge", "release", "ship",
    "revert", "rollback", "incident", "outage", "down", "timeout",
}

# Minimum message length to auto-pass (detailed discussion)
_LENGTH_THRESHOLD = 200


class ChatObserver:
    """Passive chat observer with Stage 1 keyword filtering and batching."""

    def __init__(
        self,
        config: ObservationConfig,
        project_profiles: dict[str, set[str]] | None = None,
    ):
        """Initialize the observer.

        Args:
            config: Observation configuration
            project_profiles: Map of project_id -> set of project-specific terms
        """
        self._config = config
        self._project_profiles = project_profiles or {}
        self._buffers: dict[int, list[dict]] = defaultdict(list)
        self._buffer_timestamps: dict[int, float] = {}

    @property
    def enabled(self) -> bool:
        """Return whether observation is enabled."""
        return self._config.enabled

    def set_project_profiles(self, profiles: dict[str, set[str]]) -> None:
        """Update project profiles (used when profiles change at runtime)."""
        self._project_profiles = profiles

    def passes_stage1(self, message: dict) -> bool:
        """Check if a message passes Stage 1 keyword filter.

        Args:
            message: Message dict with keys: channel_id, project_id, author,
                content, timestamp, is_bot

        Returns:
            True if message should be buffered for batch analysis
        """
        if not self._config.enabled:
            return False

        # Reject bot messages
        if message.get("is_bot", False):
            return False

        # Reject unknown projects
        project_id = message.get("project_id")
        if project_id not in self._project_profiles:
            return False

        content = message.get("content", "").lower()

        # Pass long messages (detailed discussion)
        if len(content) >= _LENGTH_THRESHOLD:
            return True

        # Pass if contains project-specific term
        project_terms = self._project_profiles.get(project_id, set())
        if any(term.lower() in content for term in project_terms):
            return True

        # Pass if contains action word
        words = set(content.split())
        if words & _ACTION_WORDS:
            return True

        # Pass if contains custom keyword
        if any(keyword.lower() in content for keyword in self._config.stage1_keywords):
            return True

        # Otherwise reject
        return False

    def buffer_message(self, message: dict) -> None:
        """Add a message to the buffer for its channel.

        Args:
            message: Message dict (must include channel_id)
        """
        channel_id = message["channel_id"]
        self._buffers[channel_id].append(message)
        if channel_id not in self._buffer_timestamps:
            self._buffer_timestamps[channel_id] = time.time()

    def get_buffer_size(self, channel_id: int) -> int:
        """Return the number of buffered messages for a channel."""
        return len(self._buffers.get(channel_id, []))

    def flush_buffer(self, channel_id: int) -> list[dict]:
        """Remove and return all buffered messages for a channel.

        Args:
            channel_id: Channel to flush

        Returns:
            List of buffered messages (empty if no buffer exists)
        """
        messages = self._buffers.pop(channel_id, [])
        self._buffer_timestamps.pop(channel_id, None)
        return messages

    def has_pending_channels(self) -> bool:
        """Return whether any channels have buffered messages."""
        return len(self._buffers) > 0

    def pending_channel_ids(self) -> list[int]:
        """Return list of channel IDs with buffered messages."""
        return list(self._buffers.keys())
