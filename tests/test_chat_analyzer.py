"""Tests for the ChatAnalyzer service and its integrations.

Tests cover:
- Message buffering and formatting with timestamps
- Memory system integration in context gathering
- Auto-execution logic and rate limiting
- Guard checks (confidence, rate limit, dedup, cooldown)
- Config validation for new fields
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.chat_analyzer import (
    AnalyzerSuggestion,
    BufferedMessage,
    ChatAnalyzer,
    MAX_BUFFER_SIZE,
)
from src.config import ChatAnalyzerConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config():
    """Default ChatAnalyzerConfig for testing."""
    return ChatAnalyzerConfig(
        enabled=True,
        interval_seconds=60,
        min_messages_to_analyze=2,
        confidence_threshold=0.7,
        max_suggestions_per_hour=5,
        chat_history_window=20,
        include_timestamps=True,
        memory_integration=True,
        memory_search_top_k=3,
        include_profile=True,
        auto_execute_enabled=False,
    )


@pytest.fixture
def mock_db():
    """Mock Database instance."""
    db = AsyncMock()
    db.get_project = AsyncMock(return_value=MagicMock(
        name="test-proj", id="test-proj", status=MagicMock(value="active"),
    ))
    db.list_tasks = AsyncMock(return_value=[])
    db.get_project_workspace_path = AsyncMock(return_value="/tmp/workspace")
    db.count_recent_suggestions = AsyncMock(return_value=0)
    db.get_suggestion_hash_exists = AsyncMock(return_value=False)
    db.get_last_dismiss_time = AsyncMock(return_value=None)
    db.create_chat_analyzer_suggestion = AsyncMock(return_value=1)
    db.resolve_chat_analyzer_suggestion = AsyncMock()
    return db


@pytest.fixture
def mock_bus():
    """Mock EventBus."""
    bus = MagicMock()
    bus.subscribe = MagicMock()
    return bus


@pytest.fixture
def mock_memory_manager():
    """Mock MemoryManager."""
    mm = AsyncMock()
    mm.get_profile = AsyncMock(return_value="# Test Project\nA test project with REST API architecture.")
    mm.search = AsyncMock(return_value=[
        {
            "source": "/tmp/memory/tasks/task-1.md",
            "heading": "Task: Implement auth",
            "content": "Added JWT authentication to the API layer.",
            "score": 0.85,
        },
    ])
    return mm


@pytest.fixture
def analyzer(config, mock_db, mock_bus, mock_memory_manager):
    """ChatAnalyzer instance with mocked dependencies."""
    return ChatAnalyzer(
        db=mock_db,
        bus=mock_bus,
        config=config,
        data_dir="/tmp/test-data",
        memory_manager=mock_memory_manager,
    )


# ---------------------------------------------------------------------------
# Message formatting tests
# ---------------------------------------------------------------------------


class TestMessageFormatting:
    def test_format_messages_with_timestamps(self, analyzer):
        """Messages should include timestamps when configured."""
        buffer = deque([
            BufferedMessage("alice", "hello", 1710000000.0, False),
            BufferedMessage("bob", "hi there", 1710000060.0, False),
        ], maxlen=MAX_BUFFER_SIZE)

        result = analyzer._format_messages(buffer)
        assert "[" in result  # timestamp brackets
        assert "[alice]" in result
        assert "[bob]" in result
        assert "hello" in result
        assert "hi there" in result

    def test_format_messages_without_timestamps(self, analyzer):
        """When include_timestamps is False, no timestamps in output."""
        analyzer._config.include_timestamps = False
        buffer = deque([
            BufferedMessage("alice", "hello", 1710000000.0, False),
        ], maxlen=MAX_BUFFER_SIZE)

        result = analyzer._format_messages(buffer)
        assert "[alice] hello" in result
        # Should not have time-formatted prefix
        lines = result.strip().split("\n")
        assert lines[0] == "[alice] hello"

    def test_format_messages_respects_window(self, analyzer):
        """Only the last N messages should be included based on window."""
        analyzer._config.chat_history_window = 3
        buffer = deque(maxlen=MAX_BUFFER_SIZE)
        for i in range(10):
            buffer.append(BufferedMessage(f"user{i}", f"msg{i}", float(i), False))

        result = analyzer._format_messages(buffer)
        lines = [l for l in result.strip().split("\n") if l]
        assert len(lines) == 3
        assert "msg7" in result
        assert "msg8" in result
        assert "msg9" in result
        assert "msg0" not in result

    def test_format_messages_bot_prefix(self, analyzer):
        """Bot messages should use [BOT] prefix."""
        buffer = deque([
            BufferedMessage("AgentQueue", "I created the task", 1710000000.0, True),
        ], maxlen=MAX_BUFFER_SIZE)

        result = analyzer._format_messages(buffer)
        assert "[BOT]" in result


# ---------------------------------------------------------------------------
# Memory integration tests
# ---------------------------------------------------------------------------


class TestMemoryIntegration:
    @pytest.mark.asyncio
    async def test_gather_context_includes_profile(self, analyzer, mock_memory_manager):
        """When memory integration is enabled, profile should be included."""
        context = await analyzer._gather_project_context("test-proj", "some conversation")
        assert "Project Profile" in context
        assert "REST API architecture" in context
        mock_memory_manager.get_profile.assert_called_once_with("test-proj")

    @pytest.mark.asyncio
    async def test_gather_context_includes_memory_search(self, analyzer, mock_memory_manager):
        """Memory search results should appear in context."""
        context = await analyzer._gather_project_context("test-proj", "authentication question")
        assert "Relevant Memory" in context
        assert "JWT authentication" in context
        mock_memory_manager.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_gather_context_without_memory(self, analyzer):
        """When memory_manager is None, should still gather basic context."""
        analyzer._memory_manager = None
        context = await analyzer._gather_project_context("test-proj")
        assert "Project:" in context
        assert "Project Profile" not in context

    @pytest.mark.asyncio
    async def test_gather_context_memory_disabled(self, analyzer):
        """When memory_integration is False, skip memory even if manager exists."""
        analyzer._config.memory_integration = False
        context = await analyzer._gather_project_context("test-proj", "some text")
        assert "Project Profile" not in context
        assert "Relevant Memory" not in context

    @pytest.mark.asyncio
    async def test_gather_context_low_relevance_filtered(self, analyzer, mock_memory_manager):
        """Memory results below 0.3 relevance should be filtered out."""
        mock_memory_manager.search = AsyncMock(return_value=[
            {"source": "low.md", "heading": "", "content": "irrelevant", "score": 0.1},
        ])
        context = await analyzer._gather_project_context("test-proj", "query")
        assert "irrelevant" not in context

    @pytest.mark.asyncio
    async def test_gather_context_profile_truncated(self, analyzer, mock_memory_manager):
        """Long profiles should be truncated to keep context manageable."""
        mock_memory_manager.get_profile = AsyncMock(return_value="x" * 5000)
        context = await analyzer._gather_project_context("test-proj", "query")
        assert "[profile truncated]" in context


# ---------------------------------------------------------------------------
# Auto-execution tests
# ---------------------------------------------------------------------------


class TestAutoExecution:
    @pytest.fixture
    def auto_config(self, config):
        """Config with auto-execution enabled."""
        config.auto_execute_enabled = True
        config.auto_execute_types = ["task", "answer"]
        config.auto_execute_confidence = 0.9
        config.auto_execute_max_per_hour = 2
        return config

    def test_can_auto_execute_disabled(self, analyzer):
        """Auto-execute should be rejected when disabled."""
        suggestion = AnalyzerSuggestion(
            should_suggest=True, suggestion_text="test",
            suggestion_type="task", confidence=0.95,
            auto_executable=True,
        )
        assert not analyzer._can_auto_execute(suggestion, "test-proj")

    def test_can_auto_execute_enabled(self, analyzer, auto_config):
        """Auto-execute should pass when all conditions met."""
        analyzer._config = auto_config
        analyzer._command_handler = MagicMock()
        suggestion = AnalyzerSuggestion(
            should_suggest=True, suggestion_text="test",
            suggestion_type="task", confidence=0.95,
            auto_executable=True,
        )
        assert analyzer._can_auto_execute(suggestion, "test-proj")

    def test_can_auto_execute_low_confidence(self, analyzer, auto_config):
        """Auto-execute should be rejected when confidence is below threshold."""
        analyzer._config = auto_config
        analyzer._command_handler = MagicMock()
        suggestion = AnalyzerSuggestion(
            should_suggest=True, suggestion_text="test",
            suggestion_type="task", confidence=0.85,
            auto_executable=True,
        )
        assert not analyzer._can_auto_execute(suggestion, "test-proj")

    def test_can_auto_execute_wrong_type(self, analyzer, auto_config):
        """Auto-execute should be rejected for types not in whitelist."""
        analyzer._config = auto_config
        analyzer._command_handler = MagicMock()
        suggestion = AnalyzerSuggestion(
            should_suggest=True, suggestion_text="test",
            suggestion_type="warning", confidence=0.95,
            auto_executable=True,
        )
        assert not analyzer._can_auto_execute(suggestion, "test-proj")

    def test_can_auto_execute_not_flagged(self, analyzer, auto_config):
        """Auto-execute should be rejected when LLM didn't flag it."""
        analyzer._config = auto_config
        analyzer._command_handler = MagicMock()
        suggestion = AnalyzerSuggestion(
            should_suggest=True, suggestion_text="test",
            suggestion_type="task", confidence=0.95,
            auto_executable=False,
        )
        assert not analyzer._can_auto_execute(suggestion, "test-proj")

    def test_can_auto_execute_rate_limited(self, analyzer, auto_config):
        """Auto-execute should be rejected when rate limit reached."""
        analyzer._config = auto_config
        analyzer._command_handler = MagicMock()
        # Simulate 2 recent auto-executions
        now = time.time()
        analyzer._auto_execute_counts["test-proj"] = [now - 100, now - 50]

        suggestion = AnalyzerSuggestion(
            should_suggest=True, suggestion_text="test",
            suggestion_type="task", confidence=0.95,
            auto_executable=True,
        )
        assert not analyzer._can_auto_execute(suggestion, "test-proj")

    def test_can_auto_execute_no_handler(self, analyzer, auto_config):
        """Auto-execute should be rejected when no command handler set."""
        analyzer._config = auto_config
        analyzer._command_handler = None
        suggestion = AnalyzerSuggestion(
            should_suggest=True, suggestion_text="test",
            suggestion_type="task", confidence=0.95,
            auto_executable=True,
        )
        assert not analyzer._can_auto_execute(suggestion, "test-proj")

    @pytest.mark.asyncio
    async def test_try_auto_execute_task(self, analyzer, auto_config, mock_db):
        """Auto-executing a task should call create_task via handler."""
        analyzer._config = auto_config
        handler = AsyncMock()
        handler.execute = AsyncMock(return_value={"task_id": "new-task-1"})
        analyzer._command_handler = handler
        analyzer._auto_execute = AsyncMock()

        suggestion = AnalyzerSuggestion(
            should_suggest=True,
            suggestion_text="Fix the auth bug",
            suggestion_type="task",
            confidence=0.95,
            task_title="Fix auth bug",
            auto_executable=True,
        )

        result = await analyzer._try_auto_execute(suggestion, "test-proj", 123, 1)
        assert result is True
        handler.execute.assert_called_once_with("create_task", {
            "project_id": "test-proj",
            "title": "Fix auth bug",
            "description": "Fix the auth bug",
        })
        mock_db.resolve_chat_analyzer_suggestion.assert_called_once_with(1, "auto_executed")

    @pytest.mark.asyncio
    async def test_try_auto_execute_answer(self, analyzer, auto_config):
        """Auto-executing an answer should prepare notification text."""
        analyzer._config = auto_config
        handler = AsyncMock()
        analyzer._command_handler = handler
        analyzer._auto_execute = AsyncMock()

        suggestion = AnalyzerSuggestion(
            should_suggest=True,
            suggestion_text="The API endpoint is /api/v2/users",
            suggestion_type="answer",
            confidence=0.95,
            auto_executable=True,
        )

        result = await analyzer._try_auto_execute(suggestion, "test-proj", 123, 2)
        assert result is True
        analyzer._auto_execute.assert_called_once()
        call_kwargs = analyzer._auto_execute.call_args
        assert "Auto-answer" in call_kwargs.kwargs.get("action_text", "") or \
               "Auto-answer" in (call_kwargs[1].get("action_text", "") if len(call_kwargs) > 1 else "")


