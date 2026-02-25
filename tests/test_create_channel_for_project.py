"""Tests for the create_channel_for_project command.

Covers:
- Idempotent behaviour: existing channel is linked, not duplicated
- New channel creation: _created_channel_id path
- Missing project -> error
- Missing project_id -> error
- No guild context -> error
- Channel name defaults to project ID
- Channel name normalisation (leading '#' stripped)
- Tool definition exists in TOOLS list
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


# ---------------------------------------------------------------------------
# Guild channels fixture (simulates Discord guild text channels)
# ---------------------------------------------------------------------------
GUILD_CHANNELS = [
    {"id": 111111111111111111, "name": "general"},
    {"id": 222222222222222222, "name": "my-project"},
    {"id": 333333333333333333, "name": "other-channel"},
]


class TestIdempotentExistingChannel:
    """When a channel with the target name already exists, link it."""

    async def test_links_existing_channel(self, handler, db):
        await db.create_project(Project(id="my-project", name="My Project"))

        result = await handler.execute("create_channel_for_project", {
            "project_id": "my-project",
            "guild_channels": GUILD_CHANNELS,
        })

        assert "error" not in result
        assert result["action"] == "linked_existing"
        assert result["project_id"] == "my-project"
        assert result["channel_id"] == "222222222222222222"
        assert result["channel_name"] == "my-project"
        assert result["status"] == "linked"

        # Verify the DB was updated.
        project = await db.get_project("my-project")
        assert project.discord_channel_id == "222222222222222222"

    async def test_existing_channel_with_custom_name(self, handler, db):
        await db.create_project(Project(id="p-1", name="Alpha"))

        result = await handler.execute("create_channel_for_project", {
            "project_id": "p-1",
            "channel_name": "general",
            "guild_channels": GUILD_CHANNELS,
        })

        assert "error" not in result
        assert result["action"] == "linked_existing"
        assert result["channel_id"] == "111111111111111111"
        assert result["channel_name"] == "general"

    async def test_rerun_is_idempotent(self, handler, db):
        """Running the command twice for the same project/channel should succeed both times."""
        await db.create_project(Project(id="my-project", name="My Project"))

        result1 = await handler.execute("create_channel_for_project", {
            "project_id": "my-project",
            "guild_channels": GUILD_CHANNELS,
        })
        result2 = await handler.execute("create_channel_for_project", {
            "project_id": "my-project",
            "guild_channels": GUILD_CHANNELS,
        })

        assert "error" not in result1
        assert "error" not in result2
        assert result1["action"] == "linked_existing"
        assert result2["action"] == "linked_existing"
        assert result1["channel_id"] == result2["channel_id"]


class TestNewChannelCreation:
    """When no channel matches, the Discord layer creates one and passes _created_channel_id."""

    async def test_created_channel_linked(self, handler, db):
        await db.create_project(Project(id="new-project", name="New Project"))

        result = await handler.execute("create_channel_for_project", {
            "project_id": "new-project",
            "channel_name": "new-project",
            "guild_channels": GUILD_CHANNELS,  # "new-project" not in this list
            "_created_channel_id": "444444444444444444",
        })

        assert "error" not in result
        assert result["action"] == "created"
        assert result["channel_id"] == "444444444444444444"
        assert result["channel_name"] == "new-project"
        assert result["status"] == "linked"

        project = await db.get_project("new-project")
        assert project.discord_channel_id == "444444444444444444"


class TestErrorCases:
    """Test various error conditions."""

    async def test_project_not_found(self, handler):
        result = await handler.execute("create_channel_for_project", {
            "project_id": "nonexistent",
            "guild_channels": GUILD_CHANNELS,
        })

        assert "error" in result
        assert "not found" in result["error"].lower()

    async def test_missing_project_id(self, handler):
        result = await handler.execute("create_channel_for_project", {
            "guild_channels": GUILD_CHANNELS,
        })

        assert "error" in result
        assert "project_id" in result["error"].lower()

    async def test_no_guild_context(self, handler, db):
        """Without guild_channels or _created_channel_id, returns an error."""
        await db.create_project(Project(id="p-1", name="Alpha"))

        result = await handler.execute("create_channel_for_project", {
            "project_id": "p-1",
            "channel_name": "nonexistent-channel",
        })

        assert "error" in result
        assert "guild context" in result["error"].lower() or "discord" in result["error"].lower()


class TestChannelNameDefaults:
    """Test channel name defaults and normalisation."""

    async def test_defaults_to_project_id(self, handler, db):
        await db.create_project(Project(id="my-project", name="My Project"))

        result = await handler.execute("create_channel_for_project", {
            "project_id": "my-project",
            "guild_channels": GUILD_CHANNELS,
        })

        # Should find "my-project" channel since channel_name defaults to project_id
        assert "error" not in result
        assert result["channel_name"] == "my-project"

    async def test_hash_prefix_stripped(self, handler, db):
        await db.create_project(Project(id="p-1", name="Alpha"))

        result = await handler.execute("create_channel_for_project", {
            "project_id": "p-1",
            "channel_name": "#general",
            "guild_channels": GUILD_CHANNELS,
        })

        assert "error" not in result
        assert result["channel_name"] == "general"
        assert result["channel_id"] == "111111111111111111"

    async def test_project_name_as_fallback(self, handler, db):
        """project_name arg can be used as an alias for project_id."""
        await db.create_project(Project(id="my-project", name="My Project"))

        result = await handler.execute("create_channel_for_project", {
            "project_name": "my-project",
            "guild_channels": GUILD_CHANNELS,
        })

        assert "error" not in result
        assert result["project_id"] == "my-project"


class TestToolDefinition:
    """Verify the tool is properly defined in the TOOLS list."""

    def test_tool_exists_in_tools_list(self):
        from src.chat_agent import TOOLS

        tool_names = [t["name"] for t in TOOLS]
        assert "create_channel_for_project" in tool_names

    def test_tool_schema(self):
        from src.chat_agent import TOOLS

        tool = next(t for t in TOOLS if t["name"] == "create_channel_for_project")
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert "project_id" in schema["properties"]
        assert "channel_name" in schema["properties"]
        assert "channel_type" not in schema["properties"]
        assert "project_id" in schema["required"]
        # channel_name is optional (defaults to project_id)
        assert "channel_name" not in schema["required"]
