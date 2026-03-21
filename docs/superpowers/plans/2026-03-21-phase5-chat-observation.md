# Phase 5: Chat Observation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the ChatAnalyzer background service with integrated chat observation in the Supervisor — deterministic keyword filtering (Stage 1) followed by a Supervisor LLM pass (Stage 2) with conversation batching.

**Architecture:** A new `ChatObserver` class handles Stage 1 (deterministic keyword/pattern filter) and conversation batching. Messages passing Stage 1 are sent to `Supervisor.observe()` (Stage 2) for LLM evaluation. The Supervisor decides to update memory, post a suggestion, or ignore. The existing `AnalyzerSuggestionView` Discord UI is migrated to `src/discord/views.py` as a standalone component. The old `ChatAnalyzer` is NOT removed here — that happens in Phase 6.

**Tech Stack:** Python 3.12+, asyncio, pytest, existing EventBus, MemoryManager, ToolRegistry from earlier phases.

**Dependencies:** Phase 4 (Supervisor Identity + Reflection) — completed.

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `src/chat_observer.py` | Create | Stage 1 keyword filter + conversation batching + Stage 2 dispatch |
| `src/config.py` | Modify | Add `ObservationConfig` to `SupervisorConfig` |
| `src/supervisor.py` | Modify | Add `observe()` method for Stage 2 LLM pass |
| `src/discord/views.py` | Create | Migrated `SuggestionView` (accept/dismiss buttons) |
| `src/discord/bot.py` | Modify | Wire `ChatObserver`, add suggestion posting callback |
| `tests/test_chat_observer.py` | Create | Tests for ChatObserver |
| `tests/test_supervisor.py` | Modify | Tests for `observe()` method |
| `specs/chat-observer.md` | Create | Behavioral spec |

---

### Task 1: Add ObservationConfig to SupervisorConfig

**Files:**
- Modify: `src/config.py`
- Modify: `tests/test_config_supervisor.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_config_supervisor.py`:

```python
def test_observation_config_defaults():
    from src.config import ObservationConfig
    cfg = ObservationConfig()
    assert cfg.enabled is True
    assert cfg.batch_window_seconds == 60
    assert cfg.max_buffer_size == 20
    assert cfg.stage1_keywords == []


def test_observation_config_validation():
    from src.config import ObservationConfig
    cfg = ObservationConfig(batch_window_seconds=0)
    errors = cfg.validate()
    assert any("batch_window_seconds" in str(e) for e in errors)


def test_supervisor_config_has_observation():
    from src.config import SupervisorConfig
    cfg = SupervisorConfig()
    assert hasattr(cfg, "observation")
    assert cfg.observation.enabled is True


def test_observation_config_from_yaml():
    from src.config import SupervisorConfig, ObservationConfig
    cfg = SupervisorConfig(observation=ObservationConfig(
        enabled=False, batch_window_seconds=30, max_buffer_size=10,
        stage1_keywords=["deploy", "hotfix"],
    ))
    assert cfg.observation.enabled is False
    assert cfg.observation.batch_window_seconds == 30
    assert cfg.observation.stage1_keywords == ["deploy", "hotfix"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config_supervisor.py -v`
Expected: FAIL — `ObservationConfig` doesn't exist

- [ ] **Step 3: Implement ObservationConfig**

In `src/config.py`, add after `ReflectionConfig`:

```python
@dataclass
class ObservationConfig:
    """Configuration for the Supervisor's passive chat observation."""
    enabled: bool = True
    batch_window_seconds: int = 60
    max_buffer_size: int = 20
    stage1_keywords: list[str] = field(default_factory=list)

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.batch_window_seconds < 5:
            errors.append(ConfigError(
                "observation", "batch_window_seconds", "must be >= 5"
            ))
        if self.max_buffer_size < 1:
            errors.append(ConfigError(
                "observation", "max_buffer_size", "must be >= 1"
            ))
        return errors
```

Update `SupervisorConfig` to include the observation field:

```python
@dataclass
class SupervisorConfig:
    """Top-level Supervisor configuration."""
    reflection: ReflectionConfig = field(default_factory=ReflectionConfig)
    observation: ObservationConfig = field(default_factory=ObservationConfig)

    def validate(self) -> list[ConfigError]:
        return self.reflection.validate() + self.observation.validate()
```

