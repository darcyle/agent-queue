# Independent Verification: Per-Project Discord Channel Infrastructure

**Verification date:** 2026-02-24
**Branch:** `sharp-zenith/verdict`
**Verifying:** `eager-vault/background` plan.md (Executive Summary)
**Method:** Independent direct code inspection of every file touching channel, project, or routing logic
**Auditor scope:** All 15 source files in `src/`, `setup_wizard.py`, and all test files enumerated

---

## Verdict: CONFIRMED

The original assessment is **accurate in all material claims**. My independent audit of every file confirms:

1. The per-project Discord channel system is **architecturally complete and production-ready** at the storage, routing, orchestrator, and LLM integration layers.
2. All **five identified gaps** are real, correctly located, and accurately characterized.
3. **No additional gaps** were discovered beyond those already documented.

---

## Layer-by-Layer Verification

### 1. Storage Layer — CONFIRMED Complete

**Verified claims:**
- `src/models.py` L94–95: `Project` dataclass has `discord_channel_id: str | None` and `discord_control_channel_id: str | None` — **confirmed**.
- `src/database.py` L24–26: Schema includes both `TEXT` columns on `projects` table — **confirmed**.
- `src/database.py` L200–201: Idempotent `ALTER TABLE` migrations for both columns — **confirmed**. The try/except pattern at L202–206 silently ignores "column already exists" errors, making migrations safe to re-run.
- `src/database.py` L216–227: `create_project()` persists both channel fields — **confirmed** (parameters at positions 9 and 10 in the INSERT).
- `src/database.py` L250–262: `update_project()` uses generic `**kwargs` pattern supporting partial updates to any field including channel IDs — **confirmed**.
- `src/database.py` L264–277: `_row_to_project()` reads both channel fields with safe `"key" in keys` guards — **confirmed**.

**Finding:** Full lifecycle coverage. No storage gaps.

### 2. Routing Layer — CONFIRMED Complete (with Global Fallback)

**Verified claims:**
- `src/discord/bot.py` L31–33: Per-project channel caches declared as `dict[str, discord.TextChannel]` — **confirmed**.
- `src/discord/bot.py` L197–229: `_resolve_project_channels()` iterates all projects at startup, resolves channel IDs to `discord.TextChannel` objects via `guild.get_channel()` — **confirmed**.
- `src/discord/bot.py` L231–245: `_get_notification_channel(project_id)` and `_get_control_channel(project_id)` check per-project cache first, fall back to global — **confirmed**. The fallback is clean: `if project_id and project_id in self._project_channels: return ...; return self._notifications_channel`.
- `src/discord/bot.py` L67–78: `update_project_channel()` hot-swaps the in-memory cache after `/set-channel` or `/create-channel` — **confirmed**.
- `src/discord/bot.py` L259–307: `_create_task_thread()` routes via `_get_notification_channel(project_id)`, so threads land in the correct per-project channel — **confirmed**.

**Finding:** Routing correctness is solid. Global fallback ensures no notification is silently dropped.

### 3. Orchestrator Layer — CONFIRMED Complete

**Verified claims:**
- `src/orchestrator.py` L31: `NotifyCallback = Callable[[str, str | None], Awaitable[None]]` — second argument is `project_id` — **confirmed**.
- `src/orchestrator.py` L115–127: `_notify_channel(message, project_id)` — **confirmed**.
- `src/orchestrator.py` L128–137: `_control_channel_post(message, project_id)` — **confirmed**.

**Exhaustive call-site audit:** Every notification in the orchestrator passes `project_id`:
- L109–112: `stop_task` — passes `task.project_id` — **confirmed**.
- L233–236: `_execute_task_safe` timeout — passes `action.project_id` — **confirmed**.
- L251–254: `_execute_task_safe` error — passes `action.project_id` — **confirmed**.
- L659: task start notification — passes `action.project_id` — **confirmed**.
- L667: thread creation — passes `action.project_id` — **confirmed**.
- L722: fallback message stream — passes `action.project_id` — **confirmed**.
- L770–777: rate limit notices — passes `action.project_id` — **confirmed**.
- L804, L814: `_post` and `_notify_brief` helpers — use `action.project_id` in fallback path — **confirmed**.
- L846, L855, L885, L902: completion, approval, control-channel posts — all pass `project_id` — **confirmed**.
- L949–961: failure notifications — all pass `project_id` — **confirmed**.

**Finding:** Zero omissions. Every notification path carries `project_id`.

### 4. Discord Command Layer — CONFIRMED Complete

