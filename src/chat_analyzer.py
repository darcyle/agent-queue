"""Background chat analyzer that watches project conversations and suggests actions.

The ChatAnalyzer subscribes to ``chat.message`` events on the EventBus,
buffers messages per channel, and periodically analyzes the conversation
using a local LLM (Ollama by default). When it spots something useful —
a question it can answer, a task that should be created, relevant context
the user might not know about, or a potential issue — it posts a suggestion
as a Discord embed with Accept/Dismiss buttons.

Design principles:
- **Minimal overhead**: runs on the local LLM to avoid burning Claude tokens.
- **Non-intrusive**: suggestions have accept/reject UI; dismissed ones don't repeat.
- **Project-scoped**: analysis happens per-project-channel with project context.

See the plan in plans/chat-analyzer-agent.md for the full design.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from src.config import ChatAnalyzerConfig
from src.database import Database
from src.event_bus import EventBus

logger = logging.getLogger(__name__)

# Maximum messages to keep in the per-channel buffer
MAX_BUFFER_SIZE = 50

# System prompt for the analyzer LLM
ANALYZER_SYSTEM_PROMPT = """\
You are a background conversation analyzer for a software development project management system.
You watch the conversation between a user and a bot in a project channel and look for opportunities
to proactively help.

Your job is to analyze the recent conversation and decide if you should make a suggestion.
You should ONLY suggest when you have high confidence that your suggestion would be genuinely useful.

Suggestion types:
- "answer": You can directly answer a question the user asked or is struggling with.
- "task": A task should be created based on what's being discussed (a bug to fix, a feature to build, etc.).
- "context": There's relevant project context (from notes, recent tasks, etc.) that the user might not know about.
- "warning": There's a potential issue worth flagging (conflicting tasks, known bugs related to the discussion, etc.).

You MUST respond with valid JSON only, no other text:
{
  "should_suggest": true/false,
  "suggestion": "Your suggestion text here (be concise and actionable)",
  "type": "answer|task|context|warning",
  "confidence": 0.0-1.0,
  "task_title": "optional - only for type=task, a short task title"
}

