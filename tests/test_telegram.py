"""Tests for the Telegram bot package.

Covers:
- TelegramMessagingAdapter ABC compliance
- TelegramBot authorization, message routing, and chat management
- Telegram notification formatting (MarkdownV2 escaping, splitting)
- Command parsing and routing to CommandHandler
- Integration: message received -> supervisor invoked -> response sent
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import AppConfig, TelegramConfig
from src.messaging.base import MessagingAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> AppConfig:
    """Create a minimal AppConfig for Telegram testing."""
    tg = TelegramConfig(
        bot_token="123:FAKE_TOKEN",
        chat_id="-100999",
        authorized_users=["111", "222"],
        per_project_chats={"proj1": "-100888"},
        use_topics=True,
    )
    config = AppConfig(messaging_platform="telegram", telegram=tg)
    for k, v in overrides.items():
        setattr(config, k, v)
    return config


def _make_orchestrator() -> MagicMock:
    """Create a mock orchestrator."""
    orch = MagicMock()
    orch.llm_logger = None
    orch.hooks = None
    orch.set_notify_callback = MagicMock()
    orch.set_create_thread_callback = MagicMock()
    orch.set_command_handler = MagicMock()
    orch.set_supervisor = MagicMock()
    return orch


# ---------------------------------------------------------------------------
# Notification formatting
# ---------------------------------------------------------------------------


class TestMarkdownV2Escaping:
    """Test MarkdownV2 escaping utilities."""

    def test_escape_special_chars(self):
        from src.telegram.notifications import escape_markdown

        result = escape_markdown("Hello *world* [test]")
        assert "\\*" in result
        assert "\\[" in result
        assert "\\]" in result

    def test_escape_all_special_chars(self):
        from src.telegram.notifications import escape_markdown

        # All special chars from the spec
        for ch in "_*[]()~`>#+-=|{}.!":
            assert f"\\{ch}" in escape_markdown(ch)

    def test_escape_plain_text_unchanged(self):
        from src.telegram.notifications import escape_markdown

        result = escape_markdown("Hello world 123")
        assert result == "Hello world 123"

    def test_bold(self):
        from src.telegram.notifications import bold

        result = bold("test")
        assert result == "*test*"

    def test_bold_with_special_chars(self):
        from src.telegram.notifications import bold

        result = bold("hello [world]")
        assert result == "*hello \\[world\\]*"

    def test_italic(self):
        from src.telegram.notifications import italic

        result = italic("test")
        assert result == "_test_"

    def test_code(self):
        from src.telegram.notifications import code

        result = code("var = 1")
        assert result == "`var = 1`"

    def test_code_block(self):
        from src.telegram.notifications import code_block

        result = code_block("print('hi')", "python")
        assert result == "```python\nprint('hi')\n```"

    def test_link(self):
        from src.telegram.notifications import link

        result = link("click here", "https://example.com")
        assert result == "[click here](https://example.com)"


class TestMessageSplitting:
    """Test message splitting for Telegram's 4096 char limit."""

    def test_short_message_not_split(self):
        from src.telegram.notifications import split_message

        result = split_message("short message")
        assert result == ["short message"]

    def test_long_message_split_on_newlines(self):
        from src.telegram.notifications import split_message

        # Create a message that exceeds limit
        lines = ["line " * 50 + "\n" for _ in range(30)]
        text = "".join(lines)
        result = split_message(text, limit=500)
        assert len(result) > 1
        for chunk in result:
            assert len(chunk) <= 500

    def test_single_long_line_hard_split(self):
        from src.telegram.notifications import split_message

        text = "x" * 1000
        result = split_message(text, limit=300)
        assert len(result) == 4  # ceil(1000/300) = 4
        assert all(len(chunk) <= 300 for chunk in result)

    def test_empty_message(self):
        from src.telegram.notifications import split_message

        result = split_message("")
        assert result == [""]


