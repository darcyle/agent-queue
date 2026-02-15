# Agent Queue Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a lightweight agent orchestration system with deterministic scheduling, multi-agent support, and Discord-based remote control.

**Architecture:** Single Python asyncio process with SQLite persistence, event-driven state machine for task/agent lifecycle, agent adapters abstracting CLI differences, Discord bot for control plane.

**Tech Stack:** Python 3.12+, asyncio, aiosqlite, discord.py, claude-agent-sdk, PyYAML, pytest + pytest-asyncio

---

## Task 1: Project Scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `src/__init__.py`
- Create: `src/main.py` (empty entry point)
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

**Step 1: Create pyproject.toml**

```toml
[project]
name = "agent-queue"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "aiosqlite>=0.20.0",
    "discord.py>=2.3.0",
    "claude-agent-sdk>=0.1.30",
    "pyyaml>=6.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "ruff>=0.4.0",
]

[project.scripts]
agent-queue = "src.main:main"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
target-version = "py312"
line-length = 100
```

**Step 2: Create empty module files**

```python
# src/__init__.py
# (empty)
```

```python
# src/main.py
def main():
    pass

if __name__ == "__main__":
    main()
```

```python
# tests/__init__.py
# (empty)
```

```python
# tests/conftest.py
import pytest
```

**Step 3: Install dependencies**

Run: `cd /Users/jack.kern/Shared/AI/agent-queue && pip install -e ".[dev]"`

**Step 4: Verify pytest runs**

Run: `pytest --co -q`
Expected: "no tests ran" with exit 0 (or 5 for no tests collected — both fine)

**Step 5: Commit**

```bash
git add pyproject.toml src/ tests/
git commit -m "chore: scaffold project with dependencies"
```

---

## Task 2: Models & Enums

**Files:**
- Create: `src/models.py`
- Create: `tests/test_models.py`

**Step 1: Write tests for enums and dataclasses**

```python
# tests/test_models.py
from src.models import (
    TaskStatus, TaskEvent, AgentState, AgentResult,
    ProjectStatus, VerificationType,
    Project, Task, Agent, RepoConfig, TaskContext, AgentOutput,
)


class TestTaskStatus:
    def test_all_states_exist(self):
        expected = {
            "DEFINED", "READY", "ASSIGNED", "IN_PROGRESS",
            "WAITING_INPUT", "PAUSED", "VERIFYING",
            "COMPLETED", "FAILED", "BLOCKED",
        }
        assert {s.value for s in TaskStatus} == expected


class TestTaskEvent:
    def test_all_events_exist(self):
        expected = {
            "DEPS_MET", "ASSIGNED", "AGENT_STARTED",
            "AGENT_COMPLETED", "AGENT_FAILED", "TOKENS_EXHAUSTED",
            "AGENT_QUESTION", "HUMAN_REPLIED", "INPUT_TIMEOUT",
            "RESUME_TIMER", "VERIFY_PASSED", "VERIFY_FAILED",
            "RETRY", "MAX_RETRIES",
        }
        assert {e.value for e in TaskEvent} == expected


class TestAgentState:
    def test_all_states_exist(self):
        expected = {"IDLE", "STARTING", "BUSY", "PAUSED", "ERROR"}
        assert {s.value for s in AgentState} == expected


class TestTask:
    def test_create_minimal_task(self):
        task = Task(
            id="t-1",
            project_id="p-1",
            title="Do something",
            description="Details here",
        )
        assert task.status == TaskStatus.DEFINED
        assert task.priority == 100
        assert task.retry_count == 0
        assert task.max_retries == 3
        assert task.parent_task_id is None

    def test_task_fields(self):
        task = Task(
            id="t-2",
            project_id="p-1",
            title="Test",
            description="Desc",
            priority=50,
            verification_type=VerificationType.HUMAN,
        )
        assert task.priority == 50
        assert task.verification_type == VerificationType.HUMAN


class TestAgent:
    def test_create_agent(self):
        agent = Agent(id="a-1", name="claude-1", agent_type="claude")
        assert agent.state == AgentState.IDLE
        assert agent.current_task_id is None
        assert agent.total_tokens_used == 0


class TestProject:
    def test_create_project(self):
        project = Project(id="p-1", name="alpha")
        assert project.credit_weight == 1.0
        assert project.max_concurrent_agents == 2
        assert project.status == ProjectStatus.ACTIVE
        assert project.budget_limit is None
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_models.py -v`
Expected: ImportError — module doesn't exist yet

**Step 3: Implement models**

```python
# src/models.py
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(Enum):
    DEFINED = "DEFINED"
    READY = "READY"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING_INPUT = "WAITING_INPUT"
    PAUSED = "PAUSED"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class TaskEvent(Enum):
    DEPS_MET = "DEPS_MET"
    ASSIGNED = "ASSIGNED"
    AGENT_STARTED = "AGENT_STARTED"
    AGENT_COMPLETED = "AGENT_COMPLETED"
    AGENT_FAILED = "AGENT_FAILED"
    TOKENS_EXHAUSTED = "TOKENS_EXHAUSTED"
    AGENT_QUESTION = "AGENT_QUESTION"
    HUMAN_REPLIED = "HUMAN_REPLIED"
    INPUT_TIMEOUT = "INPUT_TIMEOUT"
    RESUME_TIMER = "RESUME_TIMER"
    VERIFY_PASSED = "VERIFY_PASSED"
    VERIFY_FAILED = "VERIFY_FAILED"
    RETRY = "RETRY"
    MAX_RETRIES = "MAX_RETRIES"


class AgentState(Enum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    BUSY = "BUSY"
    PAUSED = "PAUSED"
    ERROR = "ERROR"


class AgentResult(Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED_TOKENS = "paused_tokens"
    PAUSED_RATE_LIMIT = "paused_rate_limit"


class ProjectStatus(Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"


class VerificationType(Enum):
    AUTO_TEST = "auto_test"
    QA_AGENT = "qa_agent"
    HUMAN = "human"


@dataclass
class RepoConfig:
    id: str
    project_id: str
    url: str
    default_branch: str = "main"
    checkout_base_path: str = ""


@dataclass
class Project:
    id: str
    name: str
    credit_weight: float = 1.0
    max_concurrent_agents: int = 2
    status: ProjectStatus = ProjectStatus.ACTIVE
    total_tokens_used: int = 0
    budget_limit: int | None = None


@dataclass
class Task:
    id: str
    project_id: str
    title: str
    description: str
    priority: int = 100
    status: TaskStatus = TaskStatus.DEFINED
    verification_type: VerificationType = VerificationType.AUTO_TEST
    retry_count: int = 0
    max_retries: int = 3
    parent_task_id: str | None = None
    repo_id: str | None = None
    assigned_agent_id: str | None = None
    branch_name: str | None = None
    resume_after: float | None = None  # unix timestamp


@dataclass
class Agent:
    id: str
    name: str
    agent_type: str  # "claude", "codex", "cursor", "aider"
    state: AgentState = AgentState.IDLE
    current_task_id: str | None = None
    checkout_path: str | None = None
    pid: int | None = None
    last_heartbeat: float | None = None
    total_tokens_used: int = 0
    session_tokens_used: int = 0


@dataclass
class TaskContext:
    description: str
    acceptance_criteria: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)
    checkout_path: str = ""
    branch_name: str = ""
    attached_context: list[str] = field(default_factory=list)
    mcp_servers: list[dict] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)


@dataclass
class AgentOutput:
    result: AgentResult
    summary: str = ""
    files_changed: list[str] = field(default_factory=list)
    tokens_used: int = 0
    error_message: str | None = None
```

**Step 4: Run tests**

Run: `pytest tests/test_models.py -v`
Expected: All pass

**Step 5: Commit**

```bash
git add src/models.py tests/test_models.py
git commit -m "feat: add core data models and enums"
```

---

## Task 3: Task State Machine

**Files:**
- Create: `src/state_machine.py`
- Create: `tests/test_state_machine.py`

**Step 1: Write exhaustive transition matrix tests**

```python
# tests/test_state_machine.py
import pytest
from src.models import TaskStatus, TaskEvent
from src.state_machine import (
    task_transition,
    InvalidTransition,
    VALID_TASK_TRANSITIONS,
)

ALL_STATUSES = list(TaskStatus)
ALL_EVENTS = list(TaskEvent)


class TestValidTransitions:
    @pytest.mark.parametrize(
        "state,event,expected",
        [
            (TaskStatus.DEFINED, TaskEvent.DEPS_MET, TaskStatus.READY),
            (TaskStatus.READY, TaskEvent.ASSIGNED, TaskStatus.ASSIGNED),
            (TaskStatus.ASSIGNED, TaskEvent.AGENT_STARTED, TaskStatus.IN_PROGRESS),
            (TaskStatus.IN_PROGRESS, TaskEvent.AGENT_COMPLETED, TaskStatus.VERIFYING),
            (TaskStatus.IN_PROGRESS, TaskEvent.AGENT_FAILED, TaskStatus.FAILED),
            (TaskStatus.IN_PROGRESS, TaskEvent.TOKENS_EXHAUSTED, TaskStatus.PAUSED),
            (TaskStatus.IN_PROGRESS, TaskEvent.AGENT_QUESTION, TaskStatus.WAITING_INPUT),
            (TaskStatus.WAITING_INPUT, TaskEvent.HUMAN_REPLIED, TaskStatus.IN_PROGRESS),
            (TaskStatus.WAITING_INPUT, TaskEvent.INPUT_TIMEOUT, TaskStatus.PAUSED),
            (TaskStatus.PAUSED, TaskEvent.RESUME_TIMER, TaskStatus.READY),
            (TaskStatus.VERIFYING, TaskEvent.VERIFY_PASSED, TaskStatus.COMPLETED),
            (TaskStatus.VERIFYING, TaskEvent.VERIFY_FAILED, TaskStatus.FAILED),
            (TaskStatus.FAILED, TaskEvent.RETRY, TaskStatus.READY),
            (TaskStatus.FAILED, TaskEvent.MAX_RETRIES, TaskStatus.BLOCKED),
        ],
    )
    def test_valid_transition(self, state, event, expected):
        result = task_transition(state, event)
        assert result == expected


class TestInvalidTransitions:
    @pytest.mark.parametrize(
        "state,event",
        [
            (s, e)
            for s in ALL_STATUSES
            for e in ALL_EVENTS
            if (s, e) not in VALID_TASK_TRANSITIONS
        ],
    )
    def test_invalid_transition_rejected(self, state, event):
        with pytest.raises(InvalidTransition):
            task_transition(state, event)


class TestTransitionTableCompleteness:
    def test_every_state_has_at_least_one_outgoing_transition(self):
        """Non-terminal states must have at least one valid transition."""
        terminal = {TaskStatus.COMPLETED, TaskStatus.BLOCKED}
        for state in ALL_STATUSES:
            if state in terminal:
                continue
            outgoing = [e for e in ALL_EVENTS if (state, e) in VALID_TASK_TRANSITIONS]
            assert len(outgoing) > 0, f"{state} has no outgoing transitions"

    def test_terminal_states_have_no_outgoing_transitions(self):
        terminal = {TaskStatus.COMPLETED, TaskStatus.BLOCKED}
        for state in terminal:
            outgoing = [e for e in ALL_EVENTS if (state, e) in VALID_TASK_TRANSITIONS]
            assert len(outgoing) == 0, f"Terminal {state} has outgoing transitions: {outgoing}"

    def test_paused_always_leads_to_ready(self):
        """PAUSED must always have a path back to READY (deadlock prevention)."""
        result = task_transition(TaskStatus.PAUSED, TaskEvent.RESUME_TIMER)
        assert result == TaskStatus.READY
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_state_machine.py -v`
Expected: ImportError

