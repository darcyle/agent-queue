from __future__ import annotations

from src.adapters.base import AgentAdapter
from src.adapters.claude import ClaudeAdapter, ClaudeAdapterConfig


class AdapterFactory:
    """Creates agent adapters by type."""

    def __init__(self, claude_config: ClaudeAdapterConfig | None = None):
        self._claude_config = claude_config

    def create(self, agent_type: str) -> AgentAdapter:
        if agent_type == "claude":
            return ClaudeAdapter(self._claude_config)
        raise ValueError(f"Unknown agent type: {agent_type}")
