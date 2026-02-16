from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from src.adapters.base import AgentAdapter, MessageCallback
from src.models import AgentOutput, AgentResult, TaskContext


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

                # Capture result
                if hasattr(message, "result"):
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
        # Assistant text content
        if hasattr(message, "type"):
            msg_type = message.type

            # Assistant messages with text content
            if msg_type == "assistant" and hasattr(message, "content"):
                parts = []
                for block in message.content:
                    if hasattr(block, "type"):
                        if block.type == "text" and hasattr(block, "text"):
                            parts.append(block.text)
                        elif block.type == "tool_use" and hasattr(block, "name"):
                            parts.append(f"[using tool: {block.name}]")
                return "\n".join(parts) if parts else None

            # Result message
            if msg_type == "result" and hasattr(message, "result"):
                return f"**Result:** {message.result}"

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
