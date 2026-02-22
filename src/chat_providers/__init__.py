from __future__ import annotations

from src.config import ChatProviderConfig

from .anthropic import AnthropicChatProvider
from .base import ChatProvider
from .types import ChatResponse, TextBlock, ToolUseBlock


def create_chat_provider(config: ChatProviderConfig) -> ChatProvider | None:
    """Create a chat provider based on configuration.

    Returns None if the provider cannot be initialized (e.g. missing credentials).
    """
    if config.provider == "ollama":
        from .ollama import OllamaChatProvider

        return OllamaChatProvider(
            model=config.model or "qwen2.5:32b-instruct-q3_K_M",
            base_url=config.base_url or "http://localhost:11434/v1",
        )

    # Default: anthropic
    provider = AnthropicChatProvider(model=config.model)
    if not provider.is_configured:
        return None
    return provider


__all__ = [
    "ChatProvider",
    "ChatResponse",
    "TextBlock",
    "ToolUseBlock",
    "create_chat_provider",
]
