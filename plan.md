# Background Context: Per-Project Channel System — Verified State

**Date:** 2026-02-24
**Status:** Independently verified and confirmed (see `sharp-stone/verdict-confirmed`)
**Source audits:** `eager-vault/background` (deep code inspection), `sharp-stone/verdict-confirmed` (independent re-verification)
**Scope:** All source files in `src/`, `setup_wizard.py`, and all test files

---

## Executive Summary

The per-project Discord channel system in Agent Queue is **architecturally complete and production-ready** across all core layers: storage, routing, orchestrator callbacks, Discord commands, command handler business logic, and LLM chat integration. Independent audit of every file confirms all material claims from the original assessment are accurate.

**Five targeted gaps** remain, all in the UX and automation layers. None require architectural refactoring — each can be closed with additive changes to existing code. Total estimated effort: ~5 hours.

---

## Architecture Overview

Agent Queue is a single-process asyncio orchestrator that manages Claude agents working on coding projects. It uses SQLite for persistence, Discord for remote control, and a deterministic scheduler (zero LLM calls for scheduling decisions).

### Key Architectural Patterns

1. **Unified Command Handler**: Single `CommandHandler.execute(name, args)` serves both Discord slash commands and LLM chat agent tools — zero business logic duplication.
2. **Callback-based notification routing**: Orchestrator uses typed callbacks (`NotifyCallback`, `CreateThreadCallback`) that accept optional `project_id` for per-project channel routing.
3. **Two-tier fallback**: All notification paths try project-specific channel first, then fall back to global channel. No notification is silently dropped.
4. **Runtime cache updates**: Bot maintains in-memory channel caches that can be hot-swapped without restart via `update_project_channel()`.

---

## Verified Component Inventory

### 1. Storage Layer — COMPLETE

| Component | Location | Status |
|-----------|----------|--------|
| Schema columns | `src/database.py` L24-25 (`discord_channel_id TEXT`, `discord_control_channel_id TEXT`) | ✅ |
| Idempotent migrations | `src/database.py` L200-201 (`ALTER TABLE` with try/except) | ✅ |
| `create_project()` persists both IDs | `src/database.py` L216-227 | ✅ |
| `update_project()` supports partial updates | `src/database.py` L250-262 | ✅ |
| `_row_to_project()` safe key guards | `src/database.py` L264-277 | ✅ |
| Cascading delete | `src/database.py` L610-633 | ✅ |

### 2. Data Models — COMPLETE

| Component | Location | Status |
|-----------|----------|--------|
| `Project.discord_channel_id: str \| None` | `src/models.py` L94 | ✅ |
| `Project.discord_control_channel_id: str \| None` | `src/models.py` L95 | ✅ |

### 3. Bot Channel Routing — COMPLETE

| Component | Location | Status |
|-----------|----------|--------|
| Forward-lookup caches | `src/discord/bot.py` L31-33 | ✅ |
| Startup resolution (`_resolve_project_channels`) | `src/discord/bot.py` L197-229 | ✅ |
| Two-tier fallback routing | `src/discord/bot.py` L231-245 | ✅ |
| Runtime cache updates | `src/discord/bot.py` L67-78 | ✅ |
| Per-project thread creation | `src/discord/bot.py` L259-307 | ✅ |
| Notes thread persistence | `src/discord/bot.py` L39-65 (`notes_threads.json`) | ✅ |

### 4. Orchestrator Callbacks — COMPLETE

| Component | Location | Status |
|-----------|----------|--------|
| `NotifyCallback` type (carries `project_id`) | `src/orchestrator.py` L31 | ✅ |
| `CreateThreadCallback` type (carries `project_id`) | `src/orchestrator.py` L41-44 | ✅ |
| `_notify_channel()` dispatches with `project_id` | `src/orchestrator.py` L115-137 | ✅ |
| **All** notification call sites pass `project_id` | L109-112, L233-236, L251-254, L659, L667, L722, L770-777, L804, L814, L846, L855, L885, L902, L949-961 | ✅ |

### 5. Discord Commands — COMPLETE

