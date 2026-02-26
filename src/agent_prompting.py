"""Agent prompting layer for plan-aware task execution.

This module handles Layer 3 of the multi-layer task splitting fix:
generating context-aware prompts for agents that produce well-structured
plans suitable for automatic parsing and task generation.

The prompting strategy addresses the root cause of over-parsed design
documents by guiding agents to:
  1. Produce focused implementation plans, not design documents
  2. Use clear action-verb headings that the parser can classify
  3. Structure plans with numbered phases under an Implementation section
  4. Include effort estimates and file change details
  5. Keep plans appropriately scoped for the task's depth level

Key insight: the best fix for over-parsing is preventing the problem
at the source. If agents produce well-structured implementation plans
instead of sprawling design documents, the parser's job is trivial.

Prompt hierarchy:
  - Root tasks: Can produce implementation plans with subtask generation
  - Depth-1 subtasks: Focused execution with optional sub-splitting
  - Max-depth subtasks: Pure execution, no plan generation allowed
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class PromptConfig:
    """Configuration for agent prompt generation.

    Parameters
    ----------
    max_plan_depth : int
        Maximum depth of recursive plan generation. Should match the
        discovery config's max_plan_depth.
    max_steps_per_plan : int
        Maximum steps a plan should produce. Communicated to the agent
        as a guideline, enforced by the parser as a hard cap.
    project_context : str
        Brief description of the project for agent context.
    """

    max_plan_depth: int = 2
    max_steps_per_plan: int = 5
    project_context: str = ""


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

# The plan structure guide is included in prompts for tasks that are
# expected to produce implementation plans. It teaches agents to write
# plans that the parser can reliably extract actionable steps from.
PLAN_STRUCTURE_GUIDE = """\
## Plan Structure Requirements

When producing an implementation plan, follow these formatting rules
to ensure the plan can be automatically parsed into subtasks.

IMPORTANT: Each phase becomes a separate subtask executed by an agent.
Phases should be COARSE — each one should represent a substantial chunk
of work containing MANY concrete steps. Do NOT create a separate
phase for every small action (e.g. "create file X", "add import Y").
Instead, group related work into 2-4 broad phases. Fewer, larger
phases are ALWAYS preferred.

### DO:
- Use action-verb headings: "## Implement X", "## Create Y", "## Add Z"
- Use numbered phases: "## Phase 1: Database Layer and Migrations"
- Group related work AGGRESSIVELY: a phase like "Build API Endpoints"
  should include creating routes, adding validation, writing handlers,
  AND writing tests for those endpoints — all in one phase
- Include a numbered outline of ALL concrete steps within each phase
  (so the agent can see the full scope of work at a glance)
- Include detailed descriptions for each step after the outline
- Include estimated effort for each phase
- Put implementation phases under a "## Implementation Plan" container heading
- Keep the total number of phases between 2 and {max_steps} (aim for 2-4)
- Each phase should be independently executable and represent several
  hours of focused work

### DON'T:
- Don't create more than 4 phases unless the work is truly enormous
- Don't create a separate phase for each individual file change or function
- Don't include overview/summary sections as separate headings
- Don't include design discussion, architecture review, or rationale sections
- Don't include reference material (file change summaries, API specs, examples)
- Don't include sections labeled "Future Work", "Out of Scope", or "Background"
- Don't produce more than {max_steps} implementation phases
- Don't write a design document when asked to implement something

### Ideal Plan Structure:

```markdown
# Implementation Plan: [Feature Name]

## Implementation Plan

### Phase 1: [Action Verb] [Broad Area of Work]

[High-level description of this phase]

Steps in this phase:
1. [Step within this phase]
2. [Another step within this phase]
3. [Yet another step]
4. [More steps as needed]

[Detailed description for step 1]

[Detailed description for step 2]

...

Files to modify/create:
- `path/to/file1.py`
- `path/to/file2.py`

**Estimated effort:** ~N hours

### Phase 2: [Action Verb] [Broad Area of Work]

[Description covering multiple related changes]

Steps in this phase:
1. [Step]
2. [Step]
3. [Step]
4. [Step]

[Detailed descriptions for each step]

**Estimated effort:** ~N hours
```
"""

# Execution focus instructions for subtasks that should execute,
# not generate more plans.
EXECUTION_FOCUS_INSTRUCTIONS = """\
## Execution Focus

