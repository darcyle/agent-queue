"""Unit tests for task dependency features: _cmd_task_deps handler, batch
dependency queries, and list_tasks with show_dependencies."""

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


# ---------------------------------------------------------------------------
# Batch dependency query tests (get_dependency_map_for_tasks)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_dep_map_empty_list(db):
    """Batch query with no task IDs returns empty dict."""
    result = await db.get_dependency_map_for_tasks([])
    assert result == {}


@pytest.mark.asyncio
async def test_batch_dep_map_no_deps(db):
    """Tasks with no dependencies should have empty lists."""
    await db.create_task(_task("solo-1", title="Solo 1"))
    await db.create_task(_task("solo-2", title="Solo 2"))

    result = await db.get_dependency_map_for_tasks(["solo-1", "solo-2"])
    assert result["solo-1"]["depends_on"] == []
    assert result["solo-1"]["blocks"] == []
    assert result["solo-2"]["depends_on"] == []
    assert result["solo-2"]["blocks"] == []


@pytest.mark.asyncio
async def test_batch_dep_map_upstream_and_downstream(db):
    """Batch query correctly returns upstream and downstream deps."""
    await db.create_task(_task("a", title="Task A"))
    await db.create_task(_task("b", title="Task B"))
    await db.create_task(_task("c", title="Task C"))

    await db.add_dependency("b", "a")  # b depends on a
    await db.add_dependency("c", "b")  # c depends on b

    result = await db.get_dependency_map_for_tasks(["a", "b", "c"])

    # a: no upstream, blocks b
    assert result["a"]["depends_on"] == []
    assert result["a"]["blocks"] == ["b"]

    # b: depends on a, blocks c
    assert len(result["b"]["depends_on"]) == 1
    assert result["b"]["depends_on"][0]["id"] == "a"
    assert result["b"]["blocks"] == ["c"]

    # c: depends on b, blocks nothing
    assert len(result["c"]["depends_on"]) == 1
    assert result["c"]["depends_on"][0]["id"] == "b"
    assert result["c"]["blocks"] == []


@pytest.mark.asyncio
async def test_batch_dep_map_includes_status(db):
    """Batch query includes task status for upstream dependencies."""
    await db.create_task(_task("done", title="Done Task"))
    await db.update_task("done", status=TaskStatus.COMPLETED)
    await db.create_task(_task("pending", title="Pending Task"))
    await db.add_dependency("pending", "done")

    result = await db.get_dependency_map_for_tasks(["pending"])
    assert result["pending"]["depends_on"][0]["status"] == "COMPLETED"


# ---------------------------------------------------------------------------
# list_tasks with show_dependencies (end-to-end via command handler)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tasks_show_dependencies(handler, db):
    """list_tasks with show_dependencies=True should include dep data."""
    await db.create_task(_task("parent", title="Parent"))
    await db.create_task(_task("child", title="Child"))
    await db.add_dependency("child", "parent")

    result = await handler.execute("list_tasks", {
        "project_id": PROJECT_ID,
        "show_dependencies": True,
    })
    assert "error" not in result
    tasks_by_id = {t["id"]: t for t in result["tasks"]}

    # parent blocks child
    assert "child" in tasks_by_id["parent"]["blocks"]
    # child depends on parent
    assert len(tasks_by_id["child"]["depends_on"]) == 1
    assert tasks_by_id["child"]["depends_on"][0]["id"] == "parent"


@pytest.mark.asyncio
async def test_list_tasks_no_dependencies_flag(handler, db):
    """list_tasks without show_dependencies should not include dep data."""
    await db.create_task(_task("t1", title="Task 1"))

    result = await handler.execute("list_tasks", {
        "project_id": PROJECT_ID,
    })
    assert "error" not in result
    task = result["tasks"][0]
    assert "depends_on" not in task
    assert "blocks" not in task


@pytest.mark.asyncio
async def test_list_tasks_no_project_context(handler, db):
    """list_tasks without project_id returns all tasks."""
    await db.create_task(_task("t1", title="Task 1"))

    result = await handler.execute("list_tasks", {})
    assert "error" not in result
    assert result["total"] >= 1