**Step 3: Implement state machine**

```python
# src/state_machine.py
from __future__ import annotations

from src.models import TaskStatus, TaskEvent


class InvalidTransition(Exception):
    def __init__(self, state: TaskStatus, event: TaskEvent):
        self.state = state
        self.event = event
        super().__init__(f"Invalid transition: ({state.value}, {event.value})")


VALID_TASK_TRANSITIONS: dict[tuple[TaskStatus, TaskEvent], TaskStatus] = {
    (TaskStatus.DEFINED, TaskEvent.DEPS_MET): TaskStatus.READY,
    (TaskStatus.READY, TaskEvent.ASSIGNED): TaskStatus.ASSIGNED,
    (TaskStatus.ASSIGNED, TaskEvent.AGENT_STARTED): TaskStatus.IN_PROGRESS,
    (TaskStatus.IN_PROGRESS, TaskEvent.AGENT_COMPLETED): TaskStatus.VERIFYING,
    (TaskStatus.IN_PROGRESS, TaskEvent.AGENT_FAILED): TaskStatus.FAILED,
    (TaskStatus.IN_PROGRESS, TaskEvent.TOKENS_EXHAUSTED): TaskStatus.PAUSED,
    (TaskStatus.IN_PROGRESS, TaskEvent.AGENT_QUESTION): TaskStatus.WAITING_INPUT,
    (TaskStatus.WAITING_INPUT, TaskEvent.HUMAN_REPLIED): TaskStatus.IN_PROGRESS,
    (TaskStatus.WAITING_INPUT, TaskEvent.INPUT_TIMEOUT): TaskStatus.PAUSED,
    (TaskStatus.PAUSED, TaskEvent.RESUME_TIMER): TaskStatus.READY,
    (TaskStatus.VERIFYING, TaskEvent.VERIFY_PASSED): TaskStatus.COMPLETED,
    (TaskStatus.VERIFYING, TaskEvent.VERIFY_FAILED): TaskStatus.FAILED,
    (TaskStatus.FAILED, TaskEvent.RETRY): TaskStatus.READY,
    (TaskStatus.FAILED, TaskEvent.MAX_RETRIES): TaskStatus.BLOCKED,
}


def task_transition(current: TaskStatus, event: TaskEvent) -> TaskStatus:
    key = (current, event)
    if key not in VALID_TASK_TRANSITIONS:
        raise InvalidTransition(current, event)
    return VALID_TASK_TRANSITIONS[key]
```

**Step 4: Run tests**

Run: `pytest tests/test_state_machine.py -v`
Expected: All pass

**Step 5: Commit**

```bash
git add src/state_machine.py tests/test_state_machine.py
git commit -m "feat: add task state machine with exhaustive transition tests"
```

---

## Task 4: DAG Dependency Validation

**Files:**
- Modify: `src/state_machine.py`
- Modify: `tests/test_state_machine.py`

**Step 1: Write DAG validation tests**

Append to `tests/test_state_machine.py`:

```python
from src.state_machine import validate_dag, CyclicDependencyError


class TestDAGValidation:
    def test_no_dependencies(self):
        deps = {}
        validate_dag(deps)  # should not raise

    def test_linear_chain(self):
        deps = {"t-2": {"t-1"}, "t-3": {"t-2"}}
        validate_dag(deps)  # should not raise

    def test_diamond_dependency(self):
        deps = {"t-3": {"t-1", "t-2"}, "t-4": {"t-3"}}
        validate_dag(deps)  # should not raise

    def test_self_dependency_rejected(self):
        deps = {"t-1": {"t-1"}}
        with pytest.raises(CyclicDependencyError):
            validate_dag(deps)

    def test_two_node_cycle_rejected(self):
        deps = {"t-1": {"t-2"}, "t-2": {"t-1"}}
        with pytest.raises(CyclicDependencyError):
            validate_dag(deps)

    def test_three_node_cycle_rejected(self):
        deps = {"t-1": {"t-2"}, "t-2": {"t-3"}, "t-3": {"t-1"}}
        with pytest.raises(CyclicDependencyError):
            validate_dag(deps)

    def test_cycle_in_larger_graph_rejected(self):
        deps = {
            "t-2": {"t-1"},
            "t-3": {"t-2"},
            "t-4": {"t-3"},
            "t-3a": {"t-4"},  # creates cycle: t-2 -> t-3 -> t-4 -> t-3a... but t-3 depends on t-2
        }
        # No cycle here actually. Let me fix:
        deps = {
            "t-2": {"t-1"},
            "t-3": {"t-2"},
            "t-4": {"t-3"},
            "t-2": {"t-4"},  # cycle: t-2 -> t-3 -> t-4 -> t-2
        }
        with pytest.raises(CyclicDependencyError):
            validate_dag(deps)

    def test_add_dependency_validates(self):
        """Adding a dependency that would create a cycle is rejected."""
        existing = {"t-2": {"t-1"}, "t-3": {"t-2"}}
        with pytest.raises(CyclicDependencyError):
            validate_dag_with_new_edge(existing, "t-1", depends_on="t-3")
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_state_machine.py::TestDAGValidation -v`
Expected: ImportError for new functions

**Step 3: Implement DAG validation**

Append to `src/state_machine.py`:

```python
class CyclicDependencyError(Exception):
    def __init__(self, cycle: list[str] | None = None):
        msg = "Cyclic dependency detected"
        if cycle:
            msg += f": {' -> '.join(cycle)}"
        super().__init__(msg)


def validate_dag(deps: dict[str, set[str]]) -> None:
    """Validate that the dependency graph is a DAG (no cycles). Uses DFS."""
    WHITE, GRAY, BLACK = 0, 1, 2
    all_nodes = set(deps.keys())
    for targets in deps.values():
        all_nodes.update(targets)

    color: dict[str, int] = {n: WHITE for n in all_nodes}

    def dfs(node: str) -> None:
        color[node] = GRAY
        for dep in deps.get(node, set()):
            if color[dep] == GRAY:
                raise CyclicDependencyError([node, dep])
            if color[dep] == WHITE:
                dfs(dep)
        color[node] = BLACK

    for node in all_nodes:
        if color[node] == WHITE:
            dfs(node)


def validate_dag_with_new_edge(
    deps: dict[str, set[str]], task_id: str, depends_on: str
) -> None:
    """Validate that adding a new edge doesn't create a cycle."""
    new_deps = {k: set(v) for k, v in deps.items()}
    new_deps.setdefault(task_id, set()).add(depends_on)
    validate_dag(new_deps)
```

**Step 4: Run tests**

Run: `pytest tests/test_state_machine.py -v`
Expected: All pass

**Step 5: Commit**

```bash
git add src/state_machine.py tests/test_state_machine.py
git commit -m "feat: add DAG dependency validation with cycle detection"
```

---

## Task 5: Database Layer

**Files:**
- Create: `src/database.py`
- Create: `tests/test_database.py`

**Step 1: Write database tests**

