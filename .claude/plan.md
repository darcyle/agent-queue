# Chat UI Enhancements — Comprehensive Implementation Plan

This plan covers all work needed to improve readability, navigation, and user
experience when interacting with Agent Queue through Discord. It draws from
the `notes/chat-ui-enhancements.md` note, the
`notes/discord-responses-improvement-plan.md` research document, and audit of
the current codebase.

---

## Current State Audit

Before planning forward, it is essential to understand what has **already been
completed** so effort is not duplicated:

| Item | Status | Evidence |
|------|--------|----------|
| Embed factory (`src/discord/embeds.py`) | ✅ Done | Full module: `EmbedStyle`, `make_embed()`, convenience builders, `truncate()`, `unix_timestamp()`, `check_embed_size()` |
| Status color/emoji mappings | ✅ Done | `STATUS_COLORS` and `STATUS_EMOJIS` dicts in `embeds.py`, covering all 11 `TaskStatus` values |
| Notification embed formatters | ✅ Done | All 8 `format_*_embed()` functions in `notifications.py` |
| Orchestrator sends embeds | ✅ Done | `_notify_channel()` passes both text + embed; `bot.py._send_message()` accepts `embed` kwarg |
| Interactive UI components | ✅ Done | `NotesView`, `TaskReportView`, `StatusToggleButton`, `TaskDetailSelect` in `commands.py` |
| Threaded task updates | ✅ Partial | Task output streams to per-task threads; brief notifications reply to thread root |
| Task dependency DAG in DB | ✅ Done | `task_dependencies` table, `add_dependency()`, `get_dependencies()`, `get_blocking_dependencies()`, `get_dependents()`, `validate_dag()` |
| Parent/subtask model field | ✅ Done | `Task.parent_task_id` and `Task.is_plan_subtask` fields exist |

### What Remains

| Enhancement | Priority | Status |
|-------------|----------|--------|
| Subtask display improvements (tree view) | P2 | 🔲 Not started |
| Task query defaults (active-only filter) | P2 | 🔲 Not started |
| Better short descriptions (type tags) | P3 | 🔲 Not started |
| Task dependency visualization | P3 | 🔲 Not started |
| Multi-device support | P2 | 🔬 Needs research |
| Slash command consistency (embed factory) | P2 | 🔲 Not started |
| Rich progress bars | P3 | 🔲 Not started |
| Interactive action buttons on notifications | P3 | 🔲 Not started |

---

## Architecture Notes

### Key Files

| File | Role | Lines |
|------|------|-------|
| `src/command_handler.py` | Unified command execution (business logic) | ~1,980 |
| `src/chat_agent.py` | LLM tool definitions and response formatting | ~1,371 |
| `src/discord/commands.py` | Discord slash commands (50+), UI components | ~2,844 |
| `src/discord/bot.py` | Bot core, message routing, history compaction | ~813 |
| `src/discord/notifications.py` | Task lifecycle formatters (text + embed) | ~430 |
| `src/discord/embeds.py` | Centralized embed factory | ~394 |
| `src/database.py` | SQLite persistence, dependency queries | ~1,259 |
| `src/models.py` | Task, Agent, Project dataclasses | varies |
| `src/state_machine.py` | Task state transitions, DAG validation | varies |

### Design Principles

1. **CommandHandler is the single code path** — all business logic goes here;
   Discord commands and ChatAgent are thin presentation layers.
2. **Embed factory is the single source of truth** — all embeds flow through
   `src/discord/embeds.py`.
3. **Discord API hard limits** — 2,000 chars per message, 6,000 chars total
   per embed, 25 fields per embed. All formatting must respect these.
4. **Backward compatibility** — string formatters are preserved alongside
   embeds for logging and testing.

### Existing Data Model Support

- `Task.parent_task_id: str | None` — links subtask to parent
- `Task.is_plan_subtask: bool` — distinguishes auto-generated plan subtasks
- `task_dependencies` table — explicit DAG edges between tasks
- `database.get_dependencies()`, `get_dependents()`, `get_blocking_dependencies()` — query helpers already exist

---

## Phase 1: Task Query Defaults (Active-Only Filtering)

**Priority:** P2
**Estimated effort:** 3–4 hours
**Rationale:** Highest daily-use impact; every `list tasks` call benefits.

### Subtasks

