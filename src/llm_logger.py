"""LLM interaction logging for prompt optimization and debugging.

Writes JSONL entries to date-organized files under ``logs/llm/``.  Two log
streams are maintained:

- ``chat_provider.jsonl`` — every ``ChatProvider.create_message()`` call
  (chat bot, hooks, plan parsing, summarization).
- ``claude_agent.jsonl`` — Claude Code agent task sessions (one entry per
  ``adapter.wait()`` completion).

Log files are append-only JSONL (one JSON object per line).  Retention is
configurable; ``cleanup_old_logs()`` removes date directories older than
``retention_days``.

All writes are synchronous (single ``json.dumps`` + file append) which is
fast enough for the expected call volume and avoids async complexity.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from typing import Any


class LLMLogger:
    """Append-only JSONL logger for LLM interactions."""

    def __init__(self, base_dir: str = "logs/llm", enabled: bool = True,
                 retention_days: int = 30):
        self._base_dir = base_dir
        self._enabled = enabled
        self._retention_days = retention_days

    def log_chat_provider_call(
        self,
        *,
        caller: str,
        model: str,
        provider: str,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
        response: Any = None,
        error: str | None = None,
        duration_ms: int = 0,
    ) -> None:
        """Log a ChatProvider.create_message() call."""
        if not self._enabled:
            return

        # Extract tool names only (schemas are large and static)
        tool_names = [t.get("name", "") for t in tools] if tools else []

        # Build response summary
        resp_summary: dict[str, Any] = {}
        if response is not None:
            resp_summary["text_parts"] = getattr(response, "text_parts", [])
            tool_uses = getattr(response, "tool_uses", [])
            if tool_uses:
                resp_summary["tool_uses"] = [
                    {
                        "name": getattr(tu, "name", ""),
                        "input_keys": list(getattr(tu, "input", {}).keys())
                        if isinstance(getattr(tu, "input", None), dict)
                        else [],
                    }
                    for tu in tool_uses
                ]

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "caller": caller,
            "model": model,
            "provider": provider,
            "duration_ms": duration_ms,
            "input": {
                "system_prompt_length": len(system),
                "message_count": len(messages),
                "messages": self._summarize_messages(messages),
                "tool_names": tool_names,
                "max_tokens": max_tokens,
            },
            "output": resp_summary,
            "error": error,
        }

        self._append("chat_provider.jsonl", entry)

    def log_agent_session(
        self,
        *,
        task_id: str,
        session_id: str | None = None,
        model: str = "",
        prompt: str = "",
        config_summary: dict | None = None,
        output: Any = None,
        duration_ms: int = 0,
    ) -> None:
        """Log a Claude Code agent task session."""
        if not self._enabled:
            return

        output_summary: dict[str, Any] = {}
        if output is not None:
            output_summary["result"] = str(getattr(output, "result", ""))
            output_summary["summary"] = getattr(output, "summary", "")
            output_summary["tokens_used"] = getattr(output, "tokens_used", 0)
            output_summary["files_changed"] = getattr(output, "files_changed", [])
            error_msg = getattr(output, "error_message", "")
            if error_msg:
                output_summary["error"] = error_msg[:500]

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_id": task_id,
            "session_id": session_id,
            "model": model,
            "duration_ms": duration_ms,
            "input": {
                "prompt_length": len(prompt),
                "prompt_preview": prompt[:300],
                **(config_summary or {}),
            },
            "output": output_summary,
        }

        self._append("claude_agent.jsonl", entry)

    def cleanup_old_logs(self) -> int:
        """Delete date directories older than retention_days.

        Returns the number of directories removed.
        """
        if not os.path.isdir(self._base_dir):
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        cutoff_str = cutoff.strftime("%Y-%m-%d")
        removed = 0

        for name in os.listdir(self._base_dir):
            dir_path = os.path.join(self._base_dir, name)
            if not os.path.isdir(dir_path):
                continue
            # Only consider directories that look like date folders (YYYY-MM-DD)
            if len(name) == 10 and name < cutoff_str:
                try:
                    shutil.rmtree(dir_path)
                    removed += 1
                except OSError as e:
                    print(f"LLMLogger: failed to remove {dir_path}: {e}")

        return removed

    def _append(self, filename: str, entry: dict) -> None:
        """Append a single JSON line to a date-organized file."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        dir_path = os.path.join(self._base_dir, today)
        os.makedirs(dir_path, exist_ok=True)

        file_path = os.path.join(dir_path, filename)
        line = json.dumps(entry, default=str, ensure_ascii=False) + "\n"

        with open(file_path, "a", encoding="utf-8") as f:
            f.write(line)

    @staticmethod
    def _summarize_messages(messages: list[dict]) -> list[dict]:
        """Create a compact summary of messages for logging.

        Captures role and content length/preview rather than full content
        to keep log entries reasonable in size.
        """
        summaries = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, str):
                summaries.append({
                    "role": role,
                    "content_length": len(content),
                    "content_preview": content[:150],
                })
            elif isinstance(content, list):
                # Tool results or multi-part content
                summaries.append({
                    "role": role,
                    "content_type": "list",
                    "content_count": len(content),
                })
            else:
                summaries.append({
                    "role": role,
                    "content_type": type(content).__name__,
                })
        return summaries
