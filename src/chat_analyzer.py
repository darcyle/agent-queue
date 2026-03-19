"""Background chat analyzer that watches project conversations and suggests actions.

The ChatAnalyzer subscribes to ``chat.message`` events on the EventBus,
buffers messages per channel, and periodically analyzes the conversation
using a local LLM (Ollama by default). When it spots something useful —
a question it can answer, a task that should be created, relevant context
the user might not know about, or a potential issue — it posts a suggestion
as a Discord embed with Accept/Dismiss buttons.

Integrations:
- **Memory system**: queries project profiles and semantic memory for richer
  context during analysis (architecture decisions, past tasks, conventions).
- **Chat agent tools**: can auto-execute high-confidence actions (create tasks,
  post answers) via the CommandHandler when auto-execution is enabled.
- **Chat history**: configurable window of recent messages with timestamps
  and author metadata for accurate conversation understanding.

Design principles:
- **Minimal overhead**: runs on the local LLM to avoid burning Claude tokens.
- **Non-intrusive**: suggestions have accept/reject UI; dismissed ones don't repeat.
- **Project-scoped**: analysis happens per-project-channel with project context.
- **Memory-aware**: leverages project memory for informed suggestions.
- **Traceable**: all auto-executed actions are logged and stored in the DB.

See the plan in plans/chat-analyzer-agent.md for the full design.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.config import ChatAnalyzerConfig
from src.database import Database
from src.event_bus import EventBus

if TYPE_CHECKING:
    from src.command_handler import CommandHandler
    from src.memory import MemoryManager

logger = logging.getLogger(__name__)

# Maximum messages to keep in the per-channel buffer
MAX_BUFFER_SIZE = 50

# System prompt for the analyzer LLM
ANALYZER_SYSTEM_PROMPT = """\
You are a project assistant analyzer. You observe chat conversations about software projects and decide if you can help.

Given the recent conversation, project context, and project memory (if available), analyze whether you have something genuinely useful to contribute.

Suggestion types:
- "answer": You can directly answer a question the user asked or is struggling with. Use memory/profile context to give informed answers.
- "task": A task should be created based on what's being discussed (a bug to fix, a feature to build, etc.).
- "context": There's relevant project context (from memory, profiles, notes, past tasks) that the user might not know about or that's relevant to their discussion.
- "warning": There's a potential issue worth flagging (conflicting tasks, known bugs related to the discussion, architectural concerns from the project profile, etc.).

