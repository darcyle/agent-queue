# Chat UI Enhancements — Comprehensive Implementation Plan

> **Date:** February 25, 2026
> **Branch:** `swift-flare/plan-out-chat-ui-enhancements-work`
> **Purpose:** Complete strategy for improving readability, navigation, and user
> experience when interacting with Agent Queue through Discord.

---

## Current State Summary

Before defining forward work, this section captures what has **already been
completed** in committed code to avoid duplicate effort.

### What's Done (Committed)

| Item | Evidence |
|------|----------|
| Embed factory (`src/discord/embeds.py`) | Full 500+ line module: `EmbedStyle`, `make_embed()`, 5 convenience builders, `status_embed()`, `truncate()`, `unix_timestamp()`, `check_embed_size()`, `progress_bar()`, `format_tree_task()` |
| Status color/emoji mappings | `STATUS_COLORS` and `STATUS_EMOJIS` dicts covering all 11 `TaskStatus` values |
| Notification embed formatters | All 8 `format_*_embed()` functions in `notifications.py` (431+ lines) |
| Orchestrator sends embeds (6/8) | `_notify_channel()` passes text + embed; 6 formatters wired up: completed, failed, blocked, pr_created, chain_stuck, stuck_defined_task |
| Interactive UI components | `NotesView`, `TaskReportView`, `StatusToggleButton`, `TaskDetailSelect`, note buttons in `commands.py` |
| Interactive action buttons | `TaskFailedView` (Retry/Skip/View Error), `TaskApprovalView` (Approve/Restart), `TaskBlockedView` (Restart/Skip) in `notifications.py` |
| Thread reply embed support | `notify_main_channel()` now accepts `embed` kwarg and forwards it |
| Task dependency DAG in DB | `task_dependencies` table, `add_dependency()`, `get_dependencies()`, `get_blocking_dependencies()`, `get_dependents()`, `validate_dag()` |
| Parent/subtask model | `Task.parent_task_id` and `Task.is_plan_subtask` fields |
| `get_subtasks()` DB helper | Exists in `database.py` and used by command_handler |
| Progress bars | `progress_bar()` utility in `embeds.py`, used in 4 locations in `commands.py` |
| Tree task formatting | `format_tree_task()` in `embeds.py` with box-drawing characters, used in task detail/list views |
| Active-only filter | `/tasks` command has `show_completed` param, filters completed tasks by default |
| Type tags (emoji indicators) | 📋 plan subtask, 📦 has subtasks, 🔗 has PR, 🔒 approval required — in task listings |
| Dependency visualization | depends_on/blocks sections in task detail view with status emojis |
| Inline embed removal | All 7 inline `discord.Embed()` calls in commands.py replaced with factory functions |
| `_send_error()` helper | Defined in `commands.py` for uniform error embed responses |

### What's Partially Complete

| Item | Current State | Remaining Work |
|------|--------------|----------------|
| Slash command error consistency | 13 uses of `_send_error()` | ~72 plain-text `f"Error:..."` responses still need conversion |
| Orchestrator embed wiring | 6/8 formatters called | `format_agent_question_embed` and `format_budget_warning_embed` not wired |
| Command handler active-only filter | Filtering done in slash command layer only | `_cmd_list_tasks` in `command_handler.py` lacks native `show_completed` / `include_completed` parameter |

### What's Not Started

| Item | Priority | Est. Effort |
|------|----------|-------------|
| Unit tests for embeds/notifications | P2 | 3–4h |
| `task_type` field in Task model/DB | P3 | 1.5h |
| Type tag auto-generation in plan parser | P3 | 2h |
| ChatAgent tool schema updates | P2 | 2h |
| Cross-project active task query | P2 | 2h |
| `get_task_tree` recursive DB helper | P2 | 1.5h |
| `/list-tasks` tree mode as slash command option | P2 | 1h |
| Multi-device support (research + prototype) | P2 | 8–10h |
| Visual QA on desktop + mobile Discord | P2 | 1h |
| Documentation updates | P2 | 1h |

---