```python
# tests/test_database.py
import pytest
from src.database import Database
from src.models import (
    Project, Task, Agent, TaskStatus, AgentState,
    ProjectStatus, VerificationType,
)


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


class TestProjectCRUD:
    async def test_create_and_get_project(self, db):
        project = Project(id="p-1", name="alpha", credit_weight=3.0)
        await db.create_project(project)
        result = await db.get_project("p-1")
        assert result.name == "alpha"
        assert result.credit_weight == 3.0

    async def test_list_projects(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_project(Project(id="p-2", name="beta"))
        projects = await db.list_projects()
        assert len(projects) == 2

    async def test_update_project(self, db):
        await db.create_project(Project(id="p-1", name="alpha", credit_weight=1.0))
        await db.update_project("p-1", credit_weight=5.0)
        result = await db.get_project("p-1")
        assert result.credit_weight == 5.0

    async def test_get_nonexistent_project_returns_none(self, db):
        result = await db.get_project("nope")
        assert result is None


class TestTaskCRUD:
    async def test_create_and_get_task(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        task = Task(id="t-1", project_id="p-1", title="Do thing", description="Details")
        await db.create_task(task)
        result = await db.get_task("t-1")
        assert result.title == "Do thing"
        assert result.status == TaskStatus.DEFINED

    async def test_update_task_status(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_task(
            Task(id="t-1", project_id="p-1", title="X", description="Y")
        )
        await db.update_task("t-1", status=TaskStatus.READY)
        result = await db.get_task("t-1")
        assert result.status == TaskStatus.READY

    async def test_list_tasks_by_project(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_project(Project(id="p-2", name="beta"))
        await db.create_task(
            Task(id="t-1", project_id="p-1", title="A", description="D")
        )
        await db.create_task(
            Task(id="t-2", project_id="p-2", title="B", description="D")
        )
        tasks = await db.list_tasks(project_id="p-1")
        assert len(tasks) == 1
        assert tasks[0].id == "t-1"

    async def test_list_tasks_by_status(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_task(
            Task(id="t-1", project_id="p-1", title="A", description="D",
                 status=TaskStatus.READY)
        )
        await db.create_task(
            Task(id="t-2", project_id="p-1", title="B", description="D",
                 status=TaskStatus.DEFINED)
        )
        tasks = await db.list_tasks(project_id="p-1", status=TaskStatus.READY)
        assert len(tasks) == 1
        assert tasks[0].id == "t-1"

    async def test_get_subtasks(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_task(
            Task(id="t-1", project_id="p-1", title="Parent", description="D")
        )
        await db.create_task(
            Task(id="t-2", project_id="p-1", title="Child",
                 description="D", parent_task_id="t-1")
        )
        subtasks = await db.get_subtasks("t-1")
        assert len(subtasks) == 1
        assert subtasks[0].id == "t-2"


class TestTaskDependencies:
    async def test_add_and_get_dependencies(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_task(
            Task(id="t-1", project_id="p-1", title="A", description="D")
        )
        await db.create_task(
            Task(id="t-2", project_id="p-1", title="B", description="D")
        )
        await db.add_dependency("t-2", depends_on="t-1")
        deps = await db.get_dependencies("t-2")
        assert deps == {"t-1"}

    async def test_check_dependencies_met(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_task(
            Task(id="t-1", project_id="p-1", title="A",
                 description="D", status=TaskStatus.DEFINED)
        )
        await db.create_task(
            Task(id="t-2", project_id="p-1", title="B", description="D")
        )
        await db.add_dependency("t-2", depends_on="t-1")
        assert not await db.are_dependencies_met("t-2")

        await db.update_task("t-1", status=TaskStatus.COMPLETED)
        assert await db.are_dependencies_met("t-2")


class TestAgentCRUD:
    async def test_create_and_get_agent(self, db):
        agent = Agent(id="a-1", name="claude-1", agent_type="claude")
        await db.create_agent(agent)
        result = await db.get_agent("a-1")
        assert result.name == "claude-1"
        assert result.state == AgentState.IDLE

    async def test_update_agent_state(self, db):
        await db.create_agent(
            Agent(id="a-1", name="claude-1", agent_type="claude")
        )
        await db.update_agent("a-1", state=AgentState.BUSY, current_task_id="t-1")
        result = await db.get_agent("a-1")
        assert result.state == AgentState.BUSY
        assert result.current_task_id == "t-1"

    async def test_list_idle_agents(self, db):
        await db.create_agent(
            Agent(id="a-1", name="claude-1", agent_type="claude")
        )
        await db.create_agent(
            Agent(id="a-2", name="claude-2", agent_type="claude",
                  state=AgentState.BUSY)
        )
        idle = await db.list_agents(state=AgentState.IDLE)
        assert len(idle) == 1
        assert idle[0].id == "a-1"


class TestTokenLedger:
    async def test_record_and_sum_tokens(self, db):
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_agent(
            Agent(id="a-1", name="claude-1", agent_type="claude")
        )
        await db.create_task(
            Task(id="t-1", project_id="p-1", title="A", description="D")
        )
        await db.record_token_usage("p-1", "a-1", "t-1", 5000)
        await db.record_token_usage("p-1", "a-1", "t-1", 3000)
        total = await db.get_project_token_usage("p-1")
        assert total == 8000


class TestEvents:
    async def test_log_and_retrieve_events(self, db):
        await db.log_event("task_created", project_id="p-1", task_id="t-1")
        events = await db.get_recent_events(limit=10)
        assert len(events) == 1
        assert events[0]["event_type"] == "task_created"


class TestAtomicTransition:
    async def test_atomic_task_agent_update(self, db):
        """Task and agent state update atomically."""
        await db.create_project(Project(id="p-1", name="alpha"))
        await db.create_task(
            Task(id="t-1", project_id="p-1", title="A",
                 description="D", status=TaskStatus.READY)
        )
        await db.create_agent(
            Agent(id="a-1", name="claude-1", agent_type="claude")
        )
        await db.assign_task_to_agent("t-1", "a-1")
        task = await db.get_task("t-1")
        agent = await db.get_agent("a-1")
        assert task.status == TaskStatus.ASSIGNED
        assert task.assigned_agent_id == "a-1"
        assert agent.state == AgentState.STARTING
        assert agent.current_task_id == "t-1"
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_database.py -v`
Expected: ImportError

**Step 3: Implement database layer**

