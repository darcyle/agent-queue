# Executive Summary: Per-Project Discord Channel Infrastructure

**Audit date:** 2026-02-24
**Branch:** `amber-current/executive-summary`
**Scope:** Full codebase audit of multi-channel support across all layers

---

## Overall Assessment

The per-project Discord channel system is **production-ready** at the storage, routing, and LLM integration layers. The full data flow — project creation, channel assignment, notification routing with global fallback — is operational and deployed. Five tactical gaps remain, concentrated entirely in the **UX and automation** layers. No architectural changes are required; all gaps can be closed with targeted additions to existing code.

**Maturity breakdown:**

| Layer | Readiness | Notes |
|-------|-----------|-------|
| Database & Models | **Complete** | Schema, migrations, CRUD all in place |
| Bot Routing & Dispatch | **Complete** | Two-tier fallback, runtime cache updates, thread creation |
| Orchestrator Callbacks | **Complete** | All notification paths carry `project_id` |
| LLM Integration | **Complete** | Tools exposed, system prompts documented |
| Manual Channel Management | **Complete** | `/set-channel` and `/create-channel` work end-to-end |
| Automatic Context Injection | **Complete** | Project control channels auto-inject `[Context: ...]` |
| Auto-detection & Automation | **Gaps remain** | 5 items below |

---

## Validated Infrastructure (What's Working)

### 1. Database & Persistence (`src/database.py`, `src/models.py`)

The `projects` table includes `discord_channel_id` and `discord_control_channel_id` columns (schema lines 24-25). Migrations add these columns safely to existing databases (lines 200-201). The `Project` dataclass mirrors both fields as `str | None` (models.py lines 94-95). All CRUD operations — `create_project()`, `update_project()`, `list_projects()`, `get_project()` — handle channel IDs correctly.

### 2. Bot Channel Routing (`src/discord/bot.py`)

The bot maintains two runtime caches (`_project_channels`, `_project_control_channels`) mapping `project_id -> discord.TextChannel` (lines 32-33). On startup, `_resolve_project_channels()` (lines 197-229) queries the database and populates both caches, logging warnings for missing channels.

Two-tier fallback routing is implemented via `_get_notification_channel()` and `_get_control_channel()` (lines 231-245): project-specific channel if available, global channel otherwise. This pattern is used consistently across `_send_notification()`, `_send_control_message()`, and `_create_task_thread()`.

Runtime updates are supported via `update_project_channel()` (lines 67-78), called immediately after `/set-channel` or `/create-channel` — no bot restart needed.

### 3. Orchestrator Callback Architecture (`src/orchestrator.py`)

The `NotifyCallback` type signature (line 31) accepts `(str, str | None)` — message text plus optional `project_id`. The `CreateThreadCallback` type (lines 41-44) follows the same pattern. All notification dispatches throughout the orchestrator pass `project_id` from the task or action being processed (verified at lines 111, 235, 722, 772, 777).

### 4. LLM Tool Integration (`src/chat_agent.py`)

Two tools are exposed to the LLM: `set_project_channel` (for linking channels) and `get_project_channels` (for querying channel config). System prompts reference both tools with usage guidance (lines 673, 723).

### 5. Slash Commands (`src/discord/commands.py`)

- **`/set-channel`** (lines 342-376): Links an existing Discord channel to a project. Uses Discord's native channel picker UI. Updates bot cache immediately.
- **`/create-channel`** (lines 378-452): Creates a new Discord text channel and links it. Supports optional category placement and custom naming.

### 6. Command Handler Backend (`src/command_handler.py`)

`_cmd_set_project_channel()` (lines 197-219) validates project existence and channel type, then persists to the database. `_cmd_get_project_channels()` (lines 221-231) returns both channel IDs for a project. Project listings include channel IDs when present (lines 131-148).

### 7. Context Injection for NL Messages (`src/discord/bot.py`)

The `on_message()` handler detects when a message arrives in a project control channel (lines 333-338) and prepends `[Context: this is the control channel for project 'X'...]` (lines 370-376). This enables the LLM to auto-scope commands without the user specifying `project_id`. The same pattern applies to notes threads (lines 377-383).

### 8. Configuration (`src/config.py`)

`DiscordConfig` (lines 11-19) holds global channel name mappings (`control`, `notifications`, `agent_questions`). Per-project channels are managed via runtime commands + DB, keeping config simple. This is appropriate for the current architecture.

---

## Identified Gaps

### Gap 1: No Reverse Channel-to-Project Lookup — **HIGH priority**

**Location:** `src/discord/bot.py`
**Problem:** Forward lookup (project_id -> channel) exists but there is no channel_id -> project_id reverse mapping. The `on_message()` handler uses a linear scan of `_project_control_channels.items()` (lines 333-337) as a workaround.
**Impact:** Slash commands executed in a project's channel cannot auto-detect which project the user intends. Also a minor O(n) performance issue per inbound message.
**Fix:** Add `_channel_to_project: dict[int, str]` populated in `_resolve_project_channels()` and `update_project_channel()`. Replace the linear scan in `on_message()`.
**Effort:** ~10 lines, no architectural change.

### Gap 2: Slash Commands Require Explicit `project_id` — **HIGH priority**

