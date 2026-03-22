"""Event-driven and periodic hook engine for automated workflows.

Hooks enable the system to react to task lifecycle events or run on a timer
without human intervention.  Each hook follows a pipeline::

    trigger -> gather context (shell/file/http/db/git steps)
    -> short-circuit check -> render prompt template -> invoke LLM with tools

The LLM invocation uses a full Supervisor instance with tool access, so hooks
can create tasks, check status, send notifications, etc. -- anything a human
user can do via Discord chat, a hook can do autonomously.

Two trigger types are supported:

- **Periodic**: fires on a timer (``interval_seconds``), checked every
  orchestrator tick (~5s).  Actual firing granularity is bounded by the
  tick interval — a 10s periodic hook will fire every 10-15s, not exactly
  every 10s.
- **Event**: fires when a matching EventBus event arrives (e.g.
  ``task.completed``).  Events are delivered asynchronously via
  ``_on_event``, which re-queries all enabled hooks for matches.

Concurrency and cooldown interaction::

    Hook fires → _last_run_time[hook_id] = now, task added to _running
    ↓
    Subsequent triggers check THREE gates before firing:
      1. Concurrency: len(_running) < max_concurrent_hooks   (global cap)
      2. In-flight:   hook_id not in _running                 (per-hook cap)
      3. Cooldown:    now - _last_run_time[hook_id] >= cooldown_seconds
    ↓
    All three must pass → hook fires again
    ↓
    When task finishes: tick() reaps it from _running,
    freeing the per-hook and global concurrency slots

Tool access in hook LLM calls:

    Each hook invocation creates a fresh Supervisor instance with the same
    tool set that human users have via Discord chat.  This means hooks can:
    - Create tasks (``/add-task``)
    - Query task status
    - Send notifications
    - Use any registered MCP tools
    The Supervisor is stateless — no conversation history carries between
    hook invocations.  The rendered prompt is the entire context.

Integration with the orchestrator:

    The orchestrator creates the HookEngine at ``initialize()`` and calls
    ``hooks.tick()`` every cycle (step 7 of ``run_one_cycle``).  The engine
    holds a back-reference to the orchestrator (via ``set_orchestrator``)
    for LLM invocation (Supervisor creation) and memory search access.

    See ``src/orchestrator.py::initialize()`` and ``run_one_cycle()`` for
    the integration points.
    See ``specs/hooks.md`` for the full specification.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone

from src.chat_providers import ChatProvider, LoggedChatProvider, create_chat_provider
from src.config import AppConfig, ChatProviderConfig
from src.database import Database
from src.event_bus import EventBus
from src.file_watcher import FileWatcher, WatchRule
from src.logging_config import CorrelationContext
from src.models import Hook, HookRun, ProjectStatus, Task, TaskStatus
from src.prompt_registry import registry as _prompt_registry

logger = logging.getLogger(__name__)

# Named DB queries — an allowlist of safe, read-only SQL queries that hooks
# can execute via the ``db_query`` context step.  This is a security boundary:
# hooks cannot run arbitrary SQL, only queries listed here.  Parameters are
# bound via SQLite's ``?``-style parameterization to prevent injection.
#
# To add a new query: add an entry here with a descriptive key, then reference
# it in a hook's context_steps as ``{"type": "db_query", "query": "<key>"}``.
NAMED_QUERIES = {
    "recent_task_results": (
        "SELECT t.id, t.title, t.status, tr.result, tr.summary, tr.error_message, "
        "tr.tokens_used, tr.created_at "
        "FROM tasks t LEFT JOIN task_results tr ON tr.task_id = t.id "
        "ORDER BY tr.created_at DESC LIMIT 20"
    ),
    "task_detail": (
        "SELECT t.*, tr.result, tr.summary, tr.error_message, tr.tokens_used "
        "FROM tasks t LEFT JOIN task_results tr ON tr.task_id = t.id "
        "WHERE t.id = :task_id "
        "ORDER BY tr.created_at DESC LIMIT 1"
    ),
    "recent_events": (
        "SELECT * FROM events ORDER BY id DESC LIMIT 50"
    ),
    "hook_runs": (
        "SELECT * FROM hook_runs WHERE hook_id = :hook_id "
        "ORDER BY started_at DESC LIMIT 10"
    ),
    "failed_tasks": (
        "SELECT t.id, t.title, t.status, t.project_id, tr.error_message, "
        "tr.summary, tr.created_at "
        "FROM tasks t LEFT JOIN task_results tr ON tr.task_id = t.id "
        "WHERE t.status = 'failed' "
        "ORDER BY tr.created_at DESC LIMIT 20"
    ),
    "project_tasks_by_status": (
        "SELECT t.id, t.title, t.status, t.priority, t.created_at "
        "FROM tasks t WHERE t.project_id = :project_id "
        "AND t.status = :status "
        "ORDER BY t.priority ASC, t.created_at DESC LIMIT 20"
    ),
    "recent_hook_activity": (
        "SELECT hr.id, hr.hook_id, h.name as hook_name, hr.trigger_reason, "
        "hr.status, hr.tokens_used, hr.started_at, hr.completed_at "
        "FROM hook_runs hr JOIN hooks h ON h.id = hr.hook_id "
        "ORDER BY hr.started_at DESC LIMIT 20"
    ),
}


class HookEngine:
    """Manages hook lifecycle: scheduling, context gathering, LLM invocation.

    The engine subscribes to all EventBus events (wildcard ``*``) so it can
    match event-driven hooks.  Periodic hooks are checked on each ``tick()``
    call from the orchestrator loop.  Running hooks are tracked as asyncio
    Tasks with a configurable concurrency cap.
    """

    def __init__(self, db: Database, bus: EventBus, config: AppConfig):
        self.db = db
        self.bus = bus
        self.config = config
        # In-flight hook executions.  Keyed by hook_id so we can:
        # (a) enforce "one run at a time per hook" — skip if already in-flight,
        # (b) count towards the global max_concurrent_hooks cap,
        # (c) reap finished tasks and surface exceptions in tick().
        self._running: dict[str, asyncio.Task] = {}
        # Tracks when each hook last started (epoch seconds).  Used for both
        # periodic interval checks ("has enough time passed?") and cooldown
        # enforcement ("is the hook still in its cooldown window?").
        # Pre-populated from DB at initialize() to survive daemon restarts.
        self._last_run_time: dict[str, float] = {}
        # FileWatcher for file/folder change monitoring.  Created at
        # initialize() if file_watcher_enabled is True.  Watches are
        # extracted from hook trigger configs that reference file.changed
        # or folder.changed event types.
        self.file_watcher: FileWatcher | None = None
        # Supervisor instance for LLM invocations.  Set via set_supervisor().
        self._supervisor = None

    async def initialize(self) -> None:
        """Subscribe to EventBus for event-driven hooks and restore state.

        Three setup steps:
        1. Register a wildcard EventBus subscriber so ``_on_event`` receives
           every event type.  The method then filters for matching hooks.
        2. Subscribe to ``config.reloaded`` so the hook engine picks up
           changes to ``hook_engine`` settings at runtime.
        3. Pre-populate ``_last_run_time`` from the DB so that periodic hooks
           don't all fire immediately on daemon startup.  Without this, a
           restart would cause every periodic hook to trigger simultaneously
           (because their in-memory last-run timestamps would default to 0).
        """
        self.bus.subscribe("*", self._on_event)
        self.bus.subscribe("config.reloaded", self._on_config_reloaded)
        # Pre-populate last run times from DB
        hooks = await self.db.list_hooks(enabled=True)
        for hook in hooks:
            last_run = await self.db.get_last_hook_run(hook.id)
            if last_run:
                self._last_run_time[hook.id] = last_run.started_at

        # Initialize FileWatcher for file/folder change event hooks
        if self.config.hook_engine.file_watcher_enabled:
            self.file_watcher = FileWatcher(
                self.bus,
                debounce_seconds=self.config.hook_engine.file_watcher_debounce_seconds,
                poll_interval=self.config.hook_engine.file_watcher_poll_interval,
            )
            await self._sync_file_watches(hooks)

    async def _on_config_reloaded(self, data: dict) -> None:
        """Handle config.reloaded events — update hook engine config reference."""
        config = data.get("config")
        if config is not None:
            self.config = config

    async def _sync_file_watches(self, hooks: list[Hook] | None = None) -> None:
        """Synchronize FileWatcher rules from hook trigger configs.

        Scans all enabled hooks for ``file.changed`` and ``folder.changed``
        event triggers that include a ``watch`` config block, and registers
        corresponding WatchRules with the FileWatcher.

        Called at initialize() and can be called again to pick up new hooks.
        """
        if not self.file_watcher:
            return

        if hooks is None:
            hooks = await self.db.list_hooks(enabled=True)

        # Track which watch IDs are still active
        active_ids: set[str] = set()

        for hook in hooks:
            trigger = json.loads(hook.trigger)
            if trigger.get("type") != "event":
                continue

            event_type = trigger.get("event_type", "")
            watch_cfg = trigger.get("watch")
            if not watch_cfg:
                continue

            if event_type not in ("file.changed", "folder.changed"):
                continue

            paths = watch_cfg.get("paths", [])
            if not paths:
                continue

            # Resolve base_dir from project workspace if available
            base_dir = watch_cfg.get("base_dir", "")
            if not base_dir:
                try:
                    ws_path = await self.db.get_project_workspace_path(
                        hook.project_id
                    )
                    if ws_path:
                        base_dir = ws_path
                except Exception:
                    pass

            watch_type = "folder" if event_type == "folder.changed" else "file"
            rule = WatchRule(
                watch_id=hook.id,
                project_id=watch_cfg.get("project_id", hook.project_id),
                paths=paths,
                recursive=watch_cfg.get("recursive", False),
                extensions=watch_cfg.get("extensions"),
                watch_type=watch_type,
                base_dir=base_dir,
            )
            self.file_watcher.add_watch(rule)
            active_ids.add(hook.id)

        # Remove watches for hooks that no longer exist or are disabled
        stale_ids = set(self.file_watcher._watches.keys()) - active_ids
        for stale_id in stale_ids:
            self.file_watcher.remove_watch(stale_id)

    async def tick(self) -> None:
        """Called every orchestrator cycle (~5s). Manage hook lifecycle.

        This method performs two duties each tick:

        **1. Reap completed hook tasks** — scan ``_running`` for asyncio
        Tasks that have finished.  Surface any unhandled exceptions as log
        errors (they are otherwise silently swallowed by asyncio).  Remove
        finished tasks from the dict so their hook_id is eligible to fire
        again.

        **2. Check periodic hook schedules** — for each enabled periodic
        hook, compare the current time against its ``interval_seconds`` and
        ``cooldown_seconds``.  If both thresholds are met and the global
        concurrency cap (``max_concurrent_hooks``) has room, launch the
        hook as a new asyncio task.

        Event-driven hooks are NOT checked here — they are triggered
        asynchronously via ``_on_event`` when the EventBus delivers a
        matching event.
        """
        # Phase 1: Reap completed hook tasks and surface exceptions.
        done = [hid for hid, t in self._running.items() if t.done()]
        for hid in done:
            task = self._running.pop(hid)
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc:
                    logger.error("Hook task %s failed: %s", hid, exc)

        # Phase 2: Check periodic hooks that are due to fire.
        hooks = await self.db.list_hooks(enabled=True)
        now = time.time()
        max_concurrent = self.config.hook_engine.max_concurrent_hooks

        # Pre-fetch project statuses to skip hooks for paused projects.
        checked_projects: dict[str, bool] = {}  # project_id -> is_paused
        for hook in hooks:
            pid = hook.project_id
            if pid not in checked_projects:
                project = await self.db.get_project(pid)
                checked_projects[pid] = (
                    project is not None and project.status == ProjectStatus.PAUSED
                )

        for hook in hooks:
            if len(self._running) >= max_concurrent:
                break  # Global concurrency cap reached
            if hook.id in self._running:
                continue  # Already in-flight — skip
            if checked_projects.get(hook.project_id, False):
                logger.debug(
                    "Skipping hook %s (%s): project %s is paused",
                    hook.id, hook.name, hook.project_id,
                )
                continue

            trigger = json.loads(hook.trigger)
            trigger_type = trigger.get("type")

            if trigger_type == "periodic":
                interval = trigger.get("interval_seconds", 3600)
                last = self._last_run_time.get(hook.id, 0)
                if now - last >= interval:
                    if self._check_cooldown(hook, now):
                        now_iso = datetime.fromtimestamp(
                            now, tz=timezone.utc
                        ).isoformat()
                        timing_data: dict = {
                            "current_time": now_iso,
                            "current_time_epoch": now,
                        }
                        if last:
                            timing_data["last_run_time"] = datetime.fromtimestamp(
                                last, tz=timezone.utc
                            ).isoformat()
                            timing_data["last_run_time_epoch"] = last
                            timing_data["seconds_since_last_run"] = now - last
                        self._launch_hook(
                            hook, "periodic", event_data=timing_data
                        )

        # Phase 3: Poll file watcher for filesystem changes.
        # The file watcher emits file.changed / folder.changed events on
        # the EventBus, which are then picked up by _on_event like any
        # other event-driven hook.
        if self.file_watcher:
            try:
                await self.file_watcher.check()
            except Exception as e:
                logger.warning("FileWatcher check failed: %s", e)

    async def _on_event(self, data: dict) -> None:
        """Handle EventBus events for event-driven hooks.

        Called by the EventBus wildcard subscription (``*``) so this method
        receives *every* event.  For each event:

        1. Extract the event type from ``data["_event_type"]``.
        2. Scan all enabled hooks for those with ``trigger.type == "event"``
           and a matching ``trigger.event_type``.
        3. Skip hooks that are already in-flight, on cooldown, or would
           exceed the concurrency cap.
        4. Launch matching hooks with the full event data payload so
           context steps can reference ``{{event.field}}`` placeholders.

        Note: this re-queries enabled hooks from the DB on every event.
        This is acceptable because events are infrequent (task lifecycle
        transitions) and the query is fast (typically <10 hooks).
        """
        event_type = data.get("_event_type", "")
        hooks = await self.db.list_hooks(enabled=True)
        now = time.time()

        for hook in hooks:
            if hook.id in self._running:
                continue

            trigger = json.loads(hook.trigger)
            if trigger.get("type") != "event":
                continue
            if trigger.get("event_type") != event_type:
                continue

            # Hooks are project-scoped — only fire on events from the same project
            event_project = data.get("project_id", "")
            if event_project and hook.project_id != event_project:
                continue

            # Skip hooks for paused projects
            project = await self.db.get_project(hook.project_id)
            if project and project.status == ProjectStatus.PAUSED:
                logger.debug(
                    "Skipping hook %s (%s): project %s is paused",
                    hook.id, hook.name, hook.project_id,
                )
                continue

            if not self._check_cooldown(hook, now):
                continue

            if len(self._running) >= self.config.hook_engine.max_concurrent_hooks:
                break

            self._launch_hook(
                hook,
                f"event:{event_type}",
                event_data=data,
            )

    def _check_cooldown(self, hook: Hook, now: float) -> bool:
        """Return True if enough time has passed since last run."""
        last = self._last_run_time.get(hook.id, 0)
        return (now - last) >= hook.cooldown_seconds

    def _launch_hook(
        self,
        hook: Hook,
        trigger_reason: str,
        event_data: dict | None = None,
    ) -> None:
        """Launch hook execution as a fire-and-forget asyncio task.

        Records the launch time immediately (before the task runs) so that
        cooldown checks in subsequent ticks/events see the hook as recently
        fired.  The asyncio Task is stored in ``_running`` so ``tick()`` can
        reap it and surface any exceptions.
        """
        self._last_run_time[hook.id] = time.time()
        task = asyncio.create_task(
            self._execute_hook(hook, trigger_reason, event_data)
        )
        self._running[hook.id] = task

    async def _execute_hook(
        self,
        hook: Hook,
        trigger_reason: str,
        event_data: dict | None = None,
    ) -> None:
        """Execute the full hook pipeline for a single invocation.

        Steps:
        1. Run context-gathering steps (shell, file, http, db, git) sequentially
        2. Check short-circuit conditions -- if a step signals "nothing to do",
           the hook is marked skipped and the LLM is never called (saves tokens)
        3. Render the prompt template with step results and event data
        4. Invoke the LLM via a Supervisor instance with full tool access
        5. Record the run outcome (completed/failed/skipped) in the database
        """
        with CorrelationContext(
            hook_id=hook.id,
            project_id=hook.project_id,
            component="hooks",
        ):
            await self._execute_hook_inner(hook, trigger_reason, event_data)

    async def _execute_hook_inner(
        self,
        hook: Hook,
        trigger_reason: str,
        event_data: dict | None = None,
    ) -> None:
        """Inner implementation of the hook execution pipeline.

        Runs with the correlation context already set (by ``_execute_hook``).
        The pipeline has 4 phases, each of which updates the HookRun record
        in the DB for observability:

        Phase 1 — Context gathering:
            Run each context step sequentially (shell, file, http, db, git,
            memory_search).  Results are persisted so operators can inspect
            what data the hook saw.

        Phase 2 — Short-circuit check:
            Evaluate skip conditions (e.g., "skip if shell exit 0").  If a
            skip condition matches, the LLM is never called — this is the
            primary cost-saving mechanism for periodic hooks that usually
            have nothing to report.

        Phase 3 — Prompt rendering:
            Substitute ``{{step_N}}``, ``{{event.field}}`` placeholders in
            the prompt template with actual context step results and event
            data.

        Phase 4 — LLM invocation:
            Create a Supervisor and send the rendered prompt.  The LLM can
            use tools (create tasks, send notifications, etc.) as part of
            its response.

        Error handling: any exception in phases 1-4 is caught, logged, and
        recorded as a "failed" HookRun.  The exception does NOT propagate
        to the caller (tick/event handler) — individual hook failures must
        not disrupt the rest of the system.
        """
        run = HookRun(
            id=str(uuid.uuid4())[:12],
            hook_id=hook.id,
            project_id=hook.project_id,
            trigger_reason=trigger_reason,
            status="running",
            event_data=json.dumps(event_data) if event_data else None,
            started_at=time.time(),
        )
        await self.db.create_hook_run(run)

        # Log to the project's chat channel that the hook is running
        orchestrator = getattr(self, "_orchestrator", None)
        if orchestrator:
            await orchestrator._notify_channel(
                f"🪝 Hook **{hook.name}** is running (trigger: `{trigger_reason}`).",
                project_id=hook.project_id,
            )

        try:
            # Check if this hook skips LLM entirely (only runs context steps)
            trigger = json.loads(hook.trigger)
            skip_llm = trigger.get("skip_llm", False)

            # 1. Run context steps
            steps = json.loads(hook.context_steps)
            step_results = await self._run_context_steps(steps, event_data)

            await self.db.update_hook_run(
                run.id, context_results=json.dumps(step_results)
            )

            # 1b. If skip_llm, we're done after context steps
            if skip_llm:
                await self.db.update_hook_run(
                    run.id,
                    status="completed",
                    completed_at=time.time(),
                )
                logger.info("Hook %s completed (skip_llm)", hook.name)
                return

            # 2. Check short-circuit
            skip_reason = self._should_skip_llm(steps, step_results)
            if skip_reason:
                await self.db.update_hook_run(
                    run.id,
                    status="skipped",
                    skipped_reason=skip_reason,
                    completed_at=time.time(),
                )
                logger.info("Hook %s skipped: %s", hook.name, skip_reason)
                if orchestrator:
                    await orchestrator._notify_channel(
                        f"🪝 Hook **{hook.name}** skipped: {skip_reason}",
                        project_id=hook.project_id,
                    )
                return

            # 3. Render prompt
            prompt = self._render_prompt(
                hook.prompt_template, step_results, event_data
            )
            await self.db.update_hook_run(run.id, prompt_sent=prompt)

            # 4. Invoke LLM — track tool calls for the completion summary
            tool_labels: list[str] = []

            async def _on_hook_progress(event: str, detail: str | None) -> None:
                if event == "tool_use" and detail:
                    tool_labels.append(detail)

            response, tokens = await self._invoke_llm(
                hook, prompt, trigger_reason=trigger_reason,
                on_progress=_on_hook_progress,
                event_data=event_data,
            )

            await self.db.update_hook_run(
                run.id,
                status="completed",
                llm_response=response,
                tokens_used=tokens,
                completed_at=time.time(),
            )
            logger.info(
                "Hook %s completed, tokens=%d", hook.name, tokens
            )

            # Notify completion with tool calls and response summary
            if orchestrator:
                parts = [f"🪝 Hook **{hook.name}** completed."]
                if tool_labels:
                    steps = " → ".join(f"`{t}`" for t in tool_labels)
                    parts.append(f"🔧 {steps}")
                if response:
                    # Truncate long responses for the notification
                    summary = response if len(response) <= 200 else response[:200] + "…"
                    parts.append(f"> {summary}")
                await orchestrator._notify_channel(
                    "\n".join(parts),
                    project_id=hook.project_id,
                )

        except Exception as e:
            logger.error("Hook %s failed: %s", hook.name, e)
            await self.db.update_hook_run(
                run.id,
                status="failed",
                llm_response=str(e),
                completed_at=time.time(),
            )
            if orchestrator:
                try:
                    await orchestrator._notify_channel(
                        f"🪝 Hook **{hook.name}** failed: {e}",
                        project_id=hook.project_id,
                    )
                except Exception:
                    pass

    async def _run_context_steps(
        self,
        steps: list[dict],
        event_data: dict | None = None,
    ) -> list[dict]:
        """Execute the context-gathering pipeline: a sequence of data-fetching steps.

        Steps run **sequentially** (not concurrently) because later steps may
        depend on earlier results via template placeholders.  Each step
        produces a result dict whose shape depends on the step type:

        - ``shell``: ``{stdout, stderr, exit_code}``
        - ``read_file``: ``{content}``
        - ``http``: ``{body, status_code}``
        - ``db_query``: ``{rows, count}``
        - ``git_diff``: ``{diff, exit_code}``
        - ``memory_search``: ``{content, count}``

        On error, the step result contains an ``{error: "..."}`` entry
        instead.  The pipeline continues past errors so that downstream
        steps and the short-circuit check still run.
        """
        results = []
        for i, step in enumerate(steps):
            step_type = step.get("type", "")
            try:
                if step_type == "shell":
                    result = await self._step_shell(step)
                elif step_type == "read_file":
                    result = await self._step_read_file(step)
                elif step_type == "http":
                    result = await self._step_http(step)
                elif step_type == "db_query":
                    result = await self._step_db_query(step, event_data)
                elif step_type == "git_diff":
                    result = await self._step_git_diff(step)
                elif step_type == "memory_search":
                    result = await self._step_memory_search(step, event_data)
                elif step_type == "create_task":
                    result = await self._step_create_task(step, event_data)
                elif step_type == "run_tests":
                    result = await self._step_run_tests(step)
                elif step_type == "list_files":
                    result = await self._step_list_files(step, event_data)
                elif step_type == "file_diff":
                    result = await self._step_file_diff(step, event_data)
                else:
                    result = {"error": f"Unknown step type: {step_type}"}
            except Exception as e:
                result = {"error": str(e)}

            result["_step_index"] = i
            results.append(result)
        return results

    async def _step_shell(self, step: dict) -> dict:
        """Execute a shell command and capture output."""
        command = step.get("command", "")
        timeout = step.get("timeout", 60)

        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return {
                "stdout": "",
                "stderr": f"Command timed out after {timeout}s",
                "exit_code": -1,
            }

        return {
            "stdout": stdout.decode("utf-8", errors="replace")[:50000],
            "stderr": stderr.decode("utf-8", errors="replace")[:10000],
            "exit_code": proc.returncode,
        }

    async def _step_read_file(self, step: dict) -> dict:
        """Read a file's contents."""
        path = step.get("path", "")
        max_lines = step.get("max_lines", 500)

        try:
            with open(path, "r") as f:
                lines = []
                for i, line in enumerate(f):
                    if i >= max_lines:
                        break
                    lines.append(line.rstrip("\n"))
            return {"content": "\n".join(lines)}
        except Exception as e:
            return {"error": str(e)}

    async def _step_http(self, step: dict) -> dict:
        """Make an HTTP request."""
        import urllib.request
        import urllib.error

        url = step.get("url", "")
        timeout = step.get("timeout", 30)

        try:
            req = urllib.request.Request(url)
            resp = await asyncio.to_thread(
                urllib.request.urlopen, req, timeout=timeout
            )
            body = resp.read().decode("utf-8", errors="replace")[:50000]
            return {"body": body, "status_code": resp.status}
        except urllib.error.HTTPError as e:
            return {"body": str(e), "status_code": e.code}
        except Exception as e:
            return {"body": "", "status_code": 0, "error": str(e)}

    async def _step_db_query(
        self, step: dict, event_data: dict | None = None
    ) -> dict:
        """Run a pre-defined named DB query (not arbitrary SQL).

        Security: hooks cannot execute arbitrary SQL.  The ``query`` field
        must match a key in ``NAMED_QUERIES`` — a hardcoded allowlist of
        safe, read-only queries.  Parameters are passed via SQLite's
        parameterized query mechanism (``:param_name``) to prevent injection.

        Template placeholders (``{{event.field}}``) in parameter values are
        resolved before execution, allowing event-driven hooks to query
        data related to the triggering event.
        """
        query_name = step.get("query", "")
        params = step.get("params", {})

        if query_name not in NAMED_QUERIES:
            return {"error": f"Unknown query: {query_name}"}

        sql = NAMED_QUERIES[query_name]

        # Interpolate params from event data
        resolved_params = {}
        for key, value in params.items():
            if isinstance(value, str) and value.startswith("{{") and value.endswith("}}"):
                value = self._resolve_placeholder(value, [], event_data)
            resolved_params[key] = value

        try:
            cursor = await self.db._db.execute(sql, resolved_params)
            rows = await cursor.fetchall()
            return {
                "rows": [dict(r) for r in rows],
                "count": len(rows),
            }
        except Exception as e:
            return {"error": str(e)}

    async def _step_git_diff(self, step: dict) -> dict:
        """Get git diff output."""
        workspace = step.get("workspace", ".")
        base_branch = step.get("base_branch", "main")

        try:
            result = await asyncio.create_subprocess_exec(
                "git", "diff", f"{base_branch}...HEAD",
                cwd=workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                result.communicate(), timeout=30
            )
            return {
                "diff": stdout.decode("utf-8", errors="replace")[:50000],
                "exit_code": result.returncode,
            }
        except Exception as e:
            return {"error": str(e)}

    async def _step_memory_search(
        self, step: dict, event_data: dict | None = None
    ) -> dict:
        """Execute a memory_search context step.

        Performs a semantic search against a project's memory index and
        returns the matching chunks as a formatted context string.  When the
        memory subsystem is unavailable (not configured, memsearch not
        installed, etc.) the step returns an empty ``content`` string rather
        than an error — hooks should degrade gracefully.

        Step config keys:
            ``project_id`` – target project (or ``{{event.project_id}}``)
            ``query``      – semantic search query (template placeholders OK)
            ``top_k``      – max results to return (default 3)
        """
        project_id = step.get("project_id", "")
        query = step.get("query", "")
        top_k = step.get("top_k", 3)

        # Resolve template placeholders in project_id and query
        if event_data:
            if "{{" in project_id:
                project_id = self._resolve_placeholder(
                    project_id, [], event_data
                )
            if "{{" in query:
                query = self._resolve_placeholder(query, [], event_data)

        if not project_id or not query:
            return {"content": "", "error": "project_id and query are required"}

        orchestrator = getattr(self, "_orchestrator", None)
        if not orchestrator or not getattr(orchestrator, "memory_manager", None):
            return {"content": "", "count": 0}

        # Look up workspace path for the project
        try:
            workspace = await self.db.get_project_workspace_path(project_id)
        except Exception:
            workspace = None

        if not workspace:
            return {"content": "", "count": 0, "error": f"No workspace for project '{project_id}'"}

        try:
            results = await orchestrator.memory_manager.search(
                project_id, workspace, query, top_k=top_k
            )
        except Exception as e:
            logger.warning("memory_search step failed for project %s: %s", project_id, e)
            return {"content": "", "count": 0, "error": str(e)}

        # Format results as a readable context string
        if not results:
            return {"content": "", "count": 0}

        parts = []
        for i, mem in enumerate(results, 1):
            source = mem.get("source", "unknown")
            heading = mem.get("heading", "")
            content = mem.get("content", "")
            score = mem.get("score", 0)
            header = f"[{i}] {heading}" if heading else f"[{i}] (from {source})"
            parts.append(f"{header}  (score: {score:.3f})\n{content}")

        formatted = "\n\n---\n\n".join(parts)
        return {"content": formatted, "count": len(results)}

    async def _step_create_task(
        self, step: dict, event_data: dict | None = None
    ) -> dict:
        """Create a task from hook step config, resolving {{event.field}} placeholders."""
        from src.task_names import generate_task_id

        def resolve(template: str) -> str:
            if not template or not event_data:
                return template
            return re.sub(
                r"\{\{(.+?)\}\}",
                lambda m: self._resolve_placeholder(
                    "{{" + m.group(1) + "}}", [], event_data,
                ),
                template,
            )

        try:
            task_id = await generate_task_id(self.db)
            title = resolve(step.get("title_template", "Hook-created task"))
            description = resolve(step.get("description_template", ""))
            project_id = resolve(step.get("project_id", ""))
            parent_task_id = resolve(step.get("parent_task_id")) if step.get("parent_task_id") else None
            profile_id = resolve(step.get("profile_id")) if step.get("profile_id") else None
            preferred_workspace_id = resolve(step.get("preferred_workspace_id")) if step.get("preferred_workspace_id") else None
            branch_name = resolve(step.get("branch_name")) if step.get("branch_name") else None
            priority = step.get("priority", 100)

            task = Task(
                id=task_id,
                project_id=project_id,
                title=title,
                description=description,
                priority=priority,
                status=TaskStatus.DEFINED,
                parent_task_id=parent_task_id,
                profile_id=profile_id,
                preferred_workspace_id=preferred_workspace_id,
                branch_name=branch_name,
            )
            await self.db.create_task(task)

            # Add context entries if specified
            for ctx_entry in step.get("context_entries", []):
                resolved = {k: resolve(v) for k, v in ctx_entry.items()}
                await self.db.add_task_context(
                    task_id,
                    type=resolved.get("type", "system"),
                    label=resolved.get("label", ""),
                    content=resolved.get("content", ""),
                )

            logger.info("Hook created task %s: %s", task_id, title)
            return {"task_id": task_id, "created": True}
        except Exception as e:
            logger.error("create_task step failed: %s", e)
            return {"error": str(e)}

    async def _step_run_tests(self, step: dict) -> dict:
        """Run a test command and parse results for automated testing hooks.

        Extends the basic ``shell`` step with structured test result parsing.
        Captures exit code, stdout/stderr, and attempts to extract individual
        test failure names from common test frameworks (pytest, jest, mocha).

        Step config keys:
            ``command``    – test command to run (e.g. ``"pytest tests/ -v"``)
            ``timeout``    – max seconds (default 300 for test suites)
            ``workspace``  – working directory (default ``"."``)
            ``framework``  – hint for parsing: ``"pytest"``, ``"jest"``, or ``"auto"``
        """
        command = step.get("command", "pytest")
        timeout = step.get("timeout", 300)
        workspace = step.get("workspace", ".")
        framework = step.get("framework", "auto")

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return {
                    "stdout": "",
                    "stderr": f"Test command timed out after {timeout}s",
                    "exit_code": -1,
                    "passed": False,
                    "failures": [],
                    "test_count": 0,
                }

            stdout_str = stdout.decode("utf-8", errors="replace")[:100000]
            stderr_str = stderr.decode("utf-8", errors="replace")[:50000]
            exit_code = proc.returncode
            passed = exit_code == 0

            # Parse test failures from output
            failures = self._parse_test_failures(
                stdout_str, stderr_str, framework
            )

            # Try to extract test count
            test_count = self._parse_test_count(stdout_str, framework)

            return {
                "stdout": stdout_str,
                "stderr": stderr_str,
                "exit_code": exit_code,
                "passed": passed,
                "failures": failures,
                "test_count": test_count,
                "framework": framework,
            }
        except Exception as e:
            return {"error": str(e), "passed": False, "failures": []}

    @staticmethod
    def _parse_test_failures(
        stdout: str, stderr: str, framework: str
    ) -> list[str]:
        """Extract failing test names from test output.

        Supports pytest (``FAILED test_file.py::test_name``) and
        jest/mocha (``✕ test description`` or ``FAIL test_file``).
        """
        failures = []
        combined = stdout + "\n" + stderr

        # pytest: "FAILED tests/test_foo.py::test_bar - AssertionError..."
        for match in re.finditer(
            r"FAILED\s+(\S+::\S+)", combined
        ):
            failures.append(match.group(1))

        # jest/mocha: "✕ test description" or "● test description"
        if not failures:
            for match in re.finditer(
                r"[✕●✗]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$", combined, re.MULTILINE
            ):
                failures.append(match.group(1).strip())

        # Generic: "FAIL " prefix (jest summary style)
        if not failures:
            for match in re.finditer(
                r"^FAIL\s+(\S+)", combined, re.MULTILINE
            ):
                failures.append(match.group(1))

        return failures[:50]  # Cap at 50 to avoid huge payloads

    @staticmethod
    def _parse_test_count(stdout: str, framework: str) -> int:
        """Try to extract total test count from test output."""
        # jest: "Tests: 2 failed, 5 passed, 7 total" — check first since
        # the "passed" pattern below would also match the jest format
        match = re.search(r"Tests:\s+.*?(\d+)\s+total", stdout)
        if match:
            return int(match.group(1))

        # pytest: "5 passed, 2 failed"
        match = re.search(
            r"(\d+)\s+passed(?:.*?(\d+)\s+failed)?", stdout
        )
        if match:
            total = int(match.group(1))
            if match.group(2):
                total += int(match.group(2))
            return total

        return 0

    async def _step_list_files(
        self, step: dict, event_data: dict | None = None
    ) -> dict:
        """List files in a directory, optionally filtered by extension or pattern.

        Useful for docs hooks and folder watch automation to enumerate what
        files exist in a directory before taking action.

        Step config keys:
            ``path``       – directory to list (supports ``{{event.path}}``)
            ``recursive``  – descend into subdirectories (default False)
            ``extensions`` – list of extensions to filter (e.g. ``[".md", ".txt"]``)
            ``max_files``  – maximum number of files to return (default 200)
        """
        path = step.get("path", ".")
        recursive = step.get("recursive", False)
        extensions = step.get("extensions")
        max_files = step.get("max_files", 200)

        # Resolve placeholders in path
        if event_data and "{{" in path:
            path = self._resolve_placeholder(path, [], event_data)

        if not os.path.isdir(path):
            return {"error": f"Not a directory: {path}", "files": []}

        files = []
        try:
            if recursive:
                for dirpath, dirnames, filenames in os.walk(path):
                    dirnames[:] = [
                        d for d in dirnames if not d.startswith(".")
                    ]
                    for fname in sorted(filenames):
                        if fname.startswith("."):
                            continue
                        if extensions and not any(
                            fname.endswith(ext) for ext in extensions
                        ):
                            continue
                        full = os.path.join(dirpath, fname)
                        rel = os.path.relpath(full, path)
                        try:
                            stat = os.stat(full)
                            files.append({
                                "path": rel,
                                "size": stat.st_size,
                                "mtime": stat.st_mtime,
                            })
                        except OSError:
                            pass
                        if len(files) >= max_files:
                            break
                    if len(files) >= max_files:
                        break
            else:
                for fname in sorted(os.listdir(path)):
                    if fname.startswith("."):
                        continue
                    full = os.path.join(path, fname)
                    if not os.path.isfile(full):
                        continue
                    if extensions and not any(
                        fname.endswith(ext) for ext in extensions
                    ):
                        continue
                    try:
                        stat = os.stat(full)
                        files.append({
                            "path": fname,
                            "size": stat.st_size,
                            "mtime": stat.st_mtime,
                        })
                    except OSError:
                        pass
                    if len(files) >= max_files:
                        break
        except (OSError, PermissionError) as e:
            return {"error": str(e), "files": []}

        return {
            "files": files,
            "count": len(files),
            "directory": path,
            "content": "\n".join(f["path"] for f in files),
        }

    async def _step_file_diff(
        self, step: dict, event_data: dict | None = None
    ) -> dict:
        """Get the diff of a specific file against its last committed version.

        Useful for file change hooks to see exactly what changed.

        Step config keys:
            ``path``       – file path (supports ``{{event.path}}``)
            ``workspace``  – git workspace root (default ``"."``)
        """
        path = step.get("path", "")
        workspace = step.get("workspace", ".")

        # Resolve placeholders
        if event_data and "{{" in path:
            path = self._resolve_placeholder(path, [], event_data)
        if event_data and "{{" in workspace:
            workspace = self._resolve_placeholder(workspace, [], event_data)

        if not path:
            return {"error": "path is required", "diff": ""}

        try:
            result = await asyncio.create_subprocess_exec(
                "git", "diff", "HEAD", "--", path,
                cwd=workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                result.communicate(), timeout=30
            )
            diff_output = stdout.decode("utf-8", errors="replace")[:50000]

            # If no diff against HEAD, try against index
            if not diff_output.strip():
                result2 = await asyncio.create_subprocess_exec(
                    "git", "diff", "--cached", "--", path,
                    cwd=workspace,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout2, _ = await asyncio.wait_for(
                    result2.communicate(), timeout=30
                )
                diff_output = stdout2.decode("utf-8", errors="replace")[:50000]

            return {
                "diff": diff_output,
                "path": path,
                "exit_code": result.returncode,
            }
        except Exception as e:
            return {"error": str(e), "diff": ""}

    def _should_skip_llm(
        self, steps: list[dict], results: list[dict]
    ) -> str | None:
        """Evaluate short-circuit conditions to skip the LLM invocation.

        Each context step can declare a skip condition.  If the condition is
        met, the hook is marked "skipped" and the LLM is never called.  This
        is a cost-saving mechanism: most periodic hooks (e.g. "check if any
        tasks failed") produce empty results the majority of the time.

        Supported skip conditions (set on the step config):
        - ``skip_llm_if_exit_zero``: skip if a shell step exited 0 (success)
        - ``skip_llm_if_empty``: skip if stdout + content is empty
        - ``skip_llm_if_status_ok``: skip if an HTTP step returned 2xx

        Returns a human-readable reason string, or None if no skip applies.
        """
        for i, step in enumerate(steps):
            if i >= len(results):
                break
            result = results[i]

            if step.get("skip_llm_if_exit_zero") and result.get("exit_code") == 0:
                return f"step_{i}: exit code 0 (skip_llm_if_exit_zero)"

            if step.get("skip_llm_if_empty"):
                output = result.get("stdout", "") + result.get("content", "")
                if not output.strip():
                    return f"step_{i}: output empty (skip_llm_if_empty)"

            if step.get("skip_llm_if_status_ok"):
                status = result.get("status_code", 0)
                if 200 <= status < 300:
                    return f"step_{i}: HTTP {status} (skip_llm_if_status_ok)"

        return None

    def _render_prompt(
        self,
        template: str,
        step_results: list[dict],
        event_data: dict | None = None,
    ) -> str:
        """Render the hook's prompt template by substituting placeholders.

        Uses a regex to find all ``{{...}}`` patterns and delegates each
        match to ``_resolve_placeholder``.  The rendered prompt is then
        sent to the LLM as the hook's input.

        Placeholders reference context step results (``{{step_0}}``,
        ``{{step_1.exit_code}}``) or event data (``{{event.task_id}}``),
        allowing hooks to inject dynamic context into their prompts.
        """
        def replacer(match):
            placeholder = match.group(1)
            return self._resolve_placeholder(
                "{{" + placeholder + "}}", step_results, event_data
            )

        return re.sub(r"\{\{(.+?)\}\}", replacer, template)

    def _resolve_placeholder(
        self,
        placeholder: str,
        step_results: list[dict] | None = None,
        event_data: dict | None = None,
    ) -> str:
        """Resolve a single ``{{...}}`` template placeholder to a string.

        Supported placeholder forms:

        - ``{{event}}`` — the full event data as JSON
        - ``{{event.field}}`` — a single field from the event data dict
        - ``{{step_N}}`` — auto-selects the "main" output from step N
          (prefers stdout > content > body > diff, falls back to JSON)
        - ``{{step_N.field}}`` — a specific field from step N's result dict

        Unrecognized placeholders are returned as-is (including the braces)
        so they're visible in the rendered prompt for debugging.
        """
        step_results = step_results or []
        key = placeholder.strip("{}")

        # {{event.field}}
        if key.startswith("event."):
            field = key[6:]
            if event_data:
                return str(event_data.get(field, ""))
            return ""

        # {{event}}
        if key == "event":
            return json.dumps(event_data) if event_data else ""

        # {{step_N}} or {{step_N.field}}
        step_match = re.match(r"step_(\d+)(?:\.(.+))?", key)
        if step_match:
            idx = int(step_match.group(1))
            field = step_match.group(2)
            if idx < len(step_results):
                result = step_results[idx]
                if field:
                    return str(result.get(field, ""))
                # Default: return stdout or content or body or diff
                for k in ("stdout", "content", "body", "diff"):
                    if k in result and result[k]:
                        return str(result[k])
                return json.dumps(result)
            return ""

        return placeholder

    async def _build_hook_context(
        self, hook: Hook, trigger_reason: str,
        event_data: dict | None = None,
    ) -> str:
        """Build a dynamic context preamble for the hook's LLM prompt.

        Fetches project metadata (name, workspace path, repo URL, default
        branch) and renders the ``hook-context`` prompt template with these
        values.  This gives the hook's LLM a clear understanding of:

        - **Which project** it's operating on (name, ID, workspace path)
        - **Why it fired** (periodic, event, manual, cron)
        - **Its role**: a dispatcher that should create tasks for code work
        - **What tools** it has (task management, git, notes, memory, etc.)

        For periodic hooks, timing context (current time, last run time) is
        included so the LLM can scope its work to changes since the last run.

        The preamble is prepended to the rendered hook prompt so it always
        appears at the top of the LLM's context, regardless of what the
        hook author wrote in the prompt template.
        """
        # Fetch project details for richer context
        project = await self.db.get_project(hook.project_id)
        project_name = project.name if project else hook.project_id

        workspace_dir = await self.db.get_project_workspace_path(
            hook.project_id
        )

        # Build optional context lines (only include if available)
        ws_line = (
            f"Workspace: `{workspace_dir}`\n" if workspace_dir else ""
        )
        repo_line = (
            f"Repository: `{project.repo_url}`\n"
            if project and project.repo_url else ""
        )
        branch_line = (
            f"Default branch: `{project.repo_default_branch}`\n"
            if project and project.repo_default_branch else ""
        )

        # Build timing context for periodic hooks
        timing_line = ""
        if event_data and trigger_reason == "periodic":
            parts = []
            if event_data.get("current_time"):
                parts.append(f"Current time: `{event_data['current_time']}`")
            if event_data.get("last_run_time"):
                parts.append(
                    f"Last run: `{event_data['last_run_time']}`"
                )
                secs = event_data.get("seconds_since_last_run")
                if secs is not None:
                    parts.append(
                        f"Elapsed since last run: {int(secs)}s"
                    )
            else:
                parts.append("Last run: *first run*")
            timing_line = "\n".join(parts) + "\n"

        from src.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        builder.set_identity("hook-context", {
            "hook_name": hook.name,
            "project_id": hook.project_id or "",
            "project_name": project_name,
            "workspace_dir": ws_line,
            "repo_url": repo_line,
            "default_branch": branch_line,
            "trigger_reason": trigger_reason,
            "timing_context": timing_line,
        })
        result, _ = builder.build()
        return result

    async def _invoke_llm(
        self, hook: Hook, prompt: str,
        trigger_reason: str = "unknown",
        on_progress=None,
        event_data: dict | None = None,
    ) -> tuple[str, int]:
        """Invoke the LLM through the Supervisor.

        Uses the Supervisor's process_hook_llm() method instead of creating
        a fresh ChatAgent instance. Preserves per-hook llm_config overrides
        by temporarily swapping the provider.
        """
        # Build dynamic context preamble
        context_preamble = await self._build_hook_context(
            hook, trigger_reason, event_data=event_data,
        )

        if self._supervisor:
            # Handle per-hook LLM config overrides
            original_provider = None
            if hook.llm_config:
                llm_cfg = json.loads(hook.llm_config)
                provider_config = ChatProviderConfig(
                    provider=llm_cfg.get("provider", self.config.chat_provider.provider),
                    model=llm_cfg.get("model", self.config.chat_provider.model),
                    base_url=llm_cfg.get("base_url", self.config.chat_provider.base_url),
                )
                original_provider = self._supervisor._provider
                hook_provider = create_chat_provider(provider_config)
                if hook_provider:
                    orchestrator = self._orchestrator
                    if hasattr(orchestrator, 'llm_logger') and orchestrator.llm_logger._enabled:
                        hook_provider = LoggedChatProvider(
                            hook_provider, orchestrator.llm_logger, caller="hook_engine"
                        )
                    self._supervisor._provider = hook_provider

            try:
                response = await self._supervisor.process_hook_llm(
                    hook_context=context_preamble,
                    rendered_prompt=prompt,
                    project_id=hook.project_id,
                    hook_name=hook.name,
                    on_progress=on_progress,
                )
            finally:
                # Restore original provider if we swapped it
                if original_provider is not None:
                    self._supervisor._provider = original_provider

            tokens = len(context_preamble + prompt) // 4 + len(response) // 4
            return response, tokens

        # Fallback: create Supervisor directly (backward compat)
        from src.supervisor import Supervisor

        if hook.llm_config:
            llm_cfg = json.loads(hook.llm_config)
            provider_config = ChatProviderConfig(
                provider=llm_cfg.get("provider", self.config.chat_provider.provider),
                model=llm_cfg.get("model", self.config.chat_provider.model),
                base_url=llm_cfg.get("base_url", self.config.chat_provider.base_url),
            )
        else:
            provider_config = self.config.chat_provider

        orchestrator = self._orchestrator
        supervisor = Supervisor(orchestrator, self.config)
        supervisor.set_active_project(hook.project_id)

        provider = create_chat_provider(provider_config)
        if provider and hasattr(orchestrator, 'llm_logger') and orchestrator.llm_logger._enabled:
            provider = LoggedChatProvider(
                provider, orchestrator.llm_logger, caller="hook_engine"
            )
        supervisor._provider = provider

        if not supervisor._provider:
            raise RuntimeError(f"Failed to create LLM provider: {provider_config.provider}")

        full_prompt = context_preamble + prompt
        response = await supervisor.chat(
            text=full_prompt,
            user_name="hook:" + hook.name,
            on_progress=on_progress,
        )
        tokens = len(full_prompt) // 4 + len(response) // 4
        return response, tokens

    async def fire_hook(self, hook_id: str) -> str:
        """Manually trigger a hook, ignoring cooldown.

        Used by the ``/fire-hook`` Discord command to allow operators to
        run a hook on-demand (e.g. for testing or urgent checks).  Unlike
        the normal periodic/event trigger path, this bypasses cooldown
        checks — but it still respects the "already running" guard (a
        hook cannot be fired if it's already in-flight).

        Returns the hook ID (used as a proxy run identifier).
        """
        hook = await self.db.get_hook(hook_id)
        if not hook:
            raise ValueError(f"Hook '{hook_id}' not found")
        if hook.id in self._running:
            raise ValueError(f"Hook '{hook_id}' is already running")

        self._last_run_time[hook.id] = time.time()
        task = asyncio.create_task(
            self._execute_hook(hook, "manual")
        )
        self._running[hook.id] = task
        return hook.id

    async def shutdown(self) -> None:
        """Cancel all running hook tasks and wait for them to finish.

        Uses ``asyncio.gather(..., return_exceptions=True)`` to ensure we
        don't propagate CancelledError — we just want everything stopped.
        """
        for hook_id, task in self._running.items():
            if not task.done():
                task.cancel()
        # Wait for all to finish
        if self._running:
            await asyncio.gather(
                *self._running.values(), return_exceptions=True
            )
        self._running.clear()

    def set_orchestrator(self, orchestrator) -> None:
        """Store reference to the orchestrator (for LLM invocation).

        The HookEngine needs access to the Orchestrator for two reasons:
        1. ``_invoke_llm`` creates a Supervisor which requires an orchestrator
           reference to register its tools (task management, status queries).
        2. ``_step_memory_search`` accesses ``orchestrator.memory_manager``
           for semantic search in context steps.

        This is a circular reference (orchestrator owns hooks, hooks reference
        orchestrator) that is broken at shutdown via ``hooks.shutdown()``.
        """
        self._orchestrator = orchestrator

    def set_supervisor(self, supervisor) -> None:
        """Set the Supervisor instance for LLM invocations."""
        self._supervisor = supervisor