Update `_load_supervisor()` in the YAML loading section to parse the observation sub-key (follow the same pattern used for reflection — look for `_load_supervisor` or the section where `supervisor:` YAML is parsed into `SupervisorConfig`).

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_config_supervisor.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/config.py tests/test_config_supervisor.py
git commit -m "Add ObservationConfig to SupervisorConfig"
```

---

### Task 2: Create ChatObserver with Stage 1 Keyword Filter

**Files:**
- Create: `src/chat_observer.py`
- Create: `tests/test_chat_observer.py`

The ChatObserver receives `chat.message` events from the EventBus, buffers messages per channel, and runs a deterministic keyword/pattern filter (Stage 1). Messages that don't pass Stage 1 are silently dropped — zero LLM cost.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_chat_observer.py
"""Tests for ChatObserver — Stage 1 keyword filter and batching."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_observer(keywords=None, enabled=True):
    from src.config import ObservationConfig
    from src.chat_observer import ChatObserver
    config = ObservationConfig(
        enabled=enabled,
        batch_window_seconds=60,
        max_buffer_size=20,
        stage1_keywords=keywords or [],
    )
    # project_profiles: dict mapping project_id → set of terms
    return ChatObserver(config, project_profiles={
        "my-game": {"particle", "renderer", "unity", "shader"},
    })


def _make_message(content, project_id="my-game", author="alice", is_bot=False):
    return {
        "channel_id": 12345,
        "project_id": project_id,
        "author": author,
        "content": content,
        "timestamp": 1000.0,
        "is_bot": is_bot,
    }


def test_stage1_passes_project_term():
    obs = _make_observer()
    msg = _make_message("the particle system is broken")
    assert obs.passes_stage1(msg) is True


def test_stage1_passes_action_word():
    obs = _make_observer()
    msg = _make_message("we need to deploy the hotfix today")
    assert obs.passes_stage1(msg) is True


def test_stage1_passes_custom_keyword():
    obs = _make_observer(keywords=["kubernetes"])
    msg = _make_message("kubernetes pod is crashlooping")
    assert obs.passes_stage1(msg) is True


def test_stage1_rejects_trivial():
    obs = _make_observer()
    msg = _make_message("ok")
    assert obs.passes_stage1(msg) is False


def test_stage1_rejects_short_generic():
    obs = _make_observer()
    msg = _make_message("sounds good to me")
    assert obs.passes_stage1(msg) is False


def test_stage1_passes_long_message():
    obs = _make_observer()
    long_msg = "I've been thinking about the architecture and " * 10
    msg = _make_message(long_msg)
    assert obs.passes_stage1(msg) is True


def test_stage1_rejects_bot_messages():
    obs = _make_observer()
    msg = _make_message("the particle system looks good", is_bot=True)
    assert obs.passes_stage1(msg) is False


def test_stage1_disabled():
    obs = _make_observer(enabled=False)
    msg = _make_message("the particle system is broken")
    assert obs.passes_stage1(msg) is False


def test_stage1_unknown_project():
    obs = _make_observer()
    msg = _make_message("something about particles", project_id="unknown-proj")
    # No project profile terms, but action words or length could still trigger
    assert obs.passes_stage1(msg) is False


def test_buffer_message():
    obs = _make_observer()
    msg = _make_message("the particle system crashed")
    obs.buffer_message(msg)
    assert obs.get_buffer_size(12345) == 1


def test_flush_buffer():
    obs = _make_observer()
    obs.buffer_message(_make_message("msg 1"))
    obs.buffer_message(_make_message("msg 2"))
    batch = obs.flush_buffer(12345)
    assert len(batch) == 2
    assert obs.get_buffer_size(12345) == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_chat_observer.py -v`
Expected: FAIL — `src.chat_observer` doesn't exist

- [ ] **Step 3: Implement ChatObserver**

