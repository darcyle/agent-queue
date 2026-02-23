"""Parse implementation plan files into structured task definitions.

When an agent completes a task that results in an implementation plan
(written to ``.claude/plan.md`` or a similar file in the workspace), this
module reads the plan file, parses its markdown content, and extracts
individual steps that can be turned into follow-up tasks.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


# Default locations where agents write plan files (checked in order).
DEFAULT_PLAN_FILE_PATTERNS = [
    ".claude/plan.md",
    "plan.md",
]


@dataclass
class PlanStep:
    """A single actionable step extracted from an implementation plan."""

    title: str
    description: str
    priority_hint: int = 0  # 0-based index in the original plan (for ordering)


@dataclass
class ParsedPlan:
    """The result of parsing a plan file."""

    source_file: str
    steps: list[PlanStep] = field(default_factory=list)
    raw_content: str = ""


def find_plan_file(workspace: str, patterns: list[str] | None = None) -> str | None:
    """Search for a plan file in the workspace directory.

    Checks each candidate path in order and returns the first one that
    exists, or ``None`` if no plan file is found.
    """
    candidates = patterns or DEFAULT_PLAN_FILE_PATTERNS
    for pattern in candidates:
        path = os.path.join(workspace, pattern)
        if os.path.isfile(path):
            return path
    return None


def read_plan_file(path: str) -> str:
    """Read the raw contents of a plan file."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def parse_plan(content: str, source_file: str = "") -> ParsedPlan:
    """Parse plan markdown content into structured steps.

    Supports several common plan formats:

    1. **Heading-based plans** — Each ``## Section`` or ``### Section``
       becomes a step, with the text underneath as the description.

    2. **Numbered-list plans** — Top-level numbered items (``1. ...``,
       ``2. ...``) become individual steps with the item text and any
       indented sub-items as the description.

    3. **Mixed** — If both heading sections and numbered lists are present,
       heading sections take priority.

    Returns a ``ParsedPlan`` containing the ordered list of steps.
    """
    result = ParsedPlan(source_file=source_file, raw_content=content)

    if not content.strip():
        return result

    # Try heading-based parsing first
    steps = _parse_heading_sections(content)
    if steps:
        result.steps = steps
        return result

    # Fall back to numbered-list parsing
    steps = _parse_numbered_list(content)
    if steps:
        result.steps = steps
        return result

    # Final fallback: treat the entire content as a single step
    # (only if there's meaningful content beyond a title)
    title = _extract_title(content)
    body = _remove_title(content).strip()
    if body:
        result.steps = [PlanStep(title=title or "Implementation Plan", description=body)]

    return result


def _parse_heading_sections(content: str) -> list[PlanStep]:
    """Parse sections delimited by ## or ### headings."""
    # Match ## or ### headings (not # which is the document title)
    heading_pattern = re.compile(r"^(#{2,3})\s+(.+)$", re.MULTILINE)
    matches = list(heading_pattern.finditer(content))

    if not matches:
        return []

    steps: list[PlanStep] = []
    for i, match in enumerate(matches):
        title = match.group(2).strip()

        # Clean up common prefixes like "Step 1:", "1.", "Phase 1:" etc.
        title = _clean_step_title(title)

        if not title:
            continue

        # Extract body between this heading and the next (or end of file)
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[start:end].strip()

        steps.append(PlanStep(
            title=title,
            description=body,
            priority_hint=i,
        ))

    return steps


def _parse_numbered_list(content: str) -> list[PlanStep]:
    """Parse top-level numbered items as individual steps."""
    # Match lines starting with a number followed by a dot or parenthesis
    item_pattern = re.compile(r"^(\d+)[.)]\s+(.+)$", re.MULTILINE)
    matches = list(item_pattern.finditer(content))

    if not matches:
        return []

    steps: list[PlanStep] = []
    for i, match in enumerate(matches):
        title = match.group(2).strip()

        # Clean up markdown formatting from the title
        title = _clean_step_title(title)

        if not title:
            continue

        # Collect continuation lines (indented or sub-items) until the next
        # top-level numbered item or end of content
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body_text = content[start:end].strip()

        # Combine title and body into the description
        description = title
        if body_text:
            description = f"{title}\n\n{body_text}"

        steps.append(PlanStep(
            title=_truncate(title, 120),
            description=description,
            priority_hint=i,
        ))

    return steps


def _extract_title(content: str) -> str:
    """Extract the document title (first # heading) if present."""
    match = re.match(r"^#\s+(.+)$", content.strip(), re.MULTILINE)
    return match.group(1).strip() if match else ""


def _remove_title(content: str) -> str:
    """Remove the first # heading from the content."""
    return re.sub(r"^#\s+.+$\n?", "", content.strip(), count=1, flags=re.MULTILINE)


def _clean_step_title(title: str) -> str:
    """Remove common step numbering/labeling prefixes and markdown formatting."""
    # Remove leading patterns like "Step 1:", "Phase 1:", "1.", "1)"
    # Require a digit after the keyword so plain "Task Foo" is kept.
    title = re.sub(
        r"^(?:Step|Phase|Part|Task)\s*\d+\s*[:.)\-\u2014\u2013]\s*",
        "", title, flags=re.IGNORECASE,
    )
    # Handle keyword + digit without separator: "Step 1 Do something"
    title = re.sub(
        r"^(?:Step|Phase|Part|Task)\s+\d+\s+",
        "", title, flags=re.IGNORECASE,
    )
    # Remove leading number + separator if still present
    title = re.sub(r"^\d+[.):\-\u2014\u2013]\s*", "", title)
    # Remove bold/italic markdown
    title = re.sub(r"\*{1,2}(.+?)\*{1,2}", r"\1", title)
    return title.strip()


def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len, adding ellipsis if needed."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def build_task_description(
    step: PlanStep,
    parent_task: object | None = None,
    plan_context: str = "",
) -> str:
    """Build a self-contained task description from a plan step.

    The description is enriched with context from the parent task and
    the overall plan so that the executing agent has all the information
    it needs without access to external context.

    Args:
        step: The parsed plan step.
        parent_task: The parent task object (must have .title and .description).
        plan_context: Additional context from the plan preamble.

    Returns:
        A self-contained description string.
    """
    parts: list[str] = []

    parts.append(f"**{step.title}**\n")

    if plan_context:
        parts.append(f"## Background Context\n{plan_context}\n")

    if parent_task and hasattr(parent_task, "title"):
        parts.append(
            f"This task is part of the implementation plan from: "
            f"**{parent_task.title}**\n"
        )

    parts.append(f"## Task Details\n{step.description}")

    return "\n".join(parts)
