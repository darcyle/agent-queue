"""Tests for the set_project_channel command.

Covers:
- Link a channel to a project
- Project not found -> error
- DB state is updated correctly
- Setting a new channel replaces the previous one
"""

import pytest
from src.config import AppConfig, DiscordConfig
from src.command_handler import CommandHandler
from src.database import Database
from src.models import Project
from src.orchestrator import Orchestrator


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "test.db"),
    )


@pytest.fixture
async def handler(db, config):
    orchestrator = Orchestrator(config)
    orchestrator.db = db
    return CommandHandler(orchestrator, config)


class TestSetProjectChannel:
    """Link a channel to a project."""

    async def test_link_channel(self, handler, db):
        await db.create_project(Project(id="p-1", name="Alpha"))

        result = await handler.execute("set_project_channel", {
            "project_id": "p-1",
            "channel_id": "111111111111111111",
        })

        assert "error" not in result
        assert result["project_id"] == "p-1"
        assert result["channel_id"] == "111111111111111111"
        assert result["status"] == "linked"

        project = await db.get_project("p-1")
        assert project.discord_channel_id == "111111111111111111"

    async def test_replace_existing_channel(self, handler, db):
        """Setting a new channel replaces the old one."""
        await db.create_project(Project(id="p-1", name="Alpha"))

        await handler.execute("set_project_channel", {
            "project_id": "p-1",
            "channel_id": "111111111111111111",
        })
        result = await handler.execute("set_project_channel", {
            "project_id": "p-1",
            "channel_id": "999999999999999999",
        })

        assert "error" not in result
        project = await db.get_project("p-1")
        assert project.discord_channel_id == "999999999999999999"


class TestSetProjectChannelErrors:
    """Error conditions for set_project_channel."""

    async def test_project_not_found(self, handler):
        result = await handler.execute("set_project_channel", {
            "project_id": "nonexistent",
            "channel_id": "111111111111111111",
        })

        assert "error" in result
        assert "not found" in result["error"].lower()


class TestSetProjectChannelToolDefinition:
    """Verify the tool is properly defined in the TOOLS list."""

    def test_tool_exists_in_tools_list(self):
        from src.chat_agent import TOOLS

        tool_names = [t["name"] for t in TOOLS]
        assert "set_project_channel" in tool_names

    def test_tool_schema(self):
        from src.chat_agent import TOOLS

        tool = next(t for t in TOOLS if t["name"] == "set_project_channel")
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert "project_id" in schema["properties"]
        assert "channel_id" in schema["properties"]
        assert "channel_type" not in schema["properties"]
        assert "project_id" in schema["required"]
        assert "channel_id" in schema["required"]
