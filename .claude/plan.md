---
auto_tasks: true
---

# Messaging Adapter Layer — Discord + Telegram Support

## Background & Architecture Analysis

### Current State

The system is already well-decoupled from Discord at the orchestrator level. The `Orchestrator` communicates with the UI layer exclusively through injected callbacks:

- `set_notify_callback(callback)` — all notifications (task started/completed/failed, PR created, agent questions, budget warnings)
- `set_create_thread_callback(callback)` — creates per-task output threads, returns `(send_to_thread, notify_main_channel)` pair
- `set_command_handler(handler)` — passes `CommandHandler` for interactive views
- `set_supervisor(supervisor)` — passes `Supervisor` for post-task LLM operations

The `CommandHandler` is already unified — Discord slash commands and LLM tools both call the same `_cmd_*` methods. The `Supervisor` is stateless and platform-agnostic.

**However**, the wiring in `main.py` hardcodes `AgentQueueBot` (Discord), and the bot startup sequence assumes Discord is always present. The `AppConfig` only has a `discord: DiscordConfig` field. Interactive views (buttons, modals) are tightly Discord-specific in `src/discord/views.py` and `src/discord/notifications.py`.

### Design Decisions

1. **One platform at a time** — The config selects either `discord` or `telegram` as the messaging platform. Running both simultaneously adds complexity (dual routing, message dedup, channel mapping) with minimal benefit. The orchestrator callbacks are single-assignment.

2. **Shared abstractions** — A `MessagingAdapter` ABC defines the contract that both Discord and Telegram implement. The orchestrator and main.py talk only to this interface.

3. **Platform-specific directories** — `src/discord/` stays as-is, `src/telegram/` mirrors its structure. No shared base classes for platform-specific UI concerns (embeds vs inline keyboards).

4. **Feature parity via CommandHandler** — Both platforms get full command access through the same `CommandHandler`. Telegram uses bot commands (`/create_task`) mapped to the same handler methods.

### Key Files

| File | Current Role | Changes Needed |
|------|-------------|----------------|
| `src/main.py` | Hardcodes Discord bot | Use factory based on config to create the right messaging adapter |
| `src/config.py` | Only `DiscordConfig` | Add `TelegramConfig`, add `messaging_platform` selector |
| `src/orchestrator.py` | Callback-based (already clean) | No changes needed — callbacks are platform-agnostic |
| `src/discord/bot.py` | `AgentQueueBot` class | Extract interface methods into `MessagingAdapter` ABC |
| `src/discord/notifications.py` | Discord-specific formatting | Keep as-is, Telegram gets its own formatters |
| `src/command_handler.py` | Unified command logic | No changes — already platform-agnostic |
| `src/supervisor.py` | Stateless LLM agent | No changes — already platform-agnostic |

---

## Phase 1: Define the MessagingAdapter ABC and config changes

Create `src/messaging/` package with the abstract base class that defines the contract both Discord and Telegram must implement. Also add `TelegramConfig` and `messaging_platform` selector to config.

### Files to create:
- `src/messaging/__init__.py` — exports `MessagingAdapter`, `create_messaging_adapter`
- `src/messaging/base.py` — the ABC

### MessagingAdapter interface:

```python
class MessagingAdapter(ABC):
    """Abstract messaging platform adapter."""

    @abstractmethod
    async def start(self) -> None:
        """Connect to the platform and begin listening for messages."""

    @abstractmethod
    async def wait_until_ready(self) -> None:
        """Block until the platform connection is established and ready."""

    @abstractmethod
    async def close(self) -> None:
        """Disconnect from the platform gracefully."""

    @abstractmethod
    async def send_message(self, text: str, project_id: str | None = None,
                           *, embed: Any = None, view: Any = None) -> None:
        """Send a notification message to the appropriate channel/chat."""

    @abstractmethod
    async def create_task_thread(self, task, project) -> tuple[MessageCallback, MessageCallback]:
        """Create a thread/topic for task output streaming.
        Returns (send_to_thread, notify_main_channel) callbacks."""

    @abstractmethod
    def get_command_handler(self) -> Any:
        """Return the CommandHandler instance."""

    @abstractmethod
    def get_supervisor(self) -> Any:
        """Return the Supervisor instance."""
```

### Config changes in `src/config.py`:

Add `TelegramConfig` dataclass:
```python
@dataclass
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""           # Main chat/group for notifications
    authorized_users: list[str] = field(default_factory=list)  # Telegram user IDs
    per_project_chats: dict[str, str] = field(default_factory=dict)  # project_id -> chat_id
    use_topics: bool = True     # Use forum topics for task threads (requires supergroup)
```

Add to `AppConfig`:
```python
messaging_platform: str = "discord"  # "discord" or "telegram"
telegram: TelegramConfig = field(default_factory=TelegramConfig)
```

Only validate the active platform's config — skip telegram validation when `messaging_platform == "discord"` and vice versa.

### Factory function:
```python
def create_messaging_adapter(config: AppConfig, orchestrator: Orchestrator) -> MessagingAdapter:
    if config.messaging_platform == "discord":
        from src.discord.adapter import DiscordMessagingAdapter
        return DiscordMessagingAdapter(config, orchestrator)
    elif config.messaging_platform == "telegram":
        from src.telegram.adapter import TelegramMessagingAdapter
        return TelegramMessagingAdapter(config, orchestrator)
    else:
        raise ValueError(f"Unknown messaging platform: {config.messaging_platform}")
```

### Tests:
- ABC cannot be instantiated directly
- Factory raises on unknown platform
- Factory returns correct type for "discord" and "telegram"
- Config validation only validates active platform
- Default `messaging_platform` is "discord" (backward compatible)

---

## Phase 2: Wrap Discord in the MessagingAdapter

Wrap the existing `AgentQueueBot` in a `DiscordMessagingAdapter` class that implements the ABC. This is a thin wrapper — the existing bot code stays intact.

### Files to create/modify:
- Create `src/discord/adapter.py` — `DiscordMessagingAdapter(MessagingAdapter)` that wraps `AgentQueueBot`
- Modify `src/main.py` — replace hardcoded `AgentQueueBot` with `create_messaging_adapter()`

### DiscordMessagingAdapter implementation:

```python
class DiscordMessagingAdapter(MessagingAdapter):
    def __init__(self, config: AppConfig, orchestrator: Orchestrator):
        self._bot = AgentQueueBot(config, orchestrator)
        self._config = config

    async def start(self) -> None:
        await self._bot.start(self._config.discord.bot_token)

    async def wait_until_ready(self) -> None:
        await self._bot.wait_until_ready()

    async def close(self) -> None:
        await self._bot.close()

    async def send_message(self, text, project_id=None, *, embed=None, view=None):
        await self._bot._send_message(text, project_id, embed=embed, view=view)

    async def create_task_thread(self, task, project):
        return await self._bot._create_task_thread(task, project)

    def get_command_handler(self):
        return self._bot.agent.handler

    def get_supervisor(self):
        return self._bot.agent
```

### Changes to `src/main.py`:

Replace the hardcoded `AgentQueueBot` creation with the factory. Move callback registration from Discord's `on_ready()` to `main.py` so it's explicit and platform-agnostic:

```python
adapter = create_messaging_adapter(config, orch)
# Callbacks registered after adapter.wait_until_ready()
orch.set_notify_callback(adapter.send_message)
orch.set_create_thread_callback(adapter.create_task_thread)
orch.set_command_handler(adapter.get_command_handler())
orch.set_supervisor(adapter.get_supervisor())
```

The Discord `on_ready()` handler should skip callback registration when it detects the adapter has already registered them.

### Tests:
- `DiscordMessagingAdapter` correctly wraps `AgentQueueBot`
- All existing tests continue to pass (no behavior change)
- `main.py` creates the adapter through the factory
- Callback registration happens through the adapter path

---

## Phase 3: Implement Telegram Bot Core

Create `src/telegram/` package with the Telegram bot implementation using `python-telegram-bot` (async).

### Files to create:
- `src/telegram/__init__.py`
- `src/telegram/bot.py` — `TelegramBot` class (core message handling, chat routing)
- `src/telegram/adapter.py` — `TelegramMessagingAdapter(MessagingAdapter)` wrapper
- `src/telegram/commands.py` — Telegram command handlers (`/create_task`, `/list_tasks`, etc.)
- `src/telegram/notifications.py` — Telegram-specific message formatting (MarkdownV2)

### Dependencies:
- Add `python-telegram-bot[ext]` to requirements (async-native, well-maintained)

