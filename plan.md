# What Already Exists — Validated ✅

This document validates the existing per-project channel infrastructure in agent-queue and identifies specific gaps that need to be addressed for full multi-channel support with automatic channel creation, channel-context-aware project resolution, and updated setup wizard support.

**Validation method:** Direct code inspection of every file in the codebase that touches channel, project, or routing logic. Every claim below includes file path and line-number references verified against the current source.

---

## 1. Database Layer ✅

### Schema (Verified)
**File:** `src/database.py` lines 14–27

The `projects` table includes both per-project channel ID columns in the `CREATE TABLE` statement:

```sql
CREATE TABLE IF NOT EXISTS projects (
    ...
    discord_channel_id TEXT,          -- line 24
    discord_control_channel_id TEXT,  -- line 25
    ...
);
```

Both columns are nullable, meaning per-project channels are opt-in — projects without dedicated channels fall back to global.

### Migrations (Verified)
**File:** `src/database.py` lines 200–201

Backward-compatible `ALTER TABLE` migrations exist for both columns:
```python
"ALTER TABLE projects ADD COLUMN discord_channel_id TEXT",
"ALTER TABLE projects ADD COLUMN discord_control_channel_id TEXT",
```
These are wrapped in try/except (lines 203–206) so they silently succeed on new databases and silently skip on upgraded ones.

### CRUD Operations (Verified)
**File:** `src/database.py`

- **`create_project()`** (lines 215–227) — Inserts both `discord_channel_id` and `discord_control_channel_id` from the Project dataclass.
- **`update_project()`** (lines 250–262) — Generic `**kwargs` updater, supports setting either channel field by name.
- **`_row_to_project()`** (lines 264–277) — Deserializes both fields with safe `"key" in keys` checks for backward compatibility with pre-migration databases.
- **`list_projects()`** (lines 238–248) — Returns full Project objects including channel fields.
- **`get_project()`** (lines 229–236) — Single project lookup with full fields.

**Status: COMPLETE** — No database changes needed for per-project channel storage.

---

## 2. Data Model ✅

### Project Dataclass (Verified)
**File:** `src/models.py` lines 84–95

```python
@dataclass
class Project:
    id: str
    name: str
    credit_weight: float = 1.0
    max_concurrent_agents: int = 2
    status: ProjectStatus = ProjectStatus.ACTIVE
    total_tokens_used: int = 0
    budget_limit: int | None = None
    workspace_path: str | None = None
    discord_channel_id: str | None = None          # line 94
    discord_control_channel_id: str | None = None   # line 95
```

Both channel fields are typed as `str | None` with `None` defaults, consistent with opt-in semantics.

**Status: COMPLETE** — No model changes needed.

---

## 3. Bot Channel Resolution & Routing ✅

### Per-Project Channel Caches (Verified)
**File:** `src/discord/bot.py` lines 32–33

```python
self._project_channels: dict[str, discord.TextChannel] = {}           # project_id → notifications channel
self._project_control_channels: dict[str, discord.TextChannel] = {}   # project_id → control channel
```

Two separate forward-lookup dictionaries, mapping `project_id` string → `discord.TextChannel` object. These are in-memory caches populated at startup and updated at runtime.

### Startup Resolution (Verified)
**File:** `src/discord/bot.py` lines 197–229 (`_resolve_project_channels()`)

At bot startup (called from `on_ready()` at line 122), this method:
1. Reads all projects from the database
2. For each project with `discord_channel_id` set, looks up the channel via `self._guild.get_channel(int(...))`
3. If found and is a `TextChannel`, caches it in `_project_channels`
4. If not found, logs a warning (lines 216–219)
5. Repeats for `discord_control_channel_id` → `_project_control_channels`

### Fallback Routing (Verified)
**File:** `src/discord/bot.py` lines 231–245

```python
def _get_notification_channel(self, project_id=None):
    if project_id and project_id in self._project_channels:
        return self._project_channels[project_id]
    return self._notifications_channel  # global fallback

def _get_control_channel(self, project_id=None):
    if project_id and project_id in self._project_control_channels:
        return self._project_control_channels[project_id]
    return self._control_channel  # global fallback
```

Clean two-tier resolution: project-specific → global. If no project_id is given or the project has no dedicated channel, global channels are used.

### Runtime Cache Updates (Verified)
**File:** `src/discord/bot.py` lines 67–78

```python
def update_project_channel(self, project_id, channel, channel_type="notifications"):
    if channel_type == "control":
        self._project_control_channels[project_id] = channel
    else:
        self._project_channels[project_id] = channel
```

Called immediately after `/set-channel` and `/create-channel` commands (from `commands.py` lines 373, 449) so the bot routes to new channels without restart.