## Design Constraints & Principles

These apply to all phases below and should be followed by every implementing agent.

1. **CommandHandler is the single code path** — all business logic goes in
   `src/command_handler.py`; Discord commands and ChatAgent are thin presentation
   layers that call `CommandHandler.execute()`.
2. **Embed factory is the single source of truth** — all embeds flow through
   `src/discord/embeds.py`. Never construct `discord.Embed()` inline.
3. **Discord API hard limits** — 2,000 chars per message, 6,000 chars total per
   embed, 25 fields per embed, 4,096 chars per description, 1,024 chars per
   field value. Always use `truncate()` and `check_embed_size()`.
4. **Backward compatibility** — string formatters in `notifications.py` are
   preserved alongside embeds for logging and testing.
5. **Ephemeral errors** — all error responses to slash commands must be
   `ephemeral=True` to avoid channel clutter.
6. **No new dependencies** — everything uses `discord.py >= 2.3.0` built-in
   `discord.Embed` and `discord.ui`.

### Key File Map

| File | Role |
|------|------|
| `src/command_handler.py` | Unified command execution — business logic |
| `src/chat_agent.py` | LLM tool definitions and response formatting |
| `src/discord/commands.py` | Discord slash commands (50+), UI components |
| `src/discord/bot.py` | Bot core, message routing, thread management |
| `src/discord/notifications.py` | Task lifecycle formatters (text + embed + views) |
| `src/discord/embeds.py` | Centralized embed factory, progress bars, tree helpers |
| `src/database.py` | SQLite persistence, dependency/subtask queries |
| `src/models.py` | Task, Agent, Project dataclasses and enums |
| `src/orchestrator.py` | Task lifecycle management, notification dispatch |
| `src/plan_parser.py` | Plan file parsing, automatic subtask creation |

---

## Phase 1: Complete Slash Command Error Consistency

**Priority:** P2
**Estimated effort:** 3–4 hours
**Rationale:** ~72 plain-text error responses remain unconverted. This is the
single largest visual inconsistency still present and has the widest daily-use
impact.

### 1.1 Audit and categorize all remaining plain-text error responses

- Search `commands.py` for all `f"Error:` and `"Error:` patterns (~72 instances).
- Categorize each by command group: project, task, agent, repo, notes, system.
- Produce a checklist of locations to convert.
- **Files:** `src/discord/commands.py` (read-only audit, then edits)

### 1.2 Convert all plain-text errors to `_send_error()` with ephemeral

- Replace every `await interaction.response.send_message(f"Error: ...", ephemeral=True)` with `await _send_error(interaction, result['error'])` or the equivalent `error_embed()` call.
- The `_send_error()` helper already exists and creates ephemeral error embeds. Use it consistently.
- Work command-group by command-group to minimize merge conflicts:
  1. Project commands (~8 errors)
  2. Task commands (~15 errors)
  3. Agent commands (~8 errors)
  4. Repo commands (~6 errors)
  5. Notes commands (~10 errors)
  6. Status/system commands (~5 errors)
  7. Remaining miscellaneous (~20 errors)
- **Files:** `src/discord/commands.py`

### 1.3 Convert remaining success responses to factory embeds

- Scan for any remaining `await interaction.response.send_message(f"✅ ...")` patterns.
- Replace with `success_embed()` calls for consistent branding (footer + timestamp).
- **Files:** `src/discord/commands.py`

### Milestone

> **M1:** Every slash command error response uses `_send_error()` / `error_embed()` and is ephemeral. Every success response uses the embed factory. No inline `discord.Embed()` or plain-text errors remain.

---

## Phase 2: Wire Up Missing Orchestrator Notifications

**Priority:** P2
**Estimated effort:** 2–3 hours
**Rationale:** Two notification formatters exist but are dead code. Wiring them
provides richer user feedback for agent questions and budget warnings.

### 2.1 Wire `format_agent_question_embed` into orchestrator

