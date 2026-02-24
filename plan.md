# Executive Summary: Per-Project Discord Channel System

**Date:** 2026-02-24
**Status:** Independently verified and confirmed
**Source audits:** `eager-vault/background` (deep code inspection), `sharp-stone/verdict-confirmed` (independent re-verification)
**Scope:** All 34 source files in `src/`, `setup_wizard.py`, and 14 test files

---

## Verdict

The per-project Discord channel system in Agent Queue is **architecturally complete and production-ready** across all six core layers: storage, routing, orchestrator callbacks, Discord commands, command handler business logic, and LLM chat integration. Independent audit of every file confirms all material claims from the original assessment are accurate.

**Five targeted gaps** remain, all concentrated in the **UX and automation layers**. None require architectural refactoring — each can be closed with additive changes to existing code. Total estimated effort: **~5 hours**.

---

## System Architecture Overview

Agent Queue is a lightweight task queue and orchestrator (~8,260 LOC across 34 Python files) built for AI agents. It manages parallel task execution, token budgets, rate limits, and provides Discord-based remote control. The per-project channel system enables multi-team, multi-project management from a single Discord guild by routing notifications and control messages to project-specific channels.

### Data Flow

```
User/LLM Command → CommandHandler → Database (persist channel IDs)
                                   → Bot cache (hot-swap routing)

Orchestrator Event → _notify_channel(msg, project_id)
                   → Bot._get_notification_channel(project_id)
                   → Per-project channel (if configured) OR global fallback
```

---

## Confirmed-Complete Layers

### 1. Storage Layer — COMPLETE

**Files:** `src/database.py`, `src/models.py`

- `Project` dataclass carries `discord_channel_id: str | None` and `discord_control_channel_id: str | None`
- `projects` table schema includes both `TEXT` columns
- Idempotent `ALTER TABLE` migrations with `try/except` guards for safe re-runs
- Full CRUD: `create_project()` persists both IDs, `update_project()` supports partial updates, `_row_to_project()` handles missing columns gracefully
- `delete_project()` cascade removes tasks, repos, ledger entries, hooks, and hook runs
- Token ledger tracks usage per `project_id` for proportional scheduling

**No changes needed.**

### 2. Routing Layer — COMPLETE (with Global Fallback)

**File:** `src/discord/bot.py`

- Per-project channel caches: `_project_channels: dict[str, TextChannel]` and `_project_control_channels: dict[str, TextChannel]`
- `_resolve_project_channels()` iterates all projects at startup, resolves channel IDs via `guild.get_channel()`, logs warnings for missing channels
- Two-tier fallback routing:
  ```python
  def _get_notification_channel(self, project_id=None):
      if project_id and project_id in self._project_channels:
          return self._project_channels[project_id]
      return self._notifications_channel  # global fallback
  ```
- `update_project_channel()` hot-swaps in-memory cache at runtime (zero-restart updates)
- `_create_task_thread()` creates threads in the project-specific notifications channel
- Per-channel message history compaction with summary caching
- Notes thread persistence to `notes_threads.json` survives bot restarts

**No changes needed.**

### 3. Orchestrator Callbacks — COMPLETE

**File:** `src/orchestrator.py`

- `NotifyCallback` and `CreateThreadCallback` type signatures carry optional `project_id`
- `_notify_channel()` and `_control_channel_post()` forward `project_id` to bot
- **Exhaustive call-site audit:** Every notification path in the orchestrator passes `project_id` — verified at 14 distinct call sites covering task start, completion, failure, pause, rate limit, PR creation, plan parsing, and verification events
- Callback registration via `set_notify_callback()`, `set_control_callback()`, `set_create_thread_callback()`

**No changes needed. Zero omissions.**

### 4. Discord Commands — COMPLETE

**File:** `src/discord/commands.py`

- `/set-channel` — Links an existing Discord channel to a project. Native `discord.TextChannel` picker UI, updates DB + bot cache immediately
- `/create-channel` — Creates a new text channel and links it. Supports custom name, channel type (notifications/control), and category selection
- `/projects` — Inline display of channel assignments using Discord mention syntax
- ~30 total slash commands covering projects, tasks, agents, repos, hooks, and orchestrator control
- Long message handling: splits at line boundaries (>2000 chars), attaches as file (>6000 chars)

**Core commands complete. Gap 3 addresses a validation inefficiency in `/create-channel`.**

### 5. Command Handler Business Logic — COMPLETE

**File:** `src/command_handler.py`

- `_cmd_set_project_channel()` — Validates project exists, validates `channel_type` enum, persists to database
- `_cmd_get_project_channels()` — Returns both `notifications_channel_id` and `control_channel_id`
- `_cmd_list_projects()` — Includes both channel IDs in output when present
- `_cmd_create_project()` — Creates workspace directory and database record
- `_cmd_delete_project()` — Safety check for IN_PROGRESS tasks, then cascade delete

**No changes needed to core logic.**

### 6. LLM Chat Integration — COMPLETE

**File:** `src/chat_agent.py`

- `set_project_channel` tool definition (JSON schema) for LLM function calling
- `get_project_channels` tool definition for querying configured channels
- System prompt (lines 715-724) documents per-project channel management workflow
- ~40 total LLM tools covering all command categories

**File:** `src/discord/bot.py`

- Per-project control channel context injection: when a message arrives in a project control channel, the bot prepends `[Context: this is the control channel for project 'X']` before passing to the LLM
- Notes thread context injection follows the same pattern
- The LLM auto-scopes all tools to the correct project without the user specifying `project_id`