class TestNotificationFormatters:
    """Test task notification formatters."""

    def test_format_server_started(self):
        from src.telegram.notifications import format_server_started

        result = format_server_started()
        assert "AgentQueue" in result
        assert "online" in result

    def test_format_task_started(self):
        from src.telegram.notifications import format_task_started

        task = MagicMock(title="Fix bug #123", id="t1", task_type="bugfix")
        project = MagicMock(name="my-project", id="p1")
        result = format_task_started(task, project)
        assert "Task Started" in result
        assert "Fix bug" in result
        assert "my\\-project" in result  # escaped

    def test_format_task_completed(self):
        from src.telegram.notifications import format_task_completed

        task = MagicMock(title="Add feature", id="t2")
        project = MagicMock(name="proj", id="p1")
        result = format_task_completed(task, project, summary="All tests pass")
        assert "Completed" in result
        assert "All tests pass" in result

    def test_format_task_failed(self):
        from src.telegram.notifications import format_task_failed

        task = MagicMock(title="Deploy", id="t3")
        project = MagicMock(name="proj", id="p1")
        result = format_task_failed(task, project, error="Timeout exceeded")
        assert "Failed" in result
        assert "Timeout exceeded" in result

    def test_format_embed_as_text(self):
        from src.telegram.notifications import format_embed_as_text

        result = format_embed_as_text(
            title="Server Online",
            description="All systems go",
            fields=[("Status", "Running"), ("Tasks", "5")],
            footer="Updated just now",
        )
        assert "*Server Online*" in result  # bold
        assert "All systems go" in result
        assert "*Status*" in result
        assert "Running" in result
        assert "_Updated just now_" in result  # italic

    def test_format_embed_with_url(self):
        from src.telegram.notifications import format_embed_as_text

        result = format_embed_as_text(
            title="PR Ready",
            url="https://github.com/org/repo/pull/1",
        )
        assert "[PR Ready]" in result
        assert "https://github.com/org/repo/pull/1" in result


class TestInlineKeyboard:
    """Test inline keyboard helpers."""

    def test_make_inline_keyboard(self):
        from src.telegram.notifications import make_inline_keyboard

        buttons = [("Retry", "retry_task:id=123"), ("Skip", "skip_task:id=123")]
        result = make_inline_keyboard(buttons)
        assert len(result) == 2
        assert result[0][0]["text"] == "Retry"
        assert result[0][0]["callback_data"] == "retry_task:id=123"
        assert result[1][0]["text"] == "Skip"


# ---------------------------------------------------------------------------
# TelegramMessagingAdapter ABC compliance
# ---------------------------------------------------------------------------