#### 1.1 Add status filtering to `_cmd_list_tasks` in `command_handler.py`

Currently `_cmd_list_tasks` accepts an optional `status` filter but defaults
to returning **all** tasks. Change the default behavior:

- When no `status` argument is provided and no `--all` flag is set, return
  only tasks with status in `{DEFINED, READY, ASSIGNED, IN_PROGRESS,
  WAITING_INPUT, PAUSED, VERIFYING, AWAITING_APPROVAL}` — i.e., exclude
  `COMPLETED`, `FAILED`, and `BLOCKED`.
- Add a new `include_completed` boolean argument (default `False`). When
  `True`, return all tasks regardless of status.
- Add a `completed_only` boolean argument for explicitly viewing finished
  tasks.
- Preserve existing `status` filter as an override (when set, it takes
  precedence over defaults).

**Files:** `src/command_handler.py`

#### 1.2 Add `--all` and `--completed` flags to the `/list-tasks` slash command

- Add `all_tasks: bool = False` option to the slash command.
- Add `completed: bool = False` option.
- Pass these through to `CommandHandler.execute("list_tasks", ...)`.
- Update the ephemeral footer to indicate filtering is active (e.g.,
  "Showing active tasks. Use `/list-tasks all:True` to see all.").

**Files:** `src/discord/commands.py`

#### 1.3 Update ChatAgent `list_tasks` tool schema

- Add `include_completed` and `show_all` parameters to the LLM tool
  definition.
- Update the tool's system prompt description to explain that completed tasks
  are hidden by default.
- Ensure the LLM naturally says "I see N active tasks" rather than listing
  hundreds of completed ones.

**Files:** `src/chat_agent.py`

#### 1.4 Add cross-project active task query

- Add a new command `_cmd_list_active_tasks_all_projects` (or extend
  `list_tasks` with `cross_project=True` flag).
- Query: all tasks with non-terminal status across all projects.
- Group results by project for readability.
- Add corresponding slash command `/active-tasks` or flag on `/list-tasks`.

**Files:** `src/command_handler.py`, `src/database.py`, `src/discord/commands.py`, `src/chat_agent.py`

### Milestone

> **M1:** Running `/list-tasks` shows only active/pending tasks. Completed
> tasks require `--all` or `--completed`. Cross-project query is available.

---

## Phase 2: Subtask Display Improvements (Tree View)

**Priority:** P2
**Estimated effort:** 6–8 hours
**Rationale:** Core readability improvement for understanding task hierarchy.

### Subtasks

#### 2.1 Add `get_subtasks` and `get_task_tree` database helpers

- `get_subtasks(parent_task_id)` → list of tasks where
  `parent_task_id = ?`.
- `get_task_tree(root_task_id)` → recursive query returning a nested tree
  structure (root + all descendants).
- `get_parent_tasks(project_id)` → tasks where `parent_task_id IS NULL` for
  a given project (top-level tasks only).

**Files:** `src/database.py`

#### 2.2 Build tree-view formatter in `command_handler.py`

Create a utility function that renders a task tree as a Unicode tree string:

```
Task #12: Implement authentication system [IN_PROGRESS]
├── #13: Set up JWT middleware [COMPLETED] ✅
├── #14: Create login endpoint [IN_PROGRESS] 🟡
│   ├── #15: Add input validation [READY] 🔵
│   └── #16: Write tests [DEFINED] ⚪
└── #17: Create registration endpoint [DEFINED] ⚪

3/5 subtasks complete
```

Implementation notes:
- Use `├──`, `└──`, `│` box-drawing characters for tree branches.
- Append status emoji from `STATUS_EMOJIS`.
- Include a summary line: `X/Y subtasks complete`.
- Implement truncation for trees exceeding Discord's 2,000 char limit:
  collapse deep nesting to `... (N more subtasks)`.
- Support compact mode (parent + summary counts only) vs. expanded mode
  (full tree).

**Files:** `src/command_handler.py` (new formatter function)

#### 2.3 Add `tree` display mode to `_cmd_list_tasks`

- Add `display_mode` argument: `"flat"` (default, current behavior),
  `"tree"`, or `"compact"`.
- When `"tree"`: group tasks by parent, render tree for each root task.
- When `"compact"`: show only parent tasks with subtask count summary.
- Flat mode remains the default for backward compatibility.

**Files:** `src/command_handler.py`

