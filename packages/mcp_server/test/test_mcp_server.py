"""Tests for the agent-queue MCP server.

Tests cover:
- MCP protocol compliance (tool listing, resource listing, prompt listing)
- Dynamic tool registration from _ALL_TOOL_DEFINITIONS
- Excluded commands are not registered
- Resource reads return correct data
- Tool calls delegate to CommandHandler.execute()
- Error handling for missing entities
- Serialization helpers
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock

import pytest

# Ensure project root is on the path
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.database import Database
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Project,
    ProjectStatus,
    RepoSourceType,
    Task,
    TaskStatus,
    TaskType,
    Workspace,
)
from src.tool_registry import _ALL_TOOL_DEFINITIONS
from packages.mcp_server.mcp_interfaces import (
    ResourceScheme,
    agent_to_dict,
    profile_to_dict,
    project_to_dict,
    task_to_dict,
    workspace_to_dict,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def db(tmp_path):
    """Create a fresh in-memory database for each test."""
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
async def populated_db(db):
    """Database pre-populated with test data."""
    # Create a project
    project = Project(
        id="test-project",
        name="Test Project",
        credit_weight=1.0,
        max_concurrent_agents=2,
        status=ProjectStatus.ACTIVE,
        repo_url="https://github.com/test/repo",
        repo_default_branch="main",
    )
    await db.create_project(project)

    # Create agent first (tasks may reference it via assigned_agent_id FK)
    agent = Agent(
        id="agent-1",
        name="Claude Agent 1",
        agent_type="claude",
        state=AgentState.BUSY,
        current_task_id=None,  # will be set after task creation
    )
    await db.create_agent(agent)

    # Create tasks
    task1 = Task(
        id="task-001",
        project_id="test-project",
        title="Implement feature X",
        description="Build the X feature with tests",
        priority=100,
        status=TaskStatus.READY,
        task_type=TaskType.FEATURE,
    )
    task2 = Task(
        id="task-002",
        project_id="test-project",
        title="Fix bug Y",
        description="Fix the Y bug in module Z",
        priority=50,
        status=TaskStatus.IN_PROGRESS,
        task_type=TaskType.BUGFIX,
        assigned_agent_id="agent-1",
    )
    task3 = Task(
        id="task-003",
        project_id="test-project",
        title="Awaiting approval",
        description="Needs review",
        status=TaskStatus.AWAITING_APPROVAL,
        requires_approval=True,
    )
    await db.create_task(task1)
    await db.create_task(task2)
    await db.create_task(task3)

    # Create dependency
    await db.add_dependency("task-001", "task-002")

    # Update agent with current task
    await db.update_agent("agent-1", current_task_id="task-002")

    # Create profile
    profile = AgentProfile(
        id="reviewer",
        name="Code Reviewer",
        description="Reviews pull requests",
        model="claude-sonnet-4-20250514",
        permission_mode="plan",
        allowed_tools=["Read", "Grep", "Glob"],
    )
    await db.create_profile(profile)

    yield db


# ---------------------------------------------------------------------------
# Serialization helper tests
# ---------------------------------------------------------------------------

class TestSerializationHelpers:
    """Test the model-to-dict serialization functions."""

    def test_task_to_dict(self):
        task = Task(
            id="t1",
            project_id="p1",
            title="Test task",
            description="A test",
            priority=100,
            status=TaskStatus.READY,
            task_type=TaskType.FEATURE,
        )
        d = task_to_dict(task)
        assert d["id"] == "t1"
        assert d["status"] == "READY"
        assert d["task_type"] == "feature"
        assert d["priority"] == 100

    def test_task_to_dict_no_type(self):
        task = Task(id="t1", project_id="p1", title="Test", description="", task_type=None)
        d = task_to_dict(task)
        assert d["task_type"] is None

    def test_project_to_dict(self):
        project = Project(
            id="p1",
            name="Proj",
            status=ProjectStatus.ACTIVE,
            credit_weight=2.0,
        )
        d = project_to_dict(project)
        assert d["id"] == "p1"
        assert d["status"] == "ACTIVE"
        assert d["credit_weight"] == 2.0

    def test_agent_to_dict(self):
        agent = Agent(
            id="a1",
            name="Agent 1",
            agent_type="claude",
            state=AgentState.BUSY,
            current_task_id="t1",
        )
        d = agent_to_dict(agent)
        assert d["id"] == "a1"
        assert d["state"] == "BUSY"
        assert d["current_task_id"] == "t1"

    def test_profile_to_dict(self):
        profile = AgentProfile(
            id="dev",
            name="Developer",
            allowed_tools=["Read", "Write"],
            mcp_servers={"test": {"command": "npx test"}},
        )
        d = profile_to_dict(profile)
        assert d["id"] == "dev"
        assert d["allowed_tools"] == ["Read", "Write"]
        assert "test" in d["mcp_servers"]

    def test_workspace_to_dict(self):
        ws = Workspace(
            id="ws1",
            project_id="p1",
            workspace_path="/tmp/ws",
            source_type=RepoSourceType.LINK,
        )
        d = workspace_to_dict(ws)
        assert d["id"] == "ws1"
        assert d["source_type"] == "link"


# ---------------------------------------------------------------------------
# Resource URI scheme tests
# ---------------------------------------------------------------------------

class TestResourceSchemes:
    def test_uri_schemes(self):
        assert ResourceScheme.TASK == "agentqueue://tasks"
        assert ResourceScheme.PROJECT == "agentqueue://projects"
        assert ResourceScheme.AGENT == "agentqueue://agents"
        assert ResourceScheme.EVENT == "agentqueue://events"
        assert ResourceScheme.PROFILE == "agentqueue://profiles"
        assert ResourceScheme.WORKSPACE == "agentqueue://workspaces"


# ---------------------------------------------------------------------------
# MCP server integration tests (using FastMCP's call_tool / read_resource)
# ---------------------------------------------------------------------------

@pytest.fixture
async def mcp_server(populated_db, tmp_path, monkeypatch):
    """Create and configure a FastMCP server instance with test database."""
    from src.event_bus import EventBus
    import packages.mcp_server.mcp_server as mcp_mod

    test_bus = EventBus()

    # Patch the helpers that retrieve db/event_bus from MCP context
    async def _mock_get_db():
        return populated_db

    async def _mock_get_event_bus():
        return test_bus

    monkeypatch.setattr(mcp_mod, "_get_db", _mock_get_db)
    monkeypatch.setattr(mcp_mod, "_get_event_bus", _mock_get_event_bus)

    yield mcp_mod.mcp


@pytest.fixture
async def mcp_server_with_handler(populated_db, tmp_path, monkeypatch):
    """MCP server with a mock CommandHandler for testing tool calls."""
    from src.event_bus import EventBus
    import packages.mcp_server.mcp_server as mcp_mod

    test_bus = EventBus()

    async def _mock_get_db():
        return populated_db

    async def _mock_get_event_bus():
        return test_bus

    # Create a mock command handler
    mock_handler = AsyncMock()

    async def _mock_get_command_handler():
        return mock_handler

    monkeypatch.setattr(mcp_mod, "_get_db", _mock_get_db)
    monkeypatch.setattr(mcp_mod, "_get_event_bus", _mock_get_event_bus)
    monkeypatch.setattr(mcp_mod, "_get_command_handler", _mock_get_command_handler)

    yield mcp_mod.mcp, mock_handler


class TestDynamicToolRegistration:
    """Test that tools are dynamically registered from _ALL_TOOL_DEFINITIONS."""

    async def test_all_non_excluded_tools_registered(self, mcp_server):
        """Every tool in _ALL_TOOL_DEFINITIONS that isn't excluded should be registered."""
        from packages.mcp_server.mcp_server import DEFAULT_EXCLUDED_COMMANDS

        tools = await mcp_server.list_tools()
        tool_names = {t.name for t in tools}

        for defn in _ALL_TOOL_DEFINITIONS:
            name = defn["name"]
            if name in DEFAULT_EXCLUDED_COMMANDS:
                assert name not in tool_names, f"Excluded command {name} should NOT be registered"
            else:
                assert name in tool_names, f"Command {name} should be registered as MCP tool"

    async def test_excluded_commands_not_registered(self, mcp_server):
        """Commands in DEFAULT_EXCLUDED_COMMANDS should not appear as MCP tools."""
        from packages.mcp_server.mcp_server import DEFAULT_EXCLUDED_COMMANDS

        tools = await mcp_server.list_tools()
        tool_names = {t.name for t in tools}

        for cmd in DEFAULT_EXCLUDED_COMMANDS:
            assert cmd not in tool_names, f"Excluded command {cmd} should not be an MCP tool"

    async def test_tools_have_descriptions(self, mcp_server):
        tools = await mcp_server.list_tools()
        for tool in tools:
            assert tool.description, f"Tool {tool.name} has no description"

    async def test_tool_schemas_match_registry(self, mcp_server):
        """Each registered tool should use the input_schema from _ALL_TOOL_DEFINITIONS."""
        tools = await mcp_server.list_tools()
        tool_map = {t.name: t for t in tools}

        # Build a map of first-seen definitions (duplicates are skipped by registration)
        seen: set[str] = set()
        first_defs: dict[str, dict] = {}
        for defn in _ALL_TOOL_DEFINITIONS:
            name = defn["name"]
            if name not in seen:
                first_defs[name] = defn
                seen.add(name)

        for name, defn in first_defs.items():
            if name not in tool_map:
                continue  # excluded
            expected_schema = defn.get("input_schema", {"type": "object", "properties": {}})
            actual_schema = tool_map[name].inputSchema
            assert actual_schema == expected_schema, (
                f"Schema mismatch for {name}: expected {expected_schema}, got {actual_schema}"
            )

    async def test_core_tools_present(self, mcp_server):
        """Spot-check that key tools from the registry are registered."""
        tools = await mcp_server.list_tools()
        tool_names = {t.name for t in tools}

        # Task management
        assert "create_task" in tool_names
        assert "stop_task" in tool_names
        assert "restart_task" in tool_names
        assert "approve_task" in tool_names

        # Project management
        assert "pause_project" in tool_names
        assert "resume_project" in tool_names
        assert "list_projects" in tool_names

        # Dependencies
        assert "add_dependency" in tool_names
        assert "remove_dependency" in tool_names

        # Workspaces
        assert "list_workspaces" in tool_names

        # Agents
        assert "list_agents" in tool_names

    async def test_dangerous_commands_excluded(self, mcp_server):
        """Dangerous commands should be excluded by default."""
        tools = await mcp_server.list_tools()
        tool_names = {t.name for t in tools}

        assert "shutdown" not in tool_names
        assert "restart_daemon" not in tool_names
        assert "update_and_restart" not in tool_names
        assert "run_command" not in tool_names

    async def test_meta_tools_excluded(self, mcp_server):
        """LLM context management meta-tools should be excluded."""
        tools = await mcp_server.list_tools()
        tool_names = {t.name for t in tools}

        assert "browse_tools" not in tool_names
        assert "load_tools" not in tool_names


