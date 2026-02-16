from __future__ import annotations

from abc import ABC, abstractmethod

from src.models import AgentOutput, TaskContext


class AgentAdapter(ABC):
    @abstractmethod
    async def start(self, task: TaskContext) -> None:
        """Launch the agent process with the given task."""

    @abstractmethod
    async def wait(self) -> AgentOutput:
        """Wait for the agent to finish and return results."""

    @abstractmethod
    async def stop(self) -> None:
        """Forcefully stop the agent."""

    @abstractmethod
    async def is_alive(self) -> bool:
        """Check if the agent process is still running."""
