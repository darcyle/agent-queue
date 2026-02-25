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
import json
import os
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from src.config import AppConfig
from src.discord.notifications import classify_error
from src.git.manager import GitError
from src.models import (
    Agent, Hook, Project, ProjectStatus, RepoConfig, RepoSourceType,
    Task, TaskStatus,
)
from src.orchestrator import Orchestrator
from src.task_names import generate_task_id


def _count_by(items, key_fn) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        k = key_fn(item)
        counts[k] = counts.get(k, 0) + 1
    return counts


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
            info = {
                "id": p.id,
                "name": p.name,
                "status": p.status.value,
                "credit_weight": p.credit_weight,
                "max_concurrent_agents": p.max_concurrent_agents,
                "workspace": p.workspace_path,
            }
            if p.discord_channel_id:
                info["discord_channel_id"] = p.discord_channel_id
            result.append(info)
        return {"projects": result}

    async def _cmd_create_project(self, args: dict) -> dict:
        project_id = args["name"].lower().replace(" ", "-")
        workspace = os.path.join(self.config.workspace_dir, project_id)
        os.makedirs(workspace, exist_ok=True)
        project = Project(
            id=project_id,
            name=args["name"],
            credit_weight=args.get("credit_weight", 1.0),
            max_concurrent_agents=args.get("max_concurrent_agents", 2),
            workspace_path=workspace,
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
            "workspace": workspace,
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
        if not updates:
            return {"error": "No fields to update. Provide name, credit_weight, or max_concurrent_agents."}
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

    async def _cmd_create_channel_for_project(self, args: dict) -> dict:
        """Create (or reuse) a dedicated Discord channel for a project.

        **Idempotent:** If a channel with the target name already exists in
        the guild it is linked to the project instead of creating a duplicate.

        Required args:
            project_id:   Project ID (or name) to link the channel to.
        Optional args:
            channel_name: Desired channel name (defaults to project ID).
            category_id:  Discord category ID to place the channel in.
            guild_channels: List of ``{id, name}`` dicts for idempotency
                            lookup (injected by the Discord command layer).
            _created_channel_id: Pre-created channel ID (set by the Discord
                                 command layer when it had to create a new
                                 channel because none matched).

        Returns a dict with ``action`` = ``"linked_existing"`` or
        ``"created"`` so callers can report what happened.
        """
        pid = args.get("project_id") or args.get("project_name")
        if not pid:
            return {"error": "project_id is required"}

        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}

        channel_name = args.get("channel_name") or pid
        # Normalise: strip leading '#' if the user included one.
        channel_name = channel_name.lstrip("#").strip()

        # --- Idempotency: check for an existing channel with this name ---
        existing_channel_id: str | None = None
        guild_channels = args.get("guild_channels")
        if guild_channels:
            for ch in guild_channels:
                if ch["name"] == channel_name:
                    existing_channel_id = str(ch["id"])
                    break

        if existing_channel_id:
            # Channel already exists — link it (no creation needed).
            link_result = await self._cmd_set_project_channel({
                "project_id": pid,
                "channel_id": existing_channel_id,
            })
            if "error" in link_result:
                return link_result
            return {
                **link_result,
                "action": "linked_existing",
                "channel_name": channel_name,
            }

        # --- No existing channel: the Discord layer must create one ---
        # If the caller already created it and passed the ID, link it.
        created_channel_id: str | None = args.get("_created_channel_id")
        if created_channel_id:
            link_result = await self._cmd_set_project_channel({
                "project_id": pid,
                "channel_id": created_channel_id,
            })
            if "error" in link_result:
                return link_result
            return {
                **link_result,
                "action": "created",
                "channel_name": channel_name,
            }

        # Called without guild context and without a pre-created channel.
        # This happens when the LLM chat agent calls the tool (no Discord
        # guild access).  Return an informative error.
        return {
            "error": (
                "Cannot create a Discord channel without guild context. "
                "Use this command from Discord, or use set_project_channel "
                "with an existing channel_id instead."
            )
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

    async def _cmd_list_tasks(self, args: dict) -> dict:
        kwargs = {}
        if "project_id" in args:
            kwargs["project_id"] = args["project_id"]
        if "status" in args:
            kwargs["status"] = TaskStatus(args["status"])
        tasks = await self.db.list_tasks(**kwargs)
        return {
            "tasks": [
                {
                    "id": t.id,
                    "project_id": t.project_id,
                    "title": t.title,
                    "status": t.status.value,
                    "priority": t.priority,
                    "assigned_agent": t.assigned_agent_id,
                }
                for t in tasks[:200]
            ],
            "total": len(tasks),
        }

    async def _cmd_create_task(self, args: dict) -> dict:
        project_id = args.get("project_id")
        if not project_id:
            project_id = "quick-tasks"
            existing = await self.db.get_project(project_id)
            if not existing:
                workspace = os.path.join(self.config.workspace_dir, project_id)
                os.makedirs(workspace, exist_ok=True)
                await self.db.create_project(Project(
                    id=project_id,
                    name="Quick Tasks",
                    credit_weight=0.5,
                    max_concurrent_agents=1,
                    workspace_path=workspace,
                ))
        task_id = await generate_task_id(self.db)
        repo_id = args.get("repo_id")
        requires_approval = args.get("requires_approval", False)
        task = Task(
            id=task_id,
            project_id=project_id,
            title=args["title"],
            description=args["description"],
            priority=args.get("priority", 100),
            status=TaskStatus.READY,
            repo_id=repo_id,
            requires_approval=requires_approval,
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
        }
        if task.pr_url:
            info["pr_url"] = task.pr_url
        return info

    async def _cmd_edit_task(self, args: dict) -> dict:
        task = await self.db.get_task(args["task_id"])
        if not task:
            return {"error": f"Task '{args['task_id']}' not found"}
        updates = {}
        if "title" in args:
            updates["title"] = args["title"]
        if "description" in args:
            updates["description"] = args["description"]
        if "priority" in args:
            updates["priority"] = args["priority"]
        if not updates:
            return {"error": "No fields to update. Provide title, description, or priority."}
        await self.db.update_task(args["task_id"], **updates)
        return {"updated": args["task_id"], "fields": list(updates.keys())}

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

        # Resolve checkout path from agent_workspaces
        checkout_path = None
        if task.assigned_agent_id:
            ws = await self.db.get_agent_workspace(
                task.assigned_agent_id, task.project_id,
            )
            if ws:
                checkout_path = ws.workspace_path

        # Fallback: repo source_path
        repo = None
        if task.repo_id:
            repo = await self.db.get_repo(task.repo_id)
        if not checkout_path and repo and repo.source_path:
            checkout_path = repo.source_path
        if not checkout_path:
            return {"error": "Could not determine checkout path for diff"}

        default_branch = repo.default_branch if repo else "main"
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
        agent_id = args["name"].lower().replace(" ", "-")
        agent = Agent(
            id=agent_id,
            name=args["name"],
            agent_type=args.get("agent_type", "claude"),
        )
        await self.db.create_agent(agent)
        return {"created": agent_id, "name": agent.name}

    async def _cmd_set_agent_workspace(self, args: dict) -> dict:
        """Set the workspace path for an agent in a specific project."""
        agent_id = args["agent_id"]
        project_id = args["project_id"]
        workspace_path = args["workspace_path"]
        repo_id = args.get("repo_id")

        agent = await self.db.get_agent(agent_id)
        if not agent:
            return {"error": f"Agent '{agent_id}' not found"}
        project = await self.db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}
        if repo_id:
            repo = await self.db.get_repo(repo_id)
            if not repo:
                return {"error": f"Repo '{repo_id}' not found"}

        await self.db.set_agent_workspace(
            agent_id, project_id, workspace_path, repo_id=repo_id,
        )
        result = {
            "agent_id": agent_id,
            "project_id": project_id,
            "workspace_path": workspace_path,
        }
        if repo_id:
            result["repo_id"] = repo_id
        return result

    # -----------------------------------------------------------------------
    # Repo commands -- register repositories for projects.
    # Three source types: "clone" (git URL -- agents get isolated checkouts),
    # "link" (existing directory on disk -- agents work in-place), and "init"
    # (create a new empty git repo in the project workspace).
    # -----------------------------------------------------------------------

    async def _cmd_add_repo(self, args: dict) -> dict:
        project_id = args["project_id"]
        project = await self.db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}

        source = args["source"]
        source_type = RepoSourceType(source)
        url = args.get("url", "")
        path = args.get("path", "")
        default_branch = args.get("default_branch", "main")

        if source_type == RepoSourceType.CLONE and not url:
            return {"error": "Clone repos require a 'url' parameter"}
        if source_type == RepoSourceType.LINK and not path:
            return {"error": "Link repos require a 'path' parameter"}
        if source_type == RepoSourceType.LINK and not os.path.isdir(path):
            return {"error": f"Path '{path}' does not exist or is not a directory"}

        repo_name = args.get("name")
        if not repo_name:
            if url:
                repo_name = url.rstrip("/").split("/")[-1].replace(".git", "")
            elif path:
                repo_name = os.path.basename(path.rstrip("/"))
            else:
                repo_name = f"{project_id}-repo"

        repo_id = repo_name.lower().replace(" ", "-")

        repo = RepoConfig(
            id=repo_id,
            project_id=project_id,
            source_type=source_type,
            url=url,
            source_path=path,
            default_branch=default_branch,
        )
        await self.db.create_repo(repo)
        return {
            "created": repo_id,
            "name": repo_name,
            "source_type": source,
        }

    async def _cmd_list_repos(self, args: dict) -> dict:
        project_id = args.get("project_id")
        repos = await self.db.list_repos(project_id=project_id)
        return {
            "repos": [
                {
                    "id": r.id,
                    "project_id": r.project_id,
                    "source_type": r.source_type.value,
                    "url": r.url,
                    "source_path": r.source_path,
                    "default_branch": r.default_branch,
                    "checkout_base_path": r.checkout_base_path,
                }
                for r in repos
            ]
        }

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
        repos = await self.db.list_repos(project_id=project_id)
        repo_statuses = []

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
            workspace = project.workspace_path
            if not workspace or not os.path.isdir(workspace):
                return {
                    "error": f"Project '{project_id}' has no repos and no valid workspace path"
                }

            if not git.validate_checkout(workspace):
                return {
                    "error": f"Project workspace '{workspace}' is not a git repository"
                }

            branch = git.get_current_branch(workspace)
            status_output = git.get_status(workspace)
            recent_commits = git.get_recent_commits(workspace, count=5)

            repo_statuses.append({
                "repo_id": "(workspace)",
                "path": workspace,
                "branch": branch,
                "status": status_output or "(clean)",
                "recent_commits": recent_commits,
            })

        return {
            "project_id": project_id,
            "project_name": project.name,
            "repos": repo_statuses,
        }

    async def _resolve_repo_path(
        self, args: dict,
    ) -> tuple[str | None, RepoConfig | None, dict | None]:
        """Resolve the git checkout path for a project/repo pair.

        Returns ``(checkout_path, repo_config, error_dict)``.
        On success *error_dict* is ``None``.  On failure *checkout_path* is
        ``None``.

        When only *repo_id* is supplied (without *project_id*) the repo is
        looked up directly — this keeps older repo-id-only commands working.

        When neither *project_id* nor *repo_id* is supplied, falls back to
        the active project (``_active_project_id``) so that commands issued
        in a project channel work without explicitly specifying identifiers.
        """
        project_id = args.get("project_id")
        repo_id = args.get("repo_id")

        # Fall back to the active project when no identifiers are supplied.
        if not project_id and not repo_id:
            if self._active_project_id:
                project_id = self._active_project_id
                args["project_id"] = project_id  # inject for downstream use
            else:
                return None, None, {"error": "project_id is required (no active project set)"}
        elif not project_id and repo_id:
            # When only repo_id is given, try to inherit project context from
            # the active project so downstream code can reference args["project_id"].
            if self._active_project_id:
                project_id = self._active_project_id
                args["project_id"] = project_id

        project = None
        if project_id:
            project = await self.db.get_project(project_id)
            if not project:
                return None, None, {"error": f"Project '{project_id}' not found"}

        git = self.orchestrator.git

        if repo_id:
            repo = await self.db.get_repo(repo_id)
            if not repo:
                return None, None, {"error": f"Repo '{repo_id}' not found"}
        elif project_id:
            repos = await self.db.list_repos(project_id=project_id)
            repo = repos[0] if repos else None
        else:
            repo = None

        if repo:
            if repo.source_type == RepoSourceType.LINK and repo.source_path:
                checkout_path = repo.source_path
            elif repo.source_type in (RepoSourceType.CLONE, RepoSourceType.INIT) and repo.checkout_base_path:
                checkout_path = repo.checkout_base_path
            else:
                return None, repo, {"error": f"Repo '{repo.id}' has no usable path"}
        else:
            if not project:
                return None, None, {"error": "No repo found and no project context"}
            checkout_path = project.workspace_path
            if not checkout_path or not os.path.isdir(checkout_path):
                return None, None, {"error": f"Project '{project_id}' has no repos and no valid workspace"}

        if not os.path.isdir(checkout_path):
            return None, repo, {"error": f"Path not found: {checkout_path}"}
        if not git.validate_checkout(checkout_path):
            return None, repo, {"error": f"Not a valid git repository: {checkout_path}"}

        return checkout_path, repo, None

    async def _cmd_git_commit(self, args: dict) -> dict:
        """Stage all changes and create a commit in a repository."""
        message = args["message"]
        checkout_path, repo, err = await self._resolve_repo_path(args)
        if err:
            return err
        repo_id = args.get("repo_id") or (repo.id if repo else "(workspace)")
        try:
            committed = self.orchestrator.git.commit_all(checkout_path, message)
        except GitError as e:
            return {"error": str(e)}
        if not committed:
            return {"repo_id": repo_id, "committed": False, "message": "Nothing to commit — working tree clean"}
        return {"repo_id": repo_id, "committed": True, "commit_message": message}

    async def _cmd_git_push(self, args: dict) -> dict:
        """Push a branch to the remote origin."""
        checkout_path, repo, err = await self._resolve_repo_path(args)
        if err:
            return err
        repo_id = args.get("repo_id") or (repo.id if repo else "(workspace)")
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
        checkout_path, repo, err = await self._resolve_repo_path(args)
        if err:
            return err
        repo_id = args.get("repo_id") or (repo.id if repo else "(workspace)")
        try:
            self.orchestrator.git.create_branch(checkout_path, branch_name)
        except GitError as e:
            return {"error": str(e)}
        return {"repo_id": repo_id, "created_branch": branch_name}

    async def _cmd_git_merge(self, args: dict) -> dict:
        """Merge a branch into the default branch."""
        branch_name = args["branch_name"]
        checkout_path, repo, err = await self._resolve_repo_path(args)
        if err:
            return err
        repo_id = args.get("repo_id") or (repo.id if repo else "(workspace)")
        default_branch = args.get("default_branch") or (repo.default_branch if repo else "main") or "main"
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
        checkout_path, repo, err = await self._resolve_repo_path(args)
        if err:
            return err
        repo_id = args.get("repo_id") or (repo.id if repo else "(workspace)")
        git = self.orchestrator.git
        branch = args.get("branch") or git.get_current_branch(checkout_path)
        if not branch:
            return {"error": "Could not determine current branch"}
        base = args.get("base") or (repo.default_branch if repo else "main") or "main"
        try:
            pr_url = git.create_pr(checkout_path, branch, title, body, base)
        except GitError as e:
            return {"error": str(e)}
        return {"repo_id": repo_id, "pr_url": pr_url, "branch": branch, "base": base}

    async def _cmd_git_changed_files(self, args: dict) -> dict:
        """List files changed compared to a base branch."""
        checkout_path, repo, err = await self._resolve_repo_path(args)
        if err:
            return err
        repo_id = args.get("repo_id") or (repo.id if repo else "(workspace)")
        base_branch = args.get("base_branch") or (repo.default_branch if repo else "main") or "main"
        files = self.orchestrator.git.get_changed_files(checkout_path, base_branch)
        return {
            "repo_id": repo_id,
            "base_branch": base_branch,
            "files": files,
            "count": len(files),
        }

    async def _cmd_git_log(self, args: dict) -> dict:
        """Show recent commit log for a repository."""
        checkout_path, repo, err = await self._resolve_repo_path(args)
        if err:
            return err

        git = self.orchestrator.git
        count = args.get("count", 10)

        log_output = git.get_recent_commits(checkout_path, count=count)
        branch = git.get_current_branch(checkout_path)

        return {
            "project_id": args["project_id"],
            "repo_id": repo.id if repo else "(workspace)",
            "branch": branch,
            "log": log_output or "(no commits)",
        }

    # -- Additional project-based git commands ------------------------------

    async def _cmd_git_branch(self, args: dict) -> dict:
        """List branches or create a new branch.

        If ``name`` is provided a new branch is created and checked out;
        otherwise all local branches are listed.
        """

        checkout_path, repo, err = await self._resolve_repo_path(args)
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

        checkout_path, repo, err = await self._resolve_repo_path(args)
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
        checkout_path, repo, err = await self._resolve_repo_path(args)
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
            "repo_id": repo.id if repo else "(workspace)",
            "base_branch": base or "(working tree)",
            "diff": diff or "(no changes)",
        }

    async def _cmd_create_branch(self, args: dict) -> dict:
        """Create and switch to a new branch in a project's repo."""
        branch_name = args.get("branch_name")
        if not branch_name:
            return {"error": "branch_name is required"}

        checkout_path, repo, err = await self._resolve_repo_path(args)
        if err:
            return err

        git = self.orchestrator.git
        try:
            git.create_branch(checkout_path, branch_name)
        except GitError as e:
            return {"error": str(e)}

        return {
            "project_id": args["project_id"],
            "repo_id": repo.id if repo else "(workspace)",
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

        checkout_path, repo, err = await self._resolve_repo_path(args)
        if err:
            return err

        git = self.orchestrator.git
        try:
            git.checkout_branch(checkout_path, branch_name)
        except GitError as e:
            return {"error": str(e)}

        result = {
            "project_id": args["project_id"],
            "repo_id": repo.id if repo else "(workspace)",
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

        checkout_path, repo, err = await self._resolve_repo_path(args)
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
                "repo_id": repo.id if repo else "(workspace)",
                "status": "nothing_to_commit",
                "message": "No changes to commit",
            }

        result = {
            "project_id": args["project_id"],
            "repo_id": repo.id if repo else "(workspace)",
            "commit_message": message,
            "status": "committed",
        }
        warning = await self._warn_if_in_progress(args["project_id"])
        if warning:
            result["warning"] = warning
        return result

    async def _cmd_push_branch(self, args: dict) -> dict:
        """Push the current (or specified) branch to origin."""
        checkout_path, repo, err = await self._resolve_repo_path(args)
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
            "repo_id": repo.id if repo else "(workspace)",
            "branch": branch_name,
            "status": "pushed",
        }

    async def _cmd_merge_branch(self, args: dict) -> dict:
        """Merge a branch into the default branch in a project's repo."""
        branch_name = args.get("branch_name")
        if not branch_name:
            return {"error": "branch_name is required"}

        checkout_path, repo, err = await self._resolve_repo_path(args)
        if err:
            return err

        git = self.orchestrator.git
        default_branch = repo.default_branch if repo else "main"

        try:
            success = git.merge_branch(checkout_path, branch_name, default_branch)
        except GitError as e:
            return {"error": str(e)}

        warning = await self._warn_if_in_progress(args["project_id"])

        if not success:
            result = {
                "project_id": args["project_id"],
                "repo_id": repo.id if repo else "(workspace)",
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
            "repo_id": repo.id if repo else "(workspace)",
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
        workspace = project.workspace_path or os.path.join(
            self.config.workspace_dir, args["project_id"]
        )
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
        workspace = project.workspace_path or os.path.join(
            self.config.workspace_dir, args["project_id"]
        )
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
        workspace = project.workspace_path or os.path.join(
            self.config.workspace_dir, args["project_id"]
        )
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
        workspace = project.workspace_path or os.path.join(
            self.config.workspace_dir, args["project_id"]
        )
        notes_dir = os.path.join(workspace, "notes")
        os.makedirs(notes_dir, exist_ok=True)
        slug = self.orchestrator.git.slugify(args["title"])
        if not slug:
            return {"error": "Title produces an empty filename"}
        fpath = os.path.join(notes_dir, f"{slug}.md")
        existed = os.path.isfile(fpath)
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
            note_filename = f"{slug}.md"
            await self.on_note_written(
                args["project_id"], note_filename, fpath,
            )
        return result

    async def _cmd_compare_specs_notes(self, args: dict) -> dict:
        project = await self.db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        workspace = project.workspace_path or os.path.join(
            self.config.workspace_dir, args["project_id"]
        )

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
        workspace = project.workspace_path or os.path.join(
            self.config.workspace_dir, args["project_id"]
        )
        notes_dir = os.path.join(workspace, "notes")
        fpath = self._resolve_note_path(notes_dir, args["title"])
        if not fpath:
            return {"error": f"Note '{args['title']}' not found"}
        os.remove(fpath)
        return {"deleted": fpath, "title": args["title"]}

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
            project = await self.db.get_project(working_dir)
            if project and project.workspace_path:
                working_dir = project.workspace_path
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
