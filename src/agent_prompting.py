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

from src.prompt_registry import registry


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
    max_steps_per_plan: int = 20
    project_context: str = ""


# ---------------------------------------------------------------------------
# Prompt templates — loaded from src/prompts/*.md via the prompt registry.
#
# The source of truth for each prompt is the corresponding .md file in
# src/prompts/.  These helper functions render them with the appropriate
# variables so that callers get a ready-to-use string.
# ---------------------------------------------------------------------------


def _render_plan_structure_guide(max_steps: int | str) -> str:
    """Render the plan structure guide with ``max_steps`` filled in."""
    return registry.render("plan-structure-guide", {"max_steps": str(max_steps)})


def _get_execution_focus() -> str:
    """Return the execution-focus instructions (no variables)."""
    return registry.get("execution-focus")


def _render_controlled_splitting(current_depth: int | str, max_depth: int | str) -> str:
    """Render the controlled-splitting instructions with depth info."""
    return registry.render("controlled-splitting", {
        "current_depth": str(current_depth),
        "max_depth": str(max_depth),
    })


# Backward-compatible module-level constants.  These load the template
# body from the registry (with ``{{var}}`` Mustache placeholders converted
# to ``{var}`` Python-format placeholders) so that existing code using
# ``PLAN_STRUCTURE_GUIDE.replace("{max_steps}", ...)`` keeps working.
def _compat_plan_structure_guide() -> str:
    body = registry.get("plan-structure-guide")
    return body.replace("{{max_steps}}", "{max_steps}")


def _compat_controlled_splitting() -> str:
    body = registry.get("controlled-splitting")
    return body.replace("{{current_depth}}", "{current_depth}").replace("{{max_depth}}", "{max_depth}")


# These are evaluated at first import.  For hot-reload, call
# ``registry.reload()`` and then re-import.
PLAN_STRUCTURE_GUIDE: str = _compat_plan_structure_guide()
EXECUTION_FOCUS_INSTRUCTIONS: str = _get_execution_focus()
CONTROLLED_SPLITTING_INSTRUCTIONS: str = _compat_controlled_splitting()


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
    parts.append(_render_plan_structure_guide(config.max_steps_per_plan))

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
    parts.append(_get_execution_focus())

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
    parts.append(_render_controlled_splitting(task.plan_depth, config.max_plan_depth))

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
        parts.append(_render_plan_structure_guide(min(5, config.max_steps_per_plan)))

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

    # Render the retry header from the template
    signal_list = "\n".join(f"- {s}" for s in design_signals)
    retry_header = registry.render("retry-design-doc", {
        "task_title": task.title,
        "signal_list": signal_list,
        "max_steps": str(config.max_steps_per_plan),
    })
    parts.append(retry_header)

    # Include the plan structure guide with emphasis
    parts.append(_render_plan_structure_guide(config.max_steps_per_plan))

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