class TestTelegramAdapterABC:
    """Verify TelegramMessagingAdapter implements MessagingAdapter correctly."""

    @patch("src.telegram.bot.TelegramBot")
    def test_is_messaging_adapter(self, mock_bot_cls):
        """Adapter is a subclass of MessagingAdapter."""
        from src.telegram.adapter import TelegramMessagingAdapter

        assert issubclass(TelegramMessagingAdapter, MessagingAdapter)

    @patch("src.telegram.bot.TelegramBot")
    def test_instantiation(self, mock_bot_cls):
        """Adapter can be instantiated with config and orchestrator."""
        from src.telegram.adapter import TelegramMessagingAdapter

        config = _make_config()
        orch = _make_orchestrator()
        adapter = TelegramMessagingAdapter(config, orch)
        assert isinstance(adapter, MessagingAdapter)

    @patch("src.telegram.bot.TelegramBot")
    async def test_start_delegates(self, mock_bot_cls):
        from src.telegram.adapter import TelegramMessagingAdapter

        mock_bot = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        adapter = TelegramMessagingAdapter(_make_config(), _make_orchestrator())
        await adapter.start()
        mock_bot.start.assert_awaited_once()

    @patch("src.telegram.bot.TelegramBot")
    async def test_wait_until_ready_delegates(self, mock_bot_cls):
        from src.telegram.adapter import TelegramMessagingAdapter

        mock_bot = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        adapter = TelegramMessagingAdapter(_make_config(), _make_orchestrator())
        await adapter.wait_until_ready()
        mock_bot.wait_until_ready.assert_awaited_once()

    @patch("src.telegram.bot.TelegramBot")
    async def test_close_delegates(self, mock_bot_cls):
        from src.telegram.adapter import TelegramMessagingAdapter

        mock_bot = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        adapter = TelegramMessagingAdapter(_make_config(), _make_orchestrator())
        await adapter.close()
        mock_bot.stop.assert_awaited_once()

    @patch("src.telegram.bot.TelegramBot")
    async def test_send_message_delegates(self, mock_bot_cls):
        from src.telegram.adapter import TelegramMessagingAdapter

        mock_bot = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        adapter = TelegramMessagingAdapter(_make_config(), _make_orchestrator())
        await adapter.send_message("hello", "proj1", embed="fake_embed")
        mock_bot.send_notification.assert_awaited_once_with(
            "hello", "proj1", embed="fake_embed", view=None
        )

    @patch("src.telegram.bot.TelegramBot")
    async def test_create_task_thread_delegates(self, mock_bot_cls):
        from src.telegram.adapter import TelegramMessagingAdapter

        mock_bot = AsyncMock()
        mock_bot.create_task_topic.return_value = (AsyncMock(), AsyncMock())
        mock_bot_cls.return_value = mock_bot

        adapter = TelegramMessagingAdapter(_make_config(), _make_orchestrator())
        task = MagicMock(title="Test task", id="t1")
        project = MagicMock(id="proj1")
        result = await adapter.create_task_thread(task, project)
        assert result is not None
        assert len(result) == 2

    @patch("src.telegram.bot.TelegramBot")
    async def test_create_task_thread_returns_noop_on_failure(self, mock_bot_cls):
        from src.telegram.adapter import TelegramMessagingAdapter

        mock_bot = AsyncMock()
        mock_bot.create_task_topic.return_value = None
        mock_bot_cls.return_value = mock_bot

        adapter = TelegramMessagingAdapter(_make_config(), _make_orchestrator())
        task = MagicMock(title="Test", id="t1")
        project = MagicMock(id="p1")
        result = await adapter.create_task_thread(task, project)
        # Should return noop callbacks, not None
        assert result is not None
        send_to_thread, notify_main = result
        # Should not raise
        await send_to_thread("test")
        await notify_main("test")

    @patch("src.telegram.bot.TelegramBot")
    def test_get_command_handler(self, mock_bot_cls):
        from src.telegram.adapter import TelegramMessagingAdapter

        mock_bot = MagicMock()
        mock_bot.handler = MagicMock()
        mock_bot_cls.return_value = mock_bot

        adapter = TelegramMessagingAdapter(_make_config(), _make_orchestrator())
        assert adapter.get_command_handler() is mock_bot.handler

    @patch("src.telegram.bot.TelegramBot")
    def test_get_supervisor(self, mock_bot_cls):
        from src.telegram.adapter import TelegramMessagingAdapter

        mock_bot = MagicMock()
        mock_bot.supervisor = MagicMock()
        mock_bot_cls.return_value = mock_bot

        adapter = TelegramMessagingAdapter(_make_config(), _make_orchestrator())
        assert adapter.get_supervisor() is mock_bot.supervisor


# ---------------------------------------------------------------------------
# TelegramBot unit tests
# ---------------------------------------------------------------------------


class TestTelegramBotAuth:
    """Test authorization logic."""

    def test_authorized_user(self):
        """User in authorized_users list passes."""
        from src.telegram.bot import TelegramBot

        with patch("src.telegram.bot.TelegramBot.__init__", return_value=None):
            bot = TelegramBot.__new__(TelegramBot)
            bot.config = _make_config()
            assert bot._is_authorized(111) is True
            assert bot._is_authorized("222") is True

    def test_unauthorized_user(self):
        """User not in authorized_users list is rejected."""
        from src.telegram.bot import TelegramBot

        with patch("src.telegram.bot.TelegramBot.__init__", return_value=None):
            bot = TelegramBot.__new__(TelegramBot)
            bot.config = _make_config()
            assert bot._is_authorized(999) is False

    def test_empty_authorized_list_allows_all(self):
        """When authorized_users is empty, all users are allowed."""
        from src.telegram.bot import TelegramBot

        with patch("src.telegram.bot.TelegramBot.__init__", return_value=None):
            bot = TelegramBot.__new__(TelegramBot)
            config = _make_config()
            config.telegram.authorized_users = []
            bot.config = config
            assert bot._is_authorized(999) is True


