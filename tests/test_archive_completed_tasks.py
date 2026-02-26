"""Tests for the archive_completed_tasks command.

Verifies that completed tasks are moved to the archived_tasks/ directory
in the project workspace as markdown reference notes, and then deleted
from the database.
"""

import os

import pytest
from unittest.mock import MagicMock

from src.command_handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import (
    Agent, AgentOutput, AgentResult, Project, Task, TaskStatus, TaskType,
)
from src.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PROJECT_ID = "test-proj"


def _task(
    task_id: str,
    title: str = "",
    description: str = "Task description",
    status: TaskStatus = TaskStatus.DEFINED,
    **kwargs,
) -> Task:
    return Task(
        id=task_id,
        project_id=PROJECT_ID,
        title=title or task_id,
        description=description,
        status=status,
        **kwargs,
    )


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(
        Project(
            id=PROJECT_ID,
            name="Test Project",
            workspace_path=str(tmp_path / "workspaces" / PROJECT_ID),
        )
    )
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
    orchestrator.git = MagicMock()
    return CommandHandler(orchestrator, config)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_no_completed_tasks(handler, db):
    """When there are no completed tasks, returns an empty list."""
    await db.create_task(_task("t-1", title="Active task"))
    result = await handler.execute(
        "archive_completed_tasks", {"project_id": PROJECT_ID}
    )
    assert "error" not in result
    assert result["archived_count"] == 0
    assert result["archived"] == []


@pytest.mark.asyncio
async def test_archive_completed_tasks_creates_files(handler, db, tmp_path):
    """Completed tasks are archived as markdown files."""
    await db.create_task(_task("t-1", title="Done task", status=TaskStatus.COMPLETED))
    await db.create_task(_task("t-2", title="Also done", status=TaskStatus.COMPLETED))
    # Non-completed task should NOT be archived
    await db.create_task(_task("t-3", title="Still active"))

    result = await handler.execute(
        "archive_completed_tasks", {"project_id": PROJECT_ID}
    )

    assert "error" not in result
    assert result["archived_count"] == 2

    # Verify archive files exist
    archive_dir = result["archive_dir"]
    assert os.path.isdir(archive_dir)
    assert os.path.isfile(os.path.join(archive_dir, "t-1.md"))
    assert os.path.isfile(os.path.join(archive_dir, "t-2.md"))

    # Verify content of archive note
    with open(os.path.join(archive_dir, "t-1.md")) as f:
        content = f.read()
    assert "# Done task" in content
    assert "`t-1`" in content
    assert "COMPLETED" in content


@pytest.mark.asyncio
async def test_archive_removes_tasks_from_database(handler, db):
    """Archived tasks are deleted from the database."""
    await db.create_task(_task("t-1", title="To archive", status=TaskStatus.COMPLETED))
    await db.create_task(_task("t-2", title="Keep active"))

    await handler.execute(
        "archive_completed_tasks", {"project_id": PROJECT_ID}
    )

    # Archived task should be gone
    assert await db.get_task("t-1") is None
    # Active task should remain
    assert await db.get_task("t-2") is not None


@pytest.mark.asyncio
async def test_archive_includes_task_result(handler, db, tmp_path):
    """Archive notes include execution results (summary, files changed, etc.)."""
    await db.create_task(_task("t-1", title="Result task", status=TaskStatus.COMPLETED))
    await db.create_agent(Agent(id="agent-1", name="Test Agent", agent_type="claude"))
    await db.save_task_result(
        "t-1",
        "agent-1",
        AgentOutput(
            result=AgentResult.COMPLETED,
            summary="Implemented the feature successfully",
            files_changed=["src/main.py", "tests/test_main.py"],
            tokens_used=5000,
        ),
    )

    result = await handler.execute(
        "archive_completed_tasks", {"project_id": PROJECT_ID}
    )

    archive_dir = result["archive_dir"]
    with open(os.path.join(archive_dir, "t-1.md")) as f:
        content = f.read()

    assert "Implemented the feature successfully" in content
    assert "`src/main.py`" in content
    assert "`tests/test_main.py`" in content
    assert "5,000" in content