class TestMCPToolCalls:
    """Test that tool calls delegate to CommandHandler.execute()."""

    async def test_tool_delegates_to_command_handler(self, mcp_server_with_handler):
        """Calling an MCP tool should call command_handler.execute()."""
        server, mock_handler = mcp_server_with_handler
        mock_handler.execute.return_value = {"success": True, "projects": []}

        result = await server.call_tool("list_projects", {})
        data = json.loads(result[0].text)

        mock_handler.execute.assert_called_once_with("list_projects", {})
        assert data["success"] is True

    async def test_tool_passes_arguments(self, mcp_server_with_handler):
        """Arguments should be forwarded to command_handler.execute()."""
        server, mock_handler = mcp_server_with_handler
        mock_handler.execute.return_value = {"success": True, "task": {"id": "new-task"}}

        await server.call_tool("create_task", {
            "project_id": "test-project",
            "title": "New task",
            "description": "Test",
        })

        mock_handler.execute.assert_called_once_with("create_task", {
            "project_id": "test-project",
            "title": "New task",
            "description": "Test",
        })

    async def test_tool_returns_json(self, mcp_server_with_handler):
        """Tool results should be JSON-serialized."""
        server, mock_handler = mcp_server_with_handler
        mock_handler.execute.return_value = {
            "success": True,
            "message": "Task paused",
        }

        result = await server.call_tool("pause_project", {"project_id": "p1"})
        data = json.loads(result[0].text)

        assert data["success"] is True
        assert data["message"] == "Task paused"

    async def test_tool_handles_error_response(self, mcp_server_with_handler):
        """Error dicts from CommandHandler should be returned as-is."""
        server, mock_handler = mcp_server_with_handler
        mock_handler.execute.return_value = {"error": "Project not found: bad-id"}

        result = await server.call_tool("pause_project", {"project_id": "bad-id"})
        data = json.loads(result[0].text)

        assert "error" in data
        assert "not found" in data["error"]


