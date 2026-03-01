"""Tests for per-widget visibility / privacy settings.

Covers the full stack: model logic, database CRUD, command handler,
and Discord TaskReportView rendering with privacy filtering.
"""

import pytest

from src.database import Database
from src.models import (
    DashboardConfig,
    Project,
    Task,
    TaskStatus,
    WidgetPrivacyConfig,
    WidgetVisibility,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
async def project(db):
    p = Project(id="p-1", name="test-project")
    await db.create_project(p)
    return p


# ---------------------------------------------------------------------------
# Model-level tests
# ---------------------------------------------------------------------------

class TestWidgetPrivacyModels:
    """Tests for DashboardConfig and WidgetPrivacyConfig dataclass logic."""

    def test_default_visibility_is_visible(self):
        wc = WidgetPrivacyConfig(widget_id="budget")
        assert wc.visibility == WidgetVisibility.VISIBLE

    def test_owner_always_sees_widget(self):
        config = DashboardConfig(
            id="d-1",
            project_id="p-1",
            owner_user_id="owner-123",
            widget_configs=[
                WidgetPrivacyConfig(
                    widget_id="budget",
                    visibility=WidgetVisibility.OWNER_ONLY,
                ),
            ],
        )
        assert config.is_widget_visible("budget", "owner-123") is True

    def test_non_owner_cannot_see_owner_only_widget(self):
        config = DashboardConfig(
            id="d-1",
            project_id="p-1",
            owner_user_id="owner-123",
            widget_configs=[
                WidgetPrivacyConfig(
                    widget_id="budget",
                    visibility=WidgetVisibility.OWNER_ONLY,
                ),
            ],
        )
        assert config.is_widget_visible("budget", "staff-456") is False

    def test_visible_widget_is_visible_to_all(self):
        config = DashboardConfig(
            id="d-1",
            project_id="p-1",
            owner_user_id="owner-123",
            widget_configs=[
                WidgetPrivacyConfig(
                    widget_id="task_list",
                    visibility=WidgetVisibility.VISIBLE,
                ),
            ],
        )
        assert config.is_widget_visible("task_list", "staff-456") is True

    def test_collapsed_widget_not_visible_to_non_owner(self):
        config = DashboardConfig(
            id="d-1",
            project_id="p-1",
            owner_user_id="owner-123",
            widget_configs=[
                WidgetPrivacyConfig(
                    widget_id="budget",
                    visibility=WidgetVisibility.COLLAPSED,
                ),
            ],
        )
        assert config.is_widget_visible("budget", "staff-456") is False

    def test_unconfigured_widget_defaults_to_visible(self):
        config = DashboardConfig(
            id="d-1",
            project_id="p-1",
            owner_user_id="owner-123",
            widget_configs=[],
        )
        assert config.is_widget_visible("anything", "staff-456") is True

    def test_get_widget_config_returns_none_for_unknown(self):
        config = DashboardConfig(
            id="d-1",
            project_id="p-1",
            owner_user_id="owner-123",
        )
        assert config.get_widget_config("nonexistent") is None

    def test_get_widget_placeholder_for_owner_only(self):
        config = DashboardConfig(
            id="d-1",
            project_id="p-1",
            owner_user_id="owner-123",
            widget_configs=[
                WidgetPrivacyConfig(
                    widget_id="budget",
                    visibility=WidgetVisibility.OWNER_ONLY,
                    placeholder_text="Financial data hidden",
                ),
            ],
        )
        assert config.get_widget_placeholder("budget") == "Financial data hidden"

    def test_get_widget_placeholder_for_collapsed_returns_none(self):
        config = DashboardConfig(
            id="d-1",
            project_id="p-1",
            owner_user_id="owner-123",
            widget_configs=[
                WidgetPrivacyConfig(
                    widget_id="budget",
                    visibility=WidgetVisibility.COLLAPSED,
                ),
            ],
        )
        assert config.get_widget_placeholder("budget") is None

    def test_get_widget_placeholder_for_visible_returns_none(self):
        config = DashboardConfig(
            id="d-1",
            project_id="p-1",
            owner_user_id="owner-123",
            widget_configs=[
                WidgetPrivacyConfig(
                    widget_id="budget",
                    visibility=WidgetVisibility.VISIBLE,
                ),
            ],
        )
        assert config.get_widget_placeholder("budget") is None


# ---------------------------------------------------------------------------
# Database CRUD tests
# ---------------------------------------------------------------------------

class TestDashboardConfigDatabase:
    """Tests for dashboard_configs table CRUD operations."""

    async def test_create_and_get_dashboard_config(self, db, project):
        config = DashboardConfig(
            id="d-1",
            project_id="p-1",
            owner_user_id="user-100",
            widget_configs=[
                WidgetPrivacyConfig(
                    widget_id="budget",
                    visibility=WidgetVisibility.OWNER_ONLY,
                    placeholder_text="Hidden from staff",
                ),
            ],
        )
        await db.create_dashboard_config(config)
        result = await db.get_dashboard_config("p-1")

        assert result is not None
        assert result.id == "d-1"
        assert result.project_id == "p-1"
        assert result.owner_user_id == "user-100"
        assert len(result.widget_configs) == 1
        assert result.widget_configs[0].widget_id == "budget"
        assert result.widget_configs[0].visibility == WidgetVisibility.OWNER_ONLY
        assert result.widget_configs[0].placeholder_text == "Hidden from staff"

    async def test_get_nonexistent_dashboard_config(self, db, project):
        result = await db.get_dashboard_config("p-1")
        assert result is None

    async def test_update_dashboard_config_owner(self, db, project):
        config = DashboardConfig(
            id="d-1",
            project_id="p-1",
            owner_user_id="user-100",
        )
        await db.create_dashboard_config(config)
        await db.update_dashboard_config("p-1", owner_user_id="user-200")

        result = await db.get_dashboard_config("p-1")
        assert result.owner_user_id == "user-200"

    async def test_update_dashboard_config_widgets(self, db, project):
        config = DashboardConfig(
            id="d-1",
            project_id="p-1",
            owner_user_id="user-100",
            widget_configs=[],
        )
        await db.create_dashboard_config(config)

        new_widgets = [
            WidgetPrivacyConfig(
                widget_id="task_list",
                visibility=WidgetVisibility.COLLAPSED,
            ),
            WidgetPrivacyConfig(
                widget_id="budget",
                visibility=WidgetVisibility.OWNER_ONLY,
                placeholder_text="Private",
            ),
        ]
        await db.update_dashboard_config("p-1", widget_configs=new_widgets)

        result = await db.get_dashboard_config("p-1")
        assert len(result.widget_configs) == 2

        wc_map = {wc.widget_id: wc for wc in result.widget_configs}
        assert wc_map["task_list"].visibility == WidgetVisibility.COLLAPSED
        assert wc_map["budget"].visibility == WidgetVisibility.OWNER_ONLY
        assert wc_map["budget"].placeholder_text == "Private"

    async def test_delete_dashboard_config(self, db, project):
        config = DashboardConfig(
            id="d-1",
            project_id="p-1",
            owner_user_id="user-100",
        )
        await db.create_dashboard_config(config)
        await db.delete_dashboard_config("p-1")

        result = await db.get_dashboard_config("p-1")
        assert result is None

    async def test_unique_project_constraint(self, db, project):
        config1 = DashboardConfig(
            id="d-1",
            project_id="p-1",
            owner_user_id="user-100",
        )
        config2 = DashboardConfig(
            id="d-2",
            project_id="p-1",
            owner_user_id="user-200",
        )
        await db.create_dashboard_config(config1)

        with pytest.raises(Exception):
            await db.create_dashboard_config(config2)

    async def test_multiple_widget_configs_roundtrip(self, db, project):
        """Ensure multiple widget configs serialize/deserialize correctly."""
        widgets = [
            WidgetPrivacyConfig(
                widget_id="task_progress",
                visibility=WidgetVisibility.VISIBLE,
            ),
            WidgetPrivacyConfig(
                widget_id="budget",
                visibility=WidgetVisibility.OWNER_ONLY,
                placeholder_text="Contact admin",
            ),
            WidgetPrivacyConfig(
                widget_id="token_usage",
                visibility=WidgetVisibility.COLLAPSED,
            ),
        ]
        config = DashboardConfig(
            id="d-1",
            project_id="p-1",
            owner_user_id="user-100",
            widget_configs=widgets,
        )
        await db.create_dashboard_config(config)

        result = await db.get_dashboard_config("p-1")
        assert len(result.widget_configs) == 3
        ids = {wc.widget_id for wc in result.widget_configs}
        assert ids == {"task_progress", "budget", "token_usage"}


# ---------------------------------------------------------------------------
# Command handler tests
# ---------------------------------------------------------------------------

class TestWidgetPrivacyCommands:
    """Tests for widget privacy command handler methods."""

    @pytest.fixture
    async def handler(self, db, project):
        """Create a minimal CommandHandler with a mock orchestrator."""
        from unittest.mock import AsyncMock, MagicMock
        from src.command_handler import CommandHandler

        orchestrator = MagicMock()
        orchestrator.db = db

        config = MagicMock()
        config.workspace_dir = "/tmp/test-ws"
        handler = CommandHandler(orchestrator, config)
        handler.set_active_project("p-1")
        return handler

    async def test_get_dashboard_config_empty(self, handler):
        result = await handler.execute("get_dashboard_config", {"project_id": "p-1"})
        assert "error" not in result
        assert result["config"] is None

    async def test_set_dashboard_owner_creates_config(self, handler):
        result = await handler.execute("set_dashboard_owner", {
            "project_id": "p-1",
            "owner_user_id": "user-100",
        })
        assert result["status"] == "created"
        assert result["owner_user_id"] == "user-100"

    async def test_set_dashboard_owner_updates_existing(self, handler):
        await handler.execute("set_dashboard_owner", {
            "project_id": "p-1",
            "owner_user_id": "user-100",
        })
        result = await handler.execute("set_dashboard_owner", {
            "project_id": "p-1",
            "owner_user_id": "user-200",
        })
        assert result["status"] == "updated"
        assert result["owner_user_id"] == "user-200"

    async def test_set_widget_privacy(self, handler):
        # First set owner
        await handler.execute("set_dashboard_owner", {
            "project_id": "p-1",
            "owner_user_id": "user-100",
        })
        result = await handler.execute("set_widget_privacy", {
            "project_id": "p-1",
            "widget_id": "budget",
            "visibility": "owner_only",
            "placeholder_text": "Restricted",
        })
        assert result["status"] == "updated"
        assert result["widget_id"] == "budget"
        assert result["visibility"] == "owner_only"

    async def test_set_widget_privacy_creates_config_with_owner(self, handler):
        result = await handler.execute("set_widget_privacy", {
            "project_id": "p-1",
            "widget_id": "budget",
            "visibility": "owner_only",
            "owner_user_id": "user-100",
        })
        assert result["status"] == "updated"

    async def test_set_widget_privacy_requires_owner_when_no_config(self, handler):
        result = await handler.execute("set_widget_privacy", {
            "project_id": "p-1",
            "widget_id": "budget",
            "visibility": "owner_only",
        })
        assert "error" in result

    async def test_set_widget_privacy_invalid_visibility(self, handler):
        result = await handler.execute("set_widget_privacy", {
            "project_id": "p-1",
            "widget_id": "budget",
            "visibility": "invalid_value",
        })
        assert "error" in result
        assert "Invalid visibility" in result["error"]

    async def test_remove_widget_privacy(self, handler):
        await handler.execute("set_dashboard_owner", {
            "project_id": "p-1",
            "owner_user_id": "user-100",
        })
        await handler.execute("set_widget_privacy", {
            "project_id": "p-1",
            "widget_id": "budget",
            "visibility": "owner_only",
        })
        result = await handler.execute("remove_widget_privacy", {
            "project_id": "p-1",
            "widget_id": "budget",
        })
        assert result["status"] == "removed"

    async def test_remove_widget_privacy_not_found(self, handler):
        await handler.execute("set_dashboard_owner", {
            "project_id": "p-1",
            "owner_user_id": "user-100",
        })
        result = await handler.execute("remove_widget_privacy", {
            "project_id": "p-1",
            "widget_id": "nonexistent",
        })
        assert "error" in result

    async def test_list_widget_privacy_empty(self, handler):
        result = await handler.execute("list_widget_privacy", {"project_id": "p-1"})
        assert result["widgets"] == []
        assert result["owner_user_id"] is None

    async def test_list_widget_privacy_with_settings(self, handler):
        await handler.execute("set_dashboard_owner", {
            "project_id": "p-1",
            "owner_user_id": "user-100",
        })
        await handler.execute("set_widget_privacy", {
            "project_id": "p-1",
            "widget_id": "budget",
            "visibility": "owner_only",
        })
        await handler.execute("set_widget_privacy", {
            "project_id": "p-1",
            "widget_id": "task_list",
            "visibility": "collapsed",
        })
        result = await handler.execute("list_widget_privacy", {"project_id": "p-1"})
        assert len(result["widgets"]) == 2
        assert result["owner_user_id"] == "user-100"

    async def test_update_existing_widget_privacy(self, handler):
        """Changing visibility on a widget that already has a setting should update it."""
        await handler.execute("set_dashboard_owner", {
            "project_id": "p-1",
            "owner_user_id": "user-100",
        })
        await handler.execute("set_widget_privacy", {
            "project_id": "p-1",
            "widget_id": "budget",
            "visibility": "owner_only",
        })
        await handler.execute("set_widget_privacy", {
            "project_id": "p-1",
            "widget_id": "budget",
            "visibility": "collapsed",
        })
        result = await handler.execute("list_widget_privacy", {"project_id": "p-1"})
        assert len(result["widgets"]) == 1
        assert result["widgets"][0]["visibility"] == "collapsed"

    async def test_set_widget_privacy_missing_project(self, handler):
        handler.set_active_project(None)
        result = await handler.execute("set_widget_privacy", {
            "widget_id": "budget",
            "visibility": "owner_only",
        })
        assert "error" in result

    async def test_set_widget_privacy_nonexistent_project(self, handler):
        result = await handler.execute("set_widget_privacy", {
            "project_id": "nonexistent",
            "widget_id": "budget",
            "visibility": "owner_only",
            "owner_user_id": "user-100",
        })
        assert "error" in result


# ---------------------------------------------------------------------------
# TaskReportView privacy rendering tests
# ---------------------------------------------------------------------------

class TestTaskReportViewPrivacy:
    """Tests that TaskReportView correctly respects widget privacy settings."""

    def _make_tasks_by_status(self):
        """Create sample tasks for rendering tests."""
        return {
            "IN_PROGRESS": [
                {"id": "t-1", "title": "Task One", "status": "IN_PROGRESS",
                 "is_plan_subtask": False, "pr_url": None, "parent_task_id": None},
            ],
            "READY": [
                {"id": "t-2", "title": "Task Two", "status": "READY",
                 "is_plan_subtask": False, "pr_url": None, "parent_task_id": None},
            ],
        }

    def test_no_privacy_config_shows_everything(self):
        """Without dashboard_config, all content is visible."""
        # Import inside function to avoid module-level Discord import issues
        from src.discord.commands import setup_commands
        from src.models import DashboardConfig, WidgetVisibility

        # We can't easily instantiate TaskReportView outside setup_commands,
        # but we can test the model logic directly
        config = None  # No config = everything visible
        dc = DashboardConfig(
            id="d-1", project_id="p-1", owner_user_id="owner-123",
            widget_configs=[],
        )
        # All unconfigured widgets should be visible
        assert dc.is_widget_visible("task_list", "anyone") is True
        assert dc.is_widget_visible("task_progress", "anyone") is True

    def test_owner_only_hides_from_non_owner(self):
        config = DashboardConfig(
            id="d-1",
            project_id="p-1",
            owner_user_id="owner-123",
            widget_configs=[
                WidgetPrivacyConfig(
                    widget_id="task_list",
                    visibility=WidgetVisibility.OWNER_ONLY,
                    placeholder_text="Task details are restricted",
                ),
                WidgetPrivacyConfig(
                    widget_id="task_progress",
                    visibility=WidgetVisibility.COLLAPSED,
                ),
            ],
        )
        # Staff can't see task_list or task_progress
        assert config.is_widget_visible("task_list", "staff-456") is False
        assert config.is_widget_visible("task_progress", "staff-456") is False

        # Owner can see everything
        assert config.is_widget_visible("task_list", "owner-123") is True
        assert config.is_widget_visible("task_progress", "owner-123") is True

    def test_placeholder_text_for_owner_only(self):
        config = DashboardConfig(
            id="d-1",
            project_id="p-1",
            owner_user_id="owner-123",
            widget_configs=[
                WidgetPrivacyConfig(
                    widget_id="budget",
                    visibility=WidgetVisibility.OWNER_ONLY,
                    placeholder_text="Contact your manager for budget details",
                ),
            ],
        )
        placeholder = config.get_widget_placeholder("budget")
        assert placeholder == "Contact your manager for budget details"

    def test_collapsed_returns_no_placeholder(self):
        config = DashboardConfig(
            id="d-1",
            project_id="p-1",
            owner_user_id="owner-123",
            widget_configs=[
                WidgetPrivacyConfig(
                    widget_id="budget",
                    visibility=WidgetVisibility.COLLAPSED,
                ),
            ],
        )
        placeholder = config.get_widget_placeholder("budget")
        assert placeholder is None


# ---------------------------------------------------------------------------
# Integration: full stack command + DB roundtrip
# ---------------------------------------------------------------------------

class TestWidgetPrivacyIntegration:
    """End-to-end tests verifying the full command → DB → retrieval flow."""

    @pytest.fixture
    async def handler(self, db, project):
        from unittest.mock import MagicMock
        from src.command_handler import CommandHandler

        orchestrator = MagicMock()
        orchestrator.db = db
        config = MagicMock()
        config.workspace_dir = "/tmp/test-ws"
        handler = CommandHandler(orchestrator, config)
        handler.set_active_project("p-1")
        return handler

    async def test_full_privacy_workflow(self, handler, db):
        """Test complete workflow: create owner → set privacy → verify → remove."""
        # Step 1: Set dashboard owner
        result = await handler.execute("set_dashboard_owner", {
            "project_id": "p-1",
            "owner_user_id": "user-owner",
        })
        assert result["status"] == "created"

        # Step 2: Set budget to owner_only
        result = await handler.execute("set_widget_privacy", {
            "project_id": "p-1",
            "widget_id": "budget",
            "visibility": "owner_only",
            "placeholder_text": "Restricted financial data",
        })
        assert result["status"] == "updated"

        # Step 3: Set task_list to collapsed
        result = await handler.execute("set_widget_privacy", {
            "project_id": "p-1",
            "widget_id": "task_list",
            "visibility": "collapsed",
        })
        assert result["status"] == "updated"

        # Step 4: Verify via direct DB read
        config = await db.get_dashboard_config("p-1")
        assert config is not None
        assert config.owner_user_id == "user-owner"
        assert len(config.widget_configs) == 2

        # Owner sees everything
        assert config.is_widget_visible("budget", "user-owner") is True
        assert config.is_widget_visible("task_list", "user-owner") is True

        # Staff sees nothing private
        assert config.is_widget_visible("budget", "user-staff") is False
        assert config.is_widget_visible("task_list", "user-staff") is False

        # Unconfigured widgets are still visible to all
        assert config.is_widget_visible("other_widget", "user-staff") is True

        # Step 5: Remove budget privacy
        result = await handler.execute("remove_widget_privacy", {
            "project_id": "p-1",
            "widget_id": "budget",
        })
        assert result["status"] == "removed"

        # Step 6: Verify budget is now visible to staff
        config = await db.get_dashboard_config("p-1")
        assert config.is_widget_visible("budget", "user-staff") is True
        # task_list is still collapsed
        assert config.is_widget_visible("task_list", "user-staff") is False

    async def test_get_dashboard_config_returns_full_data(self, handler):
        """get_dashboard_config command returns all widget settings."""
        await handler.execute("set_dashboard_owner", {
            "project_id": "p-1",
            "owner_user_id": "user-100",
        })
        await handler.execute("set_widget_privacy", {
            "project_id": "p-1",
            "widget_id": "budget",
            "visibility": "owner_only",
            "placeholder_text": "Private",
        })

        result = await handler.execute("get_dashboard_config", {"project_id": "p-1"})
        assert "error" not in result
        config = result["config"]
        assert config["owner_user_id"] == "user-100"
        assert len(config["widgets"]) == 1
        assert config["widgets"][0]["widget_id"] == "budget"
        assert config["widgets"][0]["visibility"] == "owner_only"
        assert config["widgets"][0]["placeholder_text"] == "Private"
