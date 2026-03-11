"""Orchestrator — the central brain of the agent queue system.

Runs a ~5-second loop that drives the entire task lifecycle: promoting
DEFINED tasks whose dependencies are met, scheduling READY tasks onto idle
agents, launching agent execution as background asyncio tasks, managing git
workspaces (clone/link/init), parsing plan files into chained subtasks,
handling PR/approval workflows, and monitoring for stuck tasks.

Design principle: **zero LLM calls for orchestration**.  All scheduling and
state-machine logic is purely deterministic.  Every token budget goes to
actual agent work, not coordination overhead.

Heavy operations (agent execution, git clones) run as background asyncio
tasks so the main loop stays responsive and can continue checking heartbeats,
promoting tasks, and handling approvals while agents work.

See ``specs/orchestrator.md`` for the full behavioral specification.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Callable, Awaitable

from src.adapters.base import MessageCallback
from src.logging_config import CorrelationContext
from src.config import AppConfig
from src.llm_logger import LLMLogger
from src.database import Database
from src.discord.notifications import (
    format_task_started, format_task_completed, format_task_failed,
    format_task_blocked, format_pr_created, format_agent_question,
    format_chain_stuck, format_stuck_defined_task,
    format_budget_warning,
    format_task_started_embed, format_task_completed_embed,
    format_task_failed_embed, format_task_blocked_embed,
    format_pr_created_embed, format_agent_question_embed,
    format_chain_stuck_embed, format_stuck_defined_task_embed,
    format_budget_warning_embed,
    TaskStartedView, TaskFailedView, TaskApprovalView, TaskBlockedView, AgentQuestionView,
)
from src.event_bus import EventBus
from src.git.manager import GitManager
from src.models import (
    AgentOutput, AgentProfile, AgentResult, AgentState,
    RepoSourceType, Task, TaskStatus, TaskContext, Workspace,
)
from src.hooks import HookEngine
from src.plan_parser import (
    find_plan_file, read_plan_file, parse_plan, build_task_description,
)
from src.plan_parser_llm import parse_plan_with_llm
from src.scheduler import AssignAction, Scheduler, SchedulerState
from src.task_names import generate_task_id
from src.tokens.budget import BudgetManager

logger = logging.getLogger(__name__)

# Sends a formatted message to a Discord channel.  The optional project_id
# lets the callback route to per-project channels instead of the global one.
# When an ``embed`` kwarg is provided, the callback should prefer it over
# the plain-text message for Discord display.
NotifyCallback = Callable[..., Awaitable[None]]

# Sends a single message into an already-created Discord thread.
ThreadSendCallback = Callable[[str], Awaitable[None]]

# Creates a Discord thread for streaming agent output and returns two
# send functions: one for posting into the thread itself, and one for
# posting a brief summary/reply to the parent notifications channel.
# Args: (thread_name, initial_message, project_id)
# Returns: (send_to_thread, notify_main) or None if thread creation failed.
CreateThreadCallback = Callable[
    [str, str, str | None],
    Awaitable[tuple[ThreadSendCallback, ThreadSendCallback] | None],
]


class Orchestrator:
    """Coordinates the full task lifecycle across multiple projects and agents.

    The orchestrator is deliberately decoupled from Discord: it communicates
    through injected callbacks (``set_notify_callback``,
    ``set_create_thread_callback``) rather than importing Discord directly.
    This makes it testable in isolation and keeps the transport layer
    pluggable.

    Key internal state:

    * ``_running_tasks`` — maps task_id to a background ``asyncio.Task``
      for each agent execution currently in flight.  The main loop checks
      this dict every cycle to clean up finished work and avoid double-
      launching.

    * ``_adapters`` — maps agent_id to the live adapter instance (e.g.
      ``ClaudeAdapter``) so we can stop or cancel a running agent from
      admin commands like ``stop_task``.

    * ``_paused`` — when True, the scheduler is skipped entirely (no new
      work is assigned) but monitoring, approvals, and promotions continue.
    """

    def __init__(self, config: AppConfig, adapter_factory=None):
        """Initialize the orchestrator with its configuration and subsystems.

        The orchestrator owns the following key subsystems:

        * **Database** (``self.db``) — single SQLite connection used by all
          methods.  Must be closed via ``shutdown()``.
        * **EventBus** (``self.bus``) — in-process pub/sub for event-driven
          hooks and cross-component notifications.
        * **BudgetManager** (``self.budget``) — tracks global daily token
          spend to prevent runaway cost.
        * **GitManager** (``self.git``) — stateless helper for git operations
          (clone, branch, merge, PR creation).
        * **HookEngine** (``self.hooks``) — initialized lazily in
          ``initialize()`` only when ``hook_engine.enabled`` is True.
        * **MemoryManager** (``self.memory_manager``) — optional semantic
          memory integration; initialized only when ``memory.enabled`` and
          the memsearch package is installed.

        Internal tracking dicts (all keyed for quick lookup, garbage-
        collected periodically to avoid unbounded growth):

        * ``_running_tasks`` — prevents double-launching the same task.
        * ``_adapters`` — enables ``stop_task`` to reach into a running agent.
        * ``_no_pr_reminded_at`` — rate-limits "no PR" reminders per task.
        * ``_stuck_notified_at`` — rate-limits "stuck DEFINED" alerts per task.
        * ``_budget_warned_at`` — prevents duplicate budget threshold alerts.

        Args:
            config: The validated application configuration.
            adapter_factory: Factory for creating agent adapters (e.g.
                ``AdapterFactory``).  If None, task execution is disabled
                and the orchestrator will log an error when ``_execute_task``
                is called.
        """
        self.config = config
        self.db = Database(config.database_path)
        self.bus = EventBus()
        self.budget = BudgetManager(
            global_budget=config.global_token_budget_daily
        )
        self.git = GitManager()
        self._adapter_factory = adapter_factory

        # --- Live agent tracking ---
        # Maps agent_id → adapter instance so we can send stop signals to
        # running agents from admin commands (``stop_task``).
        self._adapters: dict[str, object] = {}
        # Maps task_id → asyncio.Task for each background agent execution.
        # Checked every cycle to detect finished work and prevent double-launch.
        self._running_tasks: dict[str, asyncio.Task] = {}

        # --- Discord integration (injected post-init via setters) ---
        self._notify: NotifyCallback | None = None
        self._create_thread: CreateThreadCallback | None = None

        # --- Orchestrator state ---
        self._paused: bool = False

        # --- Rate-limiting timestamps for periodic checks ---
        self._last_approval_check: float = 0.0
        self._last_log_cleanup: float = 0.0
        self._last_auto_archive: float = 0.0
        self._last_config_reload: float = 0.0

        # LLM interaction logger — writes JSONL files for each chat provider
        # call and agent session, enabling cost tracking and prompt analytics.
        self.llm_logger = LLMLogger(
            enabled=config.llm_logging.enabled,
            retention_days=config.llm_logging.retention_days,
        )

        # Chat provider for LLM-based plan parsing — lazily created only
        # when auto_task.use_llm_parser is True.  Wrapped with LoggedChatProvider
        # so plan-parsing token usage shows up in LLM logs under the
        # "plan_parser" caller tag.
        self._chat_provider = None
        if config.auto_task.use_llm_parser:
            try:
                from src.chat_providers import create_chat_provider, LoggedChatProvider
                provider = create_chat_provider(config.chat_provider)
                if provider and self.llm_logger._enabled:
                    provider = LoggedChatProvider(
                        provider, self.llm_logger, caller="plan_parser"
                    )
                self._chat_provider = provider
            except Exception:
                pass

        # --- Notification rate-limiting dicts ---
        # Tracks the last time we sent a reminder for an AWAITING_APPROVAL
        # task that has no PR URL (keyed by task_id).  Garbage-collected
        # each cycle in ``_check_awaiting_approval``.
        self._no_pr_reminded_at: dict[str, float] = {}
        # Tracks the last time a "stuck DEFINED" notification was sent for
        # each task (keyed by task_id) to rate-limit alerts.  Garbage-collected
        # each cycle in ``_check_stuck_defined_tasks``.
        self._stuck_notified_at: dict[str, float] = {}
        # Tracks per-project budget warning thresholds already sent so we
        # don't spam the same warning.  Keyed by project_id, value is the
        # highest threshold percentage (e.g. 80, 95) already notified.
        # Cleared when usage drops below the lowest threshold (new window).
        self._budget_warned_at: dict[str, int] = {}

        # --- Subsystems initialized later ---
        self.hooks: HookEngine | None = None
        # Semantic memory manager — optional integration with memsearch.
        # Initialized only when config.memory.enabled is True and the
        # memsearch package is installed.
        self.memory_manager: "MemoryManager | None" = None
        if hasattr(config, "memory") and config.memory.enabled:
            try:
                from src.memory import MemoryManager
                self.memory_manager = MemoryManager(
                    config.memory, storage_root=config.workspace_dir
                )
            except Exception as e:
                logger.warning("Memory manager initialization failed: %s", e)

        # Reference to the command handler, set by the bot after initialization.
        # Used to pass handler references to interactive Discord views (e.g.
        # Retry/Skip buttons on failed task notifications).
        self._command_handler: Any = None

    def set_command_handler(self, handler: Any) -> None:
        """Store a reference to the command handler for interactive views."""
        self._command_handler = handler

    def _get_handler(self) -> Any:
        """Return the command handler or None. Used by interactive views."""
        return self._command_handler

    async def _sync_profiles_from_config(self) -> None:
        """Sync agent profiles from YAML config into the database.

        For each profile in config.agent_profiles, create or update the
        corresponding database row.  This is idempotent and runs at startup.
        """
        for pc in self.config.agent_profiles:
            existing = await self.db.get_profile(pc.id)
            if existing:
                await self.db.update_profile(
                    pc.id,
                    name=pc.name,
                    description=pc.description,
                    model=pc.model,
                    permission_mode=pc.permission_mode,
                    allowed_tools=pc.allowed_tools,
                    mcp_servers=pc.mcp_servers,
                    system_prompt_suffix=pc.system_prompt_suffix,
                    install=pc.install,
                )
            else:
                await self.db.create_profile(AgentProfile(
                    id=pc.id,
                    name=pc.name,
                    description=pc.description,
                    model=pc.model,
                    permission_mode=pc.permission_mode,
                    allowed_tools=pc.allowed_tools,
                    mcp_servers=pc.mcp_servers,
                    system_prompt_suffix=pc.system_prompt_suffix,
                    install=pc.install,
                ))

    async def _resolve_profile(self, task: Task) -> AgentProfile | None:
        """Resolve which profile to use: task → project → None (system default)."""
        if task.profile_id:
            return await self.db.get_profile(task.profile_id)
        project = await self.db.get_project(task.project_id)
        if project and project.default_profile_id:
            return await self.db.get_profile(project.default_profile_id)
        return None

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def set_notify_callback(self, callback: NotifyCallback) -> None:
        """Set a callback for sending notifications (e.g. to Discord)."""
        self._notify = callback

    def set_create_thread_callback(self, callback: CreateThreadCallback) -> None:
        """Set a callback for creating per-task threads."""
        self._create_thread = callback

    async def skip_task(self, task_id: str) -> tuple[str | None, list[Task]]:
        """Skip a BLOCKED or FAILED task to unblock its dependency chain.

        This is an admin escape hatch: it marks the task as COMPLETED (even
        though no work was done) so that downstream dependents whose only
        remaining unmet dependency was this task can proceed.  The method
        performs a forward walk of the dependency graph to report which
        tasks will be unblocked, giving the operator visibility into the
        blast radius before the next cycle promotes them.

        Returns (error_string | None, list_of_tasks_that_will_be_unblocked).
        """
        task = await self.db.get_task(task_id)
        if not task:
            return f"Task '{task_id}' not found", []
        if task.status not in (TaskStatus.BLOCKED, TaskStatus.FAILED):
            return (
                f"Task is not BLOCKED or FAILED (status: {task.status.value}). "
                f"Only blocked/failed tasks can be skipped.",
                [],
            )

        await self.db.transition_task(
            task_id,
            TaskStatus.COMPLETED,
            context="skip_task",
        )
        await self.db.log_event(
            "task_skipped",
            project_id=task.project_id,
            task_id=task.id,
            payload=f"skipped from {task.status.value}",
        )

        # Find which downstream tasks will now become unblocked.
        # After we set this task to COMPLETED, any direct dependents
        # whose other deps are also met will be promoted by the
        # next _check_defined_tasks cycle.
        unblocked: list[Task] = []
        dependents = await self.db.get_dependents(task_id)
        for dep_id in dependents:
            dep_task = await self.db.get_task(dep_id)
            if dep_task and dep_task.status == TaskStatus.DEFINED:
                # Check if all deps (including the now-skipped one) are met
                if await self.db.are_dependencies_met(dep_id):
                    unblocked.append(dep_task)

        await self._notify_channel(
            f"**Task Skipped:** `{task_id}` — {task.title}\n"
            f"Marked as COMPLETED to unblock dependency chain."
            + (f"\n{len(unblocked)} task(s) will be unblocked in the next cycle."
               if unblocked else ""),
            project_id=task.project_id,
        )

        return None, unblocked

    async def stop_task(self, task_id: str) -> str | None:
        """Forcibly stop an in-progress task and release its agent.

        Sends a stop signal to the running adapter, transitions the task to
        BLOCKED, resets the agent to IDLE, and checks whether stopping this
        task orphans any downstream dependency chain (notifying if so).

        Returns None on success, or an error string if the task cannot be stopped.
        """
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
                logger.error("Error stopping adapter for agent %s: %s", agent_id, e)

        # Release workspace lock and reset task/agent state
        await self.db.release_workspaces_for_task(task_id)
        await self.db.transition_task(task_id, TaskStatus.BLOCKED,
                                      context="stop_task",
                                      assigned_agent_id=None)
        if agent_id:
            await self.db.update_agent(agent_id, state=AgentState.IDLE,
                                       current_task_id=None)
            self._adapters.pop(agent_id, None)

        await self._notify_channel(
            f"**Task Stopped:** `{task_id}` — {task.title}",
            project_id=task.project_id,
        )
        # Check if stopping this task blocks a dependency chain
        await self._notify_stuck_chain(task)
        return None

    async def _notify_channel(
        self,
        message: str,
        project_id: str | None = None,
        *,
        embed: Any = None,
        view: Any = None,
    ) -> None:
        """Send a notification if a callback is set.

        When *project_id* is given the callback can route the message to a
        per-project Discord channel (falling back to the global notifications
        channel if the project has none configured).

        When *embed* is provided the callback can use it for rich Discord
        rendering while still keeping *message* for logging/fallback.

        When *view* is provided, interactive buttons are attached to the embed
        message (e.g. Retry/Skip buttons on failed task notifications).
        """
        if self._notify:
            try:
                # Only pass embed/view kwargs when set to maintain backward
                # compatibility with callbacks that don't accept them.
                kwargs: dict[str, Any] = {}
                if embed is not None:
                    kwargs["embed"] = embed
                if view is not None:
                    kwargs["view"] = view
                if kwargs:
                    await self._notify(message, project_id, **kwargs)
                else:
                    await self._notify(message, project_id)
            except Exception as e:
                logger.error("Notification error: %s", e)

    async def _notify_agent_question(
        self,
        task: Task,
        agent: Any,
        question: str,
        project_id: str | None = None,
    ) -> None:
        """Send an agent-question notification with both text and embed.

        Called when the orchestrator detects that an agent is asking the user
        a question (e.g. via the ``AskUserQuestion`` tool).  Sends the
        notification to the project channel using both the plain-text
        formatter (for logging) and the rich embed formatter (for Discord).
        """
        from src.models import Agent
        # Ensure we have proper model objects (may receive raw DB rows)
        if not isinstance(agent, Agent):
            agent = await self.db.get_agent(getattr(agent, "id", agent))
        msg = format_agent_question(task, agent, question)
        embed = format_agent_question_embed(task, agent, question)
        handler_ref = self._get_handler()
        view = AgentQuestionView(task.id, handler=handler_ref)
        await self._notify_channel(
            msg,
            project_id=project_id or task.project_id,
            embed=embed,
            view=view,
        )
        await self.db.log_event(
            "agent_question",
            project_id=task.project_id,
            task_id=task.id,
            agent_id=agent.id,
            payload=question[:500],
        )

    # Budget warning thresholds — notifications are sent when project usage
    # crosses each percentage level (ascending).  Only the highest crossed
    # threshold triggers a notification; lower thresholds that were already
    # notified are not re-sent.
    _BUDGET_WARNING_THRESHOLDS: list[int] = [75, 90, 95]

    async def _check_budget_warning(
        self,
        project_id: str,
        tokens_just_used: int,
    ) -> None:
        """Check whether a project's token usage has crossed a warning threshold.

        Called after recording token usage for a task.  Queries the project's
        ``budget_limit`` and current rolling-window usage, then sends a
        ``format_budget_warning_embed`` notification if the usage percentage
        has crossed one of the defined thresholds since the last notification.

        Rate-limited: each threshold level is notified at most once per
        project until the budget resets (e.g. new rolling window).
        """
        project = await self.db.get_project(project_id)
        if not project or not project.budget_limit:
            return  # No budget configured — nothing to warn about

        limit = project.budget_limit

        # Get current usage in the rolling window
        window_start = time.time() - (
            self.config.scheduling.rolling_window_hours * 3600
        )
        usage = await self.db.get_project_token_usage(
            project_id, since=window_start,
        )

        pct = (usage / limit * 100) if limit > 0 else 0
        if pct < self._BUDGET_WARNING_THRESHOLDS[0]:
            # Usage below the lowest threshold — clear any previous warnings
            # so they can fire again in the next budget window.
            self._budget_warned_at.pop(project_id, None)
            return

        # Find the highest threshold that has been crossed
        crossed = 0
        for threshold in self._BUDGET_WARNING_THRESHOLDS:
            if pct >= threshold:
                crossed = threshold

        if crossed == 0:
            return

        # Only notify if we haven't already notified for this threshold level
        last_warned = self._budget_warned_at.get(project_id, 0)
        if last_warned >= crossed:
            return  # Already warned at this level or higher

        self._budget_warned_at[project_id] = crossed

        project_name = project.name or project_id
        msg = format_budget_warning(project_name, usage, limit)
        embed = format_budget_warning_embed(project_name, usage, limit)
        await self._notify_channel(
            msg,
            project_id=project_id,
            embed=embed,
        )
        await self.db.log_event(
            "budget_warning",
            project_id=project_id,
            payload=f"usage={usage:,}/{limit:,} ({pct:.0f}%), threshold={crossed}%",
        )
        logger.info("Budget warning: project %s at %.0f%% (%s/%s tokens, threshold=%d%%)", project_id, pct, usage, limit, crossed)

    async def initialize(self) -> None:
        """Boot the orchestrator: DB schema, profile sync, crash recovery, hooks.

        Initialization order matters:

        1. **Database** — creates tables/indexes if they don't exist.
        2. **Profile sync** — ensures YAML-defined agent profiles exist in the
           DB (idempotent upsert).
        3. **Stale state recovery** — after a daemon restart, no adapters are
           actually running, so any IN_PROGRESS tasks or BUSY agents from the
           previous run are reset to safe states (READY / IDLE).
        4. **Hook engine** — subscribes to the EventBus and pre-populates
           last-run timestamps from the DB for accurate periodic scheduling.

        This method must be called (and awaited) before ``run_one_cycle()``.
        """
        await self.db.initialize()
        await self._sync_profiles_from_config()
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
            if a.state == AgentState.BUSY:
                logger.info("Recovery: resetting agent '%s' from %s to IDLE", a.name, a.state.value)
                await self.db.update_agent(a.id, state=AgentState.IDLE, current_task_id=None)

        # Release all workspace locks (no agents are running after restart)
        all_workspaces = await self.db.list_workspaces()
        for ws in all_workspaces:
            if ws.locked_by_agent_id:
                logger.info("Recovery: releasing workspace lock '%s' (was locked by %s)", ws.id, ws.locked_by_agent_id)
                await self.db.release_workspace(ws.id)

        # Reset IN_PROGRESS tasks back to READY so they get re-scheduled
        tasks = await self.db.list_tasks(status=TaskStatus.IN_PROGRESS)
        for t in tasks:
            logger.info("Recovery: resetting task '%s' (%s) from IN_PROGRESS to READY", t.id, t.title)
            await self.db.transition_task(t.id, TaskStatus.READY,
                                          context="recovery",
                                          assigned_agent_id=None)

    async def wait_for_running_tasks(self, timeout: float | None = None) -> None:
        """Wait for all background task executions to finish.

        This is primarily useful in tests where ``run_one_cycle`` fires off
        background coroutines and the caller needs to wait for them to
        complete before inspecting results.
        """
        if not self._running_tasks:
            return
        tasks = list(self._running_tasks.values())
        if timeout is not None:
            await asyncio.wait(tasks, timeout=timeout)
        else:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _format_memory_context(self, memories: list[dict]) -> str:
        """Format memsearch results as a readable context block for the agent.

        Each memory entry is rendered with its source, heading, and content
        so the agent can see where the information came from and how relevant
        it is.  The output is a single string suitable for appending to
        ``TaskContext.attached_context``.
        """
        lines = ["## Relevant Context from Project Memory\n"]
        for i, mem in enumerate(memories, 1):
            source = mem.get("source", "unknown")
            heading = mem.get("heading", "")
            content = mem.get("content", "")
            score = mem.get("score", 0)
            lines.append(f"### Memory {i} (relevance: {score:.2f})")
            lines.append(f"*Source: {source}*")
            if heading:
                lines.append(f"*Section: {heading}*\n")
            lines.append(content)
            lines.append("")
        return "\n".join(lines)

    async def shutdown(self) -> None:
        # Wait for any running task executions to finish before closing
        # the database, otherwise they'll hit "Cannot operate on a closed
        # database" errors.
        await self.wait_for_running_tasks(timeout=10)
        if self.hooks:
            await self.hooks.shutdown()
        if self.memory_manager:
            try:
                await self.memory_manager.close()
            except Exception as e:
                logger.warning("Memory manager shutdown error: %s", e)
        await self.db.close()

    async def run_one_cycle(self) -> None:
        """Run one iteration of the orchestrator's main loop.

        The ordering of checks is intentional and matters:

        1. **Approvals first** — complete tasks whose PRs were merged so
           their dependents can be promoted in the same cycle.
        2. **Resume paused** — bring back rate-limited/token-exhausted tasks
           whose backoff timers have expired.
        3. **Promote DEFINED** — check dependency satisfaction and move tasks
           to READY.  This must happen after approvals so freshly-completed
           parent tasks unblock their children immediately.
        4. **Stuck monitoring** — rate-limited alerts for DEFINED tasks that
           have been waiting too long (runs after promotion so we don't
           false-alarm on tasks that just got promoted).
        5. **Schedule** — assign READY tasks to idle agents (skipped when
           the orchestrator is paused).
        6. **Launch** — fire off background asyncio tasks for each new
           assignment.  These run concurrently with future cycles.
        7. **Hook engine tick** — run any registered hooks.
        8. **Config hot-reload** — periodically re-read non-critical settings
           from disk (scheduling, archive, monitoring, etc.) without restart.
        9. **Log cleanup** — prune old LLM interaction logs.
        10. **Auto-archive** — sweep terminal tasks older than the configured
            threshold into the archive so they no longer clutter active views.
        """
        try:
            # 0. Check AWAITING_APPROVAL tasks for PR merge status
            await self._check_awaiting_approval()

            # 1. Check for PAUSED tasks that should resume
            await self._resume_paused_tasks()

            # 2. Check DEFINED tasks for dependency resolution
            await self._check_defined_tasks()

            # 2b. Check for tasks stuck in DEFINED status beyond threshold
            await self._check_stuck_defined_tasks()

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

            # 6. Hot-reload non-critical config settings (~once per 5 minutes)
            now = time.time()
            if now - self._last_config_reload >= 300:
                self._last_config_reload = now
                self._try_reload_config()

            # 7. Periodic log cleanup and analytics flush (~once per hour)
            if now - self._last_log_cleanup >= 3600:
                self._last_log_cleanup = now
                try:
                    removed = self.llm_logger.cleanup_old_logs()
                    if removed:
                        logger.info("LLM log cleanup: removed %d old directory(ies)", removed)
                    # Flush prompt analytics to disk for long-term analysis
                    self.llm_logger.flush_analytics()
                except Exception as e:
                    logger.error("LLM log cleanup error: %s", e)

            # 8. Auto-archive stale terminal tasks (~once per hour)
            await self._auto_archive_tasks()
        except Exception as e:
            logger.error("Scheduler cycle error", exc_info=True)

    async def _execute_task_safe(self, action: AssignAction) -> None:
        """Wrapper around _execute_task that catches exceptions and enforces timeout."""
        with CorrelationContext(
            task_id=action.task_id,
            project_id=action.project_id,
            component="orchestrator",
        ):
            await self._execute_task_safe_inner(action)

    async def _execute_task_safe_inner(self, action: AssignAction) -> None:
        """Inner implementation with correlation context already set.

        Wraps ``_execute_task`` with two safety nets:

        **Timeout guard** (``asyncio.TimeoutError``):
            If the agent exceeds ``stuck_timeout_seconds``, the adapter is
            force-stopped and the task transitions to BLOCKED.  BLOCKED (not
            READY) is chosen deliberately: a timed-out task likely has an
            underlying issue (infinite loop, unresponsive API) and should
            not be blindly rescheduled.  An operator must investigate and
            manually retry or skip.  Downstream dependents are checked for
            orphan chains.

        **Unexpected error** (``Exception``):
            Infrastructure errors (e.g. DB unavailable, adapter crash) are
            caught here.  The task transitions back to READY (not BLOCKED)
            because the failure is likely transient — the next scheduling
            cycle can retry it on a different agent.  The cleanup is wrapped
            in its own try/except so a secondary DB failure doesn't mask the
            original error.

        In both cases, the ``finally`` block removes the task from
        ``_running_tasks`` so the main loop knows this slot is free.
        """
        timeout = self.config.agents_config.stuck_timeout_seconds
        try:
            if timeout > 0:
                await asyncio.wait_for(self._execute_task(action), timeout=timeout)
            else:
                await self._execute_task(action)
        except asyncio.TimeoutError:
            logger.warning("Task %s timed out after %ds", action.task_id, timeout)
            # Stop the adapter if it's still running — best-effort, the
            # subprocess may already be dead.
            if action.agent_id in self._adapters:
                try:
                    await self._adapters[action.agent_id].stop()
                except Exception:
                    pass
            # Release resources: workspace lock, task assignment, agent state.
            # Order matters: release workspace first so it's available for
            # other tasks even if subsequent DB calls fail.
            await self.db.release_workspaces_for_task(action.task_id)
            await self.db.transition_task(
                action.task_id, TaskStatus.BLOCKED,
                context="timeout",
                assigned_agent_id=None)
            await self.db.update_agent(
                action.agent_id, state=AgentState.IDLE,
                current_task_id=None)
            self._adapters.pop(action.agent_id, None)
            await self._notify_channel(
                f"**Task Timed Out:** `{action.task_id}` — exceeded {timeout}s. Marked as BLOCKED.",
                project_id=action.project_id,
            )
            # Check if this blocked task breaks a dependency chain
            task = await self.db.get_task(action.task_id)
            if task:
                await self._notify_stuck_chain(task)
            return
        except Exception as e:
            logger.error("Error executing task %s", action.task_id, exc_info=True)
            # Best-effort cleanup — wrapped in try/except so a secondary
            # failure doesn't mask the original error in the log.
            try:
                await self.db.release_workspaces_for_task(action.task_id)
                # Return to READY (not BLOCKED) — transient infra errors
                # should be retried automatically.
                await self.db.transition_task(
                    action.task_id, TaskStatus.READY,
                    context="execution_error",
                    assigned_agent_id=None)
                await self.db.update_agent(
                    action.agent_id, state=AgentState.IDLE,
                    current_task_id=None)
            except Exception:
                pass
            await self._notify_channel(
                f"**Error executing task** `{action.task_id}`: {e}",
                project_id=action.project_id,
            )
        finally:
            # Always remove from _running_tasks so the main loop knows
            # this execution slot is free for new assignments.
            self._running_tasks.pop(action.task_id, None)

    def _try_reload_config(self) -> None:
        """Attempt to hot-reload non-critical configuration settings from disk.

        Called periodically (~every 5 minutes) from the main loop. Only
        reloads settings that are safe to change at runtime without restart:
        scheduling, pause_retry, auto_task, archive, monitoring, hook_engine,
        and llm_logging.  Critical settings (discord, database_path,
        workspace_dir, chat_provider, memory) are NOT reloaded.

        If the reload fails (e.g., YAML parse error), the current config is
        kept unchanged and the error is logged.
        """
        try:
            updated = self.config.reload_non_critical()
            if updated is not self.config:
                self.config = updated
                # Update hook engine config reference if it exists
                if self.hooks:
                    self.hooks.config = updated
                logger.info("Config hot-reload: non-critical settings refreshed from disk")
        except Exception as e:
            logger.warning("Config hot-reload error: %s", e)

    async def _resume_paused_tasks(self) -> None:
        """Check PAUSED tasks whose ``resume_after`` has elapsed and promote to READY.

        Tasks enter PAUSED when the agent hits a rate limit or token
        exhaustion, with ``resume_after`` set to a future timestamp.
        This method scans all PAUSED tasks and transitions any whose
        backoff timer has expired back to READY for re-scheduling.
        """
        paused = await self.db.list_tasks(status=TaskStatus.PAUSED)
        now = time.time()
        for task in paused:
            if task.resume_after and task.resume_after <= now:
                await self.db.transition_task(task.id, TaskStatus.READY,
                                              context="resume_paused",
                                              assigned_agent_id=None,
                                              resume_after=None)

    async def _check_defined_tasks(self) -> None:
        """Promote DEFINED tasks to READY when all dependencies are satisfied.

        Scans all DEFINED tasks and checks their dependency list:
        - Tasks with no dependencies are immediately promoted to READY.
        - Tasks with dependencies are promoted only when every upstream
          dependency has reached COMPLETED status.

        This runs after ``_check_awaiting_approval`` so that freshly-merged
        PRs can unblock their dependents in the same cycle.
        """
        defined = await self.db.list_tasks(status=TaskStatus.DEFINED)
        for task in defined:
            deps = await self.db.get_dependencies(task.id)
            if not deps:
                # No dependencies — promote to READY
                await self.db.transition_task(task.id, TaskStatus.READY,
                                              context="deps_met_no_deps")
            else:
                deps_met = await self.db.are_dependencies_met(task.id)
                if deps_met:
                    await self.db.transition_task(task.id, TaskStatus.READY,
                                                  context="deps_met")

    async def _check_stuck_defined_tasks(self) -> None:
        """Monitoring: detect DEFINED tasks stuck waiting for dependencies.

        Queries for tasks that have been in DEFINED status longer than
        ``monitoring.stuck_task_threshold_seconds`` and sends a notification
        with details about which upstream dependencies are blocking them.

        Notifications are rate-limited to one per threshold period per task
        (tracked in ``_stuck_notified_at``) to avoid flooding Discord.
        The tracker is garbage-collected each cycle to remove entries for
        tasks that are no longer stuck.
        """
        threshold = self.config.monitoring.stuck_task_threshold_seconds
        if threshold <= 0:
            return  # Disabled

        stuck_tasks = await self.db.get_stuck_defined_tasks(threshold)
        if not stuck_tasks:
            return

        now = time.time()

        # Clean up notification tracker for tasks no longer DEFINED
        stuck_ids = {t.id for t in stuck_tasks}
        for tid in list(self._stuck_notified_at):
            if tid not in stuck_ids:
                del self._stuck_notified_at[tid]

        for task in stuck_tasks:
            # Rate-limit: only notify once per threshold period per task
            last_notified = self._stuck_notified_at.get(task.id, 0)
            if now - last_notified < threshold:
                continue

            # Find which dependencies are blocking this task
            blocking = await self.db.get_blocking_dependencies(task.id)

            # Calculate how long the task has been stuck
            task_created_at = await self.db.get_task_created_at(task.id)
            if not task_created_at:
                task_created_at = now  # fallback (should not happen)
            stuck_hours = (now - task_created_at) / 3600

            msg = format_stuck_defined_task(task, blocking, stuck_hours)
            await self._notify_channel(
                msg,
                project_id=task.project_id,
                embed=format_stuck_defined_task_embed(task, blocking, stuck_hours),
            )

            # Log the event
            blocking_info = ", ".join(
                f"{dep_id}({dep_status})" for dep_id, _, dep_status in blocking[:10]
            )
            await self.db.log_event(
                "stuck_defined_task",
                project_id=task.project_id,
                task_id=task.id,
                payload=f"stuck_hours={stuck_hours:.1f}, "
                        f"blocking=[{blocking_info}]",
            )
            logger.info("Stuck task detected: %s — %s (DEFINED for %.1fh, blocked by %d deps)", task.id, task.title, stuck_hours, len(blocking))

            self._stuck_notified_at[task.id] = now

    async def _auto_archive_tasks(self) -> None:
        """Automatically archive terminal tasks older than the configured threshold.

        Runs at most once per hour (rate-limited via ``_last_auto_archive``)
        and only when ``config.archive.enabled`` is True.  Tasks matching the
        configured terminal statuses whose ``updated_at`` is older than
        ``archive.after_hours`` are silently moved to the ``archived_tasks``
        table so they no longer appear in active views.

        This eliminates the need for agents or operators to manually run
        ``/archive-tasks``; the orchestrator handles it automatically.
        """
        archive_cfg = self.config.archive
        if not archive_cfg.enabled:
            return

        now = time.time()
        # Rate-limit to once per hour
        if now - self._last_auto_archive < 3600:
            return
        self._last_auto_archive = now

        older_than_seconds = archive_cfg.after_hours * 3600
        try:
            archived_ids = await self.db.archive_old_terminal_tasks(
                statuses=archive_cfg.statuses,
                older_than_seconds=older_than_seconds,
            )
        except Exception as e:
            logger.error("Auto-archive error: %s", e)
            return

        if archived_ids:
            logger.info("Auto-archived %d terminal task(s) older than %.1fh: %s%s", len(archived_ids), archive_cfg.after_hours, ', '.join(archived_ids[:10]), '...' if len(archived_ids) > 10 else '')
            for tid in archived_ids:
                try:
                    await self.db.log_event(
                        "task_auto_archived", task_id=tid,
                    )
                except Exception:
                    pass

    async def _find_stuck_downstream(self, blocked_task_id: str) -> list[Task]:
        """BFS walk of the dependency graph to find orphaned DEFINED tasks.

        Starting from a BLOCKED task, walks forward through ``get_dependents``
        and collects every downstream task still in DEFINED status.  These
        tasks can never proceed because their dependency chain is broken.

        Only DEFINED tasks are collected — tasks that have already been
        promoted past the dependency gate (READY, IN_PROGRESS, etc.) are
        not affected by the upstream blockage.

        Used by ``_notify_stuck_chain`` to give operators visibility into
        the full blast radius when a task fails or is stopped.
        """
        stuck: list[Task] = []
        visited: set[str] = set()
        queue: list[str] = [blocked_task_id]

        while queue:
            current_id = queue.pop(0)
            if current_id in visited:
                continue
            visited.add(current_id)

            dependents = await self.db.get_dependents(current_id)
            for dep_id in dependents:
                if dep_id in visited:
                    continue
                task = await self.db.get_task(dep_id)
                if not task:
                    continue
                # Only DEFINED tasks are "stuck" — tasks in other states
                # (READY, IN_PROGRESS, etc.) have already moved past the
                # dependency gate.
                if task.status == TaskStatus.DEFINED:
                    stuck.append(task)
                    # Continue walking: this stuck task may itself have
                    # downstream dependents.
                    queue.append(dep_id)

        return stuck

    async def _notify_stuck_chain(self, blocked_task: Task) -> None:
        """Check for downstream stuck tasks and send a notification."""
        stuck = await self._find_stuck_downstream(blocked_task.id)
        if not stuck:
            return

        msg = format_chain_stuck(blocked_task, stuck)
        await self._notify_channel(
            msg,
            project_id=blocked_task.project_id,
            embed=format_chain_stuck_embed(blocked_task, stuck),
        )
        await self.db.log_event(
            "chain_stuck",
            project_id=blocked_task.project_id,
            task_id=blocked_task.id,
            payload=f"stuck_count={len(stuck)}, "
                    f"stuck_ids={[t.id for t in stuck[:20]]}",
        )

    # Budget warning thresholds — notify once per threshold crossing.
    _BUDGET_THRESHOLDS: list[int] = [80, 95]

    async def _check_budget_warning(
        self, project_id: str, tokens_added: int,
    ) -> None:
        """Send a budget warning if a project crosses a spending threshold.

        Called after recording token usage.  Each threshold (80%, 95%) fires
        at most once per project; the ``_budget_warned_at`` dict tracks the
        highest threshold already notified to avoid duplicate alerts.
        """
        project = await self.db.get_project(project_id)
        if not project or project.budget_limit is None or project.budget_limit <= 0:
            return

        usage = await self.db.get_project_token_usage(project_id)
        pct = usage / project.budget_limit * 100

        prev_threshold = self._budget_warned_at.get(project_id, 0)

        for threshold in self._BUDGET_THRESHOLDS:
            if pct >= threshold > prev_threshold:
                msg = format_budget_warning(
                    project.name, usage, project.budget_limit,
                )
                embed = format_budget_warning_embed(
                    project.name, usage, project.budget_limit,
                )
                await self._notify_channel(
                    msg,
                    project_id=project_id,
                    embed=embed,
                )
                await self.db.log_event(
                    "budget_warning",
                    project_id=project_id,
                    payload=f"threshold={threshold}%, usage={usage:,}/{project.budget_limit:,}",
                )
                self._budget_warned_at[project_id] = threshold

    async def _schedule(self) -> list[AssignAction]:
        """Build scheduler state and compute task-to-agent assignments.

        Gathers the current state of all projects, tasks, agents, token
        usage within the rolling window, and per-project workspace
        availability.  Passes this snapshot to the proportional fair-share
        scheduler (``Scheduler.schedule()``) which returns a list of
        ``AssignAction`` objects mapping tasks to agents.

        The scheduler runs deterministically with no LLM calls — it uses
        credit weights, deficit accounting, and workspace availability to
        decide which project's READY tasks should be assigned next.
        """
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
            if a.state == AgentState.BUSY and a.current_task_id:
                task = await self.db.get_task(a.current_task_id)
                if task:
                    active_counts[task.project_id] = (
                        active_counts.get(task.project_id, 0) + 1
                    )

        total_used = sum(project_usage.values())

        # Count available (unlocked) workspaces per project
        workspace_counts: dict[str, int] = {}
        for p in projects:
            workspace_counts[p.id] = await self.db.count_available_workspaces(p.id)

        state = SchedulerState(
            projects=projects,
            tasks=tasks,
            agents=agents,
            project_token_usage=project_usage,
            project_active_agent_counts=active_counts,
            tasks_completed_in_window={},
            project_available_workspaces=workspace_counts,
            global_budget=self.config.global_token_budget_daily,
            global_tokens_used=total_used,
        )

        return Scheduler.schedule(state)

    async def _prepare_workspace(self, task: Task, agent) -> str | None:
        """Acquire a workspace lock and prepare it for the task.

        1. Acquire an unlocked workspace for the project via
           ``db.acquire_workspace()``.
        2. If no workspace is available, return ``None`` (caller must handle).
        3. Perform git operations based on ``workspace.source_type``
           (clone/link) using ``project.repo_url`` / ``project.repo_default_branch``.
        4. Return the workspace path.

        For plan subtasks, reuses the parent task's branch name so all steps
        accumulate commits on a single branch.
        """
        project = await self.db.get_project(task.project_id)
        ws = await self.db.acquire_workspace(task.project_id, agent.id, task.id)

        if not ws:
            return None

        workspace = ws.workspace_path
        repo_url = project.repo_url if project else ""
        default_branch = project.repo_default_branch if project else "main"

        # Subtasks reuse the parent's branch to accumulate commits
        if task.is_plan_subtask and task.parent_task_id:
            parent = await self.db.get_task(task.parent_task_id)
            branch_name = (parent.branch_name if parent and parent.branch_name
                           else GitManager.make_branch_name(task.id, task.title))
        else:
            branch_name = GitManager.make_branch_name(task.id, task.title)

        reuse_branch = task.is_plan_subtask and task.parent_task_id
        rebase_on_switch = self.config.auto_task.rebase_between_subtasks

        # Git operations may fail but should never prevent returning the workspace path.
        try:
            if ws.source_type == RepoSourceType.CLONE:
                if not self.git.validate_checkout(workspace):
                    os.makedirs(os.path.dirname(workspace), exist_ok=True)
                    if repo_url:
                        self.git.create_checkout(repo_url, workspace)
                if reuse_branch:
                    self.git.switch_to_branch(
                        workspace, branch_name,
                        default_branch=default_branch,
                        rebase=rebase_on_switch,
                    )
                else:
                    self.git.prepare_for_task(workspace, branch_name, default_branch)

            elif ws.source_type == RepoSourceType.LINK:
                if not os.path.isdir(workspace):
                    await self._notify_channel(
                        f"**Warning:** Linked workspace path `{workspace}` does not exist.",
                        project_id=task.project_id,
                    )
                elif self.git.validate_checkout(workspace):
                    if reuse_branch:
                        self.git.switch_to_branch(
                            workspace, branch_name,
                            default_branch=default_branch,
                            rebase=rebase_on_switch,
                        )
                    else:
                        self.git.prepare_for_task(workspace, branch_name, default_branch)

            # Update task branch in DB
            await self.db.update_task(task.id, branch_name=branch_name)
        except Exception as e:
            await self._notify_channel(
                f"**Git Warning:** Task `{task.id}` — branch setup failed: {e}\n"
                f"Agent will work in `{workspace}` without branch management.",
                project_id=task.project_id,
            )

        return workspace

    async def _complete_workspace(self, task: Task, agent) -> str | None:
        """Post-completion git workflow: commit changes, then merge or open a PR.

        Finds the workspace locked by this task, performs git operations using
        the project's repo config, and releases the workspace lock.

        Returns a PR URL if one was created, otherwise None.
        """
        # Find workspace locked by this task
        ws = await self.db.get_workspace_for_task(task.id)
        workspace = ws.workspace_path if ws else None
        if not workspace or not self.git.validate_checkout(workspace):
            return None

        if not task.branch_name:
            return None

        project = await self.db.get_project(task.project_id)
        default_branch = project.repo_default_branch if project else "main"
        has_repo = bool(project and project.repo_url)

        # Commit any uncommitted work on the task branch
        committed = self.git.commit_all(
            workspace, f"agent: {task.title}\n\nTask-Id: {task.id}"
        )
        if not committed:
            logger.info("Task %s: no changes to commit on branch %s", task.id, task.branch_name)

        # Build a lightweight repo-like object for _merge_and_push / _create_pr_for_task
        # that still expects RepoConfig. Use a minimal compat wrapper.
        from src.models import RepoConfig
        repo = RepoConfig(
            id=f"project-{task.project_id}",
            project_id=task.project_id,
            source_type=ws.source_type,
            url=project.repo_url if project else "",
            default_branch=default_branch,
        ) if has_repo or ws else None

        # --- Plan subtask branching logic ---
        # Subtasks in a chained plan all share the same branch.  Intermediate
        # subtasks only commit (no merge/push) so the next subtask picks up
        # where the previous one left off.  Only the *final* subtask in the
        # chain triggers the merge-or-PR workflow:
        #   - If the parent task requires approval → create a PR
        #   - If not → merge directly into the default branch
        # Between subtasks, an optional mid-chain rebase keeps the branch
        # up-to-date with the default branch (controlled by
        # ``auto_task.rebase_between_subtasks``).
        if task.is_plan_subtask:
            is_last = await self._is_last_subtask(task)
            if is_last and repo:
                parent = await self.db.get_task(task.parent_task_id)
                if parent and parent.requires_approval:
                    return await self._create_pr_for_task(task, repo, workspace)
                else:
                    await self._merge_and_push(task, repo, workspace)
            elif not is_last and repo and self.config.auto_task.rebase_between_subtasks:
                try:
                    synced = self.git.mid_chain_sync(
                        workspace, task.branch_name, default_branch,
                    )
                    if synced:
                        logger.info("Task %s: mid-chain sync OK — branch %s rebased onto origin/%s", task.id, task.branch_name, default_branch)
                    else:
                        logger.info("Task %s: mid-chain rebase skipped (conflict) — branch left as-is", task.id)
                except Exception as e:
                    logger.warning("Task %s: mid-chain sync failed (non-fatal): %s", task.id, e)
            return None

        if repo and task.requires_approval:
            return await self._create_pr_for_task(task, repo, workspace)
        elif repo:
            await self._merge_and_push(task, repo, workspace)
            return None

        return None

    async def _is_last_subtask(self, task: Task) -> bool:
        """Check if all sibling subtasks (same parent) are COMPLETED except this one."""
        if not task.parent_task_id:
            return True
        siblings = await self.db.get_subtasks(task.parent_task_id)
        for sibling in siblings:
            if sibling.id == task.id:
                continue
            if sibling.status != TaskStatus.COMPLETED:
                return False
        return True

    async def _merge_and_push(
        self, task: Task, repo: RepoConfig, workspace: str,
        *, _max_retries: int = 3,
    ) -> None:
        """Merge the task branch into default and push.

        For repos with a remote origin (CLONE or LINK workspaces backed by a
        remote), delegates to :meth:`GitManager.sync_and_merge` which handles
        the full fetch → hard-reset → merge → push-with-retry cycle.
        The *_max_retries* parameter controls total push attempts (including
        the initial one); internally this maps to
        ``max_retries = _max_retries - 1``.

        For repos without a remote (INIT or truly local repos), falls back to
        a simple local merge via :meth:`GitManager.merge_branch` — no push or
        retry is needed.

        **Recovery on failure:** If the merge or push fails, the workspace is
        reset to a clean state so it's ready for the next task.  For repos
        with a remote this means hard-resetting the default branch to
        ``origin/<default_branch>`` (discarding any un-pushed merge commits).
        For local-only repos this means checking out the default branch.
        Recovery is best-effort — failures are silently ignored.
        """
        has_remote = self.git.has_remote(workspace)

        if has_remote:
            # sync_and_merge handles fetch, hard-reset, merge, and push
            # with retry.  max_retries counts *retries* after the first
            # attempt, so subtract 1 from _max_retries (total attempts).
            success, error = self.git.sync_and_merge(
                workspace,
                task.branch_name,
                repo.default_branch,
                max_retries=max(_max_retries - 1, 0),
            )
            if not success:
                if error == "merge_conflict":
                    await self._notify_channel(
                        f"**Merge Conflict:** Task `{task.id}` branch "
                        f"`{task.branch_name}` has conflicts with "
                        f"`{repo.default_branch}`. Manual resolution needed.",
                        project_id=task.project_id,
                    )
                else:
                    # error starts with "push_failed: …"
                    await self._notify_channel(
                        f"**Push Failed:** Could not push `{repo.default_branch}` "
                        f"for task `{task.id}` after {_max_retries} attempts. "
                        f"Workspace may be diverged. Details: {error}",
                        project_id=task.project_id,
                    )
                # Recovery: reset workspace to origin state so it's clean
                # for the next task.  After a failed push the local default
                # branch may contain un-pushed merge commits; hard-resetting
                # to origin discards them.
                try:
                    self.git.recover_workspace(workspace, repo.default_branch)
                except Exception:
                    pass  # best-effort recovery
                return
        else:
            # LINK / INIT repos have no remote — just merge locally.
            merged = self.git.merge_branch(
                workspace, task.branch_name, repo.default_branch,
            )
            if not merged:
                # Rebase fallback: rebase the task branch onto the default
                # branch and retry the merge.  This resolves conflicts caused
                # by the task branch being based on a stale snapshot.
                rebased = self.git.rebase_onto(
                    workspace, task.branch_name, repo.default_branch,
                )
                if rebased:
                    merged = self.git.merge_branch(
                        workspace, task.branch_name, repo.default_branch,
                    )
            if not merged:
                await self._notify_channel(
                    f"**Merge Conflict:** Task `{task.id}` branch "
                    f"`{task.branch_name}` has conflicts with "
                    f"`{repo.default_branch}`. Manual resolution needed.",
                    project_id=task.project_id,
                )
                # Recovery: ensure we're on the default branch so the
                # workspace is clean for the next task.  merge_branch()
                # already aborts the merge, but we make sure we're on the
                # right branch as a safety net.
                try:
                    self.git._run(
                        ["checkout", repo.default_branch], cwd=workspace,
                    )
                except Exception:
                    pass  # best-effort recovery
                return

            # Clean up the task branch after successful local merge
            try:
                self.git.delete_branch(
                    workspace, task.branch_name,
                    delete_remote=False,
                )
            except Exception:
                pass  # branch cleanup is best-effort
            return

        # Clean up the task branch after successful merge + push
        try:
            self.git.delete_branch(
                workspace, task.branch_name,
                delete_remote=has_remote,
            )
        except Exception:
            pass  # branch cleanup is best-effort

    async def _create_pr_for_task(
        self, task: Task, repo: RepoConfig, workspace: str,
    ) -> str | None:
        """Push the task branch and create a PR. Returns the PR URL or None.

        Uses ``force_with_lease=True`` when pushing the task branch so that
        retries (e.g. after a failed PR creation where the push succeeded)
        don't fail with a non-fast-forward error.  ``--force-with-lease`` is
        safe here because the task branch is owned exclusively by this agent —
        no other user is expected to push to it (resolves **G5**).
        """
        if not self.git.has_remote(workspace):
            # No remote — notify user to review the branch locally
            await self._notify_channel(
                f"**Approval Required:** Task `{task.id}` — {task.title}\n"
                f"Branch `{task.branch_name}` is ready for review in `{workspace}`.\n"
                f"Use the `approve_task` command to complete it.",
                project_id=task.project_id,
            )
            return None

        try:
            # Use --force-with-lease so the push succeeds even when the
            # branch was previously pushed (e.g. task retries or subtask
            # chains that push intermediate results).  Task branches are
            # owned by a single agent, so force-pushing is safe.
            self.git.push_branch(
                workspace, task.branch_name, force_with_lease=True,
            )
        except Exception as e:
            await self._notify_channel(
                f"**Push Failed:** Could not push branch `{task.branch_name}` "
                f"for task `{task.id}`: {e}",
                project_id=task.project_id,
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
                f"Branch `{task.branch_name}` has been pushed. Create a PR manually.",
                project_id=task.project_id,
            )
            return None

    async def _generate_tasks_from_plan(
        self, task: Task, workspace: str
    ) -> list[Task]:
        """The auto-task pipeline: discover a plan file, parse it, and create subtasks.

        Called after a task completes successfully.  Searches the workspace
        for a plan file (e.g. ``.claude/plan.md``) using configurable glob
        patterns, parses it with either a regex parser or an LLM-based
        parser, and creates one new task per plan step.

        When ``chain_dependencies`` is enabled (the default), each subtask
        depends on the previous one, forming a serial execution chain on a
        shared git branch.  Only the final subtask in the chain inherits
        the parent's ``requires_approval`` flag, so intermediate steps
        don't block the chain waiting for human review.

        The plan file is archived to ``.claude/plans/<task_id>-plan.md``
        after processing to prevent re-processing if the workspace is
        reused.

        Subtasks cannot themselves generate further sub-plans (guarded by
        ``is_plan_subtask``) to prevent recursive plan explosion.

        Returns the list of created tasks (empty if no plan was found or
        auto-task generation is disabled).
        """
        config = self.config.auto_task
        if not config.enabled:
            return []

        # Prevent recursive plan explosion: subtasks must not generate
        # further sub-plans.
        if task.is_plan_subtask:
            return []

        plan_path = find_plan_file(workspace, config.plan_file_patterns)
        if not plan_path:
            logger.debug("Auto-task: no plan file found for task %s in workspace %s (searched patterns: %s)", task.id, workspace, config.plan_file_patterns)
            return []

        try:
            raw = read_plan_file(plan_path)
        except Exception as e:
            logger.warning("Auto-task: failed to read plan file %s: %s", plan_path, e)
            return []

        if config.use_llm_parser and self._chat_provider:
            try:
                plan = await parse_plan_with_llm(
                    raw, self._chat_provider,
                    source_file=plan_path,
                    max_steps=config.max_steps_per_plan,
                )
            except Exception as e:
                logger.warning("LLM plan parser failed, falling back to regex: %s", e)
                plan = parse_plan(
                    raw, source_file=plan_path,
                    max_steps=config.max_steps_per_plan,
                )
        else:
            plan = parse_plan(
                raw, source_file=plan_path,
                max_steps=config.max_steps_per_plan,
            )

        # Smart LLM fallback: if the regex parser produced steps but they
        # look low-quality (many informational headings, few actionable),
        # automatically retry with the LLM parser for better results.
        if (
            plan.steps
            and not config.use_llm_parser
            and self._chat_provider
        ):
            from src.plan_parser import _score_parse_quality
            quality = _score_parse_quality(plan.steps)
            if quality < 0.4 and len(plan.steps) > 5:
                logger.info("Auto-task: regex parse quality low (%.2f) for %s, retrying with LLM parser", quality, plan_path)
                try:
                    llm_plan = await parse_plan_with_llm(
                        raw, self._chat_provider,
                        source_file=plan_path,
                        max_steps=config.max_steps_per_plan,
                    )
                    if llm_plan.steps:
                        plan = llm_plan
                        logger.info("Auto-task: LLM parser produced %d steps (replacing regex result)", len(plan.steps))
                except Exception as e:
                    logger.warning("Auto-task: LLM fallback failed, keeping regex result: %s", e)

        if not plan.steps:
            logger.info("Auto-task: plan file %s parsed but contained no steps", plan_path)
            return []

        logger.info("Auto-task: found %d steps in plan file %s for task %s", len(plan.steps), plan_path, task.id)

        # Archive the plan file for traceability (so it won't be re-processed
        # if the workspace is reused for another task).
        archived_path = None
        try:
            plans_dir = os.path.join(workspace, ".claude", "plans")
            os.makedirs(plans_dir, exist_ok=True)
            archived_path = os.path.join(plans_dir, f"{task.id}-plan.md")
            os.rename(plan_path, archived_path)
        except OSError:
            pass

        # Extract any preamble text before the first step as shared context
        plan_context = ""
        if plan.steps and plan.raw_content:
            first_step_title = plan.steps[0].title
            idx = plan.raw_content.find(first_step_title)
            if idx > 0:
                plan_context = plan.raw_content[:idx].strip()
                # Remove the document title heading if present
                import re
                plan_context = re.sub(
                    r"^#\s+.+$\n?", "", plan_context, count=1, flags=re.MULTILINE
                ).strip()

        created_tasks: list[Task] = []
        prev_task_id: str | None = None
        total_steps = len(plan.steps)

        for step_idx, step in enumerate(plan.steps):
            new_id = await generate_task_id(self.db)
            description = build_task_description(
                step, parent_task=task, plan_context=plan_context
            )

            # When chain_dependencies is enabled, only the final step
            # should require approval.  Intermediate steps run without
            # approval so the chain isn't blocked at every step.
            is_last_step = (step_idx == total_steps - 1)
            if config.inherit_approval and config.chain_dependencies:
                step_requires_approval = (
                    task.requires_approval if is_last_step else False
                )
            elif config.inherit_approval:
                step_requires_approval = task.requires_approval
            else:
                step_requires_approval = False

            new_task = Task(
                id=new_id,
                project_id=task.project_id,
                title=step.title,
                description=description,
                priority=config.base_priority + step.priority_hint,
                status=TaskStatus.DEFINED,
                parent_task_id=task.id,
                requires_approval=step_requires_approval,
                plan_source=archived_path,
                is_plan_subtask=True,
            )

            await self.db.create_task(new_task)

            # Chain dependencies: each step depends on the previous one
            if config.chain_dependencies and prev_task_id:
                await self.db.add_dependency(new_id, depends_on=prev_task_id)

            created_tasks.append(new_task)
            prev_task_id = new_id

        return created_tasks

    # How often (seconds) to re-send reminders for tasks awaiting manual
    # approval (no PR URL).
    _NO_PR_REMINDER_INTERVAL: int = 3600      # 1 hour
    # After this many seconds without approval, escalate the notification.
    _NO_PR_ESCALATION_THRESHOLD: int = 86400  # 24 hours
    # Tasks that don't require approval and have no PR URL are auto-completed
    # after this grace period (seconds) to avoid races with the PR-creation
    # path.  Set to 0 for immediate completion.
    _NO_PR_AUTO_COMPLETE_GRACE: int = 120     # 2 minutes

    async def _check_awaiting_approval(self) -> None:
        """Poll PR merge status for tasks in AWAITING_APPROVAL. Throttled to once per 60s.

        Two paths:

        * **Tasks with a PR URL** — check whether the PR has been merged
          (complete the task) or closed without merge (block the task and
          alert about orphaned downstream dependents).
        * **Tasks without a PR URL** — either auto-complete them after a
          grace period (if they don't actually require approval, which can
          happen for intermediate plan subtasks), or send periodic reminders
          so they don't rot silently in the queue.
        """
        now = time.time()
        if now - self._last_approval_check < 60:
            return
        self._last_approval_check = now

        tasks = await self.db.list_tasks(status=TaskStatus.AWAITING_APPROVAL)

        # Clean up reminder tracking for tasks that are no longer AWAITING_APPROVAL.
        active_ids = {t.id for t in tasks}
        for tid in list(self._no_pr_reminded_at):
            if tid not in active_ids:
                del self._no_pr_reminded_at[tid]

        for task in tasks:
            if not task.pr_url:
                await self._handle_awaiting_no_pr(task, now)
                continue

            await self._check_pr_status(task)

    async def _handle_awaiting_no_pr(self, task: Task, now: float) -> None:
        """Handle an AWAITING_APPROVAL task that has no PR URL.

        * If the task doesn't actually require approval, auto-complete it after
          a short grace period (avoids a race with slow PR creation).
        * If the task *does* require approval, send periodic reminders so it
          doesn't rot silently.
        """
        updated_at = await self.db.get_task_updated_at(task.id)
        age = (now - updated_at) if updated_at else 0

        # --- Auto-complete path ---------------------------------------------------
        if not task.requires_approval:
            if age >= self._NO_PR_AUTO_COMPLETE_GRACE:
                await self.db.transition_task(
                    task.id, TaskStatus.COMPLETED,
                    context="auto_complete_no_pr")
                await self.db.log_event(
                    "task_completed", project_id=task.project_id,
                    task_id=task.id,
                    payload="auto-completed: no PR and approval not required")
                await self._notify_channel(
                    f"**Auto-completed:** Task `{task.id}` — {task.title} "
                    f"(no PR created, approval not required).",
                    project_id=task.project_id,
                )
                self._no_pr_reminded_at.pop(task.id, None)
            return

        # --- Manual-approval path -------------------------------------------------
        last_reminded = self._no_pr_reminded_at.get(task.id, 0.0)
        if now - last_reminded < self._NO_PR_REMINDER_INTERVAL:
            return  # throttle reminders

        self._no_pr_reminded_at[task.id] = now

        if age >= self._NO_PR_ESCALATION_THRESHOLD:
            hours = int(age // 3600)
            await self._notify_channel(
                f"⚠️ **Stuck Task:** `{task.id}` — {task.title} has been "
                f"AWAITING_APPROVAL for **{hours}h** with no PR URL.\n"
                f"Use `approve_task {task.id}` to complete it or investigate "
                f"why no PR was created.",
                project_id=task.project_id,
            )
            await self.db.log_event(
                "approval_stuck", project_id=task.project_id,
                task_id=task.id,
                payload=f"no_pr_url, age={hours}h")
        else:
            await self._notify_channel(
                f"🔍 **Awaiting manual approval:** Task `{task.id}` — "
                f"{task.title}\nNo PR URL — use `approve_task {task.id}` "
                f"to complete.",
                project_id=task.project_id,
            )

    async def _check_pr_status(self, task: Task) -> None:
        """Check whether a PR-backed AWAITING_APPROVAL task has been merged."""
        # Need a checkout path to run gh commands
        checkout_path = None
        # Try workspace locked by this task first
        ws = await self.db.get_workspace_for_task(task.id)
        if ws:
            checkout_path = ws.workspace_path
        # Fall back to any workspace for this project
        if not checkout_path:
            workspaces = await self.db.list_workspaces(project_id=task.project_id)
            if workspaces:
                checkout_path = workspaces[0].workspace_path
        if not checkout_path:
            return

        try:
            merged = self.git.check_pr_merged(checkout_path, task.pr_url)
        except Exception as e:
            logger.warning("Error checking PR for task %s: %s", task.id, e)
            return

        if merged is True:
            await self.db.transition_task(
                task.id, TaskStatus.COMPLETED,
                context="pr_merged")
            await self.db.log_event(
                "task_completed", project_id=task.project_id,
                task_id=task.id)
            await self._notify_channel(
                f"**PR Merged:** Task `{task.id}` — {task.title} is now COMPLETED.",
                project_id=task.project_id,
            )
            # Clean up the task branch (remote may already be deleted by GitHub)
            if task.branch_name:
                try:
                    self.git.delete_branch(
                        checkout_path, task.branch_name, delete_remote=True,
                    )
                except Exception:
                    pass  # branch cleanup is best-effort
        elif merged is None:
            # Closed without merge
            await self.db.transition_task(
                task.id, TaskStatus.BLOCKED,
                context="pr_closed")
            await self._notify_channel(
                f"**PR Closed:** Task `{task.id}` — {task.title} "
                f"was closed without merging. Marked as BLOCKED.",
                project_id=task.project_id,
            )
            await self._notify_stuck_chain(task)

    async def _execute_task(self, action: AssignAction) -> None:
        """The full task execution pipeline, run as a background asyncio task.

        Steps:
        1. **Assign** — mark task IN_PROGRESS and agent BUSY in the DB.
        2. **Workspace setup** — clone/link/init the repo, create or switch
           to the task branch (see ``_prepare_workspace``).
        3. **Agent launch** — create an adapter, inject system context and
           the task description, and start the agent process.
        4. **Stream + wait** — forward agent messages to the Discord thread
           while waiting for completion.  If the agent hits a rate limit,
           an exponential-backoff retry loop re-initializes and retries
           (up to ``rate_limit_max_retries`` times) before giving up.
        5. **Result handling** — branch on the agent result:
           - COMPLETED: run ``_complete_workspace`` (commit/merge/PR), then
             ``_generate_tasks_from_plan`` to create follow-up subtasks.
           - FAILED: increment retry count; if exhausted, mark BLOCKED and
             notify about orphaned downstream tasks.
           - PAUSED (rate limit or tokens): set a ``resume_after`` timestamp
             so the task is automatically retried after the backoff.
        6. **Free agent** — reset agent to IDLE regardless of outcome.
        """
        if not self._adapter_factory:
            logger.error("Cannot execute task %s: no adapter factory configured", action.task_id)
            await self._notify_channel(
                f"**Error:** Cannot execute task `{action.task_id}` — no agent adapter configured.",
                project_id=action.project_id,
            )
            return

        # Assign
        await self.db.assign_task_to_agent(action.task_id, action.agent_id)

        # Start agent
        await self.db.transition_task(action.task_id, TaskStatus.IN_PROGRESS,
                                      context="agent_started")
        await self.db.update_agent(action.agent_id, state=AgentState.BUSY)

        task = await self.db.get_task(action.task_id)
        agent = await self.db.get_agent(action.agent_id)

        # Prepare workspace (repo checkout/worktree/init)
        project = await self.db.get_project(action.project_id)
        try:
            workspace = await self._prepare_workspace(task, agent)
        except Exception as e:
            await self._notify_channel(
                f"**Workspace Error:** Task `{task.id}` — {e}",
                project_id=action.project_id,
            )
            workspace = None

        if not workspace:
            # No workspace available — return task to READY and free the agent
            await self.db.transition_task(
                action.task_id, TaskStatus.READY,
                context="no_workspace_available")
            await self.db.update_agent(action.agent_id, state=AgentState.IDLE)
            await self._notify_channel(
                f"**No Workspace:** Task `{task.id}` returned to READY — "
                f"project `{action.project_id}` has no available workspaces. "
                f"Use `/add-workspace` to create one.",
                project_id=action.project_id,
            )
            return

        # Re-fetch task/agent in case _prepare_workspace updated them
        task = await self.db.get_task(action.task_id)
        agent = await self.db.get_agent(action.agent_id)

        # Fetch the workspace object for display in notifications
        ws_obj = await self.db.get_workspace_for_task(task.id)

        # Notify that work is starting
        start_msg = format_task_started(task, agent, workspace=ws_obj)
        handler_ref = self._get_handler()
        await self._notify_channel(
            start_msg,
            project_id=action.project_id,
            embed=format_task_started_embed(task, agent, workspace=ws_obj),
            view=TaskStartedView(task.id, handler=handler_ref),
        )

        # Create a thread for streaming agent output
        thread_send: ThreadSendCallback | None = None
        thread_main_notify: ThreadSendCallback | None = None
        if self._create_thread:
            try:
                thread_name = f"{task.id} | {task.title}"[:100]
                thread_result = await self._create_thread(thread_name, start_msg, action.project_id)
                if thread_result:
                    thread_send, thread_main_notify = thread_result
                    logger.debug("Created thread for task %s", task.id)
                else:
                    logger.warning("Thread creation returned None for task %s", task.id)
            except Exception as e:
                logger.error("Failed to create thread for task %s", task.id, exc_info=True)
        else:
            logger.debug("No thread callback set for task %s", task.id)

        profile = await self._resolve_profile(task)
        if profile:
            logger.info("Task %s: profile='%s' tools=%s mcp=%s", task.id, profile.id, profile.allowed_tools or '(default)', list(profile.mcp_servers.keys()) if profile.mcp_servers else '(none)')
        else:
            logger.info("Task %s: no profile (using system defaults)", task.id)
        adapter = self._adapter_factory.create("claude", profile=profile)
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

        if task.is_plan_subtask:
            # Subtask prompt: implement directly, no re-planning
            context_lines.append(
                "\n## Important: Execution Rules\n"
                "You are running autonomously — there is NO interactive user.\n"
                "Do NOT use plan mode or EnterPlanMode.\n"
                "Do NOT write implementation plans or plan files.\n"
                "Your task is one step of an existing implementation plan — write code, not plans.\n"
                "Implement the changes described below DIRECTLY.\n"
                "If you encounter ambiguity, make reasonable decisions and document in code comments.\n"
                "\n## Important: Committing Your Work\n"
                "When you have finished, you MUST commit your work:\n"
                "1. `git add` the files you changed\n"
                "2. `git commit` with a descriptive message\n"
                "Do NOT push — the system handles pushing and PR creation.\n"
                "\n## Important: Keeping Your Workspace in Sync\n"
                "Before starting work, pull the latest changes from the main branch:\n"
                "1. `git fetch origin`\n"
                "2. `git rebase origin/main` (if on a task branch)\n"
                "This ensures you're working with the latest code and reduces merge conflicts.\n"
                "If a rebase has conflicts you cannot resolve, proceed with your work anyway —\n"
                "the system will handle conflicts during the merge phase."
            )
        else:
            # Root task prompt: may write plans
            context_lines.append(
                "\n## Important: Execution Rules\n"
                "You are running autonomously — there is NO interactive user to approve plans.\n"
                "Do NOT use plan mode or EnterPlanMode. Implement the changes DIRECTLY.\n"
                "If the task description contains a plan, execute it immediately — do not re-plan.\n"
                "\n## Important: Committing Your Work\n"
                "When you have finished making changes, you MUST commit your work:\n"
                "1. `git add` the files you changed\n"
                "2. `git commit` with a descriptive message\n"
                "Do NOT push — the system handles pushing and PR creation.\n"
                "\n## Important: Keeping Your Workspace in Sync\n"
                "Before starting work, pull the latest changes from the main branch:\n"
                "1. `git fetch origin`\n"
                "2. `git rebase origin/main` (if on a task branch)\n"
                "This ensures you're working with the latest code and reduces merge conflicts.\n"
                "If a rebase has conflicts you cannot resolve, proceed with your work anyway —\n"
                "the system will handle conflicts during the merge phase.\n"
                "\n## CRITICAL: Writing Implementation Plans\n"
                "Most tasks do NOT require writing a plan — just implement the changes directly.\n"
                "Only write a plan if the task explicitly asks you to create an implementation plan,\n"
                "investigate and propose changes, or produce a multi-step strategy for follow-up work.\n"
                "\n"
                "If you DO need to write a plan, you MUST follow these rules exactly:\n"
                "1. Write the plan to **`.claude/plan.md`** in the workspace root (preferred)\n"
                "   or `plan.md` — these are the ONLY locations the system checks first\n"
                "2. Do NOT write plans to `notes/`, `docs/`, or any other directory — plans\n"
                "   written elsewhere may not be detected for automatic task splitting\n"
                "3. Name each implementation phase clearly: `## Phase 1: <title>`,\n"
                "   `## Phase 2: <title>`, etc.\n"
                "4. Put ALL background/reference material (design specs, constraints,\n"
                "   architecture notes) BEFORE the phase headings, NOT as separate phases\n"
                "5. Keep each phase focused on a single actionable implementation step\n"
                "\n"
                "This is required for the system to automatically split your plan into\n"
                "follow-up tasks. Plans that mix reference sections with implementation\n"
                "phases will produce low-quality task splits."
            )

        # Inject results from direct upstream dependencies
        dep_ids = await self.db.get_dependencies(task.id)
        if dep_ids:
            dep_sections = []
            for dep_id in sorted(dep_ids):
                dep_task = await self.db.get_task(dep_id)
                dep_result = await self.db.get_task_result(dep_id)
                if not dep_task or not dep_result:
                    continue
                title = dep_task.title or dep_id
                summary = dep_result.get("summary") or "(no summary recorded)"
                if len(summary) > 2000:
                    summary = summary[:2000] + "... [truncated]"
                files = dep_result.get("files_changed") or []
                section = f"### {title}\n**Summary:** {summary}"
                if files:
                    file_list = "\n".join(f"  - `{f}`" for f in files)
                    section += f"\n**Files changed:**\n{file_list}"
                dep_sections.append(section)
            if dep_sections:
                context_lines.append(
                    "\n## Completed Upstream Work\n"
                    "The following tasks were direct dependencies of your task "
                    "and have already been completed:\n\n"
                    + "\n\n".join(dep_sections)
                )

        # Inject profile-specific role instructions
        if profile and profile.system_prompt_suffix:
            context_lines.append(
                f"\n## Agent Role Instructions\n{profile.system_prompt_suffix}"
            )

        context_lines.append(f"\n## Task\n{task.description}")

        full_description = "\n".join(context_lines)

        ctx = TaskContext(
            task_id=task.id,
            description=full_description,
            checkout_path=workspace,
            branch_name=task.branch_name or "",
            mcp_servers=(
                dict(profile.mcp_servers)
                if profile and profile.mcp_servers else {}
            ),
        )

        # Memory recall: inject relevant historical context from memsearch.
        # Runs before agent launch so the agent sees past task results,
        # project notes, and knowledge-base entries that are semantically
        # relevant to the current task.  Failures are non-fatal.
        if self.memory_manager:
            try:
                memories = await self.memory_manager.recall(task, workspace)
                if memories:
                    memory_block = self._format_memory_context(memories)
                    ctx.attached_context.append(memory_block)
            except Exception as e:
                logger.warning("Memory recall failed for task %s: %s", task.id, e)

        await adapter.start(ctx)

        # --- Agent message streaming and question detection ---
        #
        # The agent adapter calls our ``forward_agent_message`` callback each
        # time it produces output (typically every few seconds during active
        # generation).  This closure serves two purposes:
        #
        # 1. **Streaming to Discord** — messages are routed to the per-task
        #    thread if one was created, otherwise to the project's notification
        #    channel with a task/agent header prepended.
        #
        # 2. **Question detection** — the Claude adapter emits a special marker
        #    ``**[AskUserQuestion...]**`` when the agent invokes the
        #    ``AskUserQuestion`` tool.  We detect this marker to send a rich
        #    notification (embed + interactive buttons) to the main channel so
        #    human operators are alerted.  The ``_question_notified`` flag
        #    ensures we only send one such notification per task execution.
        _question_notified = False

        async def forward_agent_message(text: str) -> None:
            nonlocal _question_notified
            if thread_send:
                await thread_send(text)
            else:
                header = f"`{task.id}` | **{agent.name}**\n"
                await self._notify_channel(header + text, project_id=action.project_id)

            # Detect agent questions — the Claude adapter formats
            # AskUserQuestion tool use as "**[AskUserQuestion...]**".
            # When detected, send a dedicated rich notification.
            if not _question_notified and "**[AskUserQuestion" in text:
                _question_notified = True
                # Extract the question text from the message.  The
                # full question details follow the tool-use marker in
                # subsequent lines; use the entire text as context.
                question_text = text.replace("**[AskUserQuestion]**", "").strip()
                if not question_text:
                    question_text = "(Agent is requesting user input — check the task thread for details.)"
                try:
                    await self._notify_agent_question(
                        task, agent, question_text,
                        project_id=action.project_id,
                    )
                except Exception as e:
                    logger.warning("Agent question notification failed: %s", e)

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
                logger.info("Task %s: rate-limit retries exhausted (%d), pausing task.", task.id, _rl_max_retries)
                break

            _backoff = min(_rl_base * (2 ** (_rl_attempt - 1)), _rl_max_backoff)
            logger.info("Task %s: rate limited (attempt %d/%d), waiting %ds before retry.", task.id, _rl_attempt, _rl_max_retries, _backoff)

            await self._notify_channel(
                "⏳ Claude is currently rate-limited. We will try again in a moment.",
                project_id=action.project_id,
            )

            await asyncio.sleep(_backoff)

            await self._notify_channel("✅ Rate limit cleared — resuming now.", project_id=action.project_id)

            # Re-initialise the adapter so the next call starts a fresh query.
            await adapter.start(ctx)

        # -----------------------------------------------------------------
        # Post-execution: record metrics and handle result.
        #
        # At this point the agent has finished (or been rate-limit-retried
        # to exhaustion).  ``output`` holds the final AgentOutput with one
        # of five possible results:
        #   - COMPLETED   → success path (commit/merge/PR/auto-task)
        #   - FAILED      → retry or block path
        #   - PAUSED_RATE_LIMIT / PAUSED_TOKENS → backoff + reschedule
        #   - WAITING_INPUT → agent needs human answer
        # -----------------------------------------------------------------

        # Record token usage for budget tracking before handling the result,
        # so budget warnings are sent even if the result handling fails.
        if output.tokens_used > 0:
            await self.db.record_token_usage(
                action.project_id, action.agent_id,
                action.task_id, output.tokens_used,
            )
            # Check if the project's budget usage has crossed a warning threshold
            try:
                await self._check_budget_warning(
                    action.project_id, output.tokens_used,
                )
            except Exception as e:
                logger.warning("Budget warning check failed: %s", e)

        # Persist task result
        try:
            await self.db.save_task_result(action.task_id, action.agent_id, output)
        except Exception as e:
            logger.error("Failed to save task result: %s", e)

        # Re-fetch task in case retry_count changed
        task = await self.db.get_task(action.task_id)

        # Helper: post to thread if available, otherwise to notifications channel.
        # Used for in-progress updates (e.g. git errors, paused notices).
        # When no thread exists and *embed* is provided, the embed is forwarded
        # to the channel for rich rendering.
        async def _post(msg: str, *, embed: Any = None) -> None:
            if thread_send:
                await thread_send(msg)
            else:
                await self._notify_channel(msg, project_id=action.project_id, embed=embed)

        # Helper: post a brief notification to the main (notifications) channel.
        # When a thread exists this replies to the thread-root message so the
        # notification is visually linked to the thread.  Falls back to a plain
        # channel message when no thread is available.
        async def _notify_brief(msg: str, *, embed: Any = None) -> None:
            if thread_main_notify:
                await thread_main_notify(msg, embed=embed)
            else:
                await self._notify_channel(msg, project_id=action.project_id, embed=embed)

        # ============================================================
        # Result handling: branch on agent outcome
        # ============================================================

        if output.result == AgentResult.COMPLETED:
            # --- Success path ---
            # Transition through VERIFYING (a brief intermediate state) while
            # we run post-completion git operations.  The final state depends
            # on whether the task requires approval (→ AWAITING_APPROVAL) or
            # not (→ COMPLETED).
            await self.db.transition_task(action.task_id, TaskStatus.VERIFYING,
                                          context="agent_completed")

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
                await self.db.transition_task(
                    action.task_id,
                    TaskStatus.AWAITING_APPROVAL,
                    context="pr_created",
                    pr_url=pr_url,
                )
                await self.db.log_event("pr_created",
                                        project_id=action.project_id,
                                        task_id=action.task_id,
                                        agent_id=action.agent_id,
                                        payload=pr_url)
                # Attach approval buttons when not in a thread (thread_send
                # doesn't support views/embeds; the main channel notification
                # already includes the embed).
                approval_view = TaskApprovalView(
                    task.id, handler=self._get_handler()
                )
                if thread_send:
                    await thread_send(format_pr_created(task, pr_url))
                else:
                    await self._notify_channel(
                        format_pr_created(task, pr_url),
                        project_id=action.project_id,
                        embed=format_pr_created_embed(task, pr_url),
                        view=approval_view,
                    )
                brief = f"🔍 PR created for review: {task.title} (`{task.id}`)\n{pr_url}"
                await _notify_brief(brief)
            elif task.requires_approval and not pr_url:
                # Approval required but no PR (e.g. LINK repo) — wait for manual approval
                await self.db.transition_task(
                    action.task_id,
                    TaskStatus.AWAITING_APPROVAL,
                    context="approval_required_no_pr",
                )
                brief = f"🔍 Awaiting manual approval: {task.title} (`{task.id}`)"
                await _notify_brief(brief)
            else:
                # No approval needed — mark completed
                await self.db.transition_task(action.task_id, TaskStatus.COMPLETED,
                                              context="completed_no_approval")
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
                    await self._notify_channel(
                        format_task_completed(task, agent, output),
                        project_id=action.project_id,
                        embed=format_task_completed_embed(task, agent, output),
                    )
                brief = f"✅ Task completed: {task.title} (`{task.id}`)"
                from datetime import datetime, timezone as _tz
                log_date = datetime.now(_tz.utc).strftime("%Y-%m-%d")
                log_path = f"logs/llm/{log_date}/tasks/{task.id}.jsonl"
                brief_embed = format_task_completed_embed(task, agent, output)
                brief_embed.set_footer(text=f"Log: {log_path}")
                await _notify_brief(brief, embed=brief_embed)

            # --- Auto-task generation from implementation plans ---
            # After any successful completion path, check for plan files
            # in the workspace and generate follow-up tasks.
            try:
                generated = await self._generate_tasks_from_plan(task, workspace)
                if generated:
                    # Re-check DEFINED tasks so newly created subtasks whose
                    # dependencies are already met get promoted to READY in
                    # this same cycle rather than waiting for the next one.
                    await self._check_defined_tasks()

                    from src.discord.notifications import (
                        format_plan_generated,
                        format_plan_generated_embed,
                    )
                    is_chained = self.config.auto_task.chain_dependencies
                    plan_msg = format_plan_generated(
                        task, generated,
                        workspace_path=workspace,
                        chained=is_chained,
                    )
                    plan_embed = format_plan_generated_embed(
                        task, generated,
                        workspace_path=workspace,
                        chained=is_chained,
                    )
                    if thread_send:
                        await thread_send(plan_msg)
                    await _notify_brief(plan_msg, embed=plan_embed)
            except Exception as e:
                logger.error("Auto-task generation error for task %s", task.id, exc_info=True)

            # Save completed task result as a memory for future recall.
            if self.memory_manager:
                try:
                    await self.memory_manager.remember(task, output, workspace)
                except Exception as e:
                    logger.warning("Memory remember failed for task %s: %s", task.id, e)

        elif output.result == AgentResult.FAILED:
            # --- Failure path ---
            # Two sub-paths depending on whether retries are left:
            #   - Retries remaining → READY (will be re-scheduled next cycle)
            #   - Retries exhausted → BLOCKED (requires operator intervention)
            # BLOCKED tasks also trigger a dependency chain check to alert
            # operators about downstream tasks that will never proceed.
            new_retry = task.retry_count + 1
            if new_retry >= task.max_retries:
                await self.db.transition_task(action.task_id, TaskStatus.BLOCKED,
                                              context="max_retries",
                                              retry_count=new_retry)
                brief = (
                    f"🚫 Task blocked: {task.title} (`{task.id}`) — "
                    f"max retries ({task.max_retries}) exhausted"
                )
            else:
                await self.db.transition_task(action.task_id, TaskStatus.READY,
                                              context="retry",
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
                handler_ref = self._get_handler()
                if new_retry >= task.max_retries:
                    await self._notify_channel(
                        format_task_blocked(task, last_error=output.error_message),
                        project_id=action.project_id,
                        embed=format_task_blocked_embed(task, last_error=output.error_message),
                        view=TaskBlockedView(task.id, handler=handler_ref),
                    )
                else:
                    await self._notify_channel(
                        format_task_failed(task, agent, output),
                        project_id=action.project_id,
                        embed=format_task_failed_embed(task, agent, output),
                        view=TaskFailedView(task.id, handler=handler_ref),
                    )
            # Brief notification → main channel (reply to thread or standalone)
            await _notify_brief(brief)

            # Check if this blocked task breaks a dependency chain
            if new_retry >= task.max_retries:
                await self._notify_stuck_chain(task)

            # Save failed task result as a memory — failures are valuable
            # context for future tasks working in the same area.
            if self.memory_manager:
                try:
                    await self.memory_manager.remember(task, output, workspace)
                except Exception as e:
                    logger.warning("Memory remember failed for task %s: %s", task.id, e)

        elif output.result in (
            AgentResult.PAUSED_TOKENS, AgentResult.PAUSED_RATE_LIMIT
        ):
            # --- Pause path ---
            # The agent hit an API rate limit or token quota.  Set a
            # ``resume_after`` timestamp so ``_resume_paused_tasks`` will
            # automatically re-promote the task to READY after the backoff
            # period expires.  The backoff duration differs by cause:
            #   - Rate limit: shorter (typically 60s), since these clear quickly.
            #   - Token exhaustion: longer (typically 300s+), since quotas
            #     reset on a longer cycle.
            retry_secs = (
                self.config.pause_retry.rate_limit_backoff_seconds
                if output.result == AgentResult.PAUSED_RATE_LIMIT
                else self.config.pause_retry.token_exhaustion_retry_seconds
            )
            await self.db.transition_task(
                action.task_id, TaskStatus.PAUSED,
                context="tokens_exhausted",
                resume_after=time.time() + retry_secs,
            )
            reason = "rate limit" if output.result == AgentResult.PAUSED_RATE_LIMIT else "token exhaustion"
            await _post(
                f"**Task Paused:** `{task.id}` — {task.title}\n"
                f"Reason: {reason}. Will retry in {retry_secs}s."
            )

        elif output.result == AgentResult.WAITING_INPUT:
            # --- Agent question path ---
            # The agent invoked the AskUserQuestion tool and is waiting for
            # a human response.  WAITING_INPUT is a terminal state for the
            # current execution — the agent process has exited.  When the
            # human responds (via ``/answer`` command), the task is moved
            # back to READY with the answer injected into the task context.
            question_text = output.question or output.summary or "(no question text)"
            await self.db.transition_task(
                action.task_id, TaskStatus.WAITING_INPUT,
                context="agent_question",
            )
            await self.db.log_event(
                "agent_question",
                project_id=action.project_id,
                task_id=action.task_id,
                agent_id=action.agent_id,
                payload=question_text[:500],
            )
            msg = format_agent_question(task, agent, question_text)
            embed = format_agent_question_embed(task, agent, question_text)
            question_view = AgentQuestionView(
                task.id, handler=self._get_handler()
            )
            if thread_send:
                await thread_send(msg)
            else:
                await self._notify_channel(
                    msg,
                    project_id=action.project_id,
                    embed=embed,
                    view=question_view,
                )
            brief = f"❓ Agent question on: {task.title} (`{task.id}`)"
            await _notify_brief(brief, embed=embed)

        # ============================================================
        # Cleanup: release workspace and free the agent
        # ============================================================

        # Release workspace lock so it's available for the next task.
        # This must happen regardless of outcome (success, failure, pause).
        await self.db.release_workspaces_for_task(action.task_id)

        # Free the agent — but respect an externally-set PAUSED state.
        # An admin may have paused the agent via ``/pause-agent`` while
        # it was busy; in that case we preserve PAUSED instead of
        # resetting to IDLE, so the agent stays offline until explicitly
        # resumed.
        post_agent = await self.db.get_agent(action.agent_id)
        next_state = (AgentState.PAUSED
                      if post_agent and post_agent.state == AgentState.PAUSED
                      else AgentState.IDLE)
        await self.db.update_agent(action.agent_id,
                                   state=next_state,
                                   current_task_id=None)
