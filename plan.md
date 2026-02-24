# Per-Project Channel Infrastructure — Validation Summary

This document summarizes the validation of existing per-project channel infrastructure and identifies specific gaps that need to be addressed to fully implement multi-channel support with automatic channel creation, channel-context-aware project resolution, and updated setup wizard support.

**Audit date:** 2026-02-24
**Branch:** `calm-beacon/summary`
**Status:** All existing infrastructure validated against source code. 5 gaps identified, 3 additional observations noted.

---

## Executive Summary

The per-project Discord channel system is **largely complete** at the storage, routing, and LLM integration layers. The core data flow — project creation → channel assignment → notification routing with global fallback — is fully operational. Five tactical gaps remain, concentrated in the **UX and automation** layers: slash commands can't auto-detect project context from channels, channel creation isn't idempotent, and new projects don't get channels automatically.

---

## What's Working (Validated ✅)

| Layer | Component | Status | Key Files |
|-------|-----------|--------|-----------|
| **Database** | `discord_channel_id` + `discord_control_channel_id` columns, migrations, CRUD | Complete | `src/database.py` |
| **Model** | `Project` dataclass with both channel fields (`str \| None`) | Complete | `src/models.py` |
| **Bot Routing** | Forward-lookup caches, startup resolution, two-tier fallback (project → global) | Complete | `src/discord/bot.py` |
| **Orchestrator** | All callbacks pass `project_id` for per-project routing | Complete | `src/orchestrator.py` |
| **NL Context** | Messages in project channels get auto-injected `[Context: project=...]` | Complete | `src/discord/bot.py` |
| **Slash Commands** | `/set-channel` and `/create-channel` for manual channel management | Complete | `src/discord/commands.py` |
| **LLM Tools** | `set_project_channel` and `get_project_channels` in TOOLS list | Complete | `src/chat_agent.py` |
| **Command Handler** | Backend persistence, validation, list-includes-channels | Complete | `src/command_handler.py` |
| **Config** | Global channel names resolved at startup by guild scan | Complete | `src/config.py` |

---

## Identified Gaps

### Gap 1: No Reverse Channel→Project Lookup (HIGH)
- **Problem:** Forward lookup (project→channel) exists but there's no channel→project reverse mapping
- **Impact:** Slash commands in project channels can't auto-infer which project the user means
- **Fix:** Add `_channel_to_project: dict[int, str]` reverse dict, populated alongside forward caches
- **Complexity:** Low (~10 lines)
- **Files:** `src/discord/bot.py`

### Gap 2: Slash Commands Don't Auto-Infer Project from Channel (HIGH)
- **Problem:** All project-scoped commands require explicit `project_id`, even in a project's dedicated channel
- **Impact:** Defeats the UX purpose of per-project channels; users must type `project_id=my-app` in `#my-app-control`
- **Fix:** Add fallback chain: explicit param → reverse channel lookup → active project → require explicit
- **Complexity:** Medium (touches many commands)
- **Files:** `src/discord/commands.py`, `src/discord/bot.py`
- **Depends on:** Gap 1

### Gap 3: No Idempotent Channel Creation (MEDIUM)
- **Problem:** `/create-channel` always creates new channels; running twice creates duplicates
- **Impact:** Automation and re-runs aren't safe
- **Fix:** Scan `guild.text_channels` for existing name match before creating
- **Complexity:** Low
- **Files:** `src/discord/commands.py`

### Gap 4: No Auto-Channel Creation on Project Creation (MEDIUM)
- **Problem:** `_cmd_create_project()` creates workspace + DB record but no Discord channels
- **Impact:** Every new project requires manual `/set-channel` or `/create-channel`
- **Fix:** Add `auto_channels` param + `discord.auto_create_project_channels` config + category config
- **Complexity:** Medium
- **Files:** `src/command_handler.py`, `src/discord/commands.py`, `src/config.py`
- **Depends on:** Gap 3

### Gap 5: Setup Wizard Lacks Per-Project Channel Guidance (LOW)
- **Problem:** Wizard configures only global channels; first project gets no channel setup step
- **Impact:** New users don't discover per-project channels during onboarding
- **Fix:** Add post-project-creation step with guidance or post-start `/setup-channels` command
- **Complexity:** Medium
- **Files:** `setup_wizard.py`

---

## Additional Observations

### Observation 1: LLM Tool Requires Numeric Channel IDs
The `set_project_channel` tool requires a raw numeric `channel_id` — no name-based resolution. In natural language conversations, the LLM can't resolve `#my-app-notifications` to an ID. The `/set-channel` slash command works around this via Discord's UI channel picker. Consider adding a `resolve_channel` tool or accepting `channel_name` as an alternative parameter.
- **Files:** `src/chat_agent.py`, `src/command_handler.py`

### Observation 2: Linear Scan in Message Handler
The `on_message()` handler uses a linear scan of `_project_control_channels.items()` to detect which project a message belongs to (bot.py lines 333-337). This is O(n) per message. The reverse-lookup dict from Gap 1 would also eliminate this scan.
- **Files:** `src/discord/bot.py`

### Observation 3: No Per-Project Channel Config Options
There are no configuration options for `auto_create_project_channels` or `project_channel_category` in `DiscordConfig`. Per-project channels are managed entirely through runtime commands and DB records (by design), but adding config options (per Gap 4) would enable better defaults.
- **Files:** `src/config.py`

---

## Recommended Implementation Order

```
Gap 1 (reverse lookup)          <- foundational, ~10 lines, unblocks Gap 2
  └-> Gap 2 (auto-infer project) <- highest UX impact
Gap 3 (idempotent creation)     <- safety, unblocks Gap 4
  └-> Gap 4 (auto-create)        <- workflow improvement
Gap 5 (wizard update)           <- lowest priority, docs may suffice
```

## Components That Need No Changes

- Database schema and migrations
- Project dataclass
- Bot channel routing (forward-lookup + fallback)
- Orchestrator callback propagation
- Natural language context injection
- Channel management slash commands (`/set-channel`, `/create-channel`)
- LLM tools (`set_project_channel`, `get_project_channels`)
- Command handler backend
- System prompt documentation