- Find where the orchestrator transitions a task to `WAITING_INPUT` status.
- At that point, call `format_agent_question_embed(task, agent, question)` and pass the resulting embed to `_notify_channel()`.
- Import `format_agent_question_embed` from `notifications.py`.
- Ensure the text version (`format_agent_question()`) is also passed as fallback.
- **Files:** `src/orchestrator.py`, `src/discord/notifications.py` (import only)

### 2.2 Wire `format_budget_warning_embed` into orchestrator

- Identify the code path where budget/token limits are checked (likely in the scheduling loop or token budget module).
- When usage exceeds a configurable threshold (e.g., 80% and 95%), call `format_budget_warning_embed(project_name, usage, limit)`.
- Pass the embed to `_notify_channel()`.
- Avoid spamming: track the last warning time per project and rate-limit to at most once per hour.
- **Files:** `src/orchestrator.py`, `src/tokens/budget.py` (check thresholds)

### 2.3 Add an `AgentQuestionView` with modal reply button

- Create a `discord.ui.View` subclass that attaches a "Reply" button to agent question notifications.
- When clicked, open a `discord.ui.Modal` with a text input for the user's response.
- On submit, call `CommandHandler.execute("provide_input", {"task_id": ..., "input": ...})`.
- Attach this view when sending the `format_agent_question_embed` notification.
- **Files:** `src/discord/notifications.py` (new View class), `src/orchestrator.py`

### Milestone

> **M2:** All 8 notification embed formatters are wired into the orchestrator.
> Agent questions show a modal reply button. Budget warnings fire at 80% and 95%
> thresholds with rate limiting.

---

## Phase 3: Command Handler Active-Only Filtering & Cross-Project Query

**Priority:** P2
**Estimated effort:** 3–4 hours
**Rationale:** The active-only filter currently lives only in the slash command
layer. Pushing it into `CommandHandler` ensures the ChatAgent also benefits,
and the cross-project query is a new capability requested in UX feedback.

### 3.1 Add `include_completed` parameter to `_cmd_list_tasks`

- Currently `_cmd_list_tasks` in `command_handler.py` returns all tasks. Add an
  `include_completed: bool = False` parameter.
- When `False`, exclude tasks with status in `{COMPLETED, FAILED, BLOCKED}`.
- When `True`, return all tasks.
- Add a `completed_only: bool = False` parameter for viewing just finished tasks.
- Preserve existing `status` filter as an override (when provided, it takes
  precedence).
- **Files:** `src/command_handler.py`

### 3.2 Update slash command to delegate filtering to CommandHandler

- Remove the filtering logic from the `/tasks` slash command in `commands.py`.
- Instead, pass `include_completed=show_completed` through to
  `CommandHandler.execute("list_tasks", ...)`.
- This ensures consistent behavior whether tasks are listed via slash command
  or ChatAgent.
- **Files:** `src/discord/commands.py`

### 3.3 Update ChatAgent `list_tasks` tool schema

- Add `include_completed` and `show_all` parameters to the LLM tool definition.
- Update the tool description to explain that completed tasks are hidden by
  default.
- Ensure the LLM naturally says "I see N active tasks" when using the default
  filter.
- **Files:** `src/chat_agent.py`

### 3.4 Add cross-project active task query

- Add `_cmd_list_active_tasks_all_projects` to `command_handler.py` (or extend
  `list_tasks` with `cross_project=True` flag).
- Query: all tasks with non-terminal status across all projects.
- Group results by project for readability.
- Add corresponding slash command `/active-tasks` in `commands.py`.
- Add `list_active_tasks_all_projects` tool to ChatAgent.
- **Files:** `src/command_handler.py`, `src/database.py`, `src/discord/commands.py`, `src/chat_agent.py`

### Milestone

> **M3:** `list_tasks` filters completed tasks by default at the CommandHandler
> level. ChatAgent benefits from the same filter. `/active-tasks` shows all
> active work across every project.

---

## Phase 4: Subtask Tree View as a First-Class Display Mode

**Priority:** P2
**Estimated effort:** 5–6 hours
**Rationale:** Tree formatting helpers exist in `embeds.py` and are used in
task detail views, but there is no "tree mode" option on `/list-tasks` or the
ChatAgent `list_tasks` tool. This phase promotes tree view to a first-class
display mode.

