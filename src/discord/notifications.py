from __future__ import annotations

from src.models import Task, Agent, AgentOutput


def format_task_completed(task: Task, agent: Agent, output: AgentOutput) -> str:
    lines = [
        f"**Task Completed:** `{task.id}` — {task.title}",
        f"Agent: {agent.name}",
        f"Tokens used: {output.tokens_used:,}",
    ]
    if output.summary:
        lines.append(f"Summary: {output.summary[:200]}")
    if output.files_changed:
        lines.append(f"Files changed: {', '.join(output.files_changed[:10])}")
    return "\n".join(lines)


def format_task_failed(task: Task, agent: Agent, output: AgentOutput) -> str:
    lines = [
        f"**Task Failed:** `{task.id}` — {task.title}",
        f"Agent: {agent.name}",
        f"Retry: {task.retry_count}/{task.max_retries}",
    ]
    if output.error_message:
        lines.append(f"Error: {output.error_message[:200]}")
    return "\n".join(lines)


def format_task_blocked(task: Task) -> str:
    return (
        f"**Task Blocked:** `{task.id}` — {task.title}\n"
        f"Max retries ({task.max_retries}) exhausted. Manual intervention required."
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
