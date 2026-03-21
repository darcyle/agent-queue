# Phase 4: Supervisor Identity + Reflection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform ChatAgent into Supervisor — the single intelligent entity with an action-reflect cycle that wraps every interaction.

**Architecture:** Rename `ChatAgent` → `Supervisor` across all files. Add a reflection system (`src/reflection.py`) that wraps every Supervisor action with a verification pass. Wire reflection configuration into `AppConfig`. The hook engine stops creating fresh ChatAgent instances and instead calls `supervisor.process_hook_llm()`. Reflection depth (deep/standard/light) is determined by trigger type.

**Tech Stack:** Python 3.12+, asyncio, pytest, existing PromptBuilder/RuleManager/ToolRegistry from Phases 1-3.

**Dependencies:** Phases 1 (PromptBuilder), 2 (Rule System), 3 (Tiered Tools) — all completed and merged.

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `src/supervisor.py` | Create (rename from `chat_agent.py`) | Supervisor class — the single intelligent entity |
| `src/chat_agent.py` | Modify → re-export shim | Backward-compat re-export of Supervisor as ChatAgent |
| `src/reflection.py` | Create | ReflectionEngine — action-reflect cycle logic, depth determination, safety controls |
| `src/config.py` | Modify | Add `SupervisorConfig` and `ReflectionConfig` dataclasses |
| `src/hooks.py` | Modify | Replace ChatAgent instantiation with `supervisor.process_hook_llm()` callback |
| `src/discord/bot.py` | Modify | Update imports, `ChatAgent` → `Supervisor` |
| `src/prompts/supervisor_system.md` | Create | Supervisor identity template with three activation modes |
| `src/discord/commands.py` | Modify | Update ChatAgent docstring references |
| `src/chat_providers/logged.py` | Modify | Update default caller string from "chat_agent.chat" to "supervisor.chat" |
| `src/prompt_builder.py` | Modify | Update comment reference to chat_agent |
| `specs/supervisor.md` | Create | Behavioral spec for Supervisor |
| `specs/reflection.md` | Create | Behavioral spec for ReflectionEngine |
| `tests/test_supervisor.py` | Create | Tests for Supervisor class |
| `tests/test_reflection.py` | Create | Tests for ReflectionEngine |

---

### Task 1: Add Supervisor and Reflection Config

**Files:**
- Modify: `src/config.py`
- Test: `tests/test_config_supervisor.py`

- [ ] **Step 1: Write failing tests for new config dataclasses**

```python
# tests/test_config_supervisor.py
"""Tests for SupervisorConfig and ReflectionConfig."""


def test_reflection_config_defaults():
    from src.config import ReflectionConfig
    cfg = ReflectionConfig()
    assert cfg.level == "full"
    assert cfg.periodic_interval == 900
    assert cfg.max_depth == 3
    assert cfg.per_cycle_token_cap == 10000
    assert cfg.hourly_token_circuit_breaker == 100000


def test_reflection_config_validation_valid():
    from src.config import ReflectionConfig
    cfg = ReflectionConfig(level="moderate", max_depth=2)
    errors = cfg.validate()
    assert len(errors) == 0


def test_reflection_config_validation_invalid_level():
    from src.config import ReflectionConfig
    cfg = ReflectionConfig(level="turbo")
    errors = cfg.validate()
    assert any("level" in str(e) for e in errors)


def test_reflection_config_validation_invalid_depth():
    from src.config import ReflectionConfig
    cfg = ReflectionConfig(max_depth=0)
    errors = cfg.validate()
    assert any("max_depth" in str(e) for e in errors)


def test_supervisor_config_defaults():
    from src.config import SupervisorConfig
    cfg = SupervisorConfig()
    assert cfg.reflection is not None
    assert cfg.reflection.level == "full"


def test_supervisor_config_in_app_config():
    from src.config import AppConfig, SupervisorConfig
    app = AppConfig()
    assert hasattr(app, "supervisor")
    assert isinstance(app.supervisor, SupervisorConfig)


def test_reflection_config_off_disables():
    from src.config import ReflectionConfig
    cfg = ReflectionConfig(level="off")
    errors = cfg.validate()
    assert len(errors) == 0


def test_supervisor_config_validation():
    from src.config import SupervisorConfig
    cfg = SupervisorConfig()
    errors = cfg.validate()
    assert len(errors) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config_supervisor.py -v`
Expected: FAIL — `ReflectionConfig` and `SupervisorConfig` don't exist

- [ ] **Step 3: Implement config dataclasses**

Add to `src/config.py` (after `ChatAnalyzerConfig`, before `AppConfig`):

```python
@dataclass
class ReflectionConfig:
    """Configuration for the Supervisor's action-reflect cycle.

    Controls reflection depth, periodic sweeps, and safety limits.
    """

    level: str = "full"  # full | moderate | minimal | off
    periodic_interval: int = 900  # seconds between proactive sweeps
    max_depth: int = 3  # maximum nested reflection iterations
    per_cycle_token_cap: int = 10000  # max tokens per reflection cycle
    hourly_token_circuit_breaker: int = 100000  # drop to minimal if exceeded

    _VALID_LEVELS = {"full", "moderate", "minimal", "off"}

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.level not in self._VALID_LEVELS:
            errors.append(ConfigError(
                "reflection", "level",
                f"must be one of {sorted(self._VALID_LEVELS)}, got '{self.level}'"
            ))
        if self.max_depth < 1:
            errors.append(ConfigError("reflection", "max_depth", "must be >= 1"))
        if self.periodic_interval < 0:
            errors.append(ConfigError(
                "reflection", "periodic_interval", "must be >= 0"
            ))
        if self.per_cycle_token_cap < 0:
            errors.append(ConfigError(
                "reflection", "per_cycle_token_cap", "must be >= 0"
            ))
        if self.hourly_token_circuit_breaker < 0:
            errors.append(ConfigError(
                "reflection", "hourly_token_circuit_breaker", "must be >= 0"
            ))
        return errors


@dataclass
class SupervisorConfig:
    """Top-level Supervisor configuration."""

    reflection: ReflectionConfig = field(default_factory=ReflectionConfig)

    def validate(self) -> list[ConfigError]:
        return self.reflection.validate()
```

Add `supervisor` field to `AppConfig`:

```python
supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)
```

Add to `AppConfig.validate()`:

```python
errors.extend(self.supervisor.validate())
```

Add YAML loading in the config loader function (near `hook_engine` loading):

```python
if "supervisor" in raw:
    s = raw["supervisor"]
    reflection = s.get("reflection", {})
    config.supervisor = SupervisorConfig(
        reflection=ReflectionConfig(
            level=reflection.get("level", "full"),
            periodic_interval=reflection.get("periodic_interval", 900),
            max_depth=reflection.get("max_depth", 3),
            per_cycle_token_cap=reflection.get("per_cycle_token_cap", 10000),
            hourly_token_circuit_breaker=reflection.get("hourly_token_circuit_breaker", 100000),
        ),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config_supervisor.py -v`
Expected: PASS

- [ ] **Step 5: Run existing config tests for regression**

Run: `pytest tests/ -k "config" -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add src/config.py tests/test_config_supervisor.py
git commit -m "Add SupervisorConfig and ReflectionConfig to AppConfig"
```

