import asyncio
import os

import pytest
from src.orchestrator import Orchestrator
from src.database import Database
from src.models import (
    Project, Task, Agent, TaskStatus, AgentState, AgentResult,
    TaskContext, AgentOutput, RepoConfig, RepoSourceType,
)
from src.adapters.base import AgentAdapter, MessageCallback
from src.config import AppConfig, AutoTaskConfig


class MockAdapter(AgentAdapter):
    def __init__(self, result=AgentResult.COMPLETED, tokens=1000):
        self._result = result
        self._tokens = tokens

    async def start(self, task): pass

    async def wait(self, on_message: MessageCallback | None = None):
        return AgentOutput(result=self._result, summary="Done",
                           tokens_used=self._tokens)

    async def stop(self): pass
    async def is_alive(self): return True


class MockAdapterFactory:
    def __init__(self, result=AgentResult.COMPLETED, tokens=1000):
        self.result = result
        self.tokens = tokens

    def create(self, agent_type: str) -> AgentAdapter:
        return MockAdapter(result=self.result, tokens=self.tokens)


async def _drain_running_tasks(orch: Orchestrator) -> None:
    """Wait for all background tasks launched by the orchestrator to complete.

    ``run_one_cycle`` launches ``_execute_task_safe`` as background
    ``asyncio.Task`` objects.  Tests must await these before asserting on
    final task status, otherwise there is a race between the background
    coroutine and the assertions.
    """
    if orch._running_tasks:
        await asyncio.gather(*orch._running_tasks.values(), return_exceptions=True)
        orch._running_tasks.clear()


@pytest.fixture
async def orch(tmp_path):
    config = AppConfig(
        database_path=str(tmp_path / "test.db"),
        workspace_dir=str(tmp_path / "workspaces"),
    )
    o = Orchestrator(config, adapter_factory=MockAdapterFactory())
    await o.initialize()
    yield o
    # Drain any remaining background tasks before closing DB
    await _drain_running_tasks(o)
    await o.shutdown()


class TestOrchestratorLifecycle:
    async def test_full_task_lifecycle(self, orch):
        """DEFINED → READY → ASSIGNED → IN_PROGRESS → VERIFYING → COMPLETED"""
        await orch.db.create_project(Project(id="p-1", name="alpha"))
        await orch.db.create_agent(Agent(id="a-1", name="claude-1",
                                         agent_type="claude"))
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Test",
            description="Do it", status=TaskStatus.READY,
        ))

        await orch.run_one_cycle()
        await _drain_running_tasks(orch)

        task = await orch.db.get_task("t-1")
        assert task.status == TaskStatus.COMPLETED

    async def test_failed_task_retries(self, orch):
        orch._adapter_factory = MockAdapterFactory(result=AgentResult.FAILED)
        await orch.db.create_project(Project(id="p-1", name="alpha"))
        await orch.db.create_agent(Agent(id="a-1", name="claude-1",
                                         agent_type="claude"))
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Test",
            description="Do it", status=TaskStatus.READY,
            max_retries=2,
        ))

        await orch.run_one_cycle()
        await _drain_running_tasks(orch)

        task = await orch.db.get_task("t-1")
        # Should be READY for retry (failed once, max 2)
        assert task.status == TaskStatus.READY
        assert task.retry_count == 1

    async def test_paused_on_token_exhaustion(self, orch):
        orch._adapter_factory = MockAdapterFactory(
            result=AgentResult.PAUSED_TOKENS
        )
        await orch.db.create_project(Project(id="p-1", name="alpha"))
        await orch.db.create_agent(Agent(id="a-1", name="claude-1",
                                         agent_type="claude"))
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Test",
            description="Do it", status=TaskStatus.READY,
        ))

        await orch.run_one_cycle()
        await _drain_running_tasks(orch)

        task = await orch.db.get_task("t-1")
        assert task.status == TaskStatus.PAUSED
        assert task.resume_after is not None

    async def test_dependencies_block_scheduling(self, orch):
        await orch.db.create_project(Project(id="p-1", name="alpha"))
        await orch.db.create_agent(Agent(id="a-1", name="claude-1",
                                         agent_type="claude"))
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="First",
            description="Do first", status=TaskStatus.DEFINED,
        ))
        await orch.db.create_task(Task(
            id="t-2", project_id="p-1", title="Second",
            description="Do second", status=TaskStatus.DEFINED,
        ))
        await orch.db.add_dependency("t-2", depends_on="t-1")

        # t-1 has no deps so it gets promoted to READY and executed.
        # t-2 depends on t-1 which is not yet COMPLETED at scheduling time,
        # so it stays DEFINED until the next cycle.
        await orch.run_one_cycle()
        await _drain_running_tasks(orch)

        t1 = await orch.db.get_task("t-1")
        t2 = await orch.db.get_task("t-2")
        # t-1 was promoted, scheduled, executed, completed
        assert t1.status == TaskStatus.COMPLETED
        # t-2 stays DEFINED because t-1 wasn't completed when deps were checked
        assert t2.status == TaskStatus.DEFINED


