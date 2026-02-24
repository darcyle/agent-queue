# Verdict: CONFIRMED

## Background Context

**Verification date:** 2026-02-24
**Branch:** `sharp-stone/verdict-confirmed`
**Verifying:** `eager-vault/background` plan.md (Executive Summary)
**Method:** Independent direct code inspection of every file touching channel, project, or routing logic
**Auditor scope:** All 15 source files in `src/`, `setup_wizard.py`, and all test files enumerated

---

## Executive Summary

The original assessment in `plan.md` is **accurate in all material claims**. Independent audit of every file confirms:

1. The per-project Discord channel system is **architecturally complete and production-ready** at the storage, routing, orchestrator, and LLM integration layers.
2. All **five identified gaps** are real, correctly located, and accurately characterized.
3. **No additional gaps** were discovered beyond those already documented.

---

## Layer-by-Layer Verification Results

### Storage Layer — CONFIRMED Complete
- `src/models.py` L94-95: `Project` dataclass has both `discord_channel_id` and `discord_control_channel_id` fields
- `src/database.py` L24-26: Schema includes both `TEXT` columns on `projects` table
- `src/database.py` L200-201: Idempotent `ALTER TABLE` migrations with safe try/except guards
- `src/database.py` L216-227: `create_project()` persists both channel fields
- `src/database.py` L250-262: `update_project()` supports partial updates to channel IDs via `**kwargs`
- `src/database.py` L264-277: `_row_to_project()` reads both fields with safe key guards

**Finding:** Full lifecycle coverage. No storage gaps.

### Routing Layer — CONFIRMED Complete (with Global Fallback)
- `src/discord/bot.py` L31-33: Per-project channel caches as `dict[str, discord.TextChannel]`
- `src/discord/bot.py` L197-229: `_resolve_project_channels()` iterates all projects at startup, resolves channel IDs via `guild.get_channel()`
- `src/discord/bot.py` L231-245: `_get_notification_channel()` and `_get_control_channel()` — per-project cache first, global fallback
- `src/discord/bot.py` L67-78: `update_project_channel()` hot-swaps in-memory cache (zero-restart updates)
- `src/discord/bot.py` L259-307: `_create_task_thread()` routes via per-project channel

**Finding:** Routing correctness is solid. Global fallback ensures no notification is silently dropped.

### Orchestrator Layer — CONFIRMED Complete
- `src/orchestrator.py` L31: `NotifyCallback` signature carries `project_id`
- `src/orchestrator.py` L115-137: Both `_notify_channel` and `_control_channel_post` forward `project_id`
- **Exhaustive call-site audit:** Every notification in the orchestrator passes `project_id` — verified at L109-112, L233-236, L251-254, L659, L667, L722, L770-777, L804, L814, L846, L855, L885, L902, L949-961

**Finding:** Zero omissions. Every notification path carries `project_id`.

### Discord Command Layer — CONFIRMED Complete
- `/set-channel` (L342-376): Links existing channel, updates DB + bot cache immediately
- `/create-channel` (L378-452): Creates channel via `guild.create_text_channel()`, links and caches
- `/projects` (L191-209): Inline display of channel assignments using Discord mention syntax

### Command Handler (Business Logic) — CONFIRMED Complete
- `_cmd_set_project_channel` (L197-219): Validates project, validates `channel_type` enum, persists
- `_cmd_get_project_channels` (L221-231): Returns both channel IDs
- `_cmd_list_projects` (L131-148): Includes both channel IDs in output

### LLM Chat Integration — CONFIRMED Complete
- `src/chat_agent.py` L77-99: `set_project_channel` tool definition
- `src/chat_agent.py` L100-111: `get_project_channels` tool definition
- `src/discord/bot.py` L370-376: Per-project control channel context injection
- `src/chat_agent.py` L715-724: System prompt documents channel management workflow

**Finding:** The LLM can both manage channels (via tools) and automatically scope to the correct project (via context injection).

---

## Gap Verification

All five gaps independently verified against the codebase. All are in the **UX and automation** layers — none require architectural changes.

### Gap 1: Setup Wizard Ignores Per-Project Channels — CONFIRMED
- `setup_wizard.py` L220-325: Configures only three global channel names. Zero mentions of per-project channels.
- New users never discover per-project isolation exists.
- **Fix:** Optional post-setup step to create/link project channels (~50 lines).

### Gap 2: Natural Language Parser is Dead Code — CONFIRMED
- `src/discord/nl_parser.py`: 42-line file, never imported anywhere in `src/`.
- `NLParserConfig` in `src/config.py` L23-25 is loaded but never consumed at runtime.
- The LLM-based `ChatAgent` with context injection fully supersedes this stub.
- **Fix:** Remove dead code and config (~30 minutes).

### Gap 3: `/create-channel` Uses Inefficient Project Validation — CONFIRMED
- `src/discord/commands.py` L403: Vestigial no-op `get_task` call (dead code).
- L404-406: Loads all projects to validate one. Direct `get_project()` exists and is used elsewhere.
- **Fix:** Replace with direct project lookup (~15 minutes).

### Gap 4: No Channel Map / Overview Command — CONFIRMED (with nuance)
- No `/channel-map` or `/channels` command exists.
- However, `/projects` (L191-209) already shows channel assignments inline.
- **Priority downgraded from Medium to Low** — `/projects` partially covers this need.
- **Fix:** Dedicated `/channel-map` command (~1 hour).

### Gap 5: No Channel Cleanup on Project Deletion or Channel Loss — CONFIRMED
- `_cmd_delete_project` (L233-245): Database cascade removes channel IDs, but bot's in-memory caches are **not cleared**.
- `_resolve_project_channels` (L211-229): Stale channel IDs (deleted Discord channels) logged but not corrected.
- Routing still works via global fallback — hygiene issue, not correctness bug.
- **Fix:** Clear bot cache on delete + nullify stale IDs (~1 hour).

---

## Additional Observations (Not New Gaps)

1. **Channel context injection ordering:** `on_message` iterates `_project_control_channels.items()` — O(n) in projects. Fine at current scale; a reverse-lookup dict would help at scale.
2. **Thread creation failure handling:** Falls back to direct channel posting. Correct and robust.
3. **Notes threads:** Separate context injection path from control channels. Both work correctly.

---

## Revised Effort & Priority

| Gap | Effort | Risk | Priority |
|-----|--------|------|----------|
| Gap 3: `/create-channel` validation fix | ~15 min | None | **High** (code quality) |
| Gap 5: Channel cleanup on deletion | ~1 hour | Low | **Medium** |
| Gap 1: Setup wizard per-project channels | ~2 hours | Low | **Medium** |
| Gap 2: NL parser dead code removal | ~30 min | None | **Low** |
| Gap 4: Channel map overview command | ~1 hour | None | **Low** |
| **Total** | **~5 hours** | — | — |

Recommended order: Gap 3 -> Gap 5 -> Gap 1 -> Gap 2 -> Gap 4

---

## Conclusion

The original Executive Summary assessment is **independently confirmed as accurate**. The per-project Discord channel system has solid, production-grade infrastructure across all core layers (storage, routing, orchestrator, commands, LLM integration). All five identified gaps are real, correctly characterized, and independently addressable. No new gaps were discovered. Total estimated effort to close all gaps: ~5 hours with zero risk to existing functionality.
