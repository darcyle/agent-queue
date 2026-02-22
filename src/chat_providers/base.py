from __future__ import annotations

from abc import ABC, abstractmethod

from .types import ChatResponse


class ChatProvider(ABC):
    @abstractmethod
    async def create_message(
        self,
        *,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
    ) -> ChatResponse:
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        ...