---

### Task 2: Create ReflectionEngine

**Files:**
- Create: `src/reflection.py`
- Create: `tests/test_reflection.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_reflection.py
"""Tests for ReflectionEngine — action-reflect cycle."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_engine(level="full", max_depth=3, per_cycle_cap=10000, hourly_cap=100000):
    from src.config import ReflectionConfig
    from src.reflection import ReflectionEngine

    config = ReflectionConfig(
        level=level,
        max_depth=max_depth,
        per_cycle_token_cap=per_cycle_cap,
        hourly_token_circuit_breaker=hourly_cap,
    )
    return ReflectionEngine(config)


def test_determine_depth_task_completion():
    engine = _make_engine()
    depth = engine.determine_depth("task.completed", {})
    assert depth == "deep"


def test_determine_depth_user_request():
    engine = _make_engine()
    depth = engine.determine_depth("user.request", {})
    assert depth == "standard"


def test_determine_depth_passive_observation():
    engine = _make_engine()
    depth = engine.determine_depth("passive.observation", {})
    assert depth == "light"


def test_determine_depth_hook_failure():
    engine = _make_engine()
    depth = engine.determine_depth("hook.failed", {})
    assert depth == "deep"


def test_determine_depth_hook_success():
    engine = _make_engine()
    depth = engine.determine_depth("hook.completed", {})
    assert depth == "standard"


def test_determine_depth_periodic():
    engine = _make_engine()
    depth = engine.determine_depth("periodic.sweep", {})
    assert depth == "light"


def test_level_off_always_returns_none():
    engine = _make_engine(level="off")
    depth = engine.determine_depth("task.completed", {})
    assert depth is None


def test_level_minimal_downgrades():
    engine = _make_engine(level="minimal")
    depth = engine.determine_depth("task.completed", {})
    # minimal: only deep for explicit trigger matches
    assert depth == "light"


def test_should_reflect_off():
    engine = _make_engine(level="off")
    assert engine.should_reflect("task.completed") is False


def test_should_reflect_full():
    engine = _make_engine(level="full")
    assert engine.should_reflect("task.completed") is True


def test_build_reflection_prompt_deep():
    engine = _make_engine()
    prompt = engine.build_reflection_prompt(
        depth="deep",
        trigger="task.completed",
        action_summary="Created task fix-login",
        action_results=[{"tool": "create_task", "result": {"id": "t-123"}}],
    )
    assert "Did I do what was asked" in prompt
    assert "verify results" in prompt.lower() or "succeed" in prompt.lower()
    assert "rules" in prompt.lower()
    assert "memory" in prompt.lower()
    assert "follow-up" in prompt.lower()


def test_build_reflection_prompt_light():
    engine = _make_engine()
    prompt = engine.build_reflection_prompt(
        depth="light",
        trigger="passive.observation",
        action_summary="Observed chat about API design",
        action_results=[],
    )
    # Light prompt is shorter, no rule scan
    assert "memory" in prompt.lower()
    assert len(prompt) < 500


def test_max_depth_tracking():
    engine = _make_engine(max_depth=2)
    assert engine.can_reflect_deeper(current_depth=0) is True
    assert engine.can_reflect_deeper(current_depth=1) is True
    assert engine.can_reflect_deeper(current_depth=2) is False


def test_token_tracking():
    engine = _make_engine(per_cycle_cap=100)
    assert engine.can_continue_cycle(tokens_used=50) is True
    assert engine.can_continue_cycle(tokens_used=100) is False
    assert engine.can_continue_cycle(tokens_used=150) is False


def test_circuit_breaker_not_tripped():
    engine = _make_engine(hourly_cap=1000)
    engine.record_tokens(500)
    assert engine.is_circuit_breaker_tripped() is False


def test_circuit_breaker_tripped():
    engine = _make_engine(hourly_cap=1000)
    engine.record_tokens(1001)
    assert engine.is_circuit_breaker_tripped() is True


def test_circuit_breaker_resets_after_hour():
    import time
    engine = _make_engine(hourly_cap=1000)
    # Manually set old timestamp
    engine._token_ledger.append((time.time() - 3700, 999))
    assert engine.is_circuit_breaker_tripped() is False


def test_record_tokens_accumulates():
    engine = _make_engine()
    engine.record_tokens(100)
    engine.record_tokens(200)
    assert engine.hourly_tokens_used() == 300
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_reflection.py -v`
Expected: FAIL — `src.reflection` doesn't exist

- [ ] **Step 3: Implement ReflectionEngine**

