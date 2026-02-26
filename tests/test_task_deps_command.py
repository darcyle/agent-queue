"""Unit tests for the _cmd_task_deps command handler method."""

import pytest
from unittest.mock import MagicMock

from src.command_handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Project, Task, TaskStatus
from src.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PROJECT_ID = "proj"


def _task(task_id: str, title: str = "", description: str = "d") -> Task:
    """Helper to build a Task dataclass with sensible defaults."""
    return Task(id=task_id, project_id=PROJECT_ID, title=title or task_id, description=description)


@pytest.fixture
async def db(tmp_path):
    """Create a real database for tests."""
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    # Create a project so FK constraints are satisfied
    await database.create_project(Project(id=PROJECT_ID, name="Test Project"))
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
    """Create a CommandHandler with a real database."""
    orchestrator = Orchestrator(config)
    orchestrator.db = db
    orchestrator.git = MagicMock()
    return CommandHandler(orchestrator, config)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_deps_missing_task_id(handler):
    """Should return an error when task_id is empty."""
    result = await handler.execute("task_deps", {"task_id": ""})
    assert "error" in result


@pytest.mark.asyncio
async def test_task_deps_task_not_found(handler):
    """Should return an error when the task does not exist."""
    result = await handler.execute("task_deps", {"task_id": "nonexistent"})
    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_task_deps_no_dependencies(handler, db):
    """A task with no dependencies should return empty lists."""
    await db.create_task(_task("task-1", title="Standalone task"))
    result = await handler.execute("task_deps", {"task_id": "task-1"})
    assert "error" not in result
    assert result["task_id"] == "task-1"
    assert result["title"] == "Standalone task"
    assert result["status"] == "DEFINED"
    assert result["depends_on"] == []
    assert result["blocks"] == []


@pytest.mark.asyncio
async def test_task_deps_with_upstream(handler, db):
    """Should list upstream dependencies (what the task needs)."""
    await db.create_task(_task("dep-1", title="Dependency 1"))
    await db.create_task(_task("dep-2", title="Dependency 2"))
    await db.create_task(_task("main-task", title="Main Task"))

    await db.add_dependency("main-task", "dep-1")
    await db.add_dependency("main-task", "dep-2")

    result = await handler.execute("task_deps", {"task_id": "main-task"})
    assert "error" not in result
    assert len(result["depends_on"]) == 2
    dep_ids = {d["id"] for d in result["depends_on"]}
    assert dep_ids == {"dep-1", "dep-2"}
    # Each entry should have id, title, status
    for dep in result["depends_on"]:
        assert "id" in dep
        assert "title" in dep
        assert "status" in dep


@pytest.mark.asyncio
async def test_task_deps_with_downstream(handler, db):
    """Should list downstream dependents (what the task blocks)."""
    await db.create_task(_task("blocker", title="Blocker"))
    await db.create_task(_task("blocked-1", title="Blocked 1"))
    await db.create_task(_task("blocked-2", title="Blocked 2"))

    await db.add_dependency("blocked-1", "blocker")
    await db.add_dependency("blocked-2", "blocker")

    result = await handler.execute("task_deps", {"task_id": "blocker"})
    assert "error" not in result
    assert len(result["blocks"]) == 2
    block_ids = {b["id"] for b in result["blocks"]}
    assert block_ids == {"blocked-1", "blocked-2"}


@pytest.mark.asyncio
async def test_task_deps_both_directions(handler, db):
    """A task can both depend on and block other tasks."""
    await db.create_task(_task("first", title="First"))
    await db.create_task(_task("middle", title="Middle"))
    await db.create_task(_task("last", title="Last"))

    await db.add_dependency("middle", "first")  # middle depends on first
    await db.add_dependency("last", "middle")    # last depends on middle

    result = await handler.execute("task_deps", {"task_id": "middle"})
    assert "error" not in result
    assert len(result["depends_on"]) == 1
    assert result["depends_on"][0]["id"] == "first"
    assert len(result["blocks"]) == 1
    assert result["blocks"][0]["id"] == "last"


@pytest.mark.asyncio
async def test_task_deps_status_reflected(handler, db):
    """Dependency status should reflect actual task status."""
    await db.create_task(_task("dep-done", title="Done Dep"))
    await db.update_task("dep-done", status=TaskStatus.COMPLETED)
    await db.create_task(_task("main", title="Main"))
    await db.add_dependency("main", "dep-done")

    result = await handler.execute("task_deps", {"task_id": "main"})
    assert result["depends_on"][0]["status"] == "COMPLETED"