```python
# src/database.py
from __future__ import annotations

import time
import uuid

import aiosqlite

from src.models import (
    Agent, AgentState, Project, ProjectStatus, Task, TaskStatus,
    VerificationType,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    credit_weight REAL NOT NULL DEFAULT 1.0,
    max_concurrent_agents INTEGER NOT NULL DEFAULT 2,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    total_tokens_used INTEGER NOT NULL DEFAULT 0,
    budget_limit INTEGER,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS repos (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    url TEXT NOT NULL,
    default_branch TEXT NOT NULL DEFAULT 'main',
    checkout_base_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    parent_task_id TEXT REFERENCES tasks(id),
    repo_id TEXT REFERENCES repos(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL DEFAULT 'DEFINED',
    verification_type TEXT NOT NULL DEFAULT 'auto_test',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    assigned_agent_id TEXT REFERENCES agents(id),
    branch_name TEXT,
    resume_after REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS task_criteria (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (task_id, depends_on_task_id),
    CHECK (task_id != depends_on_task_id)
);

CREATE TABLE IF NOT EXISTS task_context (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    type TEXT NOT NULL,
    label TEXT,
    content TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_tools (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    type TEXT NOT NULL,
    config TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'IDLE',
    current_task_id TEXT REFERENCES tasks(id),
    checkout_path TEXT,
    pid INTEGER,
    last_heartbeat REAL,
    total_tokens_used INTEGER NOT NULL DEFAULT 0,
    session_tokens_used INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS token_ledger (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    agent_id TEXT NOT NULL REFERENCES agents(id),
    task_id TEXT NOT NULL REFERENCES tasks(id),
    tokens_used INTEGER NOT NULL,
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    project_id TEXT,
    task_id TEXT,
    agent_id TEXT,
    payload TEXT,
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_limits (
    id TEXT PRIMARY KEY,
    agent_type TEXT NOT NULL,
    limit_type TEXT NOT NULL,
    max_tokens INTEGER NOT NULL,
    current_tokens INTEGER NOT NULL DEFAULT 0,
    window_start REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS system_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # --- Projects ---

    async def create_project(self, project: Project) -> None:
        await self._db.execute(
            "INSERT INTO projects (id, name, credit_weight, max_concurrent_agents, "
            "status, total_tokens_used, budget_limit, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project.id, project.name, project.credit_weight,
             project.max_concurrent_agents, project.status.value,
             project.total_tokens_used, project.budget_limit, time.time()),
        )
        await self._db.commit()

    async def get_project(self, project_id: str) -> Project | None:
        cursor = await self._db.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_project(row)

    async def list_projects(
        self, status: ProjectStatus | None = None
    ) -> list[Project]:
        if status:
            cursor = await self._db.execute(
                "SELECT * FROM projects WHERE status = ?", (status.value,)
            )
        else:
            cursor = await self._db.execute("SELECT * FROM projects")
        rows = await cursor.fetchall()
        return [self._row_to_project(r) for r in rows]

    async def update_project(self, project_id: str, **kwargs) -> None:
        sets = []
        vals = []
        for key, value in kwargs.items():
            if isinstance(value, ProjectStatus):
                value = value.value
            sets.append(f"{key} = ?")
            vals.append(value)
        vals.append(project_id)
        await self._db.execute(
            f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", vals
        )
        await self._db.commit()

    def _row_to_project(self, row) -> Project:
        return Project(
            id=row["id"],
            name=row["name"],
            credit_weight=row["credit_weight"],
            max_concurrent_agents=row["max_concurrent_agents"],
            status=ProjectStatus(row["status"]),
            total_tokens_used=row["total_tokens_used"],
            budget_limit=row["budget_limit"],
        )

    # --- Tasks ---

    async def create_task(self, task: Task) -> None:
        now = time.time()
        await self._db.execute(
            "INSERT INTO tasks (id, project_id, parent_task_id, repo_id, title, "
            "description, priority, status, verification_type, retry_count, "
            "max_retries, assigned_agent_id, branch_name, resume_after, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task.id, task.project_id, task.parent_task_id, task.repo_id,
             task.title, task.description, task.priority, task.status.value,
             task.verification_type.value, task.retry_count, task.max_retries,
             task.assigned_agent_id, task.branch_name, task.resume_after,
             now, now),
        )
        await self._db.commit()

    async def get_task(self, task_id: str) -> Task | None:
        cursor = await self._db.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_task(row)

    async def list_tasks(
        self,
        project_id: str | None = None,
        status: TaskStatus | None = None,
    ) -> list[Task]:
        conditions = []
        vals = []
        if project_id:
            conditions.append("project_id = ?")
            vals.append(project_id)
        if status:
            conditions.append("status = ?")
            vals.append(status.value)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.execute(
            f"SELECT * FROM tasks {where} ORDER BY priority ASC, created_at ASC",
            vals,
        )
        rows = await cursor.fetchall()
        return [self._row_to_task(r) for r in rows]

    async def update_task(self, task_id: str, **kwargs) -> None:
        sets = []
        vals = []
        for key, value in kwargs.items():
            if isinstance(value, (TaskStatus, VerificationType)):
                value = value.value
            sets.append(f"{key} = ?")
            vals.append(value)
        sets.append("updated_at = ?")
        vals.append(time.time())
        vals.append(task_id)
        await self._db.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals
        )
        await self._db.commit()

    async def get_subtasks(self, parent_task_id: str) -> list[Task]:
        cursor = await self._db.execute(
            "SELECT * FROM tasks WHERE parent_task_id = ?", (parent_task_id,)
        )
        rows = await cursor.fetchall()
        return [self._row_to_task(r) for r in rows]

    def _row_to_task(self, row) -> Task:
        return Task(
            id=row["id"],
            project_id=row["project_id"],
            parent_task_id=row["parent_task_id"],
            repo_id=row["repo_id"],
            title=row["title"],
            description=row["description"],
            priority=row["priority"],
            status=TaskStatus(row["status"]),
            verification_type=VerificationType(row["verification_type"]),
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            assigned_agent_id=row["assigned_agent_id"],
            branch_name=row["branch_name"],
            resume_after=row["resume_after"],
        )

    # --- Dependencies ---

    async def add_dependency(self, task_id: str, depends_on: str) -> None:
        await self._db.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (?, ?)",
            (task_id, depends_on),
        )
        await self._db.commit()

    async def get_dependencies(self, task_id: str) -> set[str]:
        cursor = await self._db.execute(
            "SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ?",
            (task_id,),
        )
        rows = await cursor.fetchall()
        return {r["depends_on_task_id"] for r in rows}

    async def get_all_dependencies(self) -> dict[str, set[str]]:
        cursor = await self._db.execute("SELECT * FROM task_dependencies")
        rows = await cursor.fetchall()
        deps: dict[str, set[str]] = {}
        for r in rows:
            deps.setdefault(r["task_id"], set()).add(r["depends_on_task_id"])
        return deps

    async def are_dependencies_met(self, task_id: str) -> bool:
        cursor = await self._db.execute(
            "SELECT d.depends_on_task_id, t.status "
            "FROM task_dependencies d "
            "JOIN tasks t ON t.id = d.depends_on_task_id "
            "WHERE d.task_id = ?",
            (task_id,),
        )
        rows = await cursor.fetchall()
        return all(r["status"] == TaskStatus.COMPLETED.value for r in rows)

    # --- Agents ---

    async def create_agent(self, agent: Agent) -> None:
        await self._db.execute(
            "INSERT INTO agents (id, name, agent_type, state, current_task_id, "
            "checkout_path, pid, last_heartbeat, total_tokens_used, "
            "session_tokens_used, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent.id, agent.name, agent.type if hasattr(agent, 'type') else agent.agent_type,
             agent.state.value, agent.current_task_id,
             agent.checkout_path, agent.pid, agent.last_heartbeat,
             agent.total_tokens_used, agent.session_tokens_used, time.time()),
        )
        await self._db.commit()

    async def get_agent(self, agent_id: str) -> Agent | None:
        cursor = await self._db.execute(
            "SELECT * FROM agents WHERE id = ?", (agent_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_agent(row)

    async def list_agents(
        self, state: AgentState | None = None
    ) -> list[Agent]:
        if state:
            cursor = await self._db.execute(
                "SELECT * FROM agents WHERE state = ?", (state.value,)
            )
        else:
            cursor = await self._db.execute("SELECT * FROM agents")
        rows = await cursor.fetchall()
        return [self._row_to_agent(r) for r in rows]

    async def update_agent(self, agent_id: str, **kwargs) -> None:
        sets = []
        vals = []
        for key, value in kwargs.items():
            if isinstance(value, AgentState):
                value = value.value
            sets.append(f"{key} = ?")
            vals.append(value)
        vals.append(agent_id)
        await self._db.execute(
            f"UPDATE agents SET {', '.join(sets)} WHERE id = ?", vals
        )
        await self._db.commit()

    def _row_to_agent(self, row) -> Agent:
        return Agent(
            id=row["id"],
            name=row["name"],
            agent_type=row["agent_type"],
            state=AgentState(row["state"]),
            current_task_id=row["current_task_id"],
            checkout_path=row["checkout_path"],
            pid=row["pid"],
            last_heartbeat=row["last_heartbeat"],
            total_tokens_used=row["total_tokens_used"],
            session_tokens_used=row["session_tokens_used"],
        )

    # --- Token Ledger ---

    async def record_token_usage(
        self, project_id: str, agent_id: str, task_id: str, tokens: int
    ) -> None:
        await self._db.execute(
            "INSERT INTO token_ledger (id, project_id, agent_id, task_id, "
            "tokens_used, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), project_id, agent_id, task_id, tokens, time.time()),
        )
        await self._db.commit()

    async def get_project_token_usage(
        self, project_id: str, since: float | None = None
    ) -> int:
        if since:
            cursor = await self._db.execute(
                "SELECT COALESCE(SUM(tokens_used), 0) as total "
                "FROM token_ledger WHERE project_id = ? AND timestamp >= ?",
                (project_id, since),
            )
        else:
            cursor = await self._db.execute(
                "SELECT COALESCE(SUM(tokens_used), 0) as total "
                "FROM token_ledger WHERE project_id = ?",
                (project_id,),
            )
        row = await cursor.fetchone()
        return row["total"]

    # --- Events ---

    async def log_event(
        self,
        event_type: str,
        project_id: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        payload: str | None = None,
    ) -> None:
        await self._db.execute(
            "INSERT INTO events (event_type, project_id, task_id, agent_id, "
            "payload, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (event_type, project_id, task_id, agent_id, payload, time.time()),
        )
        await self._db.commit()

    async def get_recent_events(self, limit: int = 50) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # --- Atomic Operations ---

    async def assign_task_to_agent(self, task_id: str, agent_id: str) -> None:
        async with self._db.execute("BEGIN"):
            await self._db.execute(
                "UPDATE tasks SET status = ?, assigned_agent_id = ?, updated_at = ? "
                "WHERE id = ?",
                (TaskStatus.ASSIGNED.value, agent_id, time.time(), task_id),
            )
            await self._db.execute(
                "UPDATE agents SET state = ?, current_task_id = ? WHERE id = ?",
                (AgentState.STARTING.value, task_id, agent_id),
            )
            await self._db.execute(
                "INSERT INTO events (event_type, project_id, task_id, agent_id, "
                "timestamp) VALUES (?, (SELECT project_id FROM tasks WHERE id = ?), "
                "?, ?, ?)",
                ("task_assigned", task_id, task_id, agent_id, time.time()),
            )
        await self._db.commit()
```

**Step 4: Run tests**

Run: `pytest tests/test_database.py -v`
Expected: All pass

**Step 5: Commit**

```bash
git add src/database.py tests/test_database.py
git commit -m "feat: add SQLite database layer with CRUD and atomic transitions"
```

---

## Task 6: Event Bus

**Files:**
- Create: `src/event_bus.py`
- Create: `tests/test_event_bus.py`

**Step 1: Write event bus tests**

```python
# tests/test_event_bus.py
import asyncio
import pytest
from src.event_bus import EventBus


class TestEventBus:
    async def test_subscribe_and_emit(self):
        bus = EventBus()
        received = []
        bus.subscribe("task_completed", lambda data: received.append(data))
        await bus.emit("task_completed", {"task_id": "t-1"})
        assert len(received) == 1
        assert received[0]["task_id"] == "t-1"

    async def test_multiple_subscribers(self):
        bus = EventBus()
        received_a = []
        received_b = []
        bus.subscribe("test", lambda d: received_a.append(d))
        bus.subscribe("test", lambda d: received_b.append(d))
        await bus.emit("test", {"x": 1})
        assert len(received_a) == 1
        assert len(received_b) == 1

    async def test_no_cross_talk(self):
        bus = EventBus()
        received = []
        bus.subscribe("event_a", lambda d: received.append(d))
        await bus.emit("event_b", {"x": 1})
        assert len(received) == 0

    async def test_wildcard_subscriber(self):
        bus = EventBus()
        received = []
        bus.subscribe("*", lambda d: received.append(d))
        await bus.emit("anything", {"x": 1})
        await bus.emit("something_else", {"y": 2})
        assert len(received) == 2

    async def test_async_handler(self):
        bus = EventBus()
        received = []

        async def handler(data):
            await asyncio.sleep(0)
            received.append(data)

        bus.subscribe("test", handler)
        await bus.emit("test", {"x": 1})
        assert len(received) == 1
```

**Step 2: Run to verify failure, then implement**

```python
# src/event_bus.py
from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from typing import Any, Callable


class EventBus:
    def __init__(self):
        self._handlers: dict[str, list[Callable]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Callable) -> None:
        self._handlers[event_type].append(handler)

    async def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        data["_event_type"] = event_type
        handlers = list(self._handlers.get(event_type, []))
        handlers.extend(self._handlers.get("*", []))
        for handler in handlers:
            if inspect.iscoroutinefunction(handler):
                await handler(data)
            else:
                handler(data)
```

**Step 3: Run tests**

Run: `pytest tests/test_event_bus.py -v`
Expected: All pass

**Step 4: Commit**

```bash
git add src/event_bus.py tests/test_event_bus.py
git commit -m "feat: add in-process async event bus"
```

---

## Task 7: Scheduler

**Files:**
- Create: `src/scheduler.py`
- Create: `tests/test_scheduler.py`

**Step 1: Write scheduler tests**

