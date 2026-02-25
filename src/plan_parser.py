"""Parse implementation plan files into structured task definitions.

When an agent completes a task that results in an implementation plan
(written to ``.claude/plan.md`` or a similar file in the workspace), this
module reads the plan file, parses its markdown content, and extracts
individual steps that can be turned into follow-up tasks.
"""

from __future__ import annotations

import glob
import os
import re
import time
from dataclasses import dataclass, field


# Headings that are informational/structural and should not become tasks.
NON_ACTIONABLE_HEADINGS = {
    "overview", "summary", "background", "context", "introduction",
    "conclusion", "notes", "references", "appendix", "prerequisites",
    "requirements", "assumptions", "risks", "open questions",
    "future work", "out of scope", "glossary", "table of contents",
    "motivation", "goals", "non-goals", "success criteria",
    "files modified", "files to modify", "file changes",
    "testing strategy", "testing notes", "rollback plan",
    "implementation order", "dependency graph", "design decisions",
    "key decisions", "additional observations", "identified gaps",
    "verdict", "executive summary", "problem statement",
    "data flow", "file change summary", "user workflow examples",
    # Additional patterns for design documents and reference sections
    "current state", "current inconsistencies", "proposed standard patterns",
    "design principles", "color palette", "embed structure template",
    "inline field layout rules", "visual design specification",
    "libraries and dependencies", "risk assessment",
    "risk assessment & mitigations", "no additional dependencies required",
    "optional enhancements", "total character guard",
    "what embeds support", "hard limits", "markdown support",
    "expected outcome", "estimated effort", "technical details",
    "api reference", "data model", "schema", "configuration",
    "constraints", "capabilities", "architecture", "proposed architecture",
    "color scheme", "style guide", "branding", "dependencies",
    "existing implementation", "current implementation",
    "design specification", "visual design", "migration notes",
    "backward compatibility", "security considerations",
    "performance considerations", "error handling strategy",
    "project structure", "file organization", "directory structure",
    "environment setup", "infrastructure", "deployment",
    "summary of recommendations", "summary of changes",
}

# Keywords in a ## heading that indicate an "implementation section" container.
# Sub-headings (###) within such a section are the actual implementation steps.
IMPLEMENTATION_SECTION_KEYWORDS = {
    "implementation plan", "implementation steps", "implementation phases",
    "action items", "tasks", "work plan", "execution plan",
    "implementation order", "development phases", "coding tasks",
    "implementation", "plan of action", "action plan",
    "development plan", "work items", "deliverables",
}

# Pattern to detect step/phase-like headings (always considered actionable).
STEP_HEADING_PATTERN = re.compile(
    r"^(?:Phase|Step|Part|Task|Stage)\s+\d",
    re.IGNORECASE,
)

# Default locations where agents write plan files (checked in order).
# Entries containing glob characters (*, ?) are expanded via glob.glob().
DEFAULT_PLAN_FILE_PATTERNS = [
    ".claude/plan.md",
    "plan.md",
    "docs/plans/*.md",
    "plans/*.md",
    "docs/plan.md",
    "notes/*.md",
    "notes/plans/*.md",
]

# Plan-indicator patterns for deep scan fallback (headings with Phase/Step numbering).
_PLAN_INDICATOR_PATTERN = re.compile(
    r"^#{1,3}\s+(?:Phase|Step|Part|Stage)\s+\d",
    re.MULTILINE | re.IGNORECASE,
)

# Keywords in headings that strongly suggest informational content (used by quality scorer).
_INFORMATIONAL_KEYWORDS = {
    "support", "limits", "palette", "template", "principles", "overview",
    "current", "proposed", "specification", "constraints", "capabilities",
    "existing", "libraries", "dependencies", "risk", "assessment",
    "color", "visual", "branding", "layout", "rules",
}