**No changes needed.**

### 7. Scheduling & Token Management — COMPLETE

**File:** `src/scheduler.py`

- Groups ready tasks by `project_id`
- Proportional distribution based on `credit_weight`
- Per-project concurrency limits (`max_concurrent_agents`)
- Per-project budget limits (`budget_limit`)
- Minimum task guarantee prevents project starvation
- Every `AssignAction` carries `project_id` for downstream routing

**No changes needed.**

---

## Five Targeted Gaps

All gaps are in the UX and automation layers. None affect routing correctness or system stability.

### Gap 1: Setup Wizard Ignores Per-Project Channels — MEDIUM priority

**File:** `setup_wizard.py` (lines 220-325)
**Current state:** Wizard configures only the three global channel names (control, notifications, agent-questions). Zero mentions of per-project channels.
**Impact:** New users never discover per-project isolation exists during onboarding.
**Fix:** Add optional post-setup step to create/link project channels, or display a hint about the feature.
**Effort:** ~2 hours

### Gap 2: Natural Language Parser Is Dead Code — LOW priority

**File:** `src/discord/nl_parser.py` (42 lines)
**Current state:** Contains a keyword-matching stub (`parse_natural_language()`) that is never imported anywhere in `src/`. The `NLParserConfig` in `src/config.py` is loaded but never consumed at runtime.
**Impact:** The LLM-based `ChatAgent` with context injection fully supersedes this stub. Dead code adds confusion.
**Fix:** Remove `nl_parser.py` and associated config. ~30 minutes.
**Effort:** ~30 minutes

### Gap 3: `/create-channel` Uses Inefficient Project Validation — HIGH priority (code quality)

**File:** `src/discord/commands.py` (lines 402-406)
**Current state:** Contains a vestigial no-op `get_task` call (line 403) and loads all projects to validate one (lines 404-406). A direct `get_project()` lookup exists and is used elsewhere.
**Impact:** Unnecessary database overhead and dead code in a user-facing command.
**Fix:** Replace `list_projects` + scan with direct `get_project()` lookup. Remove dead `get_task` call.
**Effort:** ~15 minutes

### Gap 4: No Channel Map / Overview Command — LOW priority

**Current state:** No dedicated `/channel-map` or `/channels` command exists.
**Nuance:** `/projects` (lines 191-209) already shows channel assignments inline, partially covering this need.
**Impact:** Users cannot get a quick bird's-eye view of channel-to-project mappings.
**Fix:** Add dedicated `/channel-map` command that shows all project-channel associations in a compact format.
**Effort:** ~1 hour

### Gap 5: No Channel Cleanup on Project Deletion or Channel Loss — MEDIUM priority

**File:** `src/command_handler.py` (lines 233-245), `src/discord/bot.py`
**Current state:** `_cmd_delete_project()` performs database cascade but does **not** clear the bot's in-memory channel caches (`_project_channels`, `_project_control_channels`). `_resolve_project_channels()` logs warnings for stale/deleted Discord channels but doesn't nullify the database entries.
**Impact:** Stale cache entries accumulate. Routing still works via global fallback — this is a hygiene issue, not a correctness bug.
**Fix:** Clear bot cache entries on project delete + nullify stale channel IDs during startup resolution.
**Effort:** ~1 hour

---

## Implementation Roadmap

### Phase 1 — Quick Wins (< 1 hour)
- **Gap 3:** Fix `/create-channel` project validation — direct lookup, remove dead code
- **Gap 2:** Remove dead `nl_parser.py` and unused config

### Phase 2 — Hygiene (< 2 hours)
- **Gap 5:** Channel cleanup — clear bot cache on project delete, nullify stale IDs at startup

### Phase 3 — UX Polish (< 2 hours)
- **Gap 1:** Setup wizard — add per-project channel guidance/hint
- **Gap 4:** Channel map command — compact overview of all mappings

### Dependency Graph

```
Gap 3 (validation fix) ──────── standalone
Gap 2 (dead code removal) ───── standalone
Gap 5 (cleanup) ─────────────── standalone
Gap 1 (setup wizard) ────────── standalone
Gap 4 (channel map) ─────────── standalone
```

All gaps are independent — they can be implemented in any order or in parallel.

---

## Additional Observations

These are not gaps but noteworthy items for future consideration:

1. **O(n) channel lookup in `on_message`:** The `_project_control_channels` linear scan (bot.py lines 333-337) works at current scale. A reverse-lookup dict (`channel_id -> project_id`) would help at scale and is trivial to add alongside Gap 5.

2. **LLM tool requires numeric channel IDs:** The `set_project_channel` tool requires raw numeric Discord IDs. Users refer to channels by name in NL conversations. A `resolve_channel_by_name` tool or accepting `channel_name` as an alternative would improve the NL experience.

3. **Thread creation failure handling:** Falls back to direct channel posting. Correct and robust — no changes needed.

4. **Notes threads:** Separate context injection path from control channels. Both work correctly with independent project scoping.

5. **Rate limit resilience:** In-process exponential backoff retry loop before pausing, with mandatory `resume_after` timers preventing deadlock. Fully operational.

---

## Conclusion

The per-project Discord channel system has solid, production-grade infrastructure across all core layers. The five identified gaps are real, accurately characterized, and independently addressable with additive changes. No architectural refactoring is required. Total estimated effort to close all gaps: **~5 hours** with zero risk to existing functionality.

**Recommended first action:** Gap 3 (15-minute fix) to clean up the most visible code quality issue.
