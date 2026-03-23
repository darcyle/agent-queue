---
auto_tasks: true
---

# Messaging Adapter Layer — Discord + Telegram Support

## Background & Design

### Current Architecture

The system currently hard-wires Discord as the sole messaging transport. The core orchestration logic (Orchestrator, CommandHandler, Supervisor) is already **well-decoupled** from Discord through callback injection:

- `Orchestrator.set_notify_callback()` — transport-agnostic notification dispatch
- `Orchestrator.set_create_thread_callback()` — transport-agnostic thread creation
- `CommandHandler.execute()` — pure business logic, no transport coupling
- `Supervisor` — LLM chat loop, takes message history lists, platform-agnostic
- `EventBus` — pub/sub independent of transport

However, Discord-specific types leak in several places:
1. **`orchestrator.py`** imports `src.discord.notifications` at module level (line 86) and uses `discord.Embed`/`discord.ui.View` in `_notify_channel` calls (~40 call sites passing `embed=` and `view=` kwargs)
2. **`main.py`** directly imports and instantiates `AgentQueueBot`, hard-codes `bot.start(config.discord.bot_token)`
3. **`config.py`** has `DiscordConfig` as a required top-level field on `AppConfig` with validation that requires `bot_token` and `guild_id`
4. **Notification formatting** (`src/discord/notifications.py`, `embeds.py`, `views.py`) produces Discord-native objects

### Design Decisions

1. **One transport per deployment** — config chooses `messaging: discord` or `messaging: telegram`. No multi-transport bridging. This keeps the abstraction simple.
2. **Abstract Messaging Port** — a new `MessagingPort` protocol/ABC defines the transport contract. Both `DiscordTransport` and `TelegramTransport` implement it.
3. **Platform-agnostic notification layer** — replace `discord.Embed`/`discord.ui.View` kwargs with a platform-neutral `RichNotification` dataclass that each transport renders into its native format.
4. **Factory pattern in `main.py`** — a `create_messaging_transport(config)` factory reads the config and returns the appropriate transport.
5. **Telegram implementation** — uses `python-telegram-bot` (async, well-maintained). Telegram "topics" in a supergroup map to Discord threads. Inline keyboards map to Discord buttons/views.

### Key Interfaces

```python
# src/messaging/port.py

@dataclass
class RichNotification:
    """Platform-neutral rich notification."""
    title: str
    description: str
    color: str = "default"  # "success", "error", "warning", "info", "critical"
    fields: list[tuple[str, str, bool]] = field(default_factory=list)  # (name, value, inline)
    footer: str = ""
    actions: list[NotificationAction] = field(default_factory=list)

@dataclass
class NotificationAction:
    """A button/action attached to a notification."""
    label: str
    action_id: str  # maps to CommandHandler.execute() call
    style: str = "primary"  # "primary", "danger", "secondary"
    args: dict = field(default_factory=dict)

class MessagingPort(ABC):
    """Abstract messaging transport contract."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def wait_until_ready(self) -> None: ...

    async def send_message(
        self, text: str, project_id: str | None = None, *,
        notification: RichNotification | None = None,
    ) -> Any: ...

    async def create_thread(
        self, channel_id: str, thread_name: str,
        initial_message: str | None = None,
    ) -> tuple[ThreadSendCallback, ThreadSendCallback] | None: ...

    def set_command_handler(self, handler: CommandHandler) -> None: ...
    def set_supervisor(self, supervisor: Supervisor) -> None: ...
```

### Config Changes

```yaml
# New top-level field
messaging: discord  # or "telegram"

# Existing discord: section stays as-is
discord:
  bot_token: ${DISCORD_BOT_TOKEN}
  guild_id: "..."
  # ...

# New telegram: section
telegram:
  bot_token: ${TELEGRAM_BOT_TOKEN}
  chat_id: "..."  # supergroup ID for main channel
  authorized_users: ["user1_id"]
  per_project_topics: true  # use forum topics for per-project routing
```

---

## Phase 1: Create the MessagingPort abstraction and RichNotification types

Create the abstract messaging interface that both Discord and Telegram will implement.

**Files to create:**
- `src/messaging/__init__.py` — exports
- `src/messaging/port.py` — `MessagingPort` ABC, `RichNotification`, `NotificationAction`, callback type aliases
- `src/messaging/types.py` — shared type aliases (`ThreadSendCallback`, `NotifyCallback`, `CreateThreadCallback`)

**Files to modify:**
- `src/orchestrator.py` — change the `NotifyCallback` and `CreateThreadCallback` type aliases to import from `src/messaging/types.py` instead of defining inline. Update `_notify_channel` signature to accept `notification: RichNotification | None` alongside the existing `embed`/`view` kwargs (backward-compatible — both work during migration).

**Key details:**
- `RichNotification` must support all current notification patterns: success/error/warning embeds, fields, footers, action buttons
- `NotificationAction` carries enough info for any transport to render a button and dispatch the callback to `CommandHandler.execute()`
- Keep existing `embed`/`view` kwargs working during migration (deprecate later)
- Add tests for RichNotification construction and field validation

---

## Phase 2: Create a platform-neutral notification formatter

Replace the Discord-specific `src/discord/notifications.py` format functions with platform-neutral equivalents that return `RichNotification` objects.

**Files to create:**
- `src/messaging/notifications.py` — all `format_*_embed` functions that currently return `discord.Embed` are duplicated here returning `RichNotification` instead. Plain-text `format_*` functions (no `_embed` suffix) move here unchanged.

