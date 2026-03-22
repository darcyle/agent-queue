"""Backward-compatibility shim — ChatAgent is now Supervisor."""
from src.supervisor import (  # noqa: F401
    Supervisor as ChatAgent,
    TOOLS,
    SYSTEM_PROMPT_TEMPLATE,
    _tool_label,
)
__all__ = ["ChatAgent", "TOOLS", "SYSTEM_PROMPT_TEMPLATE"]
