from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ParsedCommand:
    intent: str  # "create_task", "status", "pause_project", etc.
    project_name: str | None = None
    task_title: str | None = None
    task_description: str | None = None
    priority: int | None = None
    raw_text: str = ""


async def parse_natural_language(text: str) -> ParsedCommand:
    """Parse natural language into a structured command.

    This is a stub that will be replaced with an LLM-based parser.
    For now, it returns a basic parsed command based on keyword matching.
    """
    text_lower = text.lower().strip()

    if any(w in text_lower for w in ["status", "how are", "what's running"]):
        return ParsedCommand(intent="status", raw_text=text)

    if any(w in text_lower for w in ["pause", "stop", "hold"]):
        return ParsedCommand(intent="pause_project", raw_text=text)

    if any(w in text_lower for w in ["resume", "unpause", "continue", "start"]):
        return ParsedCommand(intent="resume_project", raw_text=text)

    if any(w in text_lower for w in ["add task", "create task", "new task", "do this"]):
        return ParsedCommand(
            intent="create_task",
            task_title=text[:100],
            task_description=text,
            raw_text=text,
        )

    return ParsedCommand(intent="unknown", raw_text=text)