class TestTelegramBotChatRouting:
    """Test per-project chat routing."""

    def _make_bot(self) -> Any:
        from src.telegram.bot import TelegramBot

        with patch("src.telegram.bot.TelegramBot.__init__", return_value=None):
            bot = TelegramBot.__new__(TelegramBot)
            bot.config = _make_config()
            bot._main_chat_id = -100999
            bot._project_chats = {"proj1": -100888}
            bot._chat_to_project = {-100888: "proj1"}
            return bot

    def test_get_chat_id_project(self):
        bot = self._make_bot()
        assert bot._get_chat_id("proj1") == -100888

    def test_get_chat_id_fallback(self):
        bot = self._make_bot()
        assert bot._get_chat_id("unknown_proj") == -100999

    def test_get_chat_id_none(self):
        bot = self._make_bot()
        assert bot._get_chat_id(None) == -100999

    def test_update_project_chat(self):
        bot = self._make_bot()
        bot.update_project_chat("proj2", -100777)
        assert bot._get_chat_id("proj2") == -100777
        assert bot._chat_to_project[-100777] == "proj2"

    def test_clear_project_chats(self):
        bot = self._make_bot()
        bot.clear_project_chats("proj1")
        assert bot._get_chat_id("proj1") == -100999  # falls back to main
        assert -100888 not in bot._chat_to_project

    def test_update_replaces_old_chat(self):
        bot = self._make_bot()
        bot.update_project_chat("proj1", -100777)
        assert bot._get_chat_id("proj1") == -100777
        # Old chat ID should be removed from reverse lookup
        assert -100888 not in bot._chat_to_project


class TestTelegramBotMessageHistory:
    """Test message buffer and history building."""

    def _make_bot(self) -> Any:
        import collections as c
        from src.telegram.bot import TelegramBot, CachedMessage

        with patch("src.telegram.bot.TelegramBot.__init__", return_value=None):
            bot = TelegramBot.__new__(TelegramBot)
            bot._chat_buffers = {}
            bot._buffer_last_access = {}
            return bot

    def test_append_to_buffer_creates_deque(self):
        from src.telegram.bot import CachedMessage

        bot = self._make_bot()
        msg = CachedMessage(
            message_id=1, author_name="Alice", is_bot=False,
            content="Hello", created_at=1000.0, chat_id=100,
        )
        bot._append_to_buffer(100, msg)
        assert 100 in bot._chat_buffers
        assert len(bot._chat_buffers[100]) == 1

    def test_build_message_history(self):
        from src.telegram.bot import CachedMessage

        bot = self._make_bot()
        bot._append_to_buffer(100, CachedMessage(
            message_id=1, author_name="Alice", is_bot=False,
            content="What's the status?", created_at=1000.0, chat_id=100,
        ))
        bot._append_to_buffer(100, CachedMessage(
            message_id=0, author_name="AgentQueue", is_bot=True,
            content="All systems running.", created_at=1001.0, chat_id=100,
        ))
        history = bot._build_message_history(100)
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert "[Alice]" in history[0]["content"]
        assert history[1]["role"] == "assistant"

    def test_empty_buffer_returns_empty(self):
        bot = self._make_bot()
        assert bot._build_message_history(999) == []


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