```python
# src/reflection.py
"""Reflection engine for the Supervisor's action-reflect cycle.

Every action the Supervisor takes gets a verification pass. The
reflection depth varies by trigger type and configured level.

Safety controls prevent runaway loops:
- Maximum reflection depth (default 3)
- Per-cycle token cap
- Hourly circuit breaker
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from src.config import ReflectionConfig


# Trigger → default reflection depth mapping
_DEEP_TRIGGERS = {"task.completed", "task.failed", "hook.failed"}
_STANDARD_TRIGGERS = {"user.request", "hook.completed"}
_LIGHT_TRIGGERS = {"passive.observation", "periodic.sweep"}


class ReflectionEngine:
    """Manages the action-reflect cycle for the Supervisor.

    Determines reflection depth, builds reflection prompts,
    tracks token usage, and enforces safety limits.
    """

    def __init__(self, config: ReflectionConfig):
        self._config = config
        self._token_ledger: list[tuple[float, int]] = []  # (timestamp, tokens)

    @property
    def level(self) -> str:
        return self._config.level

    def should_reflect(self, trigger: str) -> bool:
        """Whether reflection should happen at all for this trigger."""
        if self._config.level == "off":
            return False
        if self.is_circuit_breaker_tripped():
            return False
        return True

    def determine_depth(
        self, trigger: str, context: dict
    ) -> str | None:
        """Determine reflection depth for a given trigger.

        Returns "deep", "standard", "light", or None (skip).
        """
        if self._config.level == "off":
            return None

        if self._config.level == "minimal":
            # minimal: only light reflection, skip unless explicit trigger
            return "light"

        if self._config.level == "moderate":
            # moderate: standard for deep triggers, light for others
            if trigger in _DEEP_TRIGGERS:
                return "standard"
            return "light"

        # level == "full"
        if trigger in _DEEP_TRIGGERS:
            return "deep"
        if trigger in _STANDARD_TRIGGERS:
            return "standard"
        return "light"

    def build_reflection_prompt(
        self,
        depth: str,
        trigger: str,
        action_summary: str,
        action_results: list[dict],
    ) -> str:
        """Build the reflection prompt based on depth.

        Deep: Full 5-question verification + rule scan + memory + follow-up.
        Standard: Verify success + check relevant rules.
        Light: Update memory if relevant.
        """
        if depth == "deep":
            return self._build_deep_prompt(trigger, action_summary, action_results)
        if depth == "standard":
            return self._build_standard_prompt(trigger, action_summary, action_results)
        return self._build_light_prompt(trigger, action_summary, action_results)

    def _build_deep_prompt(
        self, trigger: str, summary: str, results: list[dict]
    ) -> str:
        results_text = "\n".join(
            f"- {r.get('tool', 'action')}: {r.get('result', '')}" for r in results
        ) if results else "No tool results."

        return (
            f"## Reflection (trigger: {trigger})\n\n"
            f"**Action taken:** {summary}\n\n"
            f"**Results:**\n{results_text}\n\n"
            "Evaluate this action:\n"
            "1. Did I do what was asked/intended?\n"
            "2. Did the actions succeed? **Verify concretely** — if you created "
            "a task, call `list_tasks` to confirm it exists. If you updated "
            "something, read it back. Do not assume success.\n"
            "3. Are there relevant rules I should evaluate now? Use "
            "`browse_rules` to check.\n"
            "4. Did I learn anything that should update memory?\n"
            "5. Is there follow-up work needed?\n\n"
            "If follow-up is needed, take action. Otherwise, confirm completion."
        )

    def _build_standard_prompt(
        self, trigger: str, summary: str, results: list[dict]
    ) -> str:
        results_text = "\n".join(
            f"- {r.get('tool', 'action')}: {r.get('result', '')}" for r in results
        ) if results else "No tool results."

        return (
            f"## Reflection (trigger: {trigger})\n\n"
            f"**Action taken:** {summary}\n\n"
            f"**Results:**\n{results_text}\n\n"
            "Quick check:\n"
            "1. Did the action succeed?\n"
            "2. Any directly relevant rules to check?\n"
        )

    def _build_light_prompt(
        self, trigger: str, summary: str, results: list[dict]
    ) -> str:
        return (
            f"## Reflection (trigger: {trigger})\n\n"
            f"**Summary:** {summary}\n\n"
            "Update memory if this is relevant. Skip if not notable."
        )

    def can_reflect_deeper(self, current_depth: int) -> bool:
        """Whether another nested reflection iteration is allowed."""
        return current_depth < self._config.max_depth

    def can_continue_cycle(self, tokens_used: int) -> bool:
        """Whether the current cycle can continue given tokens consumed."""
        return tokens_used < self._config.per_cycle_token_cap

    def record_tokens(self, tokens: int) -> None:
        """Record tokens used for circuit breaker tracking."""
        self._token_ledger.append((time.time(), tokens))

    def hourly_tokens_used(self) -> int:
        """Total tokens used in the last hour."""
        cutoff = time.time() - 3600
        return sum(t for ts, t in self._token_ledger if ts > cutoff)

    def is_circuit_breaker_tripped(self) -> bool:
        """Whether the hourly token limit has been exceeded."""
        return self.hourly_tokens_used() >= self._config.hourly_token_circuit_breaker
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_reflection.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/reflection.py tests/test_reflection.py
git commit -m "Add ReflectionEngine with depth determination and safety controls"
```

---

### Task 3: Rename ChatAgent → Supervisor

**Files:**
- Create: `src/supervisor.py` (move content from `src/chat_agent.py`)
- Modify: `src/chat_agent.py` (backward-compat shim)
- Create: `tests/test_supervisor.py`

This task is a mechanical rename. The class moves to `src/supervisor.py`, and `src/chat_agent.py` becomes a thin re-export shim so existing imports don't break.

- [ ] **Step 1: Write tests that verify Supervisor exists and works**

```python
# tests/test_supervisor.py
"""Tests for Supervisor — the single intelligent entity."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_supervisor():
    """Build a Supervisor with mocked dependencies."""
    from src.supervisor import Supervisor

    orch = MagicMock()
    orch.config = MagicMock()
    orch.llm_logger = MagicMock()
    orch.llm_logger._enabled = False
    config = MagicMock()
    config.workspace_dir = "/tmp/test"
    config.chat_provider = MagicMock()
    config.supervisor = MagicMock()
    config.supervisor.reflection = MagicMock()
    config.supervisor.reflection.level = "full"
    config.supervisor.reflection.max_depth = 3
    config.supervisor.reflection.per_cycle_token_cap = 10000
    config.supervisor.reflection.hourly_token_circuit_breaker = 100000
    config.supervisor.reflection.periodic_interval = 900

    return Supervisor(orch, config)


def test_supervisor_class_exists():
    from src.supervisor import Supervisor
    assert Supervisor is not None


def test_supervisor_inherits_chat_behavior():
    sup = _make_supervisor()
    assert hasattr(sup, "chat")
    assert hasattr(sup, "initialize")
    assert hasattr(sup, "summarize")
    assert hasattr(sup, "handler")


def test_supervisor_has_reflection_engine():
    sup = _make_supervisor()
    assert hasattr(sup, "reflection")
    assert sup.reflection is not None


def test_backward_compat_import():
    """ChatAgent import still works via shim."""
    from src.chat_agent import ChatAgent
    assert ChatAgent is not None


def test_backward_compat_is_supervisor():
    from src.chat_agent import ChatAgent
    from src.supervisor import Supervisor
    assert ChatAgent is Supervisor


def test_set_active_project():
    sup = _make_supervisor()
    sup.set_active_project("my-project")
    assert sup._active_project_id == "my-project"


def test_build_system_prompt_returns_string():
    sup = _make_supervisor()
    prompt = sup._build_system_prompt()
    assert isinstance(prompt, str)
    assert len(prompt) > 0


def test_process_hook_llm_exists():
    """Supervisor exposes process_hook_llm for hook engine integration."""
    sup = _make_supervisor()
    assert hasattr(sup, "process_hook_llm")
    assert callable(sup.process_hook_llm)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_supervisor.py -v`
Expected: FAIL — `src.supervisor` doesn't exist

- [ ] **Step 3: Create `src/supervisor.py` by moving ChatAgent content**

Create `src/supervisor.py` with the full content of `src/chat_agent.py`, but rename the class:

```python
# src/supervisor.py
"""Supervisor — the single intelligent entity in the agent-queue system.

The Supervisor is the evolved ChatAgent. All LLM reasoning flows through it.
It handles three activation modes:
1. Direct Address — user @mentions, full authority to act
2. Passive Observation — absorbs chat, suggests but doesn't act (Phase 5)
3. Reflection — periodic/event-driven self-verification (action-reflect cycle)

The ``chat()`` method is the main entry point for direct address. It sends the
user message (plus history) to the LLM, executes tool-use blocks via
CommandHandler, and includes a reflection pass after actions complete.

Tool definitions live in ``tool_registry.py``. The Supervisor starts each
interaction with core tools and expands on demand via ``load_tools``.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from src.chat_providers import ChatProvider, LoggedChatProvider, create_chat_provider
from src.command_handler import CommandHandler
from src.config import AppConfig
from src.llm_logger import LLMLogger
from src.orchestrator import Orchestrator
from src.reflection import ReflectionEngine
from src.tool_registry import ToolRegistry as _ToolRegistry

# Backward-compat alias
TOOLS = _ToolRegistry().get_all_tools()

SYSTEM_PROMPT_TEMPLATE = """You are AgentQueue, a Discord bot that manages an AI agent task queue.
Workspaces root: {workspace_dir}
Use browse_tools/load_tools to discover and load tool categories on demand."""


def _tool_label(name: str, input_data: dict) -> str:
    """Return a short descriptive label for a tool call."""
    detail: str | None = None

    if name == "run_command":
        detail = input_data.get("command")
    elif name == "search_files":
        mode = input_data.get("mode", "grep")
        pattern = input_data.get("pattern", "")
        detail = f"{mode}: {pattern}" if pattern else mode
    elif name == "create_task":
        detail = input_data.get("title")
    elif name == "update_task":
        detail = input_data.get("task_id")
    elif name == "git_log":
        detail = input_data.get("project_id")
    elif name == "git_diff":
        detail = input_data.get("project_id")
    elif name == "git_status":
        detail = input_data.get("project_id")
    elif name == "git_commit":
        detail = input_data.get("message")
    elif name == "git_push":
        detail = input_data.get("branch")
    elif name == "git_pull":
        detail = input_data.get("branch")
    elif name == "git_checkout":
        detail = input_data.get("branch")
    elif name == "read_file":
        detail = input_data.get("path")
    elif name == "write_file":
        detail = input_data.get("path")
    elif name == "list_tasks":
        detail = input_data.get("status")
    elif name == "assign_task":
        detail = input_data.get("task_id")

    if detail:
        if len(detail) > 60:
            detail = detail[:57] + "..."
        return f"{name}({detail})"
    return name


class Supervisor:
    """The single intelligent entity in the agent-queue system.

    Owns the tool definitions, system prompt, LLM client, multi-turn
    tool-use loop, and reflection engine. Callers (Discord bot, CLI,
    hook engine) are responsible for building message history and
    routing responses.

    Replaces the former ChatAgent class.
    """

    def __init__(self, orchestrator: Orchestrator, config: AppConfig,
                 llm_logger: LLMLogger | None = None):
        self.orchestrator = orchestrator
        self.config = config
        self._provider: ChatProvider | None = None
        self._llm_logger = llm_logger
        self.handler = CommandHandler(orchestrator, config)
        self.reflection = ReflectionEngine(config.supervisor.reflection)

    def initialize(self) -> bool:
        """Create LLM provider. Returns True if provider is ready."""
        provider = create_chat_provider(self.config.chat_provider)
        if provider and self._llm_logger and self._llm_logger._enabled:
            provider = LoggedChatProvider(
                provider, self._llm_logger, caller="supervisor.chat"
            )
        self._provider = provider
        return self._provider is not None

    @property
    def is_ready(self) -> bool:
        return self._provider is not None

    async def is_model_loaded(self) -> bool:
        """Check if the LLM model is loaded and ready."""
        if not self._provider:
            return True
        return await self._provider.is_model_loaded()

    @property
    def model(self) -> str | None:
        return self._provider.model_name if self._provider else None

    def set_active_project(self, project_id: str | None) -> None:
        self.handler.set_active_project(project_id)

    @property
    def _active_project_id(self) -> str | None:
        return self.handler._active_project_id

    def reload_credentials(self) -> bool:
        """Re-create the LLM provider. Returns True on success."""
        return self.initialize()

    def _build_system_prompt(self) -> str:
        from src.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        builder.set_identity(
            "chat-agent-system",
            {"workspace_dir": self.config.workspace_dir},
        )
        if self._active_project_id:
            builder.add_context(
                "active_project",
                f"ACTIVE PROJECT: `{self._active_project_id}`. "
                f"Use this as the default project_id for all tools unless the user "
                f"explicitly specifies a different project. When creating tasks, "
                f"listing notes, or any project-scoped operation, use this project.",
            )
        system_prompt, _ = builder.build()
        return system_prompt

    async def chat(
        self,
        text: str,
        user_name: str,
        history: list[dict] | None = None,
        on_progress: "Callable[[str, str | None], Awaitable[None]] | None" = None,
    ) -> str:
        """Process a user message with tool use. Returns response text.

        Starts with core tools only. When the LLM calls ``load_tools``,
        the requested category's tool definitions are added to the active
        set for subsequent turns within this interaction.
        """
        if not self._provider:
            raise RuntimeError("LLM provider not initialized — call initialize() first")

        registry = _ToolRegistry()

        # Mutable tool set — starts with core, expands via load_tools
        active_tools: dict[str, dict] = {
            t["name"]: t for t in registry.get_core_tools()
        }

        messages = list(history) if history else []

        # Append current message
        current = {"role": "user", "content": f"[from {user_name}]: {text}"}
        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"] += "\n" + current["content"]
        else:
            messages.append(current)

        # Multi-turn tool-use loop
        tool_actions: list[str] = []
        max_rounds = getattr(self, "_max_tool_rounds", 10)

        for round_num in range(max_rounds):
            if on_progress:
                if round_num == 0:
                    await on_progress("thinking", None)
                else:
                    await on_progress("thinking", f"round {round_num + 1}")

            resp = await self._provider.create_message(
                messages=messages,
                system=self._build_system_prompt(),
                tools=list(active_tools.values()),
                max_tokens=1024,
            )

            if not resp.tool_uses:
                if on_progress:
                    await on_progress("responding", None)
                response = "\n".join(resp.text_parts).strip()
                if response:
                    return response
                if tool_actions:
                    return f"Done. Actions taken: {', '.join(tool_actions)}"
                return "Done."

            # Only keep tool_use blocks in assistant message
            messages.append({"role": "assistant", "content": resp.tool_uses})

            tool_results = []
            for tool_use in resp.tool_uses:
                label = _tool_label(tool_use.name, tool_use.input)
                if on_progress:
                    await on_progress("tool_use", label)
                result = await self._execute_tool(tool_use.name, tool_use.input)
                tool_actions.append(label)

                # If load_tools was called, expand active tool set
                if tool_use.name == "load_tools" and "loaded" in result:
                    category = result["loaded"]
                    cat_tools = registry.get_category_tools(category)
                    if cat_tools:
                        for t in cat_tools:
                            active_tools[t["name"]] = t

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": json.dumps(result),
                })

            messages.append({"role": "user", "content": tool_results})

        if tool_actions:
            return f"Done. Actions taken: {', '.join(tool_actions)}"
        return "Done."

    async def process_hook_llm(
        self,
        hook_context: str,
        rendered_prompt: str,
        project_id: str | None = None,
        hook_name: str = "unknown",
        on_progress: "Callable[[str, str | None], Awaitable[None]] | None" = None,
    ) -> str:
        """Process a hook's LLM invocation through the Supervisor.

        Called by the hook engine instead of creating a fresh ChatAgent.
        The Supervisor processes the hook prompt through its normal
        action-reflect cycle and returns the response.

        Returns the response text.
        """
        if project_id:
            self.set_active_project(project_id)

        full_prompt = hook_context + rendered_prompt
        return await self.chat(
            text=full_prompt,
            user_name=f"hook:{hook_name}",
            on_progress=on_progress,
        )

    async def summarize(self, transcript: str) -> str | None:
        """Summarize a conversation transcript. Returns None on failure."""
        if not self._provider:
            return None
        prev_caller = None
        if isinstance(self._provider, LoggedChatProvider):
            prev_caller = self._provider._caller
            self._provider._caller = "supervisor.summarize"
        try:
            resp = await self._provider.create_message(
                messages=[{
                    "role": "user",
                    "content": (
                        "Summarize this Discord conversation concisely. "
                        "Preserve key details: project names, task IDs, repo names, "
                        "decisions made, and any pending questions or requests. "
                        "Keep it factual and brief.\n\n"
                        f"{transcript}"
                    ),
                }],
                system="You are a helpful assistant that summarizes conversations.",
                max_tokens=512,
            )
            parts = resp.text_parts
            return parts[0] if parts else None
        except Exception as e:
            print(f"Summary generation failed: {e}")
            return None
        finally:
            if prev_caller is not None and isinstance(self._provider, LoggedChatProvider):
                self._provider._caller = prev_caller

    async def _execute_tool(self, name: str, input_data: dict) -> dict:
        """Execute a tool call via the shared CommandHandler."""
        if name == "list_tasks" and input_data.get("show_all"):
            input_data = {**input_data, "include_completed": True}
            input_data.pop("show_all", None)
        return await self.handler.execute(name, input_data)
```

