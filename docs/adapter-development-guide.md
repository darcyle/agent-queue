# Adapter Development Guide

> **Consolidated:** See [adapter-development.md](adapter-development.md) for the
> full step-by-step guide with code examples, interface documentation, and testing
> instructions.

How to add a new agent adapter to the Agent Queue system.

## Architecture Overview

```
Orchestrator ──► AgentAdapter ──► Agent Process
    │               │                  │
    ├── start()    launch subprocess   │
    ├── wait()     stream output ◄─────┤
    ├── is_alive() check process       │
    └── stop()     kill process        │
```

## The AgentAdapter Interface

Located in `src/adapters/base.py`:

```python
class AgentAdapter(ABC):
    @abstractmethod
    async def start(self, task: TaskContext) -> None:
        """Launch the agent process with the given task."""

    @abstractmethod
    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:
        """Wait for the agent to finish and return results."""

    @abstractmethod
    async def stop(self) -> None:
        """Forcefully stop the agent."""

    @abstractmethod
    async def is_alive(self) -> bool:
        """Check if the agent process is still running."""
```

## Step-by-Step: Adding a New Adapter

### 1. Create the Adapter File

Create `src/adapters/your_agent.py` implementing `AgentAdapter`.

### 2. Register in the Adapter Factory

Update `src/adapters/__init__.py` to include your adapter type.

### 3. Register an Agent

```
/add-agent name:my-agent type:your_agent
```

## TaskContext — What Your Adapter Receives

| Field | Type | Description |
|-------|------|-------------|
| `description` | `str` | Full task description (markdown) |
| `task_id` | `str` | Unique task identifier |
| `acceptance_criteria` | `list[str]` | Success conditions |
| `test_commands` | `list[str]` | Verification commands |
| `checkout_path` | `str` | Absolute path to git worktree |
| `branch_name` | `str` | Git branch for this task |
| `attached_context` | `list[str]` | Additional context |
| `mcp_servers` | `dict` | MCP server configurations |

## AgentOutput — What Your Adapter Returns

| Field | Type | Description |
|-------|------|-------------|
| `result` | `AgentResult` | COMPLETED, FAILED, PAUSED_TOKENS, PAUSED_RATE_LIMIT, or WAITING_INPUT |
| `summary` | `str` | Human-readable summary |
| `files_changed` | `list[str]` | Modified file paths |
| `tokens_used` | `int` | Token count for budget tracking |
| `error_message` | `str \| None` | Error details on failure |
| `question` | `str \| None` | Question when WAITING_INPUT |

## AgentResult Effects

| Result | Orchestrator Action |
|--------|-------------------|
| `COMPLETED` | Task → VERIFYING |
| `FAILED` | Task → FAILED, increment retry_count |
| `PAUSED_TOKENS` | Task → PAUSED with resume_after |
| `PAUSED_RATE_LIMIT` | Task → PAUSED with backoff |
| `WAITING_INPUT` | Task → WAITING_INPUT, notify Discord |

## MessageCallback

Stream real-time output to Discord via the `on_message` callback.
Keep messages under 2000 chars, batch rapid updates.

## Reference

See `src/adapters/claude.py` (~600 lines) for a complete implementation
including environment scrubbing, resilient streaming, rate limit detection,
and graceful shutdown.