#### 2.4 Add `/list-tasks` tree mode to Discord commands

- Add `view` option to the slash command with choices:
  `"list"` (default), `"tree"`, `"compact"`.
- When `"tree"`, send the tree-formatted output in a code block for
  monospace alignment.
- Handle pagination: if the tree exceeds 2,000 chars, split across
  multiple messages or use an embed with scrollable description.

**Files:** `src/discord/commands.py`

#### 2.5 Add tree view embed for Discord

- Create `tree_view_embed()` in `embeds.py` (or as a helper in
  `commands.py`) that places the tree in the embed description inside a
  code block.
- Use embed fields for the summary line and metadata.
- Respect the 4,096-char description limit; paginate if needed.

**Files:** `src/discord/embeds.py` or `src/discord/commands.py`

#### 2.6 Update ChatAgent tool response for tree view

- When the LLM calls `list_tasks` with `display_mode="tree"`, return the
  pre-formatted tree string so the LLM can present it naturally.
- Add a new tool `get_task_tree` that returns the hierarchy for a specific
  parent task.

**Files:** `src/chat_agent.py`

### Milestone

> **M2:** Users can run `/list-tasks view:tree` to see task hierarchy as a
> tree. Compact mode shows parents with subtask counts. Tree output respects
> Discord character limits with automatic truncation.

---

## Phase 3: Slash Command Consistency (Embed Standardization)

**Priority:** P2
**Estimated effort:** 4–5 hours
**Rationale:** Cleans up visual inconsistency across all 50+ commands.

### Subtasks

#### 3.1 Audit all slash commands for embed usage

- Inventory every `interaction.response.send_message()` call in
  `commands.py`.
- Categorize each as: (a) already using embed factory, (b) using inline
  `discord.Embed()`, (c) plain text.
- Produce a checklist of commands to convert.

**Files:** `src/discord/commands.py` (read-only audit)

#### 3.2 Convert success responses to `success_embed()`

- Replace all inline `discord.Embed(color=...)` patterns for successful
  operations with `success_embed()` from the factory.
- Ensure all success embeds have consistent title format, fields, and
  branding.

**Files:** `src/discord/commands.py`

#### 3.3 Convert error responses to `error_embed()` + ephemeral

- Replace all `f"Error: {result['error']}"` plain-text responses with
  `error_embed(title="...", description=result['error'])`.
- Ensure **every** error response is ephemeral (`ephemeral=True`).

**Files:** `src/discord/commands.py`

#### 3.4 Convert status-specific responses to `status_embed()`

- Any response that displays or changes a task status should use
  `status_embed()` with the appropriate status string.
- Examples: task creation, status update, task details display.

**Files:** `src/discord/commands.py`

#### 3.5 Replace `discord.Embed()` in TaskReportView

- `TaskReportView` builds content as a plain text string with markdown
  headers. Evaluate converting the grouped task list into a series of
  status-colored embeds.
- Note: This may require using `discord.ui.View` with multiple embeds or
  keeping the current text-based approach for long lists.

**Files:** `src/discord/commands.py`

### Milestone

> **M3:** All 50+ slash commands use the embed factory consistently. Errors
> are always ephemeral embeds. Success/info/warning responses all carry
> AgentQueue branding and timestamps.

---

## Phase 4: Better Short Descriptions (Type Tags)

**Priority:** P3
**Estimated effort:** 4–6 hours
**Rationale:** Improves quick scanning; works synergistically with tree view.

### Subtasks

#### 4.1 Define task type taxonomy and validation

- Define allowed type tags: `[feature]`, `[bugfix]`, `[refactor]`,
  `[test]`, `[docs]`, `[chore]`, `[research]`, `[plan]`.
- Add a `task_type` field to the `Task` dataclass in `models.py`
  (optional string, default `None`).
- Add corresponding database column via migration in `database.py`.

**Files:** `src/models.py`, `src/database.py`

#### 4.2 Generate type tags during plan parsing

- When the plan parser creates subtasks, infer the type tag from the
  phase title and description (e.g., "Add tests for..." → `[test]`).
- Use a keyword-matching heuristic or a brief LLM classification.
- Store the inferred type in `task_type`.

**Files:** `src/plan_parser.py` or `src/plan_parser_llm.py`

#### 4.3 Allow manual type assignment at task creation