This task is a subtask generated from a parent plan. Your job is to
**execute** the implementation described below, not to create another plan.

Guidelines:
- Implement the code changes directly
- Write tests for your changes
- Do not produce a new implementation plan document
- Do not create additional plan files
- Focus on completing this specific task fully
- If the task scope is too large, implement the most critical parts
  and note what remains in code comments
"""

# Depth-aware instructions for subtasks that CAN generate sub-plans
# but should do so judiciously.
CONTROLLED_SPLITTING_INSTRUCTIONS = """\
## Task Execution with Optional Sub-Planning

This task was generated from a parent plan (depth {current_depth}/{max_depth}).
You may either:

1. **Execute directly** — Implement the changes described below.
   This is strongly preferred for well-scoped tasks.

2. **Create a focused sub-plan** — Only if the task is genuinely too
   large for a single agent session. If you do, follow the plan
   structure requirements carefully.

Guidelines:
- Strongly prefer direct execution over sub-planning
- If sub-planning, limit to 2-3 coarse phases that each group many
  related steps together
- Do NOT create fine-grained phases for individual changes
- Do NOT produce design documents or architecture reviews
- Do NOT include background, overview, or rationale sections
- Every heading in your plan must start with an action verb
"""


# ---------------------------------------------------------------------------
# Task context building
# ---------------------------------------------------------------------------

@dataclass
class TaskContext:
    """Context about a task for prompt generation."""

    task_id: str = ""
    title: str = ""
    description: str = ""
    project_id: str = ""
    is_plan_subtask: bool = False
    plan_depth: int = 0
    parent_title: str = ""
    parent_description: str = ""
    dependency_titles: list[str] = field(default_factory=list)
    workspace_path: str = ""
    files_in_scope: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def build_plan_generation_prompt(
    task: TaskContext,
    config: PromptConfig | None = None,
) -> str:
    """Build a prompt for a root task that should produce an implementation plan.

    This is used for tasks at depth 0 that are expected to analyze the
    codebase and produce a structured plan document for splitting into
    subtasks.

    Parameters
    ----------
    task : TaskContext
        Context about the task.
    config : PromptConfig or None
        Prompt configuration.

    Returns
    -------
    str
        The complete prompt for the agent.
    """
    config = config or PromptConfig()

    parts: list[str] = []

    # Task header
    parts.append(f"# Task: {task.title}\n")

    if task.description:
        parts.append(f"{task.description}\n")

    # Project context
    if config.project_context:
        parts.append(f"## Project Context\n\n{config.project_context}\n")

    # Plan structure guide
    guide = PLAN_STRUCTURE_GUIDE.replace("{max_steps}", str(config.max_steps_per_plan))
    parts.append(guide)

    # Dependencies context
    if task.dependency_titles:
        parts.append("## Completed Dependencies\n")
        for dep_title in task.dependency_titles:
            parts.append(f"- {dep_title}")
        parts.append("")

    # Files in scope
    if task.files_in_scope:
        parts.append("## Files in Scope\n")
        for f in task.files_in_scope:
            parts.append(f"- `{f}`")
        parts.append("")

    # Final instruction
    parts.append(
        "## Your Task\n\n"
        "Analyze the codebase and produce a focused implementation plan "
        f"with 2-{min(4, config.max_steps_per_plan)} actionable phases. "
        "Each phase should be independently executable and represent a "
        "substantial chunk of work containing MANY concrete steps. "
        "Do NOT create a separate phase for each small change — group "
        "related work together aggressively.\n"
        "Each phase MUST include a numbered outline of all steps within it "
        "so the executing agent can see the full scope of work at a glance.\n"
        "Write the plan as a markdown document with the structure shown above.\n"
    )

    return "\n".join(parts)


def build_execution_prompt(
    task: TaskContext,
    config: PromptConfig | None = None,
) -> str:
    """Build a prompt for a task that should execute directly.

    This is used for subtasks at maximum depth that should NOT produce
    further plans, or for any task explicitly marked for execution.

    Parameters
    ----------
    task : TaskContext
        Context about the task.
    config : PromptConfig or None
        Prompt configuration.

    Returns
    -------
    str
        The complete prompt for the agent.
    """
    config = config or PromptConfig()

    parts: list[str] = []

    # Task header
    parts.append(f"# Task: {task.title}\n")

    # Execution focus
    parts.append(EXECUTION_FOCUS_INSTRUCTIONS)

    # Task details
    if task.description:
        parts.append(f"## Task Details\n\n{task.description}\n")

    # Parent context
    if task.parent_title:
        parts.append(
            f"## Parent Task Context\n\n"
            f"This task is part of: **{task.parent_title}**\n"
        )
        if task.parent_description:
            # Truncate parent description to avoid prompt bloat
            desc = task.parent_description
            if len(desc) > 500:
                desc = desc[:500] + "..."
            parts.append(f"{desc}\n")

    # Dependencies context
    if task.dependency_titles:
        parts.append("## Completed Dependencies\n")
        for dep_title in task.dependency_titles:
            parts.append(f"- {dep_title}")
        parts.append("")

    # Files in scope
    if task.files_in_scope:
        parts.append("## Files to Modify\n")
        for f in task.files_in_scope:
            parts.append(f"- `{f}`")
        parts.append("")

    # Project context
    if config.project_context:
        parts.append(f"## Project Context\n\n{config.project_context}\n")

    return "\n".join(parts)


def build_subtask_prompt(
    task: TaskContext,
    config: PromptConfig | None = None,
) -> str:
    """Build a prompt for a plan subtask with depth-aware instructions.

    This is the main prompt builder for subtasks generated from plans.
    It automatically selects between execution-only and controlled-splitting
    modes based on the task's plan depth.

    Parameters
    ----------
    task : TaskContext
        Context about the task, including plan_depth.
    config : PromptConfig or None
        Prompt configuration.

    Returns
    -------
    str
        The complete prompt for the agent.
    """
    config = config or PromptConfig()

    # At max depth: execution only
    if task.plan_depth >= config.max_plan_depth:
        return build_execution_prompt(task, config)

    # Below max depth: controlled splitting allowed
    parts: list[str] = []

    # Task header
    parts.append(f"# Task: {task.title}\n")

    # Depth-aware instructions
    splitting_instructions = CONTROLLED_SPLITTING_INSTRUCTIONS.replace(
        "{current_depth}", str(task.plan_depth)
    ).replace(
        "{max_depth}", str(config.max_plan_depth)
    )
    parts.append(splitting_instructions)

    # Task details
    if task.description:
        parts.append(f"## Task Details\n\n{task.description}\n")

    # Parent context
    if task.parent_title:
        parts.append(
            f"## Parent Task Context\n\n"
            f"This task is part of: **{task.parent_title}**\n"
        )

    # If sub-planning is allowed, include the structure guide
    if task.plan_depth < config.max_plan_depth:
        guide = PLAN_STRUCTURE_GUIDE.replace(
            "{max_steps}", str(min(5, config.max_steps_per_plan))
        )
        parts.append(guide)

    # Dependencies context
    if task.dependency_titles:
        parts.append("## Completed Dependencies\n")
        for dep_title in task.dependency_titles:
            parts.append(f"- {dep_title}")
        parts.append("")

    # Files in scope
    if task.files_in_scope:
        parts.append("## Files in Scope\n")
        for f in task.files_in_scope:
            parts.append(f"- `{f}`")
        parts.append("")

    # Project context
    if config.project_context:
        parts.append(f"## Project Context\n\n{config.project_context}\n")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Prompt selection (convenience)
# ---------------------------------------------------------------------------

def select_prompt(
    task: TaskContext,
    config: PromptConfig | None = None,
) -> str:
    """Automatically select and build the appropriate prompt for a task.

    This is the recommended entry point for prompt generation. It inspects
    the task context and selects the right prompt builder:

    - Root tasks (depth 0, not a plan subtask) → plan generation prompt
    - Subtasks below max depth → controlled splitting prompt
    - Subtasks at max depth → execution-only prompt

    Parameters
    ----------
    task : TaskContext
        Context about the task.
    config : PromptConfig or None
        Prompt configuration.

    Returns
    -------
    str
        The complete prompt for the agent.
    """
    config = config or PromptConfig()

    if not task.is_plan_subtask:
        # Root task — can produce plans
        return build_plan_generation_prompt(task, config)

    # Plan subtask — depth-aware prompt selection
    return build_subtask_prompt(task, config)


# ---------------------------------------------------------------------------
# Anti-pattern detection in agent output
# ---------------------------------------------------------------------------

_DESIGN_DOC_SIGNALS = [
    "executive summary",
    "architecture review",
    "current state analysis",
    "design principles",
    "trade-offs",
    "alternatives considered",
    "visual design specification",
    "risk assessment",
]


def detect_design_doc_output(content: str) -> tuple[bool, list[str]]:
    """Detect if agent output is a design document instead of a plan.

    This post-hoc check identifies when an agent produced a design
    document despite being prompted for an implementation plan. This
    information can be used to:
    1. Warn the operator
    2. Re-prompt the agent with stricter instructions
    3. Apply more aggressive filtering before task generation

    Parameters
    ----------
    content : str
        The agent's output content.

    Returns
    -------
    tuple[bool, list[str]]
        (is_design_doc, signals_found)
    """
    content_lower = content.lower()
    signals_found: list[str] = []

    for signal in _DESIGN_DOC_SIGNALS:
        if signal in content_lower:
            signals_found.append(signal)

    # A document with 3+ design doc signals is likely a design doc
    is_design_doc = len(signals_found) >= 3

    return is_design_doc, signals_found


def build_retry_prompt(
    task: TaskContext,
    original_output: str,
    design_signals: list[str],
    config: PromptConfig | None = None,
) -> str:
    """Build a retry prompt when an agent produced a design doc instead of a plan.

    Parameters
    ----------
    task : TaskContext
        Original task context.
    original_output : str
        The agent's original output (the design doc).
    design_signals : list[str]
        Design doc signals detected in the output.
    config : PromptConfig or None
        Prompt configuration.

    Returns
    -------
    str
        A retry prompt with stricter instructions.
    """
    config = config or PromptConfig()

    parts: list[str] = []

    parts.append(f"# Task: {task.title} (RETRY — Implementation Plan Needed)\n")

    parts.append(
        "## Important: Previous Output Was a Design Document\n\n"
        "Your previous response was a design document rather than an "
        "implementation plan. It contained the following non-actionable "
        "sections that cannot be converted to tasks:\n"
    )
    for signal in design_signals:
        parts.append(f"- {signal}")
    parts.append("")

    parts.append(
        "**Please produce a focused IMPLEMENTATION PLAN instead.**\n\n"
        "An implementation plan contains ONLY actionable phases that "
        "describe what code to write, not design discussions or "
        "architecture reviews.\n"
    )

    # Include the plan structure guide with emphasis
    guide = PLAN_STRUCTURE_GUIDE.replace("{max_steps}", str(config.max_steps_per_plan))
    parts.append(guide)

    # Original task description
    if task.description:
        parts.append(f"## Original Task Description\n\n{task.description}\n")

    parts.append(
        "## Your Task (Retry)\n\n"
        "Produce a focused implementation plan with 2-4 coarse phases. "
        "Each phase should group MANY related steps into one substantial "
        "chunk of work. Every phase heading MUST start with an action verb "
        "(Implement, Create, Add, Update, Fix, etc.). "
        "Each phase MUST include a numbered outline of all steps within it. "
        "Do NOT create a separate phase for each small change. "
        "Do NOT include overview, architecture, design, or reference sections.\n"
    )

    return "\n".join(parts)
