"""Tests for chat analyzer commands (analyzer_status, analyzer_toggle, analyzer_history).

Tests cover:
- analyzer_status returns config and stats
- analyzer_toggle enables/disables at runtime
- analyzer_history returns recent suggestions with filtering
- Database stat aggregation methods
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.command_handler import CommandHandler
from src.config import AppConfig, ChatAnalyzerConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def analyzer_config():
    """ChatAnalyzerConfig for testing."""
    return ChatAnalyzerConfig(
        enabled=True,
        interval_seconds=300,
        min_messages_to_analyze=3,
        confidence_threshold=0.7,
        max_suggestions_per_hour=5,
        provider="ollama",
        model="llama3.2",
    )


@pytest.fixture
def app_config(tmp_path, analyzer_config):
    """AppConfig with chat analyzer configured."""
    return AppConfig(
        database_path=str(tmp_path / "test.db"),
        workspace_dir=str(tmp_path / "workspaces"),
        chat_analyzer=analyzer_config,
    )


@pytest.fixture
def mock_orchestrator(app_config):
    """Mock orchestrator with database and chat analyzer."""
    orch = MagicMock()
    orch.db = AsyncMock()
    orch.config = app_config
    orch.bus = MagicMock()
    orch.memory_manager = None
    orch.chat_analyzer = None
    return orch


@pytest.fixture
def handler(mock_orchestrator, app_config):
    """CommandHandler with mock orchestrator."""
    return CommandHandler(mock_orchestrator, app_config)


# ---------------------------------------------------------------------------
# analyzer_status tests
# ---------------------------------------------------------------------------


class TestAnalyzerStatus:
    @pytest.mark.asyncio
    async def test_returns_config_and_stats(self, handler):
        """Status should return analyzer config and aggregate stats."""
        handler.db.get_analyzer_suggestion_stats = AsyncMock(return_value={
            "total": 10, "pending": 2, "accepted": 5, "dismissed": 2, "auto_executed": 1,
        })

        result = await handler.execute("analyzer_status", {})

        assert result["enabled"] is True
        assert result["model"] == "llama3.2"
        assert result["provider"] == "ollama"
        assert result["stats"]["total"] == 10
        assert result["stats"]["accepted"] == 5
        assert result["project_id"] is None

    @pytest.mark.asyncio
    async def test_scoped_to_project(self, handler):
        """Status should pass project_id to stats query."""
        handler.db.get_analyzer_suggestion_stats = AsyncMock(return_value={
            "total": 3, "pending": 1, "accepted": 2, "dismissed": 0, "auto_executed": 0,
        })

        result = await handler.execute("analyzer_status", {"project_id": "my-proj"})

        handler.db.get_analyzer_suggestion_stats.assert_called_once_with("my-proj")
        assert result["project_id"] == "my-proj"
        assert result["stats"]["total"] == 3

    @pytest.mark.asyncio
    async def test_disabled_analyzer(self, handler):
        """Status should report enabled=False when disabled."""
        handler.config.chat_analyzer.enabled = False
        handler.db.get_analyzer_suggestion_stats = AsyncMock(return_value={
            "total": 0, "pending": 0, "accepted": 0, "dismissed": 0, "auto_executed": 0,
        })

        result = await handler.execute("analyzer_status", {})

        assert result["enabled"] is False

    @pytest.mark.asyncio
    async def test_includes_threshold_config(self, handler):
        """Status should include confidence threshold and rate limit config."""
        handler.db.get_analyzer_suggestion_stats = AsyncMock(return_value={
            "total": 0, "pending": 0, "accepted": 0, "dismissed": 0, "auto_executed": 0,
        })

        result = await handler.execute("analyzer_status", {})

        assert result["confidence_threshold"] == 0.7
        assert result["max_suggestions_per_hour"] == 5
        assert result["interval_seconds"] == 300


# ---------------------------------------------------------------------------
# analyzer_toggle tests
# ---------------------------------------------------------------------------


class TestAnalyzerToggle:
    @pytest.mark.asyncio
    async def test_disable_analyzer(self, handler):
        """Toggle should disable a running analyzer."""
        mock_analyzer = AsyncMock()
        handler.orchestrator.chat_analyzer = mock_analyzer

        result = await handler.execute("analyzer_toggle", {"enabled": False})

        assert result["enabled"] is False
        assert "disabled" in result["message"].lower()
        mock_analyzer.shutdown.assert_called_once()
        assert handler.config.chat_analyzer.enabled is False

    @pytest.mark.asyncio
    async def test_enable_analyzer(self, handler):
        """Toggle should enable a disabled analyzer and start it."""
        handler.config.chat_analyzer.enabled = False

        with patch("src.chat_analyzer.ChatAnalyzer") as MockChatAnalyzer:
            mock_instance = AsyncMock()
            MockChatAnalyzer.return_value = mock_instance

            result = await handler.execute("analyzer_toggle", {"enabled": True})

        assert result["enabled"] is True
        assert "enabled" in result["message"].lower()
        assert handler.config.chat_analyzer.enabled is True
        mock_instance.initialize.assert_called_once()

    @pytest.mark.asyncio
    async def test_toggle_flips_state(self, handler):
        """Toggle without explicit 'enabled' should flip the current state."""
        mock_analyzer = AsyncMock()
        handler.orchestrator.chat_analyzer = mock_analyzer
        # Currently enabled, should become disabled
        result = await handler.execute("analyzer_toggle", {})

        assert result["enabled"] is False
        mock_analyzer.shutdown.assert_called_once()

    @pytest.mark.asyncio
    async def test_already_enabled(self, handler):
        """Toggle to 'enabled' when already enabled is a no-op."""
        handler.db.get_analyzer_suggestion_stats = AsyncMock(return_value={
            "total": 0, "pending": 0, "accepted": 0, "dismissed": 0, "auto_executed": 0,
        })

        result = await handler.execute("analyzer_toggle", {"enabled": True})

        assert result["enabled"] is True
        assert "already" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_already_disabled(self, handler):
        """Toggle to 'disabled' when already disabled is a no-op."""
        handler.config.chat_analyzer.enabled = False

        result = await handler.execute("analyzer_toggle", {"enabled": False})

        assert result["enabled"] is False
        assert "already" in result["message"].lower()


# ---------------------------------------------------------------------------
# analyzer_history tests
# ---------------------------------------------------------------------------


class TestAnalyzerHistory:
    @pytest.mark.asyncio
    async def test_returns_recent_suggestions(self, handler):
        """History should return recent suggestions from DB."""
        now = time.time()
        handler.db.get_analyzer_suggestion_history = AsyncMock(return_value=[
            {
                "id": 1, "project_id": "proj-a", "channel_id": 100,
                "suggestion_type": "task", "suggestion_text": "Create a migration",
                "status": "accepted", "created_at": now - 3600, "resolved_at": now - 3500,
            },
            {
                "id": 2, "project_id": "proj-a", "channel_id": 100,
                "suggestion_type": "answer", "suggestion_text": "The endpoint is /api/v2",
                "status": "dismissed", "created_at": now - 1800, "resolved_at": now - 1700,
            },
        ])

        result = await handler.execute("analyzer_history", {})

        assert result["count"] == 2
        assert len(result["suggestions"]) == 2
        assert result["suggestions"][0]["suggestion_type"] == "task"
        assert result["suggestions"][1]["status"] == "dismissed"

    @pytest.mark.asyncio
    async def test_scoped_to_project(self, handler):
        """History should pass project_id filter to DB."""
        handler.db.get_analyzer_suggestion_history = AsyncMock(return_value=[])

        result = await handler.execute("analyzer_history", {"project_id": "proj-b"})

        handler.db.get_analyzer_suggestion_history.assert_called_once_with(
            project_id="proj-b", limit=20,
        )
        assert result["project_id"] == "proj-b"

    @pytest.mark.asyncio
    async def test_custom_limit(self, handler):
        """History should respect the limit parameter."""
        handler.db.get_analyzer_suggestion_history = AsyncMock(return_value=[])

        await handler.execute("analyzer_history", {"limit": 5})

        handler.db.get_analyzer_suggestion_history.assert_called_once_with(
            project_id=None, limit=5,
        )

    @pytest.mark.asyncio
    async def test_empty_history(self, handler):
        """History should handle no suggestions gracefully."""
        handler.db.get_analyzer_suggestion_history = AsyncMock(return_value=[])

        result = await handler.execute("analyzer_history", {})

        assert result["count"] == 0
        assert result["suggestions"] == []