```python
# src/chat_observer.py
"""Chat observer — deterministic Stage 1 filter and message batching.

Receives chat.message events from the EventBus, filters them through
a keyword/pattern check (Stage 1), and buffers passing messages for
batch processing by the Supervisor (Stage 2).

Stage 1 is purely deterministic — zero LLM cost. Only messages that
pass Stage 1 are sent to the Supervisor for evaluation.
"""
from __future__ import annotations

import re
from collections import defaultdict, deque

from src.config import ObservationConfig

# Action words that suggest something noteworthy is being discussed.
_ACTION_WORDS = {
    "deploy", "release", "broken", "bug", "error", "merge",
    "revert", "rollback", "hotfix", "incident", "outage",
    "crash", "fix", "blocked", "stuck", "failing", "failed",
}

# Minimum message length (chars) to pass on length alone.
_LENGTH_THRESHOLD = 200


class ChatObserver:
    """Stage 1 keyword filter and conversation buffer.

    Does NOT call the LLM. Buffers messages that pass the deterministic
    filter for batch dispatch to the Supervisor's observe() method.
    """

    def __init__(
        self,
        config: ObservationConfig,
        project_profiles: dict[str, set[str]] | None = None,
    ):
        self._config = config
        self._profiles = project_profiles or {}
        self._custom_keywords = {k.lower() for k in config.stage1_keywords}
        self._buffers: dict[int, deque[dict]] = defaultdict(
            lambda: deque(maxlen=config.max_buffer_size)
        )

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def set_project_profiles(
        self, profiles: dict[str, set[str]]
    ) -> None:
        """Update project term profiles (called on startup/refresh)."""
        self._profiles = profiles

    def passes_stage1(self, message: dict) -> bool:
        """Deterministic keyword/pattern filter. Returns True if notable."""
        if not self._config.enabled:
            return False
        if message.get("is_bot", False):
            return False

        content = message.get("content", "")
        if not content:
            return False

        content_lower = content.lower()
        words = set(re.findall(r'\w+', content_lower))

        # Check project-specific terms
        project_id = message.get("project_id", "")
        project_terms = self._profiles.get(project_id, set())
        if project_terms & words:
            return True

        # Check action words
        if _ACTION_WORDS & words:
            return True

        # Check custom keywords
        if self._custom_keywords:
            if self._custom_keywords & words:
                return True
            # Also check as substrings for multi-word keywords
            for kw in self._custom_keywords:
                if kw in content_lower:
                    return True

        # Long messages are often substantive
        if len(content) >= _LENGTH_THRESHOLD:
            return True

        return False

    def buffer_message(self, message: dict) -> None:
        """Add a message to the per-channel buffer."""
        channel_id = message.get("channel_id")
        if channel_id is not None:
            self._buffers[channel_id].append(message)

    def get_buffer_size(self, channel_id: int) -> int:
        return len(self._buffers.get(channel_id, []))

    def flush_buffer(self, channel_id: int) -> list[dict]:
        """Return and clear all buffered messages for a channel."""
        buf = self._buffers.pop(channel_id, deque())
        return list(buf)

    def has_pending_channels(self) -> bool:
        """Whether any channels have buffered messages."""
        return any(len(b) > 0 for b in self._buffers.values())

    def pending_channel_ids(self) -> list[int]:
        """Channel IDs with buffered messages."""
        return [ch for ch, b in self._buffers.items() if len(b) > 0]
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_chat_observer.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/chat_observer.py tests/test_chat_observer.py
git commit -m "Add ChatObserver with Stage 1 keyword filter and buffering"
```

---

### Task 3: Add Batch Timer and Event Wiring to ChatObserver

**Files:**
- Modify: `src/chat_observer.py`
- Modify: `tests/test_chat_observer.py`

Add the async batch timer that flushes buffers after the configured quiet window, and the `on_message()` method that integrates with the EventBus.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_chat_observer.py`:

```python
def test_on_message_buffers_passing_message():
    obs = _make_observer()
    msg = _make_message("the particle system crashed again")
    obs.on_message(msg)
    assert obs.get_buffer_size(12345) == 1


def test_on_message_drops_failing_message():
    obs = _make_observer()
    msg = _make_message("ok")
    obs.on_message(msg)
    assert obs.get_buffer_size(12345) == 0


def test_on_message_disabled_drops_all():
    obs = _make_observer(enabled=False)
    msg = _make_message("the particle system is broken")
    obs.on_message(msg)
    assert obs.get_buffer_size(12345) == 0


def test_batch_ready_by_count():
    from src.config import ObservationConfig
    from src.chat_observer import ChatObserver
    config = ObservationConfig(max_buffer_size=3)
    obs = ChatObserver(config, project_profiles={
        "my-game": {"particle"},
    })
    for i in range(3):
        obs.on_message(_make_message(f"particle issue {i}"))
    assert obs.is_batch_ready(12345) is True


def test_batch_not_ready_under_count():
    obs = _make_observer()
    obs.on_message(_make_message("the particle system crashed"))
    assert obs.is_batch_ready(12345) is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_chat_observer.py -v`
Expected: FAIL — `on_message` and `is_batch_ready` don't exist

- [ ] **Step 3: Add on_message and is_batch_ready**

Add to `ChatObserver`:

```python
    def on_message(self, message: dict) -> bool:
        """Process an incoming chat.message event.

        Runs Stage 1 filter. If the message passes, buffers it.
        Returns True if the buffer is now full (batch ready).
        """
        if not self._config.enabled:
            return False
        if self.passes_stage1(message):
            self.buffer_message(message)
            channel_id = message.get("channel_id")
            # Track last message time for quiet-window batching
            if channel_id is not None:
                self._last_message_time[channel_id] = message.get(
                    "timestamp", 0.0
                )
            return self.is_batch_ready(channel_id)
        return False

    def is_batch_ready(self, channel_id: int) -> bool:
        """Whether a channel's buffer has reached max_buffer_size."""
        return self.get_buffer_size(channel_id) >= self._config.max_buffer_size
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_chat_observer.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/chat_observer.py tests/test_chat_observer.py
git commit -m "Add on_message filtering and batch readiness to ChatObserver"
```

---

### Task 4: Add observe() Method to Supervisor

**Files:**
- Modify: `src/supervisor.py`
- Modify: `tests/test_supervisor.py`

The Supervisor's `observe()` method is the Stage 2 LLM pass. It receives a batch of messages and project context, makes a lightweight LLM call, and returns a structured decision: update memory, post a suggestion, or ignore.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_supervisor.py`:

```python
def test_observe_method_exists():
    """Supervisor has an observe() method for passive observation."""
    sup = _make_supervisor()
    assert hasattr(sup, "observe")
    assert callable(sup.observe)


def test_observe_returns_decision():
    """observe() returns a dict with action and content."""
    sup = _make_supervisor()
    sup._provider = MagicMock()

    mock_resp = MagicMock()
    mock_resp.tool_uses = []
    mock_resp.text_parts = ['{"action": "ignore"}']
    sup._provider.create_message = AsyncMock(return_value=mock_resp)

    result = asyncio.get_event_loop().run_until_complete(
        sup.observe(
            messages=[{
                "author": "alice",
                "content": "the particle system needs work",
                "timestamp": 1000.0,
            }],
            project_id="my-game",
        )
    )
    assert isinstance(result, dict)
    assert "action" in result


def test_observe_without_provider_returns_ignore():
    """observe() returns ignore when LLM is not available."""
    sup = _make_supervisor()
    sup._provider = None

    result = asyncio.get_event_loop().run_until_complete(
        sup.observe(messages=[], project_id="test")
    )
    assert result["action"] == "ignore"


def test_observe_handles_llm_error():
    """observe() returns ignore on LLM error (never crashes)."""
    sup = _make_supervisor()
    sup._provider = MagicMock()
    sup._provider.create_message = AsyncMock(side_effect=Exception("LLM down"))

    result = asyncio.get_event_loop().run_until_complete(
        sup.observe(
            messages=[{"author": "bob", "content": "deploy failed", "timestamp": 1.0}],
            project_id="test",
        )
    )
    assert result["action"] == "ignore"
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_supervisor.py::test_observe_method_exists -v`
Expected: FAIL — no `observe` method

- [ ] **Step 3: Implement observe()**

Add to `Supervisor` class in `src/supervisor.py`:

```python
    async def observe(
        self,
        messages: list[dict],
        project_id: str,
    ) -> dict:
        """Stage 2 LLM pass for passive observation.

        Receives a batch of messages that passed the Stage 1 keyword
        filter. Makes a lightweight LLM call to decide:
        - "ignore" — nothing notable
        - "memory" — update project memory with observation
        - "suggest" — post a suggestion to the channel

        Returns a dict with "action" key and optional "content",
        "suggestion_type", "task_title" keys.

        Never raises — returns {"action": "ignore"} on any error.
        """
        if not self._provider or not messages:
            return {"action": "ignore"}

        # Build conversation snippet
        lines = []
        for m in messages:
            author = m.get("author", "unknown")
            content = m.get("content", "")
            lines.append(f"[{author}]: {content}")
        conversation = "\n".join(lines)

        prompt = (
            f"## Passive Observation — Project: {project_id}\n\n"
            f"The following conversation happened in the project channel. "
            f"You are observing passively — do NOT take action on the project.\n\n"
            f"### Conversation\n{conversation}\n\n"
            f"### Instructions\n"
            f"Decide one of:\n"
            f'1. **ignore** — nothing notable. Respond: {{"action": "ignore"}}\n'
            f'2. **memory** — worth remembering (technical decision, requirement, '
            f'architecture discussion, bug report, priority). Respond: '
            f'{{"action": "memory", "content": "what to remember"}}\n'
            f'3. **suggest** — actionable work item you could help with. Respond: '
            f'{{"action": "suggest", "content": "suggestion text", '
            f'"suggestion_type": "task|answer|context|warning", '
            f'"task_title": "optional task title"}}\n\n'
            f"Respond with ONLY the JSON object, no other text."
        )

        try:
            resp = await self._provider.create_message(
                messages=[{"role": "user", "content": prompt}],
                system=(
                    "You are observing a project channel passively. "
                    "Respond with a single JSON object. No other text."
                ),
                max_tokens=256,
            )
            text = "\n".join(resp.text_parts).strip()
            return self._parse_observe_response(text)
        except Exception:
            return {"action": "ignore"}

    def _parse_observe_response(self, text: str) -> dict:
        """Parse the LLM's observation response into a structured dict."""
        import json as _json
        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(
                l for l in lines if not l.startswith("```")
            ).strip()
        try:
            result = _json.loads(text)
            if isinstance(result, dict) and "action" in result:
                if result["action"] in ("ignore", "memory", "suggest"):
                    return result
        except (_json.JSONDecodeError, TypeError):
            pass
        return {"action": "ignore"}
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_supervisor.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/supervisor.py tests/test_supervisor.py
git commit -m "Add observe() method to Supervisor for passive chat observation"
```

---

### Task 5: Migrate SuggestionView to Standalone Component

**Files:**
- Create: `src/discord/views.py`
- Modify: `tests/test_supervisor.py` (or new `tests/test_views.py`)

Migrate the `ChatAnalyzerSuggestionView` from `src/discord/notifications.py` (lines 1472-1667) to a standalone `src/discord/views.py`. The view keeps its existing behavior but is decoupled from the ChatAnalyzer.

- [ ] **Step 1: Write test that the view exists and has buttons**

```python
# tests/test_views.py
"""Tests for standalone Discord views."""