| Component | Location | Status |
|-----------|----------|--------|
| `/set-channel` (link existing channel) | `src/discord/commands.py` L342-376 | ✅ |
| `/create-channel` (create and link) | `src/discord/commands.py` L378-452 | ✅ |
| `/projects` shows channel assignments | `src/discord/commands.py` L191-209 | ✅ |

### 6. Command Handler Backend — COMPLETE

| Component | Location | Status |
|-----------|----------|--------|
| `_cmd_set_project_channel()` | `src/command_handler.py` L197-219 | ✅ |
| `_cmd_get_project_channels()` | `src/command_handler.py` L221-231 | ✅ |
| `_cmd_list_projects()` includes channel IDs | `src/command_handler.py` L131-148 | ✅ |

### 7. LLM Chat Integration — COMPLETE

| Component | Location | Status |
|-----------|----------|--------|
| `set_project_channel` tool definition | `src/chat_agent.py` L77-99 | ✅ |
| `get_project_channels` tool definition | `src/chat_agent.py` L100-111 | ✅ |
| System prompt documents channel management | `src/chat_agent.py` L715-724 | ✅ |
| Control channel context injection | `src/discord/bot.py` L370-376 | ✅ |
| Notes thread context injection | `src/discord/bot.py` L377-383 | ✅ |

### 8. Scheduling & Tokens — COMPLETE (Per-Project Isolated)

| Component | Location | Status |
|-----------|----------|--------|
| Tasks grouped by `project_id` | `src/scheduler.py` L44-47 | ✅ |
| Credit-weight proportional allocation | `src/scheduler.py` L76-84 | ✅ |
| Per-project concurrency limits | `src/scheduler.py` L96-98 | ✅ |
| Per-project budget enforcement | `src/scheduler.py` L88-93 | ✅ |
| Token ledger per-project tracking | `src/database.py` L105 | ✅ |

---

## Five Confirmed Gaps

All gaps are in UX/automation layers. None affect core correctness.

### Gap 1: No Reverse Channel-to-Project Lookup — HIGH Priority

**Location:** `src/discord/bot.py`
**Problem:** Only forward caches exist (`project_id → channel`). The `on_message()` handler at L333-337 uses O(n) linear scan as workaround.
**Impact:** Slash commands in project channels can't auto-detect project context. Minor perf cost per message.
**Fix:** Add `_channel_to_project: dict[int, str]` cache. Populate in `_resolve_project_channels()` and `update_project_channel()`. ~10 lines.

### Gap 2: Slash Commands Require Explicit `project_id` — HIGH Priority

**Location:** `src/discord/commands.py`
**Problem:** Every project-scoped command requires `project_id` param, even when issued in a project-specific channel.
**Impact:** Defeats primary UX benefit of per-project channels.
**Fix:** Add `resolve_project_id(interaction, explicit_id)` helper with fallback chain: explicit param → reverse channel lookup → single-active-project → require explicit. Apply to all project-scoped commands.
**Depends on:** Gap 1.

### Gap 3: `/create-channel` Not Idempotent — MEDIUM Priority

**Location:** `src/discord/commands.py` L419-434
**Problem:** Always creates new channel. Running twice creates duplicates.
**Impact:** Re-runs and automation create orphaned channels.
**Fix:** Check existing channels by name before creating. ~15 lines.

### Gap 4: No Automatic Channel Creation on Project Creation — MEDIUM Priority

**Location:** `src/command_handler.py` L150-162, `src/config.py`
**Problem:** `_cmd_create_project()` creates workspace and DB record but not Discord channels.
**Impact:** Per-project channels are opt-in friction rather than automatic.
**Fix:** Add `auto_create_project_channels` config option. When enabled, trigger channel creation on project creation.
**Depends on:** Gap 3 (needs idempotent creation for safety).

### Gap 5: Setup Wizard Has No Per-Project Channel Guidance — LOW Priority

**Location:** `setup_wizard.py` L264-294
**Problem:** Wizard configures global channels only. No mention of per-project channels.
**Impact:** New users don't discover the feature during onboarding.
**Fix:** Add post-setup hint or dedicated wizard step. ~50 lines.

---

## Gap Dependency Graph

