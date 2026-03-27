"""Tests for the agent-queue MCP server.

Tests cover:
- MCP protocol compliance (tool listing, resource listing, prompt listing)
- Resource reads return correct data
- Tool calls execute and return expected results
- Error handling for missing entities
- Serialization helpers
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

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


class TestMCPToolListing:
    """Test that the MCP server exposes the expected tools."""

    async def test_tools_are_registered(self, mcp_server):
        tools = await mcp_server.list_tools()
        tool_names = {t.name for t in tools}

        # Task management tools
        assert "create_task" in tool_names
        assert "stop_task" in tool_names
        assert "restart_task" in tool_names
        assert "reopen_task" in tool_names
        assert "approve_task" in tool_names
        assert "reject_task" in tool_names
        assert "get_task_details" in tool_names

        # Project management tools
        assert "pause_project" in tool_names
        assert "resume_project" in tool_names
        assert "list_projects" in tool_names

        # Dependency tools
        assert "add_dependency" in tool_names
        assert "remove_dependency" in tool_names
        assert "get_dependencies" in tool_names

        # Workspace tools
        assert "list_workspaces" in tool_names
        assert "find_merge_conflicts" in tool_names

        # Monitoring tools
        assert "get_chain_health" in tool_names
        assert "get_recent_events" in tool_names
        assert "get_system_status" in tool_names

        # Agent tools
        assert "list_agents" in tool_names

        # Profile tools
        assert "list_profiles" in tool_names
        assert "get_profile_details" in tool_names

    async def test_tools_have_descriptions(self, mcp_server):
        tools = await mcp_server.list_tools()
        for tool in tools:
            assert tool.description, f"Tool {tool.name} has no description"


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


class TestMCPToolCalls:
    """Test actual tool execution via the MCP server."""

    async def test_get_system_status(self, mcp_server):
        result = await mcp_server.call_tool("get_system_status", {})
        # FastMCP returns a list of content blocks
        assert len(result) > 0
        data = json.loads(result[0][0].text)
        assert "projects" in data
        assert "tasks" in data
        assert "agents" in data
        assert data["projects"]["total"] == 1
        assert data["agents"]["total"] == 1

    async def test_list_projects_tool(self, mcp_server):
        result = await mcp_server.call_tool("list_projects", {})
        data = json.loads(result[0][0].text)
        assert len(data) == 1
        assert data[0]["id"] == "test-project"

    async def test_get_task_details(self, mcp_server):
        result = await mcp_server.call_tool("get_task_details", {"task_id": "task-001"})
        data = json.loads(result[0][0].text)
        assert data["id"] == "task-001"
        assert data["title"] == "Implement feature X"
        assert "task-002" in data["dependencies"]

    async def test_get_task_details_not_found(self, mcp_server):
        result = await mcp_server.call_tool("get_task_details", {"task_id": "nonexistent"})
        data = json.loads(result[0][0].text)
        assert "error" in data

    async def test_create_task(self, mcp_server):
        result = await mcp_server.call_tool("create_task", {
            "project_id": "test-project",
            "title": "New MCP task",
            "description": "Created via MCP",
            "priority": 200,
            "task_type": "feature",
        })
        data = json.loads(result[0][0].text)
        assert "task_id" in data
        assert "created successfully" in data["message"]

    async def test_create_task_invalid_project(self, mcp_server):
        result = await mcp_server.call_tool("create_task", {
            "project_id": "nonexistent",
            "title": "Bad task",
            "description": "Should fail",
        })
        data = json.loads(result[0][0].text)
        assert "error" in data

    async def test_create_task_invalid_type(self, mcp_server):
        result = await mcp_server.call_tool("create_task", {
            "project_id": "test-project",
            "title": "Bad type",
            "description": "Should fail",
            "task_type": "invalid_type",
        })
        data = json.loads(result[0][0].text)
        assert "error" in data

    async def test_stop_task(self, mcp_server):
        result = await mcp_server.call_tool("stop_task", {"task_id": "task-002"})
        data = json.loads(result[0][0].text)
        assert "stopped" in data["message"]

    async def test_restart_task(self, mcp_server):
        result = await mcp_server.call_tool("restart_task", {"task_id": "task-002"})
        data = json.loads(result[0][0].text)
        assert "restarted" in data["message"]

    async def test_approve_task(self, mcp_server):
        result = await mcp_server.call_tool("approve_task", {"task_id": "task-003"})
        data = json.loads(result[0][0].text)
        assert "approved" in data["message"]

    async def test_approve_task_wrong_status(self, mcp_server):
        result = await mcp_server.call_tool("approve_task", {"task_id": "task-001"})
        data = json.loads(result[0][0].text)
        assert "error" in data
        assert "not awaiting approval" in data["error"]

    async def test_reject_task(self, mcp_server):
        result = await mcp_server.call_tool("reject_task", {
            "task_id": "task-003",
            "reason": "Needs more tests",
        })
        data = json.loads(result[0][0].text)
        assert "rejected" in data["message"]

    async def test_reopen_task(self, mcp_server):
        result = await mcp_server.call_tool("reopen_task", {
            "task_id": "task-003",
            "feedback": "Please add error handling",
        })
        data = json.loads(result[0][0].text)
        assert "reopened" in data["message"]

    async def test_pause_project(self, mcp_server):
        result = await mcp_server.call_tool("pause_project", {"project_id": "test-project"})
        data = json.loads(result[0][0].text)
        assert "paused" in data["message"]

    async def test_resume_project(self, mcp_server):
        result = await mcp_server.call_tool("resume_project", {"project_id": "test-project"})
        data = json.loads(result[0][0].text)
        assert "resumed" in data["message"]

    async def test_get_dependencies(self, mcp_server):
        result = await mcp_server.call_tool("get_dependencies", {"task_id": "task-001"})
        data = json.loads(result[0][0].text)
        assert "task-002" in data["dependencies"]
        assert data["all_met"] is False  # task-002 is IN_PROGRESS

    async def test_add_dependency(self, mcp_server):
        result = await mcp_server.call_tool("add_dependency", {
            "task_id": "task-003",
            "depends_on": "task-002",
        })
        data = json.loads(result[0][0].text)
        assert "Dependency added" in data["message"]

    async def test_remove_dependency(self, mcp_server):
        result = await mcp_server.call_tool("remove_dependency", {
            "task_id": "task-001",
            "depends_on": "task-002",
        })
        data = json.loads(result[0][0].text)
        assert "removed" in data["message"]

    async def test_list_workspaces(self, mcp_server):
        result = await mcp_server.call_tool("list_workspaces", {})
        data = json.loads(result[0][0].text)
        assert isinstance(data, list)

    async def test_get_chain_health(self, mcp_server):
        result = await mcp_server.call_tool("get_chain_health", {})
        data = json.loads(result[0][0].text)
        assert "total_tasks" in data
        assert "in_progress" in data
        assert data["total_tasks"] == 3

    async def test_get_recent_events(self, mcp_server):
        result = await mcp_server.call_tool("get_recent_events", {"limit": 5})
        data = json.loads(result[0][0].text)
        assert isinstance(data, list)

    async def test_list_agents(self, mcp_server):
        result = await mcp_server.call_tool("list_agents", {})
        data = json.loads(result[0][0].text)
        assert len(data) == 1
        assert data[0]["id"] == "agent-1"

    async def test_list_agents_by_state(self, mcp_server):
        result = await mcp_server.call_tool("list_agents", {"state": "BUSY"})
        data = json.loads(result[0][0].text)
        assert len(data) == 1

    async def test_list_agents_invalid_state(self, mcp_server):
        result = await mcp_server.call_tool("list_agents", {"state": "INVALID"})
        data = json.loads(result[0][0].text)
        assert "error" in data

    async def test_list_profiles(self, mcp_server):
        result = await mcp_server.call_tool("list_profiles", {})
        data = json.loads(result[0][0].text)
        assert len(data) == 1
        assert data[0]["id"] == "reviewer"

    async def test_get_profile_details(self, mcp_server):
        result = await mcp_server.call_tool("get_profile_details", {"profile_id": "reviewer"})
        data = json.loads(result[0][0].text)
        assert data["id"] == "reviewer"
        assert data["model"] == "claude-sonnet-4-20250514"

    async def test_get_profile_not_found(self, mcp_server):
        result = await mcp_server.call_tool("get_profile_details", {"profile_id": "nonexistent"})
        data = json.loads(result[0][0].text)
        assert "error" in data

    async def test_find_merge_conflicts(self, mcp_server):
        result = await mcp_server.call_tool("find_merge_conflicts", {})
        data = json.loads(result[0][0].text)
        assert "conflicts_found" in data

    async def test_subscribe_events(self, mcp_server):
        result = await mcp_server.call_tool("subscribe_events", {"event_types": "task_created,task_completed"})
        data = json.loads(result[0][0].text)
        assert "subscribed_to" in data
        assert "task_created" in data["subscribed_to"]


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
