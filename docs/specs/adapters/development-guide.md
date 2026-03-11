# Adapter Development Guide

How to add a new agent adapter to agent-queue so third-party agent backends
(Codex, Cursor, Aider, etc.) can be integrated.

---

## 1. Architecture Overview

The adapter layer sits between the orchestrator and AI coding agents. The
orchestrator never talks to a specific agent implementation directly — it
requests an adapter from `AdapterFactory` by type string (e.g. `"claude"`)
and interacts through the `AgentAdapter` ABC defined in `src/adapters/base.py`.

```
Orchestrator  →  AdapterFactory.create("your-agent")  →  YourAdapter
                                                            ├── start(task)
                                                            ├── wait(on_message)
                                                            ├── stop()
                                                            └── is_alive()
```

**Key source files:**

| File | Purpose |
|------|---------|
| `src/adapters/base.py` | `AgentAdapter` ABC and `MessageCallback` type |
| `src/adapters/__init__.py` | `AdapterFactory` — registry of adapter types |
| `src/adapters/claude.py` | Reference implementation (Claude Code via SDK) |
| `src/models.py` | `TaskContext`, `AgentOutput`, `AgentResult` dataclasses |

---

## 2. The AgentAdapter Interface

```python
from abc import ABC, abstractmethod
from typing import Callable, Awaitable
from src.models import AgentOutput, TaskContext

MessageCallback = Callable[[str], Awaitable[None]]

class AgentAdapter(ABC):
    @abstractmethod
    async def start(self, task: TaskContext) -> None:
        """Prepare to run the given task. Store context, reset state."""

    @abstractmethod
    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:
        """Execute the task. Stream progress via on_message. Return result."""

    @abstractmethod
    async def stop(self) -> None:
        """Request cancellation. Must be safe to call at any time."""

    @abstractmethod
    async def is_alive(self) -> bool:
        """Return True if the agent is still running."""
```

### Lifecycle

1. **`start(task)`** — The orchestrator calls this first. Store the `TaskContext`
   and reset any internal state (cancel flags, session IDs, etc.). Do **not**
   launch the agent process here — that happens in `wait()`.

2. **`wait(on_message)`** — This is where the real work happens. Launch your agent,
   stream progress updates through the `on_message` callback (which the
   orchestrator wires to a Discord thread), and block until the agent finishes.
   Return an `AgentOutput` describing the outcome.

3. **`stop()`** — Request graceful cancellation. The orchestrator may call this
   from a different coroutine while `wait()` is running. Use an `asyncio.Event`
   or similar mechanism to signal cancellation cooperatively.

4. **`is_alive()`** — The orchestrator's heartbeat monitor calls this periodically.
   Return `True` if `start()` has been called and the agent hasn't finished or
   been cancelled.

---

## 3. Data Types Your Adapter Receives and Returns

### TaskContext (input)

The adapter's entire view of the work to be done. Constructed by the orchestrator
from the task database record.

```python
@dataclass
class TaskContext:
    description: str                              # Primary task prompt (always set)
    task_id: str = ""                             # Unique task identifier
    acceptance_criteria: list[str] = field(...)   # Success conditions
    test_commands: list[str] = field(...)         # Commands to verify output
    checkout_path: str = ""                       # Working directory for the agent
    branch_name: str = ""                         # Git branch name
    attached_context: list[str] = field(...)      # Additional context strings
    mcp_servers: dict[str, dict] = field(...)     # MCP server configs (name → dict)
```

**Important fields for your adapter:**

- `description` — Always present. This is the task prompt your agent should execute.
- `checkout_path` — The workspace directory. Set this as the CWD for your agent process.
- `acceptance_criteria` / `test_commands` — Include these in the prompt you send
  to your agent so it knows the definition of done.
- `mcp_servers` — If your agent supports MCP (Model Context Protocol), forward these.

### AgentOutput (output)

What your adapter must return from `wait()`.

```python
@dataclass
class AgentOutput:
    result: AgentResult          # Required: outcome enum
    summary: str = ""            # Human-readable description of what happened
    files_changed: list[str] = field(default_factory=list)
    tokens_used: int = 0         # Total tokens consumed (input + output)
    error_message: str | None = None   # Set on failure
    question: str | None = None        # Set when result is WAITING_INPUT
```

### AgentResult (outcome enum)

