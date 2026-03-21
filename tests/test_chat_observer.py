"""Tests for ChatObserver — Stage 1 keyword filter and batching."""
import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest


def _make_observer(keywords=None, enabled=True):
    from src.config import ObservationConfig
    from src.chat_observer import ChatObserver
    config = ObservationConfig(
        enabled=enabled, batch_window_seconds=60, max_buffer_size=20,
        stage1_keywords=keywords or [],
    )
    return ChatObserver(config, project_profiles={"my-game": {"particle", "renderer", "unity", "shader"}})


def _make_message(content, project_id="my-game", author="alice", is_bot=False):
    return {"channel_id": 12345, "project_id": project_id, "author": author,
            "content": content, "timestamp": 1000.0, "is_bot": is_bot}


def test_stage1_passes_project_term():
    obs = _make_observer()
    assert obs.passes_stage1(_make_message("the particle system is broken")) is True


def test_stage1_passes_action_word():
    obs = _make_observer()
    assert obs.passes_stage1(_make_message("we need to deploy the hotfix today")) is True


def test_stage1_passes_custom_keyword():
    obs = _make_observer(keywords=["kubernetes"])
    assert obs.passes_stage1(_make_message("kubernetes pod is crashlooping")) is True


def test_stage1_rejects_trivial():
    obs = _make_observer()
    assert obs.passes_stage1(_make_message("ok")) is False


def test_stage1_rejects_short_generic():
    obs = _make_observer()
    assert obs.passes_stage1(_make_message("sounds good to me")) is False


def test_stage1_passes_long_message():
    obs = _make_observer()
    assert obs.passes_stage1(_make_message("I've been thinking about the architecture and " * 10)) is True


def test_stage1_rejects_bot_messages():
    obs = _make_observer()
    assert obs.passes_stage1(_make_message("the particle system looks good", is_bot=True)) is False


def test_stage1_disabled():
    obs = _make_observer(enabled=False)
    assert obs.passes_stage1(_make_message("the particle system is broken")) is False


def test_stage1_unknown_project():
    obs = _make_observer()
    assert obs.passes_stage1(_make_message("something about particles", project_id="unknown-proj")) is False


def test_buffer_message():
    obs = _make_observer()
    obs.buffer_message(_make_message("the particle system crashed"))
    assert obs.get_buffer_size(12345) == 1


def test_flush_buffer():
    obs = _make_observer()
    obs.buffer_message(_make_message("msg 1"))
    obs.buffer_message(_make_message("msg 2"))
    batch = obs.flush_buffer(12345)
    assert len(batch) == 2
    assert obs.get_buffer_size(12345) == 0
