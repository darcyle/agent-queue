"""Tests for database-level channel field operations.

Covers:
- Project creation with channel fields (default None)
- Updating discord_channel_id
- Updating discord_control_channel_id
- Updating both channel fields independently
- Clearing channel fields (set to None)
- Channel fields survive project update of other fields
"""

import pytest
from src.database import Database
from src.models import Project


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


class TestProjectChannelDefaults:
    """Channel fields default to None on creation."""

    async def test_new_project_has_no_channels(self, db):
        await db.create_project(Project(id="p-1", name="Alpha"))

        project = await db.get_project("p-1")
        assert project.discord_channel_id is None
        assert project.discord_control_channel_id is None


class TestUpdateDiscordChannelId:
    """Set and update the notifications channel."""

    async def test_set_notifications_channel(self, db):
        await db.create_project(Project(id="p-1", name="Alpha"))

        await db.update_project("p-1", discord_channel_id="111111111111111111")

        project = await db.get_project("p-1")
        assert project.discord_channel_id == "111111111111111111"

    async def test_replace_notifications_channel(self, db):
        await db.create_project(Project(id="p-1", name="Alpha"))
        await db.update_project("p-1", discord_channel_id="111111111111111111")

        await db.update_project("p-1", discord_channel_id="999999999999999999")

        project = await db.get_project("p-1")
        assert project.discord_channel_id == "999999999999999999"

    async def test_clear_notifications_channel(self, db):
        await db.create_project(Project(id="p-1", name="Alpha"))
        await db.update_project("p-1", discord_channel_id="111111111111111111")

        await db.update_project("p-1", discord_channel_id=None)

        project = await db.get_project("p-1")
        assert project.discord_channel_id is None


class TestUpdateDiscordControlChannelId:
    """Set and update the control channel."""

    async def test_set_control_channel(self, db):
        await db.create_project(Project(id="p-1", name="Alpha"))

        await db.update_project("p-1", discord_control_channel_id="222222222222222222")

        project = await db.get_project("p-1")
        assert project.discord_control_channel_id == "222222222222222222"

    async def test_replace_control_channel(self, db):
        await db.create_project(Project(id="p-1", name="Alpha"))
        await db.update_project("p-1", discord_control_channel_id="222222222222222222")

        await db.update_project("p-1", discord_control_channel_id="888888888888888888")

        project = await db.get_project("p-1")
        assert project.discord_control_channel_id == "888888888888888888"

    async def test_clear_control_channel(self, db):
        await db.create_project(Project(id="p-1", name="Alpha"))
        await db.update_project("p-1", discord_control_channel_id="222222222222222222")

        await db.update_project("p-1", discord_control_channel_id=None)

        project = await db.get_project("p-1")
        assert project.discord_control_channel_id is None


class TestBothChannelsIndependent:
    """Operations on one channel field don't affect the other."""

    async def test_set_both_channels(self, db):
        await db.create_project(Project(id="p-1", name="Alpha"))

        await db.update_project("p-1", discord_channel_id="111111111111111111")
        await db.update_project("p-1", discord_control_channel_id="222222222222222222")

        project = await db.get_project("p-1")
        assert project.discord_channel_id == "111111111111111111"
        assert project.discord_control_channel_id == "222222222222222222"

    async def test_update_notification_doesnt_affect_control(self, db):
        await db.create_project(Project(id="p-1", name="Alpha"))
        await db.update_project("p-1", discord_channel_id="111111111111111111")
        await db.update_project("p-1", discord_control_channel_id="222222222222222222")

        # Update only notifications
        await db.update_project("p-1", discord_channel_id="999999999999999999")

        project = await db.get_project("p-1")
        assert project.discord_channel_id == "999999999999999999"
        assert project.discord_control_channel_id == "222222222222222222"

    async def test_other_project_fields_preserve_channels(self, db):
        """Updating name/weight shouldn't affect channel fields."""
        await db.create_project(Project(id="p-1", name="Alpha"))
        await db.update_project("p-1", discord_channel_id="111111111111111111")
        await db.update_project("p-1", discord_control_channel_id="222222222222222222")

        await db.update_project("p-1", name="Alpha Updated", credit_weight=5.0)

        project = await db.get_project("p-1")
        assert project.name == "Alpha Updated"
        assert project.credit_weight == 5.0
        assert project.discord_channel_id == "111111111111111111"
        assert project.discord_control_channel_id == "222222222222222222"