**Files to modify:**
- `src/orchestrator.py` — change imports from `src.discord.notifications` to `src.messaging.notifications`. Update all `_notify_channel()` calls to pass `notification=` instead of `embed=`/`view=`. Remove the `discord` import entirely from orchestrator.
- Keep `src/discord/notifications.py` as a thin adapter that converts `RichNotification` → `discord.Embed` + `discord.ui.View` for the Discord transport.

**Key details:**
- The `classify_error()` function is transport-agnostic — move it to `src/messaging/notifications.py`
- Interactive views (`TaskFailedView`, `AgentQuestionView`, etc.) stay in `src/discord/views.py` but are constructed by the Discord transport from `NotificationAction` metadata
- This is the biggest refactor phase — ~40 call sites in orchestrator.py change from `embed=` to `notification=`
- Add tests comparing old embed output fields with new RichNotification fields to ensure parity

---

## Phase 3: Wrap Discord bot as a MessagingPort implementation

Wrap the existing `AgentQueueBot` in a `DiscordTransport` class that implements `MessagingPort`.

**Files to create:**
- `src/messaging/discord_transport.py` — `DiscordTransport(MessagingPort)` that wraps `AgentQueueBot`, converts `RichNotification` → `discord.Embed`/`discord.ui.View`, delegates to existing bot methods.

**Files to modify:**
- `src/discord/bot.py` — extract the callback-wiring logic (`set_notify_callback`, `set_create_thread_callback`, etc.) into methods that `DiscordTransport` can call. The bot itself becomes a "Discord engine" that `DiscordTransport` owns.
- `src/main.py` — replace direct `AgentQueueBot` instantiation with a factory: `transport = create_transport(config)`. Wire `transport` to orchestrator instead of bot directly. The factory reads `config.messaging` (defaulting to `"discord"` for backward compatibility).
- `src/config.py` — add `messaging: str = "discord"` field to `AppConfig`. Keep `DiscordConfig` validation only running when `messaging == "discord"`.

**Key details:**
- `DiscordTransport.send_message()` converts `RichNotification` → embed+view, then calls `bot._send_message()`
- `DiscordTransport.create_thread()` delegates to `bot._create_task_thread()`
- This phase should be **zero behavioral change** — existing Discord users see no difference
- Add integration tests that verify DiscordTransport correctly delegates to bot methods

---

## Phase 4: Add TelegramConfig and TelegramTransport skeleton

Add Telegram configuration and a skeleton transport that can connect and send plain-text messages.

**Files to create:**
- `src/messaging/telegram_transport.py` — `TelegramTransport(MessagingPort)` using `python-telegram-bot` library. Initially supports: `start()`, `stop()`, `send_message()` (plain text + RichNotification → Telegram HTML formatting), basic `on_message` routing to Supervisor.
- `src/telegram/__init__.py` — Telegram-specific helpers
- `src/telegram/formatting.py` — `RichNotification` → Telegram HTML message converter (Telegram supports `<b>`, `<i>`, `<code>`, `<a>` tags)

**Files to modify:**
- `src/config.py` — add `TelegramConfig` dataclass with `bot_token`, `chat_id`, `authorized_users`, `per_project_topics`. Add `telegram: TelegramConfig` to `AppConfig`. Validate only when `messaging == "telegram"`.
- `src/main.py` — extend transport factory to handle `"telegram"`.
- `pyproject.toml` / `requirements.txt` — add `python-telegram-bot[ext]` as optional dependency.

**Key details:**
- Telegram supergroups with "Topics" enabled map naturally to Discord's channel+thread model: the supergroup is the "server", topics are "channels/threads"
- `per_project_topics: true` creates a forum topic per project (like per-project Discord channels)
- Inline keyboards (`InlineKeyboardMarkup`) map to Discord button views
- Start with plain text + HTML formatting; inline keyboards come in Phase 5
- Add unit tests for Telegram formatting (RichNotification → HTML)
- Add integration test with mocked `python-telegram-bot` for send/receive

---

## Phase 5: Telegram interactive features — inline keyboards, topic threading, message routing

Complete the Telegram transport with full feature parity to Discord.

**Files to modify:**
- `src/messaging/telegram_transport.py` — add:
  - **Inline keyboard rendering**: Convert `NotificationAction` → `InlineKeyboardButton` with callback data encoding the `action_id` + `args`. Handle `CallbackQueryHandler` to dispatch to `CommandHandler.execute()`.
  - **Topic/thread management**: `create_thread()` creates a forum topic in the supergroup. Returns send functions scoped to that topic's `message_thread_id`.
  - **Per-project routing**: Map project IDs to topic IDs (stored in DB, same as Discord channel IDs). Route `send_message(project_id=...)` to the correct topic.
  - **Message handling**: Route incoming messages to Supervisor with project context injection (same pattern as Discord's `on_message`). Support authorized-user filtering.
  - **Attachment handling**: Download photos/documents from Telegram, save to `data_dir/attachments/`.
- `src/telegram/formatting.py` — add inline keyboard builder, topic name formatter.
- `src/database.py` — ensure project channel ID storage generalizes: either rename `discord_channel_id` to `messaging_channel_id` or add a `telegram_topic_id` field alongside it.

**Key details:**
- Telegram callback data is limited to 64 bytes — use compact encoding (e.g., `action_id:task_id` or a lookup table)
- Telegram rate limits: 30 messages/second to different chats, 20 messages/minute to same group. Implement a simple rate limiter.
- Forum topics require the supergroup to have "Topics" enabled — validate this at startup
- Message history buffering for Supervisor context works the same way as Discord's `_channel_buffers`
- Add end-to-end tests with mocked Telegram bot: message → supervisor → tool use → response
