# What Already Exists (Validated): Per-Project Channel Infrastructure

**Validation date:** 2026-02-24
**Branch:** `eager-vault/background`
**Method:** Direct code inspection of every file touching channel, project, or routing logic
**Scope:** Full codebase audit confirming existing infrastructure for multi-channel support, automatic channel creation, channel-context-aware project resolution, and setup wizard support

---

## Executive Summary

The per-project Discord channel system is **architecturally complete and production-ready** at the storage, routing, orchestrator, and LLM integration layers. The full data flow --- project creation, channel assignment, per-project notification routing with global fallback, and channel-context-aware NL command scoping --- is operational.

Five **targeted gaps** remain, all concentrated in the **UX and automation** layers. No architectural refactoring is required; each gap can be closed with additive changes to existing code.

---

## Validated Component Inventory

### 1. Database Schema & Migrations

**File:** `src/database.py`

| What | Lines | Status |
|------|-------|--------|
| `projects.discord_channel_id TEXT` column | schema line 24 | Complete |
| `projects.discord_control_channel_id TEXT` column | schema line 25 | Complete |
| `ALTER TABLE projects ADD COLUMN discord_channel_id TEXT` migration | line 200 | Complete |
| `ALTER TABLE projects ADD COLUMN discord_control_channel_id TEXT` migration | line 201 | Complete |
| Error suppression for safe re-runs | lines 203-206 | Complete |
| `create_project()` inserts both channel IDs | lines 215-227 (specifically 224-225) | Complete |
| `get_project()` retrieves all columns | lines 229-236 | Complete |
| `list_projects()` retrieves all columns | lines 238-248 | Complete |
| `update_project()` supports arbitrary field updates including channels | lines 250-262 | Complete |
| `_row_to_project()` handles missing columns gracefully | lines 264-277 (specifically 275-276) | Complete |
| `delete_project()` cascading delete (tasks, repos, ledger, hooks, hook_runs) | lines 610-633 | Complete |

**Verdict:** Full CRUD for channel IDs with safe migrations. No changes needed.

---

### 2. Data Models

**File:** `src/models.py`

| What | Lines | Status |
|------|-------|--------|
| `Project.discord_channel_id: str \| None = None` | line 94 | Complete |
| `Project.discord_control_channel_id: str \| None = None` | line 95 | Complete |
| `ProjectStatus` enum (ACTIVE, PAUSED, ARCHIVED) | lines 55-58 | Complete |

Both fields are optional (`str | None`), storing Discord numeric IDs as strings. The Project dataclass also carries `workspace_path`, `credit_weight`, `max_concurrent_agents`, and `budget_limit` for full per-project isolation.

**Verdict:** Model is complete. No changes needed.

---

### 3. Bot Channel Routing & Caching

**File:** `src/discord/bot.py`

#### 3a. Runtime Caches (lines 31-33)

```python
self._project_channels: dict[str, discord.TextChannel] = {}         # project_id -> channel
self._project_control_channels: dict[str, discord.TextChannel] = {} # project_id -> channel
```

Both are forward-lookup caches: `project_id -> discord.TextChannel`. Populated on startup and updated at runtime.

#### 3b. Startup Resolution (lines 197-229)

`_resolve_project_channels()` iterates all projects from the database. For each project with a `discord_channel_id` or `discord_control_channel_id`, it looks up the `discord.TextChannel` by numeric ID via `guild.get_channel()` and caches it. Logs warnings for missing channels (e.g., channel was deleted from Discord).

Called from `on_ready()` at line 122, after global channels are resolved.

#### 3c. Two-Tier Fallback Routing (lines 231-245)

```python
def _get_notification_channel(self, project_id=None) -> discord.TextChannel | None:
    if project_id and project_id in self._project_channels:
        return self._project_channels[project_id]
    return self._notifications_channel

def _get_control_channel(self, project_id=None) -> discord.TextChannel | None:
    if project_id and project_id in self._project_control_channels:
        return self._project_control_channels[project_id]
    return self._control_channel
```

Used consistently by `_send_notification()` (line 249), `_send_control_message()` (line 255), and `_create_task_thread()` (line 274).

#### 3d. Runtime Cache Updates (lines 67-78)

`update_project_channel(project_id, channel, channel_type)` updates the appropriate cache immediately. Called after `/set-channel` and `/create-channel` commands so routing takes effect without restart.

#### 3e. Thread Creation (lines 259-307)

`_create_task_thread()` creates a Discord thread in the appropriate notification channel (project-specific or global) for streaming agent output. Returns `(send_to_thread, notify_main_channel)` callback pair.

**Verdict:** Routing is complete with two-tier fallback, runtime updates, and thread support. No changes needed.

