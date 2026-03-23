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
| `src/setup_wizard.py` | Interactive CLI setup — hardcodes Discord in Step 2 | Add messaging platform choice, Telegram setup step |
| `specs/setup-wizard.md` | Setup wizard specification | Update to document new messaging platform selection flow |

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

## Phase 5: Update setup wizard and spec for Telegram support

Update `src/setup_wizard.py` and `specs/setup-wizard.md` to let users choose between Discord and Telegram during initial setup.

### Current setup flow (Discord-only):
1. Step 1: Workspace & Database directories
2. Step 2: Discord Bot (token, guild ID, channels, connectivity test)
3. Step 3: Agent Configuration
4. Step 4: Chat Provider
5. Step 5: Scheduling & Budget
6. Step 6: Write Config Files (hardcodes `discord:` section + `DISCORD_BOT_TOKEN` in .env)
7. Step 7: Connectivity Summary
8. Step 8: Launch Daemon

### New setup flow:
1. Step 1: Workspace & Database directories *(unchanged)*
2. **Step 2: Messaging Platform** *(new — choose Discord or Telegram)*
3. Step 3: Platform-specific setup (Discord OR Telegram) *(conditional)*
4. Step 4: Agent Configuration *(unchanged, renumbered)*
5. Step 5: Chat Provider *(unchanged, renumbered)*
6. Step 6: Scheduling & Budget *(unchanged, renumbered)*
7. Step 7: Write Config Files *(updated for platform-aware output)*
8. Step 8: Connectivity Summary *(updated for platform-aware reporting)*
9. Step 9: Launch Daemon *(unchanged, renumbered)*

### Files to modify:
- `src/setup_wizard.py` — add platform selection step, Telegram setup step, update config writing
- `specs/setup-wizard.md` — document the new steps

### New Step 2: Messaging Platform Selection

```python
def step_messaging_platform(existing: dict) -> str:
    """Choose between Discord and Telegram as the messaging platform."""
    yaml_cfg = existing.get("_yaml", {})
    existing_platform = yaml_cfg.get("messaging_platform", "discord")

    step_header(2, "Messaging Platform")
    print(f"""  Choose your messaging platform for controlling agent-queue.
  You can switch later by re-running the setup wizard.

  {BOLD}[1] Discord{RESET} — rich embeds, threads, slash commands
  {BOLD}[2] Telegram{RESET} — lightweight, mobile-first, forum topics
""")

    default = "1" if existing_platform == "discord" else "2"
    choice = prompt("Platform", default)
    platform = "telegram" if choice == "2" else "discord"

    if platform == "telegram":
        success("Selected: Telegram")
    else:
        success("Selected: Discord")

    return platform
```

### New: `step_telegram(existing)` function

Collects Telegram bot credentials and verifies connectivity. Mirrors `step_discord()` structure.

```python
def step_telegram(existing: dict) -> dict:
    """Collect Telegram bot credentials and verify connectivity."""
    yaml_cfg = existing.get("_yaml", {})
    telegram_cfg = yaml_cfg.get("telegram", {})

    existing_token = existing.get("TELEGRAM_BOT_TOKEN", "")
    existing_chat_id = telegram_cfg.get("chat_id", "") or existing.get("TELEGRAM_CHAT_ID", "")

    # Bot Token
    if existing_token:
        bot_token = existing_token
    else:
        step_header(3, "Telegram Bot")
        print(f"""  To create a Telegram bot:
  1. Open Telegram and message {BOLD}@BotFather{RESET}
  2. Send {BOLD}/newbot{RESET} and follow the prompts to create your bot
  3. Copy the bot token BotFather gives you
  4. Add the bot to your group/supergroup as an admin
""")
        bot_token = prompt_secret("Bot token")
        if not bot_token:
            error("Bot token is required")
            sys.exit(1)
        _save_env_value("TELEGRAM_BOT_TOKEN", bot_token)

    # Chat ID
    if existing_chat_id:
        chat_id = existing_chat_id
    else:
        print(f"""
  To find your chat ID:
  1. Add {BOLD}@userinfobot{RESET} to your group, or message it directly
  2. It will reply with the chat ID (a number, negative for groups)
  3. For supergroups with topics, use the group's chat ID
""")
        chat_id = prompt("Chat ID")
        if not chat_id:
            error("Chat ID is required")
            sys.exit(1)
        _save_env_value("TELEGRAM_CHAT_ID", chat_id)

    # Use forum topics
    use_topics = telegram_cfg.get("use_topics", True)
    use_topics = prompt_yes_no("Use forum topics for task threads (requires supergroup)", use_topics)

    # Connectivity test
    print()
    info("Testing Telegram connectivity...")
    telegram_ok = _test_telegram(bot_token, chat_id)

    # Authorized users (optional)
    authorized_users = telegram_cfg.get("authorized_users", [])
    if not authorized_users:
        print()
        print(f"  {BOLD}Authorized Users{RESET} (optional)")
        info("Restrict bot access to specific Telegram user IDs.")
        info("Enter user IDs one per line (empty line to finish):")
        while True:
            uid = input("    > ").strip()
            if not uid:
                break
            authorized_users.append(uid)

    return {
        "bot_token": bot_token,
        "chat_id": chat_id,
        "use_topics": use_topics,
        "authorized_users": authorized_users,
        "telegram_ok": telegram_ok,
    }
```