```python
class AgentResult(Enum):
    COMPLETED = "completed"            # Task finished successfully
    FAILED = "failed"                  # Task failed (error_message should be set)
    PAUSED_TOKENS = "paused_tokens"    # Token budget exhausted, will retry later
    PAUSED_RATE_LIMIT = "paused_rate_limit"  # Rate limited, will retry later
    WAITING_INPUT = "waiting_input"    # Agent needs human input (set question field)
```

The orchestrator maps these to state machine transitions:

| AgentResult | Orchestrator action |
|-------------|---------------------|
| `COMPLETED` | Task → COMPLETED, create PR |
| `FAILED` | Task → FAILED (or retry if retries remain) |
| `PAUSED_TOKENS` | Task → PAUSED with resume_after timestamp |
| `PAUSED_RATE_LIMIT` | Task → PAUSED with resume_after timestamp |
| `WAITING_INPUT` | Task → WAITING_INPUT, notify Discord |

### MessageCallback

```python
MessageCallback = Callable[[str], Awaitable[None]]
```

An async function that accepts a single string. Call it with human-readable
progress updates (markdown-formatted text works well). The orchestrator forwards
these to a Discord thread so users can watch the agent work in real time.

**Guidelines for message content:**

- Keep messages concise — Discord has a 2000-character limit per message.
- Use markdown formatting (bold, code blocks, etc.) for readability.
- Don't send every line of output — summarize tool usage, show key decisions.
- It's fine to call `on_message` frequently; the orchestrator batches if needed.

---

## 4. Step-by-Step: Creating a New Adapter

### Step 1: Create the adapter module

Create `src/adapters/your_agent.py`:

```python
"""Your Agent adapter — runs tasks via the Your Agent CLI/API.

See docs/specs/adapters/development-guide.md for the adapter contract.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from src.adapters.base import AgentAdapter, MessageCallback
from src.models import AgentOutput, AgentResult, TaskContext


@dataclass
class YourAgentConfig:
    """Configuration for the Your Agent adapter.

    Add any agent-specific settings here (API keys, model selection,
    tool permissions, etc.)
    """
    model: str = ""
    timeout_seconds: int = 3600  # 1 hour default


class YourAgentAdapter(AgentAdapter):
    """AgentAdapter implementation for Your Agent.

    Each task gets a fresh session. The adapter streams messages back
    through the on_message callback and collects the final result.
    """

    def __init__(self, config: YourAgentConfig | None = None, llm_logger=None):
        self._config = config or YourAgentConfig()
        self._task: TaskContext | None = None
        self._cancel_event = asyncio.Event()
        self._process = None  # Track your subprocess/session here
        self._llm_logger = llm_logger

    async def start(self, task: TaskContext) -> None:
        """Store task context and reset state. Don't launch the agent yet."""
        self._task = task
        self._cancel_event.clear()
        self._process = None

    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:
        """Launch the agent and wait for completion."""
        if not self._task:
            return AgentOutput(
                result=AgentResult.FAILED,
                error_message="start() was not called before wait()",
            )

        tokens_used = 0
        summary_parts = []

        try:
            # Build the prompt from TaskContext
            prompt = self._build_prompt()

            # TODO: Launch your agent here
            # Example: self._process = await asyncio.create_subprocess_exec(...)

            # TODO: Stream output and check for cancellation
            # while agent_is_running:
            #     if self._cancel_event.is_set():
            #         # Clean up the process
            #         return AgentOutput(
            #             result=AgentResult.FAILED,
            #             summary="Cancelled",
            #             error_message="Agent was stopped",
            #         )
            #
            #     message = await get_next_message()
            #     if on_message and message:
            #         await on_message(message)
            #
            #     # Accumulate results
            #     summary_parts.append(message)

            return AgentOutput(
                result=AgentResult.COMPLETED,
                summary="\n".join(summary_parts) or "Completed",
                tokens_used=tokens_used,
            )

        except Exception as e:
            # Distinguish token/rate-limit errors from general failures
            error_msg = str(e)
            if "token" in error_msg.lower() or "quota" in error_msg.lower():
                return AgentOutput(
                    result=AgentResult.PAUSED_TOKENS,
                    error_message=error_msg,
                )
            if "rate" in error_msg.lower() and "limit" in error_msg.lower():
                return AgentOutput(
                    result=AgentResult.PAUSED_RATE_LIMIT,
                    error_message=error_msg,
                )
            return AgentOutput(
                result=AgentResult.FAILED,
                error_message=error_msg,
            )

    async def stop(self) -> None:
        """Signal cancellation. Clean up if a process is running."""
        self._cancel_event.set()
        # TODO: Kill your subprocess if applicable
        # if self._process:
        #     self._process.terminate()

    async def is_alive(self) -> bool:
        """Check if the agent is still running."""
        return self._task is not None and not self._cancel_event.is_set()

    def _build_prompt(self) -> str:
        """Assemble the full prompt from TaskContext fields."""
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

### Step 2: Register in AdapterFactory

Edit `src/adapters/__init__.py` to import your adapter and add it to the factory:

```python
from src.adapters.your_agent import YourAgentAdapter, YourAgentConfig

