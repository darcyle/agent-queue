# Chat UI Enhancements: Current State Audit

> **Date:** February 25, 2026
> **Branch:** `swift-flare/plan-out-chat-ui-enhancements-work`
> **Purpose:** Verify what has already been completed before planning forward work,
> so effort is not duplicated.

---

## Summary Table

| Item | Status | Evidence | Notes |
|------|--------|----------|-------|
| Embed factory (`src/discord/embeds.py`) | Done | Full 394-line module | `EmbedStyle`, `make_embed()`, 5 convenience builders, `status_embed()`, `truncate()`, `unix_timestamp()`, `check_embed_size()` |
| Status color/emoji mappings | Done | `STATUS_COLORS` and `STATUS_EMOJIS` dicts in `embeds.py` | All 11 `TaskStatus` values covered |
| Notification embed formatters | Done | All 8 `format_*_embed()` functions in `notifications.py` | 431-line module with parallel string + embed formatters |
| Orchestrator sends embeds | Partial | `_notify_channel()` passes text + embed | 6 of 8 embed formatters are wired up; 2 are unused |
| Interactive UI components | Done | `NotesView`, `TaskReportView`, `StatusToggleButton`, `TaskDetailSelect` in `commands.py` | Plus `NoteContentView`, 5 note buttons |
| Threaded task updates | Partial | Two-callback design (thread + main-channel reply) | Embeds NOT forwarded through thread reply path |
| Task dependency DAG in DB | Done | `task_dependencies` table with cycle-preventing CHECK constraint | `add_dependency()`, `get_dependencies()`, `get_blocking_dependencies()`, `get_dependents()`, `validate_dag()` |
| Parent/subtask model field | Done | `Task.parent_task_id` and `Task.is_plan_subtask` fields in `models.py` | Used to prevent recursive plan explosion |
| Slash command embed standardization | Not started | 7 inline `discord.Embed()` calls, 0 factory function calls | ~39 plain-text error responses remain |
| Unit tests for embeds/notifications | Not started | No `test_embed*` or `test_notif*` files found | Pure functions are easy to test |

---

## 1. Embed Factory (`src/discord/embeds.py`) -- DONE

**File:** `src/discord/embeds.py` (394 lines)

### Classes
- **`EmbedStyle`** (Enum) -- 5 members: `SUCCESS`, `ERROR`, `WARNING`, `INFO`, `CRITICAL`
  - Each carries a `color` (hex int) and `icon` (emoji string)

### Constants
- **`STATUS_COLORS`**: Maps all 11 `TaskStatus.value` strings to hex colors
- **`STATUS_EMOJIS`**: Maps all 11 `TaskStatus.value` strings to emoji characters
- **Discord API limits**: `LIMIT_TITLE=256`, `LIMIT_DESCRIPTION=4096`, `LIMIT_FIELD_NAME=256`, `LIMIT_FIELD_VALUE=1024`, `LIMIT_FOOTER_TEXT=2048`, `LIMIT_AUTHOR_NAME=256`, `LIMIT_FIELDS_PER_EMBED=25`, `LIMIT_TOTAL_CHARS=6000`

### Functions
| Function | Purpose |
|----------|---------|
| `truncate(text, max_len, suffix)` | Safe truncation with ellipsis |
| `unix_timestamp(dt, style)` | Discord `<t:UNIX:R>` format |
| `_embed_char_count(embed)` | Private helper for total char count |
| `check_embed_size(embed)` | Returns `(is_valid, total_chars)` tuple |
| `make_embed(style, title, ...)` | Core factory with auto-truncation and branding |
| `success_embed(title, **kwargs)` | Green / checkmark convenience builder |
| `error_embed(title, **kwargs)` | Red / cross convenience builder |
| `warning_embed(title, **kwargs)` | Amber / warning convenience builder |
| `info_embed(title, **kwargs)` | Blue / info convenience builder |
| `critical_embed(title, **kwargs)` | Dark red / alert convenience builder |
| `status_embed(status, title, **kwargs)` | Task-status-colored embed |

