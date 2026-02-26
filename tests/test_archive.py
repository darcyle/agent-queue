"""Tests for the task archiving feature.

Covers the database layer (archive, list, restore, delete, auto-archive)
and the command handler layer (archive_tasks, archive_task, list_archived,
restore_task, archive_settings).  Also covers the orchestrator's automatic
archiving logic.
"""

import time

import pytest
from unittest.mock import MagicMock

from src.command_handler import CommandHandler
from src.config import AppConfig, ArchiveConfig, DiscordConfig
from src.database import Database
from src.models import Project, Task, TaskStatus
from src.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


async def _seed_project(db: Database, pid: str = "p-1") -> None:
    """Create a simple project for test tasks to reference."""
    await db.create_project(Project(id=pid, name=f"project-{pid}"))


async def _seed_task(
    db: Database,
    tid: str = "t-1",
    pid: str = "p-1",
    title: str = "Test Task",
    status: TaskStatus = TaskStatus.COMPLETED,
) -> Task:
    """Create and persist a task, returning it."""
    task = Task(
        id=tid, project_id=pid, title=title,
        description=f"Description of {title}", status=status,
    )
    await db.create_task(task)
    return task


# ---------------------------------------------------------------------------
# Database layer tests
# ---------------------------------------------------------------------------

class TestArchiveTask:
    async def test_archive_completed_task(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        result = await db.archive_task("t-1")
        assert result is True

        # Task should be gone from active table
        assert await db.get_task("t-1") is None

        # Task should exist in archive
        archived = await db.get_archived_task("t-1")
        assert archived is not None
        assert archived["id"] == "t-1"
        assert archived["title"] == "Test Task"
        assert archived["status"] == "COMPLETED"
        assert archived["archived_at"] > 0

    async def test_archive_nonexistent_task_returns_false(self, db):
        result = await db.archive_task("no-such-task")
        assert result is False

    async def test_archive_preserves_task_fields(self, db):
        await _seed_project(db)
        task = Task(
            id="t-full", project_id="p-1", title="Full Task",
            description="Lots of details", priority=42,
            status=TaskStatus.COMPLETED, max_retries=5,
            branch_name="feature/foo", pr_url="https://github.com/pr/123",
        )
        await db.create_task(task)
        await db.archive_task("t-full")

        archived = await db.get_archived_task("t-full")
        assert archived["priority"] == 42
        assert archived["max_retries"] == 5
        assert archived["branch_name"] == "feature/foo"
        assert archived["pr_url"] == "https://github.com/pr/123"
        assert archived["description"] == "Lots of details"


class TestArchiveCompletedTasks:
    async def test_archive_all_completed_for_project(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-2", status=TaskStatus.COMPLETED, title="Task 2")
        await _seed_task(db, "t-3", status=TaskStatus.READY, title="Active")

        archived_ids = await db.archive_completed_tasks(project_id="p-1")
        assert set(archived_ids) == {"t-1", "t-2"}

        # Active task should still be there
        assert await db.get_task("t-3") is not None

        # Archived tasks should be gone from active
        assert await db.get_task("t-1") is None
        assert await db.get_task("t-2") is None

        # Both should appear in archive
        archive = await db.list_archived_tasks(project_id="p-1")
        assert len(archive) == 2

    async def test_archive_completed_across_projects(self, db):
        await _seed_project(db, "p-1")
        await _seed_project(db, "p-2")
        await _seed_task(db, "t-1", pid="p-1", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-2", pid="p-2", status=TaskStatus.COMPLETED)

        archived_ids = await db.archive_completed_tasks()
        assert set(archived_ids) == {"t-1", "t-2"}

    async def test_archive_returns_empty_when_no_completed(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.READY)

        archived_ids = await db.archive_completed_tasks(project_id="p-1")
        assert archived_ids == []


class TestListArchivedTasks:
    async def test_list_archived_tasks_empty(self, db):
        tasks = await db.list_archived_tasks()
        assert tasks == []

    async def test_list_archived_tasks_by_project(self, db):
        await _seed_project(db, "p-1")
        await _seed_project(db, "p-2")
        await _seed_task(db, "t-1", pid="p-1", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-2", pid="p-2", status=TaskStatus.COMPLETED)
        await db.archive_task("t-1")
        await db.archive_task("t-2")

        p1_archive = await db.list_archived_tasks(project_id="p-1")
        assert len(p1_archive) == 1
        assert p1_archive[0]["id"] == "t-1"

    async def test_list_archived_respects_limit(self, db):
        await _seed_project(db)
        for i in range(5):
            await _seed_task(db, f"t-{i}", title=f"Task {i}", status=TaskStatus.COMPLETED)
        await db.archive_completed_tasks(project_id="p-1")

        limited = await db.list_archived_tasks(limit=2)
        assert len(limited) == 2


class TestCountArchivedTasks:
    async def test_count_archived_tasks(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-2", status=TaskStatus.COMPLETED, title="Task 2")
        await db.archive_completed_tasks(project_id="p-1")

        assert await db.count_archived_tasks() == 2
        assert await db.count_archived_tasks(project_id="p-1") == 2
        assert await db.count_archived_tasks(project_id="p-2") == 0


class TestRestoreArchivedTask:
    async def test_restore_archived_task(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await db.archive_task("t-1")

        # Verify it's archived
        assert await db.get_task("t-1") is None
        assert await db.get_archived_task("t-1") is not None

        # Restore it
        result = await db.restore_archived_task("t-1")
        assert result is True

        # Should be back in active with DEFINED status
        task = await db.get_task("t-1")
        assert task is not None
        assert task.status == TaskStatus.DEFINED
        assert task.title == "Test Task"
        assert task.retry_count == 0  # reset

        # Should be gone from archive
        assert await db.get_archived_task("t-1") is None

    async def test_restore_nonexistent_returns_false(self, db):
        result = await db.restore_archived_task("no-such-task")
        assert result is False


class TestDeleteArchivedTask:
    async def test_delete_archived_task(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await db.archive_task("t-1")

        result = await db.delete_archived_task("t-1")
        assert result is True

        # Should be gone from everywhere
        assert await db.get_task("t-1") is None
        assert await db.get_archived_task("t-1") is None

    async def test_delete_nonexistent_archived_returns_false(self, db):
        result = await db.delete_archived_task("no-such-task")
        assert result is False


# ---------------------------------------------------------------------------
# Command handler tests
# ---------------------------------------------------------------------------

class TestArchiveCommands:
    """Integration tests for archive commands through the CommandHandler.

    Uses a real Orchestrator with a real database wired in.
    """

    @pytest.fixture
    async def handler(self, db, tmp_path):
        """Create a CommandHandler with a real database."""
        config = AppConfig(
            discord=DiscordConfig(bot_token="test-token", guild_id="123"),
            workspace_dir=str(tmp_path / "workspaces"),
            database_path=str(tmp_path / "test.db"),
        )
        orchestrator = Orchestrator(config)
        orchestrator.db = db
        orchestrator.git = MagicMock()
        return CommandHandler(orchestrator, config)

    async def test_archive_tasks_command(self, handler, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-2", status=TaskStatus.COMPLETED, title="Task 2")

        result = await handler.execute("archive_tasks", {"project_id": "p-1"})
        assert "error" not in result
        assert result["archived_count"] == 2
        assert set(result["archived_ids"]) == {"t-1", "t-2"}

    async def test_archive_tasks_nothing_to_archive(self, handler, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.READY)

        result = await handler.execute("archive_tasks", {"project_id": "p-1"})
        assert "message" in result
        assert "No completed tasks" in result["message"]

    async def test_archive_single_task(self, handler, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        result = await handler.execute("archive_task", {"task_id": "t-1"})
        assert "error" not in result
        assert result["archived"] == "t-1"
        assert result["title"] == "Test Task"

    async def test_archive_single_task_active_rejected(self, handler, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.IN_PROGRESS)

        result = await handler.execute("archive_task", {"task_id": "t-1"})
        assert "error" in result
        assert "IN_PROGRESS" in result["error"]

    async def test_archive_single_task_not_found(self, handler, db):
        result = await handler.execute("archive_task", {"task_id": "nope"})
        assert "error" in result
        assert "not found" in result["error"]

    async def test_archive_failed_task(self, handler, db):
        """Failed and blocked tasks can also be archived."""
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.FAILED)

        result = await handler.execute("archive_task", {"task_id": "t-1"})
        assert "error" not in result
        assert result["status"] == "FAILED"

    async def test_archive_blocked_task(self, handler, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.BLOCKED)

        result = await handler.execute("archive_task", {"task_id": "t-1"})
        assert "error" not in result
        assert result["status"] == "BLOCKED"

    async def test_list_archived_command(self, handler, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await db.archive_task("t-1")

        result = await handler.execute("list_archived", {"project_id": "p-1"})
        assert "error" not in result
        assert result["count"] == 1
        assert result["total"] == 1
        assert result["tasks"][0]["id"] == "t-1"

    async def test_list_archived_empty(self, handler, db):
        result = await handler.execute("list_archived", {})
        assert result["count"] == 0
        assert result["tasks"] == []

    async def test_restore_task_command(self, handler, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await db.archive_task("t-1")

        result = await handler.execute("restore_task", {"task_id": "t-1"})
        assert "error" not in result
        assert result["restored"] == "t-1"
        assert result["new_status"] == "DEFINED"

        # Task should be back in active
        task = await db.get_task("t-1")
        assert task is not None
        assert task.status == TaskStatus.DEFINED

    async def test_restore_task_not_found(self, handler, db):
        result = await handler.execute("restore_task", {"task_id": "nope"})
        assert "error" in result
        assert "not found" in result["error"]

    async def test_archive_settings_command(self, handler, db):
        result = await handler.execute("archive_settings", {})
        assert "error" not in result
        assert result["enabled"] is True
        assert result["after_hours"] == 24.0
        assert "COMPLETED" in result["statuses"]
        assert result["archived_count"] == 0

    async def test_archive_settings_with_archived_tasks(self, handler, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await db.archive_task("t-1")

        result = await handler.execute("archive_settings", {})
        assert result["archived_count"] == 1


# ---------------------------------------------------------------------------
# Database: archive_old_terminal_tasks tests
# ---------------------------------------------------------------------------

class TestArchiveOldTerminalTasks:
    async def test_archive_old_completed_tasks(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        # Manually backdate updated_at to simulate an old task
        old_time = time.time() - 86400 * 2  # 2 days ago
        await db._db.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?", (old_time, "t-1"),
        )
        await db._db.commit()

        archived_ids = await db.archive_old_terminal_tasks(
            statuses=["COMPLETED"], older_than_seconds=3600,
        )
        assert archived_ids == ["t-1"]
        assert await db.get_task("t-1") is None
        assert await db.get_archived_task("t-1") is not None

    async def test_recent_tasks_not_archived(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        # Task was just created — should not be archived with a 1-hour threshold
        archived_ids = await db.archive_old_terminal_tasks(
            statuses=["COMPLETED"], older_than_seconds=3600,
        )
        assert archived_ids == []
        assert await db.get_task("t-1") is not None

    async def test_only_matching_statuses_archived(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-2", status=TaskStatus.FAILED, title="Failed")
        await _seed_task(db, "t-3", status=TaskStatus.READY, title="Active")

        # Backdate all tasks
        old_time = time.time() - 86400
        for tid in ("t-1", "t-2", "t-3"):
            await db._db.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?", (old_time, tid),
            )
        await db._db.commit()

        # Only archive COMPLETED (not FAILED or READY)
        archived_ids = await db.archive_old_terminal_tasks(
            statuses=["COMPLETED"], older_than_seconds=3600,
        )
        assert archived_ids == ["t-1"]
        assert await db.get_task("t-2") is not None  # FAILED still active
        assert await db.get_task("t-3") is not None  # READY still active

    async def test_multiple_statuses_archived(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)
        await _seed_task(db, "t-2", status=TaskStatus.FAILED, title="Failed")
        await _seed_task(db, "t-3", status=TaskStatus.BLOCKED, title="Blocked")

        old_time = time.time() - 86400
        for tid in ("t-1", "t-2", "t-3"):
            await db._db.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?", (old_time, tid),
            )
        await db._db.commit()

        archived_ids = await db.archive_old_terminal_tasks(
            statuses=["COMPLETED", "FAILED", "BLOCKED"],
            older_than_seconds=3600,
        )
        assert set(archived_ids) == {"t-1", "t-2", "t-3"}

    async def test_empty_statuses_returns_empty(self, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        archived_ids = await db.archive_old_terminal_tasks(
            statuses=[], older_than_seconds=0,
        )
        assert archived_ids == []


# ---------------------------------------------------------------------------
# Orchestrator auto-archive tests
# ---------------------------------------------------------------------------

class TestAutoArchive:
    @pytest.fixture
    async def orchestrator(self, db, tmp_path):
        config = AppConfig(
            discord=DiscordConfig(bot_token="test-token", guild_id="123"),
            workspace_dir=str(tmp_path / "workspaces"),
            database_path=str(tmp_path / "test.db"),
            archive=ArchiveConfig(
                enabled=True,
                after_hours=1.0,
                statuses=["COMPLETED", "FAILED", "BLOCKED"],
            ),
        )
        orch = Orchestrator(config)
        orch.db = db
        orch.git = MagicMock()
        return orch

    async def test_auto_archive_archives_old_tasks(self, orchestrator, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        # Backdate task to 2 hours ago
        old_time = time.time() - 7200
        await db._db.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?", (old_time, "t-1"),
        )
        await db._db.commit()

        # Force _last_auto_archive to 0 so it runs immediately
        orchestrator._last_auto_archive = 0.0
        await orchestrator._auto_archive_tasks()

        assert await db.get_task("t-1") is None
        assert await db.get_archived_task("t-1") is not None

    async def test_auto_archive_skips_recent_tasks(self, orchestrator, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        orchestrator._last_auto_archive = 0.0
        await orchestrator._auto_archive_tasks()

        # Task was just created — still in active table
        assert await db.get_task("t-1") is not None

    async def test_auto_archive_respects_rate_limit(self, orchestrator, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        old_time = time.time() - 7200
        await db._db.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?", (old_time, "t-1"),
        )
        await db._db.commit()

        # Set _last_auto_archive to now — should skip
        orchestrator._last_auto_archive = time.time()
        await orchestrator._auto_archive_tasks()

        # Task should still be active because rate limit prevented archiving
        assert await db.get_task("t-1") is not None

    async def test_auto_archive_disabled(self, orchestrator, db):
        await _seed_project(db)
        await _seed_task(db, "t-1", status=TaskStatus.COMPLETED)

        old_time = time.time() - 86400
        await db._db.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?", (old_time, "t-1"),
        )
        await db._db.commit()

        orchestrator.config.archive.enabled = False
        orchestrator._last_auto_archive = 0.0
        await orchestrator._auto_archive_tasks()

        # Task should still be active because auto-archive is disabled
        assert await db.get_task("t-1") is not None