### 4.1 Add `get_task_tree` recursive DB helper

- `get_task_tree(root_task_id)` → returns a nested dict/list structure
  representing the full hierarchy (root + all descendants, recursively).
- `get_parent_tasks(project_id)` → returns only tasks where
  `parent_task_id IS NULL` for a given project (top-level tasks).
- Use existing `get_subtasks()` as the building block.
- **Files:** `src/database.py`

### 4.2 Build tree-view text formatter in `command_handler.py`

- Create a `_format_task_tree(root_task, subtasks, depth=0, max_depth=4)` function.
- Uses box-drawing characters: `├──`, `└──`, `│  `.
- Appends status emoji from `STATUS_EMOJIS`.
- Includes a summary line: `X/Y subtasks complete`.
- Implements truncation: if tree exceeds ~1,800 chars, collapse deep nesting to
  `... (N more subtasks)`.
- Supports compact mode (parent + summary counts only) and expanded mode
  (full tree).
- **Files:** `src/command_handler.py`

### 4.3 Add `display_mode` to `_cmd_list_tasks`

- Add `display_mode` argument: `"flat"` (default, current behavior), `"tree"`,
  or `"compact"`.
- `"tree"`: group tasks by parent, render tree for each root task.
- `"compact"`: show only parent tasks with subtask count and progress bar.
- `"flat"`: existing flat list behavior.
- **Files:** `src/command_handler.py`

### 4.4 Add tree mode to `/list-tasks` slash command

- Add `view` option to the slash command with choices: `list` (default), `tree`,
  `compact`.
- When `tree`, send tree-formatted output in a code block for monospace
  alignment.
- Handle pagination: if the tree exceeds 2,000 chars, split across multiple
  messages or use an embed with scrollable description.
- **Files:** `src/discord/commands.py`

### 4.5 Add tree view embed helper

- Create `tree_view_embed()` in `embeds.py` that places the tree in the embed
  description inside a code block.
- Use embed fields for the summary line and metadata.
- Respect the 4,096-char description limit; paginate if needed.
- **Files:** `src/discord/embeds.py`

### 4.6 Update ChatAgent for tree view

- Add `display_mode` parameter to the `list_tasks` tool definition.
- When the LLM calls `list_tasks` with `display_mode="tree"`, return the
  pre-formatted tree string.
- Add a `get_task_tree` tool that returns the hierarchy for a specific parent.
- **Files:** `src/chat_agent.py`

### Milestone

> **M4:** Users can run `/list-tasks view:tree` to see task hierarchy as a tree.
> Compact mode shows parents with subtask counts + progress bars. ChatAgent can
> display and explain task trees.

---

## Phase 5: Task Type Taxonomy & Tags

**Priority:** P3
**Estimated effort:** 4–6 hours
**Rationale:** Complements existing emoji type indicators with a proper data
model. Current type tags are inferred at display time from task properties; this
phase makes type a first-class stored attribute.

### 5.1 Add `task_type` field to Task model and DB

- Add `task_type: str | None = None` to the `Task` dataclass in `models.py`.
- Add corresponding `task_type TEXT` column to the `tasks` table in `database.py`
  via schema migration (ALTER TABLE or rebuild for SQLite).
- Define allowed values as a Python enum or constant list: `feature`, `bugfix`,
  `refactor`, `test`, `docs`, `chore`, `research`, `plan`.
- **Files:** `src/models.py`, `src/database.py`

### 5.2 Allow manual type assignment at task creation

- Add `task_type` parameter to `_cmd_create_task` in `command_handler.py`.
- Add `type` option to `/add-task` slash command with autocomplete choices.
- Update ChatAgent `create_task` tool schema.
- **Files:** `src/command_handler.py`, `src/discord/commands.py`, `src/chat_agent.py`

### 5.3 Auto-generate type tags in plan parser

- When the plan parser creates subtasks from a plan file, infer the type tag
  from the phase title and description.