**Location:** `src/discord/commands.py`
**Problem:** Every project-scoped slash command (`/add-task`, `/pause`, `/resume`, `/set-channel`, etc.) requires the user to type `project_id` explicitly, even when the command is issued in a project-specific channel where the answer is obvious.
**Impact:** Defeats the primary UX benefit of per-project channels. Users must type `project_id=my-app` in `#my-app-control`.
**Fix:** Add a `resolve_project_id(interaction, explicit_id)` helper that implements the fallback chain: explicit param -> reverse channel lookup -> single-active-project -> require explicit. Apply to all project-scoped commands.
**Effort:** Medium. Touches many commands but the pattern is mechanical.
**Depends on:** Gap 1.

### Gap 3: `/create-channel` Is Not Idempotent — **MEDIUM priority**

**Location:** `src/discord/commands.py` (lines 419-434)
**Problem:** `/create-channel` always calls `guild.create_text_channel()`. Running the same command twice creates duplicate channels.
**Impact:** Automation scripts and re-runs are unsafe. Users who accidentally run the command twice get orphaned channels.
**Fix:** Before creating, scan `guild.text_channels` for an existing channel with the target name (optionally in the target category). If found, link it instead of creating a new one.
**Effort:** Low, ~15 lines.

### Gap 4: No Automatic Channel Creation on Project Creation — **MEDIUM priority**

**Location:** `src/command_handler.py`, `src/config.py`
**Problem:** `_cmd_create_project()` creates the workspace directory and database record but does not create Discord channels. Every new project requires a manual `/create-channel` or `/set-channel` step.
**Impact:** Per-project channels are opt-in friction rather than automatic infrastructure. New projects route all notifications to the global channel until manually configured.
**Fix:** Add `auto_create_project_channels` boolean and `project_channel_category` string to `DiscordConfig`. When enabled, project creation triggers channel creation (both notifications and control) in the specified category.
**Effort:** Medium. Requires plumbing Discord guild access into the command handler or using a post-creation hook.
**Depends on:** Gap 3 (idempotent creation needed for safety).

### Gap 5: Setup Wizard Has No Per-Project Channel Guidance — **LOW priority**

**Location:** `setup_wizard.py`
**Problem:** The setup wizard configures global channels (control, notifications, agent-questions) but provides no guidance or setup step for per-project channels.
**Impact:** New users don't discover the per-project channel feature during onboarding. The feature exists but is invisible until users read documentation or discover the slash commands.
**Fix:** Add a post-setup hint, a dedicated wizard step after first project creation, or a `/setup-channels` convenience command.
**Effort:** Medium, but low priority — documentation may suffice.

---

## Additional Observations

### Observation A: LLM Tool Requires Numeric Channel IDs

The `set_project_channel` LLM tool requires a raw numeric Discord channel ID. In natural language conversations, users refer to channels by name (e.g., `#my-app-notifications`), which the LLM cannot resolve to an ID. The `/set-channel` slash command works around this via Discord's native channel picker. A `resolve_channel_by_name` tool or accepting `channel_name` as an alternative parameter would improve the NL experience.

### Observation B: No Channel Cleanup on Project Deletion

`_cmd_delete_project()` (command_handler.py line 233+) removes the database record and checks for running tasks, but does not delete or unlink the associated Discord channels. Orphaned channels accumulate over time.

### Observation C: No Config Options for Channel Automation

`DiscordConfig` has no fields for `auto_create_project_channels` or `project_channel_category`. Per-project channels are entirely runtime-managed. This is fine for the current architecture but would need to change for Gap 4.

---

## Recommended Implementation Order

```
Phase 1 — Foundation (unblocks everything):
  Gap 1: Reverse channel->project lookup          [~10 lines, HIGH]

Phase 2 — UX (highest user-facing impact):
  Gap 2: Auto-infer project from channel context  [medium, HIGH]
  Gap 3: Idempotent channel creation              [~15 lines, MEDIUM]

Phase 3 — Automation:
  Gap 4: Auto-create channels on project creation [medium, MEDIUM]

Phase 4 — Polish:
  Gap 5: Setup wizard guidance                    [medium, LOW]
```

**Dependency graph:**
```
Gap 1 --> Gap 2   (reverse lookup enables auto-inference)
Gap 3 --> Gap 4   (idempotent creation enables safe automation)
Gap 5              (independent)
```

---

## Components That Need No Changes

These layers are **complete and correct** — no modifications required:

- Database schema (`projects` table, `discord_channel_id`, `discord_control_channel_id`)
- Database migrations (safe `ALTER TABLE ADD COLUMN` with error suppression)
- `Project` dataclass and field definitions
- Bot forward-lookup caches and two-tier fallback routing
- `_resolve_project_channels()` startup logic
- Orchestrator `NotifyCallback` / `CreateThreadCallback` type signatures
- All orchestrator notification dispatch calls (pass `project_id` correctly)
- `_send_notification()`, `_send_control_message()`, `_create_task_thread()`
- `update_project_channel()` runtime cache updater
- `/set-channel` and `/create-channel` slash command implementations
- `_cmd_set_project_channel()` and `_cmd_get_project_channels()` backend
- LLM tool definitions and system prompt guidance
- Natural language context injection for project control channels and notes threads
- Notes thread registration and persistence

---

## Conclusion

The per-project Discord channel system is architecturally sound and operationally complete for **manual management** workflows. The five identified gaps are all additive — they extend the system with automation and UX polish without requiring changes to existing working code. The recommended implementation order minimizes risk by building foundational capabilities first (reverse lookup) before layering on higher-level features (auto-inference, auto-creation).

Total estimated effort: **~2-3 focused implementation sessions** for all five gaps.