```python
# tests/test_scheduler.py
import pytest
from src.models import (
    Project, Task, Agent, TaskStatus, AgentState, ProjectStatus,
)
from src.scheduler import Scheduler, SchedulerState, AssignAction


def make_project(id="p-1", name="alpha", weight=1.0, max_agents=2, **kw):
    return Project(id=id, name=name, credit_weight=weight,
                   max_concurrent_agents=max_agents, **kw)


def make_task(id="t-1", project_id="p-1", status=TaskStatus.READY, priority=100, **kw):
    return Task(id=id, project_id=project_id, title=f"Task {id}",
                description="test", status=status, priority=priority, **kw)


def make_agent(id="a-1", name="claude-1", state=AgentState.IDLE, **kw):
    return Agent(id=id, name=name, agent_type="claude", state=state, **kw)


class TestScheduler:
    def test_assign_single_task_to_idle_agent(self):
        state = SchedulerState(
            projects=[make_project()],
            tasks=[make_task()],
            agents=[make_agent()],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert isinstance(actions[0], AssignAction)
        assert actions[0].task_id == "t-1"
        assert actions[0].agent_id == "a-1"

    def test_no_idle_agents_no_actions(self):
        state = SchedulerState(
            projects=[make_project()],
            tasks=[make_task()],
            agents=[make_agent(state=AgentState.BUSY)],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    def test_no_ready_tasks_no_actions(self):
        state = SchedulerState(
            projects=[make_project()],
            tasks=[make_task(status=TaskStatus.IN_PROGRESS)],
            agents=[make_agent()],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    def test_proportional_allocation_favors_deficit(self):
        state = SchedulerState(
            projects=[
                make_project(id="p-1", name="alpha", weight=3.0),
                make_project(id="p-2", name="beta", weight=1.0),
            ],
            tasks=[
                make_task(id="t-1", project_id="p-1"),
                make_task(id="t-2", project_id="p-2"),
            ],
            agents=[make_agent()],
            project_token_usage={"p-1": 60000, "p-2": 40000},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        # p-1 target=75%, actual=60% (deficit=15%)
        # p-2 target=25%, actual=40% (surplus=15%)
        assert actions[0].task_id == "t-1"

    def test_min_task_guarantee(self):
        state = SchedulerState(
            projects=[
                make_project(id="p-1", name="alpha", weight=19.0),
                make_project(id="p-2", name="beta", weight=1.0),
            ],
            tasks=[
                make_task(id="t-1", project_id="p-1"),
                make_task(id="t-2", project_id="p-2"),
            ],
            agents=[make_agent()],
            project_token_usage={"p-1": 95000, "p-2": 0},
            project_active_agent_counts={},
            tasks_completed_in_window={"p-1": 10, "p-2": 0},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].task_id == "t-2"  # min guarantee for p-2

    def test_respects_max_concurrent_agents(self):
        state = SchedulerState(
            projects=[make_project(max_agents=1)],
            tasks=[make_task(id="t-1"), make_task(id="t-2")],
            agents=[make_agent(id="a-1"), make_agent(id="a-2")],
            project_token_usage={},
            project_active_agent_counts={"p-1": 1},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0  # already at max

    def test_paused_project_skipped(self):
        state = SchedulerState(
            projects=[make_project(status=ProjectStatus.PAUSED)],
            tasks=[make_task()],
            agents=[make_agent()],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    def test_priority_ordering(self):
        state = SchedulerState(
            projects=[make_project()],
            tasks=[
                make_task(id="t-low", priority=200),
                make_task(id="t-high", priority=10),
            ],
            agents=[make_agent()],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert actions[0].task_id == "t-high"

    def test_global_budget_exhausted_stops_all(self):
        state = SchedulerState(
            projects=[make_project()],
            tasks=[make_task()],
            agents=[make_agent()],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            global_budget=100000,
            global_tokens_used=100000,
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    def test_per_project_budget_exhausted(self):
        state = SchedulerState(
            projects=[make_project(budget_limit=50000)],
            tasks=[make_task()],
            agents=[make_agent()],
            project_token_usage={"p-1": 50000},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    def test_multiple_agents_assigned_to_different_projects(self):
        state = SchedulerState(
            projects=[
                make_project(id="p-1", name="alpha"),
                make_project(id="p-2", name="beta"),
            ],
            tasks=[
                make_task(id="t-1", project_id="p-1"),
                make_task(id="t-2", project_id="p-2"),
            ],
            agents=[
                make_agent(id="a-1"),
                make_agent(id="a-2"),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 2
        task_ids = {a.task_id for a in actions}
        assert task_ids == {"t-1", "t-2"}
```

**Step 2: Run to verify failure, then implement**

```python
# src/scheduler.py
from __future__ import annotations

from dataclasses import dataclass, field

from src.models import (
    Agent, AgentState, Project, ProjectStatus, Task, TaskStatus,
)


@dataclass
class AssignAction:
    agent_id: str
    task_id: str
    project_id: str


@dataclass
class SchedulerState:
    projects: list[Project]
    tasks: list[Task]
    agents: list[Agent]
    project_token_usage: dict[str, int]  # project_id -> tokens in window
    project_active_agent_counts: dict[str, int]  # project_id -> count
    tasks_completed_in_window: dict[str, int]  # project_id -> count
    global_budget: int | None = None
    global_tokens_used: int = 0


class Scheduler:
    @staticmethod
    def schedule(state: SchedulerState) -> list[AssignAction]:
        # Check global budget
        if (
            state.global_budget is not None
            and state.global_tokens_used >= state.global_budget
        ):
            return []

        idle_agents = [a for a in state.agents if a.state == AgentState.IDLE]
        if not idle_agents:
            return []

        # Group ready tasks by project
        ready_by_project: dict[str, list[Task]] = {}
        for task in state.tasks:
            if task.status == TaskStatus.READY:
                ready_by_project.setdefault(task.project_id, []).append(task)

        # Sort tasks within each project by priority then creation order (id as proxy)
        for tasks in ready_by_project.values():
            tasks.sort(key=lambda t: (t.priority, t.id))

        # Filter to active projects with ready tasks
        active_projects = [
            p for p in state.projects
            if p.status == ProjectStatus.ACTIVE and p.id in ready_by_project
        ]
        if not active_projects:
            return []

        # Calculate total weight
        total_weight = sum(p.credit_weight for p in active_projects)
        total_tokens = sum(state.project_token_usage.values()) or 1  # avoid div/0

        # Track assignments made in this scheduling round
        actions: list[AssignAction] = []
        assigned_agents: set[str] = set()
        assigned_tasks: set[str] = set()
        round_agent_counts: dict[str, int] = dict(state.project_active_agent_counts)

        for agent in idle_agents:
            if agent.id in assigned_agents:
                continue

            # Sort projects: min-task-guarantee first, then by deficit
            def project_sort_key(p: Project) -> tuple[int, float]:
                completed = state.tasks_completed_in_window.get(p.id, 0)
                has_guarantee = 1 if completed > 0 else 0  # 0 = needs guarantee (sorts first)
                target_ratio = p.credit_weight / total_weight
                actual_ratio = state.project_token_usage.get(p.id, 0) / total_tokens
                deficit = actual_ratio - target_ratio  # negative = below target
                return (has_guarantee, deficit)

            sorted_projects = sorted(active_projects, key=project_sort_key)

            for project in sorted_projects:
                # Check per-project budget
                if (
                    project.budget_limit is not None
                    and state.project_token_usage.get(project.id, 0)
                    >= project.budget_limit
                ):
                    continue

                # Check concurrency limit
                current_agents = round_agent_counts.get(project.id, 0)
                if current_agents >= project.max_concurrent_agents:
                    continue

                # Pick highest priority ready task not yet assigned
                available = [
                    t for t in ready_by_project.get(project.id, [])
                    if t.id not in assigned_tasks
                ]
                if not available:
                    continue

                task = available[0]
                actions.append(AssignAction(
                    agent_id=agent.id,
                    task_id=task.id,
                    project_id=project.id,
                ))
                assigned_agents.add(agent.id)
                assigned_tasks.add(task.id)
                round_agent_counts[project.id] = current_agents + 1
                break

        return actions
```

**Step 3: Run tests**

Run: `pytest tests/test_scheduler.py -v`
Expected: All pass

**Step 4: Commit**

```bash
git add src/scheduler.py tests/test_scheduler.py
git commit -m "feat: add deterministic scheduler with proportional allocation"
```

---

## Task 8: Agent Adapter Base + Claude Adapter

**Files:**
- Create: `src/adapters/__init__.py`
- Create: `src/adapters/base.py`
- Create: `src/adapters/claude.py`
- Create: `tests/test_adapters.py`

**Step 1: Write adapter tests**

```python
# tests/test_adapters.py
import pytest
from src.adapters.base import AgentAdapter
from src.models import TaskContext, AgentOutput, AgentResult


class MockAdapter(AgentAdapter):
    def __init__(self, result=AgentResult.COMPLETED, tokens=1000):
        self._result = result
        self._tokens = tokens
        self.started = False
        self.stopped = False

    async def start(self, task: TaskContext) -> None:
        self.started = True

    async def wait(self) -> AgentOutput:
        return AgentOutput(
            result=self._result,
            summary="Did the thing",
            tokens_used=self._tokens,
        )

    async def stop(self) -> None:
        self.stopped = True

    async def is_alive(self) -> bool:
        return self.started and not self.stopped


class TestMockAdapter:
    async def test_lifecycle(self):
        adapter = MockAdapter()
        ctx = TaskContext(description="test task")
        await adapter.start(ctx)
        assert adapter.started
        assert await adapter.is_alive()
        output = await adapter.wait()
        assert output.result == AgentResult.COMPLETED
        assert output.tokens_used == 1000
        await adapter.stop()
        assert adapter.stopped

    async def test_failed_result(self):
        adapter = MockAdapter(result=AgentResult.FAILED)
        ctx = TaskContext(description="test")
        await adapter.start(ctx)
        output = await adapter.wait()
        assert output.result == AgentResult.FAILED

    async def test_paused_result(self):
        adapter = MockAdapter(result=AgentResult.PAUSED_RATE_LIMIT)
        ctx = TaskContext(description="test")
        await adapter.start(ctx)
        output = await adapter.wait()
        assert output.result == AgentResult.PAUSED_RATE_LIMIT
```

**Step 2: Implement base adapter**

```python
# src/adapters/__init__.py
# (empty)
```

```python
# src/adapters/base.py
from __future__ import annotations

from abc import ABC, abstractmethod

from src.models import AgentOutput, TaskContext


class AgentAdapter(ABC):
    @abstractmethod
    async def start(self, task: TaskContext) -> None:
        """Launch the agent process with the given task."""

    @abstractmethod
    async def wait(self) -> AgentOutput:
        """Wait for the agent to finish and return results."""

    @abstractmethod
    async def stop(self) -> None:
        """Forcefully stop the agent."""

    @abstractmethod
    async def is_alive(self) -> bool:
        """Check if the agent process is still running."""
```

**Step 3: Create Claude adapter stub**

The Claude adapter depends on `claude-agent-sdk` which requires API keys to test. Create the implementation but defer integration testing to manual smoke tests.

