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
    # Strategy/approach sections that describe HOW but aren't actionable steps
    "strategy", "approach", "methodology", "rationale",
    "comparison", "alternatives", "trade-offs", "tradeoffs",
    "evaluation", "analysis", "findings", "observations",
    "recommendations", "best practices", "patterns",
    # Miscellaneous informational sections
    "changelog", "version history", "release notes",
    "known issues", "limitations", "caveats",
    "faq", "troubleshooting", "debugging",
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
    "strategy", "approach", "analysis", "comparison", "alternatives",
    "rationale", "evaluation", "findings", "known", "limitations",
}

# Keywords in headings that strongly suggest actionable implementation content.
_ACTIONABLE_KEYWORDS = {
    "create", "add", "implement", "update", "fix", "refactor", "migrate",
    "convert", "build", "write", "modify", "remove", "replace", "move",
    "setup", "configure", "deploy", "test", "verify", "validate",
    "phase", "step", "foundation", "integrate",
}


@dataclass
class PlanQualityReport:
    """Quality assessment of a parsed plan document."""

    is_design_doc: bool
    is_implementation_plan: bool
    quality_score: float  # 0.0–1.0
    total_sections: int
    actionable_sections: int
    filtered_sections: int
    actionable_ratio: float
    warnings: list[str] = field(default_factory=list)
    recommendation: str = ""

    @property
    def is_suitable_for_splitting(self) -> bool:
        return (
            self.is_implementation_plan
            and self.quality_score >= 0.3
            and self.actionable_sections >= 1
        )


@dataclass
class PlanStep:
    """A single actionable step extracted from an implementation plan."""

    title: str
    description: str
    priority_hint: int = 0  # 0-based index in the original plan (for ordering)
    raw_title: str = ""  # original heading text before cleaning


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
    """Extract phase-level steps from a dedicated implementation section.

    Many complex plans are structured as design documents with a dedicated
    ``## Implementation Plan`` (or similar) section.  This parser locates
    that section and extracts its ``###`` sub-headings as steps.

    When the implementation section contains more than
    ``_IMPL_PHASE_THRESHOLD`` sub-headings **and** those sub-headings can
    be grouped under higher-level phase labels (detected via numbered
    prefixes like "Phase 1.1", "Phase 1.2", or sequential numbering), the
    parser automatically consolidates them into fewer, coarser phases.

    This prevents informational/reference sections (capabilities, color
    palettes, risk assessments, etc.) from being extracted as tasks.

    Returns an empty list if no implementation section is found.
    """
    heading_pattern = re.compile(r"^(#{2,4})\s+(.+)$", re.MULTILINE)
    fence_ranges = _fenced_code_ranges(content)
    matches = [
        m for m in heading_pattern.finditer(content)
        if not _is_inside_code_fence(m.start(), fence_ranges)
    ]

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

    # Collect ### sub-headings within the implementation section
    sub_headings: list[tuple[int, re.Match]] = []  # (index_in_matches, match)
    for i in range(impl_start + 1, impl_end):
        match = matches[i]
        level = len(match.group(1))
        if level == 3:
            sub_headings.append((i, match))

    if not sub_headings:
        return []

    # Build raw steps from ### headings (each step includes everything
    # up to the next ### heading or end of the implementation section).
    raw_steps: list[PlanStep] = []
    for idx, (i, match) in enumerate(sub_headings):
        title = _clean_step_title(match.group(2).strip())
        if not title:
            continue
        if title.lower() in NON_ACTIONABLE_HEADINGS:
            continue

        start = match.end()
        if idx + 1 < len(sub_headings):
            end = sub_headings[idx + 1][1].start()
        else:
            # End at the next ## heading or end of content
            if impl_end < len(matches):
                end = matches[impl_end].start()
            else:
                end = len(content)
        body = content[start:end].strip()

        if len(body) < 20:
            continue

        raw_steps.append(PlanStep(
            title=title,
            description=body,
            priority_hint=len(raw_steps),
            raw_title=match.group(2).strip(),
        ))

    # If we got a reasonable number of steps, return them directly.
    if len(raw_steps) <= _IMPL_PHASE_THRESHOLD:
        return raw_steps

    # Too many fine-grained steps — consolidate into coarser phases.
    return _consolidate_steps_into_phases(raw_steps)


