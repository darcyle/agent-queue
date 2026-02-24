"""Shared command handler for AgentQueue.

This module provides the single code path for all operational commands.
Both the Discord slash commands and the chat agent LLM tools delegate
their business logic here, keeping formatting and presentation separate.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from src.config import AppConfig
from src.discord.notifications import classify_error
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
    """Unified command execution layer for AgentQueue.

    Both the Discord bot and the chat agent delegate to this handler.
    Commands accept a dict of arguments and return a dict result.
    """

    def __init__(self, orchestrator: Orchestrator, config: AppConfig):
        self.orchestrator = orchestrator
        self.config = config
        self._active_project_id: str | None = None

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
    # Project commands
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
            if p.discord_control_channel_id:
                info["discord_control_channel_id"] = p.discord_control_channel_id
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
        return {"created": project_id, "name": project.name, "workspace": workspace}

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
        """Link an existing Discord channel to a project for notifications or control."""
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}

        channel_id = args["channel_id"]
        channel_type = args.get("channel_type", "notifications")
        if channel_type not in ("notifications", "control"):
            return {"error": "channel_type must be 'notifications' or 'control'"}

        if channel_type == "control":
            await self.db.update_project(pid, discord_control_channel_id=channel_id)
        else:
            await self.db.update_project(pid, discord_channel_id=channel_id)

        return {
            "project_id": pid,
            "channel_id": channel_id,
            "channel_type": channel_type,
            "status": "linked",
        }

    async def _cmd_get_project_channels(self, args: dict) -> dict:
        """Return the Discord channel IDs configured for a project."""
        pid = args["project_id"]
        project = await self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        return {
            "project_id": pid,
            "notifications_channel_id": project.discord_channel_id,
            "control_channel_id": project.discord_control_channel_id,
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
        await self.db.delete_project(pid)
        return {"deleted": pid, "name": project.name}

    # -----------------------------------------------------------------------
    # Task commands
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
                for t in tasks[:25]
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
        await self.db.update_task(
            args["task_id"],
            status=TaskStatus.READY.value,
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
        await self.db.update_task(
            args["task_id"],
            status=TaskStatus.COMPLETED.value,
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
        await self.db.update_task(task_id, status=TaskStatus(new_status))
        return {
            "task_id": task_id,
            "old_status": old_status,
            "new_status": new_status,
            "title": task.title,
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
        if not task.repo_id:
            return {"error": "Task has no associated repository"}
        repo = await self.db.get_repo(task.repo_id)
        if not repo:
            return {"error": f"Repository '{task.repo_id}' not found"}
        if not task.branch_name:
            return {"error": "Task has no branch name"}

        checkout_path = None
        if task.assigned_agent_id:
            agent = await self.db.get_agent(task.assigned_agent_id)
            if agent and agent.checkout_path:
                checkout_path = agent.checkout_path
        if not checkout_path and repo.source_path:
            checkout_path = repo.source_path
        if not checkout_path:
            return {"error": "Could not determine checkout path for diff"}

        diff = self.orchestrator.git.get_diff(checkout_path, repo.default_branch)
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
    # Agent commands
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
        repo_id = args.get("repo_id")
        if repo_id:
            repo = await self.db.get_repo(repo_id)
            if not repo:
                return {"error": f"Repo '{repo_id}' not found"}
        agent = Agent(
            id=agent_id,
            name=args["name"],
            agent_type=args.get("agent_type", "claude"),
            repo_id=repo_id,
        )
        await self.db.create_agent(agent)
        result = {"created": agent_id, "name": agent.name}
        if repo_id:
            result["repo_id"] = repo_id
        return result

    # -----------------------------------------------------------------------
    # Repo commands
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
        checkout_base = os.path.join(
            project.workspace_path or self.config.workspace_dir, "repos", repo_name
        )

        repo = RepoConfig(
            id=repo_id,
            project_id=project_id,
            source_type=source_type,
            url=url,
            source_path=path,
            default_branch=default_branch,
            checkout_base_path=checkout_base,
        )
        await self.db.create_repo(repo)
        return {
            "created": repo_id,
            "name": repo_name,
            "source_type": source,
            "checkout_base_path": checkout_base,
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
    # Events and token usage
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
    # Git commands
    # -----------------------------------------------------------------------

    async def _cmd_get_git_status(self, args: dict) -> dict:
        project_id = args["project_id"]
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

    # -----------------------------------------------------------------------
    # Hook commands
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
    # Notes commands
    # -----------------------------------------------------------------------

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
        return {
            "path": fpath,
            "title": args["title"],
            "status": "updated" if existed else "created",
        }

    async def _cmd_delete_note(self, args: dict) -> dict:
        project = await self.db.get_project(args["project_id"])
        if not project:
            return {"error": f"Project '{args['project_id']}' not found"}
        workspace = project.workspace_path or os.path.join(
            self.config.workspace_dir, args["project_id"]
        )
        slug = self.orchestrator.git.slugify(args["title"])
        fpath = os.path.join(workspace, "notes", f"{slug}.md")
        if not os.path.isfile(fpath):
            return {"error": f"Note '{args['title']}' not found"}
        os.remove(fpath)
        return {"deleted": fpath, "title": args["title"]}

    # -----------------------------------------------------------------------
    # System / control commands
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
    # File / shell commands (chat-agent-only tools — no Discord slash equiv)
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
