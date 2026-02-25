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
from src.models import Hook, HookRun

logger = logging.getLogger(__name__)

# Named DB queries (safe — not raw SQL)
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
        self._running: dict[str, asyncio.Task] = {}  # hook_id -> asyncio.Task
        self._last_run_time: dict[str, float] = {}   # hook_id -> last started_at

    async def initialize(self) -> None:
        """Subscribe to EventBus for event-driven hooks."""
        self.bus.subscribe("*", self._on_event)
        # Pre-populate last run times from DB
        hooks = await self.db.list_hooks(enabled=True)
        for hook in hooks:
            last_run = await self.db.get_last_hook_run(hook.id)
            if last_run:
                self._last_run_time[hook.id] = last_run.started_at

    async def tick(self) -> None:
        """Called every orchestrator cycle. Check periodic hooks that are due."""
        # Clean up completed tasks
        done = [hid for hid, t in self._running.items() if t.done()]
        for hid in done:
            task = self._running.pop(hid)
            # Surface exceptions
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc:
                    logger.error("Hook task %s failed: %s", hid, exc)

        hooks = await self.db.list_hooks(enabled=True)
        now = time.time()
        max_concurrent = self.config.hook_engine.max_concurrent_hooks

        for hook in hooks:
            if len(self._running) >= max_concurrent:
                break
            if hook.id in self._running:
                continue  # Already in-flight

            trigger = json.loads(hook.trigger)
            trigger_type = trigger.get("type")

            if trigger_type == "periodic":
                interval = trigger.get("interval_seconds", 3600)
                last = self._last_run_time.get(hook.id, 0)
                if now - last >= interval:
                    if self._check_cooldown(hook, now):
                        self._launch_hook(hook, "periodic")

    async def _on_event(self, data: dict) -> None:
        """Handle EventBus events for event-driven hooks."""
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
        """Launch hook execution as an asyncio task."""
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
        """Run context steps sequentially. Each step's output is available to later steps."""
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
        """Run a named DB query."""
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

    def _should_skip_llm(
        self, steps: list[dict], results: list[dict]
    ) -> str | None:
        """Check if any step's skip condition is met. Returns reason or None."""
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
        """Replace {{step_N}}, {{step_N.field}}, and {{event.field}} placeholders."""
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
        """Resolve a single {{...}} placeholder."""
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
        via its ``llm_config`` field.

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
        """Manually trigger a hook, ignoring cooldown. Returns run ID."""
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
        """Cancel all running hook tasks."""
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
        """Store reference to the orchestrator (for LLM invocation)."""
        self._orchestrator = orchestrator
