"""Adapter layer: abstracts AI coding agents behind a common interface.

The orchestrator never talks to a specific agent implementation directly.
Instead, it requests an adapter from AdapterFactory by type string (e.g.
"claude") and interacts through the AgentAdapter ABC defined in base.py.

AdapterFactory implements the factory pattern so adding a new agent backend
is a one-line change here plus a new module.  Currently only "claude"
(Claude Code via the Agent SDK) is implemented, but the design anticipates
future adapters for tools like Codex, Cursor, Aider, etc.
"""

from __future__ import annotations

from src.adapters.base import AgentAdapter
from src.adapters.claude import ClaudeAdapter, ClaudeAdapterConfig


class AdapterFactory:
    """Creates agent adapters by type.

    Holds per-type configuration (e.g. ClaudeAdapterConfig) and instantiates
    a fresh adapter for each task execution.  The orchestrator calls
    ``create("claude")`` once per task assignment.
    """

    def __init__(self, claude_config: ClaudeAdapterConfig | None = None):
        self._claude_config = claude_config

    def create(self, agent_type: str) -> AgentAdapter:
        if agent_type == "claude":
            return ClaudeAdapter(self._claude_config)
        raise ValueError(f"Unknown agent type: {agent_type}")