class TestAutoTaskGeneration:
    """Tests for auto-generating tasks from implementation plan files."""

    @pytest.fixture
    async def orch_with_workspace(self, tmp_path):
        workspace = tmp_path / "workspaces"
        workspace.mkdir()
        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(workspace),
        )
        o = Orchestrator(config, adapter_factory=MockAdapterFactory())
        await o.initialize()
        yield o, workspace
        await _drain_running_tasks(o)
        await o.shutdown()

    async def test_generates_tasks_from_plan_on_completion(self, orch_with_workspace):
        """When a completed task has a plan file in workspace, subtasks are created."""
        orch, workspace = orch_with_workspace

        # Create plan file in workspace
        claude_dir = workspace / ".claude"
        claude_dir.mkdir()
        plan_file = claude_dir / "plan.md"
        plan_file.write_text("""# Implementation Plan

## Add database models

Create the User and Post models in models.py.

## Build API endpoints

Add REST endpoints for CRUD operations.

## Write tests

Add comprehensive test suite.
""")

        await orch.db.create_project(Project(id="p-1", name="alpha"))
        await orch.db.create_agent(Agent(id="a-1", name="claude-1",
                                         agent_type="claude"))
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Plan Feature",
            description="Create implementation plan",
            status=TaskStatus.READY,
        ))

        await orch.run_one_cycle()
        await _drain_running_tasks(orch)

        # Original task should be completed
        task = await orch.db.get_task("t-1")
        assert task.status == TaskStatus.COMPLETED

        # Should have created 3 subtasks
        subtasks = await orch.db.get_subtasks("t-1")
        assert len(subtasks) == 3

        titles = [t.title for t in subtasks]
        assert "Add database models" in titles
        assert "Build API endpoints" in titles
        assert "Write tests" in titles

        # Subtasks should be in DEFINED status
        for st in subtasks:
            assert st.status == TaskStatus.DEFINED
            assert st.parent_task_id == "t-1"
            assert st.project_id == "p-1"

    async def test_plan_tasks_have_chained_dependencies(self, orch_with_workspace):
        """Generated tasks should depend on the previous step."""
        orch, workspace = orch_with_workspace

        claude_dir = workspace / ".claude"
        claude_dir.mkdir()
        (claude_dir / "plan.md").write_text("""## Step A

First.

## Step B

Second.

## Step C

Third.
""")

        await orch.db.create_project(Project(id="p-1", name="alpha"))
        await orch.db.create_agent(Agent(id="a-1", name="claude-1",
                                         agent_type="claude"))
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Plan",
            description="Plan it", status=TaskStatus.READY,
        ))

        await orch.run_one_cycle()
        await _drain_running_tasks(orch)

        subtasks = await orch.db.get_subtasks("t-1")
        assert len(subtasks) == 3

        # Sort by priority_hint (priority field)
        subtasks.sort(key=lambda t: t.priority)

        # First task should have no dependencies
        deps_a = await orch.db.get_dependencies(subtasks[0].id)
        assert len(deps_a) == 0

        # Second task should depend on first
        deps_b = await orch.db.get_dependencies(subtasks[1].id)
        assert subtasks[0].id in deps_b

        # Third task should depend on second
        deps_c = await orch.db.get_dependencies(subtasks[2].id)
        assert subtasks[1].id in deps_c

    async def test_no_tasks_generated_when_no_plan_file(self, orch_with_workspace):
        """When no plan file exists, no subtasks should be created."""
        orch, workspace = orch_with_workspace

        await orch.db.create_project(Project(id="p-1", name="alpha"))
        await orch.db.create_agent(Agent(id="a-1", name="claude-1",
                                         agent_type="claude"))
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Normal task",
            description="Just do it", status=TaskStatus.READY,
        ))

        await orch.run_one_cycle()
        await _drain_running_tasks(orch)

        task = await orch.db.get_task("t-1")
        assert task.status == TaskStatus.COMPLETED

        subtasks = await orch.db.get_subtasks("t-1")
        assert len(subtasks) == 0

    async def test_auto_task_disabled_by_config(self, tmp_path):
        """When auto_task.enabled is False, no subtasks are generated."""
        workspace = tmp_path / "workspaces"
        workspace.mkdir()

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(workspace),
            auto_task=AutoTaskConfig(enabled=False),
        )
        orch = Orchestrator(config, adapter_factory=MockAdapterFactory())
        await orch.initialize()

        # Create a plan file
        claude_dir = workspace / ".claude"
        claude_dir.mkdir()
        (claude_dir / "plan.md").write_text("## Step\n\nContent.\n")

        await orch.db.create_project(Project(id="p-1", name="alpha"))
        await orch.db.create_agent(Agent(id="a-1", name="claude-1",
                                         agent_type="claude"))
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Plan",
            description="Plan it", status=TaskStatus.READY,
        ))

        await orch.run_one_cycle()
        await _drain_running_tasks(orch)

        subtasks = await orch.db.get_subtasks("t-1")
        assert len(subtasks) == 0

        await orch.shutdown()

    async def test_plan_file_is_cleaned_up(self, orch_with_workspace):
        """Plan file should be removed after tasks are generated."""
        orch, workspace = orch_with_workspace

        claude_dir = workspace / ".claude"
        claude_dir.mkdir()
        plan_path = claude_dir / "plan.md"
        plan_path.write_text("## Task\n\nDo something.\n")

        await orch.db.create_project(Project(id="p-1", name="alpha"))
        await orch.db.create_agent(Agent(id="a-1", name="claude-1",
                                         agent_type="claude"))
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Plan",
            description="Plan it", status=TaskStatus.READY,
        ))

        await orch.run_one_cycle()
        await _drain_running_tasks(orch)

        # Plan file should be deleted
        assert not plan_path.exists()

    async def test_subtasks_inherit_repo_id(self, orch_with_workspace):
        """When inherit_repo is True, subtasks should have the parent's repo_id."""
        orch, workspace = orch_with_workspace

        # Place the plan file directly in the workspace root (simulating
        # an agent that wrote a plan in its checkout directory).
        claude_dir = workspace / ".claude"
        claude_dir.mkdir()
        (claude_dir / "plan.md").write_text("## Build it\n\nDo the build.\n")

        await orch.db.create_project(Project(id="p-1", name="alpha"))
        await orch.db.create_repo(RepoConfig(
            id="repo-1", project_id="p-1",
            source_type=RepoSourceType.INIT,
            url="", default_branch="main",
            checkout_base_path=str(workspace),
        ))

        # Create the parent task with repo_id set
        parent = Task(
            id="t-1", project_id="p-1", title="Plan",
            description="Plan it", status=TaskStatus.COMPLETED,
            repo_id="repo-1",
        )
        await orch.db.create_task(parent)

        # Call _generate_tasks_from_plan directly to test repo inheritance
        # without going through the full execution flow (which would
        # create a different workspace path for the repo checkout).
        generated = await orch._generate_tasks_from_plan(parent, str(workspace))

        assert len(generated) == 1
        assert generated[0].repo_id == "repo-1"

        # Verify the subtask was persisted in the database
        subtasks = await orch.db.get_subtasks("t-1")
        assert len(subtasks) == 1
        assert subtasks[0].repo_id == "repo-1"

    async def test_subtask_descriptions_are_self_contained(self, orch_with_workspace):
        """Generated task descriptions should include context from parent and plan."""
        orch, workspace = orch_with_workspace

        claude_dir = workspace / ".claude"
        claude_dir.mkdir()
        (claude_dir / "plan.md").write_text("""# Refactoring Plan

This plan covers the auth system refactor.

## Update password hashing

Switch from MD5 to bcrypt.

## Add session management

Implement JWT-based sessions.
""")

        await orch.db.create_project(Project(id="p-1", name="alpha"))
        await orch.db.create_agent(Agent(id="a-1", name="claude-1",
                                         agent_type="claude"))
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Auth Refactor",
            description="Refactor the authentication system",
            status=TaskStatus.READY,
        ))

        await orch.run_one_cycle()
        await _drain_running_tasks(orch)

        subtasks = await orch.db.get_subtasks("t-1")
        assert len(subtasks) == 2

        # Check that descriptions are self-contained
        for st in subtasks:
            # Should reference parent task
            assert "Auth Refactor" in st.description
            # Should have task details
            assert "Task Details" in st.description

    async def test_no_dependencies_when_chain_disabled(self, tmp_path):
        """When chain_dependencies is False, no deps between generated tasks."""
        workspace = tmp_path / "workspaces"
        workspace.mkdir()

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(workspace),
            auto_task=AutoTaskConfig(chain_dependencies=False),
        )
        orch = Orchestrator(config, adapter_factory=MockAdapterFactory())
        await orch.initialize()

        claude_dir = workspace / ".claude"
        claude_dir.mkdir()
        (claude_dir / "plan.md").write_text("## A\n\nFirst.\n\n## B\n\nSecond.\n")

        await orch.db.create_project(Project(id="p-1", name="alpha"))
        await orch.db.create_agent(Agent(id="a-1", name="claude-1",
                                         agent_type="claude"))
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Plan",
            description="Plan it", status=TaskStatus.READY,
        ))

        await orch.run_one_cycle()
        await _drain_running_tasks(orch)

        subtasks = await orch.db.get_subtasks("t-1")
        assert len(subtasks) == 2

        for st in subtasks:
            deps = await orch.db.get_dependencies(st.id)
            assert len(deps) == 0

        await orch.shutdown()

    async def test_numbered_list_plan_generates_tasks(self, orch_with_workspace):
        """Plans using numbered lists should also generate subtasks."""
        orch, workspace = orch_with_workspace

        claude_dir = workspace / ".claude"
        claude_dir.mkdir()
        (claude_dir / "plan.md").write_text("""1. Set up project scaffolding
   - Create directory structure
   - Initialize package.json

2. Implement core module
   - Add main logic
   - Add error handling

3. Add tests and documentation
""")

        await orch.db.create_project(Project(id="p-1", name="alpha"))
        await orch.db.create_agent(Agent(id="a-1", name="claude-1",
                                         agent_type="claude"))
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Build Feature",
            description="Build the feature",
            status=TaskStatus.READY,
        ))

        await orch.run_one_cycle()
        await _drain_running_tasks(orch)

        subtasks = await orch.db.get_subtasks("t-1")
        assert len(subtasks) == 3

        titles = [t.title for t in subtasks]
        assert "Set up project scaffolding" in titles
        assert "Implement core module" in titles
        assert "Add tests and documentation" in titles

    async def test_generated_tasks_promote_through_dependency_chain(
        self, orch_with_workspace
    ):
        """After auto-generation, running more cycles should promote tasks
        through the dependency chain as each completes."""
        orch, workspace = orch_with_workspace

        claude_dir = workspace / ".claude"
        claude_dir.mkdir()
        (claude_dir / "plan.md").write_text("""## First step

Do the first thing.

## Second step

Do the second thing.
""")

        await orch.db.create_project(Project(id="p-1", name="alpha"))
        await orch.db.create_agent(Agent(id="a-1", name="claude-1",
                                         agent_type="claude"))
        await orch.db.create_task(Task(
            id="t-1", project_id="p-1", title="Plan",
            description="Plan it", status=TaskStatus.READY,
        ))

        # First cycle: execute t-1, generate subtasks
        await orch.run_one_cycle()
        await _drain_running_tasks(orch)

        subtasks = await orch.db.get_subtasks("t-1")
        assert len(subtasks) == 2
        subtasks.sort(key=lambda t: t.priority)

        # Second cycle: first subtask should be promoted to READY and executed
        await orch.run_one_cycle()
        await _drain_running_tasks(orch)

        st1 = await orch.db.get_task(subtasks[0].id)
        assert st1.status == TaskStatus.COMPLETED

        # Third cycle: second subtask should now have deps met
        await orch.run_one_cycle()
        await _drain_running_tasks(orch)

        st2 = await orch.db.get_task(subtasks[1].id)
        assert st2.status == TaskStatus.COMPLETED