If the conversation is routine, administrative, or doesn't warrant intervention, set should_suggest to false.
Do NOT suggest things that are obvious or that the user/bot are already handling.
"""


@dataclass(slots=True)
class BufferedMessage:
    """Lightweight message stored in the per-channel buffer."""
    author: str
    content: str
    timestamp: float
    is_bot: bool


@dataclass(slots=True)
class AnalyzerSuggestion:
    """Parsed suggestion from the LLM."""
    should_suggest: bool
    suggestion: str
    type: str  # answer, task, context, warning
    confidence: float
    task_title: str = ""


class ChatAnalyzer:
    """Background service that analyzes project channel conversations.

    Instantiated by the orchestrator alongside the hook engine. Subscribes to
    ``chat.message`` events, buffers messages per channel, and periodically
    runs analysis using a local LLM provider.
    """

    def __init__(
        self,
        db: Database,
        bus: EventBus,
        config: ChatAnalyzerConfig,
    ):
        self._db = db
        self._bus = bus
        self._config = config

        # Per-channel message buffer: channel_id -> deque of BufferedMessage
        self._buffers: dict[int, deque[BufferedMessage]] = defaultdict(
            lambda: deque(maxlen=MAX_BUFFER_SIZE)
        )
        # Track which channel belongs to which project
        self._channel_projects: dict[int, str] = {}
        # Count of new (unanalyzed) messages per channel since last analysis
        self._new_message_counts: dict[int, int] = defaultdict(int)
        # Last analysis time per channel
        self._last_analysis: dict[int, float] = {}
        # The LLM provider instance (created lazily)
        self._provider = None
        # Background analysis task
        self._analysis_task: asyncio.Task | None = None
        # Notification callback — set by the bot after initialization
        self._notify: Any = None
        # Post-suggestion callback — set by the bot for Discord embed posting
        self._post_suggestion: Any = None

    def set_notify_callback(self, callback) -> None:
        """Set the callback for posting messages to Discord channels."""
        self._notify = callback

    def set_post_suggestion_callback(self, callback) -> None:
        """Set the callback for posting suggestion embeds with buttons."""
        self._post_suggestion = callback

    async def initialize(self) -> None:
        """Subscribe to events and start the background analysis loop."""
        if not self._config.enabled:
            return

        self._bus.subscribe("chat.message", self._on_message)
        self._analysis_task = asyncio.create_task(self._analysis_loop())
        logger.info(
            "Chat analyzer started (interval=%ds, model=%s)",
            self._config.interval_seconds,
            self._config.model,
        )

    async def shutdown(self) -> None:
        """Stop the background analysis loop."""
        if self._analysis_task and not self._analysis_task.done():
            self._analysis_task.cancel()
            try:
                await self._analysis_task
            except asyncio.CancelledError:
                pass
            self._analysis_task = None

    def _get_provider(self):
        """Lazily create the chat provider for analysis."""
        if self._provider is None:
            from src.config import ChatProviderConfig
            from src.chat_providers import create_chat_provider

            provider_config = ChatProviderConfig(
                provider=self._config.provider,
                model=self._config.model,
                base_url=self._config.base_url,
                keep_alive="5m",
            )
            self._provider = create_chat_provider(provider_config)
            if self._provider is None:
                logger.warning("Chat analyzer: could not create LLM provider")
        return self._provider

    async def _on_message(self, data: dict) -> None:
        """Handle a chat.message event by buffering the message."""
        channel_id = data.get("channel_id")
        project_id = data.get("project_id")
        if not channel_id or not project_id:
            return

        msg = BufferedMessage(
            author=data.get("author", "unknown"),
            content=data.get("content", ""),
            timestamp=data.get("timestamp", time.time()),
            is_bot=data.get("is_bot", False),
        )
        self._buffers[channel_id].append(msg)
        self._channel_projects[channel_id] = project_id
        self._new_message_counts[channel_id] += 1

    async def _analysis_loop(self) -> None:
        """Periodically analyze channels that have enough new messages."""
        while True:
            try:
                await asyncio.sleep(self._config.interval_seconds)
                await self._run_analysis_pass()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Chat analyzer loop error: %s", e, exc_info=True)

    async def _run_analysis_pass(self) -> None:
        """Check all channels and analyze those with enough new messages."""
        now = time.time()
        channels_to_analyze = []

        for channel_id, new_count in list(self._new_message_counts.items()):
            if new_count < self._config.min_messages_to_analyze:
                continue

            project_id = self._channel_projects.get(channel_id)
            if not project_id:
                continue

            # Check rate limit: max suggestions per hour
            recent_count = await self._db.count_recent_suggestions(
                project_id, now - 3600
            )
            if recent_count >= self._config.max_suggestions_per_hour:
                continue

            # Check cooldown after dismiss
            last_dismiss = await self._db.get_last_dismiss_time(
                project_id, channel_id
            )
            if last_dismiss and (now - last_dismiss) < self._config.cooldown_after_dismiss:
                continue

            channels_to_analyze.append((channel_id, project_id))

        for channel_id, project_id in channels_to_analyze:
            try:
                await self._analyze_channel(channel_id, project_id)
            except Exception as e:
                logger.error(
                    "Chat analyzer error for channel %d (project %s): %s",
                    channel_id, project_id, e,
                )

    async def _analyze_channel(self, channel_id: int, project_id: str) -> None:
        """Run LLM analysis on the recent messages in a channel."""
        provider = self._get_provider()
        if provider is None:
            return

        buffer = self._buffers.get(channel_id)
        if not buffer:
            return

        # Reset new message count
        self._new_message_counts[channel_id] = 0
        self._last_analysis[channel_id] = time.time()

        # Build the conversation context
        messages_text = self._format_messages(buffer)

        # Gather project context (notes, recent tasks)
        project_context = await self._gather_project_context(project_id)

        user_prompt = self._build_analysis_prompt(messages_text, project_context)

        # Call the LLM
        try:
            response = await provider.create_message(
                messages=[{"role": "user", "content": user_prompt}],
                system=ANALYZER_SYSTEM_PROMPT,
                max_tokens=512,
            )
        except Exception as e:
            logger.warning("Chat analyzer LLM call failed: %s", e)
            return

        # Parse the response
        suggestion = self._parse_response(response)
        if suggestion is None:
            return

        if not suggestion.should_suggest:
            return

        if suggestion.confidence < self._config.confidence_threshold:
            logger.debug(
                "Chat analyzer: suggestion below threshold (%.2f < %.2f)",
                suggestion.confidence, self._config.confidence_threshold,
            )
            return

        # Dedup check via hash
        suggestion_hash = hashlib.sha256(
            f"{project_id}:{suggestion.type}:{suggestion.suggestion}".encode()
        ).hexdigest()[:16]

        if await self._db.get_suggestion_hash_exists(project_id, suggestion_hash):
            logger.debug("Chat analyzer: duplicate suggestion skipped")
            return

        # Store the suggestion
        context_snapshot = json.dumps(
            [{"author": m.author, "content": m.content[:200]} for m in buffer],
            ensure_ascii=False,
        )
        suggestion_id = await self._db.create_chat_analyzer_suggestion(
            project_id=project_id,
            channel_id=channel_id,
            suggestion_type=suggestion.type,
            suggestion_text=suggestion.suggestion,
            suggestion_hash=suggestion_hash,
            context_snapshot=context_snapshot,
        )

        # Post to Discord
        if self._post_suggestion:
            try:
                await self._post_suggestion(
                    channel_id=channel_id,
                    project_id=project_id,
                    suggestion_id=suggestion_id,
                    suggestion_type=suggestion.type,
                    suggestion_text=suggestion.suggestion,
                    task_title=suggestion.task_title,
                    confidence=suggestion.confidence,
                )
            except Exception as e:
                logger.error("Chat analyzer: failed to post suggestion: %s", e)

        logger.info(
            "Chat analyzer: posted %s suggestion for project %s (confidence=%.2f)",
            suggestion.type, project_id, suggestion.confidence,
        )

    @staticmethod
    def _format_messages(buffer: deque[BufferedMessage]) -> str:
        """Format buffered messages into a readable conversation transcript."""
        lines = []
        for msg in buffer:
            prefix = "[BOT]" if msg.is_bot else f"[{msg.author}]"
            lines.append(f"{prefix} {msg.content}")
        return "\n".join(lines)

    async def _gather_project_context(self, project_id: str) -> str:
        """Gather project notes and recent tasks for context."""
        context_parts = []

        # Get recent tasks for the project
        try:
            tasks = await self._db.list_tasks(project_id=project_id)
            if tasks:
                recent = sorted(tasks, key=lambda t: t.id, reverse=True)[:5]
                task_lines = []
                for t in recent:
                    task_lines.append(f"- [{t.status.value}] {t.title}")
                context_parts.append(
                    "Recent tasks:\n" + "\n".join(task_lines)
                )
        except Exception as e:
            logger.debug("Chat analyzer: could not fetch tasks: %s", e)

        return "\n\n".join(context_parts) if context_parts else ""

    @staticmethod
    def _build_analysis_prompt(messages_text: str, project_context: str) -> str:
        """Build the user prompt for the analyzer LLM."""
        parts = ["Here is the recent conversation in a project channel:\n"]
        parts.append(messages_text)

        if project_context:
            parts.append("\n\nProject context:")
            parts.append(project_context)

        parts.append(
            "\n\nAnalyze this conversation and decide if you should make a suggestion. "
            "Respond with JSON only."
        )
        return "\n".join(parts)

    @staticmethod
    def _parse_response(response) -> AnalyzerSuggestion | None:
        """Parse the LLM response into an AnalyzerSuggestion."""
        text = ""
        for part in response.text_parts:
            text += part

        if not text.strip():
            return None

        # Try to extract JSON from the response (handle markdown code blocks)
        json_text = text.strip()
        if json_text.startswith("```"):
            # Strip markdown code block
            lines = json_text.split("\n")
            # Remove first and last lines (``` markers)
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            json_text = "\n".join(lines)

        try:
            data = json.loads(json_text)
        except json.JSONDecodeError:
            logger.debug("Chat analyzer: could not parse LLM response as JSON: %s", text[:200])
            return None

        valid_types = {"answer", "task", "context", "warning"}
        suggestion_type = data.get("type", "")
        if suggestion_type not in valid_types:
            suggestion_type = "context"  # fallback

        return AnalyzerSuggestion(
            should_suggest=bool(data.get("should_suggest", False)),
            suggestion=str(data.get("suggestion", "")),
            type=suggestion_type,
            confidence=float(data.get("confidence", 0.0)),
            task_title=str(data.get("task_title", "")),
        )