### Design Principles
- Pure functions, no gateway access required
- Every text property auto-truncated to Discord API limits
- Consistent footer ("AgentQueue") and optional timestamp
- Icon prepended to title based on style

---

## 2. Notification Formatters (`src/discord/notifications.py`) -- DONE

**File:** `src/discord/notifications.py` (431 lines)

### String Formatters (for logging/fallback)
1. `format_task_completed(task, agent, output)` -> str
2. `format_task_failed(task, agent, output)` -> str
3. `format_task_blocked(task, last_error)` -> str
4. `format_pr_created(task, pr_url)` -> str
5. `format_agent_question(task, agent, question)` -> str
6. `format_chain_stuck(blocked_task, stuck_tasks)` -> str
7. `format_stuck_defined_task(task, blocking_deps, stuck_hours)` -> str
8. `format_budget_warning(project_name, usage, limit)` -> str

### Embed Formatters (for Discord rich rendering)
1. `format_task_completed_embed(task, agent, output)` -> discord.Embed (uses `success_embed`)
2. `format_task_failed_embed(task, agent, output)` -> discord.Embed (uses `error_embed`)
3. `format_task_blocked_embed(task, last_error)` -> discord.Embed (uses `critical_embed`)
4. `format_pr_created_embed(task, pr_url)` -> discord.Embed (uses `info_embed`)
5. `format_agent_question_embed(task, agent, question)` -> discord.Embed (uses `warning_embed`)
6. `format_chain_stuck_embed(blocked_task, stuck_tasks)` -> discord.Embed (uses `critical_embed`)
7. `format_stuck_defined_task_embed(task, blocking_deps, stuck_hours)` -> discord.Embed (uses `warning_embed`)
8. `format_budget_warning_embed(project_name, usage, limit)` -> discord.Embed (uses `warning_embed`)

### Utility
- `classify_error(error_message)` -> `(error_type, suggestion)` -- pattern-matches against 13 error patterns
- `_ERROR_PATTERNS` -- list of `(keyword, label, suggestion)` tuples

All embed formatters use the factory functions from `embeds.py`. No inline `discord.Embed()` calls.

---

## 3. Orchestrator Embed Integration -- PARTIAL

**File:** `src/orchestrator.py`

### What's Wired Up

The orchestrator imports and uses 6 of 8 embed formatters:

| Formatter | Imported | Called with embed= | Location |
|-----------|----------|-------------------|----------|
| `format_task_completed_embed` | Yes | Yes | Line ~1596 |
| `format_task_failed_embed` | Yes | Yes | Line ~1679 |
| `format_task_blocked_embed` | Yes | Yes | Line ~1671 |
| `format_pr_created_embed` | Yes | Yes | Line ~1559 |
| `format_chain_stuck_embed` | Yes | Yes | Line ~601 |
| `format_stuck_defined_task_embed` | Yes | Yes | Line ~527 |
| `format_agent_question_embed` | **No** | **No** | Not imported or called |
| `format_budget_warning_embed` | **No** | **No** | Not imported or called |

### Gap: 2 Embed Formatters Not Wired Up
- **`format_agent_question_embed`** -- exists in `notifications.py` but the orchestrator does not import or use it. The `WAITING_INPUT` state transition exists in the state machine but no corresponding notification dispatch was found.
- **`format_budget_warning_embed`** -- exists in `notifications.py` but no budget-warning notification call was found in the orchestrator.

### Callback Architecture
- `_notify_channel(message, project_id, embed)` -- passes both text + embed to the bot callback
- Backward-compatible: only passes `embed` kwarg when not None
- `_send_message()` in `bot.py` sends embed if provided, otherwise falls back to `_send_long_message()`

---

## 4. Threaded Task Updates -- PARTIAL

**File:** `src/discord/bot.py` (lines 502-564)

