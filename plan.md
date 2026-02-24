# Executive Summary: Per-Project Discord Channel Infrastructure

**Validation date:** 2026-02-24
**Branch:** `eager-vault/background`
**Method:** Direct code inspection of every file touching channel, project, or routing logic
**Scope:** Full codebase audit confirming existing infrastructure for multi-channel support, automatic channel creation, channel-context-aware project resolution, and setup wizard support

---

## Verdict

The per-project Discord channel system is **architecturally complete and production-ready** at the storage, routing, orchestrator, and LLM integration layers. The full data flow — project creation, channel assignment, per-project notification routing with global fallback, and channel-context-aware NL command scoping — is operational.

Five **targeted gaps** remain, all concentrated in the **UX and automation** layers. No architectural refactoring is required; each gap can be closed with additive changes to existing code.

---

## Layer-by-Layer Assessment

### 1. Storage Layer — ✅ Complete

| Component | File | Status |
|-----------|------|--------|
| Schema columns | `src/database.py` L14–27 | `discord_channel_id TEXT` and `discord_control_channel_id TEXT` on `projects` table |
| Migrations | `src/database.py` L200–201 | `ALTER TABLE` migrations for both columns (idempotent) |
| CRUD operations | `src/database.py` L215–227 | `create_project` persists both fields; `update_project` supports partial updates |
| Data model | `src/models.py` L84–95 | `Project` dataclass includes `discord_channel_id: str | None` and `discord_control_channel_id: str | None` |

Both channel ID fields flow through the full persistence lifecycle: creation, reads, updates, and project listing. No storage-layer work is needed.

### 2. Routing Layer — ✅ Complete (with Global Fallback)

| Component | File | Lines | Description |
|-----------|------|-------|-------------|
| Channel resolution on startup | `src/discord/bot.py` | 197–229 | `_resolve_project_channels()` loads all projects, resolves Discord channel objects, caches in `_project_channels` / `_project_control_channels` dicts |
| Notification routing | `src/discord/bot.py` | 231–257 | `_get_notification_channel(project_id)` and `_get_control_channel(project_id)` — check per-project cache first, fall back to global |
| Runtime cache update | `src/discord/bot.py` | 67–78 | `update_project_channel()` — hot-updates cache after `/set-channel` or `/create-channel` (zero-restart) |
| Thread creation | `src/discord/bot.py` | 259–307 | `_create_task_thread()` accepts `project_id`, creates thread in the correct per-project or global channel |

**Routing correctness:** Every notification and thread-creation call site in the orchestrator passes `project_id`, ensuring per-project isolation when a channel is configured. When no per-project channel exists, messages fall through to the global channel — no notifications are lost.

### 3. Orchestrator Layer — ✅ Complete

| Component | File | Lines | Description |
|-----------|------|-------|-------------|
| Notify callback | `src/orchestrator.py` | 115–127 | `_notify_channel(message, project_id)` — forwards to bot callback with project context |
| Control callback | `src/orchestrator.py` | 128–137 | `_control_channel_post(message, project_id)` — same pattern for control channel |
| Callback registration | `src/orchestrator.py` | 72–82 | `set_notify_callback()`, `set_control_callback()`, `set_create_thread_callback()` |
| Call sites | `src/orchestrator.py` | L109–112, L327, L803–814, L846, L855, L885, L902 | All task lifecycle events (stop, error, completion, output) route via `project_id` |

The orchestrator consistently threads `project_id` through all notification paths. No orchestrator changes are needed.

### 4. Discord Command Layer — ✅ Complete

| Command | File | Lines | Description |
|---------|------|-------|-------------|
| `/set-channel` | `src/discord/commands.py` | 342–376 | Links an existing Discord channel to a project (notifications or control). Updates DB + bot cache immediately. |
| `/create-channel` | `src/discord/commands.py` | 378–452 | Creates a new text channel in the guild and auto-links it to a project. Handles permissions errors, category placement. |

Both commands immediately update the bot's in-memory channel cache via `bot.update_project_channel()`, so routing takes effect without restart.

### 5. Command Handler (Business Logic) — ✅ Complete

| Method | File | Lines | Description |
|--------|------|-------|-------------|
| `_cmd_set_project_channel` | `src/command_handler.py` | 197–219 | Validates project exists, validates channel_type, persists to DB |
| `_cmd_get_project_channels` | `src/command_handler.py` | 221–231 | Returns both channel IDs for a project |

Clean, well-validated business logic. Both methods are wired to Discord slash commands and LLM chat tools.

### 6. LLM Chat Integration — ✅ Complete

| Component | File | Lines | Description |
|-----------|------|-------|-------------|
| `set_project_channel` tool | `src/chat_agent.py` | 77–99 | LLM can programmatically link channels to projects |
| `get_project_channels` tool | `src/chat_agent.py` | 100–111 | LLM can query channel assignments |
| Channel context injection | `src/discord/bot.py` | 368–383 | Messages in per-project control channels automatically get `[Context: project_id='...']` prepended, so the LLM scopes commands to that project |
| System prompt docs | `src/chat_agent.py` | 670–724 | Documents channel management tools and usage |

The LLM can both manage and respond contextually to per-project channels.

---

## Five Targeted Gaps

All gaps are in the **UX and automation** layers. None require architectural changes — each is a self-contained, additive enhancement.

### Gap 1: Setup Wizard Ignores Per-Project Channels

**Location:** `setup_wizard.py` L217–325 (`step_discord()`)

**Current state:** The setup wizard configures three *global* channel names (control, notifications, agent_questions) and tests connectivity. It has zero awareness of per-project channels — no prompts to create them, no discovery of existing channels, no linking workflow.

**Impact:** New users complete setup with only global channels and never discover per-project isolation exists.

