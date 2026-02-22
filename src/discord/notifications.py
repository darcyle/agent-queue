from __future__ import annotations

from src.models import Task, Agent, AgentOutput

# ---------------------------------------------------------------------------
# Error classification helpers
# ---------------------------------------------------------------------------

# Maps error subtype/keyword → (short label, fix suggestion)
_ERROR_PATTERNS: list[tuple[str, str, str]] = [
    # (keyword to search in lowercased error, label, suggestion)
    (
        "error_max_structured_output_retries",
        "Structured-output failure",
        "The model couldn't produce valid JSON output after 7 tries. "
        "Simplify the task description or remove JSON-schema constraints.",
    ),
    (
        "auth",
        "Authentication error",
        "Check that ANTHROPIC_API_KEY (or claude login) is valid and not expired.",
    ),
    (
        "authentication",
        "Authentication error",
        "Check that ANTHROPIC_API_KEY (or claude login) is valid and not expired.",
    ),
    (
        "rate_limit",
        "Rate-limit",
        "The API rate limit was hit. The task will be retried automatically.",
    ),
    (
        "rate limit",
        "Rate-limit",
        "The API rate limit was hit. The task will be retried automatically.",
    ),
    (
        "429",
        "Rate-limit",
        "The API rate limit was hit. The task will be retried automatically.",
    ),
    (
        "quota",
        "Token quota exhausted",
        "Daily or session token quota exceeded. Wait for quota reset or increase limits.",
    ),
    (
        "token",
        "Token limit",
        "The context window or token budget was exceeded. Break the task into smaller pieces.",
    ),
    (
        "timeout",
        "Timeout",
        "The agent exceeded the stuck-timeout. Increase stuck_timeout_seconds or simplify the task.",
    ),
    (
        "config",
        "Configuration error",
        "A config value is invalid. Check model name, allowed_tools, and MCP server settings.",
    ),
    (
        "mcp",
        "MCP server error",
        "An MCP server failed. Verify MCP server configs in the task context.",
    ),
    (
        "permission",
        "Permission denied",
        "The agent couldn't access a file or directory. Check workspace permissions.",
    ),
    (
        "cancelled",
        "Cancelled",
        "The task was stopped manually.",
    ),
]


def classify_error(error_message: str | None) -> tuple[str, str]:
    """Return (error_type_label, fix_suggestion) for a given error message.

    Falls back to a generic label when no pattern matches.
    """
    if not error_message:
        return "Unknown error", "Check daemon logs for details."
    lowered = error_message.lower()
    for keyword, label, suggestion in _ERROR_PATTERNS:
        if keyword.lower() in lowered:
            return label, suggestion
    return "Unexpected error", "Check daemon logs (`~/.agent-queue/daemon.log`) for full details."


def format_task_completed(task: Task, agent: Agent, output: AgentOutput) -> str:
    lines = [
        f"**Task Completed:** `{task.id}` — {task.title}",
        f"Agent: {agent.name}",
        f"Tokens used: {output.tokens_used:,}",
    ]
    if output.summary:
        lines.append(f"Summary: {output.summary}")
    if output.files_changed:
        lines.append(f"Files changed: {', '.join(output.files_changed)}")
    return "\n".join(lines)


def format_task_failed(task: Task, agent: Agent, output: AgentOutput) -> str:
    error_type, suggestion = classify_error(output.error_message)
    lines = [
        f"**Task Failed:** `{task.id}` — {task.title}",
        f"Agent: {agent.name} | Retry: {task.retry_count}/{task.max_retries}",
        f"Error type: **{error_type}**",
    ]
    if output.error_message:
        # Show first 300 chars of the error — enough to diagnose without flooding Discord
        snippet = output.error_message[:300]
        if len(output.error_message) > 300:
            snippet += "…"
        lines.append(f"```\n{snippet}\n```")
    lines.append(f"💡 {suggestion}")
    lines.append(f"_Use `/agent-error {task.id}` for the full error log._")
    return "\n".join(lines)


def format_task_blocked(task: Task, last_error: str | None = None) -> str:
    lines = [
        f"**Task Blocked:** `{task.id}` — {task.title}",
        f"Max retries ({task.max_retries}) exhausted. Manual intervention required.",
    ]
    if last_error:
        error_type, suggestion = classify_error(last_error)
        lines.append(f"Last error type: **{error_type}**")
        lines.append(f"💡 {suggestion}")
    lines.append(f"_Use `/agent-error {task.id}` to inspect the last error._")
    return "\n".join(lines)


def format_pr_created(task: Task, pr_url: str) -> str:
    return (
        f"**PR Created:** `{task.id}` — {task.title}\n"
        f"Review and merge to complete: {pr_url}\n"
        f"Status: AWAITING_APPROVAL"
    )


def format_agent_question(task: Task, agent: Agent, question: str) -> str:
    return (
        f"**Agent Question:** `{task.id}` — {task.title}\n"
        f"Agent {agent.name} asks:\n> {question[:500]}"
    )


def format_budget_warning(project_name: str, usage: int, limit: int) -> str:
    pct = (usage / limit * 100) if limit > 0 else 0
    return (
        f"**Budget Warning:** Project **{project_name}** at {pct:.0f}% "
        f"({usage:,} / {limit:,} tokens)"
    )