```python
# src/adapters/claude.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from src.adapters.base import AgentAdapter
from src.models import AgentOutput, AgentResult, TaskContext


@dataclass
class ClaudeAdapterConfig:
    model: str = "claude-sonnet-4-20250514"
    permission_mode: str = "acceptEdits"
    allowed_tools: list[str] = field(default_factory=lambda: [
        "Read", "Write", "Edit", "Bash", "Glob", "Grep",
    ])


class ClaudeAdapter(AgentAdapter):
    def __init__(self, config: ClaudeAdapterConfig | None = None):
        self._config = config or ClaudeAdapterConfig()
        self._task: TaskContext | None = None
        self._cancel_event = asyncio.Event()
        self._session_id: str | None = None

    async def start(self, task: TaskContext) -> None:
        self._task = task
        self._cancel_event.clear()

    async def wait(self) -> AgentOutput:
        try:
            from claude_agent_sdk import query, ClaudeAgentOptions

            options = ClaudeAgentOptions(
                allowed_tools=self._config.allowed_tools,
                permission_mode=self._config.permission_mode,
                model=self._config.model,
                cwd=self._task.checkout_path or None,
            )
            if self._task.mcp_servers:
                options.mcp_servers = self._task.mcp_servers

            summary_parts = []
            tokens_used = 0

            async for message in query(
                prompt=self._build_prompt(),
                options=options,
            ):
                if self._cancel_event.is_set():
                    return AgentOutput(
                        result=AgentResult.FAILED,
                        summary="Cancelled",
                        error_message="Agent was stopped",
                    )

                # Capture session ID from init message
                if hasattr(message, "subtype") and message.subtype == "init":
                    self._session_id = getattr(message, "session_id", None)

                # Capture result
                if hasattr(message, "result"):
                    summary_parts.append(str(message.result))

            return AgentOutput(
                result=AgentResult.COMPLETED,
                summary="\n".join(summary_parts) or "Completed",
                tokens_used=tokens_used,
            )
        except Exception as e:
            error_msg = str(e)
            if "rate" in error_msg.lower() or "429" in error_msg:
                return AgentOutput(
                    result=AgentResult.PAUSED_RATE_LIMIT,
                    error_message=error_msg,
                )
            if "token" in error_msg.lower() or "quota" in error_msg.lower():
                return AgentOutput(
                    result=AgentResult.PAUSED_TOKENS,
                    error_message=error_msg,
                )
            return AgentOutput(
                result=AgentResult.FAILED,
                error_message=error_msg,
            )

    async def stop(self) -> None:
        self._cancel_event.set()

    async def is_alive(self) -> bool:
        return self._task is not None and not self._cancel_event.is_set()

    def _build_prompt(self) -> str:
        parts = [self._task.description]
        if self._task.acceptance_criteria:
            parts.append("\n## Acceptance Criteria")
            for c in self._task.acceptance_criteria:
                parts.append(f"- {c}")
        if self._task.test_commands:
            parts.append("\n## Test Commands")
            for cmd in self._task.test_commands:
                parts.append(f"- `{cmd}`")
        if self._task.attached_context:
            parts.append("\n## Additional Context")
            for ctx in self._task.attached_context:
                parts.append(f"- {ctx}")
        return "\n".join(parts)
```

**Step 4: Run tests**

Run: `pytest tests/test_adapters.py -v`
Expected: All pass

**Step 5: Commit**

```bash
git add src/adapters/ tests/test_adapters.py
git commit -m "feat: add agent adapter interface and Claude adapter"
```

---

## Task 9: Configuration Loading

**Files:**
- Create: `src/config.py`
- Create: `tests/test_config.py`

**Step 1: Write config tests**

```python
# tests/test_config.py
import os
import pytest
import yaml
from src.config import load_config, AppConfig


@pytest.fixture
def config_dir(tmp_path):
    return tmp_path


class TestConfigLoading:
    def test_load_minimal_config(self, config_dir):
        config_file = config_dir / "config.yaml"
        config_file.write_text(yaml.dump({
            "discord": {
                "bot_token": "test-token",
                "guild_id": "123",
            }
        }))
        config = load_config(str(config_file))
        assert config.discord.bot_token == "test-token"
        assert config.workspace_dir == os.path.expanduser("~/agent-queue-workspaces")

    def test_env_var_substitution(self, config_dir, monkeypatch):
        monkeypatch.setenv("TEST_BOT_TOKEN", "secret-token-123")
        config_file = config_dir / "config.yaml"
        config_file.write_text(yaml.dump({
            "discord": {
                "bot_token": "${TEST_BOT_TOKEN}",
                "guild_id": "123",
            }
        }))
        config = load_config(str(config_file))
        assert config.discord.bot_token == "secret-token-123"

    def test_defaults_applied(self, config_dir):
        config_file = config_dir / "config.yaml"
        config_file.write_text(yaml.dump({
            "discord": {"bot_token": "x", "guild_id": "1"}
        }))
        config = load_config(str(config_file))
        assert config.database_path == os.path.expanduser("~/.agent-queue/agent-queue.db")
        assert config.scheduling.rolling_window_hours == 24
        assert config.scheduling.min_task_guarantee is True
        assert config.agents_config.heartbeat_interval_seconds == 30

    def test_custom_workspace_dir(self, config_dir):
        config_file = config_dir / "config.yaml"
        config_file.write_text(yaml.dump({
            "workspace_dir": "/custom/path",
            "discord": {"bot_token": "x", "guild_id": "1"},
        }))
        config = load_config(str(config_file))
        assert config.workspace_dir == "/custom/path"

    def test_missing_config_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.yaml")
```

**Step 2: Implement config loading**

```python
# src/config.py
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import yaml


@dataclass
class DiscordConfig:
    bot_token: str = ""
    guild_id: str = ""
    channels: dict[str, str] = field(default_factory=lambda: {
        "control": "control",
        "notifications": "notifications",
        "agent_questions": "agent-questions",
    })
    authorized_users: list[str] = field(default_factory=list)


@dataclass
class NLParserConfig:
    model: str = "claude-haiku"
    max_tokens: int = 500


@dataclass
class AgentsDefaultConfig:
    heartbeat_interval_seconds: int = 30
    stuck_timeout_seconds: int = 600
    graceful_shutdown_timeout_seconds: int = 30


@dataclass
class SchedulingConfig:
    rolling_window_hours: int = 24
    min_task_guarantee: bool = True


@dataclass
class PauseRetryConfig:
    rate_limit_backoff_seconds: int = 60
    token_exhaustion_retry_seconds: int = 300


@dataclass
class AppConfig:
    workspace_dir: str = field(
        default_factory=lambda: os.path.expanduser("~/agent-queue-workspaces")
    )
    database_path: str = field(
        default_factory=lambda: os.path.expanduser("~/.agent-queue/agent-queue.db")
    )
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    nl_parser: NLParserConfig = field(default_factory=NLParserConfig)
    agents_config: AgentsDefaultConfig = field(default_factory=AgentsDefaultConfig)
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)
    pause_retry: PauseRetryConfig = field(default_factory=PauseRetryConfig)
    global_token_budget_daily: int | None = None
    rate_limits: dict[str, dict[str, int]] = field(default_factory=dict)


def _substitute_env_vars(value: str) -> str:
    """Replace ${ENV_VAR} with environment variable values."""
    def replacer(match):
        var_name = match.group(1)
        env_val = os.environ.get(var_name)
        if env_val is None:
            raise ValueError(f"Environment variable {var_name} not set")
        return env_val
    return re.sub(r"\$\{(\w+)\}", replacer, value)


def _process_values(obj):
    """Recursively substitute env vars in all string values."""
    if isinstance(obj, str):
        return _substitute_env_vars(obj)
    if isinstance(obj, dict):
        return {k: _process_values(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_process_values(v) for v in obj]
    return obj


def load_config(path: str) -> AppConfig:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    raw = _process_values(raw)

    config = AppConfig()

    if "workspace_dir" in raw:
        config.workspace_dir = raw["workspace_dir"]
    if "database_path" in raw:
        config.database_path = raw["database_path"]
    if "global_token_budget_daily" in raw:
        config.global_token_budget_daily = raw["global_token_budget_daily"]

    if "discord" in raw:
        d = raw["discord"]
        config.discord = DiscordConfig(
            bot_token=d.get("bot_token", ""),
            guild_id=d.get("guild_id", ""),
            channels=d.get("channels", config.discord.channels),
            authorized_users=d.get("authorized_users", []),
        )

    if "nl_parser" in raw:
        n = raw["nl_parser"]
        config.nl_parser = NLParserConfig(
            model=n.get("model", "claude-haiku"),
            max_tokens=n.get("max_tokens", 500),
        )

    if "agents" in raw:
        a = raw["agents"]
        config.agents_config = AgentsDefaultConfig(
            heartbeat_interval_seconds=a.get("heartbeat_interval_seconds", 30),
            stuck_timeout_seconds=a.get("stuck_timeout_seconds", 600),
            graceful_shutdown_timeout_seconds=a.get(
                "graceful_shutdown_timeout_seconds", 30
            ),
        )

    if "scheduling" in raw:
        s = raw["scheduling"]
        config.scheduling = SchedulingConfig(
            rolling_window_hours=s.get("rolling_window_hours", 24),
            min_task_guarantee=s.get("min_task_guarantee", True),
        )

    if "pause_retry" in raw:
        p = raw["pause_retry"]
        config.pause_retry = PauseRetryConfig(
            rate_limit_backoff_seconds=p.get("rate_limit_backoff_seconds", 60),
            token_exhaustion_retry_seconds=p.get(
                "token_exhaustion_retry_seconds", 300
            ),
        )

    if "rate_limits" in raw:
        config.rate_limits = raw["rate_limits"]

    return config
```

**Step 3: Run tests**

Run: `pytest tests/test_config.py -v`
Expected: All pass

**Step 4: Commit**

```bash
git add src/config.py tests/test_config.py
git commit -m "feat: add YAML config loading with env var substitution"
```

---

## Task 10: Git Manager

**Files:**
- Create: `src/git/__init__.py`
- Create: `src/git/manager.py`
- Create: `tests/test_git_manager.py`