**Fix:** Add an optional post-setup step that iterates configured projects and offers to create/link dedicated channels for each. Estimated: ~50 lines of additive code in `setup_wizard.py`.

### Gap 2: Natural Language Parser is Dead Code

**Location:** `src/discord/nl_parser.py` (entire file, 42 lines)

**Current state:** `parse_natural_language()` is defined but never imported or called anywhere in the codebase. The `NLParserConfig` exists in `src/config.py` L87 but is never consumed by the bot. The parser itself is a keyword-matching stub with no project-context extraction — it doesn't attempt to identify which project a user is referring to.

**Impact:** The intended flow of extracting project context from freeform messages (e.g., "pause my-app" → project_id='my-app') doesn't work. Users must either type in a per-project control channel (which has context injection) or explicitly name the project.

**Fix:** Either integrate the parser into `bot.py`'s `on_message` flow with project-context extraction, or remove the dead code and document that the LLM-based chat agent handles all NL understanding (which it already does via the context injection at L370–376). Estimated: ~30 lines to integrate, or 0 lines to remove + document.

### Gap 3: `/create-channel` Uses Inefficient Project Validation

**Location:** `src/discord/commands.py` L402–410

**Current state:** The `/create-channel` command validates that a project exists by calling `handler.execute("list_projects", {})` and scanning the full project list. Line 403 also has a vestigial no-op call (`handler.execute("get_task", {"task_id": "__noop__"})`) that serves no purpose.

**Impact:** Functionally correct but wasteful — loads all projects into memory to check if one exists. The `database.get_project(id)` method already exists and is used everywhere else (including `_cmd_set_project_channel` at L200).

**Fix:** Replace L402–410 with a direct `handler.execute("get_project", {"project_id": project_id})` or equivalent DB call. Remove the no-op line. Estimated: 5-line change.

### Gap 4: No Channel Map / Overview Command

**Current state:** There is no way for a user to see an overview of all project-to-channel assignments at a glance. The only way to check is to call `get_project_channels` for each project individually (via LLM chat) or inspect the database.

**Impact:** As the number of projects grows, users lose track of which channels are linked to which projects. There's no `/channel-map` or `/channels` command to provide a dashboard view.

**Fix:** Add a `/channel-map` slash command (and corresponding LLM tool) that lists all projects and their assigned notification/control channels. Estimated: ~40 lines in `commands.py` + ~15 lines in `command_handler.py`.

### Gap 5: No Channel Cleanup on Project Deletion or Channel Loss

**Location:** `src/command_handler.py` L233+ (`_cmd_delete_project`) and `src/discord/bot.py` L197–229 (`_resolve_project_channels`)

**Current state:**
- **Project deletion** (`_cmd_delete_project`) does not clear channel assignments or remove the project from the bot's channel cache. Orphaned channel references remain in the database.
- **Channel resolution** logs a warning if a channel ID can't be found in the guild (L216–219, L226–229), but takes no corrective action — the stale ID stays in the database.
- There is no mechanism to detect that a previously-linked Discord channel has been deleted and alert the user or fall back gracefully.

**Impact:** Over time, deleted projects or deleted Discord channels leave stale references. No data loss occurs (the fallback to global channels still works), but it creates confusion and clutter.

**Fix:**
- In `_cmd_delete_project`: clear `discord_channel_id` and `discord_control_channel_id` and remove from bot cache.
- In `_resolve_project_channels`: optionally null-out stale channel IDs in the database and log a user-visible warning.
- Estimated: ~20 lines across two files.

---

## File Inventory

| File | Role | Status |
|------|------|--------|
| `src/models.py` | `Project` dataclass with channel fields | ✅ Complete |
| `src/database.py` | Schema, migrations, CRUD for channel IDs | ✅ Complete |
| `src/orchestrator.py` | Notification callbacks with `project_id` threading | ✅ Complete |
| `src/discord/bot.py` | Channel cache, routing, context injection, thread creation | ✅ Complete |
| `src/discord/commands.py` | `/set-channel`, `/create-channel` slash commands | ✅ Complete (Gap 3: validation) |
| `src/command_handler.py` | `set_project_channel`, `get_project_channels` business logic | ✅ Complete (Gap 5: cleanup) |
| `src/chat_agent.py` | LLM tools and system prompt for channel management | ✅ Complete |
| `src/discord/nl_parser.py` | NL parsing stub — never called | ⚠️ Dead code (Gap 2) |
| `setup_wizard.py` | Discord setup — global channels only | ⚠️ No per-project support (Gap 1) |

---

## Effort Estimate

| Gap | Effort | Risk | Priority |
|-----|--------|------|----------|
| Gap 1: Setup wizard per-project channels | ~2 hours | Low | Medium |
| Gap 2: NL parser dead code | ~1 hour (integrate or remove) | Low | Low |
| Gap 3: `/create-channel` validation fix | ~15 minutes | None | High (code quality) |
| Gap 4: Channel map overview command | ~2 hours | Low | Medium |
| Gap 5: Channel cleanup on deletion | ~1 hour | Low | Medium |
| **Total** | **~6 hours** | — | — |

All five gaps are independently addressable. No gap blocks another, and none require changes to the core routing or storage architecture.

---

## Conclusion

The per-project Discord channel system has **solid, production-grade infrastructure**. The storage schema, notification routing with fallback, orchestrator integration, slash commands, and LLM tool support all work correctly and cohesively. The five identified gaps are UX polish items — improving discoverability (setup wizard, channel map), removing dead code (NL parser), fixing a minor inefficiency (`/create-channel` validation), and adding lifecycle hygiene (cleanup on deletion). Closing all five gaps requires approximately 6 hours of additive development with no risk to existing functionality.