import sys
from unittest.mock import MagicMock

# Stub discord before importing
for mod in ["discord", "discord.ext", "discord.ext.commands",
            "discord.app_commands", "discord.ui"]:
    sys.modules.setdefault(mod, MagicMock())


def test_suggestion_view_exists():
    from src.discord.views import SuggestionView
    assert SuggestionView is not None


def test_suggestion_embed_formatter():
    from src.discord.views import format_suggestion_embed
    embed = format_suggestion_embed(
        suggestion_type="task",
        text="Create a profiling task for the particle system",
        project_id="my-game",
        confidence=0.85,
    )
    assert embed is not None


def test_backward_compat_import():
    """Old import path still works."""
    from src.discord.notifications import ChatAnalyzerSuggestionView
    assert ChatAnalyzerSuggestionView is not None
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_views.py -v`
Expected: FAIL — `src.discord.views` doesn't exist

- [ ] **Step 3: Create `src/discord/views.py`**

Copy `ChatAnalyzerSuggestionView` and `format_analyzer_suggestion_embed` from `src/discord/notifications.py` (lines 1472-1667) into `src/discord/views.py`. Rename:
- `ChatAnalyzerSuggestionView` → `SuggestionView`
- `format_analyzer_suggestion_embed` → `format_suggestion_embed`
- Keep `ChatAnalyzerSuggestionView` as a backward-compat alias

```python
# src/discord/views.py
"""Standalone Discord UI views for the Supervisor.

Contains reusable view components that can be used by both the
ChatObserver (Phase 5) and any other Supervisor UI needs.
"""
from __future__ import annotations

import discord
from discord.ui import View, button, Button


def format_suggestion_embed(
    suggestion_type: str,
    text: str,
    project_id: str,
    confidence: float,
    suggestion_id: int | str | None = None,
) -> discord.Embed:
    """Create a formatted embed for a Supervisor suggestion."""
    type_config = {
        "answer": ("💡", "Answer", 0x2ECC71),
        "task": ("📋", "Task Suggestion", 0x3498DB),
        "context": ("📝", "Context", 0xF39C12),
        "warning": ("⚠️", "Warning", 0xE74C3C),
    }
    emoji, title, color = type_config.get(
        suggestion_type, ("💬", "Suggestion", 0x9B59B6)
    )

    embed = discord.Embed(
        title=f"{emoji} {title}",
        description=text,
        color=color,
    )

    # Confidence bar
    filled = round(confidence * 5)
    bar = "█" * filled + "░" * (5 - filled)
    footer = f"Confidence: {bar} {confidence:.0%}"
    if project_id:
        footer += f" | Project: {project_id}"
    embed.set_footer(text=footer)

    return embed