When project memory or profile information is provided, USE IT to inform your suggestions:
- Reference specific past tasks or decisions when relevant
- Flag when current discussion contradicts established conventions
- Suggest approaches based on what worked (or didn't) in past tasks
- Surface relevant architecture decisions or patterns from the profile

Rules:
- Only suggest if you're confident the suggestion adds real value
- Don't suggest things the user is already doing or has already solved
- Don't repeat information that was already shared in the conversation
- Prefer actionable suggestions (create a task, here's the fix) over vague advice
- If the conversation is casual/social or you're unsure, respond with should_suggest: false
- When you have memory context, prefer "context" type for surfacing relevant past knowledge
- For "task" type, always include a clear task_title

Respond with JSON only:
{
  "should_suggest": true/false,
  "suggestion_type": "answer|task|context|warning",
  "suggestion_text": "Your suggestion here",
  "confidence": 0.0-1.0,
  "reasoning": "Why this is helpful (internal, not shown to user)",
  "task_title": "optional - only for type=task, a short task title",
  "auto_executable": false
}

Set "auto_executable" to true ONLY when ALL of these conditions are met:
- The action is routine and low-risk (e.g., creating a straightforward task from explicit user request)
- The suggestion is extremely high confidence (0.95+)
- The action is clearly what the user wants based on the conversation
- The action does NOT involve destructive operations or irreversible changes
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
    suggestion_text: str
    suggestion_type: str  # answer, task, context, warning
    confidence: float
    reasoning: str = ""
    task_title: str = ""
    auto_executable: bool = False


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
        data_dir: str = "",
        memory_manager: "MemoryManager | None" = None,
    ):
        self._db = db
        self._bus = bus
        self._config = config
        self._data_dir = data_dir or os.path.expanduser("~/.agent-queue")
        self._memory_manager = memory_manager

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
        # Auto-execution callback — set by the bot for direct action execution
        self._auto_execute: Any = None
        # Command handler for auto-execution — set by the bot
        self._command_handler: "CommandHandler | None" = None
        # Track auto-executions for rate limiting
        self._auto_execute_counts: dict[str, list[float]] = defaultdict(list)

    def set_notify_callback(self, callback) -> None:
        """Set the callback for posting messages to Discord channels."""
        self._notify = callback

    def set_post_suggestion_callback(self, callback) -> None:
        """Set the callback for posting suggestion embeds with buttons."""
        self._post_suggestion = callback

    def set_command_handler(self, handler: "CommandHandler") -> None:
        """Set the command handler for auto-executing actions."""
        self._command_handler = handler

    def set_auto_execute_callback(self, callback) -> None:
        """Set the callback for notifying about auto-executed actions."""
        self._auto_execute = callback

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
        channels_to_analyze = []

        for channel_id, new_count in list(self._new_message_counts.items()):
            if new_count < self._config.min_messages_to_analyze:
                continue

            project_id = self._channel_projects.get(channel_id)
            if not project_id:
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

    async def _should_suggest(
        self,
        suggestion: AnalyzerSuggestion,
        project_id: str,
        channel_id: int,
    ) -> bool:
        """Determine whether a parsed suggestion should actually be posted.

        Checks (in order):
        1. LLM said should_suggest=false → skip
        2. Confidence below threshold → skip
        3. Rate limit: too many suggestions in the last hour → skip
        4. Dedup: suggestion hash already exists → skip
        5. Cooldown: last suggestion for this channel was dismissed recently → skip
        """
        if not suggestion.should_suggest:
            return False

        if suggestion.confidence < self._config.confidence_threshold:
            logger.debug(
                "Chat analyzer: suggestion below threshold (%.2f < %.2f)",
                suggestion.confidence, self._config.confidence_threshold,
            )
            return False

        now = time.time()

        # Rate limit: max suggestions per hour for this project
        recent_count = await self._db.count_recent_suggestions(
            project_id, now - 3600
        )
        if recent_count >= self._config.max_suggestions_per_hour:
            logger.debug(
                "Chat analyzer: rate limit reached (%d/%d) for project %s",
                recent_count, self._config.max_suggestions_per_hour, project_id,
            )
            return False

        # Dedup: compute hash of type + first 100 chars of suggestion text
        suggestion_hash = self._compute_suggestion_hash(
            project_id, suggestion.suggestion_type, suggestion.suggestion_text
        )
        if await self._db.get_suggestion_hash_exists(project_id, suggestion_hash):
            logger.debug("Chat analyzer: duplicate suggestion skipped")
            return False

        # Cooldown after dismiss
        last_dismiss = await self._db.get_last_dismiss_time(project_id, channel_id)
        if last_dismiss and (now - last_dismiss) < self._config.cooldown_after_dismiss:
            logger.debug(
                "Chat analyzer: cooldown active for channel %d (%.0fs remaining)",
                channel_id,
                self._config.cooldown_after_dismiss - (now - last_dismiss),
            )
            return False

        return True

    @staticmethod
    def _compute_suggestion_hash(
        project_id: str, suggestion_type: str, suggestion_text: str
    ) -> str:
        """Compute a dedup hash from type + first 100 chars of suggestion text."""
        return hashlib.sha256(
            f"{project_id}:{suggestion_type}:{suggestion_text[:100]}".encode()
        ).hexdigest()[:16]

    async def _analyze_channel(self, channel_id: int, project_id: str) -> None:
        """Run LLM analysis on the recent messages in a channel.

        Builds a context payload (buffered messages, project summary, recent
        tasks, project notes), sends it to the LLM with the analyzer system
        prompt, parses the structured JSON response, and — if the suggestion
        passes all guards in ``_should_suggest()`` — stores it and posts a
        Discord embed.
        """
        provider = self._get_provider()
        if provider is None:
            return

        buffer = self._buffers.get(channel_id)
        if not buffer:
            return

        # Reset new message count before analysis
        self._new_message_counts[channel_id] = 0
        self._last_analysis[channel_id] = time.time()

        # Build the conversation context from the last N buffered messages
        messages_text = self._format_messages(buffer)

        # Gather project context (summary, recent tasks, notes, memory)
        project_context = await self._gather_project_context(
            project_id, conversation_text=messages_text
        )

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

        # Parse the structured JSON response
        suggestion = self._parse_response(response)
        if suggestion is None:
            return

        # Run all guard checks (confidence, rate limit, dedup, cooldown)
        if not await self._should_suggest(suggestion, project_id, channel_id):
            return

        # Compute hash for storage and dedup
        suggestion_hash = self._compute_suggestion_hash(
            project_id, suggestion.suggestion_type, suggestion.suggestion_text
        )

        # Store the suggestion with a snapshot of the conversation context
        context_snapshot = json.dumps(
            [{"author": m.author, "content": m.content[:200]} for m in buffer],
            ensure_ascii=False,
        )
        suggestion_id = await self._db.create_chat_analyzer_suggestion(
            project_id=project_id,
            channel_id=channel_id,
            suggestion_type=suggestion.suggestion_type,
            suggestion_text=suggestion.suggestion_text,
            suggestion_hash=suggestion_hash,
            context_snapshot=context_snapshot,
        )

        # Check if this suggestion should be auto-executed
        if await self._try_auto_execute(
            suggestion, project_id, channel_id, suggestion_id
        ):
            logger.info(
                "Chat analyzer: auto-executed %s action for project %s "
                "(confidence=%.2f)",
                suggestion.suggestion_type, project_id, suggestion.confidence,
            )
            return

        # Post to Discord via the bot's callback
        if self._post_suggestion:
            try:
                await self._post_suggestion(
                    channel_id=channel_id,
                    project_id=project_id,
                    suggestion_id=suggestion_id,
                    suggestion_type=suggestion.suggestion_type,
                    suggestion_text=suggestion.suggestion_text,
                    task_title=suggestion.task_title,
                    confidence=suggestion.confidence,
                )
            except Exception as e:
                logger.error("Chat analyzer: failed to post suggestion: %s", e)

        logger.info(
            "Chat analyzer: posted %s suggestion for project %s (confidence=%.2f)",
            suggestion.suggestion_type, project_id, suggestion.confidence,
        )

    def _format_messages(self, buffer: deque[BufferedMessage]) -> str:
        """Format buffered messages into a readable conversation transcript.

        Uses the configured chat_history_window to limit how many messages
        are included. When include_timestamps is enabled, each message gets
        a human-readable timestamp for temporal context.
        """
        window = self._config.chat_history_window
        messages = list(buffer)[-window:] if window > 0 else list(buffer)

        lines = []
        for msg in messages:
            prefix = "[BOT]" if msg.is_bot else f"[{msg.author}]"
            if self._config.include_timestamps:
                ts = time.strftime("%H:%M:%S", time.gmtime(msg.timestamp))
                lines.append(f"[{ts}] {prefix} {msg.content}")
            else:
                lines.append(f"{prefix} {msg.content}")
        return "\n".join(lines)

    async def _gather_project_context(
        self, project_id: str, conversation_text: str = ""
    ) -> str:
        """Gather project summary, notes, recent tasks, and memory context.

        When a MemoryManager is available and memory_integration is enabled,
        this method enriches the context with:
        - The project profile (architecture, conventions, decisions)
        - Semantic memory search results relevant to the current conversation
        """
        context_parts = []

        # Get project summary
        workspace = ""
        try:
            project = await self._db.get_project(project_id)
            if project:
                context_parts.append(
                    f"Project: {project.name} (id: {project.id}, "
                    f"status: {project.status.value})"
                )
        except Exception as e:
            logger.debug("Chat analyzer: could not fetch project: %s", e)

        # Resolve workspace path for memory operations
        try:
            workspace = await self._db.get_project_workspace_path(project_id)
        except Exception:
            workspace = ""

        # --- Memory system integration ---
        if (
            self._memory_manager
            and self._config.memory_integration
            and workspace
        ):
            # Include project profile for architectural context
            if self._config.include_profile:
                try:
                    profile = await self._memory_manager.get_profile(project_id)
                    if profile:
                        # Truncate to keep context manageable
                        max_profile = 2000
                        if len(profile) > max_profile:
                            profile = profile[:max_profile] + "\n\n[profile truncated]"
                        context_parts.append(
                            "## Project Profile (synthesized knowledge)\n" + profile
                        )
                except Exception as e:
                    logger.debug("Chat analyzer: could not fetch profile: %s", e)

            # Semantic memory search using conversation as query
            if conversation_text:
                try:
                    # Use the recent conversation as a semantic search query
                    # to find relevant past context
                    query = conversation_text[-500:]  # Last ~500 chars as query
                    results = await self._memory_manager.search(
                        project_id, workspace, query,
                        top_k=self._config.memory_search_top_k,
                    )
                    if results:
                        memory_lines = []
                        for mem in results:
                            source = os.path.basename(mem.get("source", "unknown"))
                            heading = mem.get("heading", "")
                            content = mem.get("content", "")
                            score = mem.get("score", 0)
                            if score < 0.3:
                                continue  # Skip low-relevance results
                            entry = f"- **{source}**"
                            if heading:
                                entry += f" ({heading})"
                            entry += f" [relevance: {score:.2f}]: {content[:300]}"
                            memory_lines.append(entry)
                        if memory_lines:
                            context_parts.append(
                                "## Relevant Memory (from past tasks, notes, docs)\n"
                                + "\n".join(memory_lines)
                            )
                except Exception as e:
                    logger.debug("Chat analyzer: memory search failed: %s", e)

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

        # Get project notes (titles and first lines for context)
        try:
            notes_dir = os.path.join(self._data_dir, "notes", project_id)
            if os.path.isdir(notes_dir):
                note_lines = []
                for fname in sorted(os.listdir(notes_dir)):
                    if not fname.endswith(".md"):
                        continue
                    fpath = os.path.join(notes_dir, fname)
                    try:
                        with open(fpath, "r") as f:
                            first_line = f.readline().strip()
                        title = first_line[2:].strip() if first_line.startswith("# ") else fname[:-3]
                        note_lines.append(f"- {title}")
                    except Exception:
                        note_lines.append(f"- {fname[:-3]}")
                if note_lines:
                    context_parts.append(
                        "Project notes:\n" + "\n".join(note_lines[:10])
                    )
        except Exception as e:
            logger.debug("Chat analyzer: could not read notes: %s", e)

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

    # ------------------------------------------------------------------
    # Auto-execution
    # ------------------------------------------------------------------

    def _can_auto_execute(
        self, suggestion: AnalyzerSuggestion, project_id: str
    ) -> bool:
        """Check whether a suggestion is eligible for auto-execution.

        Auto-execution requires ALL of:
        1. auto_execute_enabled in config
        2. Suggestion type in auto_execute_types whitelist
        3. LLM flagged auto_executable=true
        4. Confidence >= auto_execute_confidence threshold (default 0.9)
        5. Not rate-limited (auto_execute_max_per_hour)
        6. CommandHandler is available
        """
        if not self._config.auto_execute_enabled:
            return False
        if not self._command_handler:
            return False
        if not suggestion.auto_executable:
            return False
        if suggestion.confidence < self._config.auto_execute_confidence:
            return False

        # Check type whitelist
        allowed_types = self._config.auto_execute_types or []
        if suggestion.suggestion_type not in allowed_types:
            return False

        # Rate limit auto-executions
        now = time.time()
        recent = self._auto_execute_counts.get(project_id, [])
        # Prune old entries
        recent = [t for t in recent if (now - t) < 3600]
        self._auto_execute_counts[project_id] = recent
        if len(recent) >= self._config.auto_execute_max_per_hour:
            logger.debug(
                "Chat analyzer: auto-execute rate limit reached for %s",
                project_id,
            )
            return False

        return True

    async def _try_auto_execute(
        self,
        suggestion: AnalyzerSuggestion,
        project_id: str,
        channel_id: int,
        suggestion_id: int,
    ) -> bool:
        """Attempt to auto-execute a suggestion if it meets all criteria.

        Returns True if the action was auto-executed, False otherwise.
        Auto-executed actions are logged and the suggestion is marked as
        accepted in the database.
        """
        if not self._can_auto_execute(suggestion, project_id):
            return False

        handler = self._command_handler
        if not handler:
            return False

        result: dict = {}
        action_taken = ""

        try:
            if suggestion.suggestion_type == "task":
                title = suggestion.task_title or suggestion.suggestion_text[:80]
                result = await handler.execute("create_task", {
                    "project_id": project_id,
                    "title": title,
                    "description": suggestion.suggestion_text,
                })
                if "error" not in result:
                    task_id = result.get("task_id", "?")
                    action_taken = f"Auto-created task `{task_id}`: {title}"
                else:
                    logger.warning(
                        "Chat analyzer: auto-execute create_task failed: %s",
                        result["error"],
                    )
                    return False

            elif suggestion.suggestion_type == "answer":
                # For answers, we post via the notification callback
                action_taken = (
                    f"💡 **Auto-answer (from analyzer):**\n"
                    f"{suggestion.suggestion_text}"
                )

            else:
                # Other types (context, warning) are not auto-executed
                return False

        except Exception as e:
            logger.error("Chat analyzer: auto-execute failed: %s", e)
            return False

        # Mark suggestion as accepted in DB
        try:
            await self._db.resolve_chat_analyzer_suggestion(
                suggestion_id, "auto_executed"
            )
        except Exception as e:
            logger.warning("Chat analyzer: could not mark auto-executed: %s", e)

        # Track for rate limiting
        self._auto_execute_counts[project_id].append(time.time())

        # Notify via callback (post to Discord channel)
        if self._auto_execute and action_taken:
            try:
                await self._auto_execute(
                    channel_id=channel_id,
                    project_id=project_id,
                    suggestion_id=suggestion_id,
                    action_text=action_taken,
                    suggestion_type=suggestion.suggestion_type,
                    confidence=suggestion.confidence,
                )
            except Exception as e:
                logger.error(
                    "Chat analyzer: auto-execute notification failed: %s", e
                )

        logger.info(
            "Chat analyzer: auto-executed %s for project %s: %s",
            suggestion.suggestion_type, project_id, action_taken[:100],
        )
        return True

    @staticmethod
    def _parse_response(response) -> AnalyzerSuggestion | None:
        """Parse the LLM response into an AnalyzerSuggestion.

        Handles common LLM response quirks:
        - Markdown code blocks wrapping the JSON
        - Leading/trailing whitespace or text around the JSON object
        - Missing or invalid fields (falls back to safe defaults)

        Returns None if the response cannot be parsed as valid JSON at all.
        """
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

        # Try to find a JSON object if there's surrounding text
        if not json_text.startswith("{"):
            start = json_text.find("{")
            end = json_text.rfind("}")
            if start != -1 and end != -1 and end > start:
                json_text = json_text[start:end + 1]

        try:
            data = json.loads(json_text)
        except json.JSONDecodeError:
            logger.debug(
                "Chat analyzer: could not parse LLM response as JSON: %s",
                text[:200],
            )
            return None

        valid_types = {"answer", "task", "context", "warning"}
        # Accept both "suggestion_type" (spec) and "type" (legacy) field names
        suggestion_type = data.get("suggestion_type") or data.get("type", "")
        if suggestion_type not in valid_types:
            suggestion_type = "context"  # fallback

        # Accept both "suggestion_text" (spec) and "suggestion" (legacy)
        suggestion_text = str(
            data.get("suggestion_text") or data.get("suggestion", "")
        )

        try:
            confidence = float(data.get("confidence", 0.0))
            # Clamp to valid range
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.0

        return AnalyzerSuggestion(
            should_suggest=bool(data.get("should_suggest", False)),
            suggestion_text=suggestion_text,
            suggestion_type=suggestion_type,
            confidence=confidence,
            reasoning=str(data.get("reasoning", "")),
            task_title=str(data.get("task_title", "")),
            auto_executable=bool(data.get("auto_executable", False)),
        )
