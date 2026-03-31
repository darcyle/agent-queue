"""MCP server for the agent-queue system.

Exposes all CommandHandler commands as MCP tools (auto-registered from
``_ALL_TOOL_DEFINITIONS`` in ``src.tool_registry``), plus read-only MCP
resources and reusable prompt templates.

Each MCP tool delegates execution to ``CommandHandler.execute(name, args)``,
ensuring feature parity with both the Discord bot and the Supervisor LLM
tool-use loop — no business logic is reimplemented here.

Usage:
    python -m packages.mcp_server.mcp_server [--config PATH]

Or via the ``agent-queue-mcp`` entry point defined in pyproject.toml.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any

from mcp.server import FastMCP
from mcp.server.fastmcp.tools import Tool
from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata, ArgModelBase
from pydantic import ConfigDict

# Add project root to path so we can import src modules
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.config import load_config
from src.command_handler import CommandHandler
from src.database import Database
from src.event_bus import EventBus
from src.models import (
    AgentState,
    TaskStatus,
)
from src.orchestrator import Orchestrator
from src.tool_registry import _ALL_TOOL_DEFINITIONS
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

DEFAULT_CONFIG_PATH = os.environ.get(
    "AGENT_QUEUE_CONFIG",
    os.path.expanduser("~/.agent-queue/config.yaml"),
)

# ---------------------------------------------------------------------------
# Excluded commands — dangerous or irrelevant for MCP clients
# ---------------------------------------------------------------------------

DEFAULT_EXCLUDED_COMMANDS = {
    "shutdown", "restart_daemon", "update_and_restart",
    "run_command",  # dangerous for external MCP clients
    "browse_tools", "load_tools",  # meta-tools for LLM context management, not MCP
}


# ---------------------------------------------------------------------------
# Permissive argument model for dynamic tool registration
# ---------------------------------------------------------------------------

class _AnyArgs(ArgModelBase):
    """Accepts any JSON fields — used for dynamically registered tools
    whose schemas come from ``_ALL_TOOL_DEFINITIONS`` rather than from
    Python function signatures."""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    def model_dump_one_level(self) -> dict[str, Any]:
        result = super().model_dump_one_level()
        if self.__pydantic_extra__:
            result.update(self.__pydantic_extra__)
        return result


# Shared FuncMetadata instance — all dynamic tools use the same permissive
# argument model (actual validation is done by CommandHandler).
_ANY_ARGS_METADATA = FuncMetadata(arg_model=_AnyArgs, fn_is_coroutine=True)


# ---------------------------------------------------------------------------
# Lifespan — initialize Orchestrator + CommandHandler, tear down on exit
# ---------------------------------------------------------------------------

@asynccontextmanager
async def server_lifespan(server: FastMCP):
    """Initialize Orchestrator + CommandHandler on startup, shut down on exit.

    The Orchestrator is fully initialized (DB, event bus, git manager, etc.)
    but its scheduling loop (``run()``) is never started — the MCP server
    only needs the command execution layer.
    """
    config_path = getattr(server, "_config_path", DEFAULT_CONFIG_PATH)
    config = load_config(config_path)

    orchestrator = Orchestrator(config)
    await orchestrator.initialize()

    command_handler = CommandHandler(orchestrator, config)
    orchestrator.set_command_handler(command_handler)

    # Keep db/event_bus accessible for resources (read-only views)
    db = orchestrator.db
    event_bus = orchestrator.event_bus

    try:
        yield {
            "db": db,
            "event_bus": event_bus,
            "orchestrator": orchestrator,
            "command_handler": command_handler,
        }
    finally:
        await orchestrator.shutdown()


# ---------------------------------------------------------------------------
# Create the FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="agent-queue",
    instructions=(
        "Agent Queue MCP server. Provides access to all CommandHandler "
        "operations (task management, project configuration, agent monitoring, "
        "workspace operations, git, hooks, memory, and more) for the "
        "agent-queue orchestrator system."
    ),
    lifespan=server_lifespan,
)


# ---------------------------------------------------------------------------
# Helper to get objects from lifespan context
# ---------------------------------------------------------------------------

async def _get_db() -> Database:
    """Retrieve the Database instance from the current MCP context."""
    ctx = mcp.get_context()
    return ctx.request_context.lifespan_context["db"]


async def _get_event_bus() -> EventBus:
    """Retrieve the EventBus instance from the current MCP context."""
    ctx = mcp.get_context()
    return ctx.request_context.lifespan_context["event_bus"]


async def _get_command_handler() -> CommandHandler:
    """Retrieve the CommandHandler instance from the current MCP context."""
    ctx = mcp.get_context()
    return ctx.request_context.lifespan_context["command_handler"]


# ---------------------------------------------------------------------------
# Dynamic tool registration from _ALL_TOOL_DEFINITIONS
# ---------------------------------------------------------------------------

def register_command_tools(
    mcp_server: FastMCP,
    excluded: set[str] | None = None,
) -> list[str]:
    """Auto-register all CommandHandler commands as MCP tools.

    For each tool definition in ``_ALL_TOOL_DEFINITIONS`` that is not in the
    exclusion set, creates a closure that calls
    ``command_handler.execute(name, args)`` and returns the JSON result,
    then registers it with FastMCP.

    Args:
        mcp_server: The FastMCP instance to register tools on.
        excluded: Set of command names to skip. Defaults to
            ``DEFAULT_EXCLUDED_COMMANDS``.

    Returns:
        List of registered tool names.
    """
    if excluded is None:
        excluded = DEFAULT_EXCLUDED_COMMANDS

    registered: list[str] = []

    for tool_def in _ALL_TOOL_DEFINITIONS:
        name = tool_def["name"]
        if name in excluded:
            logger.debug("Excluding command from MCP: %s", name)
            continue

        description = tool_def.get("description", f"Execute the {name} command.")
        input_schema = tool_def.get("input_schema", {"type": "object", "properties": {}})

        # Create a closure that captures the command name.
        # The handler receives **kwargs from the permissive _AnyArgs model
        # and delegates to CommandHandler.execute().
        def _make_handler(cmd_name: str):
            async def handler(**kwargs):
                ch = await _get_command_handler()
                result = await ch.execute(cmd_name, kwargs)
                return json.dumps(result, default=str)
            handler.__name__ = cmd_name
            handler.__qualname__ = cmd_name
            return handler

        handler_fn = _make_handler(name)

        # Construct a Tool directly so we can use our custom input_schema
        # (from tool_registry) instead of FastMCP's auto-generated schema
        # from function introspection.
        tool = Tool(
            fn=handler_fn,
            name=name,
            description=description,
            parameters=input_schema,
            fn_metadata=_ANY_ARGS_METADATA,
            is_async=True,
        )

        # Register directly on the tool manager (skip duplicates)
        if name in mcp_server._tool_manager._tools:
            logger.warning("Duplicate tool definition skipped: %s", name)
            continue
        mcp_server._tool_manager._tools[name] = tool
        registered.append(name)

    logger.info(
        "Registered %d MCP tools from tool_registry (%d excluded)",
        len(registered),
        len(excluded),
    )
    return registered


# Register all tools at module load time
_registered_tools = register_command_tools(mcp, DEFAULT_EXCLUDED_COMMANDS)


# ===========================================================================
# RESOURCES  (read-only views — kept as-is)
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
# Entry point
# ===========================================================================

def main():
    """Run the MCP server on stdio transport."""
    parser = argparse.ArgumentParser(description="Agent Queue MCP Server")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config YAML (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to SQLite database (overrides config; deprecated, use --config)",
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

    # Store config path on the server instance for lifespan access
    mcp._config_path = args.config

    # Legacy --db flag: store for backward compat (lifespan will use config)
    if args.db:
        mcp._db_path = args.db

    if args.transport == "sse":
        mcp.settings.port = args.port
    elif args.transport == "streamable-http":
        mcp.settings.port = args.port

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