### New: `_test_telegram(token, chat_id)` function

```python
def _test_telegram(token: str, chat_id: str) -> bool:
    """Test Telegram bot connectivity by calling getMe and getChat."""
    try:
        import urllib.request
        import json

        # Test 1: getMe — verifies the bot token is valid
        url = f"https://api.telegram.org/bot{token}/getMe"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            if not data.get("ok"):
                error("Invalid bot token")
                return False
            bot_name = data["result"].get("username", "unknown")
            success(f"Bot connected: @{bot_name}")

        # Test 2: getChat — verifies the chat_id is accessible
        url = f"https://api.telegram.org/bot{token}/getChat?chat_id={chat_id}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            if not data.get("ok"):
                error(f"Cannot access chat {chat_id}")
                return False
            chat_title = data["result"].get("title") or data["result"].get("first_name", "DM")
            success(f"Chat verified: {chat_title}")

        return True
    except Exception as e:
        error(f"Telegram connectivity test failed: {e}")
        return False
```

### Update `step_write_config()`:

The function signature gains a `messaging_platform` parameter and an optional `telegram_cfg` dict. Config generation becomes conditional:

```python
def step_write_config(
    workspace, db_path, messaging_platform, discord_cfg, telegram_cfg,
    agents_cfg, sched_cfg, chat_provider_cfg,
) -> Path:
```

**`.env` changes:**
- Discord: write `DISCORD_BOT_TOKEN=...` (as before)
- Telegram: write `TELEGRAM_BOT_TOKEN=...` instead

**`config.yaml` changes:**
- Always write `messaging_platform: discord` or `messaging_platform: telegram`
- Discord: write the `discord:` section (as before)
- Telegram: write the `telegram:` section instead:
  ```yaml
  messaging_platform: telegram

  telegram:
    bot_token: ${TELEGRAM_BOT_TOKEN}
    chat_id: "123456789"
    use_topics: true
    authorized_users:
      - "111222333"
  ```

### Update `step_test_connectivity()`:

Add platform-awareness:
- Discord: show Discord connectivity status (as before)
- Telegram: show Telegram bot and chat connectivity status

### Update `main()`:

```python
def main():
    banner()
    existing = _load_existing_config()
    ...
    workspace, db_path = step_directories(existing)
    messaging_platform = step_messaging_platform(existing)

    discord_cfg = None
    telegram_cfg = None
    if messaging_platform == "discord":
        discord_cfg = step_discord(existing)
    else:
        telegram_cfg = step_telegram(existing)

    agents_cfg = step_agents(existing)
    chat_provider_cfg = step_chat_provider(existing)
    sched_cfg = step_scheduling(existing)

    config_path = step_write_config(
        workspace, db_path, messaging_platform,
        discord_cfg, telegram_cfg,
        agents_cfg, sched_cfg, chat_provider_cfg,
    )

    step_test_connectivity(messaging_platform, discord_cfg, telegram_cfg, agents_cfg, chat_provider_cfg)
    step_launch(config_path)
    ...
```

### Update `specs/setup-wizard.md`:

- Add new section "Step 2: Messaging Platform" documenting the platform selection prompt
- Rename current "Step 2: Discord Bot" to "Step 3a: Discord Bot" — runs only when `messaging_platform == "discord"`
- Add new section "Step 3b: Telegram Bot" documenting Telegram credential collection, connectivity test, and authorized users — runs only when `messaging_platform == "telegram"`
- Update "Step 6: Write Config Files" to document platform-conditional `.env` and `config.yaml` output
- Update "Step 7: Connectivity Summary" to document platform-aware status display
- Renumber all subsequent steps

### Tests:
- `step_messaging_platform()` defaults to "discord" with no existing config
- `step_messaging_platform()` pre-fills from existing `messaging_platform` in config
- `step_telegram()` collects token, chat_id, use_topics, authorized_users
- `_test_telegram()` validates bot token and chat accessibility
- `step_write_config()` writes `TELEGRAM_BOT_TOKEN` to `.env` when platform is "telegram"
- `step_write_config()` writes `telegram:` section to `config.yaml` when platform is "telegram"
- `step_write_config()` writes `messaging_platform: telegram` to `config.yaml`
- Backward compatibility: existing configs without `messaging_platform` default to "discord"
- `main()` calls `step_discord()` only when Discord is selected
- `main()` calls `step_telegram()` only when Telegram is selected

---

## Phase 6: Update health check and startup diagnostics

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