- Add `task_type` parameter to `_cmd_create_task`.
- Add `type` option to `/add-task` slash command with autocomplete
  choices.
- Update ChatAgent `create_task` tool schema.

**Files:** `src/command_handler.py`, `src/discord/commands.py`, `src/chat_agent.py`

#### 4.4 Display type tags in task listings and embeds

- Prepend `[type]` tag to task titles in all listing formats:
  `/list-tasks`, tree view, `TaskReportView`, notification embeds.
- Add emoji mapping for types: 🆕 feature, 🐛 bugfix, ♻️ refactor,
  🧪 test, 📝 docs, 🔧 chore, 🔬 research, 📋 plan.

**Files:** `src/command_handler.py`, `src/discord/commands.py`, `src/discord/notifications.py`, `src/discord/embeds.py`

#### 4.5 Auto-generate concise titles via LLM

- When a task description is long (>100 chars) and no explicit title is
  short enough, optionally use the LLM to generate a one-line summary.
- This is lower priority and should be opt-in via config.

**Files:** `src/chat_agent.py`, `src/command_handler.py`

### Milestone

> **M4:** Tasks display with `[type]` tags in all views. Plan-generated
> subtasks automatically receive inferred types. Manual type assignment
> available on `/add-task`.

---

## Phase 5: Task Dependency Visualization

**Priority:** P3
**Estimated effort:** 5–7 hours
**Rationale:** Critical for understanding task ordering and blocked chains.

### Subtasks

#### 5.1 Enhance `_cmd_list_tasks` to include dependency info

- When returning task data, optionally include `depends_on` (list of
  task IDs) and `blocking` (list of task IDs that depend on this task).
- Add `show_dependencies: bool = False` argument.
- Use existing `get_dependencies()` and `get_dependents()` DB methods.

**Files:** `src/command_handler.py`, `src/database.py`

#### 5.2 Build dependency-aware text formatter

Create a formatter that shows dependency relationships:

```
🔵 #12: Set up database [READY]
   ↳ depends on: #10 (COMPLETED ✅), #11 (IN_PROGRESS 🟡)
🟡 #14: Build API endpoints [IN_PROGRESS]
   ↳ blocks: #15, #16, #17
⛔ #18: Deploy to staging [BLOCKED]
   ↳ waiting on: #14 (IN_PROGRESS 🟡), #17 (DEFINED ⚪)
```

- Show dependencies only for tasks that have them (skip clean tasks).
- Use `↳ depends on:` and `↳ blocks:` prefix lines.
- Color-code dependency status with emojis.

**Files:** `src/command_handler.py` (new formatter)

#### 5.3 Integrate dependency view with tree view

- When both tree view and dependency visualization are active, show
  dependencies as annotations on tree nodes.
- Example:
  ```
  Task #12: Auth system [IN_PROGRESS]
  ├── #13: JWT middleware [COMPLETED] ✅
  ├── #14: Login endpoint [IN_PROGRESS] 🟡 (← blocks #17)
  └── #17: Registration [DEFINED] ⚪ (← needs #14)
  ```

**Files:** `src/command_handler.py`

#### 5.4 Add `/task-deps` slash command

- New slash command: `/task-deps <task_id>` that shows:
  - All upstream dependencies (what this task needs).
  - All downstream dependents (what this task blocks).
  - Visual status of each.
- Use an embed with two field groups: "Depends On" and "Blocks".

**Files:** `src/discord/commands.py`, `src/command_handler.py`

#### 5.5 Add dependency info to ChatAgent tools

- Update `list_tasks` tool to accept `show_dependencies` param.
- Add new `get_task_dependencies` tool that returns the full dependency
  graph for a task.
- LLM can then explain: "Task X is blocked because it depends on Y which
  is still in progress."

**Files:** `src/chat_agent.py`

### Milestone

> **M5:** Running `/list-tasks` with dependency mode shows upstream/downstream
> relationships. `/task-deps <id>` provides a focused dependency view.
> Tree view integrates dependency annotations. ChatAgent can explain blocking
> chains naturally.

---

## Phase 6: Rich Progress Bars

**Priority:** P3
**Estimated effort:** 2–3 hours
**Rationale:** Quick visual enhancement for multi-step tasks.

### Subtasks

#### 6.1 Create progress bar utility in `embeds.py`

Build a Unicode progress bar generator:

```python
def progress_bar(completed: int, total: int, width: int = 10) -> str:
    """Render a text progress bar using Unicode block chars.

    Example: ████████░░ 80% (8/10)
    """
```

- Use `█` (full block) and `░` (light shade) characters.
- Include percentage and fraction.
- Configurable width (default 10 chars).

**Files:** `src/discord/embeds.py`

#### 6.2 Show progress bar in task tree compact view

- When displaying a parent task in compact mode, include a progress bar
  showing subtask completion:
  ```
  Task #12: Auth system ████████░░ 80% (4/5 subtasks)
  ```

**Files:** `src/command_handler.py`

#### 6.3 Add progress bar to project status embed

- The `/status` command or project overview should show a progress bar
  for each project:
  ```
  my-app:     ██████░░░░ 60% (12/20 tasks)
  api-server: ████████░░ 80% (8/10 tasks)
  ```

**Files:** `src/discord/commands.py`, `src/command_handler.py`

### Milestone

> **M6:** Progress bars appear in compact task views and project status.
> Visual representation of completion percentage at a glance.

---

## Phase 7: Interactive Action Buttons on Notifications

**Priority:** P3
**Estimated effort:** 4–5 hours
**Rationale:** Reduces typing; common actions become one-click.

### Subtasks

#### 7.1 Create `TaskActionView` component

- A `discord.ui.View` with buttons for common task actions:
  - **Restart** (🔄) — calls `restart_task`
  - **Skip** (⏭️) — calls `skip_task`
  - **Details** (📋) — shows task details
  - **Output** (📄) — shows agent output
- View is parameterized with `task_id` and `project_id`.
- Buttons call `CommandHandler.execute()` directly.

**Files:** `src/discord/commands.py` (new View class)

#### 7.2 Attach action buttons to failure/blocked notifications

- When a task fails or is blocked, attach `TaskActionView` to the
  notification message so users can restart or skip without typing.
- Use appropriate button styling:
  - Restart → green (SUCCESS)
  - Skip → gray (SECONDARY)
  - Details → blue (PRIMARY)

**Files:** `src/orchestrator.py` (notification calls), `src/discord/bot.py`

#### 7.3 Add approve/reject buttons to PR notifications

- When a PR is created and the task is AWAITING_APPROVAL, attach buttons:
  - **Approve** → completes the task
  - **View PR** → link button to GitHub
  - **Reject** → blocks the task
- This replaces the current text-only "Use `/approve-task`" instruction.

**Files:** `src/discord/commands.py`, `src/orchestrator.py`

#### 7.4 Add answer button for agent questions

- When an agent asks a question (WAITING_INPUT), attach a modal trigger
  button that opens a text input for the user's response.
- This is more ergonomic than requiring the user to type a reply in chat.

**Files:** `src/discord/commands.py`, `src/orchestrator.py`

### Milestone

> **M7:** Task failure notifications include Restart/Skip/Details buttons.
> PR notifications include Approve/Reject buttons. Agent questions have a
> modal reply button.

---

## Phase 8: Multi-Device Support (Research Phase)

**Priority:** P2
**Estimated effort:** Research: 4–6 hours; Implementation: TBD

### Research Subtasks

#### 8.1 Document the current single-device architecture

- Map out exactly how the bot routes messages and which state is
  device-specific vs. shared.
- Identify all places where a "device" assumption exists.

**Files:** Read-only analysis of `bot.py`, `orchestrator.py`, `config.py`

#### 8.2 Design channel-to-device routing

- Propose a mapping scheme: e.g., `device_channels` config section that
  maps `device_name → discord_channel_id`.
- Define what "device" means: the machine running an agent-queue instance,
  identified by hostname or explicit config.
- Design the routing logic: when a notification is for device X, send to
  device X's channel.

**Deliverable:** Design document in `notes/multi-device-design.md`

#### 8.3 Evaluate conflict scenarios

- What happens when two instances both receive a Discord message?
- How to prevent double-processing of commands?
- Options: leader election, channel-based routing (only one instance
  listens per channel), or message nonce deduplication.

**Deliverable:** Section in design document covering conflict resolution

#### 8.4 Prototype channel-per-device routing

- If the design is feasible, build a minimal proof of concept:
  - Config: `devices: {laptop: channel_123, desktop: channel_456}`
  - Bot: route notifications to the correct device channel
  - Bot: only process commands from the device's own channel