- [ ] **Step 4: Replace `src/chat_agent.py` with backward-compat shim**

```python
# src/chat_agent.py
"""Backward-compatibility shim — ChatAgent is now Supervisor.

All functionality has moved to ``src/supervisor.py``. This module
re-exports Supervisor as ChatAgent so existing imports continue to work.
"""
from src.supervisor import (  # noqa: F401
    Supervisor as ChatAgent,
    TOOLS,
    SYSTEM_PROMPT_TEMPLATE,
    _tool_label,
)

__all__ = ["ChatAgent", "TOOLS", "SYSTEM_PROMPT_TEMPLATE"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_supervisor.py -v`
Expected: PASS

- [ ] **Step 6: Run all existing tests for regression**

Run: `pytest tests/test_prompt_builder.py tests/test_rule_manager.py tests/test_tool_registry.py -v`
Expected: PASS (backward-compat shim ensures nothing breaks)

- [ ] **Step 7: Commit**

```bash
git add src/supervisor.py src/chat_agent.py tests/test_supervisor.py
git commit -m "Rename ChatAgent to Supervisor with backward-compat shim"
```

---

### Task 4: Update All Import Sites and References

**Files:**
- Modify: `src/discord/bot.py`
- Modify: `src/hooks.py`
- Modify: `src/command_handler.py`
- Modify: `src/discord/commands.py`
- Modify: `src/chat_providers/logged.py`
- Modify: `src/prompt_builder.py`

Update all direct `ChatAgent` references to use `Supervisor`. The backward-compat shim means nothing will break, but updating imports is cleaner.

- [ ] **Step 1: Update `src/discord/bot.py`**

Change the import at the top of the file:

```python
# OLD:
from src.chat_agent import ChatAgent
# NEW:
from src.supervisor import Supervisor
```

Change the usage in `__init__`:

```python
# OLD:
self.agent = ChatAgent(orchestrator, config, llm_logger=orchestrator.llm_logger)
# NEW:
self.agent = Supervisor(orchestrator, config, llm_logger=orchestrator.llm_logger)
```

- [ ] **Step 2: Update `src/hooks.py` docstring references**

In the module docstring (lines 39-46), update references from `ChatAgent` to `Supervisor`:

```python
# OLD: "Each hook invocation creates a fresh ChatAgent instance"
# NEW: "Hook LLM invocations go through the Supervisor's process_hook_llm() method"
```

Note: The actual `_invoke_llm` method is updated in Task 5 (hook engine integration). This task only updates docstrings and module-level comments.

- [ ] **Step 3: Update `src/command_handler.py` docstring references**

Update the module docstring and any comments that say "ChatAgent" to say "Supervisor":

```python
# Line 9: "two callers -- Discord slash commands and ChatAgent LLM tool-use"
# → "two callers -- Discord slash commands and Supervisor LLM tool-use"
```

- [ ] **Step 4: Update `src/discord/commands.py`**

Line 1483 reference: update "ChatAgent" → "Supervisor" in comment.

- [ ] **Step 5: Update `src/chat_providers/logged.py`**

Update the default caller string in the module docstring:

```python
# OLD: caller="chat_agent.chat"
# NEW: caller="supervisor.chat"
```

Also update the docstring example that references `chat_agent.chat`.

- [ ] **Step 6: Update `src/prompt_builder.py`**

Line 4 comment: update "chat_agent" → "supervisor":

```python
# OLD: "concatenation across orchestrator, adapters, chat_agent, and hooks."
# NEW: "concatenation across orchestrator, adapters, supervisor, and hooks."
```

- [ ] **Step 7: Run all tests for regression**

Run: `pytest tests/test_prompt_builder.py tests/test_rule_manager.py tests/test_tool_registry.py tests/test_supervisor.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/discord/bot.py src/hooks.py src/command_handler.py src/discord/commands.py src/chat_providers/logged.py src/prompt_builder.py
git commit -m "Update all ChatAgent references to Supervisor across codebase"
```

---

### Task 5: Wire Hook Engine to Use Supervisor

**Files:**
- Modify: `src/hooks.py:1355-1455` (the `_invoke_llm` method)
- Modify: `tests/test_supervisor.py` (add hook integration test)

The hook engine currently creates a fresh `ChatAgent` in `_invoke_llm()`. Replace this with a callback to the Supervisor's `process_hook_llm()` method. The Supervisor instance is passed to the hook engine at initialization.

- [ ] **Step 1: Write failing test**

Add to `tests/test_supervisor.py`:

```python
def test_process_hook_llm_sets_project():
    """process_hook_llm sets active project before processing."""
    sup = _make_supervisor()
    sup._provider = MagicMock()

    # Mock the provider response (no tool use, just text)
    mock_resp = MagicMock()
    mock_resp.tool_uses = []
    mock_resp.text_parts = ["Hook processed successfully"]
    sup._provider.create_message = AsyncMock(return_value=mock_resp)

    result = asyncio.get_event_loop().run_until_complete(
        sup.process_hook_llm(
            hook_context="## Hook Context\nProject: test",
            rendered_prompt="Check tunnel status",
            project_id="my-game",
            hook_name="tunnel-monitor",
        )
    )
    assert "Hook processed" in result
    assert sup._active_project_id == "my-game"
```

- [ ] **Step 2: Run to verify it passes (should pass — implemented in Task 3)**

Run: `pytest tests/test_supervisor.py::test_process_hook_llm_sets_project -v`
Expected: PASS

- [ ] **Step 3: Add supervisor reference to HookEngine**

In `src/hooks.py`, add a `set_supervisor()` method and update `_invoke_llm()`:

Add a `_supervisor` attribute in the `HookEngine.__init__`:

```python
self._supervisor = None  # Set via set_supervisor()
```

Add a setter method:

```python
def set_supervisor(self, supervisor) -> None:
    """Set the Supervisor instance for LLM invocations."""
    self._supervisor = supervisor
```

Replace the body of `_invoke_llm()` (lines 1399-1454):