# ---------------------------------------------------------------------------
# Suggestion parsing tests
# ---------------------------------------------------------------------------


class TestSuggestionParsing:
    def test_parse_with_auto_executable(self):
        """Parser should extract auto_executable field."""
        mock_response = MagicMock()
        mock_response.text_parts = [
            '{"should_suggest": true, "suggestion_type": "task", '
            '"suggestion_text": "Create a migration", "confidence": 0.95, '
            '"reasoning": "User is discussing DB changes", '
            '"task_title": "Add migration", "auto_executable": true}'
        ]
        result = ChatAnalyzer._parse_response(mock_response)
        assert result is not None
        assert result.auto_executable is True
        assert result.task_title == "Add migration"

    def test_parse_without_auto_executable(self):
        """Parser should default auto_executable to False."""
        mock_response = MagicMock()
        mock_response.text_parts = [
            '{"should_suggest": true, "suggestion_type": "answer", '
            '"suggestion_text": "The answer is 42", "confidence": 0.8}'
        ]
        result = ChatAnalyzer._parse_response(mock_response)
        assert result is not None
        assert result.auto_executable is False


# ---------------------------------------------------------------------------
# Config validation tests
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_valid_config(self, config):
        """Valid config should produce no errors."""
        errors = config.validate()
        assert len(errors) == 0

    def test_invalid_chat_history_window(self):
        """chat_history_window < 1 should be rejected."""
        cfg = ChatAnalyzerConfig(enabled=True, chat_history_window=0)
        errors = cfg.validate()
        field_errors = [e for e in errors if e.field == "chat_history_window"]
        assert len(field_errors) == 1

    def test_invalid_auto_execute_confidence(self):
        """auto_execute_confidence below confidence_threshold should error."""
        cfg = ChatAnalyzerConfig(
            enabled=True,
            confidence_threshold=0.7,
            auto_execute_confidence=0.5,
        )
        errors = cfg.validate()
        field_errors = [e for e in errors if e.field == "auto_execute_confidence"]
        assert len(field_errors) >= 1

    def test_invalid_auto_execute_types(self):
        """Invalid suggestion types should be rejected."""
        cfg = ChatAnalyzerConfig(
            enabled=True,
            auto_execute_types=["task", "invalid_type"],
        )
        errors = cfg.validate()
        field_errors = [e for e in errors if e.field == "auto_execute_types"]
        assert len(field_errors) == 1