### What Works
- Thread creation: `_create_task_thread()` creates a Discord thread per task execution
- Two-callback design:
  - `send_to_thread(text)` -- streams verbose agent output into the thread
  - `notify_main_channel(text)` -- replies to the thread-root message (appears in main channel feed)
- Agent messages stream into thread via `forward_agent_message()`
- `_post()` and `_notify_brief()` helpers route messages to thread or channel

### Gap: Embeds Not Forwarded Through Thread Reply Path

The `_notify_brief()` helper accepts an `embed` kwarg but **does not forward it** when a thread exists:

```python
async def _notify_brief(msg: str, *, embed: Any = None) -> None:
    if thread_main_notify:
        await thread_main_notify(msg)     # <-- embed is LOST here
    else:
        await self._notify_channel(msg, project_id=..., embed=embed)  # embed used only in fallback
```

Similarly, `notify_main_channel(text)` in `bot.py` only accepts plain text:

```python
async def notify_main_channel(text: str) -> None:
    await msg.reply(text)  # <-- no embed support
```

**Result:** When tasks execute in threads, the brief completion/failure notifications that reply to the thread root are always plain text, even though embed objects are available. Embeds are only rendered when no thread is present (fallback path).

---

## 5. Interactive UI Components (`src/discord/commands.py`) -- DONE

**File:** `src/discord/commands.py` (2,844 lines)

### View Classes
| Class | Purpose | Timeout | Components |
|-------|---------|---------|------------|
| `NotesView` | Interactive table-of-contents for project notes | None (persistent) | Note buttons (up to 20 per page), Prev/Next pagination, Refresh, Close Thread |
| `NoteContentView` | Dismiss button for note content display | None (persistent) | Single dismiss button |
| `TaskReportView` | Grouped task report with collapsible sections | 600s | StatusToggleButton per status, TaskDetailSelect dropdown |

### Button/Select Components
| Component | Type | Purpose |
|-----------|------|---------|
| `_NoteViewButton` | Button (gray) | View a specific note's content |
| `_NoteDismissButton` | Button (red) | Dismiss/delete a note content message |
| `_NotesRefreshButton` | Button (gray) | Re-fetch note list and rebuild view |
| `_NotesCloseButton` | Button (red) | Archive thread and clean up tracking |
| `_NotesPageButton` | Button (blue) | Navigate note pages (prev/next) |
| `StatusToggleButton` | Button (primary/secondary) | Toggle status section expand/collapse |
| `TaskDetailSelect` | Select (dropdown) | Pick a task for ephemeral detail view |

### Patterns
- Dynamic component rebuilding: views call `_rebuild_components()` on state changes
- Ephemeral responses for detail views (`defer(ephemeral=True)`)
- State tracked on bot instance (`_notes_threads`, `_note_viewers`, `_notes_toc_messages`)

---

## 6. Slash Command Embed Usage -- NOT STARTED

### Current State
- **7 inline `discord.Embed()` constructions** -- using raw hex colors, not the factory
- **0 uses of factory functions** (`success_embed`, `error_embed`, etc.) in commands
- **Only import from embeds.py:** `STATUS_COLORS` and `STATUS_EMOJIS` (for `TaskReportView`)
- **~39 plain-text error responses** -- `f"Error: {result['error']}"` pattern
- **~100 ephemeral=True instances** -- ephemeral handling is widely used but inconsistently applied to errors vs. successes

### Inline Embed Locations
1. **Project Created** (line ~855) -- `discord.Embed(title="... Project Created", color=0x2ecc71)`
2. **Task Added** (line ~1353) -- `discord.Embed(title="... Task Added", color=0x2ecc71)`
3. **Task Status Updated** (line ~1539) -- Uses `_STATUS_COLORS` but raw `discord.Embed()`
4. **Task Result** (line ~1563) -- Color selected by result, raw embed
5. **Agent Error Report** (line ~1623) -- `discord.Embed(..., color=0xe74c3c)`
6. **Agent Registered** (line ~1695) -- `discord.Embed(title="... Agent Registered", color=0x2ecc71)`
7. **Repo Registered** (line ~1774) -- `discord.Embed(title="... Repo Registered", color=0x2ecc71)`