### Notification Routing Methods (Verified)
**File:** `src/discord/bot.py` lines 247–307

Three routing methods, all accepting optional `project_id`:
- **`_send_notification(text, project_id)`** (lines 247–251) — Routes to project or global notifications channel.
- **`_send_control_message(text, project_id)`** (lines 253–257) — Routes to project or global control channel.
- **`_create_task_thread(thread_name, initial_message, project_id)`** (lines 259–307) — Creates task threads in the correct project notifications channel, returns `(send_to_thread, notify_main_channel)` callback pair.

**Status: COMPLETE** — Full forward-lookup (project→channel) routing with fallback is implemented.

---

## 4. Orchestrator Integration ✅

### Callbacks Pass project_id (Verified)
**File:** `src/orchestrator.py` lines 115–137

Both notification callbacks accept and forward `project_id`:
```python
async def _notify_channel(self, message, project_id=None):
    if self._notify:
        await self._notify(message, project_id)

async def _control_channel_post(self, message, project_id=None):
    if self._control_notify:
        await self._control_notify(message, project_id)
```

All task lifecycle notifications (completion, failure, status changes) pass `project_id` through to these callbacks, ensuring per-project routing from the orchestrator layer.

### Callback Wire-Up (Verified)
**File:** `src/discord/bot.py` lines 107–119

In `on_ready()`, the bot registers its routing methods as orchestrator callbacks:
- `self.orchestrator.set_notify_callback(self._send_notification)` (line 107)
- `self.orchestrator.set_control_callback(self._send_control_message)` (line 113)
- `self.orchestrator.set_create_thread_callback(self._create_task_thread)` (line 119)

**Status: COMPLETE** — Orchestrator correctly propagates project context to Discord routing.

---

## 5. Message Listener with Channel Context ✅

### Per-Project Control Channel Detection (Verified)
**File:** `src/discord/bot.py` lines 326–344 (in `on_message()`)

```python
# Check if this is a per-project control channel
project_control_id: str | None = None
for pid, ch in self._project_control_channels.items():
    if message.channel.id == ch.id:
        project_control_id = pid
        break
is_project_control = project_control_id is not None
```

Linear scan of `_project_control_channels` to find which project a message belongs to. Works correctly but is O(n) per message.

### Context Injection (Verified)
**File:** `src/discord/bot.py` lines 368–383

When a message arrives in a project's control channel, the bot prepends context:
```python
if is_project_control and not is_control:
    user_text = (
        f"[Context: this is the control channel for project "
        f"`{project_control_id}`. Default to using "
        f"project_id='{project_control_id}' for all project-scoped "
        f"commands.]\n{text}"
    )
```

This allows the LLM to automatically scope its tool calls to the correct project when responding to natural language in a project channel.

### Notes Thread Detection (Verified)
**File:** `src/discord/bot.py` lines 341–342, 377–383

Notes threads are also detected and get similar context injection with project-scoped defaults for notes tools.

**Status: COMPLETE** — Natural language messages in project channels are correctly context-enriched. However, this only works for LLM-processed messages, NOT for slash commands (see Gap 2).

---

## 6. Slash Commands for Channel Management ✅

### `/set-channel` (Verified)
**File:** `src/discord/commands.py` lines 342–376

- Takes `project_id` (string), `channel` (Discord TextChannel picker), `channel_type` (choice: notifications/control)
- Calls `handler.execute("set_project_channel", {...})` to persist to DB
- Calls `bot.update_project_channel()` to update runtime cache
- Reports success with channel mention

### `/create-channel` (Verified)
**File:** `src/discord/commands.py` lines 378–452

- Takes `project_id`, optional `channel_name` (defaults to project_id), optional `channel_type`, optional `category`
- Validates project exists via `list_projects`
- Calls `guild.create_text_channel()` to create the Discord channel
- Links it via `set_project_channel` command
- Updates bot cache
- Handles permission errors gracefully

### `/projects` Shows Channel Info (Verified)
**File:** `src/discord/commands.py` — The `/projects` command renders project info including channel mentions when channel IDs are set.

**Status: COMPLETE** — Interactive channel management commands are fully functional.

---

## 7. Chat Agent (LLM) Tools ✅

### `set_project_channel` Tool (Verified)
**File:** `src/chat_agent.py` lines 77–100

Full tool definition with `project_id`, `channel_id`, and `channel_type` (enum: notifications/control). Correctly delegates to the command handler.

### `get_project_channels` Tool (Verified)
**File:** `src/chat_agent.py` lines 102–110

Returns both `notifications_channel_id` and `control_channel_id` for a given project.