# Maximum number of steps from an implementation section before we
# attempt automatic consolidation into larger phases.
_IMPL_PHASE_THRESHOLD = 4


def _consolidate_steps_into_phases(
    steps: list[PlanStep],
    target_phases: int = 3,
) -> list[PlanStep]:
    """Merge a long list of fine-grained steps into fewer phases.

    Uses a simple chunking strategy: divides the ordered step list into
    ``target_phases`` roughly-equal groups.  Each group becomes a single
    phase whose title is derived from the first step and whose description
    includes a high-level outline of steps followed by the detailed
    descriptions of all contained steps.
    """
    if len(steps) <= target_phases:
        return steps

    chunk_size = max(1, (len(steps) + target_phases - 1) // target_phases)
    phases: list[PlanStep] = []

    for chunk_start in range(0, len(steps), chunk_size):
        chunk = steps[chunk_start : chunk_start + chunk_size]
        if not chunk:
            continue

        # Build a high-level outline of steps in this phase
        outline_parts: list[str] = []
        outline_parts.append("**Steps in this phase:**")
        for i, sub in enumerate(chunk, 1):
            outline_parts.append(f"{i}. {sub.title}")
        outline = "\n".join(outline_parts)

        # Build detailed descriptions for each sub-step
        detail_parts: list[str] = []
        for sub in chunk:
            detail_parts.append(f"### {sub.title}\n\n{sub.description}")
        details = "\n\n".join(detail_parts)

        combined_desc = f"{outline}\n\n---\n\n{details}"

        # Title: use the first step's title, augmented if there are multiple
        if len(chunk) == 1:
            phase_title = chunk[0].title
        else:
            phase_title = (
                f"{chunk[0].title} (and {len(chunk) - 1} related "
                f"{'step' if len(chunk) - 1 == 1 else 'steps'})"
            )

        phases.append(PlanStep(
            title=phase_title,
            description=combined_desc,
            priority_hint=len(phases),
            raw_title=chunk[0].raw_title,
        ))

    return phases


def _parse_heading_sections(content: str) -> list[PlanStep]:
    """Parse sections delimited by ## or ### headings.

    Prefers phase-level (``##``) headings when the document contains
    a mix of ``##`` and ``###`` headings.  In that case each ``##``
    becomes a single step whose description includes all ``###``
    sub-content, producing fewer but more substantial subtasks.

    Falls back to ``###``-level splitting only when there are no
    actionable ``##`` headings (i.e. the entire plan is written at
    the ``###`` level).
    """
    heading_pattern = re.compile(r"^(#{2,3})\s+(.+)$", re.MULTILINE)
    fence_ranges = _fenced_code_ranges(content)
    matches = [
        m for m in heading_pattern.finditer(content)
        if not _is_inside_code_fence(m.start(), fence_ranges)
    ]

    if not matches:
        return []

    # Determine whether we have actionable ## headings — if so, use
    # them as the primary split level and fold ### content in.
    h2_matches = [m for m in matches if len(m.group(1)) == 2]
    actionable_h2 = [
        m for m in h2_matches
        if _clean_step_title(m.group(2).strip()).lower() not in NON_ACTIONABLE_HEADINGS
        and _clean_step_title(m.group(2).strip())
    ]

    if actionable_h2:
        return _parse_at_heading_level(content, matches, level=2)

    # No actionable ## headings — fall back to ### level
    return _parse_at_heading_level(content, matches, level=3)


def _parse_at_heading_level(
    content: str,
    matches: list[re.Match],
    level: int,
) -> list[PlanStep]:
    """Extract steps from headings at the given ``level`` (2 or 3).

    Content under sub-headings (deeper than ``level``) is included in
    the parent step's description rather than spawning separate steps.
    """
    # Collect only the headings at the target level
    level_matches = [m for m in matches if len(m.group(1)) == level]

    if not level_matches:
        return []

    steps: list[PlanStep] = []
    for i, match in enumerate(level_matches):
        title = match.group(2).strip()
        title = _clean_step_title(title)

        if not title:
            continue

        if title.lower() in NON_ACTIONABLE_HEADINGS:
            continue

        # Body extends to the next same-level heading (or end of file)
        start = match.end()
        if i + 1 < len(level_matches):
            end = level_matches[i + 1].start()
        else:
            end = len(content)
        body = content[start:end].strip()

        if len(body) < 20:
            continue

        steps.append(PlanStep(
            title=title,
            description=body,
            priority_hint=len(steps),
            raw_title=match.group(2).strip(),
        ))

    return steps


def _parse_numbered_list(content: str) -> list[PlanStep]:
    """Parse top-level numbered items, consolidating into phases when needed.

    If the plan contains more than ``_IMPL_PHASE_THRESHOLD`` numbered
    items, they are automatically consolidated into fewer phases using the
    same chunking strategy as ``_consolidate_steps_into_phases``.
    """
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

    # Consolidate into phases if there are too many individual items
    if len(steps) > _IMPL_PHASE_THRESHOLD:
        steps = _consolidate_steps_into_phases(steps)

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


_FENCED_CODE_BLOCK_RE = re.compile(
    r"^`{3,}[^\n]*\n.*?^`{3,}[^\n]*$",
    re.MULTILINE | re.DOTALL,
)


def _fenced_code_ranges(content: str) -> list[tuple[int, int]]:
    """Return (start, end) byte ranges of fenced code blocks in *content*."""
    return [(m.start(), m.end()) for m in _FENCED_CODE_BLOCK_RE.finditer(content)]


def _is_inside_code_fence(pos: int, ranges: list[tuple[int, int]]) -> bool:
    """Check whether *pos* falls inside any fenced code block range."""
    for start, end in ranges:
        if start <= pos < end:
            return True
    return False


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

    When the step description contains multiple sub-steps (e.g. from
    consolidation or a well-structured phase), a high-level outline
    is extracted and presented prominently so the agent can see all
    the work in the phase at a glance.

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

    # Extract a high-level outline of steps if the description contains
    # sub-step structure (### headings or numbered items).
    outline = _extract_steps_outline(step.description)
    if outline:
        parts.append(f"## High-Level Steps\n{outline}\n")

    parts.append(f"## Task Details\n{step.description}")

    return "\n".join(parts)


def _extract_steps_outline(description: str) -> str:
    """Extract a bullet-point outline from a step description.

    Looks for ### sub-headings or a leading numbered list and produces
    a compact outline.  Returns an empty string if no sub-structure is
    found.
    """
    # If the description already contains a "Steps in this phase:" outline
    # (from consolidation), don't duplicate it
    if "**Steps in this phase:**" in description:
        return ""

    # Try ### sub-headings first
    sub_heading_pattern = re.compile(r"^###\s+(.+)$", re.MULTILINE)
    sub_headings = sub_heading_pattern.findall(description)
    if len(sub_headings) >= 2:
        lines = [f"- {_clean_step_title(h.strip())}" for h in sub_headings]
        return "\n".join(lines)

    # Try numbered items (top-level only)
    numbered_pattern = re.compile(r"^\d+[.)]\s+(.+)$", re.MULTILINE)
    items = numbered_pattern.findall(description)
    if len(items) >= 2:
        lines = [f"- {_clean_step_title(item.strip())}" for item in items[:10]]
        return "\n".join(lines)

    return ""


# ---------------------------------------------------------------------------
# Plan quality validation and orchestrator integration
# ---------------------------------------------------------------------------

def validate_plan_quality(content: str) -> PlanQualityReport:
    """Assess the quality of a plan document for task splitting.

    Analyzes the markdown content to determine whether it is an actionable
    implementation plan or a design/reference document, and scores it
    accordingly.
    """
    if not content.strip():
        return PlanQualityReport(
            is_design_doc=False,
            is_implementation_plan=False,
            quality_score=0.0,
            total_sections=0,
            actionable_sections=0,
            filtered_sections=0,
            actionable_ratio=0.0,
            warnings=[],
            recommendation="Empty document — nothing to split.",
        )

    # Extract all ## and ### headings (skip those inside code fences)
    heading_pattern = re.compile(r"^#{2,3}\s+(.+)$", re.MULTILINE)
    fence_ranges = _fenced_code_ranges(content)
    headings = [
        m.group(1).strip() for m in heading_pattern.finditer(content)
        if not _is_inside_code_fence(m.start(), fence_ranges)
    ]
    total_sections = len(headings)

    if total_sections == 0:
        return PlanQualityReport(
            is_design_doc=False,
            is_implementation_plan=False,
            quality_score=0.0,
            total_sections=0,
            actionable_sections=0,
            filtered_sections=0,
            actionable_ratio=0.0,
            warnings=[],
            recommendation="No headings found — cannot split into tasks.",
        )

    # Classify each heading
    actionable_count = 0
    design_indicators = 0
    for heading in headings:
        clean = _clean_step_title(heading)
        clean_lower = clean.lower()

        if clean_lower in NON_ACTIONABLE_HEADINGS:
            design_indicators += 1
            continue

        if STEP_HEADING_PATTERN.match(clean) or _is_likely_actionable(clean):
            actionable_count += 1
        else:
            # Check for informational keywords
            words = set(clean_lower.split())
            if words & _INFORMATIONAL_KEYWORDS:
                design_indicators += 1

    filtered = total_sections - actionable_count
    actionable_ratio = round(actionable_count / total_sections, 2) if total_sections else 0.0

    # Determine document type
    is_design_doc = design_indicators > actionable_count and actionable_count <= 2
    is_implementation_plan = actionable_count >= 2 or (
        actionable_count >= 1 and actionable_ratio >= 0.3
    )

    # Quality score: proportion of actionable sections, boosted by step patterns
    quality_score = actionable_ratio
    if any(STEP_HEADING_PATTERN.match(_clean_step_title(h)) for h in headings):
        quality_score = min(1.0, quality_score + 0.2)

    # Warnings
    warnings: list[str] = []
    if is_design_doc:
        warnings.append(
            "Document appears to be a design/reference document rather than "
            "an implementation plan."
        )
    if actionable_count > 15:
        warnings.append(
            f"High step count ({actionable_count}) — consider consolidating "
            f"related steps."
        )

    # Recommendation
    if is_design_doc:
        recommendation = (
            "This looks like a design document. Consider extracting only the "
            "implementation sections into a separate plan."
        )
    elif is_implementation_plan and quality_score >= 0.5:
        recommendation = "Good implementation plan — suitable for automatic task splitting."
    elif is_implementation_plan:
        recommendation = (
            "Implementation plan detected but quality is moderate. "
            "Review generated tasks for relevance."
        )
    else:
        recommendation = "Unable to identify clear implementation steps."

    return PlanQualityReport(
        is_design_doc=is_design_doc,
        is_implementation_plan=is_implementation_plan,
        quality_score=quality_score,
        total_sections=total_sections,
        actionable_sections=actionable_count,
        filtered_sections=filtered,
        actionable_ratio=actionable_ratio,
        warnings=warnings,
        recommendation=recommendation,
    )


def parse_and_generate_steps(
    content: str,
    *,
    max_steps: int = 20,
    enforce_quality: bool = False,
    min_quality_score: float = 0.3,
) -> tuple[list[dict], PlanQualityReport]:
    """Parse a plan document and return steps as dicts with a quality report.

    This is the main orchestrator integration point.  It combines
    ``parse_plan()`` with ``validate_plan_quality()`` and applies an
    additional post-filter to remove any remaining non-actionable steps.

    Args:
        content: Raw markdown content.
        max_steps: Maximum number of steps to return.
        enforce_quality: If True, return no steps when quality is below threshold.
        min_quality_score: Minimum quality score (used when enforce_quality=True).

    Returns:
        A tuple of (steps, quality_report) where each step is a dict with
        ``title`` and ``description`` keys.
    """
    quality = validate_plan_quality(content)

    if enforce_quality and quality.quality_score < min_quality_score:
        return [], quality

    parsed = parse_plan(content, max_steps=max_steps)

    # Post-filter: remove any remaining non-actionable steps
    filtered_steps: list[dict] = []
    for step in parsed.steps:
        title_lower = step.title.lower()
        if title_lower in NON_ACTIONABLE_HEADINGS:
            continue
        # Use raw_title (original heading) when available for fidelity
        title = step.raw_title if step.raw_title else step.title
        filtered_steps.append({
            "title": title,
            "description": step.description,
        })

    # Enforce safety cap
    filtered_steps = filtered_steps[:max_steps]

    return filtered_steps, quality