```
Gap 1 (reverse lookup) ──→ Gap 2 (auto-infer project from channel)
Gap 3 (idempotent creation) ──→ Gap 4 (auto-create on project creation)
Gap 5 (setup wizard) ── independent
```

## Recommended Implementation Order

| Phase | Gap | Effort | Priority |
|-------|-----|--------|----------|
| 1 — Foundation | Gap 1: Reverse channel→project lookup | ~10 lines | **HIGH** |
| 2 — UX | Gap 2: Auto-infer project from channel context | Medium | **HIGH** |
| 2 — UX | Gap 3: Idempotent channel creation | ~15 lines | **MEDIUM** |
| 3 — Automation | Gap 4: Auto-create channels on project creation | Medium | **MEDIUM** |
| 4 — Polish | Gap 5: Setup wizard guidance | Medium | **LOW** |
| **Total** | | **~5 hours** | |

Recommended sequence: Gap 1 → Gap 2 → Gap 3 → Gap 4 → Gap 5

---

## Additional Observations (Not Gaps)

1. **Channel context injection O(n) scan** (`bot.py` L333-337): `on_message()` iterates `_project_control_channels.items()` per message. Fine at current scale; reverse lookup (Gap 1) resolves this.
2. **Thread creation failure handling**: Falls back to direct channel posting. Correct and robust.
3. **Message history compaction**: Already channel-scoped — per-project control channels maintain separate conversation histories. Correct behavior.
4. **LLM `set_project_channel` tool requires numeric ID**: Users refer to channels by name in NL. A `resolve_channel_by_name` tool would improve NL experience (enhancement, not gap).
5. **No channel cleanup in bot cache on project deletion**: `_cmd_delete_project()` cascades in DB but doesn't clear bot's in-memory caches. Routing still works via global fallback — hygiene issue, not correctness bug.
6. **Natural language parser (`src/discord/nl_parser.py`) is dead code**: 42-line file, never imported. `NLParserConfig` in config loaded but never consumed. The LLM-based `ChatAgent` fully supersedes this stub.

---

## File Reference Quick Index

| File | Lines | Primary Role |
|------|-------|-------------|
| `src/models.py` | 186 | `Project`, `Task`, `Agent` dataclasses; `TaskStatus`, `AgentState` enums |
| `src/database.py` | 827 | SQLite schema, CRUD, migrations, token ledger |
| `src/state_machine.py` | 77 | Deterministic task state transitions |
| `src/scheduler.py` | 119 | Proportional credit-weight scheduling |
| `src/event_bus.py` | 25 | Async pub/sub for event-driven hooks |
| `src/orchestrator.py` | 985 | Main event loop, task execution, notification dispatch |
| `src/command_handler.py` | 1037 | Unified command execution (Discord + LLM) |
| `src/chat_agent.py` | 908 | LLM conversation, tool dispatch, system prompt |
| `src/hooks.py` | 505 | Hook engine: periodic/event triggers, context gathering, LLM execution |
| `src/plan_parser.py` | 249 | Parse `.claude/plan.md` into subtasks |
| `src/task_names.py` | 44 | Adjective-noun task ID generation |
| `src/config.py` | 231 | YAML config loading, `AppConfig` dataclass |
| `src/main.py` | 82 | Entry point |
| `src/discord/bot.py` | ~500 | Discord bot, channel routing, thread management |
| `src/discord/commands.py` | ~500 | Slash command definitions |
| `src/discord/notifications.py` | ~150 | Task result formatting |
| `src/discord/nl_parser.py` | 42 | Dead code (NL parser stub) |
| `src/adapters/claude.py` | ~200 | Claude SDK agent adapter |
| `src/adapters/base.py` | ~30 | Abstract agent adapter interface |
| `src/git/manager.py` | ~300 | Git operations (clone, branch, worktree, PR) |
| `src/tokens/budget.py` | ~50 | Global token budget tracking |
| `src/tokens/tracker.py` | ~80 | Per-project token ledger |
| `setup_wizard.py` | ~1000 | Interactive setup (global channels only) |

---

## Conclusion

The per-project Discord channel system has solid, production-grade infrastructure across all core layers. The five identified gaps are real, correctly characterized, independently addressable, and carry zero risk to existing functionality. No new gaps were discovered during independent verification.