```python
async def _invoke_llm(
    self, hook: Hook, prompt: str,
    trigger_reason: str = "unknown",
    on_progress=None,
    event_data: dict | None = None,
) -> tuple[str, int]:
    """Invoke the LLM through the Supervisor.

    Uses the Supervisor's process_hook_llm() method instead of creating
    a fresh ChatAgent instance. This maintains the "single intelligent
    entity" guarantee.

    If no Supervisor is set (backward compat), falls back to creating
    a ChatAgent directly.
    """
    # Build dynamic context preamble
    context_preamble = await self._build_hook_context(
        hook, trigger_reason, event_data=event_data,
    )

    if self._supervisor:
        # Handle per-hook LLM config overrides
        original_provider = None
        if hook.llm_config:
            llm_cfg = json.loads(hook.llm_config)
            provider_config = ChatProviderConfig(
                provider=llm_cfg.get("provider", self.config.chat_provider.provider),
                model=llm_cfg.get("model", self.config.chat_provider.model),
                base_url=llm_cfg.get("base_url", self.config.chat_provider.base_url),
            )
            original_provider = self._supervisor._provider
            hook_provider = create_chat_provider(provider_config)
            if hook_provider:
                orchestrator = self._orchestrator
                if hasattr(orchestrator, 'llm_logger') and orchestrator.llm_logger._enabled:
                    hook_provider = LoggedChatProvider(
                        hook_provider, orchestrator.llm_logger, caller="hook_engine"
                    )
                self._supervisor._provider = hook_provider

        try:
            response = await self._supervisor.process_hook_llm(
                hook_context=context_preamble,
                rendered_prompt=prompt,
                project_id=hook.project_id,
                hook_name=hook.name,
                on_progress=on_progress,
            )
        finally:
            # Restore original provider if we swapped it
            if original_provider is not None:
                self._supervisor._provider = original_provider

        tokens = len(context_preamble + prompt) // 4 + len(response) // 4
        return response, tokens

    # Fallback: create ChatAgent directly (backward compat)
    from src.supervisor import Supervisor

    # Determine provider config
    if hook.llm_config:
        llm_cfg = json.loads(hook.llm_config)
        provider_config = ChatProviderConfig(
            provider=llm_cfg.get("provider", self.config.chat_provider.provider),
            model=llm_cfg.get("model", self.config.chat_provider.model),
            base_url=llm_cfg.get("base_url", self.config.chat_provider.base_url),
        )
    else:
        provider_config = self.config.chat_provider

    orchestrator = self._orchestrator
    supervisor = Supervisor(orchestrator, self.config)
    supervisor.set_active_project(hook.project_id)

    provider = create_chat_provider(provider_config)
    if provider and hasattr(orchestrator, 'llm_logger') and orchestrator.llm_logger._enabled:
        provider = LoggedChatProvider(
            provider, orchestrator.llm_logger, caller="hook_engine"
        )
    supervisor._provider = provider

    if not supervisor._provider:
        raise RuntimeError(
            f"Failed to create LLM provider: {provider_config.provider}"
        )

    full_prompt = context_preamble + prompt
    response = await supervisor.chat(
        text=full_prompt,
        user_name="hook:" + hook.name,
        on_progress=on_progress,
    )
    tokens = len(full_prompt) // 4 + len(response) // 4
    return response, tokens
```

- [ ] **Step 4: Wire Supervisor into HookEngine from Discord bot**

In `src/discord/bot.py`, after creating the Supervisor, pass it to the hook engine. Find the `on_ready` or initialization section and add:

```python
# After self.agent is created:
if hasattr(self.orchestrator, 'hooks') and self.orchestrator.hooks:
    self.orchestrator.hooks.set_supervisor(self.agent)
```

- [ ] **Step 5: Run all tests**

Run: `pytest tests/test_supervisor.py tests/test_reflection.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/hooks.py src/discord/bot.py tests/test_supervisor.py
git commit -m "Wire hook engine to use Supervisor instead of creating fresh ChatAgent"
```

---

### Task 6: Add Action-Reflect Cycle to chat()

**Files:**
- Modify: `src/supervisor.py` (the `chat()` method)
- Modify: `tests/test_supervisor.py`

Add the reflection pass after the main tool-use loop completes. The Supervisor evaluates its actions and may take follow-up actions.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_supervisor.py`:

```python
def test_chat_triggers_reflection_on_tool_use():
    """After tool use, the Supervisor should attempt reflection."""
    sup = _make_supervisor()
    sup._provider = MagicMock()

    # First call: LLM uses a tool
    tool_use = MagicMock()
    tool_use.name = "create_task"
    tool_use.input = {"title": "Fix login"}
    tool_use.id = "tu-1"

    resp_with_tools = MagicMock()
    resp_with_tools.tool_uses = [tool_use]
    resp_with_tools.text_parts = []

    # Second call: LLM responds with text (after tool result)
    resp_text = MagicMock()
    resp_text.tool_uses = []
    resp_text.text_parts = ["Task created."]

    # Third call (reflection): LLM responds with text
    resp_reflect = MagicMock()
    resp_reflect.tool_uses = []
    resp_reflect.text_parts = ["Reflection: task verified."]

    sup._provider.create_message = AsyncMock(
        side_effect=[resp_with_tools, resp_text, resp_reflect]
    )

    # Mock tool execution
    sup.handler.execute = AsyncMock(return_value={"id": "t-123", "title": "Fix login"})

    result = asyncio.get_event_loop().run_until_complete(
        sup.chat("Create a task to fix login", "testuser")
    )
    assert "Task created" in result


def test_chat_skips_reflection_when_off():
    """When reflection level is off, no reflection pass happens."""
    sup = _make_supervisor()
    sup.reflection._config.level = "off"
    sup._provider = MagicMock()

    resp = MagicMock()
    resp.tool_uses = []
    resp.text_parts = ["Done."]
    sup._provider.create_message = AsyncMock(return_value=resp)

    result = asyncio.get_event_loop().run_until_complete(
        sup.chat("Hello", "testuser")
    )
    # Only 1 LLM call (no reflection)
    assert sup._provider.create_message.call_count == 1


def test_chat_no_reflection_for_simple_text():
    """No tool use = no reflection needed."""
    sup = _make_supervisor()
    sup._provider = MagicMock()

    resp = MagicMock()
    resp.tool_uses = []
    resp.text_parts = ["Hi there!"]
    sup._provider.create_message = AsyncMock(return_value=resp)

    result = asyncio.get_event_loop().run_until_complete(
        sup.chat("Hello", "testuser")
    )
    assert result == "Hi there!"
    assert sup._provider.create_message.call_count == 1
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_supervisor.py::test_chat_triggers_reflection_on_tool_use -v`
Expected: May pass or fail depending on current implementation. The test verifies reflection is attempted.

- [ ] **Step 3: Add reflection to `chat()` method**

Modify `Supervisor.chat()` in `src/supervisor.py`. After the main tool-use loop, if tools were used and reflection is enabled, do a reflection pass:

After the main for loop, before the final return, add:

```python
# --- Reflection pass ---
if tool_actions and self.reflection.should_reflect("user.request"):
    depth = self.reflection.determine_depth("user.request", {})
    if depth:
        reflection_prompt = self.reflection.build_reflection_prompt(
            depth=depth,
            trigger="user.request",
            action_summary=", ".join(tool_actions),
            action_results=[],  # Could be enriched later
        )
        # Add reflection as a follow-up message
        messages.append({
            "role": "user",
            "content": f"[system reflection]: {reflection_prompt}",
        })

        try:
            reflect_resp = await self._provider.create_message(
                messages=messages,
                system=self._build_system_prompt(),
                tools=list(active_tools.values()),
                max_tokens=512,
            )
            # Process any reflection-triggered tool calls (depth-limited)
            if reflect_resp.tool_uses and self.reflection.can_reflect_deeper(1):
                # Handle one round of reflection tool use
                messages.append({"role": "assistant", "content": reflect_resp.tool_uses})
                for tool_use in reflect_resp.tool_uses:
                    result = await self._execute_tool(tool_use.name, tool_use.input)
                    messages.append({"role": "user", "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": json.dumps(result),
                    }]})
            # Track token usage
            estimated_tokens = len(reflection_prompt) // 4
            self.reflection.record_tokens(estimated_tokens)
        except Exception:
            pass  # Reflection failure should never break the main flow