class AdapterFactory:
    def __init__(self, claude_config=None, your_agent_config=None, llm_logger=None):
        self._claude_config = claude_config or ClaudeAdapterConfig()
        self._your_agent_config = your_agent_config or YourAgentConfig()
        self._llm_logger = llm_logger

    def create(self, agent_type: str, profile=None) -> AgentAdapter:
        if agent_type == "claude":
            config = self._config_for_profile(profile)
            return ClaudeAdapter(config, llm_logger=self._llm_logger)
        if agent_type == "your-agent":
            return YourAgentAdapter(self._your_agent_config, llm_logger=self._llm_logger)
        raise ValueError(f"Unknown agent type: {agent_type}")
```

### Step 3: Add configuration support

If your adapter needs configuration from `config.yaml`, add a config section:

```yaml
adapters:
  your_agent:
    model: "your-model-id"
    timeout_seconds: 3600
```

Then wire it through `AppConfig` → `Orchestrator` → `AdapterFactory`.

### Step 3b: Support agent profiles (optional)

The orchestrator supports **agent profiles** (`AgentProfile` in `src/models.py`)
that allow per-task overrides for model, permission mode, tools, and MCP servers.
If your adapter accepts configuration that should be profile-overridable, add a
`_config_for_profile()` method to `AdapterFactory`:

```python
def _config_for_your_agent_profile(
    self, profile: AgentProfile | None,
) -> YourAgentConfig:
    """Merge profile overrides into the base YourAgentConfig."""
    if profile is None:
        return self._your_agent_config
    return YourAgentConfig(
        model=profile.model or self._your_agent_config.model,
        timeout_seconds=self._your_agent_config.timeout_seconds,
        # ... merge other profile-overridable fields
    )
```

`AgentProfile` fields available for merging:

| Field | Type | Purpose |
|-------|------|---------|
| `id` | `str` | Profile slug (e.g. `"reviewer"`, `"web-developer"`) |
| `model` | `str` | Model override (empty = use adapter default) |
| `permission_mode` | `str` | Permission mode override (empty = use default) |
| `allowed_tools` | `list[str]` | Tool whitelist override (empty = use default) |
| `mcp_servers` | `dict` | Additional MCP server configs |
| `system_prompt_suffix` | `str` | Text appended to agent instructions |

The factory's `create()` method receives the profile from the orchestrator and
passes it through to the config merger. See `AdapterFactory._config_for_profile()`
in `src/adapters/__init__.py` for the Claude implementation.

### Step 3c: Integrate LLM session logging (recommended)

The orchestrator passes an optional `llm_logger` (an `LLMLogger` instance from
`src/llm_logger.py`) to `AdapterFactory`, which forwards it to each adapter.
Logging agent sessions enables cost tracking, token analytics, and the
`/llm-stats` Discord command.

Accept the logger in your constructor and call it after each execution:

```python
def __init__(self, config=None, llm_logger=None):
    self._config = config or YourAgentConfig()
    self._llm_logger = llm_logger  # May be None

# At the end of wait(), after determining the AgentOutput:
def _log_session(self, prompt: str, output: AgentOutput,
                  start: float, time_mod) -> None:
    if not self._llm_logger:
        return
    duration_ms = int((time_mod.monotonic() - start) * 1000)
    self._llm_logger.log_agent_session(
        task_id=self._task.task_id if self._task else "",
        session_id=self._session_id,
        model=self._config.model or "(default)",
        prompt=prompt,
        config_summary={
            "timeout_seconds": self._config.timeout_seconds,
            # ... other relevant config fields
        },
        output=output,
        duration_ms=duration_ms,
    )