---

### 4. Context Injection for NL Messages

**File:** `src/discord/bot.py`

#### 4a. Project Control Channel Detection (lines 332-338)

```python
project_control_id: str | None = None
for pid, ch in self._project_control_channels.items():
    if message.channel.id == ch.id:
        project_control_id = pid
        break
is_project_control = project_control_id is not None
```

Linear scan of `_project_control_channels` to find which project owns the channel. This works but is O(n) per message.

#### 4b. Context Prepending (lines 370-376)

```python
if is_project_control and not is_control:
    user_text = (
        f"[Context: this is the control channel for project "
        f"`{project_control_id}`. Default to using "
        f"project_id='{project_control_id}' for all project-scoped "
        f"commands.]\n{text}"
    )
```

When a message arrives in a project control channel, the bot prepends a context marker before passing to the LLM. This enables the LLM to auto-scope tools to the correct project without the user specifying `project_id`.

#### 4c. Notes Thread Context (lines 377-383)

Same pattern for notes threads --- uses `_notes_threads` mapping (thread_id -> project_id) to inject project context.

**Verdict:** NL context injection is complete for control channels and notes threads. The linear scan (4a) is a minor inefficiency that Gap 1 addresses.

---

### 5. Orchestrator Callback Architecture

**File:** `src/orchestrator.py`

#### 5a. Callback Type Signatures (lines 29-44)

```python
NotifyCallback = Callable[[str, str | None], Awaitable[None]]
# (message, optional_project_id)

CreateThreadCallback = Callable[
    [str, str, str | None],
    Awaitable[tuple[ThreadSendCallback, ThreadSendCallback] | None],
]
# (thread_name, initial_message, optional_project_id)
```

Both callbacks accept an optional `project_id` parameter for per-project routing.

#### 5b. Callback Registration (lines 72-82)

```python
def set_notify_callback(self, callback: NotifyCallback) -> None
def set_control_callback(self, callback: NotifyCallback) -> None
def set_create_thread_callback(self, callback: CreateThreadCallback) -> None
```

Registered by the bot in `on_ready()` (lines 107, 113, 119).

#### 5c. Dispatch Methods (lines 115-137)

```python
async def _notify_channel(self, message, project_id=None) -> None
async def _control_channel_post(self, message, project_id=None) -> None
```

Both delegate to their respective callbacks with `project_id`.

#### 5d. Usage Throughout Orchestrator

Every notification call passes `project_id` from the task/action context. Verified at:

| Line | Context |
|------|---------|
| 109-111 | Task stopped notification |
| 659 | Task started notification |
| 722 | Task result notification |
| 777 | Rate limit cleared notification |
| 804 | Task paused notification |
| 814 | Task blocked notification |
| 846 | Task completion control channel post |
| 855 | Task failure control channel post |
| 885 | PR created control channel post |
| 902 | Plan parsing control channel post |
| 961 | Verification control channel post |

**Verdict:** Every notification path carries `project_id`. No changes needed.

---

### 6. Slash Commands for Channel Management

**File:** `src/discord/commands.py`

#### 6a. `/set-channel` (lines 342-376)

Links an existing Discord channel to a project. Parameters:
- `project_id` (required) --- project to link to
- `channel` (required) --- Discord channel picker UI (native `discord.TextChannel` type)
- `channel_type` (optional) --- "notifications" or "control" (default: "notifications")

Calls `handler.execute("set_project_channel", {...})`, then `bot.update_project_channel()` for immediate cache update.

#### 6b. `/create-channel` (lines 378-452)

Creates a new Discord text channel and links it. Parameters:
- `project_id` (required)
- `channel_name` (optional) --- defaults to `project_id`
- `channel_type` (optional) --- "notifications" or "control" (default: "notifications")
- `category` (optional) --- Discord category to create in

Validates project exists, creates channel via `guild.create_text_channel()`, links via `handler.execute("set_project_channel", {...})`, updates bot cache.

**Verdict:** Both commands are fully functional. Gap 3 addresses idempotency of `/create-channel`.

---

### 7. Command Handler Backend

**File:** `src/command_handler.py`

#### 7a. `_cmd_set_project_channel()` (lines 197-219)

Validates project exists, validates `channel_type` is "notifications" or "control", persists to database via `update_project()`. Returns structured result with project_id, channel_id, channel_type, and status.

#### 7b. `_cmd_get_project_channels()` (lines 221-231)

Returns both `notifications_channel_id` and `control_channel_id` for a project.

#### 7c. Project Listings Include Channel IDs (lines 131-148)

`_cmd_list_projects()` includes `discord_channel_id` and `discord_control_channel_id` in output when present.