# Keywords in headings that strongly suggest actionable implementation content.
_ACTIONABLE_KEYWORDS = {
    "create", "add", "implement", "update", "fix", "refactor", "migrate",
    "convert", "build", "write", "modify", "remove", "replace", "move",
    "setup", "configure", "deploy", "test", "verify", "validate",
    "phase", "step", "foundation", "integrate",
}


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

    Checks each candidate pattern in order.  Patterns that contain glob
    characters (``*`` or ``?``) are expanded; the most-recently-modified
    match is returned so that freshly written plans take priority.

    For plain (non-glob) patterns the file is checked directly.

    If no pattern matches, falls back to a deep scan that looks for
    recently-modified markdown files with plan-like structure indicators
    (e.g. headings containing "Phase 1:", "Step 2:", etc.).

    Returns the first match, or ``None`` if no plan file is found.
    """
    candidates = patterns or DEFAULT_PLAN_FILE_PATTERNS
    for pattern in candidates:
        full_pattern = os.path.join(workspace, pattern)
        if any(c in pattern for c in ("*", "?")):
            # Glob pattern — expand and pick the newest match
            matches = [p for p in glob.glob(full_pattern) if os.path.isfile(p)]
            if matches:
                # Return the most-recently-modified file so the latest plan
                # wins when multiple plan files exist (e.g. date-prefixed).
                matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                return matches[0]
        else:
            if os.path.isfile(full_pattern):
                return full_pattern

    # Fallback: deep scan for recently-modified markdown files with plan indicators
    result = _deep_scan_for_plan(workspace)
    if result:
        print(
            f"Plan file discovery: found plan via deep scan at {result} "
            f"(not in standard patterns: {candidates})"
        )
    return result


def _deep_scan_for_plan(
    workspace: str, max_age_seconds: int = 1800,
) -> str | None:
    """Fallback scan for markdown files with plan-like structure.

    Searches the workspace for ``.md`` files modified within the last
    ``max_age_seconds`` (default 30 minutes) whose content contains
    plan structure indicators such as ``## Phase 1:``, ``### Step 2:``,
    or ``## Implementation Plan``.

    Skips archived plans in ``.claude/plans/`` to avoid re-processing.

    Returns the path to the best candidate or ``None``.
    """
    cutoff = time.time() - max_age_seconds

    candidates: list[tuple[float, str]] = []
    for md_path in glob.glob(os.path.join(workspace, "**/*.md"), recursive=True):
        if not os.path.isfile(md_path):
            continue
        # Skip archived plans
        if ".claude/plans/" in md_path.replace("\\", "/"):
            continue
        mtime = os.path.getmtime(md_path)
        if mtime < cutoff:
            continue
        # Quick content check for plan-like structure
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                head = f.read(8192)
            # Check for Phase/Step headings or implementation plan sections
            if _PLAN_INDICATOR_PATTERN.search(head):
                candidates.append((mtime, md_path))
                continue
            # Also check for implementation section keywords in headings
            impl_heading = re.search(
                r"^#{1,3}\s+.*(?:implementation\s+plan|implementation\s+steps"
                r"|implementation\s+phases|action\s+items)",
                head,
                re.MULTILINE | re.IGNORECASE,
            )
            if impl_heading:
                candidates.append((mtime, md_path))
        except OSError:
            continue

    if candidates:
        candidates.sort(reverse=True)  # Newest first
        return candidates[0][1]
    return None


def read_plan_file(path: str) -> str:
    """Read the raw contents of a plan file."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def parse_plan(
    content: str, source_file: str = "", max_steps: int = 20,
) -> ParsedPlan:
    """Parse plan markdown content into structured steps.

    Supports several common plan formats:

    1. **Implementation-section plans** — Large design documents with a
       dedicated ``## Implementation Plan`` or similar section containing
       ``###`` sub-headings for each phase. Only the sub-headings within
       the implementation section become steps.

    2. **Heading-based plans** — Each ``## Section`` or ``### Section``
       becomes a step, with the text underneath as the description.

    3. **Numbered-list plans** — Top-level numbered items (``1. ...``,
       ``2. ...``) become individual steps with the item text and any
       indented sub-items as the description.

    4. **Mixed** — If both heading sections and numbered lists are present,
       heading sections take priority.

    Args:
        content: Raw markdown content of the plan file.
        source_file: Path to the plan file (for traceability).
        max_steps: Maximum number of steps to return (truncated if exceeded).

    Returns a ``ParsedPlan`` containing the ordered list of steps.
    """
    result = ParsedPlan(source_file=source_file, raw_content=content)

    if not content.strip():
        return result

    # Try implementation-section parsing first (for design documents)
    steps = _parse_implementation_section(content)
    if steps:
        result.steps = steps[:max_steps]
        return result

    # Try heading-based parsing
    steps = _parse_heading_sections(content)
    if steps:
        # Quality check: if too many non-actionable steps were extracted,
        # try to filter to only step/phase headings
        quality = _score_parse_quality(steps)
        if quality < 0.4 and len(steps) > 5:
            # Re-filter: keep only headings that look like implementation phases
            filtered = [
                s for s in steps
                if STEP_HEADING_PATTERN.match(s.title)
                or _is_likely_actionable(s.title)
            ]
            if filtered:
                steps = filtered
                print(
                    f"Plan parser: quality score {quality:.2f} too low, "
                    f"filtered {len(steps)} actionable steps from headings"
                )
        result.steps = steps[:max_steps]
        return result

    # Fall back to numbered-list parsing
    steps = _parse_numbered_list(content)
    if steps:
        result.steps = steps[:max_steps]
        return result

    # Final fallback: treat the entire content as a single step
    # (only if there's meaningful content beyond a title)
    title = _extract_title(content)
    body = _remove_title(content).strip()
    if body:
        result.steps = [PlanStep(title=title or "Implementation Plan", description=body)]

    return result


