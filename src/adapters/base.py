"""AgentAdapter ABC -- the contract between the orchestrator and any AI agent.

The interface is intentionally minimal: start, wait, stop, is_alive.  This
keeps agent backends decoupled from orchestration logic and makes it easy to
add new agent types without modifying the orchestrator.

Lifecycle:
  1. ``start(task)`` receives a TaskContext containing the workspace path,
     task description, acceptance criteria, and attached context.
  2. ``wait(on_message)`` blocks until the agent finishes.  While running it
     streams progress via the ``on_message`` callback (a MessageCallback),
     which the orchestrator wires up to a Discord thread for live output.
  3. ``stop()`` forcefully terminates the agent (e.g. on cancellation).
  4. ``is_alive()`` lets the heartbeat monitor detect dead agents.

The MessageCallback type (``async (str) -> None``) enables real-time
streaming of agent output to Discord threads without the adapter needing
to know anything about Discord.

See specs/adapters/claude.md for the full behavioral specification.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Awaitable

from src.models import AgentOutput, TaskContext

# Callback invoked with each human-readable message chunk as the agent works.
# The orchestrator typically wires this to a Discord thread for live output.
MessageCallback = Callable[[str], Awaitable[None]]


class AgentAdapter(ABC):
    @abstractmethod
    async def start(self, task: TaskContext) -> None:
        """Launch the agent process with the given task."""

    @abstractmethod
    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:
        """Wait for the agent to finish and return results."""

    @abstractmethod
    async def stop(self) -> None:
        """Forcefully stop the agent."""

    @abstractmethod
    async def is_alive(self) -> bool:
        """Check if the agent process is still running."""
