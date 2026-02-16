from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from src.adapters.base import AgentAdapter
from src.models import AgentOutput, AgentResult, TaskContext


@dataclass
class ClaudeAdapterConfig:
    model: str = "claude-sonnet-4-20250514"
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

    async def wait(self) -> AgentOutput:
        try:
            from claude_agent_sdk import query, ClaudeAgentOptions

            options = ClaudeAgentOptions(
                allowed_tools=self._config.allowed_tools,
                permission_mode=self._config.permission_mode,
                model=self._config.model,
                cwd=self._task.checkout_path or None,
            )
            if self._task.mcp_servers:
                options.mcp_servers = self._task.mcp_servers

            summary_parts = []
            tokens_used = 0

            async for message in query(
                prompt=self._build_prompt(),
                options=options,
            ):
                if self._cancel_event.is_set():
                    return AgentOutput(
                        result=AgentResult.FAILED,
                        summary="Cancelled",
                        error_message="Agent was stopped",
                    )

                # Capture session ID from init message
                if hasattr(message, "subtype") and message.subtype == "init":
                    self._session_id = getattr(message, "session_id", None)

                # Capture result
                if hasattr(message, "result"):
                    summary_parts.append(str(message.result))

            return AgentOutput(
                result=AgentResult.COMPLETED,
                summary="\n".join(summary_parts) or "Completed",
                tokens_used=tokens_used,
            )
        except Exception as e:
            error_msg = str(e)
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
                error_message=error_msg,
            )

    async def stop(self) -> None:
        self._cancel_event.set()

    async def is_alive(self) -> bool:
        return self._task is not None and not self._cancel_event.is_set()

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