- Use keyword-matching heuristic: "Add tests" → `test`, "Fix bug" → `bugfix`,
  "Refactor" → `refactor`, "Research" → `research`, etc.
- Store the inferred type in `task_type`.
- **Files:** `src/plan_parser.py`

### 5.4 Display stored type tags in task listings and embeds

- Replace the current display-time-only emoji indicators with proper
  `task_type`-based emoji mapping: 🆕 feature, 🐛 bugfix, ♻️ refactor,
  🧪 test, 📝 docs, 🔧 chore, 🔬 research, 📋 plan.
- Show type tags in: `/list-tasks`, tree view, `TaskReportView`, notification
  embeds, task detail view.
- Keep backward compatibility: if `task_type` is `None`, fall back to the
  current inferred indicators.
- **Files:** `src/command_handler.py`, `src/discord/commands.py`, `src/discord/notifications.py`, `src/discord/embeds.py`

### Milestone

> **M5:** Tasks display with stored `[type]` tags in all views. Plan-generated
> subtasks automatically receive inferred types. Manual type assignment available
> on `/add-task` and through ChatAgent.

---

## Phase 6: Enhanced Dependency Visualization

**Priority:** P3
**Estimated effort:** 4–5 hours
**Rationale:** Dependency info already appears in task detail views. This phase
makes it available in task list views and adds a dedicated `/task-deps` command.

### 6.1 Add `show_dependencies` option to `_cmd_list_tasks`

- Add `show_dependencies: bool = False` parameter.
- When `True`, include `depends_on` (list of task IDs + statuses) and `blocks`
  (list of dependent task IDs) in each task's returned data.
- Use existing `get_dependencies()` and `get_dependents()` DB methods.
- **Files:** `src/command_handler.py`

### 6.2 Build dependency-aware text formatter

- Create a formatter that annotates each task with its dependency relationships:
  ```
  🔵 #12: Set up database [READY]
     ↳ depends on: #10 (COMPLETED ✅), #11 (IN_PROGRESS 🟡)
  🟡 #14: Build API endpoints [IN_PROGRESS]
     ↳ blocks: #15, #16, #17
  ```
- Show dependencies only for tasks that have them (skip clean tasks).
- Use `↳ depends on:` and `↳ blocks:` prefix lines with status emojis.
- **Files:** `src/command_handler.py`

### 6.3 Integrate dependency annotations with tree view

- When tree view and dependency visualization are both active, annotate tree
  nodes:
  ```
  Task #12: Auth system [IN_PROGRESS]
  ├── #13: JWT middleware [COMPLETED] ✅
  ├── #14: Login endpoint [IN_PROGRESS] 🟡 (← blocks #17)
  └── #17: Registration [DEFINED] ⚪ (← needs #14)
  ```
- **Files:** `src/command_handler.py`

### 6.4 Add `/task-deps` slash command

- New slash command: `/task-deps <task_id>`.
- Shows: all upstream dependencies (what this task needs) and all downstream
  dependents (what this task blocks), with visual status of each.
- Use an embed with two field groups: "Depends On" and "Blocks".
- **Files:** `src/discord/commands.py`, `src/command_handler.py`

### 6.5 Add dependency tools to ChatAgent

- Update `list_tasks` tool to accept `show_dependencies` param.
- Add `get_task_dependencies` tool that returns the full dependency graph for
  a task.
- The LLM can then explain: "Task X is blocked because it depends on Y which
  is still in progress."
- **Files:** `src/chat_agent.py`

### Milestone

> **M6:** `/list-tasks` with dependency mode shows upstream/downstream
> relationships. `/task-deps <id>` provides a focused dependency view. Tree view
> integrates dependency annotations. ChatAgent can explain blocking chains.

---

## Phase 7: Multi-Device Support (Research & Prototype)

**Priority:** P2
**Estimated effort:** Research: 4–6 hours; Implementation: TBD
**Rationale:** Requested in UX feedback. Currently running two agent-queue
instances causes Discord action conflicts.