#### 7d. Project Creation Does NOT Create Channels (lines 150-162)

`_cmd_create_project()` creates workspace directory and database record but does not trigger Discord channel creation. This is Gap 4.

**Verdict:** Backend channel management is complete. Creation automation is Gap 4.

---

### 8. LLM Tool Integration

**File:** `src/chat_agent.py`

#### 8a. Tool Definitions (lines 76-111)

| Tool | Lines | Purpose |
|------|-------|---------|
| `set_project_channel` | 77-100 | Link channel to project. Params: project_id, channel_id, channel_type |
| `get_project_channels` | 101-111 | Query configured channels for a project |

#### 8b. System Prompt Guidance (lines 642-749)

- Line 673: Lists `set_project_channel` and `get_project_channels` in capability summary
- Lines 715-723: Dedicated section explaining per-project Discord channels:
  - Default behavior (shared global channels)
  - How to link channels (tool and slash commands)
  - Routing behavior when channels are configured
  - References to slash commands for interactive management

**Verdict:** LLM is fully aware of channel management capabilities. No changes needed.

---

### 9. Global Configuration

**File:** `src/config.py`

#### 9a. DiscordConfig (lines 10-19)

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

Global channel **names** (not IDs) are configured here. Per-project channels are stored in the database and managed at runtime.

#### 9b. No Config Fields for Auto-Creation

`DiscordConfig` has no `auto_create_project_channels` or `project_channel_category` fields. This is Gap 4's config requirement.

**Verdict:** Global config is appropriate for current architecture. Needs extension for Gap 4.

---

### 10. Setup Wizard

**File:** `setup_wizard.py`

#### 10a. Global Channel Configuration (lines 264-294)

The wizard collects channel names for the three global channels (control, notifications, agent_questions), tests connectivity, and allows channel name customization if channels aren't found.

#### 10b. No Per-Project Channel Guidance

The wizard generates `config.yaml` with global channel settings (lines 982-993) but provides no step or guidance for per-project channel setup. Users must discover the feature through documentation or slash commands.

**Verdict:** Setup wizard covers global channels only. Per-project guidance is Gap 5.

---

### 11. Scheduling & Token Tracking (Per-Project)

**File:** `src/scheduler.py`

The scheduler is fully project-aware:
- Groups ready tasks by `project_id` (lines 44-47)
- Sorts projects by credit-weight deficit and min-task-guarantee (lines 76-84)
- Enforces per-project concurrency limits (lines 96-98)
- Enforces per-project budget limits (lines 88-93)
- Every `AssignAction` carries `project_id` (lines 10-14)

**File:** `src/database.py`

Token ledger records include `project_id` (schema line 105). `get_project_token_usage()` aggregates tokens by project within a time window.

**Verdict:** Scheduling and token tracking are fully project-isolated. No changes needed.

---

## Identified Gaps

### Gap 1: No Reverse Channel-to-Project Lookup --- HIGH priority

**Location:** `src/discord/bot.py`
**Current state:** Forward lookup caches (`project_id -> channel`) exist at lines 32-33. No reverse mapping (`channel_id -> project_id`). The `on_message()` handler at lines 333-337 uses a linear scan as a workaround.
**Impact:** Slash commands in project channels cannot auto-detect project context. Minor O(n) performance cost per message.
**Required change:** Add `_channel_to_project: dict[int, str]` cache. Populate in `_resolve_project_channels()` and `update_project_channel()`. Replace linear scan in `on_message()`.
**Effort:** ~10 lines, no architectural change.

### Gap 2: Slash Commands Require Explicit `project_id` --- HIGH priority

**Location:** `src/discord/commands.py`
**Current state:** Every project-scoped slash command requires `project_id` as a parameter, even when issued in a project-specific channel where the context is obvious.
**Impact:** Defeats the primary UX benefit of per-project channels. Users must type `project_id=my-app` in `#my-app-control`.
**Required change:** Add `resolve_project_id(interaction, explicit_id)` helper implementing fallback chain: explicit param -> reverse channel lookup -> single-active-project -> require explicit. Apply to all project-scoped commands.
**Effort:** Medium. Touches many commands but the pattern is mechanical.
**Depends on:** Gap 1.

### Gap 3: `/create-channel` Is Not Idempotent --- MEDIUM priority

**Location:** `src/discord/commands.py` (lines 419-434)
**Current state:** `/create-channel` always calls `guild.create_text_channel()`. Running the command twice creates duplicate channels.
**Impact:** Re-runs and automation scripts create orphaned channels.
**Required change:** Before creating, scan `guild.text_channels` for an existing channel with the target name (optionally in the target category). If found, link it instead of creating.
**Effort:** ~15 lines.