```

This goes right after the for loop completes and before the final "Done" return.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_supervisor.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/supervisor.py tests/test_supervisor.py
git commit -m "Add action-reflect cycle to Supervisor.chat()"
```

---

### Task 7: Create Supervisor System Prompt Template

**Files:**
- Create: `src/prompts/supervisor_system.md`
- Modify: `src/supervisor.py` (update `_build_system_prompt` to use new template)
- Modify: `tests/test_supervisor.py`

The spec (Section 1) defines three activation modes for the Supervisor. The current system prompt still uses the `chat-agent-system` template. Create a proper Supervisor identity template.

- [ ] **Step 1: Write test that the supervisor template exists and renders**

Add to `tests/test_supervisor.py`:

```python
def test_supervisor_prompt_template_exists():
    """Supervisor identity template renders with workspace_dir."""
    from src.prompt_builder import PromptBuilder
    import os

    prompts_dir = os.path.join(os.path.dirname(__file__), "..", "src", "prompts")
    builder = PromptBuilder(prompts_dir=prompts_dir)
    result = builder.render_template("supervisor-system", {"workspace_dir": "/tmp/test"})
    assert result is not None
    assert "/tmp/test" in result
    assert "supervisor" in result.lower() or "single intelligent entity" in result.lower()
```

- [ ] **Step 2: Create `src/prompts/supervisor_system.md`**

```markdown
---
name: supervisor-system
description: System prompt for the Supervisor — the single intelligent entity
category: system
variables:
  - name: workspace_dir
    description: Root directory for project workspaces
    required: true
tags: [system, supervisor]
version: 1
---

You are the Supervisor — the single intelligent entity managing the agent-queue system. All LLM reasoning flows through you. You manage projects, tasks, agents, and rules through natural conversation and autonomous reflection.

System info:
- Workspaces root: {{workspace_dir}}
- Each project gets its own folder under the workspaces root.

## Three Activation Modes

**1. Direct Address** — A user @mentions you or messages you directly. You reason and act autonomously: create tasks, modify rules, manage hooks, run commands. Full authority.

**2. Passive Observation** — Users discuss the project without addressing you. You absorb information into memory and may propose suggestions, but do NOT take autonomous action on the project. You CAN update your own memory and understanding.

**3. Reflection** — Periodic and event-driven self-verification. You review task completions, hook executions, and system state. This can trigger autonomous actions because you are executing rules the user previously authorized.

## Tool Navigation

You start with a small set of core tools. Additional tools are organized into categories that you can load on demand:

1. Call `browse_tools` to see available categories (git, project, agent, hooks, memory, system)
2. Call `load_tools(category="...")` to load a category's tools for this interaction
3. The loaded tools become available immediately for subsequent calls

Core tools (always available):
- `create_task` / `list_tasks` / `edit_task` / `get_task` — task CRUD
- `browse_tools` / `load_tools` — discover and load tool categories
- `memory_search` — search project memory
- `send_message` — post to Discord channels
- `browse_rules` / `load_rule` / `save_rule` / `delete_rule` — rule management

## Role and Boundaries

You are a **dispatcher**, not a worker. You CANNOT write code, edit files, run commands, or do technical work yourself. When a user asks you to DO something technical, create a task for an agent. When a user asks for a management action, use your tools directly.

## Self-Verification

After taking actions, verify your work concretely:
- Created a task? Call `list_tasks` to confirm it exists with correct fields.
- Updated a rule? Call `load_rule` to read it back.
- Sent a message? Check the result for confirmation.

Do not assume actions succeeded — verify them.

## Task Creation Guidelines

- Task descriptions MUST be completely self-contained. The agent has NO access to this chat.
- Include ALL context: file paths, repo URLs, requirements, error messages.
- Always include the workspace path so the agent knows where to work.

## Presentation Guidelines

- Be concise in Discord messages. Use markdown formatting.
- After management actions, respond with ONE short confirmation line.

## Action Word Mappings

- "cancel", "kill", "abort" a task → `stop_task`
- "approve", "LGTM", "ship it" → `approve_task`
- "restart", "retry", "rerun" → `restart_task`

When the user asks for a multi-step workflow, call each tool in sequence.
```

- [ ] **Step 3: Update `_build_system_prompt` to use new template**

In `src/supervisor.py`, change the identity template name:

```python
# OLD:
builder.set_identity("chat-agent-system", {"workspace_dir": self.config.workspace_dir})
# NEW:
builder.set_identity("supervisor-system", {"workspace_dir": self.config.workspace_dir})
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_supervisor.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/prompts/supervisor_system.md src/supervisor.py tests/test_supervisor.py
git commit -m "Add Supervisor system prompt template with three activation modes"
```

---

### Task 8: Wire Event-Driven Reflection

**Files:**
- Modify: `src/supervisor.py`
- Modify: `tests/test_supervisor.py`

The spec requires reflection for ALL trigger types, not just `user.request`. Add a `reflect()` method that can be called by different trigger sources, and wire `process_hook_llm()` to reflect with the appropriate trigger.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_supervisor.py`:

```python
def test_reflect_method_exists():
    """Supervisor has a public reflect() method for event-driven reflection."""
    sup = _make_supervisor()
    assert hasattr(sup, "reflect")
    assert callable(sup.reflect)


def test_process_hook_llm_uses_hook_trigger():
    """process_hook_llm triggers reflection with hook-specific trigger."""
    sup = _make_supervisor()
    sup._provider = MagicMock()

    mock_resp = MagicMock()
    mock_resp.tool_uses = []
    mock_resp.text_parts = ["Hook done"]
    sup._provider.create_message = AsyncMock(return_value=mock_resp)

    # Track what trigger the reflection sees
    original_should = sup.reflection.should_reflect
    triggers_seen = []
    def track_should(trigger):
        triggers_seen.append(trigger)
        return False  # Skip actual reflection for test speed
    sup.reflection.should_reflect = track_should

    asyncio.get_event_loop().run_until_complete(
        sup.process_hook_llm(
            hook_context="ctx", rendered_prompt="prompt",
            project_id="p1", hook_name="my-hook",
        )
    )
    # The reflection trigger should be hook-related, not user.request
    assert any("hook" in t for t in triggers_seen)