**Verified commands:**
- `/set-channel` (L342–376): Links existing channel, calls `handler.execute("set_project_channel", ...)`, then `bot.update_project_channel()` for immediate cache update — **confirmed**.
- `/create-channel` (L378–452): Creates channel via `guild.create_text_channel()`, links via handler, updates cache — **confirmed**.
- `/projects` (L191–209): Inline display of channel assignments using `<#{channel_id}>` Discord mention syntax — **confirmed**.

**Finding:** Both channel management commands work correctly and update the in-memory cache immediately.

### 5. Command Handler (Business Logic) — CONFIRMED Complete

**Verified methods:**
- `_cmd_set_project_channel` (L197–219): Validates project exists, validates `channel_type` enum, persists to DB — **confirmed**.
- `_cmd_get_project_channels` (L221–231): Returns both channel IDs — **confirmed**.
- `_cmd_list_projects` (L131–148): Includes `discord_channel_id` and `discord_control_channel_id` in output — **confirmed**.

**Finding:** Clean implementation. Both methods correctly wired to slash commands and LLM tools.

### 6. LLM Chat Integration — CONFIRMED Complete

**Verified components:**
- `src/chat_agent.py` L77–99: `set_project_channel` tool definition with `project_id`, `channel_id`, `channel_type` params — **confirmed**.
- `src/chat_agent.py` L100–111: `get_project_channels` tool definition — **confirmed**.
- `src/discord/bot.py` L370–376: Per-project control channel context injection: `"[Context: this is the control channel for project '{project_control_id}'...]\n{text}"` — **confirmed**.
- `src/discord/bot.py` L377–383: Notes thread context injection — **confirmed** (separate but analogous pattern).
- `src/chat_agent.py` L715–724: System prompt documenting per-project channel features — **confirmed**.

**Finding:** The LLM can both manage channels (via tools) and automatically scope to the correct project (via context injection). The system prompt explicitly documents the channel management workflow.

---

## Gap Verification

All gaps are in the **UX and automation** layers. Each independently verified against the codebase.

### Gap 1: Setup Wizard Ignores Per-Project Channels — CONFIRMED

**Independently verified:**
- `setup_wizard.py` L220–325 (`step_discord()`): Configures only three global channel names (`control`, `notifications`, `agent_questions`). Grep for "per.project|project.*channel" in `setup_wizard.py` returns zero matches.
- No mention of `discord_channel_id` or per-project channel creation anywhere in the wizard.
- The wizard does not import or interact with the database after initial configuration, so it cannot discover existing projects to offer channel linking.

**Assessment:** Gap is real. Impact is correctly characterized — new users never discover per-project isolation. The proposed fix (optional post-setup step) is appropriate.

### Gap 2: Natural Language Parser is Dead Code — CONFIRMED

**Independently verified:**
- `src/discord/nl_parser.py`: 42-line file defining `ParsedCommand` dataclass and `parse_natural_language()` function.
- Grep for `import.*nl_parser|from.*nl_parser` across entire `src/` tree: **zero results** outside the file itself.
- `NLParserConfig` is defined in `src/config.py` L23–25 and loaded into `AppConfig` at L87, but `config.nl_parser` is never read by any runtime code.
- The `bot.py` `on_message` handler delegates entirely to the `ChatAgent` LLM — it has no NL parsing step.

**Assessment:** Gap is real. The NL parser is completely dead code. The LLM-based `ChatAgent` with context injection (L370–383) fully supersedes this stub. Recommendation: remove the dead code and its config.

### Gap 3: `/create-channel` Uses Inefficient Project Validation — CONFIRMED

**Independently verified:**
- `src/discord/commands.py` L403: `handler.execute("get_task", {"task_id": "__noop__"})` — vestigial no-op that returns an error (task not found) and the result is never used. This is dead code.
- L404–406: Calls `handler.execute("list_projects", {})` and scans the full list. The `_cmd_set_project_channel` at `command_handler.py` L200 already uses the direct `get_project(pid)` pattern.

**Assessment:** Gap is real but low-impact. The fix is straightforward — use direct project lookup.

### Gap 4: No Channel Map / Overview Command — CONFIRMED (with nuance)

**Independently verified:**
- No `/channel-map` or `/channels` command exists.
- The `/projects` command (L191–209) does show channel assignments inline using `<#{channel_id}>` syntax. This partially addresses the discoverability need.
- The `get_project_channels` LLM tool only works for one project at a time.

**Nuanced assessment:** The gap is real but less severe than originally characterized. The `/projects` command already provides a channel overview as part of its output. A dedicated `/channel-map` command would be a convenience improvement, not a critical missing feature. **Priority downgraded from Medium to Low.**

### Gap 5: No Channel Cleanup on Project Deletion or Channel Loss — CONFIRMED