### Gap 4: No Automatic Channel Creation on Project Creation --- MEDIUM priority

**Location:** `src/command_handler.py` (lines 150-162), `src/config.py`
**Current state:** `_cmd_create_project()` creates workspace and DB record but not Discord channels. Every project requires manual `/create-channel` or `/set-channel`.
**Impact:** Per-project channels are opt-in friction rather than automatic infrastructure.
**Required change:** Add `auto_create_project_channels: bool` and `project_channel_category: str` to `DiscordConfig`. When enabled, project creation triggers channel creation (notifications + control) in the specified category.
**Effort:** Medium. Requires plumbing Discord guild access into command handler or using a post-creation callback.
**Depends on:** Gap 3 (idempotent creation needed for safety).

### Gap 5: Setup Wizard Has No Per-Project Channel Guidance --- LOW priority

**Location:** `setup_wizard.py`
**Current state:** Wizard configures global channels (lines 264-294) but has no step for per-project channels.
**Impact:** New users don't discover the per-project channel feature during onboarding.
**Required change:** Add post-setup hint or dedicated wizard step. Could also be a `/setup-channels` convenience command.
**Effort:** Medium but low priority.

---

## Additional Observations

### Observation A: LLM Tool Requires Numeric Channel IDs

The `set_project_channel` tool (chat_agent.py lines 87-89) requires a raw numeric Discord channel ID. Users refer to channels by name (e.g., `#my-app`) in NL conversations. The LLM cannot resolve names to IDs. A `resolve_channel_by_name` tool or accepting `channel_name` as an alternative would improve the NL experience.

### Observation B: No Channel Cleanup on Project Deletion

`_cmd_delete_project()` (command_handler.py lines 233-245) removes the DB record and cascading entities but does not delete or unlink associated Discord channels. Orphaned channels accumulate.

### Observation C: Message History Compaction Is Channel-Scoped

The bot's `_build_message_history()` and `_get_or_create_summary()` methods (bot.py lines 416-506) are already channel-scoped. Per-project control channels naturally maintain separate conversation histories. This is correct behavior that requires no changes.

### Observation D: Notes Thread Persistence

Notes threads are persisted to `notes_threads.json` (bot.py lines 40-65) and survive bot restarts. This `thread_id -> project_id` mapping is functionally a reverse lookup and correctly provides project context for notes threads. The channel reverse lookup (Gap 1) should follow the same pattern.

---

## Dependency Graph

```
Gap 1 (reverse lookup) ---> Gap 2 (auto-infer project from channel)
Gap 3 (idempotent creation) ---> Gap 4 (auto-create on project creation)
Gap 5 (setup wizard) --- independent
```

## Recommended Implementation Order

```
Phase 1 --- Foundation:
  Gap 1: Reverse channel->project lookup         [~10 lines, HIGH priority]

Phase 2 --- UX:
  Gap 2: Auto-infer project from channel context  [medium, HIGH priority]
  Gap 3: Idempotent channel creation              [~15 lines, MEDIUM priority]

Phase 3 --- Automation:
  Gap 4: Auto-create channels on project creation  [medium, MEDIUM priority]

Phase 4 --- Polish:
  Gap 5: Setup wizard guidance                     [medium, LOW priority]
```

---

## Components Confirmed Complete (No Changes Needed)

- Database schema (`projects` table with `discord_channel_id`, `discord_control_channel_id`)
- Database migrations (safe `ALTER TABLE ADD COLUMN` with error suppression)
- `Project` dataclass field definitions
- Bot forward-lookup caches (`_project_channels`, `_project_control_channels`)
- `_resolve_project_channels()` startup logic
- Two-tier fallback routing (`_get_notification_channel`, `_get_control_channel`)
- `_send_notification()`, `_send_control_message()`, `_create_task_thread()`
- `update_project_channel()` runtime cache updater
- Orchestrator `NotifyCallback` / `CreateThreadCallback` type signatures
- All orchestrator notification dispatch calls (every one passes `project_id`)
- `/set-channel` slash command (links existing channel)
- `/create-channel` slash command (creates and links, needs idempotency)
- `_cmd_set_project_channel()` and `_cmd_get_project_channels()` backend
- LLM tool definitions (`set_project_channel`, `get_project_channels`)
- System prompt guidance for per-project channels
- NL context injection for project control channels
- NL context injection for notes threads
- Notes thread persistence (`notes_threads.json`)
- Scheduler project grouping, credit-weight scheduling, concurrency limits
- Token ledger per-project tracking
- Message history compaction (channel-scoped)
- Long message handling with file attachment fallback
