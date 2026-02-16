from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Awaitable

from src.models import AgentOutput, TaskContext

# Callback type: receives a message string to forward somewhere (e.g. Discord)
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