### 7.1 Document the current single-device architecture

- Map out how the bot routes messages and which state is device-specific vs
  shared.
- Identify all places where a "device" assumption exists in `bot.py`,
  `orchestrator.py`, and `config.py`.
- **Deliverable:** Architecture section in `notes/multi-device-design.md`

### 7.2 Design channel-to-device routing

- Propose a mapping scheme: `device_channels` config section that maps
  `device_name → discord_channel_id`.
- Define what "device" means: the machine running an agent-queue instance,
  identified by hostname or explicit config value.
- Design routing logic: notifications for device X go to device X's channel;
  commands from channel Y only processed by the instance assigned to Y.
- **Deliverable:** Design section in `notes/multi-device-design.md`

### 7.3 Evaluate conflict scenarios

- What happens when two instances both receive a Discord message?
- How to prevent double-processing of commands?
- Options: channel-based routing (only one instance listens per channel),
  message nonce deduplication, leader election.
- **Deliverable:** Conflict resolution section in design document

### 7.4 Prototype channel-per-device routing

- If the design is feasible, build a minimal proof of concept:
  - Config: `devices: {laptop: channel_123, desktop: channel_456}`
  - Bot: route notifications to the correct device channel
  - Bot: only process commands from the device's own channel
- This is gated on the research findings from 7.1–7.3.
- **Files:** `src/config.py`, `src/discord/bot.py`

### Milestone

> **M7:** Research document with architecture proposal and conflict resolution
> strategy. If feasible, a working prototype demonstrates channel-per-device
> routing.

---

## Phase 8: Unit Tests for Embeds, Notifications & Formatters

**Priority:** P2
**Estimated effort:** 4–5 hours
**Rationale:** The embed factory and notification formatters are pure functions
that are ideal for unit testing. Currently zero test coverage exists for these
modules.

### 8.1 Create `tests/test_embeds.py`

- Test `truncate()` with various inputs (under limit, at limit, over limit,
  empty string, custom suffix).
- Test `unix_timestamp()` with different datetime values and styles.
- Test `check_embed_size()` with embeds under, at, and over the 6,000-char
  limit.
- Test `make_embed()` produces correct structure (title with icon, color,
  footer, timestamp, field truncation, field count limit).
- Test all 5 convenience builders (`success_embed`, `error_embed`,
  `warning_embed`, `info_embed`, `critical_embed`) return correct styles.
- Test `status_embed()` maps task statuses to correct colors.
- Test `progress_bar()` edge cases: 0/0, 0/N, N/N, various percentages,
  different widths.
- Test `format_tree_task()` with various depths and character limits.
- **Files:** `tests/test_embeds.py`

### 8.2 Create `tests/test_notifications.py`

- Test all 8 string formatters produce non-empty output with valid inputs.
- Test all 8 embed formatters return `discord.Embed` objects with correct
  styles, titles, and fields.
- Test `classify_error()` matches all 13 error patterns correctly.
- Test embed size safety: create embeds with very long inputs and verify they
  don't exceed 6,000 chars.
- Test `TaskFailedView`, `TaskApprovalView`, `TaskBlockedView` have correct
  button counts and labels.
- **Files:** `tests/test_notifications.py`

### 8.3 Test tree view formatter

- Test various tree depths (1, 2, 3+ levels).
- Test truncation behavior at character limits.
- Test compact mode output.
- Test empty/single-task trees.
- **Files:** `tests/test_tree_view.py` or `tests/test_command_handler.py`

### 8.4 Integration tests for filtered task queries

- Test default filtering (active only).
- Test `include_completed=True` includes completed.
- Test `completed_only` shows only finished tasks.
- Test cross-project queries.
- **Files:** `tests/test_command_handler.py`

### Milestone

> **M8:** Full test coverage for `embeds.py` and `notifications.py`. Tree view
> formatter and filtered queries tested. All tests pass in CI.

---

## Phase 9: Polish, Visual QA & Documentation

**Priority:** P2
**Estimated effort:** 3–4 hours
**Rationale:** Final validation and documentation to close out the enhancement
effort.