```

- [ ] **Step 2: Add `reflect()` method and parameterize reflection trigger**

In `src/supervisor.py`, extract the reflection logic from `chat()` into a reusable `reflect()` method:

```python
async def reflect(
    self,
    trigger: str,
    action_summary: str,
    action_results: list[dict],
    messages: list[dict],
    active_tools: dict[str, dict],
) -> None:
    """Run a reflection pass for the given trigger.

    Called after actions complete. Evaluates results, checks rules,
    and may take follow-up actions (depth-limited).
    """
    if not self._provider:
        return
    if not self.reflection.should_reflect(trigger):
        return

    depth = self.reflection.determine_depth(trigger, {})
    if not depth:
        return

    reflection_prompt = self.reflection.build_reflection_prompt(
        depth=depth,
        trigger=trigger,
        action_summary=action_summary,
        action_results=action_results,
    )

    messages.append({
        "role": "user",
        "content": f"[system reflection]: {reflection_prompt}",
    })

    try:
        reflect_resp = await self._provider.create_message(
            messages=messages,
            system=self._build_system_prompt(),
            tools=list(active_tools.values()),
            max_tokens=512,
        )
        if reflect_resp.tool_uses and self.reflection.can_reflect_deeper(1):
            messages.append({"role": "assistant", "content": reflect_resp.tool_uses})
            for tool_use in reflect_resp.tool_uses:
                result = await self._execute_tool(tool_use.name, tool_use.input)
                messages.append({"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": json.dumps(result),
                }]})
        estimated_tokens = len(reflection_prompt) // 4
        self.reflection.record_tokens(estimated_tokens)
    except Exception:
        pass  # Reflection failure never breaks the main flow
```

Update `chat()` to call `self.reflect()` instead of inline reflection code.

Update `process_hook_llm()` to pass `trigger="hook.completed"` instead of defaulting to `user.request`.

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_supervisor.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/supervisor.py tests/test_supervisor.py
git commit -m "Add reflect() method and wire event-driven reflection triggers"
```

---

### Task 9: Write Behavioral Specs

**Files:**
- Create: `specs/supervisor.md`
- Create: `specs/reflection.md`

- [ ] **Step 1: Write supervisor spec**

```markdown
# Supervisor Specification

The Supervisor is the single intelligent entity in the agent-queue system.
It replaces the former `ChatAgent` class. All LLM reasoning flows through it.

## Class: Supervisor (src/supervisor.py)

### Constructor
- `Supervisor(orchestrator, config, llm_logger=None)`
- Creates a `CommandHandler` for tool execution
- Creates a `ReflectionEngine` from `config.supervisor.reflection`

### Methods

**initialize() → bool**
Creates the LLM provider. Returns True if ready.

**chat(text, user_name, history=None, on_progress=None) → str**
Main entry point for direct address. Processes user message through
multi-turn tool-use loop. If tools were used, triggers a reflection pass.
Starts with core tools only; expands via `load_tools`.

**process_hook_llm(hook_context, rendered_prompt, project_id, hook_name, on_progress) → str**
Entry point for hook engine LLM invocations. Sets active project,
combines context with prompt, processes through `chat()`.

**summarize(transcript) → str | None**
Summarizes a conversation transcript.

### Backward Compatibility
- `src/chat_agent.py` re-exports `Supervisor` as `ChatAgent`
- All existing imports continue to work
- `TOOLS` and `SYSTEM_PROMPT_TEMPLATE` re-exported for compatibility

### Three Activation Modes
1. **Direct Address** — user @mentions, full authority
2. **Passive Observation** — absorbs chat, suggests only (Phase 5)
3. **Reflection** — periodic/event-driven self-verification

### Invariants
- Only one Supervisor instance per bot (created by AgentQueueBot)
- All LLM reasoning goes through the Supervisor
- Reflection never blocks or breaks the primary response
- Hook engine uses process_hook_llm() instead of creating fresh instances
```

- [ ] **Step 2: Write reflection spec**

```markdown
# Reflection Engine Specification

The ReflectionEngine manages the Supervisor's action-reflect cycle.

## Class: ReflectionEngine (src/reflection.py)

### Constructor
- `ReflectionEngine(config: ReflectionConfig)`

### Depth Determination

| Trigger | full | moderate | minimal |
|---------|------|----------|---------|
| task.completed | deep | standard | light |
| task.failed | deep | standard | light |
| hook.failed | deep | standard | light |
| user.request | standard | light | light |
| hook.completed | standard | light | light |
| passive.observation | light | light | light |
| periodic.sweep | light | light | light |

Level "off" returns None for all triggers.

### Reflection Prompts

**Deep:** 5-question verification (intent, success, rules, memory, follow-up)
**Standard:** 2-question check (success, relevant rules)
**Light:** Memory update only

### Safety Controls

1. **max_depth** (default 3): Maximum nested reflection iterations
2. **per_cycle_token_cap** (default 10000): Per-cycle token limit
3. **hourly_token_circuit_breaker** (default 100000): Auto-downgrades to minimal

### Invariants
- Reflection failure never breaks the primary action
- Token ledger entries older than 1 hour are excluded from circuit breaker
- Circuit breaker tripped → should_reflect() returns False
```

- [ ] **Step 3: Commit**

```bash
git add specs/supervisor.md specs/reflection.md
git commit -m "Add behavioral specs for Supervisor and ReflectionEngine"
```

---

### Task 10: Final Integration Test and Cleanup

**Files:**
- Modify: `tests/test_supervisor.py` (add integration tests)
- Verify all existing tests pass

- [ ] **Step 1: Add integration test**

Add to `tests/test_supervisor.py`:

```python
def test_full_integration_supervisor_replaces_chat_agent():
    """Verify Supervisor can be used everywhere ChatAgent was."""
    from src.supervisor import Supervisor
    from src.chat_agent import ChatAgent

    # They're the same class
    assert Supervisor is ChatAgent

    # Supervisor has all the ChatAgent interface
    sup = _make_supervisor()
    assert hasattr(sup, "chat")
    assert hasattr(sup, "summarize")
    assert hasattr(sup, "initialize")
    assert hasattr(sup, "is_ready")
    assert hasattr(sup, "model")
    assert hasattr(sup, "set_active_project")
    assert hasattr(sup, "reload_credentials")
    assert hasattr(sup, "handler")

    # Plus new capabilities
    assert hasattr(sup, "reflection")
    assert hasattr(sup, "process_hook_llm")


def test_reflection_engine_wired_to_config():
    """ReflectionEngine uses config values from SupervisorConfig."""
    from src.config import AppConfig
    app = AppConfig()
    from src.reflection import ReflectionEngine
    engine = ReflectionEngine(app.supervisor.reflection)
    assert engine.level == "full"
    assert engine._config.max_depth == 3
```

- [ ] **Step 2: Run ALL tests**

Run: `pytest tests/test_config_supervisor.py tests/test_reflection.py tests/test_supervisor.py tests/test_prompt_builder.py tests/test_rule_manager.py tests/test_tool_registry.py -v`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_supervisor.py
git commit -m "Phase 4 complete: Supervisor Identity + Reflection"
```