- This is gated on the research findings.

**Files:** `src/config.py`, `src/discord/bot.py`

### Milestone

> **M8:** Research document produced with architecture proposal and conflict
> resolution strategy. If feasible, a working prototype demonstrates
> channel-per-device routing.

---

## Phase 9: Polish & Testing

**Priority:** P2
**Estimated effort:** 3–4 hours (ongoing)

### Subtasks

#### 9.1 Unit tests for tree view formatter

- Test various tree depths (1, 2, 3+ levels).
- Test truncation behavior at character limits.
- Test compact mode output.
- Test empty/single-task trees.

**Files:** `tests/test_command_handler.py` or new `tests/test_tree_view.py`

#### 9.2 Unit tests for progress bar utility

- Test edge cases: 0/0, 0/N, N/N, percentages.
- Test different widths.

**Files:** `tests/test_embeds.py`

#### 9.3 Unit tests for dependency formatter

- Test tasks with no deps, single deps, multiple deps.
- Test circular reference handling (should not occur but be safe).
- Test integration with tree view.

**Files:** `tests/test_command_handler.py`

#### 9.4 Integration tests for filtered task queries

- Test default filtering (active only).
- Test `--all` flag includes completed.
- Test `--completed` shows only finished tasks.
- Test cross-project queries.

**Files:** `tests/test_command_handler.py`

#### 9.5 Visual QA in Discord

- Test all new embeds on both desktop and mobile Discord clients.
- Verify inline fields don't break on mobile (they stack vertically).
- Verify tree view renders correctly in code blocks.
- Test character limit handling with large task sets.

**Deliverable:** QA checklist with pass/fail

#### 9.6 Update documentation

- Update `specs/discord/discord.md` with new commands and options.
- Update `specs/command-handler.md` with new command arguments.
- Update `CLAUDE.md` with any new architectural patterns.

**Files:** `specs/discord/discord.md`, `specs/command-handler.md`, `CLAUDE.md`

---

## Execution Order & Dependencies

```
Phase 1: Task Query Defaults ─────────────────┐
                                               ├──→ Phase 9: Polish & Testing
Phase 2: Subtask Display (Tree View) ────┬─────┤
                                         │     │
Phase 3: Slash Command Consistency ──────┤     │
                                         │     │
Phase 4: Better Short Descriptions ──────┤     │
                                         │     │
Phase 5: Task Dependency Visualization ──┘     │
                                               │
Phase 6: Rich Progress Bars ───────────────────┤
                                               │
Phase 7: Interactive Action Buttons ───────────┘

Phase 8: Multi-Device (Research) ─── independent, can run in parallel
```

**Critical path:** Phases 1 → 2 → 5 (each builds on the previous).
Phases 3, 4, 6, 7, and 8 can be executed in parallel with the critical path.

### Recommended Sprint Schedule

| Sprint | Phases | Focus |
|--------|--------|-------|
| Sprint 1 | Phase 1 + Phase 3 | Query defaults + embed consistency (daily-use improvements) |
| Sprint 2 | Phase 2 | Tree view (core readability) |
| Sprint 3 | Phase 4 + Phase 5 | Type tags + dependency visualization |
| Sprint 4 | Phase 6 + Phase 7 | Progress bars + action buttons (polish) |
| Sprint 5 | Phase 8 | Multi-device research + prototype |
| Sprint 6 | Phase 9 | Testing, QA, documentation |

---

## Summary of All Implementable Tasks