```

Call `_log_session()` on **every** exit path from `wait()` — success, failure,
cancellation, and token exhaustion — so analytics are complete.

### Step 4: Write tests

Create `tests/test_your_agent_adapter.py`. See the testing section below.

---

## 5. Common Pitfalls

### Cancellation must be cooperative

The orchestrator calls `stop()` from a different coroutine than `wait()`. You
**cannot** raise an exception in `wait()` from `stop()`. Instead:

- Use `asyncio.Event` as a cancellation flag.
- Check the flag frequently in your `wait()` loop (every message, every poll).
- Clean up resources (kill subprocesses, close connections) when cancelled.

```python
# In wait():
if self._cancel_event.is_set():
    await self._cleanup()
    return AgentOutput(result=AgentResult.FAILED, error_message="Cancelled")
```

### Heartbeat and is_alive()

The orchestrator's heartbeat monitor calls `is_alive()` periodically to detect
stuck or dead agents. Your implementation must:

- Return `True` while the agent is actively running.
- Return `False` after `stop()` is called or after `wait()` returns.
- **Not block** — `is_alive()` should be a fast, synchronous check.

If `is_alive()` returns `False` while the orchestrator expects the agent to be
running, the task may be marked as failed and retried. Make sure your
implementation accurately reflects the agent's state.

### Token counting

The orchestrator uses `AgentOutput.tokens_used` for budget tracking and
cost reporting. If your agent's API provides token usage information:

- Sum both input and output tokens into a single `tokens_used` value.
- If token data is unavailable, return `0` — don't guess.
- If your agent hits a token limit, return `AgentResult.PAUSED_TOKENS` so the
  orchestrator can reschedule rather than permanently failing the task.

### Error classification matters

The orchestrator handles different `AgentResult` values differently:

- **`FAILED`** → may retry (if retries remain) or mark task as failed permanently.
- **`PAUSED_TOKENS`** / **`PAUSED_RATE_LIMIT`** → task enters PAUSED state with
  a resume timer. The orchestrator will automatically retry later.
- **`WAITING_INPUT`** → task enters WAITING_INPUT state; Discord notification sent.

Classify errors correctly. A rate limit is not a "failure" — it's a temporary
pause. Token exhaustion is not a permanent failure. Misclassification leads to
unnecessary retries or premature task abandonment.

### Subprocess management

If your adapter runs an external process:

- Always clean up on cancellation, errors, and normal completion.
- Use `asyncio.create_subprocess_exec()` rather than `subprocess.Popen()` to
  stay non-blocking.
- Set `cwd` to `task.checkout_path` so the agent works in the correct workspace.
- Strip environment variables that could confuse nested agent sessions (see
  `ClaudeAdapter` for an example of stripping `CLAUDECODE` env vars).

### Streaming output to Discord

The `on_message` callback forwards text to a Discord thread. Keep in mind:

- Discord messages have a 2000-character limit. The orchestrator handles
  truncation, but sending enormous strings is wasteful.
- Don't flood with trivial updates. Summarize tool usage rather than echoing
  every line of subprocess output.
- It's fine to send nothing — `on_message` is optional and may be `None`.
- Always check `if on_message:` before calling it.

### Don't block the event loop

All adapter methods are `async`. If your agent backend has a synchronous API,
wrap blocking calls in `asyncio.to_thread()`:

```python
# Bad: blocks the event loop
result = your_sdk.run_sync(prompt)

# Good: runs in a thread pool
result = await asyncio.to_thread(your_sdk.run_sync, prompt)
```

---

## 6. Reference: ClaudeAdapter

`src/adapters/claude.py` is the canonical example implementation. Key patterns
to study:

| Pattern | How ClaudeAdapter does it |
|---------|--------------------------|
| Config dataclass | `ClaudeAdapterConfig` with model, permission_mode, allowed_tools |
| Cancel flag | `asyncio.Event` checked at the top of each message loop iteration |
| Prompt building | `_build_prompt()` assembles TaskContext into markdown |
| Message streaming | `_extract_message_text()` converts SDK types to Discord-friendly text |
| Error classification | Token/quota errors → `PAUSED_TOKENS`; all others → `FAILED` |
| Session logging | Optional `llm_logger` integration via `_log_session()` |
| Zero-output detection | 0 tokens + no summary → treated as silent failure |
| Subprocess management | Uses `shutil.which("claude")` to find CLI, strips env vars |

See `docs/specs/adapters/claude.md` for the full behavioral specification of
the Claude adapter.

---

## 7. Testing Your Adapter

### Unit test structure

```python
"""Tests for YourAgentAdapter."""