**Step 1: Write git manager tests**

```python
# tests/test_git_manager.py
import subprocess
import pytest
from src.git.manager import GitManager


@pytest.fixture
def git_repo(tmp_path):
    """Create a bare remote + working clone for testing."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                   capture_output=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(remote), str(clone)], check=True,
                   capture_output=True)
    # Create initial commit
    (clone / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=str(clone), check=True,
                   capture_output=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=t@t.com",
                     "commit", "-m", "init"], cwd=str(clone), check=True,
                   capture_output=True)
    subprocess.run(["git", "push"], cwd=str(clone), check=True,
                   capture_output=True)
    return {"remote": str(remote), "clone": str(clone)}


class TestGitManager:
    def test_create_checkout(self, git_repo, tmp_path):
        mgr = GitManager()
        checkout_path = str(tmp_path / "agent-1" / "repo")
        mgr.create_checkout(git_repo["remote"], checkout_path)
        assert (tmp_path / "agent-1" / "repo" / "README.md").exists()

    def test_create_branch(self, git_repo):
        mgr = GitManager()
        mgr.create_branch(git_repo["clone"], "task-1/do-thing")
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=git_repo["clone"], capture_output=True, text=True,
        )
        assert result.stdout.strip() == "task-1/do-thing"

    def test_prepare_for_task(self, git_repo):
        mgr = GitManager()
        mgr.prepare_for_task(git_repo["clone"], "task-1/new-feature")
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=git_repo["clone"], capture_output=True, text=True,
        )
        assert result.stdout.strip() == "task-1/new-feature"

    def test_validate_checkout(self, git_repo):
        mgr = GitManager()
        assert mgr.validate_checkout(git_repo["clone"])
        assert not mgr.validate_checkout("/nonexistent/path")

    def test_slugify(self):
        mgr = GitManager()
        assert mgr.slugify("Implement OAuth Login!") == "implement-oauth-login"
        assert mgr.slugify("fix  multiple   spaces") == "fix-multiple-spaces"
```

**Step 2: Implement git manager**

```python
# src/git/__init__.py
# (empty)
```

```python
# src/git/manager.py
from __future__ import annotations

import os
import re
import subprocess


class GitError(Exception):
    pass


class GitManager:
    def _run(self, args: list[str], cwd: str | None = None) -> str:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def create_checkout(self, repo_url: str, checkout_path: str) -> None:
        os.makedirs(os.path.dirname(checkout_path), exist_ok=True)
        self._run(["clone", repo_url, checkout_path])

    def validate_checkout(self, checkout_path: str) -> bool:
        if not os.path.isdir(checkout_path):
            return False
        try:
            self._run(["rev-parse", "--git-dir"], cwd=checkout_path)
            return True
        except GitError:
            return False

    def create_branch(self, checkout_path: str, branch_name: str) -> None:
        self._run(["checkout", "-b", branch_name], cwd=checkout_path)

    def prepare_for_task(
        self, checkout_path: str, branch_name: str,
        default_branch: str = "main",
    ) -> None:
        self._run(["fetch", "origin"], cwd=checkout_path)
        self._run(["checkout", default_branch], cwd=checkout_path)
        try:
            self._run(["pull", "origin", default_branch], cwd=checkout_path)
        except GitError:
            pass  # may fail if no upstream tracking
        self._run(["checkout", "-b", branch_name], cwd=checkout_path)

    def push_branch(self, checkout_path: str, branch_name: str) -> None:
        self._run(["push", "origin", branch_name], cwd=checkout_path)

    def merge_branch(
        self, checkout_path: str, branch_name: str,
        default_branch: str = "main",
    ) -> bool:
        """Merge branch into default. Returns True if successful, False if conflict."""
        self._run(["checkout", default_branch], cwd=checkout_path)
        try:
            self._run(["merge", branch_name], cwd=checkout_path)
            return True
        except GitError:
            self._run(["merge", "--abort"], cwd=checkout_path)
            return False

    def get_changed_files(self, checkout_path: str, base_branch: str = "main") -> list[str]:
        try:
            output = self._run(
                ["diff", "--name-only", base_branch], cwd=checkout_path
            )
            return output.split("\n") if output else []
        except GitError:
            return []

    @staticmethod
    def slugify(text: str) -> str:
        text = text.lower().strip()
        text = re.sub(r"[^\w\s-]", "", text)
        text = re.sub(r"[\s_]+", "-", text)
        text = re.sub(r"-+", "-", text)
        return text.strip("-")

    @staticmethod
    def make_branch_name(task_id: str, title: str) -> str:
        return f"{task_id}/{GitManager.slugify(title)}"
```

**Step 3: Run tests**

Run: `pytest tests/test_git_manager.py -v`
Expected: All pass

**Step 4: Commit**

```bash
git add src/git/ tests/test_git_manager.py
git commit -m "feat: add git manager for checkout and branch operations"
```

---

## Task 11: Token Tracker & Budget

**Files:**
- Create: `src/tokens/__init__.py`
- Create: `src/tokens/tracker.py`
- Create: `src/tokens/budget.py`
- Create: `tests/test_budget.py`

**Step 1: Write budget tests**

```python
# tests/test_budget.py
import time
import pytest
from src.tokens.budget import BudgetManager


class TestBudgetManager:
    def test_proportional_ratios(self):
        mgr = BudgetManager()
        weights = {"p-1": 3.0, "p-2": 1.0}
        ratios = mgr.calculate_target_ratios(weights)
        assert ratios["p-1"] == pytest.approx(0.75)
        assert ratios["p-2"] == pytest.approx(0.25)

    def test_deficit_calculation(self):
        mgr = BudgetManager()
        weights = {"p-1": 3.0, "p-2": 1.0}
        usage = {"p-1": 60000, "p-2": 40000}  # total 100k
        deficits = mgr.calculate_deficits(weights, usage)
        # p-1: target 75%, actual 60%, deficit = 0.15
        assert deficits["p-1"] == pytest.approx(0.15)
        # p-2: target 25%, actual 40%, deficit = -0.15
        assert deficits["p-2"] == pytest.approx(-0.15)

    def test_zero_usage_equal_deficit(self):
        mgr = BudgetManager()
        weights = {"p-1": 1.0, "p-2": 1.0}
        usage = {}
        deficits = mgr.calculate_deficits(weights, usage)
        assert deficits["p-1"] == pytest.approx(0.5)
        assert deficits["p-2"] == pytest.approx(0.5)

    def test_global_budget_check(self):
        mgr = BudgetManager(global_budget=100000)
        assert mgr.is_global_budget_exhausted(99999) is False
        assert mgr.is_global_budget_exhausted(100000) is True
        assert mgr.is_global_budget_exhausted(100001) is True

    def test_no_global_budget(self):
        mgr = BudgetManager(global_budget=None)
        assert mgr.is_global_budget_exhausted(999999999) is False

    def test_project_budget_check(self):
        mgr = BudgetManager()
        assert mgr.is_project_budget_exhausted(50000, budget_limit=50000) is True
        assert mgr.is_project_budget_exhausted(49999, budget_limit=50000) is False
        assert mgr.is_project_budget_exhausted(99999, budget_limit=None) is False
```

**Step 2: Implement budget manager**

```python
# src/tokens/__init__.py
# (empty)
```

```python
# src/tokens/budget.py
from __future__ import annotations


class BudgetManager:
    def __init__(self, global_budget: int | None = None):
        self.global_budget = global_budget

    def calculate_target_ratios(
        self, weights: dict[str, float]
    ) -> dict[str, float]:
        total = sum(weights.values())
        if total == 0:
            return {}
        return {pid: w / total for pid, w in weights.items()}

    def calculate_deficits(
        self, weights: dict[str, float], usage: dict[str, int]
    ) -> dict[str, float]:
        targets = self.calculate_target_ratios(weights)
        total_usage = sum(usage.values())
        if total_usage == 0:
            return dict(targets)
        result = {}
        for pid, target in targets.items():
            actual = usage.get(pid, 0) / total_usage
            result[pid] = target - actual
        return result

    def is_global_budget_exhausted(self, total_used: int) -> bool:
        if self.global_budget is None:
            return False
        return total_used >= self.global_budget

    def is_project_budget_exhausted(
        self, project_used: int, budget_limit: int | None
    ) -> bool:
        if budget_limit is None:
            return False
        return project_used >= budget_limit
```

```python
# src/tokens/tracker.py
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class RateLimitWindow:
    agent_type: str
    limit_type: str  # per_minute, per_hour, per_day
    max_tokens: int
    current_tokens: int = 0
    window_start: float = 0.0

    def __post_init__(self):
        if self.window_start == 0.0:
            self.window_start = time.time()

    @property
    def window_seconds(self) -> int:
        return {"per_minute": 60, "per_hour": 3600, "per_day": 86400}[
            self.limit_type
        ]

    def is_exceeded(self) -> bool:
        if time.time() - self.window_start > self.window_seconds:
            return False  # window has reset
        return self.current_tokens >= self.max_tokens

    def seconds_until_reset(self) -> float:
        elapsed = time.time() - self.window_start
        remaining = self.window_seconds - elapsed
        return max(0.0, remaining)

    def record(self, tokens: int) -> None:
        now = time.time()
        if now - self.window_start > self.window_seconds:
            self.current_tokens = 0
            self.window_start = now
        self.current_tokens += tokens
```

**Step 3: Run tests**

Run: `pytest tests/test_budget.py -v`
Expected: All pass

**Step 4: Commit**

```bash
git add src/tokens/ tests/test_budget.py
git commit -m "feat: add token budget manager and rate limit tracking"
```

---

## Task 12: Orchestrator Core (Integration)

**Files:**
- Create: `src/orchestrator.py`
- Create: `tests/test_orchestrator.py`

This task integrates the state machine, scheduler, database, and event bus into the core orchestration loop. This is the largest task — it wires everything together.

**Step 1: Write integration tests with mock adapters**