| # | Task | Phase | Priority | Est. Hours | Files |
|---|------|-------|----------|------------|-------|
| 1.1 | Add status filtering defaults to `_cmd_list_tasks` | 1 | P2 | 1h | `command_handler.py` |
| 1.2 | Add `--all`/`--completed` flags to `/list-tasks` | 1 | P2 | 1h | `commands.py` |
| 1.3 | Update ChatAgent `list_tasks` tool schema | 1 | P2 | 0.5h | `chat_agent.py` |
| 1.4 | Add cross-project active task query | 1 | P2 | 1.5h | `command_handler.py`, `database.py`, `commands.py`, `chat_agent.py` |
| 2.1 | Add `get_subtasks`/`get_task_tree` DB helpers | 2 | P2 | 1.5h | `database.py` |
| 2.2 | Build tree-view Unicode formatter | 2 | P2 | 2h | `command_handler.py` |
| 2.3 | Add `display_mode` to `_cmd_list_tasks` | 2 | P2 | 1h | `command_handler.py` |
| 2.4 | Add tree mode to `/list-tasks` slash command | 2 | P2 | 1h | `commands.py` |
| 2.5 | Create tree view embed for Discord | 2 | P2 | 1h | `embeds.py` / `commands.py` |
| 2.6 | Update ChatAgent for tree view | 2 | P2 | 1h | `chat_agent.py` |
| 3.1 | Audit slash commands for embed usage | 3 | P2 | 1h | `commands.py` (read) |
| 3.2 | Convert success responses to `success_embed()` | 3 | P2 | 1.5h | `commands.py` |
| 3.3 | Convert errors to `error_embed()` + ephemeral | 3 | P2 | 1.5h | `commands.py` |
| 3.4 | Convert status responses to `status_embed()` | 3 | P2 | 1h | `commands.py` |
| 3.5 | Evaluate `TaskReportView` embed conversion | 3 | P2 | 1h | `commands.py` |
| 4.1 | Define task type taxonomy + model/DB field | 4 | P3 | 1.5h | `models.py`, `database.py` |
| 4.2 | Auto-generate type tags in plan parser | 4 | P3 | 2h | `plan_parser.py` / `plan_parser_llm.py` |
| 4.3 | Manual type assignment at task creation | 4 | P3 | 1h | `command_handler.py`, `commands.py`, `chat_agent.py` |
| 4.4 | Display type tags in listings and embeds | 4 | P3 | 1.5h | `command_handler.py`, `commands.py`, `notifications.py`, `embeds.py` |
| 4.5 | Auto-generate concise titles via LLM (opt-in) | 4 | P3 | 1h | `chat_agent.py`, `command_handler.py` |
| 5.1 | Include dependency info in `_cmd_list_tasks` | 5 | P3 | 1h | `command_handler.py`, `database.py` |
| 5.2 | Build dependency-aware text formatter | 5 | P3 | 2h | `command_handler.py` |
| 5.3 | Integrate deps with tree view | 5 | P3 | 1.5h | `command_handler.py` |
| 5.4 | Add `/task-deps` slash command | 5 | P3 | 1.5h | `commands.py`, `command_handler.py` |
| 5.5 | Add dependency tools to ChatAgent | 5 | P3 | 1h | `chat_agent.py` |
| 6.1 | Create progress bar utility | 6 | P3 | 0.5h | `embeds.py` |
| 6.2 | Show progress in compact tree view | 6 | P3 | 1h | `command_handler.py` |
| 6.3 | Add progress bar to project status | 6 | P3 | 1h | `commands.py`, `command_handler.py` |
| 7.1 | Create `TaskActionView` component | 7 | P3 | 1.5h | `commands.py` |
| 7.2 | Attach action buttons to failure notifications | 7 | P3 | 1.5h | `orchestrator.py`, `bot.py` |
| 7.3 | Add approve/reject buttons to PR notifications | 7 | P3 | 1h | `commands.py`, `orchestrator.py` |
| 7.4 | Add modal reply for agent questions | 7 | P3 | 1h | `commands.py`, `orchestrator.py` |
| 8.1 | Document current single-device architecture | 8 | P2 | 1h | Analysis only |
| 8.2 | Design channel-to-device routing | 8 | P2 | 2h | `notes/multi-device-design.md` |
| 8.3 | Evaluate conflict scenarios | 8 | P2 | 1.5h | Design doc section |
| 8.4 | Prototype channel-per-device routing | 8 | P2 | 2h | `config.py`, `bot.py` |
| 9.1 | Unit tests: tree view formatter | 9 | P2 | 1h | `tests/` |
| 9.2 | Unit tests: progress bar utility | 9 | P2 | 0.5h | `tests/` |
| 9.3 | Unit tests: dependency formatter | 9 | P2 | 1h | `tests/` |
| 9.4 | Integration tests: filtered queries | 9 | P2 | 1h | `tests/` |
| 9.5 | Visual QA in Discord | 9 | P2 | 1h | Manual testing |
| 9.6 | Update documentation | 9 | P2 | 1h | `specs/`, `CLAUDE.md` |

**Total estimated effort: ~53–60 hours across 39 subtasks**