### 9.1 Visual QA in Discord

- Test all new embeds on both desktop and mobile Discord clients.
- Verify inline fields don't break on mobile (they stack vertically).
- Verify tree view renders correctly in code blocks.
- Verify progress bars display correctly at various percentages.
- Test interactive buttons (Retry, Skip, Approve, Reply) work end-to-end.
- Test character limit handling with large task sets (50+ tasks).
- **Deliverable:** QA checklist with pass/fail per item

### 9.2 Update specifications

- Update `docs/specs/discord.md` with new commands (`/active-tasks`,
  `/task-deps`), new options (`view:tree`, `show_completed`), and new
  interactive views.
- Update `docs/specs/command-handler.md` with new command arguments
  (`include_completed`, `display_mode`, `show_dependencies`, `cross_project`).
- **Files:** `docs/specs/discord.md`, `docs/specs/command-handler.md`

### 9.3 Update architecture docs

- Update `CLAUDE.md` with any new architectural patterns (embed factory
  conventions, notification view attachment pattern, multi-device routing if
  implemented).
- Add entries to `docs/architecture.md` for the embed factory and notification
  view system.
- **Files:** `CLAUDE.md`, `docs/architecture.md`

### Milestone

> **M9:** All embeds visually verified on desktop + mobile. Documentation
> updated. Chat UI Enhancements project complete.

---

## Execution Order & Dependencies

```
Phase 1: Slash Command Error Consistency ─────────┐
                                                   │
Phase 2: Wire Missing Orchestrator Notifications ──┤
                                                   │
Phase 3: CommandHandler Active-Only + Cross-Project┤
                                                   ├──→ Phase 8: Unit Tests
Phase 4: Tree View as Display Mode ────────────────┤         │
                                                   │         ▼
Phase 5: Task Type Taxonomy ───────────────────────┤  Phase 9: Polish, QA & Docs
                                                   │
Phase 6: Enhanced Dependency Visualization ────────┘

Phase 7: Multi-Device Support ─── independent, can run in parallel with anything
```

### Dependencies Between Phases

- **Phase 3 depends on Phase 1** (error consistency should be done before adding
  more commands).
- **Phase 4 depends on Phase 3** (tree view uses `display_mode` param added in
  Phase 3).
- **Phase 6 depends on Phase 4** (dependency annotations integrate with tree
  view).
- **Phase 5 is independent** (task types can be added any time).
- **Phase 7 is independent** (research can run in parallel).
- **Phase 8 depends on Phases 1–6** (tests cover all implemented features).
- **Phase 9 depends on Phase 8** (QA validates test-covered features).

### Recommended Sprint Schedule

| Sprint | Phases | Focus | Est. Hours |
|--------|--------|-------|------------|
| Sprint 1 | Phase 1 + Phase 2 | Error consistency + missing notifications | 5–7h |
| Sprint 2 | Phase 3 | CommandHandler filtering + cross-project query | 3–4h |
| Sprint 3 | Phase 4 | Tree view as first-class display mode | 5–6h |
| Sprint 4 | Phase 5 + Phase 6 | Type tags + dependency visualization | 8–11h |
| Sprint 5 | Phase 7 | Multi-device research + prototype | 4–10h |
| Sprint 6 | Phase 8 + Phase 9 | Tests, QA, documentation | 7–9h |

**Total estimated effort: ~32–47 hours across ~40 subtasks**
(Reduced from original 53–60h estimate because many items are now done)

---

## Summary of All Implementable Tasks

