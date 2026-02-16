from __future__ import annotations

import time

from src.config import AppConfig
from src.database import Database
from src.event_bus import EventBus
from src.models import (
    AgentResult, AgentState, Task, TaskStatus, TaskContext,
)
from src.scheduler import AssignAction, Scheduler, SchedulerState
from src.tokens.budget import BudgetManager


class Orchestrator:
    def __init__(self, config: AppConfig, adapter_factory=None):
        self.config = config
        self.db = Database(config.database_path)
        self.bus = EventBus()
        self.budget = BudgetManager(
            global_budget=config.global_token_budget_daily
        )
        self._adapter_factory = adapter_factory
        self._adapters: dict[str, object] = {}  # agent_id -> adapter

    async def initialize(self) -> None:
        await self.db.initialize()

    async def shutdown(self) -> None:
        await self.db.close()

    async def run_one_cycle(self) -> None:
        """Run one complete scheduling + execution cycle."""
        # 1. Check for PAUSED tasks that should resume
        await self._resume_paused_tasks()

        # 2. Check DEFINED tasks for dependency resolution
        await self._check_defined_tasks()

        # 3. Schedule
        actions = await self._schedule()

        # 4. Execute assigned tasks
        for action in actions:
            await self._execute_task(action)

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

    async def _execute_task(self, action: AssignAction) -> None:
        # Assign
        await self.db.assign_task_to_agent(action.task_id, action.agent_id)

        # Start agent
        await self.db.update_task(action.task_id,
                                  status=TaskStatus.IN_PROGRESS.value)
        await self.db.update_agent(action.agent_id, state=AgentState.BUSY)

        adapter = self._adapter_factory.create("claude")
        self._adapters[action.agent_id] = adapter

        task = await self.db.get_task(action.task_id)
        ctx = TaskContext(description=task.description)
        await adapter.start(ctx)
        output = await adapter.wait()

        # Record tokens
        if output.tokens_used > 0:
            await self.db.record_token_usage(
                action.project_id, action.agent_id,
                action.task_id, output.tokens_used,
            )

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

        elif output.result == AgentResult.FAILED:
            task = await self.db.get_task(action.task_id)
            new_retry = task.retry_count + 1
            if new_retry >= task.max_retries:
                await self.db.update_task(action.task_id,
                                          status=TaskStatus.BLOCKED.value,
                                          retry_count=new_retry)
            else:
                await self.db.update_task(action.task_id,
                                          status=TaskStatus.READY.value,
                                          retry_count=new_retry,
                                          assigned_agent_id=None)

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

        # Free agent
        await self.db.update_agent(action.agent_id,
                                   state=AgentState.IDLE,
                                   current_task_id=None)