import asyncio
import pytest
from src.adapters.your_agent import YourAgentAdapter, YourAgentConfig
from src.models import AgentOutput, AgentResult, TaskContext


@pytest.fixture
def task_context():
    return TaskContext(
        description="Implement a hello world function",
        task_id="test-task-1",
        checkout_path="/tmp/test-workspace",
        acceptance_criteria=["Function returns 'Hello, World!'"],
        test_commands=["pytest tests/test_hello.py"],
    )


@pytest.fixture
def adapter():
    return YourAgentAdapter(YourAgentConfig())


@pytest.mark.asyncio
async def test_start_stores_task(adapter, task_context):
    await adapter.start(task_context)
    assert adapter._task is task_context


@pytest.mark.asyncio
async def test_stop_sets_cancel_event(adapter, task_context):
    await adapter.start(task_context)
    assert await adapter.is_alive()
    await adapter.stop()
    assert not await adapter.is_alive()


@pytest.mark.asyncio
async def test_wait_without_start_fails(adapter):
    output = await adapter.wait()
    assert output.result == AgentResult.FAILED


@pytest.mark.asyncio
async def test_cancellation_during_wait(adapter, task_context):
    """Verify that stop() causes wait() to return FAILED."""
    await adapter.start(task_context)

    async def cancel_after_delay():
        await asyncio.sleep(0.1)
        await adapter.stop()

    asyncio.create_task(cancel_after_delay())
    output = await adapter.wait()
    assert output.result == AgentResult.FAILED
    assert "cancel" in (output.error_message or "").lower()


@pytest.mark.asyncio
async def test_message_callback_receives_output(adapter, task_context):
    """Verify on_message is called with progress text."""
    messages = []
    await adapter.start(task_context)
    output = await adapter.wait(on_message=lambda msg: messages.append(msg))
    # Assert messages were received (implementation-specific)


@pytest.mark.asyncio
async def test_token_error_returns_paused(adapter, task_context):
    """Verify token exhaustion maps to PAUSED_TOKENS."""
    # Mock your agent to raise a token error, verify output.result
    pass
```

### What to test

- **Lifecycle**: `start` → `wait` → returns `AgentOutput`
- **Cancellation**: `stop()` during `wait()` returns `FAILED`
- **Error mapping**: token errors → `PAUSED_TOKENS`, rate limits → `PAUSED_RATE_LIMIT`
- **Message streaming**: `on_message` callback receives progress updates
- **Edge cases**: `wait()` without `start()`, empty responses, zero tokens

### Integration testing

For integration tests with the real agent backend, use a separate test config
and run against a sandbox environment. Mark these tests with `@pytest.mark.integration`
so they can be skipped in CI:

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_agent_execution():
    adapter = YourAgentAdapter(YourAgentConfig(model="test-model"))
    await adapter.start(TaskContext(
        description="Create a file called hello.txt with 'Hello' in it",
        checkout_path="/tmp/integration-test",
    ))
    output = await adapter.wait()
    assert output.result == AgentResult.COMPLETED
```

---

## 8. Checklist

Before submitting your adapter:

- [ ] Subclasses `AgentAdapter` from `src/adapters/base.py`
- [ ] Implements all four methods: `start`, `wait`, `stop`, `is_alive`
- [ ] `wait()` checks cancellation flag frequently
- [ ] `wait()` streams progress via `on_message` callback
- [ ] `wait()` returns appropriate `AgentResult` for all outcomes
- [ ] Token/rate-limit errors return `PAUSED_*` not `FAILED`
- [ ] `is_alive()` is non-blocking and accurate
- [ ] Registered in `AdapterFactory` with a type string
- [ ] Has a config dataclass for agent-specific settings
- [ ] Sets `cwd` to `task.checkout_path` for subprocess execution
- [ ] Cleans up resources on cancellation, error, and completion
- [ ] Does not block the asyncio event loop
- [ ] Has unit tests covering lifecycle, cancellation, and error mapping
- [ ] Supports `AgentProfile` overrides if applicable (model, tools, etc.)
- [ ] Integrates with `LLMLogger` for session tracking (recommended)
- [ ] Has a spec document in `docs/specs/adapters/`
