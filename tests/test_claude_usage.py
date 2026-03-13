"""Tests for the claude_usage command."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_orchestrator():
    orch = MagicMock()
    orch._paused = False
    orch._config_watcher = None
    return orch


@pytest.fixture
def mock_db():
    return AsyncMock()


@pytest.fixture
def handler(mock_orchestrator, mock_db):
    from src.command_handler import CommandHandler
    h = CommandHandler(mock_orchestrator, mock_db)
    return h


class TestClaudeUsageCommand:
    """Tests for _cmd_claude_usage."""

    @pytest.mark.asyncio
    async def test_reads_stats_cache(self, handler, tmp_path):
        """Stats cache is parsed into structured output."""
        stats = {
            "totalSessions": 42,
            "totalMessages": 1000,
            "modelUsage": {
                "claude-sonnet-4-5-20250929": {
                    "inputTokens": 100,
                    "outputTokens": 200,
                    "cacheReadInputTokens": 5000,
                    "cacheCreationInputTokens": 1000,
                }
            },
            "dailyActivity": [
                {"date": "2026-03-12", "messageCount": 50, "sessionCount": 3, "toolCallCount": 20},
                {"date": "2026-03-10", "messageCount": 30, "sessionCount": 2, "toolCallCount": 10},
                {"date": "2026-03-01", "messageCount": 999, "sessionCount": 99, "toolCallCount": 99},
            ],
            "dailyModelTokens": [
                {"date": "2026-03-12", "tokensByModel": {"claude-sonnet-4-5-20250929": 5000}},
                {"date": "2026-03-01", "tokensByModel": {"claude-sonnet-4-5-20250929": 99999}},
            ],
        }

        stats_file = tmp_path / ".claude" / "stats-cache.json"
        stats_file.parent.mkdir(parents=True)
        stats_file.write_text(json.dumps(stats))

        creds_file = tmp_path / ".claude" / ".credentials.json"
        creds_file.write_text(json.dumps({
            "claudeAiOauth": {
                "subscriptionType": "max",
                "rateLimitTier": "default_claude_max_20x",
            }
        }))

        with patch("pathlib.Path.home", return_value=tmp_path):
            with patch.object(handler, "_probe_claude_rate_limit", new_callable=AsyncMock, return_value={"status": "allowed"}):
                result = await handler.execute("claude_usage", {})

        assert result["total_sessions"] == 42
        assert result["total_messages"] == 1000
        assert result["subscription"] == "max"
        assert result["rate_limit_tier"] == "default_claude_max_20x"

        # Model usage
        assert "sonnet-4-5" in result["model_usage"]
        mu = result["model_usage"]["sonnet-4-5"]
        assert mu["input"] == 100
        assert mu["output"] == 200
        assert mu["total"] == 6300  # 100 + 200 + 5000 + 1000

        # Weekly stats should include Mar 12 and Mar 10 but not Mar 1
        assert result["week"]["messages"] == 80  # 50 + 30
        assert result["week"]["sessions"] == 5   # 3 + 2

        # Weekly tokens should include Mar 12 but not Mar 1
        assert result["week_tokens_by_model"]["sonnet-4-5"] == 5000

    @pytest.mark.asyncio
    async def test_missing_stats_cache(self, handler, tmp_path):
        """Graceful fallback when stats cache doesn't exist."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            with patch.object(handler, "_probe_claude_rate_limit", new_callable=AsyncMock, return_value={}):
                result = await handler.execute("claude_usage", {})

        assert "stats_error" in result
        assert "not found" in result["stats_error"]

    @pytest.mark.asyncio
    async def test_rate_limit_probe_error_handled(self, handler, tmp_path):
        """Rate limit probe errors don't crash the command."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            with patch.object(handler, "_probe_claude_rate_limit", side_effect=Exception("network error")):
                result = await handler.execute("claude_usage", {})

        assert "rate_limit_error" in result
        assert "network error" in result["rate_limit_error"]


class TestProbeRateLimit:
    """Tests for _probe_claude_rate_limit."""

    @pytest.mark.asyncio
    async def test_uses_oauth_token(self, handler, tmp_path):
        """OAuth token from credentials is used for the API call."""
        creds_file = tmp_path / ".claude" / ".credentials.json"
        creds_file.parent.mkdir(parents=True)
        creds_file.write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "sk-test-token"}
        }))

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.headers = {
            "anthropic-ratelimit-unified-status": "allowed",
            "anthropic-ratelimit-unified-token-utilization": "0.35",
            "anthropic-ratelimit-unified-reset": "1741900800",
        }
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("pathlib.Path.home", return_value=tmp_path):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ANTHROPIC_API_KEY", None)
                with patch("aiohttp.ClientSession", return_value=mock_session):
                    result = await handler._probe_claude_rate_limit()

        assert result["status"] == "allowed"
        assert result["token-utilization"] == "0.35"
        assert result["token-utilization_pct"] == "35.0%"
        assert result["http_status"] == 200

    @pytest.mark.asyncio
    async def test_no_credentials(self, handler, tmp_path):
        """Returns error when no credentials available."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ANTHROPIC_API_KEY", None)
                result = await handler._probe_claude_rate_limit()

        assert "error" in result
