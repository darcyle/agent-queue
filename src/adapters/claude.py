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


@dataclass
class ClaudeAdapterConfig:
    model: str = ""  # Empty = let Claude Code pick the default model
    permission_mode: str = "acceptEdits"
    allowed_tools: list[str] = field(default_factory=lambda: [
        "Read", "Write", "Edit", "Bash", "Glob", "Grep",
    ])


class ClaudeAdapter(AgentAdapter):
    def __init__(self, config: ClaudeAdapterConfig | None = None):
        self._config = config or ClaudeAdapterConfig()
        self._task: TaskContext | None = None
        self._cancel_event = asyncio.Event()
        self._session_id: str | None = None

    async def start(self, task: TaskContext) -> None:
        self._task = task
        self._cancel_event.clear()

    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:
        try:
            # Strip Claude Code session markers to allow launching agent sessions
            import os
            for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
                os.environ.pop(var, None)

            from claude_agent_sdk import query, ClaudeAgentOptions
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

            options = ClaudeAgentOptions(
                allowed_tools=self._config.allowed_tools,
                permission_mode=self._config.permission_mode,
                cwd=self._task.checkout_path or None,
            )
            if self._config.model:
                options.model = self._config.model
            if self._task.mcp_servers:
                options.mcp_servers = self._task.mcp_servers

            summary_parts = []
            tokens_used = 0

            print(f"Claude adapter: starting query with prompt ({len(self._build_prompt())} chars)")
            async for message in query(
                prompt=self._build_prompt(),
                options=options,
            ):
                # Log every message type for debugging
                msg_type = getattr(message, "type", "unknown")
                msg_subtype = getattr(message, "subtype", "")
                print(f"Claude adapter message: type={msg_type} subtype={msg_subtype}")

                if self._cancel_event.is_set():
                    return AgentOutput(
                        result=AgentResult.FAILED,
                        summary="Cancelled",
                        error_message="Agent was stopped",
                    )

                # Capture session ID from init message
                if hasattr(message, "subtype") and message.subtype == "init":
                    self._session_id = getattr(message, "session_id", None)

                # Check for error messages from the SDK
                if hasattr(message, "is_error") and message.is_error:
                    error_text = getattr(message, "error", str(message))
                    print(f"Claude adapter: SDK reported error: {error_text}")

                # Forward interesting messages to the callback
                if on_message:
                    text = self._extract_message_text(message)
                    if text:
                        await on_message(text)
                    else:
                        # Debug: log unhandled message structure
                        print(f"Claude adapter: unhandled message: {repr(message)[:300]}")

                # Capture result and token usage from ResultMessage
                if isinstance(message, ResultMessage):
                    if message.result:
                        summary_parts.append(str(message.result))
                    usage = getattr(message, "usage", None)
                    if usage and isinstance(usage, dict):
                        tokens_used = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                elif hasattr(message, "result") and message.result:
                    summary_parts.append(str(message.result))

            print(f"Claude adapter: query completed, {len(summary_parts)} result parts")

            return AgentOutput(
                result=AgentResult.COMPLETED,
                summary="\n".join(summary_parts) or "Completed",
                tokens_used=tokens_used,
            )
        except Exception as e:
            import traceback
            error_msg = str(e)
            full_traceback = traceback.format_exc()
            print(f"Claude adapter error: {error_msg}")
            print(full_traceback)

            if "rate" in error_msg.lower() or "429" in error_msg:
                return AgentOutput(
                    result=AgentResult.PAUSED_RATE_LIMIT,
                    error_message=error_msg,
                )
            if "token" in error_msg.lower() or "quota" in error_msg.lower():
                return AgentOutput(
                    result=AgentResult.PAUSED_TOKENS,
                    error_message=error_msg,
                )
            return AgentOutput(
                result=AgentResult.FAILED,
                error_message=f"{error_msg}\n{full_traceback}",
            )

    async def stop(self) -> None:
        self._cancel_event.set()

    async def is_alive(self) -> bool:
        return self._task is not None and not self._cancel_event.is_set()

    def _extract_message_text(self, message) -> str | None:
        """Extract a human-readable string from a claude_agent_sdk message."""
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