def _parse_implementation_section(content: str) -> list[PlanStep]:
    """Extract steps from a dedicated implementation section of a design document.

    Many complex plans are structured as design documents with a dedicated
    "Implementation Plan" section containing the actual phases/steps. This
    parser identifies such a section by looking for a ``##`` heading whose
    title matches known implementation keywords, then extracts ONLY the
    ``###`` sub-headings within that section as steps.

    This prevents informational/reference sections (capabilities, color
    palettes, risk assessments, etc.) from being extracted as tasks.

    Returns an empty list if no implementation section is found.
    """
    heading_pattern = re.compile(r"^(#{2,3})\s+(.+)$", re.MULTILINE)
    matches = list(heading_pattern.finditer(content))

    if not matches:
        return []

    # Find the implementation section container
    impl_start = None
    impl_end = None
    for i, match in enumerate(matches):
        level = len(match.group(1))
        title = _clean_step_title(match.group(2).strip())

        if level == 2 and title.lower() in IMPLEMENTATION_SECTION_KEYWORDS:
            impl_start = i
            # Find the end: next ## heading or end of doc
            for j in range(i + 1, len(matches)):
                if len(matches[j].group(1)) == 2:
                    impl_end = j
                    break
            if impl_end is None:
                impl_end = len(matches)
            break

    if impl_start is None:
        return []

    # Extract ### sub-headings within the implementation section
    steps: list[PlanStep] = []
    for i in range(impl_start + 1, impl_end):
        match = matches[i]
        level = len(match.group(1))
        if level != 3:
            continue

        title = _clean_step_title(match.group(2).strip())
        if not title:
            continue

        # Skip non-actionable sub-headings even within the implementation section
        if title.lower() in NON_ACTIONABLE_HEADINGS:
            continue

        # Extract body between this heading and the next (or end of section)
        start = match.end()
        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            end = len(content)
        body = content[start:end].strip()

        if len(body) < 20:
            continue

        steps.append(PlanStep(
            title=title,
            description=body,
            priority_hint=len(steps),
        ))

    return steps


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

        # Skip non-actionable headings (overviews, summaries, etc.)
        if title.lower() in NON_ACTIONABLE_HEADINGS:
            continue

        # Extract body between this heading and the next (or end of file)
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[start:end].strip()

        # Skip steps with too little content to be actionable
        if len(body) < 20:
            continue

        steps.append(PlanStep(
            title=title,
            description=body,
            priority_hint=len(steps),
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


def _score_parse_quality(steps: list[PlanStep]) -> float:
    """Score parsed steps 0.0-1.0 based on how likely they are actionable.

    A low score suggests the regex parser extracted many informational
    headings rather than implementation steps, and the result should be
    filtered or re-parsed with the LLM parser.
    """
    if not steps:
        return 0.0

    actionable_count = 0.0
    for step in steps:
        title_lower = step.title.lower()
        words = set(title_lower.split())

        # Headings matching Phase/Step patterns are definitively actionable
        if STEP_HEADING_PATTERN.match(step.title):
            actionable_count += 1.0
            continue

        if words & _ACTIONABLE_KEYWORDS:
            actionable_count += 1.0
        elif words & _INFORMATIONAL_KEYWORDS:
            actionable_count -= 0.5

    return max(0.0, min(1.0, actionable_count / len(steps)))


def _is_likely_actionable(title: str) -> bool:
    """Heuristic: does this heading title look like an implementation task?"""
    title_lower = title.lower()
    words = set(title_lower.split())

    # Definite actionable: starts with a verb
    if words & _ACTIONABLE_KEYWORDS:
        return True

    # Definite actionable: matches Phase/Step pattern
    if STEP_HEADING_PATTERN.match(title):
        return True

    # Check for verb phrases common in task titles
    verb_patterns = [
        r"^(?:create|add|implement|update|fix|refactor|migrate|convert|build|write|modify|remove|replace|move|setup|configure|deploy|test|verify|validate)\b",
        r"^(?:set up|clean up|wire up|hook up|plug in|roll out)\b",
    ]
    for pattern in verb_patterns:
        if re.match(pattern, title_lower):
            return True

    return False


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