class TestTelegramCommands:
    """Test command parsing and routing to CommandHandler."""

    async def test_create_task_routes_to_handler(self):
        from src.telegram.commands import cmd_create_task

        handler = AsyncMock()
        handler.execute.return_value = {"task_id": "t1", "title": "Do stuff"}

        update = MagicMock()
        update.effective_message = AsyncMock()
        context = MagicMock()
        context.args = ["Fix", "the", "login", "bug"]

        await cmd_create_task(update, context, handler)
        handler.execute.assert_awaited_once_with(
            "create_task", {"description": "Fix the login bug"}
        )

    async def test_create_task_no_args(self):
        from src.telegram.commands import cmd_create_task

        handler = AsyncMock()
        update = MagicMock()
        update.effective_message = AsyncMock()
        context = MagicMock()
        context.args = []

        await cmd_create_task(update, context, handler)
        handler.execute.assert_not_awaited()
        update.effective_message.reply_text.assert_awaited_once()

    async def test_list_tasks_routes_to_handler(self):
        from src.telegram.commands import cmd_list_tasks

        handler = AsyncMock()
        handler.execute.return_value = {"tasks": []}

        update = MagicMock()
        update.effective_message = AsyncMock()
        context = MagicMock()
        context.args = ["ready"]

        await cmd_list_tasks(update, context, handler)
        handler.execute.assert_awaited_once_with("list_tasks", {"status": "ready"})

    async def test_status_command(self):
        from src.telegram.commands import cmd_status

        handler = AsyncMock()
        handler.execute.return_value = {"agents": 2, "tasks": 5}

        update = MagicMock()
        update.effective_message = AsyncMock()
        context = MagicMock()
        context.args = []

        await cmd_status(update, context, handler)
        handler.execute.assert_awaited_once_with("status", {})

    async def test_error_result_formatting(self):
        from src.telegram.commands import _send_result

        update = MagicMock()
        update.effective_message = AsyncMock()

        await _send_result(update, {"error": "Task not found"}, "Failed")
        call_args = update.effective_message.reply_text.call_args
        assert "Error" in call_args[0][0]
        assert "Task not found" in call_args[0][0]


# ---------------------------------------------------------------------------
# Integration: message -> supervisor -> response
# ---------------------------------------------------------------------------


class TestTelegramBotIntegration:
    """Integration tests for the message -> supervisor -> response flow."""

    async def test_process_chat_message_calls_supervisor(self):
        """Verify that an incoming message triggers Supervisor.chat()."""
        from src.telegram.bot import TelegramBot, CachedMessage

        with patch("src.telegram.bot.TelegramBot.__init__", return_value=None):
            bot = TelegramBot.__new__(TelegramBot)
            bot._chat_buffers = {}
            bot._buffer_last_access = {}
            bot._application = MagicMock()
            bot._application.bot = AsyncMock()
            bot.handler = MagicMock()
            bot.handler._active_project_id = None

            # Mock supervisor
            bot._supervisor = MagicMock()
            bot._supervisor._provider = MagicMock()  # truthy = configured
            bot._supervisor.chat = AsyncMock(return_value="I'm on it!")

            # Seed buffer with a user message
            bot._append_to_buffer(100, CachedMessage(
                message_id=1, author_name="Alice", is_bot=False,
                content="What is the status?", created_at=1000.0, chat_id=100,
            ))

            await bot._process_chat_message(
                chat_id=100, project_id="proj1", user_name="Alice"
            )

            bot._supervisor.chat.assert_awaited_once()
            # Bot should have sent the response
            bot._application.bot.send_message.assert_awaited()

    async def test_process_chat_message_no_provider(self):
        """Verify graceful handling when no LLM provider is configured."""
        from src.telegram.bot import TelegramBot, CachedMessage

        with patch("src.telegram.bot.TelegramBot.__init__", return_value=None):
            bot = TelegramBot.__new__(TelegramBot)
            bot._chat_buffers = {}
            bot._buffer_last_access = {}
            bot._application = MagicMock()
            bot._application.bot = AsyncMock()

            bot._supervisor = MagicMock()
            bot._supervisor._provider = None  # No provider

            await bot._process_chat_message(
                chat_id=100, project_id=None, user_name="Alice"
            )

            bot._application.bot.send_message.assert_awaited()
            call_text = bot._application.bot.send_message.call_args[1]["text"]
            assert "LLM provider" in call_text or "ANTHROPIC_API_KEY" in call_text