@pytest.mark.asyncio
async def test_archive_include_failed(handler, db):
    """With include_failed=True, FAILED and BLOCKED tasks are also archived."""
    await db.create_task(_task("t-1", status=TaskStatus.COMPLETED))
    await db.create_task(_task("t-2", status=TaskStatus.FAILED))
    await db.create_task(_task("t-3", status=TaskStatus.BLOCKED))
    await db.create_task(_task("t-4", title="Active"))  # DEFINED — should stay

    result = await handler.execute(
        "archive_completed_tasks", {
            "project_id": PROJECT_ID,
            "include_failed": True,
        }
    )

    assert result["archived_count"] == 3
    archived_ids = {a["id"] for a in result["archived"]}
    assert archived_ids == {"t-1", "t-2", "t-3"}

    # All three should be removed from the database
    assert await db.get_task("t-1") is None
    assert await db.get_task("t-2") is None
    assert await db.get_task("t-3") is None
    # Active task should remain
    assert await db.get_task("t-4") is not None


@pytest.mark.asyncio
async def test_archive_nonexistent_project(handler):
    """Archiving for a nonexistent project returns an error."""
    result = await handler.execute(
        "archive_completed_tasks", {"project_id": "no-such-project"}
    )
    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_archive_preserves_metadata(handler, db, tmp_path):
    """Archive notes include task metadata like type, branch, and PR URL."""
    await db.create_task(
        _task(
            "t-meta",
            title="Metadata task",
            status=TaskStatus.COMPLETED,
            task_type=TaskType.FEATURE,
            branch_name="feat/metadata-task",
            pr_url="https://github.com/org/repo/pull/42",
        )
    )

    result = await handler.execute(
        "archive_completed_tasks", {"project_id": PROJECT_ID}
    )

    archive_dir = result["archive_dir"]
    with open(os.path.join(archive_dir, "t-meta.md")) as f:
        content = f.read()

    assert "feature" in content
    assert "`feat/metadata-task`" in content
    assert "https://github.com/org/repo/pull/42" in content


@pytest.mark.asyncio
async def test_archive_preserves_dependencies(handler, db, tmp_path):
    """Archive notes include the task's upstream dependencies."""
    await db.create_task(_task("t-dep-up", status=TaskStatus.COMPLETED))
    await db.create_task(_task("t-dep-down", status=TaskStatus.COMPLETED))
    await db.add_dependency("t-dep-down", "t-dep-up")

    result = await handler.execute(
        "archive_completed_tasks", {"project_id": PROJECT_ID}
    )

    archive_dir = result["archive_dir"]
    with open(os.path.join(archive_dir, "t-dep-down.md")) as f:
        content = f.read()

    assert "`t-dep-up`" in content
    assert "Dependencies" in content


@pytest.mark.asyncio
async def test_archive_uses_active_project_fallback(handler, db):
    """When no project_id is given, falls back to active project."""
    handler.set_active_project(PROJECT_ID)
    await db.create_task(_task("t-1", status=TaskStatus.COMPLETED))

    result = await handler.execute("archive_completed_tasks", {})

    assert "error" not in result
    assert result["archived_count"] == 1
    assert result["project_id"] == PROJECT_ID


@pytest.mark.asyncio
async def test_archive_no_project_id_returns_error(handler):
    """When no project_id is given and no active project is set, returns error."""
    result = await handler.execute("archive_completed_tasks", {})
    assert "error" in result
    assert "project_id" in result["error"]


@pytest.mark.asyncio
async def test_archive_task_without_result(handler, db, tmp_path):
    """Tasks without execution results get archived with a placeholder note."""
    await db.create_task(_task("t-no-result", status=TaskStatus.COMPLETED))

    result = await handler.execute(
        "archive_completed_tasks", {"project_id": PROJECT_ID}
    )

    archive_dir = result["archive_dir"]
    with open(os.path.join(archive_dir, "t-no-result.md")) as f:
        content = f.read()

    assert "No execution result recorded" in content
