from __future__ import annotations

import asyncio
import os
import time
from typing import Callable, Awaitable

from src.adapters.base import MessageCallback
from src.config import AppConfig
from src.database import Database
from src.discord.notifications import (
    format_task_completed, format_task_failed, format_task_blocked,
    format_pr_created,
)
from src.event_bus import EventBus
from src.git.manager import GitManager
from src.models import (
    AgentOutput, AgentResult, AgentState, RepoConfig, RepoSourceType,
    Task, TaskStatus, TaskContext,
)
from src.hooks import HookEngine
from src.scheduler import AssignAction, Scheduler, SchedulerState
from src.tokens.budget import BudgetManager

# Callback that sends a formatted string to a Discord channel
NotifyCallback = Callable[[str], Awaitable[None]]

# Callback that creates a thread and returns two send functions:
#   [0] send_to_thread  — streams content into the thread
#   [1] notify_main     — posts a brief message to the main channel (e.g. a reply
#                         to the thread-root message in the notifications channel)
# Args: (thread_name, initial_message) -> (send_to_thread, notify_main) | None
CreateThreadCallback = Callable[[str, str], Awaitable[tuple[NotifyCallback, NotifyCallback] | None]]


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
        self._running_tasks: dict[str, asyncio.Task] = {}  # task_id -> asyncio.Task
        self._notify: NotifyCallback | None = None
        self._control_notify: NotifyCallback | None = None
        self._create_thread: CreateThreadCallback | None = None
        self._paused: bool = False
        self._last_approval_check: float = 0.0
        self.hooks: HookEngine | None = None

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def set_notify_callback(self, callback: NotifyCallback) -> None:
        """Set a callback for sending notifications (e.g. to Discord)."""
        self._notify = callback

    def set_control_callback(self, callback: NotifyCallback) -> None:
        """Set a callback for posting full summaries to the control channel."""
        self._control_notify = callback

    def set_create_thread_callback(self, callback: CreateThreadCallback) -> None:
        """Set a callback for creating per-task threads."""
        self._create_thread = callback

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
        if self.config.hook_engine.enabled:
            self.hooks = HookEngine(self.db, self.bus, self.config)
            self.hooks.set_orchestrator(self)
            await self.hooks.initialize()

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
        if self.hooks:
            await self.hooks.shutdown()
        await self.db.close()

    async def run_one_cycle(self) -> None:
        """Run one complete scheduling + execution cycle."""
        try:
            # 0. Check AWAITING_APPROVAL tasks for PR merge status
            await self._check_awaiting_approval()

            # 1. Check for PAUSED tasks that should resume
            await self._resume_paused_tasks()

            # 2. Check DEFINED tasks for dependency resolution
            await self._check_defined_tasks()

            # 3. Schedule (skipped when orchestrator is paused)
            if not self._paused:
                actions = await self._schedule()
            else:
                actions = []

            # 4. Launch assigned tasks as background coroutines
            # Clean up completed background tasks
            done = [tid for tid, t in self._running_tasks.items() if t.done()]
            for tid in done:
                self._running_tasks.pop(tid)

            for action in actions:
                if action.task_id in self._running_tasks:
                    continue  # Already running
                bg = asyncio.create_task(self._execute_task_safe(action))
                self._running_tasks[action.task_id] = bg

            # 5. Run hook engine tick
            if self.hooks:
                await self.hooks.tick()
        except Exception as e:
            print(f"Scheduler cycle error: {e}")
            import traceback
            traceback.print_exc()

    async def _execute_task_safe(self, action: AssignAction) -> None:
        """Wrapper around _execute_task that catches exceptions and enforces timeout."""
        timeout = self.config.agents_config.stuck_timeout_seconds
        try:
            await asyncio.wait_for(self._execute_task(action), timeout=timeout)
        except asyncio.TimeoutError:
            print(f"Task {action.task_id} timed out after {timeout}s")
            # Stop the adapter if it's still running
            if action.agent_id in self._adapters:
                try:
                    await self._adapters[action.agent_id].stop()
                except Exception:
                    pass
            await self.db.update_task(
                action.task_id, status=TaskStatus.BLOCKED.value,
                assigned_agent_id=None)
            await self.db.update_agent(
                action.agent_id, state=AgentState.IDLE,
                current_task_id=None)
            self._adapters.pop(action.agent_id, None)
            await self._notify_channel(
                f"**Task Timed Out:** `{action.task_id}` — exceeded {timeout}s. Marked as BLOCKED."
            )
            return
        except Exception as e:
            print(f"Error executing task {action.task_id}: {e}")
            import traceback
            traceback.print_exc()
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
        finally:
            self._running_tasks.pop(action.task_id, None)

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
        # Use task's repo, or fall back to agent's assigned repo
        repo_id = task.repo_id or agent.repo_id
        if not repo_id:
            return None  # No repo — use project workspace

        repo = await self.db.get_repo(repo_id)
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
            # Work directly in the source directory (preserves .env, venv, etc.)
            workspace = repo.source_path
            if self.git.validate_checkout(workspace):
                self.git.prepare_for_task(workspace, branch_name, repo.default_branch)
            else:
                # Not a git repo — just use the directory as-is
                pass

        elif repo.source_type == RepoSourceType.INIT:
            if not self.git.validate_checkout(workspace):
                self.git.init_repo(workspace)
            self.git.create_branch(workspace, branch_name)

        # Update task and agent in DB
        await self.db.update_task(task.id, branch_name=branch_name)
        await self.db.update_agent(agent.id, checkout_path=workspace)

        return workspace

    async def _complete_workspace(self, task: Task, agent) -> str | None:
        """Post-completion: commit agent work, then merge or create PR.

        Returns a PR URL if one was created, otherwise None.
        """
        workspace = agent.checkout_path
        if not workspace or not self.git.validate_checkout(workspace):
            return None

        if not task.branch_name:
            return None

        # Commit any uncommitted work on the task branch
        committed = self.git.commit_all(
            workspace, f"agent: {task.title}\n\nTask-Id: {task.id}"
        )
        if not committed:
            print(f"Task {task.id}: no changes to commit on branch {task.branch_name}")

        # Resolve repo config (task's repo or agent's assigned repo)
        repo_id = task.repo_id or agent.repo_id
        repo = await self.db.get_repo(repo_id) if repo_id else None

        if repo and task.requires_approval:
            return await self._create_pr_for_task(task, repo, workspace)
        elif repo:
            await self._merge_and_push(task, repo, workspace)
            return None

        # No repo config — changes are committed on the branch but
        # no merge/push/PR is attempted (e.g. local-only workspace)
        return None

    async def _merge_and_push(self, task: Task, repo: RepoConfig, workspace: str) -> None:
        """Merge the task branch into default and push (clone repos only)."""
        merged = self.git.merge_branch(workspace, task.branch_name, repo.default_branch)
        if not merged:
            await self._notify_channel(
                f"**Merge Conflict:** Task `{task.id}` branch `{task.branch_name}` "
                f"has conflicts with `{repo.default_branch}`. Manual resolution needed."
            )
            return

        if repo.source_type == RepoSourceType.CLONE:
            try:
                self.git.push_branch(workspace, repo.default_branch)
            except Exception as e:
                await self._notify_channel(
                    f"**Push Failed:** Could not push `{repo.default_branch}` for task `{task.id}`: {e}"
                )

    async def _create_pr_for_task(
        self, task: Task, repo: RepoConfig, workspace: str,
    ) -> str | None:
        """Push the task branch and create a PR. Returns the PR URL or None."""
        if repo.source_type == RepoSourceType.LINK:
            # LINK repos typically have no remote — notify user to review manually
            await self._notify_channel(
                f"**Approval Required:** Task `{task.id}` — {task.title}\n"
                f"Branch `{task.branch_name}` is ready for review in `{workspace}`.\n"
                f"Use the `approve_task` command to complete it."
            )
            return None

        try:
            self.git.push_branch(workspace, task.branch_name)
        except Exception as e:
            await self._notify_channel(
                f"**Push Failed:** Could not push branch `{task.branch_name}` "
                f"for task `{task.id}`: {e}"
            )
            return None

        try:
            pr_url = self.git.create_pr(
                workspace,
                branch=task.branch_name,
                title=task.title,
                body=f"Automated PR for task `{task.id}`.\n\n{task.description[:500]}",
                base=repo.default_branch,
            )
            return pr_url
        except Exception as e:
            await self._notify_channel(
                f"**PR Creation Failed:** Task `{task.id}` — {e}\n"
                f"Branch `{task.branch_name}` has been pushed. Create a PR manually."
            )
            return None

    async def _check_awaiting_approval(self) -> None:
        """Poll PR status for tasks awaiting approval. Throttled to once per 60s."""
        now = time.time()
        if now - self._last_approval_check < 60:
            return
        self._last_approval_check = now

        tasks = await self.db.list_tasks(status=TaskStatus.AWAITING_APPROVAL)
        for task in tasks:
            if not task.pr_url:
                continue

            # Need a checkout path to run gh commands
            checkout_path = None
            if task.assigned_agent_id:
                agent = await self.db.get_agent(task.assigned_agent_id)
                if agent and agent.checkout_path:
                    checkout_path = agent.checkout_path
            if not checkout_path and task.repo_id:
                repo = await self.db.get_repo(task.repo_id)
                if repo and repo.source_path:
                    checkout_path = repo.source_path

            if not checkout_path:
                continue

            try:
                merged = self.git.check_pr_merged(checkout_path, task.pr_url)
            except Exception as e:
                print(f"Error checking PR for task {task.id}: {e}")
                continue

            if merged is True:
                await self.db.update_task(
                    task.id, status=TaskStatus.COMPLETED.value)
                await self.db.log_event(
                    "task_completed", project_id=task.project_id,
                    task_id=task.id)
                await self._notify_channel(
                    f"**PR Merged:** Task `{task.id}` — {task.title} is now COMPLETED."
                )
            elif merged is None:
                # Closed without merge
                await self.db.update_task(
                    task.id, status=TaskStatus.BLOCKED.value)
                await self._notify_channel(
                    f"**PR Closed:** Task `{task.id}` — {task.title} "
                    f"was closed without merging. Marked as BLOCKED."
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
        start_msg = (
            f"**Task Started:** `{task.id}` — {task.title}\n"
            f"Agent: {agent.name}"
            + (f"\nBranch: `{task.branch_name}`" if task.branch_name else "")
        )
        await self._notify_channel(start_msg)

        # Create a thread for streaming agent output
        thread_send: NotifyCallback | None = None
        thread_main_notify: NotifyCallback | None = None
        if self._create_thread:
            try:
                thread_name = f"{task.id} | {task.title}"[:100]
                thread_result = await self._create_thread(thread_name, start_msg)
                if thread_result:
                    thread_send, thread_main_notify = thread_result
                    print(f"Created thread for task {task.id}")
                else:
                    print(f"Thread creation returned None for task {task.id}")
            except Exception as e:
                import traceback
                print(f"Failed to create thread for task {task.id}: {e}")
                traceback.print_exc()
        else:
            print(f"No thread callback set for task {task.id}")

        adapter = self._adapter_factory.create("claude")
        self._adapters[action.agent_id] = adapter

        # Inject system context so the agent knows where it's working
        context_lines = [
            "## System Context",
            f"- Workspace directory: {workspace}",
            f"- Global workspaces root: {self.config.workspace_dir}",
            f"- Project: {project.name} (id: {project.id})",
        ]
        if task.branch_name:
            context_lines.append(f"- Git branch: {task.branch_name}")

        context_lines.append(
            "\n## Important: Committing Your Work\n"
            "When you have finished making changes, you MUST commit your work:\n"
            "1. `git add` the files you changed\n"
            "2. `git commit` with a descriptive message\n"
            "Do NOT push — the system handles pushing and PR creation."
        )

        context_lines.append(f"\n## Task\n{task.description}")

        full_description = "\n".join(context_lines)

        ctx = TaskContext(
            description=full_description,
            checkout_path=workspace,
            branch_name=task.branch_name or "",
        )
        await adapter.start(ctx)

        # Stream agent messages to the task thread (or fall back to notifications)
        async def forward_agent_message(text: str) -> None:
            if thread_send:
                await thread_send(text)
            else:
                header = f"`{task.id}` | **{agent.name}**\n"
                await self._notify_channel(header + text)

        # ------------------------------------------------------------------ #
        # Exponential-backoff retry loop for Claude API rate limits.
        #
        # On every PAUSED_RATE_LIMIT result we:
        #   1. Post an immediate "rate-limited" notice to Discord.
        #   2. Sleep for an exponentially-growing delay (base * 2^attempt,
        #      capped at rate_limit_max_backoff_seconds).
        #   3. Post a "resuming now" notice to Discord.
        #   4. Re-initialise the adapter and retry the query.
        #
        # After rate_limit_max_retries consecutive rate-limit hits we give
        # up and fall through to the normal PAUSED_RATE_LIMIT path, which
        # pauses the task in the DB and retries it in the next scheduler
        # cycle.
        #
        # NOTE: The total sleep time across all retries may exceed
        # agents_config.stuck_timeout_seconds.  If you enable multiple
        # retries, raise stuck_timeout_seconds in your config accordingly.
        # ------------------------------------------------------------------ #
        _rl_base = self.config.pause_retry.rate_limit_backoff_seconds
        _rl_max_backoff = self.config.pause_retry.rate_limit_max_backoff_seconds
        _rl_max_retries = self.config.pause_retry.rate_limit_max_retries
        _rl_attempt = 0

        while True:
            output = await adapter.wait(on_message=forward_agent_message)

            if output.result != AgentResult.PAUSED_RATE_LIMIT:
                break  # Completed, failed, or token-exhausted — leave the loop.

            _rl_attempt += 1
            if _rl_attempt > _rl_max_retries:
                # Auto-retries exhausted; let the normal PAUSED handling take over.
                print(
                    f"Task {task.id}: rate-limit retries exhausted "
                    f"({_rl_max_retries}), pausing task."
                )
                break

            _backoff = min(_rl_base * (2 ** (_rl_attempt - 1)), _rl_max_backoff)
            print(
                f"Task {task.id}: rate limited "
                f"(attempt {_rl_attempt}/{_rl_max_retries}), "
                f"waiting {_backoff}s before retry."
            )

            await self._notify_channel(
                "⏳ Claude is currently rate-limited. We will try again in a moment."
            )

            await asyncio.sleep(_backoff)

            await self._notify_channel("✅ Rate limit cleared — resuming now.")

            # Re-initialise the adapter so the next call starts a fresh query.
            await adapter.start(ctx)

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

        # Helper: post to thread if available, otherwise to notifications channel.
        # Used for in-progress updates (e.g. git errors, paused notices).
        async def _post(msg: str) -> None:
            if thread_send:
                await thread_send(msg)
            else:
                await self._notify_channel(msg)

        # Helper: post a brief notification to the main (notifications) channel.
        # When a thread exists this replies to the thread-root message so the
        # notification is visually linked to the thread.  Falls back to a plain
        # channel message when no thread is available.
        async def _notify_brief(msg: str) -> None:
            if thread_main_notify:
                await thread_main_notify(msg)
            else:
                await self._notify_channel(msg)

        # Handle result
        if output.result == AgentResult.COMPLETED:
            await self.db.update_task(action.task_id,
                                      status=TaskStatus.VERIFYING.value)

            # Post-completion: commit, merge or create PR
            pr_url = None
            try:
                pr_url = await self._complete_workspace(task, agent)
            except Exception as e:
                await _post(
                    f"**Post-completion git error** for task `{task.id}`: {e}"
                )

            if pr_url:
                # PR-based approval workflow
                await self.db.update_task(
                    action.task_id,
                    status=TaskStatus.AWAITING_APPROVAL.value,
                    pr_url=pr_url,
                )
                await self.db.log_event("pr_created",
                                        project_id=action.project_id,
                                        task_id=action.task_id,
                                        agent_id=action.agent_id,
                                        payload=pr_url)
                await _post(format_pr_created(task, pr_url))
                brief = f"🔍 PR created for review: {task.title} (`{task.id}`)\n{pr_url}"
                if thread_send:
                    await _notify_brief(brief)
                await self._control_channel_post(brief)
            elif task.requires_approval and not pr_url:
                # Approval required but no PR (e.g. LINK repo) — wait for manual approval
                await self.db.update_task(
                    action.task_id,
                    status=TaskStatus.AWAITING_APPROVAL.value,
                )
                brief = f"🔍 Awaiting manual approval: {task.title} (`{task.id}`)"
                await _notify_brief(brief)
                await self._control_channel_post(brief)
            else:
                # No approval needed — mark completed
                await self.db.update_task(action.task_id,
                                          status=TaskStatus.COMPLETED.value)
                await self.db.log_event("task_completed",
                                        project_id=action.project_id,
                                        task_id=action.task_id,
                                        agent_id=action.agent_id)
                # Full summary → last message in the task thread.
                if thread_send:
                    summary_lines = [
                        f"**Task Completed:** `{task.id}` — {task.title}",
                        f"Agent: {agent.name} | Tokens: {output.tokens_used:,}",
                    ]
                    if output.summary:
                        summary_lines.append(f"\n**Summary:**\n{output.summary}")
                    if output.files_changed:
                        summary_lines.append(
                            f"\n**Files changed:** {', '.join(output.files_changed)}"
                        )
                    await thread_send("\n".join(summary_lines))
                else:
                    await self._notify_channel(format_task_completed(task, agent, output))
                brief = f"✅ Task completed: {task.title} (`{task.id}`)"
                if thread_send:
                    await _notify_brief(brief)
                await self._control_channel_post(brief)

        elif output.result == AgentResult.FAILED:
            new_retry = task.retry_count + 1
            if new_retry >= task.max_retries:
                await self.db.update_task(action.task_id,
                                          status=TaskStatus.BLOCKED.value,
                                          retry_count=new_retry)
                brief = (
                    f"🚫 Task blocked: {task.title} (`{task.id}`) — "
                    f"max retries ({task.max_retries}) exhausted"
                )
            else:
                await self.db.update_task(action.task_id,
                                          status=TaskStatus.READY.value,
                                          retry_count=new_retry,
                                          assigned_agent_id=None)
                brief = (
                    f"⚠️ Task failed: {task.title} (`{task.id}`) — "
                    f"retry {new_retry}/{task.max_retries}"
                )
            # Full failure summary → thread; fallback to notifications if no thread
            if thread_send:
                from src.discord.notifications import classify_error
                error_type, suggestion = classify_error(output.error_message)
                label = "Blocked" if new_retry >= task.max_retries else "Failed"
                fail_lines = [
                    f"**Task {label}:** `{task.id}` — {task.title}",
                    f"Agent: {agent.name} | Retry: {new_retry}/{task.max_retries}",
                    f"Error type: **{error_type}**",
                ]
                if output.error_message:
                    snippet = output.error_message[:400]
                    if len(output.error_message) > 400:
                        snippet += "…"
                    fail_lines.append(f"```\n{snippet}\n```")
                fail_lines.append(f"💡 {suggestion}")
                fail_lines.append(f"_Use `/agent-error {task.id}` for full details._")
                if output.summary:
                    fail_lines.append(f"\n**Summary:**\n{output.summary}")
                await thread_send("\n".join(fail_lines))
            else:
                if new_retry >= task.max_retries:
                    await self._notify_channel(
                        format_task_blocked(task, last_error=output.error_message)
                    )
                else:
                    await self._notify_channel(format_task_failed(task, agent, output))
            # Brief notification → main channel (reply to thread) + control channel
            if thread_send:
                await _notify_brief(brief)
            await self._control_channel_post(brief)

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
            await _post(
                f"**Task Paused:** `{task.id}` — {task.title}\n"
                f"Reason: {reason}. Will retry in {retry_secs}s."
            )

        # Free agent
        await self.db.update_agent(action.agent_id,
                                   state=AgentState.IDLE,
                                   current_task_id=None)