### Work Needed
- Replace all 7 inline `discord.Embed()` calls with factory functions
- Convert ~39 plain-text error responses to `error_embed()` with ephemeral=True
- Ensure consistent branding (footer, timestamp) across all command responses

---

## 7. Task Dependency DAG -- DONE

**File:** `src/database.py`

### Schema
```sql
CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (task_id, depends_on_task_id),
    CHECK (task_id != depends_on_task_id)
);
```

### Functions
| Function | File | Purpose |
|----------|------|---------|
| `add_dependency(task_id, depends_on)` | database.py | Insert dependency edge |
| `get_dependencies(task_id)` | database.py | Get upstream task IDs |
| `get_blocking_dependencies(task_id)` | database.py | Get unmet deps (status != COMPLETED) |
| `get_dependents(task_id)` | database.py | Reverse lookup -- downstream tasks |
| `validate_dag(deps)` | state_machine.py | Three-color DFS cycle detection |
| `validate_dag_with_new_edge(deps, task_id, depends_on)` | state_machine.py | Prospective edge validation |

---

## 8. Parent/Subtask Model -- DONE

**File:** `src/models.py`

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `parent_task_id` | `str \| None` | `None` | Links subtasks to parent (plan-generation lineage) |
| `is_plan_subtask` | `bool` | `False` | Prevents recursive plan explosion |

Both fields exist in the `tasks` table schema (database.py) and are used by the orchestrator
to reuse parent branches and guard against recursive plan generation.

---

## 9. TaskStatus Enum -- DONE

**File:** `src/models.py` (11 states)

| Status | Emoji | Color | Description |
|--------|-------|-------|-------------|
| DEFINED | white | Gray (#95a5a6) | Created, dependencies not yet met |
| READY | blue | Blue (#3498db) | Dependencies met, awaiting assignment |
| ASSIGNED | clipboard | Purple (#9b59b6) | Agent assigned, execution pending |
| IN_PROGRESS | yellow | Amber (#f39c12) | Agent actively executing |
| WAITING_INPUT | speech | Teal (#1abc9c) | Agent paused for human input |
| PAUSED | pause | Dark gray (#7f8c8d) | Paused (rate limit, token exhaustion) |
| VERIFYING | magnifying | Dark blue (#2980b9) | Output verification in progress |
| AWAITING_APPROVAL | hourglass | Orange (#e67e22) | PR created, awaiting approval |
| COMPLETED | green | Green (#2ecc71) | Successfully finished |
| FAILED | red | Red (#e74c3c) | Failed (retryable) |
| BLOCKED | no-entry | Dark red (#992d22) | Permanently blocked |

---

## 10. Unit Test Coverage -- NOT STARTED

No dedicated test files found for:
- `src/discord/embeds.py` (pure functions, ideal for unit testing)
- `src/discord/notifications.py` (embed formatters)
- Embed size validation logic

---

## Identified Gaps for Forward Work

### High Priority
1. **Thread reply embeds** -- `notify_main_channel()` and `_notify_brief()` do not forward
   embed objects when threads are present. Brief notifications in threads are always plain text.
2. **Slash command standardization** -- 7 inline `discord.Embed()` calls and ~39 plain-text
   errors should use factory functions for consistency and branding.

### Medium Priority
3. **Agent question notification** -- `format_agent_question_embed()` exists but is not
   called anywhere in the orchestrator.
4. **Budget warning notification** -- `format_budget_warning_embed()` exists but is not
   called anywhere in the orchestrator.
5. **Unit tests** -- No test coverage for `embeds.py` or notification embed formatters.

### Low Priority
6. **Interactive components for notifications** -- Retry buttons on failed tasks,
   confirmation dialogs for destructive commands, pagination for long lists (mentioned
   in the research doc but not yet implemented).
7. **Chat agent response formatting** -- Currently plain text; could benefit from structured
   embeds for tool execution results (optional enhancement).