class SuggestionView(View):
    """Discord view with Accept/Dismiss buttons for Supervisor suggestions.

    Persistent across bot restarts via custom_id encoding.
    Accepted suggestions trigger the Supervisor in direct-address mode.
    Dismissed suggestions are logged to memory.
    """

    def __init__(
        self,
        suggestion_id: int | str,
        suggestion_type: str,
        text: str,
        project_id: str,
        task_title: str | None = None,
        handler=None,
        db=None,
    ):
        super().__init__(timeout=None)
        self.suggestion_id = suggestion_id
        self.suggestion_type = suggestion_type
        self.text = text
        self.project_id = project_id
        self.task_title = task_title
        self._handler = handler
        self._db = db

        # Persistent buttons with encoded custom_id
        accept_btn = Button(
            label="Accept",
            style=discord.ButtonStyle.success,
            custom_id=f"suggest_accept:{suggestion_id}",
        )
        accept_btn.callback = self._on_accept
        self.add_item(accept_btn)

        dismiss_btn = Button(
            label="Dismiss",
            style=discord.ButtonStyle.secondary,
            custom_id=f"suggest_dismiss:{suggestion_id}",
        )
        dismiss_btn.callback = self._on_dismiss
        self.add_item(dismiss_btn)

    async def _on_accept(self, interaction: discord.Interaction) -> None:
        """Handle accept button click."""
        if self._db:
            await self._db.resolve_chat_analyzer_suggestion(
                self.suggestion_id, "accepted"
            )
        if self._handler and self.suggestion_type == "task":
            await self._handler.execute("create_task", {
                "project_id": self.project_id,
                "title": self.task_title or self.text[:80],
                "description": self.text,
            })
        # Update embed to show accepted state
        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            embed.color = 0x27AE60
            embed.set_footer(text=f"✅ Accepted by {interaction.user.display_name}")
        await interaction.response.edit_message(embed=embed, view=None)

    async def _on_dismiss(self, interaction: discord.Interaction) -> None:
        """Handle dismiss button click."""
        if self._db:
            await self._db.resolve_chat_analyzer_suggestion(
                self.suggestion_id, "dismissed"
            )
        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            embed.color = 0x95A5A6
            embed.set_footer(text=f"❌ Dismissed by {interaction.user.display_name}")
        await interaction.response.edit_message(embed=embed, view=None)


# Backward compatibility alias
ChatAnalyzerSuggestionView = SuggestionView
```

Update `src/discord/notifications.py` to import from the new location. At the end of `notifications.py` where `AnalyzerSuggestionView` and `format_analyzer_suggestion_embed` are defined (lines ~1472-1667), replace them with re-exports:

```python
# Migrated to src/discord/views.py — re-export for backward compatibility
from src.discord.views import (
    SuggestionView as ChatAnalyzerSuggestionView,
    format_suggestion_embed as format_analyzer_suggestion_embed,
)
AnalyzerSuggestionView = ChatAnalyzerSuggestionView
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_views.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/discord/views.py src/discord/notifications.py tests/test_views.py
git commit -m "Migrate SuggestionView to standalone discord/views.py"
```

---

### Task 6: Wire ChatObserver into Discord Bot

**Files:**
- Modify: `src/discord/bot.py`
- Modify: `src/chat_observer.py` (add async start/stop)
- Modify: `tests/test_chat_observer.py`

Wire the ChatObserver into the Discord bot. The bot creates the observer on startup, feeds it `chat.message` events, and the observer dispatches batches to `Supervisor.observe()` on a timer.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_chat_observer.py`:

```python
def test_start_creates_timer_task():
    """start() creates the background batch timer."""
    obs = _make_observer()
    callback = AsyncMock()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(obs.start(callback))
        assert obs._running is True
    finally:
        loop.run_until_complete(obs.stop())
        loop.close()


def test_stop_cancels_timer():
    obs = _make_observer()
    callback = AsyncMock()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(obs.start(callback))
        loop.run_until_complete(obs.stop())
        assert obs._running is False
    finally:
        loop.close()


def test_process_batch_calls_callback():
    """When batch is flushed, callback receives messages and project_id."""
    obs = _make_observer()
    callback = AsyncMock(return_value={"action": "ignore"})

    obs.on_message(_make_message("particle system crashed"))
    batch = obs.flush_buffer(12345)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            obs._process_batch(12345, batch, callback)
        )
        callback.assert_called_once()
        call_kwargs = callback.call_args
        assert len(call_kwargs[1]["messages"]) == 1
    finally:
        loop.close()
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_chat_observer.py -v`
Expected: FAIL — `start`, `stop`, `_process_batch` don't exist

- [ ] **Step 3: Add async lifecycle and batch dispatch**

Add to `ChatObserver`:

```python
    def __init__(self, config, project_profiles=None):
        # ... existing init ...
        self._running = False
        self._timer_task: asyncio.Task | None = None
        self._observe_callback = None
        self._suggest_callback = None
        self._memory_callback = None
        self._last_message_time: dict[int, float] = {}

    async def start(
        self,
        observe_callback,
        suggest_callback=None,
        memory_callback=None,
    ) -> None:
        """Start the background batch timer.

        observe_callback: async fn(messages, project_id) -> dict
        suggest_callback: async fn(channel_id, project_id, suggestion) -> None
        memory_callback: async fn(project_id, content) -> None
        """
        self._observe_callback = observe_callback
        self._suggest_callback = suggest_callback
        self._memory_callback = memory_callback
        self._running = True
        self._timer_task = asyncio.create_task(self._batch_loop())

    async def stop(self) -> None:
        """Stop the background batch timer."""
        self._running = False
        if self._timer_task:
            self._timer_task.cancel()
            try:
                await self._timer_task
            except asyncio.CancelledError:
                pass
            self._timer_task = None

    async def _batch_loop(self) -> None:
        """Periodically check for channels with quiet windows expired."""
        import time
        while self._running:
            try:
                await asyncio.sleep(5)  # Check every 5 seconds
                if not self._observe_callback:
                    continue
                now = time.time()
                window = self._config.batch_window_seconds
                for channel_id in self.pending_channel_ids():
                    last_msg = self._last_message_time.get(channel_id, 0)
                    # Flush if quiet window expired OR buffer is full
                    if (now - last_msg >= window) or self.is_batch_ready(channel_id):
                        batch = self.flush_buffer(channel_id)
                        if batch:
                            await self._process_batch(
                                channel_id, batch, self._observe_callback
                            )
            except asyncio.CancelledError:
                break
            except Exception:
                pass  # Observer errors must never crash the bot

    async def _process_batch(
        self,
        channel_id: int,
        batch: list[dict],
        callback,
    ) -> None:
        """Send a batch to the Supervisor and handle the result."""
        if not batch:
            return
        project_id = batch[0].get("project_id", "")
        result = await callback(messages=batch, project_id=project_id)

        action = result.get("action")
        if action == "suggest" and self._suggest_callback:
            await self._suggest_callback(
                channel_id, project_id, result
            )
        elif action == "memory" and self._memory_callback:
            await self._memory_callback(
                project_id, result.get("content", "")
            )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_chat_observer.py -v`
Expected: PASS

- [ ] **Step 5: Wire into Discord bot**

In `src/discord/bot.py`, add ChatObserver initialization alongside the existing ChatAnalyzer setup. Look for where `self.agent = Supervisor(...)` is created (in `__init__` or `setup_hook`), and add:

```python
# After Supervisor creation
from src.chat_observer import ChatObserver
self._chat_observer = ChatObserver(
    config=self.orchestrator.config.supervisor.observation,
    project_profiles=self._build_project_profiles(),
)
```

Add a helper to build project profiles from registered projects:

```python
def _build_project_profiles(self) -> dict[str, set[str]]:
    """Build keyword sets from project names and repo names."""
    profiles = {}
    for proj_id, channel_id in self._channel_to_project.items():
        # Use project name words as terms
        terms = set(proj_id.replace("-", " ").replace("_", " ").split())
        profiles[proj_id] = terms
    return profiles
```

In the `on_message` handler, after the existing `chat.message` event emission (around line 1131-1141), add:

```python
        # Feed message to ChatObserver for Stage 1 filtering
        if hasattr(self, '_chat_observer') and project_id_for_event:
            self._chat_observer.on_message({
                "channel_id": channel_id,
                "project_id": project_id_for_event,
                "author": (
                    "AgentQueue" if message.author == self.user
                    else message.author.display_name
                ),
                "content": message.content,
                "timestamp": message.created_at.timestamp(),
                "is_bot": message.author == self.user,
            })
```

In `setup_hook` or `on_ready`, start the observer:

```python
if hasattr(self, '_chat_observer') and self._chat_observer.enabled:
    await self._chat_observer.start(
        observe_callback=self.agent.observe,
        suggest_callback=self._post_observation_suggestion,
        memory_callback=self._store_observation_memory,
    )
```

Add the suggestion posting callback:

```python
async def _post_observation_suggestion(
    self, channel_id: int, project_id: str, suggestion: dict
) -> None:
    """Post a Supervisor observation suggestion to Discord."""
    from src.discord.views import SuggestionView, format_suggestion_embed
    channel = self.get_channel(channel_id)
    if not channel:
        return
    suggestion_type = suggestion.get("suggestion_type", "context")
    text = suggestion.get("content", "")
    task_title = suggestion.get("task_title")

    # Store in DB
    suggestion_id = None
    if self.orchestrator.db:
        suggestion_id = await self.orchestrator.db.create_chat_analyzer_suggestion(
            project_id=project_id,
            channel_id=channel_id,
            suggestion_type=suggestion_type,
            suggestion_text=text,
        )

    embed = format_suggestion_embed(
        suggestion_type=suggestion_type,
        text=text,
        project_id=project_id,
        confidence=0.8,
        suggestion_id=suggestion_id,
    )
    view = SuggestionView(
        suggestion_id=suggestion_id or 0,
        suggestion_type=suggestion_type,
        text=text,
        project_id=project_id,
        task_title=task_title,
        handler=self.agent.handler,
        db=self.orchestrator.db,
    )
    await channel.send(embed=embed, view=view)

async def _store_observation_memory(
    self, project_id: str, content: str
) -> None:
    """Store an observation memory update via the Supervisor."""
    if not content:
        return
    try:
        await self.agent.handler.execute("memory_search", {
            "project_id": project_id,
            "query": content[:100],
        })
        # Use the memory system to store the observation
        if hasattr(self.orchestrator, 'memory_manager') and self.orchestrator.memory_manager:
            await self.orchestrator.memory_manager.add_memory(
                project_id=project_id,
                content=f"[Observed] {content}",
                source="chat_observation",
            )
    except Exception:
        pass  # Memory updates must never crash the bot
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_chat_observer.py tests/test_supervisor.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/chat_observer.py src/discord/bot.py tests/test_chat_observer.py
git commit -m "Wire ChatObserver into Discord bot with batch dispatch"
```

---

### Task 7: Write Behavioral Spec and Integration Test

**Files:**
- Create: `specs/chat-observer.md`
- Modify: `tests/test_chat_observer.py`

- [ ] **Step 1: Write integration test**

Add to `tests/test_chat_observer.py`:

```python
def test_full_flow_filter_buffer_flush():
    """End-to-end: messages are filtered, buffered, and flushed."""
    obs = _make_observer()

    # Mix of passing and failing messages
    obs.on_message(_make_message("ok"))  # fails stage 1
    obs.on_message(_make_message("lol"))  # fails stage 1
    obs.on_message(_make_message("the particle system is broken"))  # passes
    obs.on_message(_make_message("yes"))  # fails stage 1
    obs.on_message(_make_message("we need to deploy the fix"))  # passes

    assert obs.get_buffer_size(12345) == 2
    batch = obs.flush_buffer(12345)
    assert len(batch) == 2
    assert "particle" in batch[0]["content"]
    assert "deploy" in batch[1]["content"]


def test_observer_config_wired_to_supervisor_config():
    """ObservationConfig is accessible from SupervisorConfig."""
    from src.config import AppConfig
    app = AppConfig()
    assert hasattr(app.supervisor, "observation")
    assert app.supervisor.observation.enabled is True
    assert app.supervisor.observation.batch_window_seconds == 60
```

- [ ] **Step 2: Write spec**

Create `specs/chat-observer.md`:

```markdown
# Chat Observer Specification

The ChatObserver replaces the ChatAnalyzer with integrated observation
in the Supervisor. It processes project channel messages through a two-stage
pipeline: deterministic filtering (Stage 1) followed by Supervisor LLM
evaluation (Stage 2).

## Class: ChatObserver (src/chat_observer.py)

### Two-Stage Processing

**Stage 1 — Deterministic filter (zero LLM cost):**
- Project-specific terms from project profiles
- Action words: deploy, release, broken, bug, error, merge, etc.
- Custom keywords from config
- Message length threshold (200+ chars)
- Bot messages are always filtered out

**Stage 2 — Supervisor LLM pass (via Supervisor.observe()):**
- Only processes messages that pass Stage 1
- Lightweight call with conversation batch + project context
- Returns structured decision: ignore, memory update, or suggestion

### Conversation Batching
- Messages buffered per channel
- Flushed after configurable quiet window (default 60s) or max count (default 20)
- Processed as single conversation snippet

### Suggestion UI
- Reuses SuggestionView (src/discord/views.py) with Accept/Dismiss buttons
- Accepted suggestions trigger Supervisor in direct-address mode
- Dismissed suggestions logged to memory

### Configuration (under supervisor.observation)
- enabled: bool (default true)
- batch_window_seconds: int (default 60)
- max_buffer_size: int (default 20)
- stage1_keywords: list[str] (additional custom keywords)

### Invariants
- Stage 1 is purely deterministic — zero token cost for filtered messages
- Observer errors never crash the Discord bot
- Bot's own messages are always filtered out
- Empty batches are never sent to Stage 2
```

- [ ] **Step 3: Run all Phase 5 tests**

Run: `pytest tests/test_chat_observer.py tests/test_supervisor.py tests/test_config_supervisor.py tests/test_views.py -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add specs/chat-observer.md tests/test_chat_observer.py
git commit -m "Phase 5 complete: Chat Observation with two-stage pipeline"
```