### System Prompt Documentation (Verified)
**File:** `src/chat_agent.py` lines 715–723

The LLM system prompt documents the per-project channels feature, including:
- Default fallback behavior
- How to use `set_project_channel` and `get_project_channels`
- Reference to `/set-channel` and `/create-channel` commands

**Status: COMPLETE** — LLM has full awareness and tooling for channel management.

---

## 8. Command Handler (Unified Backend) ✅

### `_cmd_set_project_channel` (Verified)
**File:** `src/command_handler.py` lines 197–219

- Validates project exists
- Validates `channel_type` is "notifications" or "control"
- Updates the correct DB column via `update_project()`
- Returns structured result

### `_cmd_get_project_channels` (Verified)
**File:** `src/command_handler.py` lines 221–231

- Returns both channel IDs from the project record

### `_cmd_list_projects` Includes Channels (Verified)
**File:** `src/command_handler.py` lines 131–148

- Includes `discord_channel_id` and `discord_control_channel_id` in output when set

### `_cmd_create_project` Does NOT Create Channels (Verified)
**File:** `src/command_handler.py` lines 150–162

- Creates workspace directory and project record
- Does NOT create or link any Discord channels (see Gap 4)

**Status: COMPLETE** — Backend commands for channel management are solid. Gap exists in project creation flow.

---

## 9. Global Channel Configuration ✅

### DiscordConfig (Verified)
**File:** `src/config.py` lines 10–19

```python
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
```

Global channels are identified by **name** (not ID) and resolved at startup by scanning guild text channels.

**Note:** There is no `auto_create_project_channels` or `project_channel_category` config option (see Gap 4).

**Status: COMPLETE** — Global channel config works. Per-project channel config is managed in the DB, not config.yaml (by design).

---

## 10. Setup Wizard (with Gaps)

### What Exists (Verified)
**File:** `setup_wizard.py` (~1000 lines)

The setup wizard handles:
1. Discord bot token and guild ID
2. Global channel names (control, notifications, agent_questions)
3. API key configuration (Anthropic, AWS Bedrock, Google Vertex AI)
4. Chat provider selection (Anthropic or Ollama)
5. Workspace directory
6. First project creation (name, repo URL, agent)
7. Config.yaml generation

### What's Missing (Verified)
- No per-project channel configuration step
- No option to auto-create channels during project setup
- No guidance for setting up project-specific channels after initial setup
- The wizard runs before the bot starts, so it cannot call the Discord API to create channels

**Status: PARTIALLY COMPLETE** — Global channel setup works. Per-project channel setup is missing (see Gap 5).

---

## Identified Gaps

### Gap 1: No Reverse Channel→Project Lookup (HIGH PRIORITY)

**Current state:** Forward lookup (project→channel) exists via `_project_channels` and `_project_control_channels` dicts. No reverse lookup (channel→project) exists.

**Evidence:** The `on_message()` handler performs a linear scan of `_project_control_channels.items()` (lines 333–337) as a workaround. Slash commands have no equivalent mechanism.

**Impact:** Slash commands issued in project channels cannot auto-infer the project context.

**Fix needed:** Add `_channel_to_project: dict[int, str]` reverse-lookup dict, populated alongside forward caches in `_resolve_project_channels()` and `update_project_channel()`.

**Affected files:** `src/discord/bot.py`

---

### Gap 2: Slash Commands Don't Auto-Infer Project from Channel (HIGH PRIORITY)

**Current state:** All slash commands that accept `project_id` require it to be explicitly typed, even when issued from a project's dedicated channel.

**Evidence:** Commands like `/tasks`, `/add-task`, `/pause`, `/resume`, `/status` all take `project_id` as a required or strongly expected parameter with no fallback logic.

**Impact:** Defeats the UX purpose of per-project channels. Users in `#my-app-control` must still type `project_id=my-app` on every command.

**Fix needed:** For each project-scoped slash command, add fallback:
1. If `project_id` explicitly provided → use it
2. Else if command issued in a per-project channel → infer from reverse lookup (Gap 1)
3. Else if `_active_project_id` is set → use it
4. Else → require explicit (or show all projects)

**Affected files:** `src/discord/commands.py`, `src/discord/bot.py`

---

### Gap 3: No Idempotent Channel Creation (MEDIUM PRIORITY)

**Current state:** `/create-channel` always calls `guild.create_text_channel()` — creates a new channel even if one with the same name already exists.

**Evidence:** `src/discord/commands.py` lines 420–424 — no check for existing channels before creation.

**Impact:** Running `/create-channel my-app` twice creates two `#my-app` channels. Automation and re-runs are not safe.

