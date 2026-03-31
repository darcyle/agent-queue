"""MCP server for the agent-queue system.

Exposes agent-queue tasks, projects, agents, profiles, and workspaces as MCP
resources, and wraps CommandHandler operations as MCP tools. Runs on stdio
transport for integration with MCP-compatible clients (e.g. Claude Desktop).

Usage:
    python -m packages.mcp-server.mcp_server [--db PATH]

Or via the ``agent-queue-mcp`` entry point defined in pyproject.toml.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any

from mcp.server import FastMCP

# Add project root to path so we can import src modules
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.database import Database
from src.event_bus import EventBus
from src.models import (
    AgentState,
    ProjectStatus,
    TaskStatus,
)
from packages.mcp_server.mcp_interfaces import (  # noqa: E402
    agent_to_dict,
    profile_to_dict,
    project_to_dict,
    task_to_dict,
    workspace_to_dict,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

DEFAULT_DB_PATH = os.environ.get(
    "AGENT_QUEUE_DB",
    os.path.expanduser("~/.agent-queue/agent_queue.db"),
)


# ---------------------------------------------------------------------------
# Lifespan — initialize and tear down the database connection
# ---------------------------------------------------------------------------

@asynccontextmanager
async def server_lifespan(server: FastMCP):
    """Initialize database on startup, close on shutdown."""
    db_path = getattr(server, "_db_path", DEFAULT_DB_PATH)
    db = Database(db_path)
    await db.initialize()
    event_bus = EventBus()
    try:
        yield {"db": db, "event_bus": event_bus}
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Create the FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="agent-queue",
    instructions=(
        "Agent Queue MCP server. Provides access to task queue management, "
        "project configuration, agent monitoring, and workspace operations "
        "for the agent-queue orchestrator system."
    ),
    lifespan=server_lifespan,
)


# ---------------------------------------------------------------------------
# Helper to get database from context
# ---------------------------------------------------------------------------

async def _get_db() -> Database:
    """Retrieve the Database instance from the current MCP context."""
    ctx = mcp.get_context()
    return ctx.request_context.lifespan_context["db"]


async def _get_event_bus() -> EventBus:
    """Retrieve the EventBus instance from the current MCP context."""
    ctx = mcp.get_context()
    return ctx.request_context.lifespan_context["event_bus"]


# ===========================================================================
# RESOURCES
# ===========================================================================

# --- Tasks -----------------------------------------------------------------

@mcp.resource("agentqueue://tasks")
async def list_all_tasks() -> str:
    """List all active and recent tasks across all projects."""
    db = await _get_db()
    tasks = await db.list_tasks()
    return json.dumps([task_to_dict(t) for t in tasks], indent=2)


@mcp.resource("agentqueue://tasks/active")
async def list_active_tasks() -> str:
    """List all currently active tasks (IN_PROGRESS, ASSIGNED, READY)."""
    db = await _get_db()
    active_statuses = [TaskStatus.IN_PROGRESS, TaskStatus.ASSIGNED, TaskStatus.READY]
    all_tasks = await db.list_tasks()
    active = [t for t in all_tasks if t.status in active_statuses]
    return json.dumps([task_to_dict(t) for t in active], indent=2)


@mcp.resource("agentqueue://tasks/{task_id}")
async def get_task(task_id: str) -> str:
    """Get detailed information about a specific task."""
    db = await _get_db()
    task = await db.get_task(task_id)
    if not task:
        return json.dumps({"error": f"Task not found: {task_id}"})
    result = task_to_dict(task)
    # Include dependencies
    deps = await db.get_dependencies(task_id)
    result["dependencies"] = list(deps)
    # Include context entries
    contexts = await db.get_task_contexts(task_id)
    result["context"] = contexts
    return json.dumps(result, indent=2)


@mcp.resource("agentqueue://tasks/by-project/{project_id}")
async def list_tasks_by_project(project_id: str) -> str:
    """List all tasks for a specific project."""
    db = await _get_db()
    tasks = await db.list_tasks(project_id=project_id)
    return json.dumps([task_to_dict(t) for t in tasks], indent=2)


@mcp.resource("agentqueue://tasks/by-status/{status}")
async def list_tasks_by_status(status: str) -> str:
    """List all tasks with a given status (e.g. IN_PROGRESS, READY, COMPLETED)."""
    db = await _get_db()
    try:
        task_status = TaskStatus(status)
    except ValueError:
        return json.dumps({"error": f"Invalid status: {status}. Valid: {[s.value for s in TaskStatus]}"})
    tasks = await db.list_tasks(status=task_status)
    return json.dumps([task_to_dict(t) for t in tasks], indent=2)


# --- Projects --------------------------------------------------------------

@mcp.resource("agentqueue://projects")
async def list_all_projects() -> str:
    """List all configured projects."""
    db = await _get_db()
    projects = await db.list_projects()
    return json.dumps([project_to_dict(p) for p in projects], indent=2)


@mcp.resource("agentqueue://projects/{project_id}")
async def get_project(project_id: str) -> str:
    """Get details for a specific project."""
    db = await _get_db()
    project = await db.get_project(project_id)
    if not project:
        return json.dumps({"error": f"Project not found: {project_id}"})
    return json.dumps(project_to_dict(project), indent=2)


# --- Agents ----------------------------------------------------------------

@mcp.resource("agentqueue://agents")
async def list_all_agents() -> str:
    """List all registered agents and their current state."""
    db = await _get_db()
    agents = await db.list_agents()
    return json.dumps([agent_to_dict(a) for a in agents], indent=2)


@mcp.resource("agentqueue://agents/active")
async def list_active_agents() -> str:
    """List agents currently working on tasks."""
    db = await _get_db()
    agents = await db.list_agents(state=AgentState.BUSY)
    return json.dumps([agent_to_dict(a) for a in agents], indent=2)


# --- Profiles --------------------------------------------------------------

@mcp.resource("agentqueue://profiles")
async def list_all_profiles() -> str:
    """List all agent profiles."""
    db = await _get_db()
    profiles = await db.list_profiles()
    return json.dumps([profile_to_dict(p) for p in profiles], indent=2)


@mcp.resource("agentqueue://profiles/{profile_id}")
async def get_profile(profile_id: str) -> str:
    """Get details for a specific agent profile."""
    db = await _get_db()
    profile = await db.get_profile(profile_id)
    if not profile:
        return json.dumps({"error": f"Profile not found: {profile_id}"})
    return json.dumps(profile_to_dict(profile), indent=2)


# --- Events ----------------------------------------------------------------

@mcp.resource("agentqueue://events/recent")
async def list_recent_events() -> str:
    """List recent system events (last 50)."""
    db = await _get_db()
    events = await db.get_recent_events(limit=50)
    return json.dumps(events, indent=2, default=str)


# --- Workspaces ------------------------------------------------------------

@mcp.resource("agentqueue://workspaces")
async def list_all_workspaces() -> str:
    """List all workspaces across all projects."""
    db = await _get_db()
    projects = await db.list_projects()
    all_workspaces = []
    for p in projects:
        ws_list = await db.list_workspaces(p.id)
        all_workspaces.extend([workspace_to_dict(w) for w in ws_list])
    return json.dumps(all_workspaces, indent=2)


@mcp.resource("agentqueue://workspaces/by-project/{project_id}")
async def list_workspaces_by_project(project_id: str) -> str:
    """List workspaces for a specific project."""
    db = await _get_db()
    workspaces = await db.list_workspaces(project_id)
    return json.dumps([workspace_to_dict(w) for w in workspaces], indent=2)


# ===========================================================================
# TOOLS — Task Management
# ===========================================================================

@mcp.tool()
async def create_task(
    project_id: str,
    title: str,
    description: str,
    priority: int = 100,
    task_type: str = "",
    profile_id: str = "",
    parent_task_id: str = "",
    requires_approval: bool = False,
) -> str:
    """Create a new task in the agent queue.

    Args:
        project_id: ID of the project to create the task in
        title: Short title for the task
        description: Full description of what the task should accomplish
        priority: Priority value (higher = more important, default 100)
        task_type: Type of task (feature, bugfix, refactor, test, docs, chore, research, plan)
        profile_id: Agent profile to use for this task
        parent_task_id: Parent task ID if this is a subtask
        requires_approval: Whether task requires human approval before completion
    """
    db = await _get_db()
    from src.task_names import generate_task_id
    from src.models import Task, TaskType

    # Validate inputs before generating ID
    tt = None
    if task_type:
        try:
            tt = TaskType(task_type)
        except ValueError:
            return json.dumps({"error": f"Invalid task_type: {task_type}"})

    project = await db.get_project(project_id)
    if not project:
        return json.dumps({"error": f"Project not found: {project_id}"})

    task_id = await generate_task_id(db)

    task = Task(
        id=task_id,
        project_id=project_id,
        title=title,
        description=description,
        priority=priority,
        task_type=tt,
        profile_id=profile_id or None,
        parent_task_id=parent_task_id or None,
        requires_approval=requires_approval,
    )
    await db.create_task(task)

    bus = await _get_event_bus()
    await bus.emit("task_created", {"task_id": task_id, "project_id": project_id})

    return json.dumps({"task_id": task_id, "message": f"Task '{title}' created successfully"})


@mcp.tool()
async def stop_task(task_id: str) -> str:
    """Stop a running task.

    Args:
        task_id: ID of the task to stop
    """
    db = await _get_db()
    task = await db.get_task(task_id)
    if not task:
        return json.dumps({"error": f"Task not found: {task_id}"})
    await db.update_task(task_id, status=TaskStatus.FAILED.value)
    bus = await _get_event_bus()
    await bus.emit("task_stopped", {"task_id": task_id})
    return json.dumps({"message": f"Task {task_id} stopped"})


@mcp.tool()
async def restart_task(task_id: str) -> str:
    """Restart a failed or stopped task.

    Args:
        task_id: ID of the task to restart
    """
    db = await _get_db()
    task = await db.get_task(task_id)
    if not task:
        return json.dumps({"error": f"Task not found: {task_id}"})
    await db.update_task(task_id, status=TaskStatus.READY.value, retry_count=0)
    bus = await _get_event_bus()
    await bus.emit("task_restarted", {"task_id": task_id})
    return json.dumps({"message": f"Task {task_id} restarted"})


@mcp.tool()
async def reopen_task(task_id: str, feedback: str = "") -> str:
    """Reopen a completed task with optional feedback.

    Args:
        task_id: ID of the task to reopen
        feedback: Optional feedback to append to the task description
    """
    db = await _get_db()
    task = await db.get_task(task_id)
    if not task:
        return json.dumps({"error": f"Task not found: {task_id}"})
    updates: dict[str, Any] = {"status": TaskStatus.READY.value, "retry_count": 0}
    if feedback:
        new_desc = f"{task.description}\n\n---\nFeedback: {feedback}"
        updates["description"] = new_desc
    await db.update_task(task_id, **updates)
    return json.dumps({"message": f"Task {task_id} reopened"})


@mcp.tool()
async def approve_task(task_id: str) -> str:
    """Approve a task that is awaiting approval.

    Args:
        task_id: ID of the task to approve
    """
    db = await _get_db()
    task = await db.get_task(task_id)
    if not task:
        return json.dumps({"error": f"Task not found: {task_id}"})
    if task.status != TaskStatus.AWAITING_APPROVAL:
        return json.dumps({"error": f"Task {task_id} is not awaiting approval (status: {task.status.value})"})
    await db.update_task(task_id, status=TaskStatus.COMPLETED.value)
    bus = await _get_event_bus()
    await bus.emit("task_approved", {"task_id": task_id})
    return json.dumps({"message": f"Task {task_id} approved and completed"})


@mcp.tool()
async def reject_task(task_id: str, reason: str = "") -> str:
    """Reject a task that is awaiting approval, sending it back to READY.

    Args:
        task_id: ID of the task to reject
        reason: Reason for rejection (appended to description)
    """
    db = await _get_db()
    task = await db.get_task(task_id)
    if not task:
        return json.dumps({"error": f"Task not found: {task_id}"})
    updates: dict[str, Any] = {"status": TaskStatus.READY.value, "retry_count": 0}
    if reason:
        updates["description"] = f"{task.description}\n\n---\nRejection reason: {reason}"
    await db.update_task(task_id, **updates)
    return json.dumps({"message": f"Task {task_id} rejected and returned to READY"})


@mcp.tool()
async def get_task_details(task_id: str) -> str:
    """Get full details for a task including dependencies and context.

    Args:
        task_id: ID of the task to inspect
    """
    db = await _get_db()
    task = await db.get_task(task_id)
    if not task:
        return json.dumps({"error": f"Task not found: {task_id}"})
    result = task_to_dict(task)
    result["dependencies"] = list(await db.get_dependencies(task_id))
    result["context"] = await db.get_task_contexts(task_id)
    subtasks = await db.get_subtasks(task_id)
    result["subtasks"] = [task_to_dict(s) for s in subtasks]
    return json.dumps(result, indent=2, default=str)


# ===========================================================================
# TOOLS — Project Management
# ===========================================================================

@mcp.tool()
async def pause_project(project_id: str) -> str:
    """Pause a project — no new tasks will be scheduled.

    Args:
        project_id: ID of the project to pause
    """
    db = await _get_db()
    project = await db.get_project(project_id)
    if not project:
        return json.dumps({"error": f"Project not found: {project_id}"})
    await db.update_project(project_id, status=ProjectStatus.PAUSED.value)
    return json.dumps({"message": f"Project {project_id} paused"})


@mcp.tool()
async def resume_project(project_id: str) -> str:
    """Resume a paused project.

    Args:
        project_id: ID of the project to resume
    """
    db = await _get_db()
    project = await db.get_project(project_id)
    if not project:
        return json.dumps({"error": f"Project not found: {project_id}"})
    await db.update_project(project_id, status=ProjectStatus.ACTIVE.value)
    return json.dumps({"message": f"Project {project_id} resumed"})


@mcp.tool()
async def list_projects() -> str:
    """List all configured projects with their status and settings."""
    db = await _get_db()
    projects = await db.list_projects()
    return json.dumps([project_to_dict(p) for p in projects], indent=2)


# ===========================================================================
# TOOLS — Dependency Management
# ===========================================================================

@mcp.tool()
async def add_dependency(task_id: str, depends_on: str) -> str:
    """Add a dependency — task_id will wait until depends_on completes.

    Args:
        task_id: The task that should wait
        depends_on: The task that must complete first
    """
    db = await _get_db()
    for tid in [task_id, depends_on]:
        if not await db.get_task(tid):
            return json.dumps({"error": f"Task not found: {tid}"})
    try:
        from src.state_machine import validate_dag_with_new_edge
        all_deps = await db.get_all_dependencies()
        validate_dag_with_new_edge(all_deps, task_id, depends_on)
    except Exception as e:
        return json.dumps({"error": f"Would create cycle: {e}"})
    await db.add_dependency(task_id, depends_on)
    return json.dumps({"message": f"Dependency added: {task_id} depends on {depends_on}"})


@mcp.tool()
async def remove_dependency(task_id: str, depends_on: str) -> str:
    """Remove a dependency between two tasks.

    Args:
        task_id: The dependent task
        depends_on: The dependency to remove
    """
    db = await _get_db()
    await db.remove_dependency(task_id, depends_on)
    return json.dumps({"message": f"Dependency removed: {task_id} no longer depends on {depends_on}"})


@mcp.tool()
async def get_dependencies(task_id: str) -> str:
    """Get all dependencies for a task.

    Args:
        task_id: The task to check dependencies for
    """
    db = await _get_db()
    deps = await db.get_dependencies(task_id)
    deps_met = await db.are_dependencies_met(task_id)
    return json.dumps({
        "task_id": task_id,
        "dependencies": list(deps),
        "all_met": deps_met,
    })


# ===========================================================================
# TOOLS — Workspace Operations
# ===========================================================================

@mcp.tool()
async def list_workspaces(project_id: str = "") -> str:
    """List workspaces, optionally filtered by project.

    Args:
        project_id: Optional project ID to filter by
    """
    db = await _get_db()
    if project_id:
        workspaces = await db.list_workspaces(project_id)
    else:
        projects = await db.list_projects()
        workspaces = []
        for p in projects:
            workspaces.extend(await db.list_workspaces(p.id))
    return json.dumps([workspace_to_dict(w) for w in workspaces], indent=2)


@mcp.tool()
async def find_merge_conflicts(project_id: str = "") -> str:
    """Check workspaces for merge conflicts.

    Args:
        project_id: Optional project ID to limit the check to
    """
    db = await _get_db()
    projects = await db.list_projects()
    if project_id:
        projects = [p for p in projects if p.id == project_id]

    conflicts: list[dict] = []
    for p in projects:
        workspaces = await db.list_workspaces(p.id)
        for ws in workspaces:
            # Check if workspace is locked (potential conflict indicator)
            if ws.locked_by_agent_id and ws.locked_by_task_id:
                conflicts.append({
                    "workspace_id": ws.id,
                    "project_id": p.id,
                    "workspace_path": ws.workspace_path,
                    "locked_by_agent": ws.locked_by_agent_id,
                    "locked_by_task": ws.locked_by_task_id,
                })
    return json.dumps({
        "conflicts_found": len(conflicts),
        "workspaces": conflicts,
    })


# ===========================================================================
# TOOLS — Agent Operations
# ===========================================================================

@mcp.tool()
async def list_agents(state: str = "") -> str:
    """List all agents, optionally filtered by state.

    Args:
        state: Optional filter (IDLE, BUSY, PAUSED, ERROR)
    """
    db = await _get_db()
    agent_state = None
    if state:
        try:
            agent_state = AgentState(state)
        except ValueError:
            return json.dumps({"error": f"Invalid state: {state}. Valid: {[s.value for s in AgentState]}"})
    agents = await db.list_agents(state=agent_state)
    return json.dumps([agent_to_dict(a) for a in agents], indent=2)


# ===========================================================================
# TOOLS — Monitoring
# ===========================================================================

@mcp.tool()
async def get_chain_health() -> str:
    """Get health status of the task dependency chain.

    Returns blocked tasks, circular dependencies, and other chain issues.
    """
    db = await _get_db()
    all_tasks = await db.list_tasks()
    all_deps = await db.get_all_dependencies()

    blocked = [t for t in all_tasks if t.status == TaskStatus.BLOCKED]
    in_progress = [t for t in all_tasks if t.status == TaskStatus.IN_PROGRESS]
    ready = [t for t in all_tasks if t.status == TaskStatus.READY]
    failed = [t for t in all_tasks if t.status == TaskStatus.FAILED]

    return json.dumps({
        "total_tasks": len(all_tasks),
        "in_progress": len(in_progress),
        "ready": len(ready),
        "blocked": len(blocked),
        "failed": len(failed),
        "total_dependencies": sum(len(d) for d in all_deps.values()),
        "blocked_tasks": [task_to_dict(t) for t in blocked],
        "failed_tasks": [task_to_dict(t) for t in failed],
    })


@mcp.tool()
async def get_recent_events(limit: int = 20) -> str:
    """Get recent system events.

    Args:
        limit: Maximum number of events to return (default 20)
    """
    db = await _get_db()
    events = await db.get_recent_events(limit=min(limit, 100))
    return json.dumps(events, indent=2, default=str)


@mcp.tool()
async def get_system_status() -> str:
    """Get an overview of the entire agent-queue system status."""
    db = await _get_db()
    projects = await db.list_projects()
    tasks = await db.list_tasks()
    agents = await db.list_agents()

    status_counts: dict[str, int] = {}
    for t in tasks:
        key = t.status.value
        status_counts[key] = status_counts.get(key, 0) + 1

    agent_state_counts: dict[str, int] = {}
    for a in agents:
        key = a.state.value
        agent_state_counts[key] = agent_state_counts.get(key, 0) + 1

    return json.dumps({
        "projects": {
            "total": len(projects),
            "active": len([p for p in projects if p.status == ProjectStatus.ACTIVE]),
            "paused": len([p for p in projects if p.status == ProjectStatus.PAUSED]),
        },
        "tasks": {
            "total": len(tasks),
            "by_status": status_counts,
        },
        "agents": {
            "total": len(agents),
            "by_state": agent_state_counts,
        },
    }, indent=2)


# ===========================================================================
# TOOLS — Profile Operations
# ===========================================================================

@mcp.tool()
async def list_profiles() -> str:
    """List all agent profiles."""
    db = await _get_db()
    profiles = await db.list_profiles()
    return json.dumps([profile_to_dict(p) for p in profiles], indent=2)


@mcp.tool()
async def get_profile_details(profile_id: str) -> str:
    """Get full details for an agent profile.

    Args:
        profile_id: ID of the profile to inspect
    """
    db = await _get_db()
    profile = await db.get_profile(profile_id)
    if not profile:
        return json.dumps({"error": f"Profile not found: {profile_id}"})
    return json.dumps(profile_to_dict(profile), indent=2)


# ===========================================================================
# PROMPTS — Reusable prompt templates
# ===========================================================================

@mcp.prompt()
async def create_task_prompt(
    project_id: str,
    task_type: str = "feature",
    context: str = "",
) -> str:
    """Generate a prompt for creating a well-structured task.

    Args:
        project_id: Target project for the task
        task_type: Type of task to create
        context: Additional context about the desired work
    """
    db = await _get_db()
    project = await db.get_project(project_id)
    project_name = project.name if project else project_id

    return f"""Create a task for the "{project_name}" project.