```python
# tests/test_orchestrator.py
import pytest
from src.orchestrator import Orchestrator
from src.database import Database
from src.models import (
    Project, Task, Agent, TaskStatus, AgentState, AgentResult,
    TaskContext, AgentOutput,
)
from src.adapters.base import AgentAdapter
from src.config import AppConfig


class MockAdapter(AgentAdapter):
    def __init__(self, result=AgentResult.COMPLETED, tokens=1000):
        self._result = result
        self._tokens = tokens

    async def start(self, task): pass
    async def wait(self):
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


@pytest.fixture
async def orch(tmp_path):
    config = AppConfig(
        database_path=str(tmp_path / "test.db"),
        workspace_dir=str(tmp_path / "workspaces"),
    )
    o = Orchestrator(config, adapter_factory=MockAdapterFactory())
    await o.initialize()
    yield o
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

        # Neither should be scheduled — t-1 is DEFINED, t-2 depends on t-1
        await orch.run_one_cycle()

        t1 = await orch.db.get_task("t-1")
        t2 = await orch.db.get_task("t-2")
        assert t1.status == TaskStatus.DEFINED
        assert t2.status == TaskStatus.DEFINED
```

**Step 2: Implement orchestrator**

```python
# src/orchestrator.py
from __future__ import annotations

import time

from src.config import AppConfig
from src.database import Database
from src.event_bus import EventBus
from src.models import (
    AgentResult, AgentState, Task, TaskStatus,
)
from src.scheduler import AssignAction, Scheduler, SchedulerState
from src.state_machine import task_transition, TaskEvent, InvalidTransition
from src.tokens.budget import BudgetManager


class Orchestrator:
    def __init__(self, config: AppConfig, adapter_factory=None):
        self.config = config
        self.db = Database(config.database_path)
        self.bus = EventBus()
        self.budget = BudgetManager(
            global_budget=config.global_token_budget_daily
        )
        self._adapter_factory = adapter_factory
        self._adapters: dict[str, object] = {}  # agent_id -> adapter

    async def initialize(self) -> None:
        await self.db.initialize()

    async def shutdown(self) -> None:
        await self.db.close()

    async def run_one_cycle(self) -> None:
        """Run one complete scheduling + execution cycle."""
        # 1. Check for PAUSED tasks that should resume
        await self._resume_paused_tasks()

        # 2. Check DEFINED tasks for dependency resolution
        await self._check_defined_tasks()

        # 3. Schedule
        actions = await self._schedule()

        # 4. Execute assigned tasks
        for action in actions:
            await self._execute_task(action)

    async def _resume_paused_tasks(self) -> None:
        paused = await self.db.list_tasks(status=TaskStatus.PAUSED)
        now = time.time()
        for task in paused:
            if task.resume_after and task.resume_after <= now:
                await self.db.update_task(task.id, status=TaskStatus.READY.value,
                                          assigned_agent_id=None, resume_after=None)

    async def _check_defined_tasks(self) -> None:
        defined = await self.db.list_tasks(status=TaskStatus.DEFINED)
        for task in defined:
            deps_met = await self.db.are_dependencies_met(task.id)
            deps = await self.db.get_dependencies(task.id)
            if deps_met or not deps:
                await self.db.update_task(task.id, status=TaskStatus.READY.value)

    async def _schedule(self) -> list[AssignAction]:
        projects = await self.db.list_projects()
        tasks = await self.db.list_tasks()
        agents = await self.db.list_agents()

        # Calculate token usage in window
        window_start = time.time() - (
            self.config.scheduling.rolling_window_hours * 3600
        )
        project_usage = {}
        for p in projects:
            project_usage[p.id] = await self.db.get_project_token_usage(
                p.id, since=window_start
            )

        # Count active agents per project
        active_counts: dict[str, int] = {}
        for a in agents:
            if a.state in (AgentState.BUSY, AgentState.STARTING) and a.current_task_id:
                task = await self.db.get_task(a.current_task_id)
                if task:
                    active_counts[task.project_id] = (
                        active_counts.get(task.project_id, 0) + 1
                    )

        total_used = sum(project_usage.values())

        state = SchedulerState(
            projects=projects,
            tasks=tasks,
            agents=agents,
            project_token_usage=project_usage,
            project_active_agent_counts=active_counts,
            tasks_completed_in_window={},  # TODO: query from events
            global_budget=self.config.global_token_budget_daily,
            global_tokens_used=total_used,
        )

        return Scheduler.schedule(state)

    async def _execute_task(self, action: AssignAction) -> None:
        # Assign
        await self.db.assign_task_to_agent(action.task_id, action.agent_id)

        # Start agent
        await self.db.update_task(action.task_id,
                                  status=TaskStatus.IN_PROGRESS.value)
        await self.db.update_agent(action.agent_id, state=AgentState.BUSY)

        adapter = self._adapter_factory.create("claude")
        self._adapters[action.agent_id] = adapter

        task = await self.db.get_task(action.task_id)
        from src.models import TaskContext
        ctx = TaskContext(description=task.description)
        await adapter.start(ctx)
        output = await adapter.wait()

        # Record tokens
        if output.tokens_used > 0:
            await self.db.record_token_usage(
                action.project_id, action.agent_id,
                action.task_id, output.tokens_used,
            )

        # Handle result
        if output.result == AgentResult.COMPLETED:
            await self.db.update_task(action.task_id,
                                      status=TaskStatus.VERIFYING.value)
            # Auto-verify for now (run test commands later)
            await self.db.update_task(action.task_id,
                                      status=TaskStatus.COMPLETED.value)
            await self.db.log_event("task_completed",
                                    project_id=action.project_id,
                                    task_id=action.task_id,
                                    agent_id=action.agent_id)

        elif output.result == AgentResult.FAILED:
            task = await self.db.get_task(action.task_id)
            new_retry = task.retry_count + 1
            if new_retry >= task.max_retries:
                await self.db.update_task(action.task_id,
                                          status=TaskStatus.BLOCKED.value,
                                          retry_count=new_retry)
            else:
                await self.db.update_task(action.task_id,
                                          status=TaskStatus.READY.value,
                                          retry_count=new_retry,
                                          assigned_agent_id=None)

        elif output.result in (
            AgentResult.PAUSED_TOKENS, AgentResult.PAUSED_RATE_LIMIT
        ):
            retry_secs = (
                self.config.pause_retry.rate_limit_backoff_seconds
                if output.result == AgentResult.PAUSED_RATE_LIMIT
                else self.config.pause_retry.token_exhaustion_retry_seconds
            )
            await self.db.update_task(
                action.task_id,
                status=TaskStatus.PAUSED.value,
                resume_after=time.time() + retry_secs,
            )

        # Free agent
        await self.db.update_agent(action.agent_id,
                                   state=AgentState.IDLE,
                                   current_task_id=None)
```

**Step 3: Run tests**

Run: `pytest tests/test_orchestrator.py -v`
Expected: All pass

**Step 4: Commit**

```bash
git add src/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: add orchestrator core with scheduling and execution"
```

---

## Task 13: Discord Bot — Core Setup

**Files:**
- Create: `src/discord/__init__.py`
- Create: `src/discord/bot.py`
- Create: `src/discord/commands.py`
- Create: `src/discord/notifications.py`
- Create: `src/discord/nl_parser.py`

This task creates the Discord bot structure. Discord bots require a live connection to test, so this task focuses on the code structure with testable command handlers separated from Discord-specific wiring.

**Step 1: Create Discord module files**

The bot setup, slash commands, notification formatter, and NL parser are created as separate modules. Command handlers are pure async functions that take parsed arguments and return response strings — making them testable without a live Discord connection.

See design doc Section 5 for the full command set. Implement `/project`, `/task`, `/agent`, `/budget`, and `/status` command groups, plus the natural language fallback and notification formatting.

**Step 2: Write tests for command handlers (pure functions)**

```python
# tests/test_discord_commands.py
import pytest
from src.database import Database
from src.models import Project, Task, Agent, TaskStatus


# Test the command handler functions directly, not through Discord
# These are extracted as pure functions that return response strings

# Tests deferred to integration — Discord commands depend on the full
# database and orchestrator being wired up. Create handler stubs and
# test the formatting/parsing logic.
```

**Step 3: Commit**

```bash
git add src/discord/ tests/test_discord_commands.py
git commit -m "feat: add Discord bot structure with command handlers"
```

---

## Task 14: Main Entry Point

**Files:**
- Modify: `src/main.py`

Wire everything together: config loading, database init, orchestrator, Discord bot, signal handlers, asyncio event loop.

**Step 1: Implement main entry point**

```python
# src/main.py
from __future__ import annotations

import asyncio
import os
import signal
import sys

from src.config import load_config
from src.orchestrator import Orchestrator


DEFAULT_CONFIG_PATH = os.path.expanduser("~/.agent-queue/config.yaml")


async def run(config_path: str) -> None:
    config = load_config(config_path)

    # Ensure database directory exists
    os.makedirs(os.path.dirname(config.database_path), exist_ok=True)

    orch = Orchestrator(config)
    await orch.initialize()

    shutdown_event = asyncio.Event()

    def handle_signal():
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    try:
        # Main loop: run scheduling cycles
        while not shutdown_event.is_set():
            await orch.run_one_cycle()
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
    finally:
        await orch.shutdown()


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH
    asyncio.run(run(config_path))


if __name__ == "__main__":
    main()
```

**Step 2: Commit**

```bash
git add src/main.py
git commit -m "feat: add main entry point with signal handling"
```

---

## Summary

| Task | Component | Dependencies |
|------|-----------|-------------|
| 1 | Project scaffolding | — |
| 2 | Models & enums | 1 |
| 3 | Task state machine | 2 |
| 4 | DAG validation | 3 |
| 5 | Database layer | 2 |
| 6 | Event bus | 1 |
| 7 | Scheduler | 2 |
| 8 | Agent adapters | 2 |
| 9 | Config loading | 1 |
| 10 | Git manager | 1 |
| 11 | Token/budget manager | 1 |
| 12 | Orchestrator core | 3, 4, 5, 6, 7, 8, 11 |
| 13 | Discord bot | 5, 12 |
| 14 | Main entry point | 9, 12 |

Tasks 1-11 can be parallelized in groups (1 first, then 2-11 mostly in parallel). Tasks 12-14 are sequential and depend on earlier work.
