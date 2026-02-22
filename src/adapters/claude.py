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

            import shutil
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
                async for message in query(prompt=current_prompt, options=options):
                    # Log only messages with meaningful subtypes to reduce noise
                    msg_subtype = getattr(message, "subtype", "")
                    if msg_subtype and msg_subtype not in ("", None):
                        msg_type = getattr(message, "type", "unknown")
                        print(f"Claude adapter message: type={msg_type} subtype={msg_subtype}")

                    if self._cancel_event.is_set():
                        return AgentOutput(
                            result=AgentResult.FAILED,
                            summary="Cancelled",
                            error_message="Agent was stopped",
                        )

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
                # The SDK throws MessageParseError for unrecognised message types
                # like rate_limit_event. Claude Code handles rate limiting internally
                # so these don't actually disrupt the work — just log a warning and
                # let the orchestrator decide what to do with whatever results we got.
                import traceback
                error_msg = str(e)
                is_rate_limit = (
                    "rate_limit" in error_msg.lower()
                    or "rate limit" in error_msg.lower()
                    or "429" in error_msg
                )
                if is_rate_limit:
                    print(f"Claude adapter WARNING: rate_limit_event from SDK (non-fatal): {error_msg}")
                else:
                    # Non-rate-limit errors are real failures
                    full_traceback = traceback.format_exc()
                    print(f"Claude adapter error: {error_msg}")
                    print(full_traceback)
                    if "token" in error_msg.lower() or "quota" in error_msg.lower():
                        return AgentOutput(
                            result=AgentResult.PAUSED_TOKENS,
                            error_message=error_msg,
                        )
                    return AgentOutput(
                        result=AgentResult.FAILED,
                        error_message=f"{error_msg}\n{full_traceback}",
                    )

            # If the CLI reported an error result, propagate it as FAILED
            if cli_error:
                print(f"Claude adapter: query failed with CLI error: {cli_error}")
                return AgentOutput(
                    result=AgentResult.FAILED,
                    error_message=cli_error,
                    tokens_used=tokens_used,
                )

            print(f"Claude adapter: query completed, {len(summary_parts)} result parts")
            return AgentOutput(
                result=AgentResult.COMPLETED,
                summary="\n".join(summary_parts) or "Completed",
                tokens_used=tokens_used,
            )
        except ImportError as e:
            return AgentOutput(
                result=AgentResult.FAILED,
                error_message=f"Claude Agent SDK not available: {e}",
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