| # | Task | Phase | Priority | Est. Hours | Files |
|---|------|-------|----------|------------|-------|
| 1.1 | Audit remaining plain-text error responses | 1 | P2 | 0.5h | `commands.py` |
| 1.2 | Convert ~72 errors to `_send_error()` + ephemeral | 1 | P2 | 2.5h | `commands.py` |
| 1.3 | Convert remaining success responses to factory embeds | 1 | P2 | 1h | `commands.py` |
| 2.1 | Wire `format_agent_question_embed` into orchestrator | 2 | P2 | 1h | `orchestrator.py` |
| 2.2 | Wire `format_budget_warning_embed` with rate limiting | 2 | P2 | 1.5h | `orchestrator.py`, `tokens/budget.py` |
| 2.3 | Create `AgentQuestionView` with modal reply button | 2 | P2 | 1h | `notifications.py`, `orchestrator.py` |
| 3.1 | Add `include_completed` param to `_cmd_list_tasks` | 3 | P2 | 1h | `command_handler.py` |
| 3.2 | Update slash command to delegate filtering | 3 | P2 | 0.5h | `commands.py` |
| 3.3 | Update ChatAgent `list_tasks` tool schema | 3 | P2 | 0.5h | `chat_agent.py` |
| 3.4 | Add cross-project active task query + `/active-tasks` | 3 | P2 | 1.5h | `command_handler.py`, `database.py`, `commands.py`, `chat_agent.py` |
| 4.1 | Add `get_task_tree` recursive DB helper | 4 | P2 | 1.5h | `database.py` |
| 4.2 | Build tree-view text formatter in command handler | 4 | P2 | 1.5h | `command_handler.py` |
| 4.3 | Add `display_mode` to `_cmd_list_tasks` | 4 | P2 | 1h | `command_handler.py` |
| 4.4 | Add tree mode to `/list-tasks` slash command | 4 | P2 | 0.5h | `commands.py` |
| 4.5 | Create `tree_view_embed()` helper | 4 | P2 | 0.5h | `embeds.py` |
| 4.6 | Update ChatAgent for tree view + `get_task_tree` tool | 4 | P2 | 0.5h | `chat_agent.py` |
| 5.1 | Add `task_type` field to Task model and DB | 5 | P3 | 1.5h | `models.py`, `database.py` |
| 5.2 | Manual type assignment at task creation | 5 | P3 | 1h | `command_handler.py`, `commands.py`, `chat_agent.py` |
| 5.3 | Auto-generate type tags in plan parser | 5 | P3 | 2h | `plan_parser.py` |
| 5.4 | Display stored type tags in all views | 5 | P3 | 1.5h | `command_handler.py`, `commands.py`, `notifications.py`, `embeds.py` |
| 6.1 | Add `show_dependencies` to `_cmd_list_tasks` | 6 | P3 | 1h | `command_handler.py` |
| 6.2 | Build dependency-aware text formatter | 6 | P3 | 1.5h | `command_handler.py` |
| 6.3 | Integrate dependency annotations with tree view | 6 | P3 | 1h | `command_handler.py` |
| 6.4 | Add `/task-deps` slash command | 6 | P3 | 1h | `commands.py`, `command_handler.py` |
| 6.5 | Add dependency tools to ChatAgent | 6 | P3 | 0.5h | `chat_agent.py` |
| 7.1 | Document current single-device architecture | 7 | P2 | 1h | Analysis only |
| 7.2 | Design channel-to-device routing | 7 | P2 | 2h | `notes/multi-device-design.md` |
| 7.3 | Evaluate conflict scenarios | 7 | P2 | 1.5h | Design doc section |
| 7.4 | Prototype channel-per-device routing | 7 | P2 | 2h | `config.py`, `bot.py` |
| 8.1 | Unit tests: embeds.py | 8 | P2 | 1.5h | `tests/test_embeds.py` |
| 8.2 | Unit tests: notifications.py | 8 | P2 | 1.5h | `tests/test_notifications.py` |
| 8.3 | Unit tests: tree view formatter | 8 | P2 | 1h | `tests/test_tree_view.py` |
| 8.4 | Integration tests: filtered queries | 8 | P2 | 1h | `tests/test_command_handler.py` |
| 9.1 | Visual QA on desktop + mobile Discord | 9 | P2 | 1h | Manual testing |
| 9.2 | Update specifications | 9 | P2 | 1h | `docs/specs/` |
| 9.3 | Update architecture docs | 9 | P2 | 1h | `CLAUDE.md`, `docs/architecture.md` |
