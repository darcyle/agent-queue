"""Shared command handler for AgentQueue.

This module provides the single code path for all operational commands.
Both the Discord slash commands and the chat agent LLM tools delegate
their business logic here, keeping formatting and presentation separate.

This is the Command Pattern in action: every operation the system supports
(50+ commands) is routed through CommandHandler.execute(name, args).  The
two callers -- Discord slash commands and ChatAgent LLM tool-use -- never
contain business logic themselves; they translate their inputs into a dict,
call execute(), and format the returned dict for their respective UIs.

The benefit is feature parity by construction.  A new command added here is
immediately available to both Discord and the chat agent without duplicating
any logic.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from src.config import AppConfig
from src.discord.embeds import STATUS_EMOJIS, progress_bar
from src.discord.notifications import classify_error
from src.git.manager import GitError
from src.models import (
    Agent, AgentProfile, AgentState, Hook, Project, ProjectStatus, RepoSourceType,
    Task, TaskStatus, TaskType, VerificationType, TASK_TYPE_VALUES, Workspace,
)
from src.orchestrator import Orchestrator
from src.state_machine import CyclicDependencyError, validate_dag_with_new_edge
from src.task_names import generate_task_id


def _count_by(items, key_fn) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        k = key_fn(item)
        counts[k] = counts.get(k, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Tree-view text formatting
# ---------------------------------------------------------------------------
# Unicode box-drawing characters for task tree rendering.  These match the
# constants in ``src/discord/embeds.py`` but are duplicated here so the
# command handler stays self-contained for formatting purposes.

_TREE_BRANCH = "├── "   # Non-last child connector
_TREE_LAST   = "└── "   # Last child connector
_TREE_PIPE   = "│   "   # Continuation pipe for deeper levels
_TREE_SPACE  = "    "   # Blank continuation (last child's subtree)

# Discord messages cap at 2,000 characters.  We leave headroom for any
# surrounding text the caller might prepend/append (embed wrapper, header, etc).
_TREE_CHAR_BUDGET = 1800


def _status_emoji(status: TaskStatus) -> str:
    """Return the status emoji for a *TaskStatus* value.

    Falls back to ``⚪`` (white circle) for unknown statuses so the tree
    never breaks even if new statuses are added before the emoji map is
    updated.
    """
    return STATUS_EMOJIS.get(status.value, "⚪")


def _count_tree_stats(node: dict) -> tuple[int, int]:
    """Return ``(completed, total)`` counts for all descendants of *node*.

    *node* uses the ``{"task": Task, "children": [...]}`` shape produced by
    ``Database.get_task_tree()``.
    """
    total = 0
    completed = 0
    for child in node.get("children", []):
        total += 1
        if child["task"].status == TaskStatus.COMPLETED:
            completed += 1
        # Recurse into grandchildren
        sub_c, sub_t = _count_tree_stats(child)
        completed += sub_c
        total += sub_t
    return completed, total


def _collect_tree_task_ids(node: dict) -> list[str]:
    """Collect all task IDs from a tree hierarchy.

    *node* uses the ``{"task": Task, "children": [...]}`` shape produced
    by ``Database.get_task_tree()``.
    """
    ids = [node["task"].id]
    for child in node.get("children", []):
        ids.extend(_collect_tree_task_ids(child))
    return ids


def _tree_dep_annotation(task_id: str, dep_map: dict[str, dict] | None) -> str:
    """Build an inline dependency annotation for a tree node.

    Returns a string like ``" (← needs #X; blocks #Y)"`` when there
    are noteworthy dependency relationships, or the empty string when
    the task has no unmet upstream dependencies and blocks nothing.

    "needs" shows only **unmet** (non-COMPLETED) upstream dependencies
    so that fully-satisfied edges don't add noise.  "blocks" always
    lists downstream dependents regardless of their status.
    """
    if not dep_map or task_id not in dep_map:
        return ""
    info = dep_map[task_id]
    parts: list[str] = []

    # Unmet upstream dependencies (what this task still needs).
    depends_on: list[dict] = info.get("depends_on", [])
    unmet = [d for d in depends_on if d.get("status") != "COMPLETED"]
    if unmet:
        ids = ", ".join(f"#{d['id']}" for d in unmet)
        parts.append(f"needs {ids}")

    # Downstream dependents (what this task blocks).
    blocks: list[str] = info.get("blocks", [])
    if blocks:
        ids = ", ".join(f"#{bid}" for bid in blocks)
        parts.append(f"blocks {ids}")

    if not parts:
        return ""
    return f" (← {'; '.join(parts)})"


def _dep_annotation(task_id: str, dep_map: dict[str, dict] | None) -> str:
    """Build a concise dependency annotation suffix for a tree node.

    When *dep_map* is provided and contains entries for *task_id*, the
    function returns a parenthesised annotation like ``(← needs #abc)``
    or ``(← blocks #xyz)`` (or both).  Returns an empty string when
    there's nothing to annotate.

    Parameters
    ----------
    task_id:
        The ID of the task being rendered.
    dep_map:
        Mapping of ``task_id`` → ``{"depends_on": [...], "blocks": [...]}``
        as produced by :meth:`CommandHandler._build_dep_map`.  Each list
        element is a dict with at least an ``"id"`` key.  May be ``None``.
    """
    if not dep_map:
        return ""
    info = dep_map.get(task_id)
    if not info:
        return ""

    parts: list[str] = []

    depends_on = info.get("depends_on", [])
    if depends_on:
        ids = ", ".join(f"#{d['id']}" for d in depends_on)
        parts.append(f"needs {ids}")

    blocks = info.get("blocks", [])
    if blocks:
        ids = ", ".join(f"#{b['id']}" for b in blocks)
        parts.append(f"blocks {ids}")

    if not parts:
        return ""
    return " (← " + ", ".join(parts) + ")"


def _collect_tree_tasks(children: list[dict]) -> list["Task"]:
    """Recursively collect all :class:`Task` objects from tree children.

    Parameters
    ----------
    children:
        A list of ``{"task": Task, "children": [...]}`` dicts, matching
        the shape returned by ``Database.get_task_tree()``.

    Returns
    -------
    list[Task]
        Flat list of every Task in the subtree (does **not** include the
        root — the caller should prepend it if needed).
    """
    result: list[Task] = []
    for node in children:
        result.append(node["task"])
        result.extend(_collect_tree_tasks(node.get("children", [])))
    return result


def _count_subtree(children: list[dict]) -> tuple[int, int]:
    """Recursively count ``(completed, total)`` tasks in a tree node list.

    Parameters
    ----------
    children:
        A list of ``{"task": Task, "children": [...]}`` dicts, matching the
        shape returned by ``Database.get_task_tree()``.

    Returns
    -------
    tuple[int, int]
        ``(completed_count, total_count)`` across the entire subtree
        (excluding the root that *owns* these children).
    """
    completed = 0
    total = 0
    for node in children:
        task: Task = node["task"]
        total += 1
        if task.status == TaskStatus.COMPLETED:
            completed += 1
        child_completed, child_total = _count_subtree(node.get("children", []))
        completed += child_completed
        total += child_total
    return completed, total


def _render_tree_node(
    task: Task,
    children: list[dict],
    *,
    depth: int,
    max_depth: int,
    prefix: str,
    is_last: bool,
    dep_map: dict[str, dict] | None = None,
) -> list[str]:
    """Render a single tree node and its descendants as lines of text.

    This is the recursive workhorse called by :func:`_format_task_tree`.
    It produces one line per visible task, using box-drawing characters to
    convey hierarchy.

    Parameters
    ----------
    task:
        The Task object for this node.
    children:
        Child tree nodes (same shape as ``get_task_tree`` output).
    depth:
        Current nesting depth (0 = root).
    max_depth:
        Maximum depth before collapsing remaining children.
    prefix:
        The box-drawing prefix inherited from the parent's formatting
        (e.g. ``"│   "`` or ``"    "``).
    is_last:
        Whether this node is the last sibling at its level.
    dep_map:
        Optional dependency mapping produced by
        :meth:`CommandHandler._build_dep_map`.  When provided, each node
        gets an inline annotation showing upstream/downstream dependencies
        (e.g. ``(← needs #abc, blocks #xyz)``).
    """
    lines: list[str] = []
    emoji = _status_emoji(task.status)
    dep_suffix = _dep_annotation(task.id, dep_map)

    # -- Format the current node's line --------------------------------------
    if depth == 0:
        # Root task: bold title + inline task id
        lines.append(f"{emoji} **{task.title}** `{task.id}`{dep_suffix}")
    else:
        connector = _TREE_LAST if is_last else _TREE_BRANCH
        lines.append(f"{prefix}{connector}{emoji} {task.title}{dep_suffix}")

    if not children:
        return lines

    # Prefix that this node's children will inherit
    if depth == 0:
        child_prefix = ""
    else:
        child_prefix = prefix + (_TREE_SPACE if is_last else _TREE_PIPE)

    # -- Depth limit: collapse the remaining subtree into a summary ----------
    if depth >= max_depth:
        completed, total = _count_subtree(children)
        noun = "subtask" if total == 1 else "subtasks"
        lines.append(
            f"{child_prefix}{_TREE_LAST}… ({total} more {noun}, "
            f"{completed} complete)"
        )
        return lines

    # -- Render each child recursively ---------------------------------------
    for i, child_node in enumerate(children):
        is_last_child = i == len(children) - 1
        child_lines = _render_tree_node(
            child_node["task"],
            child_node.get("children", []),
            depth=depth + 1,
            max_depth=max_depth,
            prefix=child_prefix,
            is_last=is_last_child,
            dep_map=dep_map,
        )
        lines.extend(child_lines)

    return lines


def _format_task_tree(
    root_task: Task,
    children: list[dict],
    *,
    depth: int = 0,
    max_depth: int = 4,
    compact: bool = False,
    dep_map: dict[str, dict] | None = None,
) -> str:
    """Format a task and its subtask tree as readable text with box-drawing chars.

    This is the single formatter for tree-view task display.  Both Discord
    slash commands and the chat-agent LLM tools call this to produce a
    consistent hierarchical rendering of parent/subtask relationships.

    Parameters
    ----------
    root_task:
        The root :class:`Task` object.
    children:
        List of ``{"task": Task, "children": [...]}`` dicts as returned by
        ``Database.get_task_tree()["children"]``.
    depth:
        Starting depth (normally ``0`` for a top-level call; pass a higher
        value when embedding this tree inside a larger view).
    max_depth:
        Maximum nesting depth to render before collapsing deeper levels
        into a ``… (N more subtasks)`` summary.
    compact:
        If ``True``, show only the root task header and a summary count
        line — no child tree at all.  Useful for dense list views.
    dep_map:
        Optional dependency mapping produced by
        :meth:`CommandHandler._build_dep_map`.  When provided, each tree
        node gets an inline annotation showing upstream/downstream
        dependencies (e.g. ``(← needs #abc, blocks #xyz)``).  Ignored
        in compact mode (too dense for annotations).

    Returns
    -------
    str
        A multi-line string suitable for Discord messages / embeds.
        Automatically truncated to ~1,800 characters to stay within
        Discord's 2,000-char message limit.

    Notes
    -----
    Truncation strategy:
        If the expanded tree exceeds ``_TREE_CHAR_BUDGET`` (~1,800 chars),
        the formatter progressively reduces ``max_depth`` until it fits.
        If even depth-1 is too long it falls back to compact mode.

    Examples
    --------
    Expanded::

        🟡 **Implement auth** `task-abc`
          2/5 subtasks complete
        ├── 🟢 Set up OAuth
        ├── 🟢 Create login page
        ├── 🟡 Add session management
        ├── ⚪ Write tests
        └── ⚪ Security review

    With dependency annotations (``dep_map`` provided)::

        🟡 **Implement auth** `task-abc`
          2/5 subtasks complete
        ├── 🟢 Set up OAuth
        ├── 🟢 Create login page
        ├── 🟡 Add session management (← blocks #task-xyz)
        ├── ⚪ Write tests (← needs #task-def)
        └── ⚪ Security review

    Compact::

        🟡 **Implement auth** `task-abc`
          2/5 subtasks complete
    """
    # -- Compute subtree statistics once (shared by all modes) ---------------
    if children:
        completed, total = _count_subtree(children)
        summary_line = f"  {completed}/{total} subtasks complete"
    else:
        completed, total = 0, 0
        summary_line = None

    # -- Compact mode: root + summary only -----------------------------------
    # Dependency annotations are intentionally omitted in compact mode —
    # the format is too dense and the annotations would dominate the output.
    if compact:
        emoji = _status_emoji(root_task.status)
        lines = [f"{emoji} **{root_task.title}** `{root_task.id}`"]
        if summary_line:
            lines.append(summary_line)
        return "\n".join(lines)

    # -- Expanded mode: full tree with box-drawing characters ----------------
    def _build_expanded(effective_max_depth: int) -> str:
        tree_lines = _render_tree_node(
            root_task,
            children,
            depth=depth,
            max_depth=effective_max_depth,
            prefix="",
            is_last=True,
            dep_map=dep_map,
        )
        # Insert summary line right after the root header
        if summary_line:
            tree_lines.insert(1, summary_line)
        return "\n".join(tree_lines)

    result = _build_expanded(max_depth)

    # -- Truncation: progressively reduce depth, then fall back to compact ---
    if len(result) > _TREE_CHAR_BUDGET:
        for reduced_depth in range(max(max_depth - 1, 1), 0, -1):
            result = _build_expanded(reduced_depth)
            if len(result) <= _TREE_CHAR_BUDGET:
                return result

        # Even depth-1 is too long — fall back to compact mode
        return _format_task_tree(
            root_task, children, depth=depth, compact=True,
        )

    return result


# ---------------------------------------------------------------------------
# Dependency-aware text formatter
# ---------------------------------------------------------------------------

# Character budget for dependency display — mirrors _TREE_CHAR_BUDGET.
_DEP_MAX_CHARS = 1800


def _format_task_dep_line(entry: dict) -> str:
    """Format a single task entry with dependency annotations.

    Parameters
    ----------
    entry:
        A task dict produced by ``_cmd_list_tasks`` when
        ``show_dependencies=True``.  Expected keys: ``id``, ``title``,
        ``status``, and optionally ``depends_on`` (list of dicts with
        ``id`` and ``status``) and ``blocks`` (list of task IDs).

    Returns
    -------
    str
        One or more lines like::

            🔵 #12: Set up database [READY]
               ↳ depends on: #10 (COMPLETED ✅), #11 (IN_PROGRESS 🟡)

        If the task has no dependencies or dependents the sub-lines are
        omitted so that "clean" tasks stay compact.
    """
    status = entry.get("status", "DEFINED")
    emoji = STATUS_EMOJIS.get(status, "⚪")
    lines: list[str] = [f"{emoji} #{entry['id']}: {entry['title']} [{status}]"]

    # --- depends_on (upstream) -----------------------------------------------
    depends_on: list[dict] = entry.get("depends_on", [])
    if depends_on:
        dep_parts: list[str] = []
        for dep in depends_on:
            dep_status = dep.get("status", "DEFINED")
            dep_emoji = STATUS_EMOJIS.get(dep_status, "⚪")
            dep_parts.append(f"#{dep['id']} ({dep_status} {dep_emoji})")
        lines.append(f"   ↳ depends on: {', '.join(dep_parts)}")

    # --- blocks (downstream) -------------------------------------------------
    blocks: list[str] = entry.get("blocks", [])
    if blocks:
        block_ids = ", ".join(f"#{bid}" for bid in blocks)
        lines.append(f"   ↳ blocks: {block_ids}")

    return "\n".join(lines)


def format_dependency_list(task_list: list[dict]) -> str:
    """Format a full task list with dependency annotations.

    Iterates over *task_list* (as produced by ``_cmd_list_tasks`` with
    ``show_dependencies=True``) and returns a multi-line string where
    each task is annotated with its upstream ``depends_on`` and
    downstream ``blocks`` relationships.  Tasks with no dependency data
    are rendered as single lines without sub-annotations.

    The output is truncated to ``_DEP_MAX_CHARS`` to stay within
    Discord embed / message limits.

    Parameters
    ----------
    task_list:
        List of task entry dicts, each containing at minimum ``id``,
        ``title``, and ``status``.  When dependency data is present,
        ``depends_on`` and ``blocks`` keys are also expected.

    Returns
    -------
    str
        Formatted multi-line string ready for embed descriptions or
        plain-text messages.
    """
    if not task_list:
        return ""

    formatted_tasks: list[str] = []
    for entry in task_list:
        formatted_tasks.append(_format_task_dep_line(entry))

    result = "\n".join(formatted_tasks)

    # Truncation guard — drop tasks from the end and add a summary.
    if len(result) <= _DEP_MAX_CHARS:
        return result

    kept: list[str] = []
    used = 0
    # Reserve space for the "… (N more tasks)" indicator (~30 chars).
    budget = _DEP_MAX_CHARS - 30
    shown = 0
    for block in formatted_tasks:
        cost = len(block) + 1  # +1 for the joining newline
        if used + cost > budget:
            break
        kept.append(block)
        used += cost
        shown += 1

    remaining = len(formatted_tasks) - shown
    if remaining > 0:
        kept.append(f"… ({remaining} more task{'s' if remaining != 1 else ''})")

    return "\n".join(kept)


def _build_archive_note(
    task,
    result: dict | None,
    dependencies: set[str],
) -> str:
    """Build a markdown reference note for an archived task.

    Produces a self-contained document that preserves the task's full
    context for future reference -- everything needed to understand what
    was done, why, and how well it went.
    """
    lines: list[str] = []
    lines.append(f"# {task.title}")
    lines.append("")
    lines.append(f"**Task ID:** `{task.id}`")
    lines.append(f"**Project:** `{task.project_id}`")
    lines.append(f"**Status:** {task.status.value}")
    lines.append(f"**Priority:** {task.priority}")
    if task.task_type:
        lines.append(f"**Type:** {task.task_type.value}")
    if task.branch_name:
        lines.append(f"**Branch:** `{task.branch_name}`")
    if task.pr_url:
        lines.append(f"**PR:** {task.pr_url}")
    if task.parent_task_id:
        lines.append(f"**Parent Task:** `{task.parent_task_id}`")
    if task.is_plan_subtask:
        lines.append("**Plan Subtask:** Yes")
    if task.plan_source:
        lines.append(f"**Plan Source:** `{task.plan_source}`")
    if dependencies:
        dep_list = ", ".join(f"`{d}`" for d in sorted(dependencies))
        lines.append(f"**Dependencies:** {dep_list}")
    lines.append(
        f"**Archived:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    lines.append("")

    # Description
    lines.append("## Description")
    lines.append("")
    lines.append(task.description)
    lines.append("")

    # Result details
    if result:
        lines.append("## Result")
        lines.append("")
        if result.get("summary"):
            lines.append(f"**Summary:** {result['summary']}")
            lines.append("")
        if result.get("files_changed"):
            lines.append("**Files Changed:**")
            for fc in result["files_changed"]:
                lines.append(f"- `{fc}`")
            lines.append("")
        if result.get("tokens_used"):
            lines.append(f"**Tokens Used:** {result['tokens_used']:,}")
            lines.append("")
        if result.get("error_message"):
            lines.append(f"**Error:** {result['error_message']}")
            lines.append("")
    else:
        lines.append("## Result")
        lines.append("")
        lines.append("_No execution result recorded._")
        lines.append("")

    return "\n".join(lines)


class CommandHandler:
    """Unified command execution layer for AgentQueue (Command Pattern).

    This is the single code path for every operation in the system.  Both
    the Discord slash commands and the ChatAgent LLM tools call
    ``handler.execute(name, args)`` -- neither contains business logic.

    Convention for command methods:
        Each ``_cmd_*`` method receives a flat ``dict`` of arguments and
        returns a ``dict``.  On success the dict contains domain data
        (e.g. ``{"task": {...}}``).  On failure it contains
        ``{"error": "human-readable message"}``.  Callers never need to
        catch exceptions -- ``execute()`` wraps every call in a try/except.

    Active project context:
        ``_active_project_id`` lets callers set an implicit project scope
        so users chatting in a project's Discord channel don't have to
        pass ``project_id`` on every command.  Many ``_cmd_*`` methods
        fall back to this when no explicit project_id is provided.

    Security helpers:
        ``_validate_path`` sandboxes all file operations to the workspace
        directory or a registered repo source path -- the chat agent can
        never escape to arbitrary filesystem locations.

        ``_resolve_repo_path`` centralizes the surprisingly tricky logic
        for finding the right git checkout directory given a combination
        of project_id, repo_id, and the active project fallback.
    """

    def __init__(self, orchestrator: Orchestrator, config: AppConfig):
        self.orchestrator = orchestrator
        self.config = config
        self._active_project_id: str | None = None
        # Optional callback invoked after a project is deleted.
        # Signature: callback(project_id: str) -> None
        # The Discord bot registers this to clean in-memory channel caches.
        self._on_project_deleted: Callable[[str], None] | None = None
        # Optional callback invoked after a note is written or appended.
        # Signature: async callback(project_id, note_filename, note_path) -> None
        # The Discord bot registers this to auto-refresh viewed notes.
        self.on_note_written: Callable | None = None

    @property
    def db(self):
        return self.orchestrator.db

    def set_active_project(self, project_id: str | None) -> None:
        self._active_project_id = project_id

    async def _validate_path(self, path: str) -> str | None:
        """Validate that a path resolves within workspace_dir or a registered repo source_path."""
        real = os.path.realpath(path)
        workspace_real = os.path.realpath(self.config.workspace_dir)
        if real.startswith(workspace_real + os.sep) or real == workspace_real:
            return real
        repos = await self.db.list_repos()
        for repo in repos:
            if repo.source_path:
                repo_real = os.path.realpath(repo.source_path)
                if real.startswith(repo_real + os.sep) or real == repo_real:
                    return real
        return None

    async def execute(self, name: str, args: dict) -> dict:
        """Execute a command by name and return a structured result dict.

        This is the single code path for all operational commands in the system.
        Both Discord slash commands and chat agent LLM tools call this method.
        """
        try:
            handler = getattr(self, f"_cmd_{name}", None)
            if handler:
                return await handler(args)
            return {"error": f"Unknown command: {name}"}
        except Exception as e:
            return {"error": str(e)}

    # -----------------------------------------------------------------------
    # Project commands -- CRUD, pause/resume, and Discord channel management.
    # Projects are the top-level grouping: each project has its own workspace
    # directory, scheduling weight, and optional dedicated Discord channel.
    # -----------------------------------------------------------------------

    async def _cmd_get_status(self, args: dict) -> dict:
        projects = await self.db.list_projects()
        agents = await self.db.list_agents()
        tasks = await self.db.list_tasks()

        agent_details = []
        for a in agents:
            info = {
                "id": a.id,
                "name": a.name,
                "state": a.state.value,
            }
            if a.current_task_id:
                current_task = await self.db.get_task(a.current_task_id)
                if current_task:
                    info["working_on"] = {
                        "task_id": current_task.id,
                        "title": current_task.title,
                        "project_id": current_task.project_id,
                        "status": current_task.status.value,
                    }
            agent_details.append(info)

        in_progress = [
            {"id": t.id, "title": t.title, "project_id": t.project_id,
             "assigned_agent": t.assigned_agent_id}
            for t in tasks if t.status == TaskStatus.IN_PROGRESS
        ]
        ready = [
            {"id": t.id, "title": t.title, "project_id": t.project_id}
            for t in tasks if t.status == TaskStatus.READY
        ]

        return {
            "projects": len(projects),
            "agents": agent_details,
            "tasks": {
                "total": len(tasks),
                "by_status": _count_by(tasks, lambda t: t.status.value),
                "in_progress": in_progress,
                "ready_to_work": ready,
            },
            "orchestrator_paused": self.orchestrator._paused,
        }

    async def _cmd_list_projects(self, args: dict) -> dict:
        projects = await self.db.list_projects()
        result = []
        for p in projects:
            ws_path = await self.db.get_project_workspace_path(p.id)
            info = {
                "id": p.id,
                "name": p.name,
                "status": p.status.value,
                "credit_weight": p.credit_weight,
                "max_concurrent_agents": p.max_concurrent_agents,
                "workspace": ws_path,
            }
            if p.discord_channel_id:
                info["discord_channel_id"] = p.discord_channel_id
            result.append(info)
        return {"projects": result}

    async def _cmd_create_project(self, args: dict) -> dict:
        project_id = args["name"].lower().replace(" ", "-")
        project = Project(
            id=project_id,
            name=args["name"],
            credit_weight=args.get("credit_weight", 1.0),
            max_concurrent_agents=args.get("max_concurrent_agents", 2),
            repo_url=args.get("repo_url", ""),
            repo_default_branch=args.get("default_branch", "main"),
        )
        await self.db.create_project(project)

        # Determine whether auto-channel creation should happen.
        # An explicit ``auto_create_channels`` arg takes precedence;
        # otherwise fall back to the per-project-channels config flag.
        explicit = args.get("auto_create_channels")
        if explicit is not None:
            should_auto_create = bool(explicit)
        else:
            ppc = self.config.discord.per_project_channels
            should_auto_create = ppc.auto_create

        return {
            "created": project_id,
            "name": project.name,
            "auto_create_channels": should_auto_create,
        }

    async def _cmd_pause_project(self, args: dict) -> dict:
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        await self.db.update_project(pid, status=ProjectStatus.PAUSED)
        return {"paused": pid, "name": project.name}

    async def _cmd_resume_project(self, args: dict) -> dict:
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        await self.db.update_project(pid, status=ProjectStatus.ACTIVE)
        return {"resumed": pid, "name": project.name}

    async def _cmd_edit_project(self, args: dict) -> dict:
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        updates = {}
        if "name" in args:
            updates["name"] = args["name"]
        if "credit_weight" in args:
            updates["credit_weight"] = args["credit_weight"]
        if "max_concurrent_agents" in args:
            updates["max_concurrent_agents"] = args["max_concurrent_agents"]
        if "budget_limit" in args:
            updates["budget_limit"] = args["budget_limit"]
        if "discord_channel_id" in args:
            updates["discord_channel_id"] = args["discord_channel_id"]
        if "default_profile_id" in args:
            dpid = args["default_profile_id"]
            if dpid is not None:
                profile = await self.db.get_profile(dpid)
                if not profile:
                    return {"error": f"Profile '{dpid}' not found"}
            updates["default_profile_id"] = dpid  # None clears it
        if not updates:
            return {
                "error": (
                    "No fields to update. Provide name, credit_weight, "
                    "max_concurrent_agents, budget_limit, discord_channel_id, "
                    "or default_profile_id."
                )
            }
        await self.db.update_project(pid, **updates)
        return {"updated": pid, "fields": list(updates.keys())}

    async def _cmd_set_project_channel(self, args: dict) -> dict:
        """Link an existing Discord channel to a project."""
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}

        channel_id = args["channel_id"]
        await self.db.update_project(pid, discord_channel_id=channel_id)

        return {
            "project_id": pid,
            "channel_id": channel_id,
            "status": "linked",
        }

    async def _cmd_set_control_interface(self, args: dict) -> dict:
        """Set a project's channel by channel *name* (string lookup).

        Resolves the channel name within the guild, then delegates to
        ``_cmd_set_project_channel``.
        Requires ``guild_channels`` to be supplied by the caller (the Discord
        command layer passes the guild's text channels so this layer stays
        Discord-import-free).
        """
        pid = args.get("project_id") or args.get("project_name")
        if not pid:
            return {"error": "project_id (or project_name) is required"}
        channel_name: str | None = args.get("channel_name")
        if not channel_name:
            return {"error": "channel_name is required"}

        # Normalise: strip leading '#' if the user included one.
        channel_name = channel_name.lstrip("#").strip()

        # --- Resolve channel name → ID ---
        # Option A: The caller already looked up the ID (Discord slash command).
        channel_id: str | None = args.get("_resolved_channel_id")

        if not channel_id:
            # Option B: guild_channels list supplied (list of {id, name} dicts).
            guild_channels = args.get("guild_channels")
            if guild_channels:
                for ch in guild_channels:
                    if ch["name"] == channel_name:
                        channel_id = str(ch["id"])
                        break
                if not channel_id:
                    return {
                        "error": f"No text channel named '{channel_name}' found in this server"
                    }
            else:
                return {
                    "error": (
                        "Cannot resolve channel name without guild context. "
                        "Use set_project_channel with a channel_id instead, "
                        "or invoke this command from Discord."
                    )
                }

        # Delegate to the existing set_project_channel handler.
        return await self._cmd_set_project_channel({
            "project_id": pid,
            "channel_id": channel_id,
        })

    async def _cmd_get_project_channels(self, args: dict) -> dict:
        """Return the Discord channel ID configured for a project."""
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        return {
            "project_id": pid,
            "channel_id": project.discord_channel_id,
        }

    async def _cmd_get_project_for_channel(self, args: dict) -> dict:
        """Reverse lookup: find which project a Discord channel belongs to.

        Scans all projects and checks ``discord_channel_id``.
        Returns the first match, or ``project_id: null`` if no project
        is linked to the channel.
        """
        channel_id = args.get("channel_id")
        if not channel_id:
            return {"error": "channel_id is required"}

        channel_id = str(channel_id)
        projects = await self.db.list_projects()
        for project in projects:
            if project.discord_channel_id == channel_id:
                return {
                    "channel_id": channel_id,
                    "project_id": project.id,
                    "project_name": project.name,
                }

        return {
            "channel_id": channel_id,
            "project_id": None,
            "project_name": None,
        }

    async def _cmd_delete_project(self, args: dict) -> dict:
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        tasks = await self.db.list_tasks(project_id=pid, status=TaskStatus.IN_PROGRESS)
        if tasks:
            return {
                "error": f"Cannot delete: {len(tasks)} task(s) currently IN_PROGRESS. "
                         "Stop them first."
            }

        # Capture channel ID before the DB cascade removes it.
        channel_ids: dict[str, str] = {}
        if project.discord_channel_id:
            channel_ids["channel"] = project.discord_channel_id

        await self.db.delete_project(pid)

        # Notify listeners (e.g. Discord bot) so they can purge in-memory
        # channel caches, notes-thread mappings, etc.
        if self._on_project_deleted:
            self._on_project_deleted(pid)

        result: dict = {"deleted": pid, "name": project.name}
        if channel_ids:
            result["channel_ids"] = channel_ids
        # Pass through the caller's archive preference so the Discord layer
        # can act on it.
        archive = args.get("archive_channels", False)
        if archive:
            result["archive_channels"] = True
        return result

    # -----------------------------------------------------------------------
    # Task commands -- CRUD plus lifecycle operations.
    # Tasks are the unit of work assigned to agents.  Beyond basic CRUD this
    # group includes stop (cancel a running task), restart (re-queue a
    # failed/completed task), skip (mark as completed without running),
    # approve (accept an AWAITING_APPROVAL task's PR), and chain-health
    # diagnostics for dependency graphs.
    # -----------------------------------------------------------------------

    # Statuses considered "finished" for the include_completed / completed_only
    # filters.  BLOCKED is included because blocked tasks are not actionable
    # until their dependencies are resolved — callers interested in the active
    # work queue typically want these hidden.
    _FINISHED_STATUSES: frozenset[TaskStatus] = frozenset({
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.BLOCKED,
    })

    async def _resolve_root_task_id(self, task_id: str) -> str:
        """Walk up the parent chain to find the topmost ancestor task ID.

        Used by tree/compact display modes to determine the root task that
        should be rendered as the tree head for a given subtask.  Includes a
        cycle guard to protect against malformed parent chains.
        """
        current_id = task_id
        seen: set[str] = set()
        while True:
            if current_id in seen:
                break  # cycle guard
            seen.add(current_id)
            task = await self.db.get_task(current_id)
            if task is None or task.parent_task_id is None:
                return current_id
            current_id = task.parent_task_id
        return current_id

    async def _build_dep_map_for_tree(
        self,
        tree_data: dict,
        base_map: dict[str, dict] | None = None,
    ) -> dict[str, dict]:
        """Build a dependency map covering every task in *tree_data*.

        Starts from *base_map* (which typically comes from the pre-fetched
        ``task_list`` entries) and fills in any tree nodes that are missing,
        so that ``_tree_dep_annotation()`` can annotate every node.

        Parameters
        ----------
        tree_data:
            A tree hierarchy dict from ``Database.get_task_tree()``.
        base_map:
            Pre-existing dependency data keyed by task ID.  Entries already
            present are reused without additional DB queries.

        Returns
        -------
        dict[str, dict]
            Mapping of ``task_id`` → ``{"depends_on": [...], "blocks": [...]}``.
        """
        result = dict(base_map) if base_map else {}
        # Find tree task IDs not already in the base map
        missing_ids = [
            tid for tid in _collect_tree_task_ids(tree_data)
            if tid not in result
        ]
        if missing_ids:
            # Batch-fetch all missing dependency data in two queries
            batch_result = await self.db.get_dependency_map_for_tasks(missing_ids)
            result.update(batch_result)
        return result

    @staticmethod
    def format_task_with_dependencies(task: dict) -> str:
        """Format a single task dict with optional dependency annotation lines.

        Produces output like::

            🔵 #12: Set up database [READY]
               ↳ depends on: #10 (COMPLETED ✅), #11 (IN_PROGRESS 🟡)

        or::

            🟡 #14: Build API endpoints [IN_PROGRESS]
               ↳ blocks: #15, #16, #17

        Tasks with no dependencies or dependents get a single line with no
        annotation.  The status emoji for the main task is looked up from
        ``STATUS_EMOJIS``; dependency references also include their status
        emoji for quick visual scanning.

        Parameters
        ----------
        task : dict
            A task dict as returned by ``_cmd_list_tasks`` when
            ``show_dependencies=True``.  Must contain at least ``id``,
            ``title``, and ``status`` keys.  May contain ``depends_on`` and
            ``blocks`` lists (each entry: ``{id, title, status}``).

        Returns
        -------
        str
            One or more lines of formatted text.
        """
        status = task.get("status", "DEFINED")
        emoji = STATUS_EMOJIS.get(status, "⚪")
        line = f"{emoji} #{task['id']}: {task['title']} [{status}]"
        lines = [line]

        # depends_on annotation
        depends_on = task.get("depends_on", [])
        if depends_on:
            parts = []
            for dep in depends_on:
                dep_emoji = STATUS_EMOJIS.get(dep["status"], "⚪")
                parts.append(f"#{dep['id']} ({dep['status']} {dep_emoji})")
            lines.append(f"   ↳ depends on: {', '.join(parts)}")

        # blocks annotation
        blocks = task.get("blocks", [])
        if blocks:
            parts = [f"#{b['id']}" for b in blocks]
            lines.append(f"   ↳ blocks: {', '.join(parts)}")

        return "\n".join(lines)

    @staticmethod
    def format_task_list_with_dependencies(tasks: list[dict]) -> str:
        """Format a full task list with dependency annotations.

        Convenience wrapper around :meth:`format_task_with_dependencies` that
        joins all task blocks with a newline separator.

        Parameters
        ----------
        tasks : list[dict]
            Task dicts as returned by ``_cmd_list_tasks`` (the ``"tasks"``
            value) when ``show_dependencies=True``.

        Returns
        -------
        str
            Multi-line formatted text ready for display.
        """
        return "\n".join(
            CommandHandler.format_task_with_dependencies(t) for t in tasks
        )

    async def _cmd_list_tasks(self, args: dict) -> dict:
        """List tasks with configurable display mode.

        Supports three ``display_mode`` values:

        ``"flat"`` (default)
            The original flat list of task dicts — every task is an
            independent row.  This is backward-compatible with all
            existing callers.

        ``"tree"``
            Group tasks by parent and render each root task's hierarchy
            using :func:`_format_task_tree` (expanded, with box-drawing
            characters).  The response includes both the pre-formatted
            text and structured data so callers can choose how to present
            it.

        ``"compact"``
            Show only root (parent) tasks with a subtask count and
            progress bar.  Uses :func:`_format_task_tree` in compact
            mode.  Ideal for dense overview lists.

        For ``"tree"`` and ``"compact"`` modes, a ``project_id`` is
        required so we can query parent tasks.  If ``project_id`` is
        missing the method silently falls back to ``"flat"``.

        When ``show_dependencies`` is ``True``, each task dict is enriched
        with ``depends_on`` (list of upstream task IDs + statuses) and
        ``blocks`` (list of downstream dependent task IDs + statuses).

        Parameters
        ----------
        args : dict
            ``project_id`` – filter by project (optional).
            ``status`` – filter by a specific TaskStatus value (optional).
            ``display_mode`` – ``"flat"``, ``"tree"``, or ``"compact"`` (default ``"flat"``).
            ``include_completed`` – if True, include terminal tasks (default False).
            ``completed_only`` – if True, show only terminal tasks (default False).
            ``show_dependencies`` – if True, enrich each task dict with
            ``depends_on`` and ``blocks`` lists and include a pre-formatted
            ``formatted`` key with the dependency-aware text representation.
        """
        display_mode: str = args.get("display_mode", "flat")
        show_dependencies: bool = args.get("show_dependencies", False)

        kwargs = {}
        if "project_id" in args:
            kwargs["project_id"] = args["project_id"]

        # An explicit `status` filter takes precedence over the convenience
        # boolean flags — the caller is asking for a specific status.
        explicit_status = "status" in args
        if explicit_status:
            kwargs["status"] = TaskStatus(args["status"])

        # ── Flat mode (default / backward-compatible) ──────────────────
        # Also used as the fallback when tree/compact lack a project_id.
        if display_mode == "flat" or "project_id" not in args:
            return await self._list_tasks_flat(
                args, kwargs, explicit_status,
                show_dependencies=show_dependencies,
            )

        # ── Tree / Compact modes ───────────────────────────────────────
        return await self._list_tasks_hierarchical(
            args, kwargs, explicit_status, compact=(display_mode == "compact"),
            show_dependencies=show_dependencies,
        )

    # -- private helpers for _cmd_list_tasks display modes -------------------

    async def _list_tasks_flat(
        self,
        args: dict,
        db_kwargs: dict,
        explicit_status: bool,
        *,
        show_dependencies: bool = False,
    ) -> dict:
        """Flat list mode — the original ``_cmd_list_tasks`` behaviour."""
        tasks = await self.db.list_tasks(**db_kwargs)

        # Apply include_completed / completed_only filtering only when no
        # explicit status filter was provided.
        include_completed: bool = args.get("include_completed", False)
        hidden_count = 0
        if not explicit_status:
            completed_only: bool = args.get("completed_only", False)
            all_count = len(tasks)

            if completed_only:
                # Show only finished tasks.
                tasks = [t for t in tasks if t.status in self._FINISHED_STATUSES]
                hidden_count = all_count - len(tasks)
            elif not include_completed:
                # Default: hide finished tasks so the list shows active work.
                tasks = [t for t in tasks if t.status not in self._FINISHED_STATUSES]
                hidden_count = all_count - len(tasks)
            # else: include_completed=True — return everything unfiltered.

        task_dicts = [self._task_to_dict(t) for t in tasks[:200]]

        if show_dependencies:
            await self._enrich_with_dependencies(task_dicts, tasks[:200])

        result: dict = {
            "display_mode": "flat",
            "tasks": task_dicts,
            "total": len(tasks),
            "hidden_completed": hidden_count,
            "filtered": not include_completed and "status" not in args,
        }
        if show_dependencies:
            result["dependency_display"] = format_dependency_list(task_dicts)
        return result

    async def _cmd_list_active_tasks_all_projects(self, args: dict) -> dict:
        """List active (non-terminal) tasks across ALL projects, grouped by project.

        This gives a cross-project overview of everything that is currently
        queued, in-progress, or otherwise actionable.  Terminal statuses
        (COMPLETED, FAILED, BLOCKED) are excluded by default but can be
        included via ``include_completed=True``.

        Uses ``Database.list_active_tasks()`` for SQL-level filtering when
        showing only active tasks, avoiding the need to fetch and discard
        potentially large numbers of completed tasks.
        """
        include_completed = args.get("include_completed", False)

        if include_completed:
            # Caller wants everything -- no status filtering.
            tasks = await self.db.list_tasks()
        else:
            # SQL-level filtering excludes terminal statuses.
            tasks = await self.db.list_active_tasks()

        # Compute how many terminal tasks were hidden (for UI hints).
        hidden_completed = 0
        if not include_completed:
            status_counts = await self.db.count_tasks_by_status()
            _terminal_values = {"COMPLETED", "FAILED", "BLOCKED"}
            hidden_completed = sum(
                cnt for st, cnt in status_counts.items() if st in _terminal_values
            )

        # Build a task-entry dict (reused for both grouped and flat views).
        def _entry(t: Task, *, include_project: bool = False) -> dict:
            d: dict = {
                "id": t.id,
                "title": t.title,
                "status": t.status.value,
                "priority": t.priority,
                "assigned_agent": t.assigned_agent_id,
                "parent_task_id": t.parent_task_id,
                "is_plan_subtask": t.is_plan_subtask,
                "task_type": t.task_type.value if t.task_type else None,
                "pr_url": t.pr_url,
                "requires_approval": t.requires_approval,
            }
            if include_project:
                d["project_id"] = t.project_id
            return d

        # Group by project_id for readability.
        by_project: dict[str, list[dict]] = {}
        for t in tasks:
            by_project.setdefault(t.project_id, []).append(_entry(t))

        # Also build a flat list (capped at 200) for simple consumers.
        flat = [_entry(t, include_project=True) for t in tasks[:200]]

        return {
            "by_project": by_project,
            "tasks": flat,
            "total": len(tasks),
            "project_count": len(by_project),
            "hidden_completed": hidden_completed,
        }

    async def _list_tasks_hierarchical(
        self,
        args: dict,
        db_kwargs: dict,
        explicit_status: bool,
        *,
        compact: bool,
        show_dependencies: bool = False,
    ) -> dict:
        """Tree or compact list mode — groups tasks by parent hierarchy.

        Fetches root (parentless) tasks for the project, then builds the
        full subtask tree for each root task using
        ``Database.get_task_tree()``.  The caller receives both
        pre-formatted text (ready for Discord) and structured data.
        """
        project_id: str = db_kwargs["project_id"]
        mode_name = "compact" if compact else "tree"

        # 1. Get all root-level tasks for the project.
        root_tasks = await self.db.get_parent_tasks(project_id)

        # 2. Apply status filtering to root tasks.
        if explicit_status:
            status_filter = TaskStatus(args["status"])
            root_tasks = [t for t in root_tasks if t.status == status_filter]
        else:
            root_tasks = self._apply_completion_filter(root_tasks, args)

        # 3. Build tree for each root and format.
        #    When show_dependencies is active we need two passes:
        #      a) collect all trees so we know every task in every subtree,
        #      b) build a dep_map for the full set, then re-format with
        #         annotations.  The first pass still stores a provisional
        #         ``formatted`` string (without annotations) so that if
        #         dep_map turns out empty the output is unchanged.
        trees: list[dict] = []
        included_roots: list[Task] = []  # Track Task objects for dependency enrichment
        # raw_trees stores (root, children) pairs for a second formatting pass
        raw_trees: list[tuple[Task, list[dict]]] = []
        total_tasks = 0

        for root in root_tasks[:200]:
            tree_data = await self.db.get_task_tree(root.id)
            if tree_data is None:
                # Shouldn't happen — root was just fetched — but be safe.
                continue

            children = tree_data.get("children", [])
            completed, subtask_total = _count_subtree(children)

            formatted = _format_task_tree(
                root, children, compact=compact,
            )

            tree_entry: dict = {
                "root": self._task_to_dict(root),
                "formatted": formatted,
                "subtask_completed": completed,
                "subtask_total": subtask_total,
            }

            # In compact mode, also include a text progress bar for
            # callers that want to display it inline.
            if compact and subtask_total > 0:
                tree_entry["progress_bar"] = progress_bar(
                    completed, subtask_total,
                )

            trees.append(tree_entry)
            included_roots.append(root)
            raw_trees.append((root, children))
            # Count root + all its subtasks
            total_tasks += 1 + subtask_total

        # Enrich root task dicts with dependency info when requested.
        if show_dependencies:
            root_dicts = [entry["root"] for entry in trees]
            await self._enrich_with_dependencies(root_dicts, included_roots)

            # Build dep_map across ALL tasks in all trees and re-format
            # expanded trees with inline annotations.  Compact mode is
            # skipped — annotations are too dense for the summary format.
            if not compact:
                all_tasks: list[Task] = []
                for root, children in raw_trees:
                    all_tasks.append(root)
                    all_tasks.extend(_collect_tree_tasks(children))

                dep_map = await self._build_dep_map(all_tasks)
                if dep_map:
                    for i, (root, children) in enumerate(raw_trees):
                        trees[i]["formatted"] = _format_task_tree(
                            root, children, compact=False, dep_map=dep_map,
                        )

        return {
            "display_mode": mode_name,
            "trees": trees,
            "total_root_tasks": len(trees),
            "total_tasks": total_tasks,
        }

    # -- shared helpers ------------------------------------------------------

    def _apply_completion_filter(
        self, tasks: list[Task], args: dict,
    ) -> list[Task]:
        """Filter a task list by the ``include_completed`` / ``completed_only``
        convenience flags.  Used by both flat and hierarchical modes.
        """
        include_completed: bool = args.get("include_completed", False)
        completed_only: bool = args.get("completed_only", False)

        if completed_only:
            return [t for t in tasks if t.status in self._FINISHED_STATUSES]
        if not include_completed:
            return [t for t in tasks if t.status not in self._FINISHED_STATUSES]
        # include_completed=True — return everything unfiltered.
        return tasks

    async def _enrich_with_dependencies(
        self,
        task_dicts: list[dict],
        tasks: list[Task],
    ) -> None:
        """Add ``depends_on`` and ``blocks`` keys to each task dict in-place.

        ``depends_on`` contains a list of upstream dependency dicts, each with
        ``id``, ``title``, and ``status``.  ``blocks`` contains a list of
        downstream dependent task IDs with the same shape.

        Uses the existing ``get_dependencies()`` and ``get_dependents()`` DB
        helpers.  Lookups are batched per-task but results are cached within
        the call to avoid redundant ``get_task()`` queries when the same
        dependency appears across multiple tasks.
        """
        # Local cache so repeated dependency IDs don't trigger extra DB reads.
        task_cache: dict[str, Task | None] = {}

        async def _resolve(task_id: str) -> dict | None:
            if task_id not in task_cache:
                task_cache[task_id] = await self.db.get_task(task_id)
            t = task_cache[task_id]
            if t is None:
                return None
            return {"id": t.id, "title": t.title, "status": t.status.value}

        for td, task in zip(task_dicts, tasks):
            # Upstream: tasks this task depends on
            dep_ids = await self.db.get_dependencies(task.id)
            if dep_ids:
                dep_details = []
                for dep_id in dep_ids:
                    resolved = await _resolve(dep_id)
                    if resolved:
                        dep_details.append(resolved)
                td["depends_on"] = dep_details
            else:
                td["depends_on"] = []

            # Downstream: tasks that depend on this task
            dependent_ids = await self.db.get_dependents(task.id)
            if dependent_ids:
                block_details = []
                for dep_id in dependent_ids:
                    resolved = await _resolve(dep_id)
                    if resolved:
                        block_details.append(resolved)
                td["blocks"] = block_details
            else:
                td["blocks"] = []

    async def _build_dep_map(
        self, tasks: list[Task],
    ) -> dict[str, dict]:
        """Build a dependency map for annotating tree nodes.

        Returns a dict mapping ``task_id`` → ``{"depends_on": [...], "blocks": [...]}``
        where each list element is ``{"id": str, "title": str, "status": str}``.

        Only tasks that have at least one dependency or dependent are included
        in the returned map — callers can treat a missing key as "no
        dependencies".

        This is similar to :meth:`_enrich_with_dependencies` but returns a
        standalone mapping suitable for passing to :func:`_format_task_tree`
        instead of mutating task dicts in-place.
        """
        # Local cache so repeated dependency IDs don't trigger extra DB reads.
        task_cache: dict[str, Task | None] = {}

        async def _resolve(task_id: str) -> dict | None:
            if task_id not in task_cache:
                task_cache[task_id] = await self.db.get_task(task_id)
            t = task_cache[task_id]
            if t is None:
                return None
            return {"id": t.id, "title": t.title, "status": t.status.value}

        dep_map: dict[str, dict] = {}

        for task in tasks:
            # Upstream: tasks this task depends on
            dep_ids = await self.db.get_dependencies(task.id)
            depends_on: list[dict] = []
            for dep_id in dep_ids:
                resolved = await _resolve(dep_id)
                if resolved:
                    depends_on.append(resolved)

            # Downstream: tasks that depend on this task
            dependent_ids = await self.db.get_dependents(task.id)
            blocks: list[dict] = []
            for dep_id in dependent_ids:
                resolved = await _resolve(dep_id)
                if resolved:
                    blocks.append(resolved)

            if depends_on or blocks:
                dep_map[task.id] = {
                    "depends_on": depends_on,
                    "blocks": blocks,
                }

        return dep_map

    @staticmethod
    def _task_to_dict(t: Task) -> dict:
        """Serialize a :class:`Task` to the standard dict used in list
        responses.  Centralises the field selection so flat and
        hierarchical modes stay consistent.
        """
        return {
            "id": t.id,
            "project_id": t.project_id,
            "title": t.title,
            "status": t.status.value,
            "priority": t.priority,
            "assigned_agent": t.assigned_agent_id,
            "parent_task_id": t.parent_task_id,
            "is_plan_subtask": t.is_plan_subtask,
            "task_type": t.task_type.value if t.task_type else None,
            "pr_url": t.pr_url,
            "requires_approval": t.requires_approval,
        }

    async def _cmd_get_task_tree(self, args: dict) -> dict:
        """Return the full subtask hierarchy for a single parent task.

        Fetches the task tree from the database and renders it using
        :func:`_format_task_tree`.  Returns both structured data and
        pre-formatted text so callers (Discord embeds, chat agent) can
        choose how to present it.

        Parameters (via *args*):
            task_id (str): Required.  The root task whose tree to fetch.
            compact (bool): If ``True``, render in compact mode (root +
                summary only).  Default ``False``.
            max_depth (int): Maximum nesting depth before collapsing.
                Default 4.
            show_dependencies (bool): If ``True``, annotate tree nodes with
                inline dependency arrows (e.g. ``← needs #abc``).
                Default ``False``.
        """
        task_id: str = args["task_id"]
        compact: bool = args.get("compact", False)
        max_depth: int = args.get("max_depth", 4)
        show_dependencies: bool = args.get("show_dependencies", False)

        tree_data = await self.db.get_task_tree(task_id)
        if tree_data is None:
            return {"error": f"Task '{task_id}' not found"}

        root_task: Task = tree_data["task"]
        children: list[dict] = tree_data.get("children", [])

        completed, subtask_total = _count_subtree(children)

        # Build dependency map for tree annotations when requested.
        dep_map: dict[str, dict] | None = None
        if show_dependencies and not compact:
            all_tasks = [root_task] + _collect_tree_tasks(children)
            dep_map = await self._build_dep_map(all_tasks)
            # Only pass dep_map if it actually contains entries.
            if not dep_map:
                dep_map = None

        formatted = _format_task_tree(
            root_task, children, compact=compact, max_depth=max_depth,
            dep_map=dep_map,
        )

        result: dict = {
            "root": self._task_to_dict(root_task),
            "formatted": formatted,
            "subtask_completed": completed,
            "subtask_total": subtask_total,
        }

        # In compact mode, include a text progress bar for inline display.
        if compact and subtask_total > 0:
            result["progress_bar"] = progress_bar(completed, subtask_total)

        return result

    async def _cmd_create_task(self, args: dict) -> dict:
        project_id = args.get("project_id") or self._active_project_id
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
        task_id = await generate_task_id(self.db)
        repo_id = args.get("repo_id")
        requires_approval = args.get("requires_approval", False)
        # Resolve optional task_type from string to enum.
        raw_task_type = args.get("task_type")
        task_type: TaskType | None = None
        if raw_task_type:
            if raw_task_type in TASK_TYPE_VALUES:
                task_type = TaskType(raw_task_type)
            else:
                return {"error": f"Invalid task_type '{raw_task_type}'. "
                        f"Allowed: {', '.join(sorted(TASK_TYPE_VALUES))}"}
        # Validate optional profile_id
        profile_id = args.get("profile_id")
        if profile_id:
            profile = await self.db.get_profile(profile_id)
            if not profile:
                return {"error": f"Profile '{profile_id}' not found"}
        task = Task(
            id=task_id,
            project_id=project_id,
            title=args["title"],
            description=args.get("description", args["title"]),
            priority=args.get("priority", 100),
            status=TaskStatus.READY,
            repo_id=repo_id,
            requires_approval=requires_approval,
            task_type=task_type,
            profile_id=profile_id,
        )
        await self.db.create_task(task)
        result = {
            "created": task_id,
            "title": task.title,
            "project_id": task.project_id,
        }
        if repo_id:
            result["repo_id"] = repo_id
        if requires_approval:
            result["requires_approval"] = True
        if task_type:
            result["task_type"] = task_type.value
        if profile_id:
            result["profile_id"] = profile_id
        return result

    async def _cmd_get_task(self, args: dict) -> dict:
        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}
        info = {
            "id": task.id,
            "project_id": task.project_id,
            "title": task.title,
            "description": task.description,
            "status": task.status.value,
            "priority": task.priority,
            "assigned_agent": task.assigned_agent_id,
            "retry_count": task.retry_count,
            "max_retries": task.max_retries,
            "requires_approval": task.requires_approval,
            "is_plan_subtask": task.is_plan_subtask,
            "task_type": task.task_type.value if task.task_type else None,
            "parent_task_id": task.parent_task_id,
            "profile_id": task.profile_id,
        }
        if task.pr_url:
            info["pr_url"] = task.pr_url

        # Dependency visualization: show what this task depends on and blocks
        deps = await self.db.get_dependencies(task.id)
        if deps:
            dep_details = []
            for dep_id in deps:
                dep_task = await self.db.get_task(dep_id)
                if dep_task:
                    dep_details.append({
                        "id": dep_task.id,
                        "title": dep_task.title,
                        "status": dep_task.status.value,
                    })
            info["depends_on"] = dep_details

        dependents = await self.db.get_dependents(task.id)
        if dependents:
            dep_details = []
            for dep_id in dependents:
                dep_task = await self.db.get_task(dep_id)
                if dep_task:
                    dep_details.append({
                        "id": dep_task.id,
                        "title": dep_task.title,
                        "status": dep_task.status.value,
                    })
            info["blocks"] = dep_details

        # Subtask info
        subtasks = await self.db.get_subtasks(task.id)
        if subtasks:
            info["subtasks"] = [
                {
                    "id": st.id,
                    "title": st.title,
                    "status": st.status.value,
                }
                for st in subtasks
            ]

        return info

    async def _cmd_task_deps(self, args: dict) -> dict:
        """Return upstream dependencies and downstream dependents for a task.

        Used by the ``/task-deps`` slash command to render a focused
        dependency view with visual status for each related task.

        Returns
        -------
        dict
            ``task_id``, ``title``, ``status``, ``depends_on`` list, and
            ``blocks`` list.  Each entry in those lists carries ``id``,
            ``title``, and ``status``.
        """
        task_id = args.get("task_id", "")
        if not task_id:
            return {"error": "task_id is required"}

        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}

        # Upstream: what this task depends on
        dep_ids = await self.db.get_dependencies(task.id)
        depends_on: list[dict] = []
        for dep_id in sorted(dep_ids):
            dep_task = await self.db.get_task(dep_id)
            if dep_task:
                depends_on.append({
                    "id": dep_task.id,
                    "title": dep_task.title,
                    "status": dep_task.status.value,
                })

        # Downstream: what this task blocks
        dependent_ids = await self.db.get_dependents(task.id)
        blocks: list[dict] = []
        for dep_id in sorted(dependent_ids):
            dep_task = await self.db.get_task(dep_id)
            if dep_task:
                blocks.append({
                    "id": dep_task.id,
                    "title": dep_task.title,
                    "status": dep_task.status.value,
                })

        return {
            "task_id": task.id,
            "title": task.title,
            "status": task.status.value,
            "depends_on": depends_on,
            "blocks": blocks,
        }

    async def _cmd_get_task_dependencies(self, args: dict) -> dict:
        """Alias for ``_cmd_task_deps`` — used by the ChatAgent tool.

        The ``/task-deps`` slash command uses ``task_deps`` while the
        ChatAgent exposes the same data as ``get_task_dependencies``.
        Both route through the same logic.
        """
        return await self._cmd_task_deps(args)

    async def _cmd_add_dependency(self, args: dict) -> dict:
        """Add a dependency edge: *task_id* depends on *depends_on*.

        Validates both tasks exist and performs cycle detection before
        persisting the edge.  Returns the updated dependency view for the
        task so callers can confirm the new state.
        """
        task_id = args.get("task_id", "")
        depends_on = args.get("depends_on", "")
        if not task_id:
            return {"error": "task_id is required"}
        if not depends_on:
            return {"error": "depends_on is required"}
        if task_id == depends_on:
            return {"error": "A task cannot depend on itself"}

        # Verify both tasks exist.
        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}
        dep_task = await self.db.get_task(depends_on)
        if not dep_task:
            return {"error": f"Dependency task '{depends_on}' not found"}

        # Check for duplicate edge.
        existing = await self.db.get_dependencies(task_id)
        if depends_on in existing:
            return {"error": f"Dependency already exists: '{task_id}' already depends on '{depends_on}'"}

        # Cycle detection — build the full graph and validate.
        all_deps = await self.db.get_all_dependencies()
        try:
            validate_dag_with_new_edge(all_deps, task_id, depends_on)
        except CyclicDependencyError as exc:
            return {"error": f"Cannot add dependency: {exc}"}

        await self.db.add_dependency(task_id, depends_on)

        return {
            "ok": True,
            "task_id": task_id,
            "depends_on": depends_on,
            "task_title": task.title,
            "depends_on_title": dep_task.title,
        }

    async def _cmd_remove_dependency(self, args: dict) -> dict:
        """Remove a dependency edge: *task_id* no longer depends on *depends_on*.

        Returns a confirmation dict.  Silently succeeds if the edge does
        not exist (idempotent).
        """
        task_id = args.get("task_id", "")
        depends_on = args.get("depends_on", "")
        if not task_id:
            return {"error": "task_id is required"}
        if not depends_on:
            return {"error": "depends_on is required"}

        # Verify the task exists (the dependency target need not still exist).
        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}

        # Check if the dependency edge actually exists.
        existing = await self.db.get_dependencies(task_id)
        if depends_on not in existing:
            return {"error": f"No dependency found: '{task_id}' does not depend on '{depends_on}'"}

        await self.db.remove_dependency(task_id, depends_on)

        return {
            "ok": True,
            "task_id": task_id,
            "removed_dependency": depends_on,
            "task_title": task.title,
        }

    async def _cmd_edit_task(self, args: dict) -> dict:
        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}

        VERIFICATION_VALUES = frozenset(v.value for v in VerificationType)

        # Handle status change separately — uses transition_task for logging
        status_changed = False
        if "status" in args:
            new_status_raw = args["status"]
            try:
                new_status = TaskStatus(new_status_raw)
            except ValueError:
                valid = ", ".join(s.value for s in TaskStatus)
                return {"error": f"Invalid status '{new_status_raw}'. Valid: {valid}"}
            old_status = task.status.value
            await self.db.transition_task(
                args["task_id"], new_status, context="edit_task",
            )
            status_changed = True

        updates = {}
        if "title" in args:
            updates["title"] = args["title"]
        if "description" in args:
            updates["description"] = args["description"]
        if "priority" in args:
            updates["priority"] = args["priority"]
        if "task_type" in args:
            raw_tt = args["task_type"]
            if raw_tt is None:
                updates["task_type"] = None  # allow clearing task_type
            elif raw_tt in TASK_TYPE_VALUES:
                updates["task_type"] = TaskType(raw_tt)
            else:
                return {"error": f"Invalid task_type '{raw_tt}'. Allowed: {', '.join(sorted(TASK_TYPE_VALUES))}"}
        if "max_retries" in args:
            updates["max_retries"] = args["max_retries"]
        if "verification_type" in args:
            raw_vt = args["verification_type"]
            if raw_vt in VERIFICATION_VALUES:
                updates["verification_type"] = VerificationType(raw_vt)
            else:
                return {"error": f"Invalid verification_type '{raw_vt}'. Allowed: {', '.join(sorted(VERIFICATION_VALUES))}"}
        if "profile_id" in args:
            pid = args["profile_id"]
            if pid is not None:
                profile = await self.db.get_profile(pid)
                if not profile:
                    return {"error": f"Profile '{pid}' not found"}
            updates["profile_id"] = pid  # None clears the profile

        if updates:
            await self.db.update_task(args["task_id"], **updates)

        all_fields = list(updates.keys())
        if status_changed:
            all_fields.append("status")

        if not all_fields:
            return {
                "error": (
                    "No fields to update. Provide title, description, priority, "
                    "task_type, status, max_retries, verification_type, or profile_id."
                )
            }

        result = {"updated": args["task_id"], "fields": all_fields}
        if status_changed:
            result["old_status"] = old_status
            result["new_status"] = new_status_raw
        return result

    async def _cmd_stop_task(self, args: dict) -> dict:
        error = await self.orchestrator.stop_task(args["task_id"])
        if error:
            return {"error": error}
        return {"stopped": args["task_id"]}

    async def _cmd_restart_task(self, args: dict) -> dict:
        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}
        if task.status == TaskStatus.IN_PROGRESS:
            return {"error": "Task is currently in progress. Stop it first."}
        old_status = task.status.value
        await self.db.transition_task(
            args["task_id"],
            TaskStatus.READY,
            context="restart_task",
            retry_count=0,
            assigned_agent_id=None,
        )
        return {
            "restarted": args["task_id"],
            "title": task.title,
            "previous_status": old_status,
        }

    async def _cmd_reopen_with_feedback(self, args: dict) -> dict:
        """Reopen a task with QA feedback appended to its description.

        Used when a completed or failed task needs to be retried because QA
        found issues.  The feedback is appended to the task description so the
        agent sees it on re-execution, and the task is reset to READY.

        Required args: task_id, feedback (the QA feedback text).
        """
        task_id = args.get("task_id")
        feedback = args.get("feedback", "").strip()
        if not task_id:
            return {"error": "task_id is required"}
        if not feedback:
            return {"error": "feedback text is required"}

        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}
        if task.status == TaskStatus.IN_PROGRESS:
            return {"error": "Task is currently in progress. Stop it first."}

        old_status = task.status.value

        # Append QA feedback to the task description so the agent sees it
        # when the task is re-executed.
        separator = "\n\n---\n**QA Feedback:**\n"
        updated_description = task.description + separator + feedback

        await self.db.transition_task(
            task_id,
            TaskStatus.READY,
            context="reopen_with_feedback",
            description=updated_description,
            retry_count=0,
            assigned_agent_id=None,
        )
        await self.db.log_event(
            "reopen_with_feedback",
            project_id=task.project_id,
            task_id=task_id,
            payload=feedback[:500],
        )
        return {
            "reopened": task_id,
            "title": task.title,
            "previous_status": old_status,
            "status": "READY",
        }

    async def _cmd_delete_task(self, args: dict) -> dict:
        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}
        if task.status == TaskStatus.IN_PROGRESS:
            error = await self.orchestrator.stop_task(args["task_id"])
            if error:
                return {"error": f"Could not stop task before deleting: {error}"}
        await self.db.delete_task(args["task_id"])
        return {"deleted": args["task_id"], "title": task.title}

    # -- Archive commands -----------------------------------------------------
    # Archive moves completed tasks out of the active view into a separate
    # ``archived_tasks`` table.  Tasks can be listed, inspected, restored, or
    # permanently deleted from the archive.

    async def _cmd_archive_tasks(self, args: dict) -> dict:
        """Archive completed (and optionally failed/blocked) tasks.

        Moves matching tasks into the ``archived_tasks`` database table and
        writes a markdown reference note for each task into the project's
        ``archived_tasks/`` workspace directory (when a workspace is available).

        Parameters
        ----------
        args : dict
            ``project_id`` – optional project scope.  When omitted, all
            matching tasks across every project are archived.
            ``include_failed`` – if ``True``, also archive FAILED and BLOCKED
            tasks in addition to COMPLETED.  Default ``False``.
        """
        project_id = args.get("project_id")
        include_failed = args.get("include_failed", False)

        # Determine which statuses to archive.
        statuses_to_archive = [TaskStatus.COMPLETED]
        if include_failed:
            statuses_to_archive.extend([TaskStatus.FAILED, TaskStatus.BLOCKED])

        tasks_to_archive: list = []
        for status in statuses_to_archive:
            tasks_to_archive.extend(
                await self.db.list_tasks(project_id=project_id, status=status)
            )

        if not tasks_to_archive:
            scope = f" in project `{project_id}`" if project_id else ""
            return {"message": f"No completed tasks to archive{scope}."}

        # Phase 1 — gather results and dependencies before any deletions.
        task_data: list[tuple] = []
        for task in tasks_to_archive:
            result = await self.db.get_task_result(task.id)
            deps = await self.db.get_dependencies(task.id)
            task_data.append((task, result, deps))

        # Phase 2 — archive each task (DB table + optional markdown note).
        archived: list[dict] = []
        for task, result, deps in task_data:
            # Write markdown note if the project has a workspace.
            archive_path = await self._write_archive_note(task, result, deps)

            # Move to the archived_tasks table.
            await self.db.archive_task(task.id)

            await self.db.log_event(
                "task_archived",
                project_id=task.project_id,
                task_id=task.id,
            )
            archived.append({
                "id": task.id,
                "title": task.title,
                "status": task.status.value,
                "archive_path": archive_path,
            })

        # Determine archive_dir for the response (use first task's project).
        archive_dir = None
        for entry in archived:
            if entry["archive_path"]:
                archive_dir = os.path.dirname(entry["archive_path"])
                break

        return {
            "archived_count": len(archived),
            "archived_ids": [a["id"] for a in archived],
            "archived": archived,
            "archive_dir": archive_dir,
            "project_id": project_id,
        }

    async def _cmd_archive_task(self, args: dict) -> dict:
        """Archive a single task by ID.

        The task must be in a terminal status (COMPLETED, FAILED, or BLOCKED).
        Active (non-terminal) tasks cannot be archived — stop or complete them
        first.

        Parameters
        ----------
        args : dict
            ``task_id`` – the task to archive.
        """
        task_id = args.get("task_id")
        if not task_id:
            return {"error": "task_id is required"}

        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}

        terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED}
        if task.status not in terminal:
            return {
                "error": (
                    f"Cannot archive task in {task.status.value} status. "
                    "Only COMPLETED, FAILED, or BLOCKED tasks can be archived."
                ),
            }

        # Gather result/deps before archiving (archive deletes child rows).
        result = await self.db.get_task_result(task_id)
        deps = await self.db.get_dependencies(task_id)
        await self._write_archive_note(task, result, deps)

        success = await self.db.archive_task(task_id)
        if not success:
            return {"error": f"Failed to archive task '{task_id}'"}

        await self.db.log_event(
            "task_archived", project_id=task.project_id, task_id=task_id,
        )
        return {
            "archived": task_id,
            "title": task.title,
            "status": task.status.value,
        }

    async def _cmd_list_archived(self, args: dict) -> dict:
        """List archived tasks, optionally scoped to a project.

        Parameters
        ----------
        args : dict
            ``project_id`` – optional project scope.
            ``limit`` – max number of results (default 50).
        """
        project_id = args.get("project_id")
        limit = int(args.get("limit", 50))
        tasks = await self.db.list_archived_tasks(
            project_id=project_id, limit=limit,
        )
        total = await self.db.count_archived_tasks(project_id=project_id)
        return {
            "tasks": tasks,
            "count": len(tasks),
            "total": total,
            "project_id": project_id,
        }

    async def _cmd_restore_task(self, args: dict) -> dict:
        """Restore an archived task back into the active task list.

        The task is restored with status DEFINED so it enters the normal
        orchestrator lifecycle (dependency check → READY → scheduling).

        Parameters
        ----------
        args : dict
            ``task_id`` – the archived task to restore.
        """
        task_id = args.get("task_id")
        if not task_id:
            return {"error": "task_id is required"}

        archived = await self.db.get_archived_task(task_id)
        if not archived:
            return {"error": f"Archived task '{task_id}' not found"}

        success = await self.db.restore_archived_task(task_id)
        if not success:
            return {"error": f"Failed to restore task '{task_id}'"}

        await self.db.log_event(
            "task_restored", project_id=archived["project_id"], task_id=task_id,
        )
        return {
            "restored": task_id,
            "title": archived["title"],
            "new_status": "DEFINED",
        }

    async def _cmd_archive_settings(self, args: dict) -> dict:
        """Return the current auto-archive configuration.

        Reads from ``config.archive`` and includes the count of currently
        archived tasks and how many terminal tasks are eligible right now.
        """
        cfg = self.config.archive
        archived_count = await self.db.count_archived_tasks()

        # Count how many active terminal tasks would be archived now
        older_than_seconds = cfg.after_hours * 3600
        import time as _time
        cutoff = _time.time() - older_than_seconds
        eligible = 0
        if cfg.enabled and cfg.statuses:
            for status in cfg.statuses:
                tasks = await self.db.list_tasks(status=TaskStatus(status))
                for t in tasks:
                    # Check updated_at from DB row
                    cursor = await self.db._db.execute(
                        "SELECT updated_at FROM tasks WHERE id = ?", (t.id,)
                    )
                    row = await cursor.fetchone()
                    if row and row["updated_at"] <= cutoff:
                        eligible += 1

        return {
            "enabled": cfg.enabled,
            "after_hours": cfg.after_hours,
            "statuses": cfg.statuses,
            "archived_count": archived_count,
            "eligible_count": eligible,
        }

    async def _cmd_provide_input(self, args: dict) -> dict:
        """Provide a human reply to an agent question (WAITING_INPUT → READY).

        The agent's question is answered by appending the human's response to the
        task description so the agent sees it on re-execution.  The task is
        transitioned to READY so the scheduler picks it up in the next cycle.

        Required args: task_id, input (the human's response text).
        """
        task_id = args.get("task_id")
        input_text = args.get("input", "").strip()
        if not task_id:
            return {"error": "task_id is required"}
        if not input_text:
            return {"error": "input text is required"}

        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}
        if task.status != TaskStatus.WAITING_INPUT:
            return {
                "error": f"Task is not waiting for input (status: {task.status.value})"
            }

        # Append the human reply to the task description so the agent sees it
        # when the task is re-executed.
        separator = "\n\n---\n**Human Reply:**\n"
        updated_description = task.description + separator + input_text
        await self.db.update_task(task_id, description=updated_description)

        # Transition WAITING_INPUT → READY so the scheduler re-runs the task.
        await self.db.transition_task(
            task_id,
            TaskStatus.READY,
            context="human_replied",
        )
        await self.db.log_event(
            "human_replied",
            project_id=task.project_id,
            task_id=task_id,
            payload=input_text[:500],
        )
        return {
            "task_id": task_id,
            "title": task.title,
            "status": "READY",
        }

    async def _cmd_approve_task(self, args: dict) -> dict:
        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}
        if task.status != TaskStatus.AWAITING_APPROVAL:
            return {"error": f"Task is not awaiting approval (status: {task.status.value})"}
        await self.db.transition_task(
            args["task_id"],
            TaskStatus.COMPLETED,
            context="approve_task",
        )
        await self.db.log_event(
            "task_completed",
            project_id=task.project_id,
            task_id=task.id,
        )
        return {"approved": args["task_id"], "title": task.title}

    async def _cmd_set_task_status(self, args: dict) -> dict:
        task_id = args["task_id"]
        new_status = args["status"]
        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}
        old_status = task.status.value
        await self.db.transition_task(task_id, TaskStatus(new_status),
                                      context="admin_set_status")
        return {
            "task_id": task_id,
            "old_status": old_status,
            "new_status": new_status,
            "title": task.title,
        }

    async def _cmd_skip_task(self, args: dict) -> dict:
        """Skip a BLOCKED/FAILED task to unblock its dependency chain."""
        error, unblocked = await self.orchestrator.skip_task(args["task_id"])
        if error:
            return {"error": error}
        return {
            "skipped": args["task_id"],
            "unblocked_count": len(unblocked),
            "unblocked": [
                {"id": t.id, "title": t.title} for t in unblocked
            ],
        }

    async def _write_archive_note(
        self,
        task,
        result: dict | None,
        dependencies: set[str],
    ) -> str | None:
        """Write a markdown reference note for a task to its project workspace.

        Returns the file path if written, or ``None`` if the project has no
        workspace or the project could not be resolved.
        """
        project = await self.db.get_project(task.project_id)
        if not project:
            return None

        workspace = await self.db.get_project_workspace_path(task.project_id)
        if not workspace:
            return None
        archive_dir = os.path.join(workspace, "archived_tasks")
        os.makedirs(archive_dir, exist_ok=True)

        note = _build_archive_note(task, result, dependencies)
        archive_path = os.path.join(archive_dir, f"{task.id}.md")
        with open(archive_path, "w") as f:
            f.write(note)
        return archive_path

    async def _cmd_get_chain_health(self, args: dict) -> dict:
        """Check dependency chain health for a task or project."""
        task_id = args.get("task_id")
        project_id = args.get("project_id")

        if task_id:
            task = await self.db.get_task(task_id)
            if not task:
                return {"error": f"Task '{task_id}' not found"}
            if task.status != TaskStatus.BLOCKED:
                return {
                    "task_id": task_id,
                    "status": task.status.value,
                    "stuck_downstream": [],
                    "message": "Task is not blocked — no stuck chain.",
                }
            stuck = await self.orchestrator._find_stuck_downstream(task_id)
            return {
                "task_id": task_id,
                "title": task.title,
                "status": task.status.value,
                "stuck_downstream": [
                    {"id": t.id, "title": t.title, "status": t.status.value}
                    for t in stuck
                ],
                "stuck_count": len(stuck),
            }

        # If project_id given (or fall back to active), list all blocked tasks
        # with stuck chains.
        pid = project_id or self._active_project_id
        blocked_tasks = await self.db.list_tasks(
            project_id=pid, status=TaskStatus.BLOCKED
        )
        chains = []
        for bt in blocked_tasks:
            stuck = await self.orchestrator._find_stuck_downstream(bt.id)
            if stuck:
                chains.append({
                    "blocked_task": {"id": bt.id, "title": bt.title},
                    "stuck_downstream": [
                        {"id": t.id, "title": t.title}
                        for t in stuck
                    ],
                    "stuck_count": len(stuck),
                })
        return {
            "project_id": pid,
            "stuck_chains": chains,
            "total_stuck_chains": len(chains),
        }

    async def _cmd_get_task_result(self, args: dict) -> dict:
        result = await self.db.get_task_result(args["task_id"])
        if not result:
            return {"error": f"No results found for task '{args['task_id']}'"}
        return result

    async def _cmd_get_task_diff(self, args: dict) -> dict:
        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}
        if not task.branch_name:
            return {"error": "Task has no branch name"}

        # Resolve checkout path from workspaces (locked by this task)
        checkout_path = None
        workspaces = await self.db.list_workspaces(project_id=task.project_id)
        for ws in workspaces:
            if ws.locked_by_task_id == task.id:
                checkout_path = ws.workspace_path
                break
        # Fallback: first workspace for the project
        if not checkout_path and workspaces:
            checkout_path = workspaces[0].workspace_path
        # Legacy fallback: repo source_path
        if not checkout_path and task.repo_id:
            repo = await self.db.get_repo(task.repo_id)
            if repo and repo.source_path:
                checkout_path = repo.source_path
        if not checkout_path:
            return {"error": "Could not determine checkout path for diff"}

        project = await self.db.get_project(task.project_id)
        default_branch = project.repo_default_branch if project else "main"
        diff = self.orchestrator.git.get_diff(checkout_path, default_branch)
        if not diff:
            return {"diff": "(no changes)", "branch": task.branch_name}
        return {"diff": diff, "branch": task.branch_name}

    async def _cmd_get_agent_error(self, args: dict) -> dict:
        task_id = args["task_id"]
        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}

        result = await self.db.get_task_result(task_id)

        info = {
            "task_id": task_id,
            "title": task.title,
            "status": task.status.value,
            "retries": f"{task.retry_count} / {task.max_retries}",
        }

        if not result:
            info["message"] = "No result recorded yet for this task"
            return info

        result_value = result.get("result", "unknown")
        error_msg = result.get("error_message") or ""
        error_type, suggestion = classify_error(error_msg)

        info["result"] = result_value
        info["error_type"] = error_type
        info["error_message"] = error_msg[:2000] if error_msg else None
        info["suggested_fix"] = suggestion
        summary = result.get("summary") or ""
        if summary:
            info["agent_summary"] = summary[:1000]

        return info

    # -----------------------------------------------------------------------
    # Agent commands -- registration and listing.
    # Agents are the worker processes (Claude Code instances) that execute
    # tasks.  These commands register new agents and inspect their state;
    # the orchestrator handles actual agent lifecycle management.
    # -----------------------------------------------------------------------

    async def _cmd_list_agents(self, args: dict) -> dict:
        agents = await self.db.list_agents()
        return {
            "agents": [
                {
                    "id": a.id,
                    "name": a.name,
                    "type": a.agent_type,
                    "state": a.state.value,
                    "current_task": a.current_task_id,
                }
                for a in agents
            ]
        }

    async def _cmd_create_agent(self, args: dict) -> dict:
        from .agent_names import generate_unique_agent_name

        name = args.get("name")
        if not name:
            name = await generate_unique_agent_name(self.db)

        agent_id = name.lower().replace(" ", "-")

        # Agents start directly as IDLE — workspace acquisition is dynamic
        agent = Agent(
            id=agent_id,
            name=name,
            agent_type=args.get("agent_type", "claude"),
            state=AgentState.IDLE,
        )
        await self.db.create_agent(agent)

        return {"created": agent_id, "name": agent.name, "state": "IDLE"}

    async def _cmd_edit_agent(self, args: dict) -> dict:
        """Edit an agent's properties (name, agent_type)."""
        agent_id = args["agent_id"]
        agent = await self.db.get_agent(agent_id)
        if not agent:
            return {"error": f"Agent '{agent_id}' not found"}

        updates = {}
        if "name" in args:
            updates["name"] = args["name"]
        if "agent_type" in args:
            updates["agent_type"] = args["agent_type"]

        if not updates:
            return {"error": "No fields to update. Provide name or agent_type."}

        await self.db.update_agent(agent_id, **updates)

        return {
            "updated": agent_id,
            "fields": list(updates.keys()),
            "name": args.get("name", agent.name),
        }

    async def _cmd_add_workspace(self, args: dict) -> dict:
        """Create a workspace for a project."""
        import uuid
        project_id = args["project_id"]
        project = await self.db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}

        source = args.get("source", "clone")
        source_type = RepoSourceType(source)
        path = args.get("path")
        name = args.get("name")

        if source_type == RepoSourceType.LINK:
            if not path:
                return {"error": "Link workspaces require a 'path' parameter"}
            if not os.path.isdir(path):
                return {"error": f"Path '{path}' does not exist or is not a directory"}
        elif source_type == RepoSourceType.CLONE:
            if not path:
                # Auto-generate path under workspace_dir/{project_id}/
                ws_name = name or f"checkout-{uuid.uuid4().hex[:6]}"
                path = os.path.join(
                    self.config.workspace_dir, project_id, ws_name,
                )
            if project.repo_url:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                try:
                    self.orchestrator.git.create_checkout(project.repo_url, path)
                except Exception as e:
                    return {"error": f"Clone failed: {e}"}

        ws_id = f"ws-{uuid.uuid4().hex[:8]}"
        workspace = Workspace(
            id=ws_id,
            project_id=project_id,
            workspace_path=path,
            source_type=source_type,
            name=name,
        )
        await self.db.create_workspace(workspace)
        return {
            "created": ws_id,
            "project_id": project_id,
            "workspace_path": path,
            "source_type": source,
        }

    async def _cmd_list_workspaces(self, args: dict) -> dict:
        """List workspaces with lock status."""
        project_id = args.get("project_id")
        if not project_id and self._active_project_id:
            project_id = self._active_project_id
        workspaces = await self.db.list_workspaces(project_id=project_id)
        return {
            "workspaces": [
                {
                    "id": ws.id,
                    "project_id": ws.project_id,
                    "workspace_path": ws.workspace_path,
                    "source_type": ws.source_type.value,
                    "name": ws.name,
                    "locked_by_agent_id": ws.locked_by_agent_id,
                    "locked_by_task_id": ws.locked_by_task_id,
                }
                for ws in workspaces
            ]
        }

    async def _cmd_remove_workspace(self, args: dict) -> dict:
        """Delete a workspace."""
        workspace_id = args["workspace_id"]
        ws = await self.db.get_workspace(workspace_id)
        if not ws:
            return {"error": f"Workspace '{workspace_id}' not found"}
        if ws.locked_by_agent_id:
            return {
                "error": f"Workspace '{workspace_id}' is locked by agent "
                         f"'{ws.locked_by_agent_id}'. Release it first."
            }
        await self.db.delete_workspace(workspace_id)
        return {"deleted": workspace_id}

    async def _cmd_release_workspace(self, args: dict) -> dict:
        """Admin force-release a stuck workspace lock."""
        workspace_id = args["workspace_id"]
        ws = await self.db.get_workspace(workspace_id)
        if not ws:
            return {"error": f"Workspace '{workspace_id}' not found"}
        if not ws.locked_by_agent_id:
            return {"workspace_id": workspace_id, "status": "already_unlocked"}
        await self.db.release_workspace(workspace_id)
        return {
            "workspace_id": workspace_id,
            "released_from_agent": ws.locked_by_agent_id,
            "released_from_task": ws.locked_by_task_id,
        }

    async def _cmd_pause_agent(self, args: dict) -> dict:
        """Pause an agent so it stops receiving new tasks.

        The agent finishes its current task (if any) but won't be assigned
        new work until resumed.
        """
        agent_id = args["agent_id"]
        agent = await self.db.get_agent(agent_id)
        if not agent:
            return {"error": f"Agent '{agent_id}' not found"}
        if agent.state == AgentState.PAUSED:
            return {"error": f"Agent '{agent_id}' is already paused"}
        if agent.state == AgentState.BUSY:
            await self.db.update_agent(agent_id, state=AgentState.PAUSED)
            return {
                "agent_id": agent_id,
                "state": "PAUSED",
                "note": "Agent will finish its current task, then stay paused.",
            }
        await self.db.update_agent(agent_id, state=AgentState.PAUSED)
        return {"agent_id": agent_id, "state": "PAUSED"}

    async def _cmd_resume_agent(self, args: dict) -> dict:
        """Resume a paused agent so it can receive tasks again."""
        agent_id = args["agent_id"]
        agent = await self.db.get_agent(agent_id)
        if not agent:
            return {"error": f"Agent '{agent_id}' not found"}
        if agent.state != AgentState.PAUSED:
            return {"error": f"Agent '{agent_id}' is {agent.state.value}, not PAUSED"}
        await self.db.update_agent(agent_id, state=AgentState.IDLE)
        return {"agent_id": agent_id, "state": "IDLE"}

    async def _cmd_delete_agent(self, args: dict) -> dict:
        """Delete an agent and all its dependent records.

        Refuses to delete an agent that is currently BUSY with a task.
        """
        agent_id = args["agent_id"]
        agent = await self.db.get_agent(agent_id)
        if not agent:
            return {"error": f"Agent '{agent_id}' not found"}
        if agent.state == AgentState.BUSY:
            return {
                "error": f"Agent '{agent_id}' is BUSY with task "
                f"'{agent.current_task_id}'. Stop the task first.",
            }
        await self.db.delete_agent(agent_id)
        return {"deleted": agent_id, "name": agent.name}

    # -----------------------------------------------------------------------
    # Events and token usage -- observability into system activity and
    # LLM token consumption, broken down by project, task, or agent.
    # -----------------------------------------------------------------------

    async def _cmd_get_recent_events(self, args: dict) -> dict:
        limit = args.get("limit", 10)
        events = await self.db.get_recent_events(limit=limit)
        return {"events": events}

    async def _cmd_get_token_usage(self, args: dict) -> dict:
        project_id = args.get("project_id")
        task_id = args.get("task_id")

        if task_id:
            cursor = await self.db._db.execute(
                "SELECT agent_id, SUM(tokens_used) as total, COUNT(*) as entries "
                "FROM token_ledger WHERE task_id = ? GROUP BY agent_id",
                (task_id,),
            )
            rows = await cursor.fetchall()
            return {
                "task_id": task_id,
                "breakdown": [
                    {"agent_id": r["agent_id"], "tokens": r["total"], "entries": r["entries"]}
                    for r in rows
                ],
                "total": sum(r["total"] for r in rows),
            }
        elif project_id:
            cursor = await self.db._db.execute(
                "SELECT task_id, agent_id, SUM(tokens_used) as total "
                "FROM token_ledger WHERE project_id = ? "
                "GROUP BY task_id, agent_id ORDER BY total DESC",
                (project_id,),
            )
            rows = await cursor.fetchall()
            return {
                "project_id": project_id,
                "breakdown": [
                    {"task_id": r["task_id"], "agent_id": r["agent_id"], "tokens": r["total"]}
                    for r in rows
                ],
                "total": sum(r["total"] for r in rows),
            }
        else:
            cursor = await self.db._db.execute(
                "SELECT project_id, SUM(tokens_used) as total "
                "FROM token_ledger GROUP BY project_id ORDER BY total DESC",
            )
            rows = await cursor.fetchall()
            return {
                "breakdown": [
                    {"project_id": r["project_id"], "tokens": r["total"]}
                    for r in rows
                ],
                "total": sum(r["total"] for r in rows),
            }

    # -----------------------------------------------------------------------
    # Git commands -- full git workflow via GitManager.
    # Two generations of git commands coexist here: the newer "git_*" set
    # (git_commit, git_push, etc.) and the older "create_branch",
    # "checkout_branch" wrappers.  Both delegate to GitManager for the
    # actual git operations.  All commands use _resolve_repo_path to find
    # the correct checkout directory before invoking git.
    # -----------------------------------------------------------------------

    async def _cmd_get_git_status(self, args: dict) -> dict:
        project_id = args.get("project_id") or self._active_project_id
        if not project_id:
            return {"error": "project_id is required (no active project set)"}
        project = await self.db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}

        git = self.orchestrator.git
        repo_statuses = []

        # Check workspaces first (new model)
        workspaces = await self.db.list_workspaces(project_id=project_id)
        if workspaces:
            for ws in workspaces:
                ws_path = ws.workspace_path
                if not os.path.isdir(ws_path):
                    repo_statuses.append({
                        "repo_id": ws.id,
                        "error": f"Path not found: {ws_path}",
                    })
                    continue
                if not git.validate_checkout(ws_path):
                    repo_statuses.append({
                        "repo_id": ws.id,
                        "error": f"Not a valid git repository: {ws_path}",
                    })
                    continue
                branch = git.get_current_branch(ws_path)
                status_output = git.get_status(ws_path)
                recent_commits = git.get_recent_commits(ws_path, count=5)
                lock_info = ""
                if ws.locked_by_agent_id:
                    lock_info = f" (locked by {ws.locked_by_agent_id})"
                repo_statuses.append({
                    "repo_id": ws.id,
                    "path": ws_path,
                    "branch": branch,
                    "status": status_output or "(clean)",
                    "recent_commits": recent_commits,
                    "lock": lock_info,
                })
        else:
            # Legacy: check repos table
            repos = await self.db.list_repos(project_id=project_id)
            if repos:
                for repo in repos:
                    if repo.source_type == RepoSourceType.LINK and repo.source_path:
                        repo_path = repo.source_path
                    elif repo.source_type == RepoSourceType.CLONE and repo.checkout_base_path:
                        repo_path = repo.checkout_base_path
                    else:
                        continue
                    if not os.path.isdir(repo_path):
                        repo_statuses.append({
                            "repo_id": repo.id,
                            "error": f"Path not found: {repo_path}",
                        })
                        continue
                    if not git.validate_checkout(repo_path):
                        repo_statuses.append({
                            "repo_id": repo.id,
                            "error": f"Not a valid git repository: {repo_path}",
                        })
                        continue
                    branch = git.get_current_branch(repo_path)
                    status_output = git.get_status(repo_path)
                    recent_commits = git.get_recent_commits(repo_path, count=5)
                    repo_statuses.append({
                        "repo_id": repo.id,
                        "path": repo_path,
                        "branch": branch,
                        "status": status_output or "(clean)",
                        "recent_commits": recent_commits,
                    })
            else:
                return {
                    "error": f"Project '{project_id}' has no workspaces. "
                    f"Use /add-workspace to create one."
                }

        return {
            "project_id": project_id,
            "project_name": project.name,
            "repos": repo_statuses,
        }

    async def _resolve_repo_path(
        self, args: dict,
    ) -> tuple[str | None, Project | None, dict | None]:
        """Resolve the git checkout path for a project.

        Returns ``(checkout_path, project, error_dict)``.
        On success *error_dict* is ``None``.  On failure *checkout_path* is
        ``None``.

        Resolution order:
        1. Project's first workspace (from the workspaces table)
        2. Legacy: project's first repo (for backward compat)

        When no *project_id* is supplied, falls back to the active project.
        """
        project_id = args.get("project_id")
        repo_id = args.get("repo_id")

        # Fall back to the active project when no identifiers are supplied.
        if not project_id and not repo_id:
            if self._active_project_id:
                project_id = self._active_project_id
                args["project_id"] = project_id
            else:
                return None, None, {"error": "project_id is required (no active project set)"}
        elif not project_id and repo_id:
            if self._active_project_id:
                project_id = self._active_project_id
                args["project_id"] = project_id

        project = None
        if project_id:
            project = await self.db.get_project(project_id)
            if not project:
                return None, None, {"error": f"Project '{project_id}' not found"}

        git = self.orchestrator.git

        # Try new workspaces table first
        checkout_path = None
        if project_id:
            workspaces = await self.db.list_workspaces(project_id=project_id)
            if workspaces:
                checkout_path = workspaces[0].workspace_path

        # Legacy fallback: try repos table
        if not checkout_path and repo_id:
            repo = await self.db.get_repo(repo_id)
            if repo:
                if repo.source_type == RepoSourceType.LINK and repo.source_path:
                    checkout_path = repo.source_path
                elif repo.source_type in (RepoSourceType.CLONE, RepoSourceType.INIT) and repo.checkout_base_path:
                    checkout_path = repo.checkout_base_path
        elif not checkout_path and project_id:
            repos = await self.db.list_repos(project_id=project_id)
            if repos:
                repo = repos[0]
                if repo.source_type == RepoSourceType.LINK and repo.source_path:
                    checkout_path = repo.source_path
                elif repo.source_type in (RepoSourceType.CLONE, RepoSourceType.INIT) and repo.checkout_base_path:
                    checkout_path = repo.checkout_base_path

        if not checkout_path:
            if not project:
                return None, None, {"error": "No workspace found and no project context"}
            return None, None, {
                "error": f"Project '{project_id}' has no workspaces. "
                f"Use /add-workspace to create one."
            }

        if not os.path.isdir(checkout_path):
            return None, project, {"error": f"Path not found: {checkout_path}"}
        if not git.validate_checkout(checkout_path):
            return None, project, {"error": f"Not a valid git repository: {checkout_path}"}

        return checkout_path, project, None

    async def _cmd_git_commit(self, args: dict) -> dict:
        """Stage all changes and create a commit in a repository."""
        message = args["message"]
        checkout_path, project, err = await self._resolve_repo_path(args)
        if err:
            return err
        repo_id = args.get("repo_id") or (project.id if project else "(workspace)")
        try:
            committed = self.orchestrator.git.commit_all(checkout_path, message)
        except GitError as e:
            return {"error": str(e)}
        if not committed:
            return {"repo_id": repo_id, "committed": False, "message": "Nothing to commit — working tree clean"}
        return {"repo_id": repo_id, "committed": True, "commit_message": message}

    async def _cmd_git_push(self, args: dict) -> dict:
        """Push a branch to the remote origin."""
        checkout_path, project, err = await self._resolve_repo_path(args)
        if err:
            return err
        repo_id = args.get("repo_id") or (project.id if project else "(workspace)")
        git = self.orchestrator.git
        branch = args.get("branch") or git.get_current_branch(checkout_path)
        if not branch:
            return {"error": "Could not determine current branch"}
        try:
            git.push_branch(checkout_path, branch)
        except GitError as e:
            return {"error": str(e)}
        return {"repo_id": repo_id, "pushed": branch}

    async def _cmd_git_create_branch(self, args: dict) -> dict:
        """Create and switch to a new git branch."""
        branch_name = args["branch_name"]
        checkout_path, project, err = await self._resolve_repo_path(args)
        if err:
            return err
        repo_id = args.get("repo_id") or (project.id if project else "(workspace)")
        try:
            self.orchestrator.git.create_branch(checkout_path, branch_name)
        except GitError as e:
            return {"error": str(e)}
        return {"repo_id": repo_id, "created_branch": branch_name}

    async def _cmd_git_merge(self, args: dict) -> dict:
        """Merge a branch into the default branch."""
        branch_name = args["branch_name"]
        checkout_path, project, err = await self._resolve_repo_path(args)
        if err:
            return err
        repo_id = args.get("repo_id") or (project.id if project else "(workspace)")
        default_branch = args.get("default_branch") or (project.repo_default_branch if project else "main") or "main"
        try:
            success = self.orchestrator.git.merge_branch(checkout_path, branch_name, default_branch)
        except GitError as e:
            return {"error": str(e)}
        if not success:
            return {
                "repo_id": repo_id,
                "merged": False,
                "into": default_branch,
                "message": f"Merge conflict — merge of '{branch_name}' into '{default_branch}' was aborted",
            }
        return {
            "repo_id": repo_id,
            "merged": True,
            "branch": branch_name,
            "into": default_branch,
        }

    async def _cmd_git_create_pr(self, args: dict) -> dict:
        """Create a GitHub pull request using the gh CLI."""
        title = args["title"]
        body = args.get("body", "")
        checkout_path, project, err = await self._resolve_repo_path(args)
        if err:
            return err
        repo_id = args.get("repo_id") or (project.id if project else "(workspace)")
        git = self.orchestrator.git
        branch = args.get("branch") or git.get_current_branch(checkout_path)
        if not branch:
            return {"error": "Could not determine current branch"}
        base = args.get("base") or (project.repo_default_branch if project else "main") or "main"
        try:
            pr_url = git.create_pr(checkout_path, branch, title, body, base)
        except GitError as e:
            return {"error": str(e)}
        return {"repo_id": repo_id, "pr_url": pr_url, "branch": branch, "base": base}

    async def _cmd_git_changed_files(self, args: dict) -> dict:
        """List files changed compared to a base branch."""
        checkout_path, project, err = await self._resolve_repo_path(args)
        if err:
            return err
        repo_id = args.get("repo_id") or (project.id if project else "(workspace)")
        base_branch = args.get("base_branch") or (project.repo_default_branch if project else "main") or "main"
        files = self.orchestrator.git.get_changed_files(checkout_path, base_branch)
        return {
            "repo_id": repo_id,
            "base_branch": base_branch,
            "files": files,
            "count": len(files),
        }

    async def _cmd_git_log(self, args: dict) -> dict:
        """Show recent commit log for a repository."""
        checkout_path, project, err = await self._resolve_repo_path(args)
        if err:
            return err

        git = self.orchestrator.git
        count = args.get("count", 10)

        log_output = git.get_recent_commits(checkout_path, count=count)
        branch = git.get_current_branch(checkout_path)

        return {
            "project_id": args["project_id"],
            "repo_id": project.id if project else "(workspace)",
            "branch": branch,
            "log": log_output or "(no commits)",
        }

    # -- Additional project-based git commands ------------------------------

    async def _cmd_git_branch(self, args: dict) -> dict:
        """List branches or create a new branch.

        If ``name`` is provided a new branch is created and checked out;
        otherwise all local branches are listed.
        """

        checkout_path, project, err = await self._resolve_repo_path(args)
        if err:
            return err

        git = self.orchestrator.git
        new_branch = args.get("name")

        if new_branch:
            try:
                git.create_branch(checkout_path, new_branch)
            except GitError as e:
                return {"error": str(e)}
            return {
                "project_id": args["project_id"],
                "created": new_branch,
                "message": f"Created and switched to branch '{new_branch}'",
            }
        else:
            branches = git.list_branches(checkout_path)
            current = git.get_current_branch(checkout_path)
            return {
                "project_id": args["project_id"],
                "current_branch": current,
                "branches": branches,
            }

    async def _cmd_git_checkout(self, args: dict) -> dict:
        """Switch to an existing branch."""

        checkout_path, project, err = await self._resolve_repo_path(args)
        if err:
            return err

        branch = args["branch"]
        git = self.orchestrator.git

        old_branch = git.get_current_branch(checkout_path)
        try:
            git.checkout_branch(checkout_path, branch)
        except GitError as e:
            return {"error": str(e)}
        new_branch = git.get_current_branch(checkout_path)

        return {
            "project_id": args["project_id"],
            "old_branch": old_branch,
            "new_branch": new_branch,
            "message": f"Switched from '{old_branch}' to '{new_branch}'",
        }

    async def _cmd_git_diff(self, args: dict) -> dict:
        """Show diff of the working tree or against a base branch."""
        checkout_path, project, err = await self._resolve_repo_path(args)
        if err:
            return err

        git = self.orchestrator.git
        base = args.get("base_branch")

        try:
            if base:
                diff = git.get_diff(checkout_path, base)
            else:
                # Working tree diff (unstaged changes)
                diff = git._run(["diff"], cwd=checkout_path)
        except GitError as e:
            return {"error": str(e)}

        return {
            "project_id": args["project_id"],
            "repo_id": project.id if project else "(workspace)",
            "base_branch": base or "(working tree)",
            "diff": diff or "(no changes)",
        }

    async def _cmd_create_branch(self, args: dict) -> dict:
        """Create and switch to a new branch in a project's repo."""
        branch_name = args.get("branch_name")
        if not branch_name:
            return {"error": "branch_name is required"}

        checkout_path, project, err = await self._resolve_repo_path(args)
        if err:
            return err

        git = self.orchestrator.git
        try:
            git.create_branch(checkout_path, branch_name)
        except GitError as e:
            return {"error": str(e)}

        return {
            "project_id": args["project_id"],
            "repo_id": project.id if project else "(workspace)",
            "branch": branch_name,
            "status": "created",
        }

    async def _warn_if_in_progress(self, project_id: str) -> str | None:
        """Return a warning string if any tasks are IN_PROGRESS for *project_id*."""
        in_progress = await self.db.list_tasks(
            project_id=project_id, status=TaskStatus.IN_PROGRESS,
        )
        if in_progress:
            return (
                f"⚠️ {len(in_progress)} task(s) currently IN_PROGRESS for this project — "
                f"this operation may disrupt running agent(s)."
            )
        return None

    async def _cmd_checkout_branch(self, args: dict) -> dict:
        """Check out an existing branch."""
        branch_name = args.get("branch_name")
        if not branch_name:
            return {"error": "branch_name is required"}

        checkout_path, project, err = await self._resolve_repo_path(args)
        if err:
            return err

        git = self.orchestrator.git
        try:
            git.checkout_branch(checkout_path, branch_name)
        except GitError as e:
            return {"error": str(e)}

        result = {
            "project_id": args["project_id"],
            "repo_id": project.id if project else "(workspace)",
            "branch": branch_name,
            "status": "checked_out",
        }
        warning = await self._warn_if_in_progress(args["project_id"])
        if warning:
            result["warning"] = warning
        return result

    async def _cmd_commit_changes(self, args: dict) -> dict:
        """Stage all changes and commit with a message."""
        message = args.get("message")
        if not message:
            return {"error": "message is required"}

        checkout_path, project, err = await self._resolve_repo_path(args)
        if err:
            return err

        git = self.orchestrator.git
        try:
            committed = git.commit_all(checkout_path, message)
        except GitError as e:
            return {"error": str(e)}

        if not committed:
            return {
                "project_id": args["project_id"],
                "repo_id": project.id if project else "(workspace)",
                "status": "nothing_to_commit",
                "message": "No changes to commit",
            }

        result = {
            "project_id": args["project_id"],
            "repo_id": project.id if project else "(workspace)",
            "commit_message": message,
            "status": "committed",
        }
        warning = await self._warn_if_in_progress(args["project_id"])
        if warning:
            result["warning"] = warning
        return result

    async def _cmd_push_branch(self, args: dict) -> dict:
        """Push the current (or specified) branch to origin."""
        checkout_path, project, err = await self._resolve_repo_path(args)
        if err:
            return err

        git = self.orchestrator.git
        branch_name = args.get("branch_name")
        if not branch_name:
            branch_name = git.get_current_branch(checkout_path)
            if not branch_name:
                return {"error": "Could not determine current branch"}

        try:
            git.push_branch(checkout_path, branch_name)
        except GitError as e:
            return {"error": str(e)}

        return {
            "project_id": args["project_id"],
            "repo_id": project.id if project else "(workspace)",
            "branch": branch_name,
            "status": "pushed",
        }

    async def _cmd_merge_branch(self, args: dict) -> dict:
        """Merge a branch into the default branch in a project's repo."""
        branch_name = args.get("branch_name")
        if not branch_name:
            return {"error": "branch_name is required"}

        checkout_path, project, err = await self._resolve_repo_path(args)
        if err:
            return err

        git = self.orchestrator.git
        default_branch = project.repo_default_branch if project else "main"

        try:
            success = git.merge_branch(checkout_path, branch_name, default_branch)
        except GitError as e:
            return {"error": str(e)}

        warning = await self._warn_if_in_progress(args["project_id"])

        if not success:
            result = {
                "project_id": args["project_id"],
                "repo_id": project.id if project else "(workspace)",
                "branch": branch_name,
                "target": default_branch,
                "status": "conflict",
                "message": "Merge conflict — merge was aborted",
            }
            if warning:
                result["warning"] = warning
            return result

        result = {
            "project_id": args["project_id"],
            "repo_id": project.id if project else "(workspace)",
            "branch": branch_name,
            "target": default_branch,
            "status": "merged",
        }
        if warning:
            result["warning"] = warning
        return result

    # -----------------------------------------------------------------------
    # Hook commands -- CRUD plus manual firing.
    # Hooks are automated routines that fire on events (e.g. task completion)
    # or on a schedule.  They gather context via shell/file/HTTP steps and
    # optionally invoke an LLM with full tool access to take corrective
    # actions (like creating fix-up tasks when tests fail).
    # -----------------------------------------------------------------------

    async def _cmd_create_hook(self, args: dict) -> dict:
        project_id = args["project_id"]
        project = await self.db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}
        hook_id = args["name"].lower().replace(" ", "-")
        hook = Hook(
            id=hook_id,
            project_id=project_id,
            name=args["name"],
            trigger=json.dumps(args["trigger"]),
            context_steps=json.dumps(args.get("context_steps", [])),
            prompt_template=args["prompt_template"],
            cooldown_seconds=args.get("cooldown_seconds", 3600),
            llm_config=json.dumps(args["llm_config"]) if args.get("llm_config") else None,
        )
        await self.db.create_hook(hook)
        return {"created": hook_id, "name": hook.name, "project_id": project_id}

    async def _cmd_list_hooks(self, args: dict) -> dict:
        project_id = args.get("project_id")
        hooks = await self.db.list_hooks(project_id=project_id)
        return {
            "hooks": [
                {
                    "id": h.id,
                    "project_id": h.project_id,
                    "name": h.name,
                    "enabled": h.enabled,
                    "trigger": json.loads(h.trigger),
                    "cooldown_seconds": h.cooldown_seconds,
                }
                for h in hooks
            ]
        }

    async def _cmd_edit_hook(self, args: dict) -> dict:
        hook_id = args["hook_id"]
        hook = await self.db.get_hook(hook_id)
        if not hook:
            return {"error": f"Hook '{hook_id}' not found"}
        updates = {}
        if "name" in args:
            updates["name"] = args["name"]
        if "enabled" in args:
            updates["enabled"] = args["enabled"]
        if "trigger" in args:
            updates["trigger"] = json.dumps(args["trigger"])
        if "context_steps" in args:
            updates["context_steps"] = json.dumps(args["context_steps"])
        if "prompt_template" in args:
            updates["prompt_template"] = args["prompt_template"]
        if "cooldown_seconds" in args:
            updates["cooldown_seconds"] = args["cooldown_seconds"]
        if "llm_config" in args:
            updates["llm_config"] = json.dumps(args["llm_config"])
        if "max_tokens_per_run" in args:
            updates["max_tokens_per_run"] = args["max_tokens_per_run"]
        if not updates:
            return {"error": "No fields to update"}
        await self.db.update_hook(hook_id, **updates)
        return {"updated": hook_id, "fields": list(updates.keys())}

    async def _cmd_delete_hook(self, args: dict) -> dict:
        hook_id = args["hook_id"]
        hook = await self.db.get_hook(hook_id)
        if not hook:
            return {"error": f"Hook '{hook_id}' not found"}
        await self.db.delete_hook(hook_id)
        return {"deleted": hook_id, "name": hook.name}

    async def _cmd_list_hook_runs(self, args: dict) -> dict:
        hook_id = args["hook_id"]
        hook = await self.db.get_hook(hook_id)
        if not hook:
            return {"error": f"Hook '{hook_id}' not found"}
        limit = args.get("limit", 10)
        runs = await self.db.list_hook_runs(hook_id, limit=limit)
        return {
            "hook_id": hook_id,
            "hook_name": hook.name,
            "runs": [
                {
                    "id": r.id,
                    "trigger_reason": r.trigger_reason,
                    "status": r.status,
                    "tokens_used": r.tokens_used,
                    "skipped_reason": r.skipped_reason,
                    "started_at": r.started_at,
                    "completed_at": r.completed_at,
                }
                for r in runs
            ],
        }

    async def _cmd_fire_hook(self, args: dict) -> dict:
        hook_id = args["hook_id"]
        hooks_engine = self.orchestrator.hooks
        if not hooks_engine:
            return {"error": "Hook engine is not enabled"}
        try:
            await hooks_engine.fire_hook(hook_id)
            return {"fired": hook_id, "status": "running"}
        except ValueError as e:
            return {"error": str(e)}

    # -----------------------------------------------------------------------
    # Notes commands -- markdown documents stored in project workspaces.
    # Notes are a lightweight knowledge base: users and hooks can write
    # specs, brainstorms, or analysis, and later turn them into tasks.
    # Stored as plain .md files under <workspace>/notes/.
    # -----------------------------------------------------------------------

    def _resolve_note_path(self, notes_dir: str, title: str) -> str | None:
        """Resolve a note file path from a title, filename, or slug.

        Tries in order:
        1. Exact filename match (e.g. "keen-beacon-splitting-analysis.md")
        2. Filename without .md extension (e.g. "keen-beacon-splitting-analysis")
        3. Slugified title (e.g. "Analysis: Why keen-beacon Was Not Split" → slug)

        Returns the full file path if found, None otherwise.
        """
        # 1. Exact filename
        if title.endswith(".md"):
            fpath = os.path.join(notes_dir, title)
            if os.path.isfile(fpath):
                return fpath

        # 2. Title as filename without extension
        fpath = os.path.join(notes_dir, f"{title}.md")
        if os.path.isfile(fpath):
            return fpath

        # 3. Slugified title
        slug = self.orchestrator.git.slugify(title)
        if slug:
            fpath = os.path.join(notes_dir, f"{slug}.md")
            if os.path.isfile(fpath):
                return fpath

        return None

    async def _cmd_list_notes(self, args: dict) -> dict:
        project = await self.db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        workspace = await self.db.get_project_workspace_path(args["project_id"])
        if not workspace:
            return {"error": f"Project '{args['project_id']}' has no workspaces. Use /add-workspace to create one."}
        notes_dir = os.path.join(workspace, "notes")
        if not os.path.isdir(notes_dir):
            return {"project_id": args["project_id"], "notes": []}
        notes = []
        for fname in sorted(os.listdir(notes_dir)):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(notes_dir, fname)
            stat = os.stat(fpath)
            title = fname[:-3].replace("-", " ").title()
            try:
                with open(fpath, "r") as f:
                    first_line = f.readline().strip()
                if first_line.startswith("# "):
                    title = first_line[2:].strip()
            except Exception:
                pass
            notes.append({
                "name": fname,
                "title": title,
                "size_bytes": stat.st_size,
                "modified": stat.st_mtime,
                "path": fpath,
            })
        return {"project_id": args["project_id"], "notes": notes}

    async def _cmd_write_note(self, args: dict) -> dict:
        project = await self.db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        workspace = await self.db.get_project_workspace_path(args["project_id"])
        if not workspace:
            return {"error": f"Project '{args['project_id']}' has no workspaces. Use /add-workspace to create one."}
        notes_dir = os.path.join(workspace, "notes")
        os.makedirs(notes_dir, exist_ok=True)
        slug = self.orchestrator.git.slugify(args["title"])
        if not slug:
            return {"error": "Title produces an empty filename"}
        fpath = os.path.join(notes_dir, f"{slug}.md")
        existed = os.path.isfile(fpath)
        with open(fpath, "w") as f:
            f.write(args["content"])
        result = {
            "path": fpath,
            "title": args["title"],
            "status": "updated" if existed else "created",
        }
        if self.on_note_written:
            await self.on_note_written(
                args["project_id"], f"{slug}.md", fpath,
            )
        return result

    async def _cmd_read_note(self, args: dict) -> dict:
        project = await self.db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        workspace = await self.db.get_project_workspace_path(args["project_id"])
        if not workspace:
            return {"error": f"Project '{args['project_id']}' has no workspaces. Use /add-workspace to create one."}
        notes_dir = os.path.join(workspace, "notes")
        fpath = self._resolve_note_path(notes_dir, args["title"])
        if not fpath:
            return {"error": f"Note '{args['title']}' not found"}
        with open(fpath, "r") as f:
            content = f.read()
        stat = os.stat(fpath)
        return {
            "content": content,
            "title": args["title"],
            "path": fpath,
            "size_bytes": stat.st_size,
        }

    async def _cmd_append_note(self, args: dict) -> dict:
        project = await self.db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        workspace = await self.db.get_project_workspace_path(args["project_id"])
        if not workspace:
            return {"error": f"Project '{args['project_id']}' has no workspaces. Use /add-workspace to create one."}
        notes_dir = os.path.join(workspace, "notes")
        os.makedirs(notes_dir, exist_ok=True)
        # Try to find an existing note first (handles .md extension, exact names, slugs)
        fpath = self._resolve_note_path(notes_dir, args["title"])
        existed = fpath is not None
        if not existed:
            # Strip .md extension before slugifying to avoid double-extension
            title_for_slug = args["title"]
            if title_for_slug.lower().endswith(".md"):
                title_for_slug = title_for_slug[:-3]
            slug = self.orchestrator.git.slugify(title_for_slug)
            if not slug:
                return {"error": "Title produces an empty filename"}
            fpath = os.path.join(notes_dir, f"{slug}.md")
        if existed:
            with open(fpath, "a") as f:
                f.write(f"\n\n{args['content']}")
            status = "appended"
        else:
            with open(fpath, "w") as f:
                f.write(f"# {args['title']}\n\n{args['content']}")
            status = "created"
        stat = os.stat(fpath)
        result = {
            "path": fpath,
            "title": args["title"],
            "status": status,
            "size_bytes": stat.st_size,
        }
        if self.on_note_written:
            note_filename = os.path.basename(fpath)
            await self.on_note_written(
                args["project_id"], note_filename, fpath,
            )
        return result

    async def _cmd_compare_specs_notes(self, args: dict) -> dict:
        project = await self.db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        workspace = await self.db.get_project_workspace_path(args["project_id"])
        if not workspace:
            return {"error": f"Project '{args['project_id']}' has no workspaces. Use /add-workspace to create one."}

        # Resolve specs directory — check repo specs/ first, then workspace specs/
        specs_path = args.get("specs_path")
        if not specs_path:
            # Try repo specs/ first
            repos = await self.db.list_repos()
            for repo in repos:
                if repo.project_id == args["project_id"] and repo.source_path:
                    candidate = os.path.join(repo.source_path, "specs")
                    if os.path.isdir(candidate):
                        specs_path = candidate
                        break
            # Fall back to workspace specs/
            if not specs_path:
                specs_path = os.path.join(workspace, "specs")

        notes_path = os.path.join(workspace, "notes")

        def _list_md_files(dirpath: str) -> list[dict]:
            if not os.path.isdir(dirpath):
                return []
            files = []
            for fname in sorted(os.listdir(dirpath)):
                if not fname.endswith(".md"):
                    continue
                fpath = os.path.join(dirpath, fname)
                stat = os.stat(fpath)
                title = fname[:-3].replace("-", " ").title()
                try:
                    with open(fpath, "r") as f:
                        first_line = f.readline().strip()
                    if first_line.startswith("# "):
                        title = first_line[2:].strip()
                except Exception:
                    pass
                files.append({
                    "name": fname,
                    "title": title,
                    "size_bytes": stat.st_size,
                })
            return files

        return {
            "specs": _list_md_files(specs_path),
            "notes": _list_md_files(notes_path),
            "specs_path": specs_path,
            "notes_path": notes_path,
            "project_id": args["project_id"],
        }

    async def _cmd_delete_note(self, args: dict) -> dict:
        project = await self.db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        workspace = await self.db.get_project_workspace_path(args["project_id"])
        if not workspace:
            return {"error": f"Project '{args['project_id']}' has no workspaces. Use /add-workspace to create one."}
        notes_dir = os.path.join(workspace, "notes")
        fpath = self._resolve_note_path(notes_dir, args["title"])
        if not fpath:
            return {"error": f"Note '{args['title']}' not found"}
        os.remove(fpath)
        return {"deleted": fpath, "title": args["title"]}

    # -----------------------------------------------------------------------
    # Memory commands -- semantic search, stats, and reindex for the
    # memsearch-powered project memory subsystem.  These delegate to
    # MemoryManager on the orchestrator.  When memory is not enabled or
    # memsearch is not installed, commands return informative errors.
    # -----------------------------------------------------------------------

    async def _cmd_memory_search(self, args: dict) -> dict:
        """Search project memory by semantic query.

        Returns the top-k most relevant memory chunks from past task
        results, project notes, and knowledge-base entries.
        """
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        query = args.get("query")
        if not query:
            return {"error": "query is required"}
        top_k = args.get("top_k", 10)

        if not self.orchestrator.memory_manager:
            return {"error": "Memory subsystem is not enabled. Set memory.enabled=true in config."}

        project = await self.db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}
        workspace = await self.db.get_project_workspace_path(project_id)
        if not workspace:
            return {"error": f"Project '{project_id}' has no workspaces. Use /add-workspace to create one."}

        try:
            results = await self.orchestrator.memory_manager.search(
                project_id, workspace, query, top_k=top_k
            )
        except Exception as e:
            return {"error": f"Memory search failed: {e}"}

        # Format results for display
        formatted = []
        for i, mem in enumerate(results, 1):
            entry = {
                "rank": i,
                "source": mem.get("source", "unknown"),
                "heading": mem.get("heading", ""),
                "content": mem.get("content", ""),
                "score": round(mem.get("score", 0), 4),
            }
            formatted.append(entry)

        return {
            "project_id": project_id,
            "query": query,
            "top_k": top_k,
            "count": len(formatted),
            "results": formatted,
        }

    async def _cmd_memory_stats(self, args: dict) -> dict:
        """Show memory index statistics for a project."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        if not self.orchestrator.memory_manager:
            return {"error": "Memory subsystem is not enabled. Set memory.enabled=true in config."}

        project = await self.db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}
        workspace = await self.db.get_project_workspace_path(project_id)
        if not workspace:
            return {"error": f"Project '{project_id}' has no workspaces. Use /add-workspace to create one."}

        try:
            stats = await self.orchestrator.memory_manager.stats(project_id, workspace)
        except Exception as e:
            return {"error": f"Failed to retrieve memory stats: {e}"}

        return {"project_id": project_id, **stats}

    async def _cmd_memory_reindex(self, args: dict) -> dict:
        """Force a full reindex of a project's memory.

        Re-scans all markdown files in memory/ and notes/ directories,
        re-embeds changed content, and updates the vector index.
        """
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}

        if not self.orchestrator.memory_manager:
            return {"error": "Memory subsystem is not enabled. Set memory.enabled=true in config."}

        project = await self.db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}
        workspace = await self.db.get_project_workspace_path(project_id)
        if not workspace:
            return {"error": f"Project '{project_id}' has no workspaces. Use /add-workspace to create one."}

        try:
            chunks_indexed = await self.orchestrator.memory_manager.reindex(
                project_id, workspace
            )
        except Exception as e:
            return {"error": f"Memory reindex failed: {e}"}

        return {
            "project_id": project_id,
            "status": "reindex_complete",
            "chunks_indexed": chunks_indexed,
        }

    # -----------------------------------------------------------------------
    # Prompt template commands -- read-only browsing of prompt templates
    # stored in <workspace>/prompts/.  Templates use YAML frontmatter for
    # metadata and Mustache-style {{variable}} placeholders for context
    # injection.  Modifications should only be done through tasks, not
    # through these commands.
    # -----------------------------------------------------------------------

    def _get_prompt_manager(self, workspace: str):
        """Create a PromptManager for the given workspace."""
        from src.prompt_manager import PromptManager
        prompts_dir = os.path.join(workspace, "prompts")
        return PromptManager(prompts_dir)

    async def _cmd_list_prompts(self, args: dict) -> dict:
        """List all prompt templates for a project, optionally filtered."""
        project = await self.db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        workspace = await self.db.get_project_workspace_path(args["project_id"])
        if not workspace:
            return {"error": f"Project '{args['project_id']}' has no workspaces. Use /add-workspace to create one."}
        pm = self._get_prompt_manager(workspace)
        templates = pm.list_templates(
            category=args.get("category"),
            tag=args.get("tag"),
        )
        return {
            "project_id": args["project_id"],
            "prompts": [t.to_dict() for t in templates],
            "categories": pm.get_categories(),
            "total": len(templates),
        }

    async def _cmd_read_prompt(self, args: dict) -> dict:
        """Read a specific prompt template's content and metadata."""
        project = await self.db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        workspace = await self.db.get_project_workspace_path(args["project_id"])
        if not workspace:
            return {"error": f"Project '{args['project_id']}' has no workspaces. Use /add-workspace to create one."}
        pm = self._get_prompt_manager(workspace)
        tmpl = pm.get_template(args["name"])
        if not tmpl:
            return {"error": f"Prompt template '{args['name']}' not found"}
        result = tmpl.to_dict()
        result["content"] = tmpl.body
        return result

    async def _cmd_render_prompt(self, args: dict) -> dict:
        """Render a prompt template with variable substitution."""
        project = await self.db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        workspace = await self.db.get_project_workspace_path(args["project_id"])
        if not workspace:
            return {"error": f"Project '{args['project_id']}' has no workspaces. Use /add-workspace to create one."}
        pm = self._get_prompt_manager(workspace)
        variables = args.get("variables", {})
        rendered = pm.render(args["name"], variables)
        if rendered is None:
            return {"error": f"Prompt template '{args['name']}' not found"}
        return {
            "name": args["name"],
            "rendered": rendered,
            "variables_used": variables,
        }

    # -----------------------------------------------------------------------
    # System / control commands -- orchestrator pause/resume, active project
    # switching, and daemon restart.  These affect the global state of the
    # system rather than any single project or task.
    # -----------------------------------------------------------------------

    async def _cmd_set_active_project(self, args: dict) -> dict:
        pid = args.get("project_id")
        if pid:
            project = await self.db.get_project(pid)
            if not project:
                return {"error": f"Project '{pid}' not found"}
            self._active_project_id = pid
            return {"active_project": pid, "name": project.name}
        else:
            self._active_project_id = None
            return {"active_project": None, "message": "Active project cleared"}

    async def _cmd_orchestrator_control(self, args: dict) -> dict:
        action = args["action"]
        orch = self.orchestrator
        if action == "pause":
            orch.pause()
            return {"status": "paused", "message": "Orchestrator paused — no new tasks will be scheduled"}
        elif action == "resume":
            orch.resume()
            return {"status": "running", "message": "Orchestrator resumed"}
        else:  # status
            running = len(orch._running_tasks)
            return {
                "status": "paused" if orch._paused else "running",
                "running_tasks": running,
            }

    async def _cmd_restart_daemon(self, args: dict) -> dict:
        os.kill(os.getpid(), signal.SIGTERM)
        return {"status": "restarting", "message": "Daemon restart initiated"}

    # -----------------------------------------------------------------------
    # File / shell commands -- sandboxed filesystem and shell access for the
    # chat agent.  These have no Discord slash command equivalent; they exist
    # so the LLM can inspect workspace files, run diagnostic commands, and
    # search codebases.  All paths are validated through _validate_path to
    # prevent escaping the workspace sandbox.
    # -----------------------------------------------------------------------

    async def _cmd_read_file(self, args: dict) -> dict:
        path = args["path"]
        max_lines = args.get("max_lines", 200)
        if not os.path.isabs(path):
            path = os.path.join(self.config.workspace_dir, path)
        validated = await self._validate_path(path)
        if not validated:
            return {"error": "Access denied: path is outside allowed directories"}
        if not os.path.isfile(validated):
            return {"error": f"File not found: {path}"}
        try:
            with open(validated, "r") as f:
                lines = []
                for i, line in enumerate(f):
                    if i >= max_lines:
                        lines.append(f"\n... truncated at {max_lines} lines ({i} total)")
                        break
                    lines.append(line.rstrip("\n"))
            return {"content": "\n".join(lines), "path": validated}
        except UnicodeDecodeError:
            return {"error": "Binary file — cannot display contents"}

    async def _cmd_run_command(self, args: dict) -> dict:
        command = args["command"]
        working_dir = args["working_dir"]
        timeout = min(args.get("timeout", 30), 120)

        if not os.path.isabs(working_dir):
            ws_path = await self.db.get_project_workspace_path(working_dir)
            if ws_path:
                working_dir = ws_path
            else:
                working_dir = os.path.join(self.config.workspace_dir, working_dir)

        validated = await self._validate_path(working_dir)
        if not validated:
            return {"error": "Access denied: working directory is outside allowed directories"}
        if not os.path.isdir(validated):
            return {"error": f"Directory not found: {working_dir}"}

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                command,
                shell=True,
                cwd=validated,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            stdout = result.stdout[:4000] if result.stdout else ""
            stderr = result.stderr[:2000] if result.stderr else ""
            return {
                "returncode": result.returncode,
                "stdout": stdout,
                "stderr": stderr,
            }
        except subprocess.TimeoutExpired:
            return {"error": f"Command timed out after {timeout}s"}

    async def _cmd_search_files(self, args: dict) -> dict:
        pattern = args["pattern"]
        path = args["path"]
        mode = args.get("mode", "grep")

        if not os.path.isabs(path):
            path = os.path.join(self.config.workspace_dir, path)
        validated = await self._validate_path(path)
        if not validated:
            return {"error": "Access denied: path is outside allowed directories"}
        if not os.path.isdir(validated):
            return {"error": f"Directory not found: {path}"}

        try:
            if mode == "grep":
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["grep", "-rn", "--include=*", "-m", "50", pattern, validated],
                    capture_output=True, text=True, timeout=30,
                )
            else:
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["find", validated, "-name", pattern, "-type", "f"],
                    capture_output=True, text=True, timeout=30,
                )
            output = result.stdout[:4000] if result.stdout else "(no matches)"
            return {"results": output, "mode": mode}
        except subprocess.TimeoutExpired:
            return {"error": "Search timed out"}

    # -----------------------------------------------------------------------
    # Agent Profile commands -- CRUD for capability bundles that configure
    # agents with specific tools, MCP servers, and system prompt overrides.
    # -----------------------------------------------------------------------

    async def _cmd_list_profiles(self, args: dict) -> dict:
        profiles = await self.db.list_profiles()
        if not profiles:
            return {"profiles": [], "count": 0}
        return {
            "profiles": [
                {
                    "id": p.id,
                    "name": p.name,
                    "description": p.description,
                    "model": p.model or "(default)",
                    "allowed_tools": p.allowed_tools,
                    "mcp_servers": list(p.mcp_servers.keys()) if p.mcp_servers else [],
                    "has_system_prompt": bool(p.system_prompt_suffix),
                }
                for p in profiles
            ],
            "count": len(profiles),
        }

    async def _cmd_create_profile(self, args: dict) -> dict:
        profile_id = args.get("id", "").strip()
        name = args.get("name", "").strip()
        if not profile_id:
            return {"error": "Profile id is required"}
        if not name:
            return {"error": "Profile name is required"}

        existing = await self.db.get_profile(profile_id)
        if existing:
            return {"error": f"Profile '{profile_id}' already exists"}

        profile = AgentProfile(
            id=profile_id,
            name=name,
            description=args.get("description", ""),
            model=args.get("model", ""),
            permission_mode=args.get("permission_mode", ""),
            allowed_tools=args.get("allowed_tools", []),
            mcp_servers=args.get("mcp_servers", {}),
            system_prompt_suffix=args.get("system_prompt_suffix", ""),
            install=args.get("install", {}),
        )
        await self.db.create_profile(profile)
        result: dict = {"created": profile_id, "name": name}
        # Soft validation — warn about unrecognized tool names
        from src.known_tools import validate_tool_names
        unknown = validate_tool_names(profile.allowed_tools)
        if unknown:
            result["warnings"] = [
                f"Unrecognized tools (will still be set): {', '.join(unknown)}"
            ]
        return result

    async def _cmd_get_profile(self, args: dict) -> dict:
        profile_id = args.get("profile_id", "").strip()
        if not profile_id:
            return {"error": "profile_id is required"}
        profile = await self.db.get_profile(profile_id)
        if not profile:
            return {"error": f"Profile '{profile_id}' not found"}
        return {
            "id": profile.id,
            "name": profile.name,
            "description": profile.description,
            "model": profile.model or "(default)",
            "permission_mode": profile.permission_mode or "(default)",
            "allowed_tools": profile.allowed_tools,
            "mcp_servers": profile.mcp_servers,
            "system_prompt_suffix": profile.system_prompt_suffix or "(none)",
            "install": profile.install,
        }

    async def _cmd_edit_profile(self, args: dict) -> dict:
        profile_id = args.get("profile_id", "").strip()
        if not profile_id:
            return {"error": "profile_id is required"}
        profile = await self.db.get_profile(profile_id)
        if not profile:
            return {"error": f"Profile '{profile_id}' not found"}

        updates = {}
        for fld in (
            "name", "description", "model", "permission_mode",
            "allowed_tools", "mcp_servers", "system_prompt_suffix", "install",
        ):
            if fld in args:
                updates[fld] = args[fld]
        if not updates:
            return {
                "error": (
                    "No fields to update. Provide name, description, model, "
                    "permission_mode, allowed_tools, mcp_servers, "
                    "system_prompt_suffix, or install."
                )
            }
        await self.db.update_profile(profile_id, **updates)
        result: dict = {"updated": profile_id, "fields": list(updates.keys())}
        # Soft validation — warn about unrecognized tool names
        if "allowed_tools" in updates:
            from src.known_tools import validate_tool_names
            unknown = validate_tool_names(updates["allowed_tools"])
            if unknown:
                result["warnings"] = [
                    f"Unrecognized tools (will still be set): {', '.join(unknown)}"
                ]
        return result

    async def _cmd_delete_profile(self, args: dict) -> dict:
        profile_id = args.get("profile_id", "").strip()
        if not profile_id:
            return {"error": "profile_id is required"}
        profile = await self.db.get_profile(profile_id)
        if not profile:
            return {"error": f"Profile '{profile_id}' not found"}
        await self.db.delete_profile(profile_id)
        return {"deleted": profile_id, "name": profile.name}

    # --- Discovery commands ------------------------------------------------

    async def _cmd_list_available_tools(self, args: dict) -> dict:
        from src.known_tools import CLAUDE_CODE_TOOLS, KNOWN_MCP_SERVERS
        tools = [
            {"name": name, "description": desc}
            for name, desc in sorted(CLAUDE_CODE_TOOLS.items())
        ]
        mcp_servers = [
            {
                "name": name,
                "description": info["description"],
                "npm_package": info.get("npm_package", ""),
            }
            for name, info in sorted(KNOWN_MCP_SERVERS.items())
        ]
        return {"tools": tools, "mcp_servers": mcp_servers}

    # --- Install manifest commands -----------------------------------------

    async def _cmd_check_profile(self, args: dict) -> dict:
        import shutil
        from src.known_tools import InstallManifest
        profile_id = args.get("profile_id", "").strip()
        if not profile_id:
            return {"error": "profile_id is required"}
        profile = await self.db.get_profile(profile_id)
        if not profile:
            return {"error": f"Profile '{profile_id}' not found"}

        manifest = InstallManifest.from_dict(profile.install)
        issues: list[str] = []

        # Check commands via shutil.which
        for cmd in manifest.commands:
            if not shutil.which(cmd):
                issues.append(f"Command not found: {cmd}")

        # Check npm packages
        for pkg in manifest.npm:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "npm", "list", "-g", pkg, "--depth=0",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
                if proc.returncode != 0:
                    issues.append(f"npm package not installed: {pkg}")
            except FileNotFoundError:
                issues.append(f"npm not available — cannot check: {pkg}")

        # Check pip packages
        for pkg in manifest.pip:
            try:
                import importlib.metadata
                importlib.metadata.version(pkg)
            except Exception:
                issues.append(f"pip package not installed: {pkg}")

        return {
            "profile_id": profile_id,
            "valid": len(issues) == 0,
            "issues": issues,
            "manifest": manifest.to_dict(),
        }

    async def _cmd_install_profile(self, args: dict) -> dict:
        profile_id = args.get("profile_id", "").strip()
        if not profile_id:
            return {"error": "profile_id is required"}

        # Run check first
        check = await self._cmd_check_profile({"profile_id": profile_id})
        if "error" in check:
            return check

        profile = await self.db.get_profile(profile_id)
        from src.known_tools import InstallManifest
        manifest = InstallManifest.from_dict(profile.install)

        if manifest.is_empty:
            return {
                "profile_id": profile_id,
                "installed": [],
                "already_present": [],
                "manual": [],
                "ready": True,
            }

        return await self._install_manifest(profile_id, manifest)

    async def _install_manifest(
        self, profile_id: str, manifest: "InstallManifest",
    ) -> dict:
        """Shared logic for installing an InstallManifest's dependencies."""
        import shutil
        installed: list[str] = []
        already_present: list[str] = []
        manual: list[str] = []

        # Install npm packages
        for pkg in manifest.npm:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "npm", "list", "-g", pkg, "--depth=0",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
                if proc.returncode == 0:
                    already_present.append(f"npm:{pkg}")
                    continue
            except FileNotFoundError:
                manual.append(f"npm not available — install manually: {pkg}")
                continue

            try:
                proc = await asyncio.create_subprocess_exec(
                    "npm", "install", "-g", pkg,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
                if proc.returncode == 0:
                    installed.append(f"npm:{pkg}")
                else:
                    stderr = await proc.stderr.read()
                    manual.append(
                        f"npm install failed for {pkg}: {stderr.decode().strip()}"
                    )
            except Exception as e:
                manual.append(f"npm install failed for {pkg}: {e}")

        # Install pip packages
        for pkg in manifest.pip:
            try:
                import importlib.metadata
                importlib.metadata.version(pkg)
                already_present.append(f"pip:{pkg}")
                continue
            except Exception:
                pass

            try:
                proc = await asyncio.create_subprocess_exec(
                    "pip", "install", pkg,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
                if proc.returncode == 0:
                    installed.append(f"pip:{pkg}")
                else:
                    stderr = await proc.stderr.read()
                    manual.append(
                        f"pip install failed for {pkg}: {stderr.decode().strip()}"
                    )
            except Exception as e:
                manual.append(f"pip install failed for {pkg}: {e}")

        # Check commands — can't auto-install system binaries
        for cmd in manifest.commands:
            if shutil.which(cmd):
                already_present.append(f"cmd:{cmd}")
            else:
                manual.append(f"Command not found (install manually): {cmd}")

        ready = len(manual) == 0
        return {
            "profile_id": profile_id,
            "installed": installed,
            "already_present": already_present,
            "manual": manual,
            "ready": ready,
        }

    # --- Export / import commands ------------------------------------------

    async def _cmd_export_profile(self, args: dict) -> dict:
        import yaml as _yaml
        profile_id = args.get("profile_id", "").strip()
        if not profile_id:
            return {"error": "profile_id is required"}
        profile = await self.db.get_profile(profile_id)
        if not profile:
            return {"error": f"Profile '{profile_id}' not found"}

        data: dict = {
            "id": profile.id,
            "name": profile.name,
        }
        if profile.description:
            data["description"] = profile.description
        if profile.model:
            data["model"] = profile.model
        if profile.permission_mode:
            data["permission_mode"] = profile.permission_mode
        if profile.allowed_tools:
            data["allowed_tools"] = profile.allowed_tools
        if profile.mcp_servers:
            data["mcp_servers"] = profile.mcp_servers
        if profile.system_prompt_suffix:
            data["system_prompt_suffix"] = profile.system_prompt_suffix
        if profile.install:
            data["install"] = profile.install

        yaml_text = f"# Agent Profile: {profile.name}\n"
        yaml_text += _yaml.dump(
            {"agent_profile": data},
            default_flow_style=False,
            sort_keys=False,
        )

        result: dict = {"yaml": yaml_text}

        # Optionally create a GitHub gist
        if args.get("create_gist"):
            import tempfile
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".yaml", delete=False,
                    prefix=f"agent-profile-{profile_id}-",
                ) as f:
                    f.write(yaml_text)
                    tmp_path = f.name

                env = {**os.environ, "GH_PROMPT_DISABLED": "1"}
                proc = await asyncio.create_subprocess_exec(
                    "gh", "gist", "create", "--public",
                    "--desc", f"Agent Profile: {profile.name}",
                    tmp_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    result["gist_url"] = stdout.decode().strip()
                else:
                    result["gist_error"] = stderr.decode().strip()
            except FileNotFoundError:
                result["gist_error"] = "gh CLI not found — install GitHub CLI"
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        return result

    async def _cmd_import_profile(self, args: dict) -> dict:
        import yaml as _yaml
        from src.known_tools import InstallManifest
        source = args.get("source", "").strip()
        if not source:
            return {"error": "source is required (YAML text or gist URL)"}

        # If source looks like a URL, fetch via gh gist
        yaml_text = source
        if source.startswith("http://") or source.startswith("https://"):
            gist_id = source.rstrip("/").split("/")[-1]
            try:
                env = {**os.environ, "GH_PROMPT_DISABLED": "1"}
                proc = await asyncio.create_subprocess_exec(
                    "gh", "gist", "view", gist_id, "--raw",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode != 0:
                    return {"error": f"Failed to fetch gist: {stderr.decode().strip()}"}
                yaml_text = stdout.decode()
            except FileNotFoundError:
                return {"error": "gh CLI not found — install GitHub CLI to import from URLs"}

        try:
            data = _yaml.safe_load(yaml_text)
        except Exception as e:
            return {"error": f"Invalid YAML: {e}"}

        if not isinstance(data, dict) or "agent_profile" not in data:
            return {"error": "YAML must contain an 'agent_profile' key"}

        pdata = data["agent_profile"]
        if not isinstance(pdata, dict):
            return {"error": "agent_profile must be a mapping"}

        profile_id = args.get("id") or pdata.get("id", "")
        if not profile_id:
            return {"error": "Profile must have an 'id' field"}

        overwrite = args.get("overwrite", False)
        existing = await self.db.get_profile(profile_id)
        if existing and not overwrite:
            return {"error": f"Profile '{profile_id}' already exists (use overwrite=true to replace)"}

        profile = AgentProfile(
            id=profile_id,
            name=args.get("name") or pdata.get("name", profile_id),
            description=pdata.get("description", ""),
            model=pdata.get("model", ""),
            permission_mode=pdata.get("permission_mode", ""),
            allowed_tools=pdata.get("allowed_tools", []),
            mcp_servers=pdata.get("mcp_servers", {}),
            system_prompt_suffix=pdata.get("system_prompt_suffix", ""),
            install=pdata.get("install", {}),
        )

        if existing and overwrite:
            await self.db.update_profile(
                profile_id,
                name=profile.name,
                description=profile.description,
                model=profile.model,
                permission_mode=profile.permission_mode,
                allowed_tools=profile.allowed_tools,
                mcp_servers=profile.mcp_servers,
                system_prompt_suffix=profile.system_prompt_suffix,
                install=profile.install,
            )
        else:
            await self.db.create_profile(profile)

        result: dict = {"imported": True, "name": profile.name, "id": profile_id}

        # Auto-install dependencies if manifest is non-empty
        manifest = InstallManifest.from_dict(profile.install)
        if not manifest.is_empty:
            install_result = await self._install_manifest(profile_id, manifest)
            result["installed"] = install_result["installed"]
            result["already_present"] = install_result["already_present"]
            result["manual"] = install_result["manual"]
            result["ready"] = install_result["ready"]
        else:
            result["ready"] = True

        return result