class TestMCPResourceListing:
    """Test that resources are properly registered."""

    async def test_resources_registered(self, mcp_server):
        resources = await mcp_server.list_resources()
        uris = {str(r.uri) for r in resources}

        assert "agentqueue://tasks" in uris
        assert "agentqueue://tasks/active" in uris
        assert "agentqueue://projects" in uris
        assert "agentqueue://agents" in uris
        assert "agentqueue://agents/active" in uris
        assert "agentqueue://profiles" in uris
        assert "agentqueue://events/recent" in uris
        assert "agentqueue://workspaces" in uris


class TestMCPPromptListing:
    """Test that prompts are registered."""

    async def test_prompts_registered(self, mcp_server):
        prompts = await mcp_server.list_prompts()
        prompt_names = {p.name for p in prompts}

        assert "create_task_prompt" in prompt_names
        assert "review_task_prompt" in prompt_names
        assert "project_overview_prompt" in prompt_names


class TestMCPResourceReads:
    """Test reading resources via the MCP server."""

    async def test_read_all_tasks(self, mcp_server):
        contents = await mcp_server.read_resource("agentqueue://tasks")
        data = json.loads(contents[0].content)
        assert len(data) == 3

    async def test_read_active_tasks(self, mcp_server):
        contents = await mcp_server.read_resource("agentqueue://tasks/active")
        data = json.loads(contents[0].content)
        # READY and IN_PROGRESS tasks
        assert len(data) == 2

    async def test_read_projects(self, mcp_server):
        contents = await mcp_server.read_resource("agentqueue://projects")
        data = json.loads(contents[0].content)
        assert len(data) == 1
        assert data[0]["id"] == "test-project"

    async def test_read_agents(self, mcp_server):
        contents = await mcp_server.read_resource("agentqueue://agents")
        data = json.loads(contents[0].content)
        assert len(data) == 1

    async def test_read_profiles(self, mcp_server):
        contents = await mcp_server.read_resource("agentqueue://profiles")
        data = json.loads(contents[0].content)
        assert len(data) == 1

    async def test_read_recent_events(self, mcp_server):
        contents = await mcp_server.read_resource("agentqueue://events/recent")
        data = json.loads(contents[0].content)
        assert isinstance(data, list)