### TelegramBot responsibilities:
- Connect to Telegram Bot API via long polling
- Route incoming messages to `Supervisor.chat()` (same as Discord bot)
- Maintain per-chat message buffers and conversation history (same pattern as Discord)
- Authorize users by Telegram user ID
- Per-project chat routing (similar to Discord's per-project channels)

### Thread equivalents:
- **Forum topics** (supergroups with topics enabled): Each task gets its own topic — closest to Discord threads. This is the preferred mode (`use_topics: true`).
- **Reply threads**: If topics aren't available, use reply chains for task output grouping.

### TelegramMessagingAdapter:

```python
class TelegramMessagingAdapter(MessagingAdapter):
    def __init__(self, config: AppConfig, orchestrator: Orchestrator):
        self._bot = TelegramBot(config, orchestrator)
        self._config = config

    async def start(self) -> None:
        await self._bot.start()

    async def wait_until_ready(self) -> None:
        await self._bot.wait_until_ready()

    async def close(self) -> None:
        await self._bot.stop()

    async def send_message(self, text, project_id=None, *, embed=None, view=None):
        await self._bot.send_notification(text, project_id, embed=embed)

    async def create_task_thread(self, task, project):
        return await self._bot.create_task_topic(task, project)

    def get_command_handler(self):
        return self._bot.handler

    def get_supervisor(self):
        return self._bot.supervisor
```

### Telegram message formatting:
- Discord embeds → Telegram MarkdownV2 formatted messages (bold title, fields as key-value lines)
- Discord buttons → Telegram inline keyboards (Retry, Approve, Skip)
- Discord file attachments → Telegram `send_document()`
- 4096 char limit per message (vs Discord's 2000) — still need splitting for very long outputs

### Command mapping:
All Telegram bot commands map to `CommandHandler.execute()`:
- `/create_task description` → `handler.execute("create_task", {...})`
- `/list_tasks` → `handler.execute("list_tasks", {})`
- `/status` → `handler.execute("status", {})`
- Natural language messages → `Supervisor.chat()` (same as Discord)

### Tests:
- Unit tests for `TelegramBot` with mocked `python-telegram-bot` API
- Unit tests for `TelegramMessagingAdapter` implementing the ABC correctly
- Unit tests for Telegram notification formatting (MarkdownV2 escaping)
- Unit tests for command parsing and routing to `CommandHandler`
- Integration test: message received → supervisor invoked → response sent

---

## Phase 4: Implement Telegram Interactive Features

Add inline keyboards (button equivalents), callback query handling, and task approval flows for Telegram.

### Files to create/modify:
- `src/telegram/views.py` — Inline keyboard builders for task actions (Retry, Skip, Approve, Dismiss)
- `src/telegram/bot.py` — Add callback query handler for button presses

### Interactive features mapping:

| Discord Feature | Telegram Equivalent |
|----------------|-------------------|
| `TaskFailedView` (Retry/Skip buttons) | Inline keyboard with Retry/Skip buttons |
| `TaskApprovalView` (Approve/Dismiss) | Inline keyboard with Approve/Dismiss buttons |
| `AgentQuestionView` (Answer button) | Inline keyboard with Answer button → prompts reply |
| Slash command autocomplete | Bot command suggestions via `setMyCommands` |
| Modal forms (project wizard) | Multi-step conversation flow with `ConversationHandler` |

### Callback query routing:
```python
# Button callback data format: "action:task_id"
# e.g., "retry:abc123", "approve:def456"
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action, task_id = update.callback_query.data.split(":", 1)
    result = await handler.execute(action, {"task_id": task_id})
    await update.callback_query.answer(result[:200])
```

### Tests:
- Inline keyboard generation for each view type
- Callback query parsing and routing
- Error handling for expired/invalid callbacks
- Conversation flow for multi-step commands

---

## Phase 5: Update health check and startup diagnostics

Update the health check endpoint and startup sequence to be platform-aware.

### Files to modify:
- `src/main.py` — update health check to report active messaging platform
- `src/health.py` — add platform-agnostic health reporting

### Changes:
- Health check `messaging` field reports which platform is active and whether it's connected:
  ```json
  {"messaging": {"ok": true, "platform": "telegram", "connected": true}}
  ```
  instead of the current:
  ```json
  {"discord": {"ok": true}}
  ```
- Startup log message indicates which platform is being used
- The `_health_checks` function queries the adapter (not the bot directly) for connection status
- Add `is_connected() -> bool` method to `MessagingAdapter` ABC for health reporting

### Tests:
- Health check returns correct platform name
- Health check reports connection status via adapter