**Fix needed:** Before creating, scan `guild.text_channels` for an existing channel with the target name (optionally in the same category). If found, reuse it; if not, create.

**Affected files:** `src/discord/commands.py`

---

### Gap 4: No Automatic Channel Creation on Project Creation (MEDIUM PRIORITY)

**Current state:** `_cmd_create_project()` creates only the workspace directory and DB record. No Discord channels are created or linked.

**Evidence:** `src/command_handler.py` lines 150–162 — no channel creation logic.

**Impact:** Every new project requires manual channel setup via `/set-channel` or `/create-channel`. This friction discourages per-project channel usage.

**Fix needed:**
- Add optional `auto_channels` parameter to project creation
- Add config option `discord.auto_create_project_channels: true`
- Add config option `discord.project_channel_category: "Projects"` for channel placement
- Use idempotent channel creation logic (Gap 3)

**Affected files:** `src/command_handler.py`, `src/discord/commands.py`, `src/config.py`

---

### Gap 5: Setup Wizard Lacks Per-Project Channel Configuration (LOW PRIORITY)

**Current state:** The wizard configures only global channels. When it creates the first project, no channels are created for it.

**Evidence:** `setup_wizard.py` — project creation step has no channel options.

**Impact:** New users don't discover per-project channels during onboarding.

**Fix needed:** After the first project is created, add a step that:
1. Asks "Would you like dedicated Discord channels for this project?"
2. If yes, records channel names to create
3. Adds a post-setup instruction: "After starting the bot, run `/create-channel <project> <name>` to create project channels"

Note: Since the wizard runs before the bot starts, it cannot call the Discord API. Best approach is either (a) documenting post-start commands, or (b) adding a `/setup-channels` command that runs after bot startup.

**Affected files:** `setup_wizard.py`

---

### Gap 6: No Channel Name Resolution for LLM Tool (LOW PRIORITY)

**Current state:** The `set_project_channel` LLM tool requires a raw numeric `channel_id`. There is no tool to resolve a channel name (e.g., `#my-app-notifications`) to an ID.

**Evidence:** `src/chat_agent.py` lines 83–99 — `channel_id` is a string field with no name-based alternative.

**Impact:** In natural language conversations, the LLM cannot resolve channel names mentioned by users (e.g., "set notifications to #my-app"). The `/set-channel` slash command works because Discord's UI handles channel resolution.

**Fix needed:** Either:
- Add a `resolve_channel` LLM tool that takes a channel name and returns the ID
- Modify `set_project_channel` to accept `channel_name` as an alternative to `channel_id`, with backend resolution
- Include a channel name→ID map in the LLM context when relevant

**Affected files:** `src/chat_agent.py`, `src/command_handler.py`

---

## Summary Table

| # | Gap | Priority | Complexity | Affected Files |
|---|-----|----------|------------|----------------|
| 1 | No reverse channel→project lookup | High | Low | `bot.py` |
| 2 | Slash commands don't auto-infer project | High | Medium | `commands.py`, `bot.py` |
| 3 | No idempotent channel creation | Medium | Low | `commands.py` |
| 4 | No auto-channel creation on project creation | Medium | Medium | `commands.py`, `command_handler.py`, `config.py` |
| 5 | Setup wizard lacks per-project channel config | Low | Medium | `setup_wizard.py` |
| 6 | No channel name resolution for LLM | Low | Low | `chat_agent.py`, `command_handler.py` |

## Recommended Implementation Order

1. **Gap 1** (reverse lookup) — Foundational, only ~10 lines of code, required by Gap 2
2. **Gap 2** (auto-infer project from channel) — Highest user-facing impact, depends on Gap 1
3. **Gap 3** (idempotent creation) — Safety requirement, required by Gap 4
4. **Gap 4** (auto-create channels on project creation) — Major workflow improvement, depends on Gap 3
5. **Gap 6** (channel name resolution for LLM) — Quality-of-life improvement
6. **Gap 5** (setup wizard update) — Lowest priority, documentation may suffice

## What Does NOT Need Changing

The following components are fully functional and need no modifications for multi-channel support:

- **Database schema** — Both channel columns exist with correct types and migrations
- **Project model** — Both channel fields are properly typed and serialized
- **Bot channel routing** — Forward lookup, fallback, and notification routing are complete
- **Orchestrator callbacks** — All pass `project_id` correctly
- **Natural language context injection** — Works correctly for LLM-processed messages
- **Channel management commands** — `/set-channel` and `/create-channel` work end-to-end
- **LLM tools** — `set_project_channel` and `get_project_channels` are functional
- **Command handler backend** — All channel commands validate and persist correctly
- **System prompt documentation** — LLM is informed about per-project channels
