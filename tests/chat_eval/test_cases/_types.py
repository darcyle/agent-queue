"""Core data types for the supervisor evaluation framework.

Updated: terminology reflects supervisor-based architecture (post-refactor).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Difficulty(Enum):
    TRIVIAL = "trivial"
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"
    ADVERSARIAL = "adversarial"


@dataclass
class ExpectedTool:
    """A tool call expected from the LLM for a given turn."""

    name: str
    args: dict = field(default_factory=dict)
    args_exact: bool = False  # If True, args must match exactly (not subset)


@dataclass
class Turn:
    """A single user message and the expected tool calls in response."""

    user_message: str
    expected_tools: list[ExpectedTool] = field(default_factory=list)
    not_expected_tools: list[str] = field(default_factory=list)
    ordered: bool = False  # If True, tool calls must appear in this order
    active_project: str | None = None  # Override active project for this turn


@dataclass
class TestCase:
    """A complete test scenario with one or more conversational turns."""

    id: str
    description: str
    turns: list[Turn]
    category: str
    tags: list[str] = field(default_factory=list)
    difficulty: Difficulty = Difficulty.EASY
    setup_commands: list[tuple[str, dict]] = field(default_factory=list)
    active_project: str | None = None  # Default active project for all turns
    requires_llm: bool = True
