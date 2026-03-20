"""Eval tests for the chat analyzer tools (analyzer_status, analyzer_toggle, analyzer_history).

Uses ScriptedProvider to verify the chat agent correctly invokes analyzer
tools in response to natural language requests.
"""

from __future__ import annotations

import pytest


class TestAnalyzerStatusTool:
    """Test that the LLM invokes analyzer_status for status queries."""

    async def test_analyzer_status_call(self, eval_agent):
        agent, recorder, provider = eval_agent

        provider.add_tool_then_text(
            "analyzer_status", {},
            "The chat analyzer is enabled, using llama3.2. "
            "It has made 10 suggestions total: 5 accepted, 3 dismissed, 2 pending.",
        )

        response = await agent.chat("is the chat analyzer running?", user_name="test")

        assert recorder.was_called("analyzer_status")
        assert "analyzer" in response.lower()

    async def test_analyzer_status_with_project(self, eval_agent):
        agent, recorder, provider = eval_agent

        provider.add_tool_then_text(
            "analyzer_status", {"project_id": "my-app"},
            "The analyzer has made 3 suggestions for my-app.",
        )

        response = await agent.chat(
            "show me the analyzer stats for my-app", user_name="test",
        )

        assert recorder.was_called("analyzer_status")
        calls = recorder.calls_for("analyzer_status")
        assert len(calls) == 1


class TestAnalyzerToggleTool:
    """Test that the LLM invokes analyzer_toggle for enable/disable requests."""

    async def test_disable_analyzer(self, eval_agent):
        agent, recorder, provider = eval_agent

        provider.add_tool_then_text(
            "analyzer_toggle", {"enabled": False},
            "I've disabled the chat analyzer.",
        )

        response = await agent.chat("turn off the chat analyzer", user_name="test")

        assert recorder.was_called("analyzer_toggle")
        assert "disabled" in response.lower() or "off" in response.lower()

    async def test_enable_analyzer(self, eval_agent):
        agent, recorder, provider = eval_agent

        provider.add_tool_then_text(
            "analyzer_toggle", {"enabled": True},
            "The chat analyzer is now enabled.",
        )

        response = await agent.chat("enable the analyzer", user_name="test")

        assert recorder.was_called("analyzer_toggle")


class TestAnalyzerHistoryTool:
    """Test that the LLM invokes analyzer_history for history queries."""

    async def test_view_history(self, eval_agent):
        agent, recorder, provider = eval_agent

        provider.add_tool_then_text(
            "analyzer_history", {},
            "Here are the recent analyzer suggestions:\n"
            "1. [task] Create a migration — accepted\n"
            "2. [answer] The endpoint is /api/v2 — dismissed",
        )

        response = await agent.chat(
            "show me what the analyzer has suggested recently", user_name="test",
        )

        assert recorder.was_called("analyzer_history")

    async def test_history_with_project_filter(self, eval_agent):
        agent, recorder, provider = eval_agent

        provider.add_tool_then_text(
            "analyzer_history", {"project_id": "backend"},
            "No recent suggestions for backend.",
        )

        response = await agent.chat(
            "any analyzer suggestions for the backend project?", user_name="test",
        )

        assert recorder.was_called("analyzer_history")
