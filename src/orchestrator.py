from __future__ import annotations

import os
import time
from typing import Callable, Awaitable

from src.adapters.base import MessageCallback
from src.config import AppConfig
from src.database import Database
from src.discord.notifications import (
    format_task_completed, format_task_failed, format_task_blocked,
)
from src.event_bus import EventBus
from src.git.manager import GitManager
from src.models import (
    AgentOutput, AgentResult, AgentState, RepoConfig, RepoSourceType,
    Task, TaskStatus, TaskContext,
)
from src.scheduler import AssignAction, Scheduler, SchedulerState
from src.tokens.budget import BudgetManager

# Callback that sends a formatted string to a Discord channel
NotifyCallback = Callable[[str], Awaitable[None]]


class Orchestrator:
    def __init__(self, config: AppConfig, adapter_factory=None):
        self.config = config
        self.db = Database(config.database_path)
        self.bus = EventBus()
        self.budget = BudgetManager(
            global_budget=config.global_token_budget_daily
        )
        self.git = GitManager()
        self._adapter_factory = adapter_factory
        self._adapters: dict[str, object] = {}  # agent_id -> adapter
        self._notify: NotifyCallback | None = None
        self._control_notify: NotifyCallback | None = None

    def set_notify_callback(self, callback: NotifyCallback) -> None:
        """Set a callback for sending notifications (e.g. to Discord)."""
        self._notify = callback

    def set_control_callback(self, callback: NotifyCallback) -> None:
        """Set a callback for posting full summaries to the control channel."""
        self._control_notify = callback

    async def stop_task(self, task_id: str) -> str | None:
        """Stop an in-progress task. Returns None on success, error string on failure."""
        task = await self.db.get_task(task_id)
        if not task:
            return f"Task '{task_id}' not found"
        if task.status != TaskStatus.IN_PROGRESS:
            return f"Task is not in progress (status: {task.status.value})"

        # Find and stop the adapter
        agent_id = task.assigned_agent_id
        if agent_id and agent_id in self._adapters:
            adapter = self._adapters[agent_id]
            try:
                await adapter.stop()
            except Exception as e:
                print(f"Error stopping adapter for agent {agent_id}: {e}")

        # Reset task and agent state
        await self.db.update_task(task_id, status=TaskStatus.BLOCKED.value,
                                  assigned_agent_id=None)
        if agent_id:
            await self.db.update_agent(agent_id, state=AgentState.IDLE,
                                       current_task_id=None)
            self._adapters.pop(agent_id, None)

        await self._notify_channel(
            f"**Task Stopped:** `{task_id}` — {task.title}"
        )
        return None

    async def _notify_channel(self, message: str) -> None:
        """Send a notification if a callback is set."""
        if self._notify:
            try:
                await self._notify(message)
            except Exception as e:
                print(f"Notification error: {e}")

    async def _control_channel_post(self, message: str) -> None:
        """Post a message to the control channel if a callback is set."""
        if self._control_notify:
            try:
                await self._control_notify(message)
            except Exception as e:
                print(f"Control channel notification error: {e}")

    async def initialize(self) -> None:
        await self.db.initialize()
        await self._recover_stale_state()

    async def _recover_stale_state(self) -> None:
        """Reset any in-flight work from a previous daemon run.

        After a restart, no adapters are actually running, so any tasks
        marked IN_PROGRESS or agents marked BUSY are stale.
        """
        # Reset BUSY agents to IDLE
        agents = await self.db.list_agents()
        for a in agents:
            if a.state in (AgentState.BUSY, AgentState.STARTING):
                print(f"Recovery: resetting agent '{a.name}' from {a.state.value} to IDLE")
                await self.db.update_agent(a.id, state=AgentState.IDLE, current_task_id=None)

        # Reset IN_PROGRESS tasks back to READY so they get re-scheduled
        tasks = await self.db.list_tasks(status=TaskStatus.IN_PROGRESS)
        for t in tasks:
            print(f"Recovery: resetting task '{t.id}' ({t.title}) from IN_PROGRESS to READY")
            await self.db.update_task(t.id, status=TaskStatus.READY.value,
                                      assigned_agent_id=None)

    async def shutdown(self) -> None:
        await self.db.close()

    async def run_one_cycle(self) -> None:
        """Run one complete scheduling + execution cycle."""
        try:
            # 1. Check for PAUSED tasks that should resume
            await self._resume_paused_tasks()

            # 2. Check DEFINED tasks for dependency resolution
            await self._check_defined_tasks()

            # 3. Schedule
            actions = await self._schedule()

            # 4. Execute assigned tasks
            for action in actions:
                try:
                    await self._execute_task(action)
                except Exception as e:
                    print(f"Error executing task {action.task_id}: {e}")
                    import traceback
                    traceback.print_exc()
                    # Try to reset the task/agent so they're not stuck
                    try:
                        await self.db.update_task(
                            action.task_id, status=TaskStatus.READY.value,
                            assigned_agent_id=None)
                        await self.db.update_agent(
                            action.agent_id, state=AgentState.IDLE,
                            current_task_id=None)
                    except Exception:
                        pass
                    await self._notify_channel(
                        f"**Error executing task** `{action.task_id}`: {e}"
                    )
        except Exception as e:
            print(f"Scheduler cycle error: {e}")
            import traceback
            traceback.print_exc()

    async def _resume_paused_tasks(self) -> None:
        paused = await self.db.list_tasks(status=TaskStatus.PAUSED)
        now = time.time()
        for task in paused:
            if task.resume_after and task.resume_after <= now:
                await self.db.update_task(task.id, status=TaskStatus.READY.value,
                                          assigned_agent_id=None, resume_after=None)

    async def _check_defined_tasks(self) -> None:
        defined = await self.db.list_tasks(status=TaskStatus.DEFINED)
        for task in defined:
            deps = await self.db.get_dependencies(task.id)
            if not deps:
                # No dependencies — promote to READY
                await self.db.update_task(task.id, status=TaskStatus.READY.value)
            else:
                deps_met = await self.db.are_dependencies_met(task.id)
                if deps_met:
                    await self.db.update_task(task.id, status=TaskStatus.READY.value)

    async def _schedule(self) -> list[AssignAction]:
        projects = await self.db.list_projects()
        tasks = await self.db.list_tasks()
        agents = await self.db.list_agents()

        # Calculate token usage in window
        window_start = time.time() - (
            self.config.scheduling.rolling_window_hours * 3600
        )
        project_usage = {}
        for p in projects:
            project_usage[p.id] = await self.db.get_project_token_usage(
                p.id, since=window_start
            )

        # Count active agents per project
        active_counts: dict[str, int] = {}
        for a in agents:
            if a.state in (AgentState.BUSY, AgentState.STARTING) and a.current_task_id:
                task = await self.db.get_task(a.current_task_id)
                if task:
                    active_counts[task.project_id] = (
                        active_counts.get(task.project_id, 0) + 1
                    )

        total_used = sum(project_usage.values())

        state = SchedulerState(
            projects=projects,
            tasks=tasks,
            agents=agents,
            project_token_usage=project_usage,
            project_active_agent_counts=active_counts,
            tasks_completed_in_window={},
            global_budget=self.config.global_token_budget_daily,
            global_tokens_used=total_used,
        )

        return Scheduler.schedule(state)

    async def _prepare_workspace(self, task: Task, agent) -> str | None:
        """Prepare a workspace for the task. Returns the workspace path, or None for fallback."""
        if not task.repo_id:
            return None  # No repo — use project workspace

        repo = await self.db.get_repo(task.repo_id)
        if not repo:
            await self._notify_channel(
                f"**Warning:** Repo `{task.repo_id}` not found for task `{task.id}`. "
                f"Falling back to project workspace."
            )
            return None

        branch_name = GitManager.make_branch_name(task.id, task.title)
        agent_checkout = os.path.join(repo.checkout_base_path, agent.name)
        # Derive repo name from url or source_path
        if repo.url:
            repo_name = repo.url.rstrip("/").split("/")[-1].replace(".git", "")
        elif repo.source_path:
            repo_name = os.path.basename(repo.source_path.rstrip("/"))
        else:
            repo_name = repo.id
        workspace = os.path.join(agent_checkout, repo_name)

        if repo.source_type == RepoSourceType.CLONE:
            if not self.git.validate_checkout(workspace):
                os.makedirs(agent_checkout, exist_ok=True)
                self.git.create_checkout(repo.url, workspace)
            self.git.prepare_for_task(workspace, branch_name, repo.default_branch)

        elif repo.source_type == RepoSourceType.LINK:
            if not os.path.isdir(repo.source_path):
                await self._notify_channel(
                    f"**Warning:** Linked repo path `{repo.source_path}` does not exist. "
                    f"Falling back to project workspace."
                )
                return None
            if os.path.isdir(workspace):
                # Worktree already exists — prepare for new task
                self.git.prepare_for_task(workspace, branch_name, repo.default_branch)
            else:
                self.git.create_worktree(repo.source_path, workspace, branch_name)

        elif repo.source_type == RepoSourceType.INIT:
            if not self.git.validate_checkout(workspace):
                self.git.init_repo(workspace)
            self.git.create_branch(workspace, branch_name)

        # Update task and agent in DB
        await self.db.update_task(task.id, branch_name=branch_name)
        await self.db.update_agent(agent.id, checkout_path=workspace)

        return workspace

    async def _complete_workspace(self, task: Task, agent) -> None:
        """Post-completion: merge branch and optionally push (clone repos only)."""
        if not task.repo_id or not task.branch_name:
            return

        repo = await self.db.get_repo(task.repo_id)
        if not repo:
            return

        workspace = agent.checkout_path
        if not workspace or not self.git.validate_checkout(workspace):
            return

        # Merge task branch into default branch
        merged = self.git.merge_branch(workspace, task.branch_name, repo.default_branch)
        if not merged:
            await self._notify_channel(
                f"**Merge Conflict:** Task `{task.id}` branch `{task.branch_name}` "
                f"has conflicts with `{repo.default_branch}`. Manual resolution needed."
            )
            return

        # Only push for clone repos (user controls their own remotes for linked repos)
        if repo.source_type == RepoSourceType.CLONE:
            try:
                self.git.push_branch(workspace, repo.default_branch)
            except Exception as e:
                await self._notify_channel(
                    f"**Push Failed:** Could not push `{repo.default_branch}` for task `{task.id}`: {e}"
                )

    async def _execute_task(self, action: AssignAction) -> None:
        if not self._adapter_factory:
            print(f"Cannot execute task {action.task_id}: no adapter factory configured")
            await self._notify_channel(
                f"**Error:** Cannot execute task `{action.task_id}` — no agent adapter configured."
            )
            return

        # Assign
        await self.db.assign_task_to_agent(action.task_id, action.agent_id)

        # Start agent
        await self.db.update_task(action.task_id,
                                  status=TaskStatus.IN_PROGRESS.value)
        await self.db.update_agent(action.agent_id, state=AgentState.BUSY)

        task = await self.db.get_task(action.task_id)
        agent = await self.db.get_agent(action.agent_id)

        # Prepare workspace (repo checkout/worktree/init)
        project = await self.db.get_project(action.project_id)
        fallback_workspace = (project.workspace_path if project and project.workspace_path
                              else self.config.workspace_dir)
        try:
            repo_workspace = await self._prepare_workspace(task, agent)
            workspace = repo_workspace or fallback_workspace
        except Exception as e:
            await self._notify_channel(
                f"**Workspace Error:** Task `{task.id}` — {e}\n"
                f"Falling back to project workspace."
            )
            workspace = fallback_workspace

        # Re-fetch task/agent in case _prepare_workspace updated them
        task = await self.db.get_task(action.task_id)
        agent = await self.db.get_agent(action.agent_id)

        # Notify that work is starting
        await self._notify_channel(
            f"**Task Started:** `{task.id}` — {task.title}\n"
            f"Agent: {agent.name}"
            + (f"\nBranch: `{task.branch_name}`" if task.branch_name else "")
        )

        adapter = self._adapter_factory.create("claude")
        self._adapters[action.agent_id] = adapter

        # Inject system context so the agent knows where it's working
        full_description = (
            f"## System Context\n"
            f"- Workspace directory: {workspace}\n"
            f"- Global workspaces root: {self.config.workspace_dir}\n"
            f"- Project: {project.name} (id: {project.id})\n"
            f"\n## Task\n"
            f"{task.description}"
        )

        ctx = TaskContext(
            description=full_description,
            checkout_path=workspace,
        )
        await adapter.start(ctx)

        # Build a message callback that prefixes agent output with task info
        async def forward_agent_message(text: str) -> None:
            header = f"`{task.id}` | **{agent.name}**\n"
            await self._notify_channel(header + text)

        output = await adapter.wait(on_message=forward_agent_message)

        # Record tokens
        if output.tokens_used > 0:
            await self.db.record_token_usage(
                action.project_id, action.agent_id,
                action.task_id, output.tokens_used,
            )

        # Persist task result
        try:
            await self.db.save_task_result(action.task_id, action.agent_id, output)
        except Exception as e:
            print(f"Failed to save task result: {e}")

        # Re-fetch task in case retry_count changed
        task = await self.db.get_task(action.task_id)

        # Handle result
        if output.result == AgentResult.COMPLETED:
            await self.db.update_task(action.task_id,
                                      status=TaskStatus.VERIFYING.value)
            # Auto-verify for now (run test commands later)
            await self.db.update_task(action.task_id,
                                      status=TaskStatus.COMPLETED.value)
            await self.db.log_event("task_completed",
                                    project_id=action.project_id,
                                    task_id=action.task_id,
                                    agent_id=action.agent_id)
            # Post-completion: merge branch and push if applicable
            try:
                await self._complete_workspace(task, agent)
            except Exception as e:
                await self._notify_channel(
                    f"**Post-completion git error** for task `{task.id}`: {e}"
                )
            await self._notify_channel(
                format_task_completed(task, agent, output)
            )
            # Post full summary to control channel
            ctrl_lines = [
                f"**Task Completed:** `{task.id}` — {task.title}",
                f"Agent: {agent.name} | Tokens: {output.tokens_used:,}",
            ]
            if output.summary:
                ctrl_lines.append(f"\n**Summary:**\n{output.summary}")
            if output.files_changed:
                ctrl_lines.append(f"\n**Files changed:** {', '.join(output.files_changed)}")
            await self._control_channel_post("\n".join(ctrl_lines))

        elif output.result == AgentResult.FAILED:
            new_retry = task.retry_count + 1
            if new_retry >= task.max_retries:
                await self.db.update_task(action.task_id,
                                          status=TaskStatus.BLOCKED.value,
                                          retry_count=new_retry)
                await self._notify_channel(format_task_blocked(task))
            else:
                await self.db.update_task(action.task_id,
                                          status=TaskStatus.READY.value,
                                          retry_count=new_retry,
                                          assigned_agent_id=None)
                await self._notify_channel(
                    format_task_failed(task, agent, output)
                )
            # Post failure details to control channel
            ctrl_lines = [
                f"**Task Failed:** `{task.id}` — {task.title}",
                f"Agent: {agent.name} | Retry: {new_retry}/{task.max_retries}",
            ]
            if output.error_message:
                ctrl_lines.append(f"\n**Error:**\n{output.error_message}")
            if output.summary:
                ctrl_lines.append(f"\n**Summary:**\n{output.summary}")
            await self._control_channel_post("\n".join(ctrl_lines))

        elif output.result in (
            AgentResult.PAUSED_TOKENS, AgentResult.PAUSED_RATE_LIMIT
        ):
            retry_secs = (
                self.config.pause_retry.rate_limit_backoff_seconds
                if output.result == AgentResult.PAUSED_RATE_LIMIT
                else self.config.pause_retry.token_exhaustion_retry_seconds
            )
            await self.db.update_task(
                action.task_id,
                status=TaskStatus.PAUSED.value,
                resume_after=time.time() + retry_secs,
            )
            reason = "rate limit" if output.result == AgentResult.PAUSED_RATE_LIMIT else "token exhaustion"
            await self._notify_channel(
                f"**Task Paused:** `{task.id}` — {task.title}\n"
                f"Reason: {reason}. Will retry in {retry_secs}s."
            )

        # Free agent
        await self.db.update_agent(action.agent_id,
                                   state=AgentState.IDLE,
                                   current_task_id=None)
