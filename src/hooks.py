"""Event-driven and periodic hook engine for automated workflows.

Hooks enable the system to react to task lifecycle events or run on a timer
without human intervention.  Each hook follows a pipeline::

    trigger -> gather context (shell/file/http/db/git steps)
    -> short-circuit check -> render prompt template -> invoke LLM with tools

The LLM invocation uses a full ChatAgent instance with tool access, so hooks
can create tasks, check status, send notifications, etc. -- anything a human
user can do via Discord chat, a hook can do autonomously.

Two trigger types are supported:
- **Periodic**: fires on a timer (``interval_seconds``), checked every
  orchestrator tick.
- **Event**: fires when a matching EventBus event arrives (e.g. task.completed).

A cooldown mechanism prevents hooks from firing too frequently, and a
configurable concurrency limit caps how many hooks can run simultaneously.

See specs/hooks.md for the full specification.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
import uuid

from src.chat_providers import ChatProvider, LoggedChatProvider, create_chat_provider
from src.config import AppConfig, ChatProviderConfig
from src.database import Database
from src.event_bus import EventBus
from src.logging_config import CorrelationContext
from src.models import Hook, HookRun

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

    async def initialize(self) -> None:
        """Subscribe to EventBus for event-driven hooks and restore state.

        Two setup steps:
        1. Register a wildcard EventBus subscriber so ``_on_event`` receives
           every event type.  The method then filters for matching hooks.
        2. Pre-populate ``_last_run_time`` from the DB so that periodic hooks
           don't all fire immediately on daemon startup.  Without this, a
           restart would cause every periodic hook to trigger simultaneously
           (because their in-memory last-run timestamps would default to 0).
        """
        self.bus.subscribe("*", self._on_event)
        # Pre-populate last run times from DB
        hooks = await self.db.list_hooks(enabled=True)
        for hook in hooks:
            last_run = await self.db.get_last_hook_run(hook.id)
            if last_run:
                self._last_run_time[hook.id] = last_run.started_at

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

        for hook in hooks:
            if len(self._running) >= max_concurrent:
                break  # Global concurrency cap reached
            if hook.id in self._running:
                continue  # Already in-flight — skip

            trigger = json.loads(hook.trigger)
            trigger_type = trigger.get("type")

            if trigger_type == "periodic":
                interval = trigger.get("interval_seconds", 3600)
                last = self._last_run_time.get(hook.id, 0)
                if now - last >= interval:
                    if self._check_cooldown(hook, now):
                        self._launch_hook(hook, "periodic")

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
        4. Invoke the LLM via a ChatAgent instance with full tool access
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
        """Inner implementation with correlation context already set."""
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

        try:
            # 1. Run context steps
            steps = json.loads(hook.context_steps)
            step_results = await self._run_context_steps(steps, event_data)

            await self.db.update_hook_run(
                run.id, context_results=json.dumps(step_results)
            )

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
                return

            # 3. Render prompt
            prompt = self._render_prompt(
                hook.prompt_template, step_results, event_data
            )
            await self.db.update_hook_run(run.id, prompt_sent=prompt)

            # 4. Invoke LLM
            response, tokens = await self._invoke_llm(hook, prompt)

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

        except Exception as e:
            logger.error("Hook %s failed: %s", hook.name, e)
            await self.db.update_hook_run(
                run.id,
                status="failed",
                llm_response=str(e),
                completed_at=time.time(),
            )

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

    async def _invoke_llm(
        self, hook: Hook, prompt: str
    ) -> tuple[str, int]:
        """Invoke the LLM with the rendered prompt using ChatAgent's full tool set.

        Creates a temporary ChatAgent instance so the hook's LLM call has
        access to all the same tools a human user would (create tasks, check
        status, etc.).  The hook can optionally override the LLM provider/model
        via its ``llm_config`` JSON field, allowing hooks to use cheaper/faster
        models for simple checks while reserving expensive models for complex
        reasoning.

        Integration details:
        - The ChatAgent instance is created fresh for each invocation (stateless).
        - If an LLM logger is configured, the provider is wrapped with
          ``LoggedChatProvider`` so hook token usage appears in analytics.
        - The ``user_name`` is set to ``"hook:<hook_name>"`` for audit trails.
        - Token counts are *estimated* from string length (÷4) since the
          ChatAgent's ``chat()`` method doesn't return exact usage.

        Returns ``(response_text, estimated_tokens_used)``.
        """
        from src.chat_agent import ChatAgent

        # Determine provider config
        if hook.llm_config:
            llm_cfg = json.loads(hook.llm_config)
            provider_config = ChatProviderConfig(
                provider=llm_cfg.get("provider", self.config.chat_provider.provider),
                model=llm_cfg.get("model", self.config.chat_provider.model),
                base_url=llm_cfg.get("base_url", self.config.chat_provider.base_url),
            )
        else:
            provider_config = self.config.chat_provider

        # Create a ChatAgent with all the existing tools
        from src.orchestrator import Orchestrator
        # We need an orchestrator reference — reuse the one that owns us
        # This is passed through the config, but we need to store it
        orchestrator = self._orchestrator
        chat_agent = ChatAgent(orchestrator, self.config)
        provider = create_chat_provider(provider_config)
        # Wrap with logging if the orchestrator has an LLM logger
        if provider and hasattr(orchestrator, 'llm_logger') and orchestrator.llm_logger._enabled:
            provider = LoggedChatProvider(
                provider, orchestrator.llm_logger, caller="hook_engine"
            )
        chat_agent._provider = provider

        if not chat_agent._provider:
            raise RuntimeError(
                f"Failed to create LLM provider: {provider_config.provider}"
            )

        # Use the chat method with the hook prompt
        response = await chat_agent.chat(
            text=prompt,
            user_name="hook:" + hook.name,
        )

        # Estimate tokens (we don't have exact count from chat())
        tokens = len(prompt) // 4 + len(response) // 4

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
        1. ``_invoke_llm`` creates a ChatAgent which requires an orchestrator
           reference to register its tools (task management, status queries).
        2. ``_step_memory_search`` accesses ``orchestrator.memory_manager``
           for semantic search in context steps.

        This is a circular reference (orchestrator owns hooks, hooks reference
        orchestrator) that is broken at shutdown via ``hooks.shutdown()``.
        """
        self._orchestrator = orchestrator