**Independently verified:**
- `src/command_handler.py` L233–245 (`_cmd_delete_project`): Calls `self.db.delete_project(pid)` which cascade-deletes the project row (and thus the channel ID columns). However, the bot's in-memory caches (`_project_channels` and `_project_control_channels`) are **not cleared**. The stale cache entries remain until bot restart.
- `src/discord/bot.py` L211–229 (`_resolve_project_channels`): When `guild.get_channel()` returns `None` (channel was deleted from Discord), the code logs a warning but takes no corrective action — the stale channel ID stays in the database.
- `src/database.py` L610–633 (`delete_project`): The cascading delete removes the project row and all associated data, which does remove the channel IDs from the database. But the in-memory cache is not notified.

**Assessment:** Gap is real. The database-side cleanup is actually fine for project deletion (cascade handles it), but the **in-memory bot cache is the real issue**. For channel-deletion detection, the assessment is correct — stale IDs persist. Routing still works via global fallback, so this is a hygiene issue, not a correctness bug.

---

## Additional Observations (Not New Gaps)

These are observations that don't constitute new gaps but may be useful context:

1. **Channel context injection ordering:** The `on_message` handler (bot.py L328–344) checks per-project control channels by iterating `_project_control_channels.items()`. This is O(n) in the number of projects with control channels. For a small number of projects this is fine; at scale, a reverse-lookup dict `{channel_id: project_id}` would be more efficient. Not a gap — works correctly as-is.

2. **Thread creation failure handling:** If `_create_task_thread` fails or returns None (bot.py L664–678), the orchestrator falls back to posting directly to the notifications channel. This is correct and robust. No gap.

3. **Notes threads and channel context:** Notes threads (bot.py L341, L377–383) have their own context injection path separate from per-project control channels. Both paths work correctly. No gap.

---

## Verified File Inventory

| File | Role | Status | Notes |
|------|------|--------|-------|
| `src/models.py` | `Project` dataclass with channel fields | Complete | L94–95 |
| `src/database.py` | Schema, migrations, CRUD for channel IDs | Complete | Both columns in schema + migrations |
| `src/orchestrator.py` | Notification callbacks with `project_id` threading | Complete | All call sites verified |
| `src/discord/bot.py` | Channel cache, routing, context injection, thread creation | Complete | Robust fallback chain |
| `src/discord/commands.py` | `/set-channel`, `/create-channel` slash commands | Complete | Gap 3: minor validation inefficiency |
| `src/command_handler.py` | `set_project_channel`, `get_project_channels` business logic | Complete | Gap 5: in-memory cache not cleared on delete |
| `src/chat_agent.py` | LLM tools + system prompt for channel management | Complete | Two tools + context injection |
| `src/config.py` | `DiscordConfig`, `NLParserConfig` | Complete | `NLParserConfig` is unused (Gap 2) |
| `src/scheduler.py` | Fair-share scheduling by project | Complete | No channel awareness needed |
| `src/main.py` | Daemon entry point | Complete | Wires bot + orchestrator correctly |
| `src/discord/nl_parser.py` | NL parsing stub — never called | Dead code | Gap 2 |
| `setup_wizard.py` | Discord setup — global channels only | No per-project support | Gap 1 |
| `src/discord/notifications.py` | Message formatters | Complete | No channel logic (correct) |
| `src/event_bus.py` | Event bus | Complete | No channel logic (correct) |
| `src/hooks.py` | Hook engine | Complete | No channel logic (correct) |

---

## Revised Effort & Priority Assessment

| Gap | Effort | Risk | Priority | Delta from Original |
|-----|--------|------|----------|-------------------|
| Gap 3: `/create-channel` validation fix | ~15 minutes | None | **High** (code quality) | Same |
| Gap 5: Channel cleanup on deletion | ~1 hour | Low | **Medium** | Same (clarified: main issue is bot cache, not DB) |
| Gap 1: Setup wizard per-project channels | ~2 hours | Low | **Medium** | Same |
| Gap 2: NL parser dead code removal | ~30 minutes | None | **Low** | Reduced: just remove, don't integrate |
| Gap 4: Channel map overview command | ~1 hour | None | **Low** | Downgraded from Medium (`/projects` partially covers it) |
| **Total** | **~5 hours** | — | — | Reduced from ~6 hours |

---

## Conclusion

**The original Executive Summary assessment is independently confirmed as accurate.** The per-project Discord channel system is architecturally complete with solid production-grade infrastructure across all core layers. All five identified gaps are real, correctly characterized, and independently addressable. No new gaps were discovered.

The recommended implementation order is: Gap 3 (quick fix) -> Gap 5 (hygiene) -> Gap 1 (discoverability) -> Gap 2 (cleanup) -> Gap 4 (convenience). Total estimated effort: ~5 hours with zero risk to existing functionality.
