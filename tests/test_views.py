"""Tests for standalone Discord views."""
import sys
from unittest.mock import MagicMock

# Stub discord before importing
for mod in ["discord", "discord.ext", "discord.ext.commands",
            "discord.app_commands", "discord.ui"]:
    sys.modules.setdefault(mod, MagicMock())


def test_suggestion_view_exists():
    from src.discord.views import SuggestionView
    assert SuggestionView is not None


def test_suggestion_embed_formatter():
    from src.discord.views import format_suggestion_embed
    embed = format_suggestion_embed(
        suggestion_type="task",
        text="Create a profiling task for the particle system",
        project_id="my-game",
        confidence=0.85,
    )
    assert embed is not None