# ---------------------------------------------------------------------------
# Event handling tests
# ---------------------------------------------------------------------------


class TestEventHandling:
    @pytest.mark.asyncio
    async def test_on_message_buffers_correctly(self, analyzer):
        """Messages should be buffered per channel."""
        await analyzer._on_message({
            "channel_id": 100,
            "project_id": "proj-1",
            "author": "alice",
            "content": "hello world",
            "timestamp": 1710000000.0,
            "is_bot": False,
        })
        assert len(analyzer._buffers[100]) == 1
        assert analyzer._channel_projects[100] == "proj-1"
        assert analyzer._new_message_counts[100] == 1

    @pytest.mark.asyncio
    async def test_on_message_includes_bot_messages(self, analyzer):
        """Bot messages should also be buffered for context."""
        await analyzer._on_message({
            "channel_id": 100,
            "project_id": "proj-1",
            "author": "AgentQueue",
            "content": "Task created!",
            "timestamp": 1710000000.0,
            "is_bot": True,
        })
        assert len(analyzer._buffers[100]) == 1
        msg = analyzer._buffers[100][0]
        assert msg.is_bot is True
        assert msg.author == "AgentQueue"


# ---------------------------------------------------------------------------
# Guard check tests
# ---------------------------------------------------------------------------


class TestGuardChecks:
    @pytest.mark.asyncio
    async def test_should_suggest_passes(self, analyzer, mock_db):
        """Valid suggestion should pass all guards."""
        suggestion = AnalyzerSuggestion(
            should_suggest=True, suggestion_text="Create a task",
            suggestion_type="task", confidence=0.8,
        )
        result = await analyzer._should_suggest(suggestion, "test-proj", 100)
        assert result is True

    @pytest.mark.asyncio
    async def test_should_suggest_low_confidence(self, analyzer, mock_db):
        """Low confidence should fail guard."""
        suggestion = AnalyzerSuggestion(
            should_suggest=True, suggestion_text="Maybe...",
            suggestion_type="task", confidence=0.3,
        )
        result = await analyzer._should_suggest(suggestion, "test-proj", 100)
        assert result is False

    @pytest.mark.asyncio
    async def test_should_suggest_false(self, analyzer, mock_db):
        """should_suggest=False should fail guard."""
        suggestion = AnalyzerSuggestion(
            should_suggest=False, suggestion_text="nope",
            suggestion_type="task", confidence=0.9,
        )
        result = await analyzer._should_suggest(suggestion, "test-proj", 100)
        assert result is False