class TestMCPPrompts:
    """Test prompt rendering."""

    async def test_create_task_prompt(self, mcp_server):
        result = await mcp_server.get_prompt("create_task_prompt", {
            "project_id": "test-project",
            "task_type": "bugfix",
            "context": "Users reporting 500 errors",
        })
        assert result.messages
        text = result.messages[0].content.text
        assert "Test Project" in text
        assert "bugfix" in text
        assert "500 errors" in text

    async def test_review_task_prompt(self, mcp_server):
        result = await mcp_server.get_prompt("review_task_prompt", {
            "task_id": "task-001",
        })
        assert result.messages
        text = result.messages[0].content.text
        assert "Implement feature X" in text
        assert "task-001" in text

    async def test_review_task_prompt_not_found(self, mcp_server):
        result = await mcp_server.get_prompt("review_task_prompt", {
            "task_id": "nonexistent",
        })
        assert result.messages
        text = result.messages[0].content.text
        assert "not found" in text

    async def test_project_overview_prompt(self, mcp_server):
        result = await mcp_server.get_prompt("project_overview_prompt", {
            "project_id": "test-project",
        })
        assert result.messages
        text = result.messages[0].content.text
        assert "Test Project" in text
        assert "test-project" in text


class TestRegisterCommandTools:
    """Test the register_command_tools function directly."""

    def test_custom_exclusion_set(self):
        """Can pass a custom exclusion set."""
        from mcp.server import FastMCP
        from packages.mcp_server.mcp_server import register_command_tools

        test_mcp = FastMCP(name="test")
        custom_excluded = {"list_projects", "create_task", "shutdown"}
        registered = register_command_tools(test_mcp, excluded=custom_excluded)

        assert "list_projects" not in registered
        assert "create_task" not in registered
        assert "shutdown" not in registered
        # Other tools should be registered
        assert "pause_project" in registered
        assert "list_tasks" in registered

    def test_empty_exclusion_registers_all(self):
        """Empty exclusion set registers every tool."""
        from mcp.server import FastMCP
        from packages.mcp_server.mcp_server import register_command_tools

        test_mcp = FastMCP(name="test")
        registered = register_command_tools(test_mcp, excluded=set())

        all_names = {d["name"] for d in _ALL_TOOL_DEFINITIONS}
        assert set(registered) == all_names