Task type: {task_type}
{f"Context: {context}" if context else ""}

Please provide:
1. A clear, concise title (under 80 characters)
2. A detailed description that includes:
   - What needs to be done
   - Why it's needed
   - Acceptance criteria
   - Any relevant technical details
3. Priority (1-1000, default 100)
4. Whether it requires human approval before completion

Format your response as JSON with keys: title, description, priority, requires_approval"""


@mcp.prompt()
async def review_task_prompt(task_id: str) -> str:
    """Generate a prompt for reviewing a completed task.

    Args:
        task_id: ID of the task to review
    """
    db = await _get_db()
    task = await db.get_task(task_id)
    if not task:
        return f"Task {task_id} not found."

    contexts = await db.get_task_contexts(task_id)
    context_text = "\n".join(
        f"- [{c.get('type', 'unknown')}] {c.get('content', '')[:200]}"
        for c in contexts
    ) if contexts else "No additional context."

    return f"""Review the following completed task:

**Task:** {task.title} ({task.id})
**Project:** {task.project_id}
**Type:** {task.task_type.value if task.task_type else "unspecified"}
**Status:** {task.status.value}

**Description:**
{task.description}

**Context:**
{context_text}

Please evaluate:
1. Was the task completed as described?
2. Are there any issues or concerns?
3. Should this be approved or rejected? Why?
4. Any follow-up tasks needed?"""


@mcp.prompt()
async def project_overview_prompt(project_id: str) -> str:
    """Generate a prompt for getting a comprehensive project overview.

    Args:
        project_id: ID of the project to overview
    """
    db = await _get_db()
    project = await db.get_project(project_id)
    if not project:
        return f"Project {project_id} not found."

    tasks = await db.list_tasks(project_id=project_id)
    status_counts: dict[str, int] = {}
    for t in tasks:
        key = t.status.value
        status_counts[key] = status_counts.get(key, 0) + 1

    workspaces = await db.list_workspaces(project_id)

    return f"""Provide an overview of the "{project.name}" project:

**Project ID:** {project.id}
**Status:** {project.status.value}
**Credit Weight:** {project.credit_weight}
**Max Concurrent Agents:** {project.max_concurrent_agents}
**Repo:** {project.repo_url or "not configured"}
**Default Branch:** {project.repo_default_branch}

**Task Summary:**
{json.dumps(status_counts, indent=2)}
Total tasks: {len(tasks)}

**Workspaces:** {len(workspaces)}

Based on this information, please provide:
1. Current project health assessment
2. Any bottlenecks or concerns
3. Recommended next actions"""


# ===========================================================================
# Streaming support — SSE event stream for real-time updates
# ===========================================================================

@mcp.tool()
async def subscribe_events(event_types: str = "*") -> str:
    """Subscribe to real-time agent-queue events.

    Returns a snapshot of recent events. For continuous streaming, use the
    SSE transport mode. Event types: task_created, task_completed,
    task_failed, agent_assigned, etc. Use '*' for all events.

    Args:
        event_types: Comma-separated event types to subscribe to, or '*' for all
    """
    db = await _get_db()
    events = await db.get_recent_events(limit=10)
    requested_types = [t.strip() for t in event_types.split(",")]

    if "*" not in requested_types:
        events = [e for e in events if e.get("event_type") in requested_types]

    return json.dumps({
        "subscribed_to": requested_types,
        "recent_events": events,
        "note": "For real-time streaming, connect via SSE transport",
    }, indent=2, default=str)


# ===========================================================================
# Entry point
# ===========================================================================

def main():
    """Run the MCP server on stdio transport."""
    parser = argparse.ArgumentParser(description="Agent Queue MCP Server")
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"Path to SQLite database (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="Transport mode (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for SSE/HTTP transport (default: 8000)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
    else:
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    # Store db path on the server instance for lifespan access
    mcp._db_path = args.db

    if args.transport == "sse":
        mcp.settings.port = args.port
    elif args.transport == "streamable-http":
        mcp.settings.port = args.port

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
