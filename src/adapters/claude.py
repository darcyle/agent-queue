"""Claude Code adapter -- runs AI agent tasks via the Claude Agent SDK.

This implements AgentAdapter by wrapping the Claude Code CLI as a subprocess,
communicating through the SDK's streaming protocol.  The orchestrator sees
only the AgentAdapter interface; all Claude-specific concerns live here.

Key design decisions:

- **System CLI over bundled binary:** We use the user's installed ``claude``
  CLI (found via ``shutil.which``) rather than the SDK's bundled binary.
  The system CLI carries the user's login credentials (from ``claude login``
  or ANTHROPIC_API_KEY); the bundled one does not.

- **Environment scrubbing:** CLAUDECODE env vars are stripped before launching
  to prevent the SDK from detecting it's inside an existing Claude session,
  which would block nested agent invocations.

- **Resilient query (_resilient_query):** The SDK's message parser crashes on
  unknown message types (e.g. ``rate_limit_event``) because its async
  generator dies on the first MessageParseError.  Our wrapper accesses SDK
  internals to iterate raw JSON messages and parse them ourselves, silently
  skipping unrecognised types instead of aborting the entire session.

- **Message extraction (_extract_message_text):** Translates the SDK's typed
  message objects (AssistantMessage, ResultMessage, etc.) into human-readable
  Discord-friendly text with markdown formatting.

See specs/adapters/claude.md for the full behavioral specification.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from src.adapters.base import AgentAdapter, MessageCallback
from src.models import AgentOutput, AgentResult, TaskContext

# Import SDK types for isinstance checks (lazy, set in wait())
_sdk_types_loaded = False
_AssistantMessage = None
_ResultMessage = None
_UserMessage = None
_TextBlock = None
_ThinkingBlock = None
_ToolUseBlock = None
_ToolResultBlock = None


async def _resilient_query(prompt, options):
    """Wrap claude_agent_sdk.query() to survive MessageParseError.

    Why this exists: The SDK's ``query()`` function yields parsed messages from
    an async generator.  Internally it calls ``parse_message()`` on each raw
    JSON dict from the CLI subprocess.  When the CLI emits an unknown message
    type (like ``rate_limit_event``), ``parse_message`` raises
    MessageParseError -- and because Python async generators can't recover
    from mid-iteration exceptions, the entire stream dies.

    This wrapper reproduces the SDK's query setup but iterates the raw JSON
    dicts ourselves, calling ``parse_message`` in a try/except so we can skip
    unrecognised types and keep the session alive.  It's fragile (depends on
    SDK internals) but necessary until the SDK handles unknown types gracefully.
    """
    from claude_agent_sdk._internal.client import InternalClient
    from claude_agent_sdk._internal.message_parser import parse_message
    from claude_agent_sdk._errors import MessageParseError as _MPE
    from claude_agent_sdk.types import ClaudeAgentOptions
    import os, json
    from collections.abc import AsyncIterable

    os.environ["CLAUDE_CODE_ENTRYPOINT"] = "sdk-py"

    client = InternalClient()

    configured_options = options
    if options.can_use_tool:
        if isinstance(prompt, str):
            raise ValueError("can_use_tool requires streaming mode")
        if options.permission_prompt_tool_name:
            raise ValueError("can_use_tool and permission_prompt_tool_name are mutually exclusive")
        from dataclasses import replace
        configured_options = replace(options, permission_prompt_tool_name="stdio")

    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
    transport = SubprocessCLITransport(prompt=prompt, options=configured_options)
    await transport.connect()

    sdk_mcp_servers = {}
    if configured_options.mcp_servers and isinstance(configured_options.mcp_servers, dict):
        for name, config in configured_options.mcp_servers.items():
            if isinstance(config, dict) and config.get("type") == "sdk":
                sdk_mcp_servers[name] = config["instance"]

    from dataclasses import asdict
    agents_dict = None
    if configured_options.agents:
        agents_dict = {
            name: {k: v for k, v in asdict(agent_def).items() if v is not None}
            for name, agent_def in configured_options.agents.items()
        }

    hooks = (
        client._convert_hooks_to_internal_format(configured_options.hooks)
        if configured_options.hooks else None
    )

    from claude_agent_sdk._internal.query import Query
    query_obj = Query(
        transport=transport,
        is_streaming_mode=True,
        can_use_tool=configured_options.can_use_tool,
        hooks=hooks,
        sdk_mcp_servers=sdk_mcp_servers,
        agents=agents_dict,
    )

    try:
        await query_obj.start()
        await query_obj.initialize()

        if isinstance(prompt, str):
            user_message = {
                "type": "user",
                "session_id": "",
                "message": {"role": "user", "content": prompt},
                "parent_tool_use_id": None,
            }
            await transport.write(json.dumps(user_message) + "\n")
            await transport.end_input()
        elif isinstance(prompt, AsyncIterable) and query_obj._tg:
            query_obj._tg.start_soon(query_obj.stream_input, prompt)

        # Iterate raw dicts, parse ourselves, skip unknown message types
        async for data in query_obj.receive_messages():
            try:
                yield parse_message(data)
            except _MPE as e:
                msg_type = data.get("type", "unknown") if isinstance(data, dict) else "unknown"
                print(f"Claude adapter: skipping unrecognised message type '{msg_type}': {e}")
                continue
    finally:
        await query_obj.close()


@dataclass
class ClaudeAdapterConfig:
    """Configuration for the Claude Code agent adapter.

    Attributes:
        model: Model ID to pass to Claude Code.  Empty string means let the
            CLI pick its default (usually the latest Sonnet).
        permission_mode: Controls which tool calls require human approval.
            "acceptEdits" auto-approves file edits but prompts for shell
            commands; other modes are more or less permissive.
        allowed_tools: Whitelist of tool names the agent may use.  Defaults
            to the safe set of file and search tools.  The orchestrator may
            extend this per-task (e.g. adding "WebSearch").
    """

    model: str = ""  # Empty = let Claude Code pick the default model
    permission_mode: str = "acceptEdits"
    allowed_tools: list[str] = field(default_factory=lambda: [
        "Read", "Write", "Edit", "Bash", "Glob", "Grep",
    ])


class ClaudeAdapter(AgentAdapter):
    """AgentAdapter implementation that runs tasks via the Claude Code CLI.

    Each task gets a fresh SDK query (subprocess).  The adapter streams
    messages back through the on_message callback and collects the final
    result/token counts from the ResultMessage.
    """

    def __init__(self, config: ClaudeAdapterConfig | None = None, llm_logger=None):
        self._config = config or ClaudeAdapterConfig()
        self._task: TaskContext | None = None
        self._cancel_event = asyncio.Event()
        self._session_id: str | None = None
        self._llm_logger = llm_logger

    async def start(self, task: TaskContext) -> None:
        self._task = task
        self._cancel_event.clear()

    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:
        import time as _time
        _wait_start = _time.monotonic()
        try:
            # Strip Claude Code session markers to allow launching agent sessions
            import os
            for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
                os.environ.pop(var, None)

            import shutil
            from claude_agent_sdk import ClaudeAgentOptions
            from claude_agent_sdk.types import (
                AssistantMessage, ResultMessage, UserMessage,
                TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock,
            )
            global _sdk_types_loaded, _AssistantMessage, _ResultMessage, _UserMessage
            global _TextBlock, _ThinkingBlock, _ToolUseBlock, _ToolResultBlock
            _AssistantMessage = AssistantMessage
            _ResultMessage = ResultMessage
            _UserMessage = UserMessage
            _TextBlock = TextBlock
            _ThinkingBlock = ThinkingBlock
            _ToolUseBlock = ToolUseBlock
            _ToolResultBlock = ToolResultBlock
            _sdk_types_loaded = True

            # Prefer the system claude CLI over the SDK's bundled binary.
            # The bundled binary has no user credentials; the system one does
            # (either via `claude login` or ANTHROPIC_API_KEY).
            system_claude = shutil.which("claude")

            options = ClaudeAgentOptions(
                allowed_tools=self._config.allowed_tools,
                permission_mode=self._config.permission_mode,
                cwd=self._task.checkout_path or None,
                cli_path=system_claude,  # None → falls back to bundled binary
            )
            if self._config.model:
                options.model = self._config.model
            if self._task.mcp_servers:
                options.mcp_servers = self._task.mcp_servers

            summary_parts = []
            tokens_used = 0
            current_prompt = self._build_prompt()

            print(f"Claude adapter: starting query (session={self._session_id or 'new'}, "
                  f"prompt={len(current_prompt)} chars)")
            cli_error: str | None = None
            try:
                async for message in _resilient_query(prompt=current_prompt, options=options):
                    # Log only messages with meaningful subtypes to reduce noise
                    msg_subtype = getattr(message, "subtype", "")
                    if msg_subtype and msg_subtype not in ("", None):
                        msg_type = getattr(message, "type", "unknown")
                        print(f"Claude adapter message: type={msg_type} subtype={msg_subtype}")

                    if self._cancel_event.is_set():
                        output = AgentOutput(
                            result=AgentResult.FAILED,
                            summary="Cancelled",
                            error_message="Agent was stopped",
                        )
                        self._log_session(current_prompt, output, _wait_start, _time)
                        return output

                    # Capture session ID from init message.
                    # SystemMessage only has .subtype and .data (a raw dict);
                    # the session_id lives in .data, not as a top-level attribute.
                    if hasattr(message, "subtype") and message.subtype == "init":
                        data = getattr(message, "data", {})
                        self._session_id = (
                            data.get("session_id")
                            if isinstance(data, dict)
                            else getattr(message, "session_id", None)
                        )
                        print(f"Claude adapter: session started ({self._session_id})")

                    # Forward interesting messages to the callback
                    if on_message:
                        text = self._extract_message_text(message)
                        if text:
                            await on_message(text)

                    # Capture result and token usage from ResultMessage
                    if isinstance(message, ResultMessage):
                        # Check for error result BEFORE treating as success
                        if getattr(message, "is_error", False):
                            err_subtype = getattr(message, "subtype", "") or "unknown"
                            err_result = str(getattr(message, "result", "") or "")
                            cli_error = (
                                f"{err_subtype}: {err_result}".strip(": ")
                                or err_subtype
                            )
                            print(f"Claude adapter: CLI returned error result: {cli_error}")
                        else:
                            if message.result:
                                summary_parts.append(str(message.result))
                        usage = getattr(message, "usage", None)
                        if usage and isinstance(usage, dict):
                            tokens_used += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                    elif hasattr(message, "result") and message.result:
                        summary_parts.append(str(message.result))

            except Exception as e:
                import traceback
                error_msg = str(e)
                full_traceback = traceback.format_exc()
                print(f"Claude adapter error: {error_msg}")
                print(full_traceback)
                if "token" in error_msg.lower() or "quota" in error_msg.lower():
                    output = AgentOutput(
                        result=AgentResult.PAUSED_TOKENS,
                        error_message=error_msg,
                    )
                    self._log_session(current_prompt, output, _wait_start, _time)
                    return output
                output = AgentOutput(
                    result=AgentResult.FAILED,
                    error_message=f"{error_msg}\n{full_traceback}",
                )
                self._log_session(current_prompt, output, _wait_start, _time)
                return output

            # If the CLI reported an error result, propagate it as FAILED
            if cli_error:
                print(f"Claude adapter: query failed with CLI error: {cli_error}")
                output = AgentOutput(
                    result=AgentResult.FAILED,
                    error_message=cli_error,
                    tokens_used=tokens_used,
                )
                self._log_session(current_prompt, output, _wait_start, _time)
                return output

            print(f"Claude adapter: query completed, {len(summary_parts)} result parts, "
                  f"{tokens_used} tokens")

            # If the agent used 0 tokens and produced no meaningful output,
            # something went wrong (e.g. auth failure, rate limit, CLI crash).
            if tokens_used == 0 and not summary_parts:
                print("Claude adapter: 0 tokens and no output — treating as failure")
                output = AgentOutput(
                    result=AgentResult.FAILED,
                    error_message=(
                        "Agent session ended with 0 tokens and no output. "
                        "Possible causes: rate limit on subscription, "
                        "authentication failure, or Claude CLI crash. "
                        "Check `claude login` status and subscription limits."
                    ),
                    tokens_used=0,
                )
                self._log_session(current_prompt, output, _wait_start, _time)
                return output

            output = AgentOutput(
                result=AgentResult.COMPLETED,
                summary="\n".join(summary_parts) or "Completed",
                tokens_used=tokens_used,
            )
            self._log_session(current_prompt, output, _wait_start, _time)
            return output
        except ImportError as e:
            return AgentOutput(
                result=AgentResult.FAILED,
                error_message=f"Claude Agent SDK not available: {e}",
            )

    def _log_session(self, prompt: str, output: AgentOutput,
                      start: float, time_mod) -> None:
        """Log agent session to LLMLogger if available."""
        if not self._llm_logger:
            return
        duration_ms = int((time_mod.monotonic() - start) * 1000)
        task_id = self._task.task_id if self._task else ""
        self._llm_logger.log_agent_session(
            task_id=task_id,
            session_id=self._session_id,
            model=self._config.model or "(default)",
            prompt=prompt,
            config_summary={
                "allowed_tools": self._config.allowed_tools,
                "permission_mode": self._config.permission_mode,
                "cwd": self._task.checkout_path if self._task else "",
            },
            output=output,
            duration_ms=duration_ms,
        )

    async def stop(self) -> None:
        self._cancel_event.set()

    async def is_alive(self) -> bool:
        return self._task is not None and not self._cancel_event.is_set()

    def _extract_message_text(self, message) -> str | None:
        """Translate SDK message objects into Discord-friendly markdown text.

        The SDK emits typed messages (AssistantMessage with content blocks,
        UserMessage with tool results, ResultMessage with final output).
        This method formats each into concise, readable text suitable for
        streaming into a Discord thread -- truncating long content, rendering
        tool use as bold labels, and summarising costs/tokens.
        """
        if not _sdk_types_loaded:
            return None

        # AssistantMessage — Claude's response with content blocks
        if isinstance(message, _AssistantMessage):
            content = getattr(message, "content", None)
            if not content:
                return None
            parts = []
            for block in content:
                if isinstance(block, _ThinkingBlock):
                    thinking = getattr(block, "thinking", "")
                    if thinking:
                        # Truncate long thinking blocks
                        preview = thinking[:500]
                        if len(thinking) > 500:
                            preview += "..."
                        parts.append(f"*thinking:* {preview}")
                elif isinstance(block, _TextBlock):
                    text = getattr(block, "text", "")
                    if text:
                        parts.append(text)
                elif isinstance(block, _ToolUseBlock):
                    name = getattr(block, "name", "unknown")
                    inp = getattr(block, "input", {})
                    detail = ""
                    if name == "Bash" and isinstance(inp, dict):
                        cmd = inp.get("command", "")[:100]
                        detail = f": `{cmd}`" if cmd else ""
                    elif name in ("Read", "Write", "Edit", "Glob", "Grep") and isinstance(inp, dict):
                        path = inp.get("file_path", inp.get("path", inp.get("pattern", "")))
                        detail = f": `{path}`" if path else ""
                    parts.append(f"**[{name}{detail}]**")
                elif isinstance(block, _ToolResultBlock):
                    content_val = getattr(block, "content", None)
                    is_error = getattr(block, "is_error", False)
                    if content_val and isinstance(content_val, str):
                        prefix = "**Error:**" if is_error else ""
                        preview = content_val[:300]
                        if len(content_val) > 300:
                            preview += "..."
                        parts.append(f"{prefix}```\n{preview}\n```")
            return "\n".join(parts) if parts else None

        # UserMessage — tool results flowing back
        if isinstance(message, _UserMessage):
            result = getattr(message, "tool_use_result", None)
            if result and isinstance(result, dict):
                content_val = result.get("content", "")
                if content_val and isinstance(content_val, str) and len(content_val) < 300:
                    return f"```\n{content_val}\n```"
            return None

        # ResultMessage — final completion
        if isinstance(message, _ResultMessage):
            result = getattr(message, "result", None)
            cost = getattr(message, "total_cost_usd", None)
            usage = getattr(message, "usage", None)
            parts = []
            if result:
                parts.append(f"**Result:** {result}")
            if cost is not None:
                parts.append(f"Cost: ${cost:.4f}")
            if usage and isinstance(usage, dict):
                input_t = usage.get("input_tokens", 0)
                output_t = usage.get("output_tokens", 0)
                if input_t or output_t:
                    parts.append(f"Tokens: {input_t:,} in / {output_t:,} out")
            return "\n".join(parts) if parts else None

        return None

    def _build_prompt(self) -> str:
        """Assemble the full prompt from TaskContext fields.

        Combines the task description with optional acceptance criteria,
        test commands, and attached context into a single markdown-formatted
        prompt that the Claude Code agent receives as its initial instruction.
        """
        parts = [self._task.description]
        if self._task.acceptance_criteria:
            parts.append("\n## Acceptance Criteria")
            for c in self._task.acceptance_criteria:
                parts.append(f"- {c}")
        if self._task.test_commands:
            parts.append("\n## Test Commands")
            for cmd in self._task.test_commands:
                parts.append(f"- `{cmd}`")
        if self._task.attached_context:
            parts.append("\n## Additional Context")
            for ctx in self._task.attached_context:
                parts.append(f"- {ctx}")
        return "\n".join(parts)
