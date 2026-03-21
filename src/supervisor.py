"""Supervisor — the single intelligent entity coordinating AgentQueue.

**Supervisor** -- the multi-turn conversation loop that manages the system.
The ``chat()`` method sends the user message (plus history) to the LLM,
checks if the response contains tool-use blocks, executes those tools via
``CommandHandler``, feeds the results back, and repeats until the LLM
produces a final text response.

Tool definitions live in ``tool_registry.py``.  ``TOOLS`` is kept here
as a backward-compatible alias that returns all tools from the registry.

Design boundaries:
    - History management (compaction, summarization, per-channel storage)
      lives in the Discord bot layer, not here.  Supervisor is stateless
      between calls -- the caller passes history in and gets text out.
    - The system prompt shapes the LLM's persona and operating rules.
      It is NOT a code-worker prompt; it instructs the LLM to act as a
      dispatcher that plans and delegates to agents via the tool interface.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from src.chat_providers import ChatProvider, LoggedChatProvider, create_chat_provider
from src.command_handler import CommandHandler
from src.config import AppConfig
from src.llm_logger import LLMLogger
from src.orchestrator import Orchestrator
from src.reflection import ReflectionEngine


# ---------------------------------------------------------------------------
# Tool definitions -- the LLM's interface to the system.
#
# Each entry describes one operation the LLM can invoke during a conversation.
# The names match CommandHandler._cmd_* methods (e.g. "create_task" calls
# _cmd_create_task).  The input_schema tells the LLM what arguments are
# available; the description tells it *when* to use the tool.
# ---------------------------------------------------------------------------
# Tool definitions have moved to tool_registry.py.
# TOOLS is kept as a backward-compatible alias.
from src.tool_registry import ToolRegistry as _ToolRegistry
TOOLS = _ToolRegistry().get_all_tools()

# ---------------------------------------------------------------------------
# System prompt -- now lives in src/prompts/chat_agent_system.md.
# SYSTEM_PROMPT_TEMPLATE below is a deprecated backward-compat stub.
# The actual prompt is loaded via PromptBuilder in _build_system_prompt().
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_TEMPLATE = """You are AgentQueue, a Discord bot that manages an AI agent task queue.
Workspaces root: {workspace_dir}
Use browse_tools/load_tools to discover and load tool categories on demand."""


def _tool_label(name: str, input_data: dict) -> str:
    """Return a short descriptive label for a tool call.

    Instead of just ``run_command`` this produces something like
    ``run_command(pytest tests/)``, giving observers a quick sense of
    what the agent is actually doing at each step.
    """
    detail: str | None = None

    if name == "run_command":
        detail = input_data.get("command")
    elif name == "search_files":
        mode = input_data.get("mode", "grep")
        pattern = input_data.get("pattern", "")
        detail = f"{mode}: {pattern}" if pattern else mode
    elif name == "create_task":
        detail = input_data.get("title")
    elif name == "update_task":
        detail = input_data.get("task_id")
    elif name == "git_log":
        detail = input_data.get("project_id")
    elif name == "git_diff":
        detail = input_data.get("project_id")
    elif name == "git_status":
        detail = input_data.get("project_id")
    elif name == "git_commit":
        detail = input_data.get("message")
    elif name == "git_push":
        detail = input_data.get("branch")
    elif name == "git_pull":
        detail = input_data.get("branch")
    elif name == "git_checkout":
        detail = input_data.get("branch")
    elif name == "read_file":
        detail = input_data.get("path")
    elif name == "write_file":
        detail = input_data.get("path")
    elif name == "list_tasks":
        detail = input_data.get("status")
    elif name == "assign_task":
        detail = input_data.get("task_id")

    if detail:
        # Truncate long details (e.g. long shell commands)
        if len(detail) > 60:
            detail = detail[:57] + "..."
        return f"{name}({detail})"
    return name


class Supervisor:
    """Platform-agnostic LLM supervisor for managing the AgentQueue system.

    Owns the tool definitions, system prompt, LLM client, and multi-turn
    tool-use loop.  Callers (Discord bot, CLI, web API) are responsible for
    building message history and routing responses.

    Business logic is delegated to the shared CommandHandler so that Discord
    slash commands and the supervisor use the same code path.
    """

    def __init__(self, orchestrator: Orchestrator, config: AppConfig,
                 llm_logger: LLMLogger | None = None):
        self.orchestrator = orchestrator
        self.config = config
        self._provider: ChatProvider | None = None
        self._llm_logger = llm_logger
        self.handler = CommandHandler(orchestrator, config)
        self.reflection = ReflectionEngine(config.supervisor.reflection)

    def initialize(self) -> bool:
        """Create LLM provider. Returns True if provider is ready."""
        provider = create_chat_provider(self.config.chat_provider)
        if provider and self._llm_logger and self._llm_logger._enabled:
            provider = LoggedChatProvider(
                provider, self._llm_logger, caller="supervisor.chat"
            )
        self._provider = provider
        return self._provider is not None

    @property
    def is_ready(self) -> bool:
        return self._provider is not None

    async def is_model_loaded(self) -> bool:
        """Check if the LLM model is loaded and ready (delegates to provider)."""
        if not self._provider:
            return True
        return await self._provider.is_model_loaded()

    @property
    def model(self) -> str | None:
        return self._provider.model_name if self._provider else None

    def set_active_project(self, project_id: str | None) -> None:
        self.handler.set_active_project(project_id)

    @property
    def _active_project_id(self) -> str | None:
        return self.handler._active_project_id

    def reload_credentials(self) -> bool:
        """Re-create the LLM provider (e.g. after token refresh). Returns True on success."""
        return self.initialize()

    def _build_system_prompt(self) -> str:
        from src.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        builder.set_identity(
            "chat-agent-system",
            {"workspace_dir": self.config.workspace_dir},
        )
        if self._active_project_id:
            builder.add_context(
                "active_project",
                f"ACTIVE PROJECT: `{self._active_project_id}`. "
                f"Use this as the default project_id for all tools unless the user "
                f"explicitly specifies a different project. When creating tasks, "
                f"listing notes, or any project-scoped operation, use this project.",
            )
        system_prompt, _ = builder.build()
        return system_prompt

    async def chat(
        self,
        text: str,
        user_name: str,
        history: list[dict] | None = None,
        on_progress: "Callable[[str, str | None], Awaitable[None]] | None" = None,
    ) -> str:
        """Process a user message with tool use. Returns response text.

        Starts with core tools only. When the LLM calls ``load_tools``,
        the requested category's tool definitions are added to the active
        set for subsequent turns within this interaction.

        ``history`` is a list of {"role": "user"|"assistant", "content": ...}
        dicts.  The caller is responsible for building history from whatever
        source it uses (Discord channel, CLI readline, HTTP session, etc.).

        ``on_progress`` is an optional async callback for reporting progress
        during multi-turn processing.  It receives ``(event, detail)`` where
        *event* is one of ``"thinking"``, ``"tool_use"``, or ``"responding"``
        and *detail* is an optional string (e.g. tool name).  This allows the
        caller to display intermediate status in a UI (Discord thinking
        indicator, etc.).
        """
        if not self._provider:
            raise RuntimeError("LLM provider not initialized — call initialize() first")

        from src.tool_registry import ToolRegistry
        registry = ToolRegistry()

        # Mutable tool set — starts with core, expands via load_tools
        active_tools: dict[str, dict] = {
            t["name"]: t for t in registry.get_core_tools()
        }

        messages = list(history) if history else []

        # Append current message
        current = {"role": "user", "content": f"[from {user_name}]: {text}"}
        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"] += "\n" + current["content"]
        else:
            messages.append(current)

        # Multi-turn tool-use loop
        tool_actions: list[str] = []
        max_rounds = getattr(self, "_max_tool_rounds", 10)

        for round_num in range(max_rounds):
            # Notify caller that the LLM is thinking
            if on_progress:
                if round_num == 0:
                    await on_progress("thinking", None)
                else:
                    await on_progress("thinking", f"round {round_num + 1}")

            resp = await self._provider.create_message(
                messages=messages,
                system=self._build_system_prompt(),
                tools=list(active_tools.values()),
                max_tokens=1024,
            )

            if not resp.tool_uses:
                if on_progress:
                    await on_progress("responding", None)
                response = "\n".join(resp.text_parts).strip()
                if response:
                    return response
                if tool_actions:
                    return f"Done. Actions taken: {', '.join(tool_actions)}"
                return "Done."

            # Only keep tool_use blocks in assistant message (drop pre-tool commentary)
            messages.append({"role": "assistant", "content": resp.tool_uses})

            tool_results = []
            for tool_use in resp.tool_uses:
                label = _tool_label(tool_use.name, tool_use.input)
                if on_progress:
                    await on_progress("tool_use", label)
                result = await self._execute_tool(tool_use.name, tool_use.input)
                tool_actions.append(label)

                # If load_tools was called, expand active tool set
                if tool_use.name == "load_tools" and "loaded" in result:
                    category = result["loaded"]
                    cat_tools = registry.get_category_tools(category)
                    if cat_tools:
                        for t in cat_tools:
                            active_tools[t["name"]] = t

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": json.dumps(result),
                })

            messages.append({"role": "user", "content": tool_results})

        if tool_actions:
            return f"Done. Actions taken: {', '.join(tool_actions)}"
        return "Done."

    async def summarize(self, transcript: str) -> str | None:
        """Summarize a conversation transcript. Returns None on failure."""
        if not self._provider:
            return None
        # Tag logged calls with the summarize caller identity
        prev_caller = None
        if isinstance(self._provider, LoggedChatProvider):
            prev_caller = self._provider._caller
            self._provider._caller = "supervisor.summarize"
        try:
            resp = await self._provider.create_message(
                messages=[{
                    "role": "user",
                    "content": (
                        "Summarize this Discord conversation concisely. "
                        "Preserve key details: project names, task IDs, repo names, "
                        "decisions made, and any pending questions or requests. "
                        "Keep it factual and brief.\n\n"
                        f"{transcript}"
                    ),
                }],
                system="You are a helpful assistant that summarizes conversations.",
                max_tokens=512,
            )
            parts = resp.text_parts
            return parts[0] if parts else None
        except Exception as e:
            print(f"Summary generation failed: {e}")
            return None
        finally:
            if prev_caller is not None and isinstance(self._provider, LoggedChatProvider):
                self._provider._caller = prev_caller

    async def process_hook_llm(
        self, hook_context: str, rendered_prompt: str,
        project_id: str | None = None, hook_name: str = "unknown",
        on_progress=None,
    ) -> str:
        """Process a hook's LLM invocation through the Supervisor."""
        if project_id:
            self.set_active_project(project_id)
        full_prompt = hook_context + rendered_prompt
        return await self.chat(
            text=full_prompt,
            user_name=f"hook:{hook_name}",
            on_progress=on_progress,
        )

    async def _execute_tool(self, name: str, input_data: dict) -> dict:
        """Execute a tool call via the shared CommandHandler.

        Performs light pre-processing to translate LLM-friendly parameter
        aliases into the canonical names understood by CommandHandler.
        """
        if name == "list_tasks" and input_data.get("show_all"):
            # show_all is an LLM-friendly alias for include_completed.
            # Map it so CommandHandler sees the canonical parameter.
            input_data = {**input_data, "include_completed": True}
            input_data.pop("show_all", None)
        return await self.handler.execute(name, input_data)
