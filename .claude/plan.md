---
auto_tasks: true
---

# Plan: Dynamic Per-Project Agent Pools

## Background & Design

### Current State
- Agents are globally defined entities stored in an `agents` table, created manually via `/create-agent`
- Any agent can work on any project's tasks — they're a shared pool
- The scheduler picks an IDLE agent + READY task and creates an `AssignAction`
- Workspaces are per-project and dynamically acquired by agents via optimistic locking
- Agent lifecycle: CREATE → IDLE → BUSY → IDLE → ... → DELETE

### Target State
- No persistent global agent registry — agents are ephemeral, created on-demand per project
- Each project has its own agent pool, sized by its workspace count
- A project with 3 workspaces gets up to 3 concurrent agents
- Agent type is always `"claude"` (configurable later via project settings)
- `/create-agent` and `/delete-agent` commands are removed
- `/agents` (or `/list-agents`) becomes project-scoped, showing only agents for that project
- Agent identity is derived from workspace assignment (e.g., `{project_id}-agent-{n}` or workspace-based)

### Key Design Decisions

1. **Agents = Workspace Slots**: Rather than managing agents as separate entities, an "agent" is simply an active execution context tied to a locked workspace. When a workspace is locked for a task, that *is* the agent.

2. **Virtual Agent Model**: We keep a lightweight representation of "agents" for display purposes (`/agents` command), but they aren't persistent DB entities requiring manual lifecycle management. An "agent" is a workspace — idle workspaces are idle agents, locked workspaces are busy agents.

3. **Scheduler Changes**: Instead of finding IDLE agents from a global pool, the scheduler finds projects with READY tasks and available (unlocked) workspaces, then creates ephemeral agent contexts. The `AssignAction` uses a workspace-derived agent ID.

4. **Backwards Compatibility**: The `token_ledger` and `task_results` tables currently reference `agent_id`. We'll use workspace-derived IDs (e.g., `ws-{workspace_id}`) as the agent_id for these records.

5. **Agent Type**: Always `"claude"` for now. Later, a `default_agent_type` field on the project model can override this.

### What Stays the Same
- Workspace model and locking mechanism (this is the foundation)
- Adapter layer (still creates ClaudeAdapter instances per task)
- Profile resolution (task → project → default)
- Task lifecycle and state machine
- Rate limit retry logic
- The scheduler's proportional fair-share algorithm (adapted to not need agent list)

### Key Files Reference
- `src/models.py` — `Agent` dataclass (lines 257-274), `AgentState` enum (lines 112-124), `Workspace` dataclass (lines 277-293)
- `src/database.py` — `agents` table schema (lines 120-133), agent CRUD methods (lines 1207-1304), `workspaces` table (lines 180-191), `acquire_workspace()` (lines 1369-1464)
- `src/scheduler.py` — `SchedulerState` (lines 88-150+), `AssignAction` (lines 75-86), `Scheduler.schedule()`
- `src/orchestrator.py` — `_schedule()` (line 1520), `_execute_task()` (line 3344), `_free_agent()`, `_prepare_workspace()` (line 1638)
- `src/command_handler.py` — `_cmd_create_agent` (lines 4044-4062), `_cmd_delete_agent` (lines 4503-4518), `_cmd_list_agents` (lines 4021-4042), `_cmd_pause_agent`/`_cmd_resume_agent` (lines 4470-4501)
- `src/adapters/__init__.py` — `AdapterFactory` (lines 20-59)
- `src/agent_names.py` — Agent name generation

---

## Phase 1: Remove Global Agent CRUD Commands and Introduce Workspace-Agent Identity

Remove `/create-agent`, `/delete-agent`, `/pause-agent`, `/resume-agent` commands. Replace the `Agent` model with a lightweight `WorkspaceAgent` view model for display purposes.

**Files to modify:**

- **`src/models.py`**:
  - Remove `Agent` dataclass and `AgentState` enum
  - Keep `AgentResult` enum (used for task results, not agent lifecycle)
  - Add `WorkspaceAgent` dataclass for display:
    ```python
    @dataclass
    class WorkspaceAgent:
        workspace_id: str
        project_id: str
        workspace_name: str | None
        state: str  # "idle" or "busy"
        current_task_id: str | None = None
        current_task_title: str | None = None
    ```

- **`src/command_handler.py`**:
  - Remove `_cmd_create_agent`, `_cmd_delete_agent`, `_cmd_pause_agent`, `_cmd_resume_agent` methods and their command registrations
  - Rewrite `_cmd_list_agents` to be project-scoped (require `project_id`), build agent view from workspaces:
    1. List all workspaces for the project via `db.list_workspaces(project_id=project_id)`
    2. For locked workspaces, look up task via `locked_by_task_id` → show as "busy"
    3. For unlocked workspaces → show as "idle"
    4. Return list of `WorkspaceAgent`-style dicts
  - Remove agent-related tool definitions from the supervisor tool registry (tools exposed to the chat LLM for creating/deleting agents)

- **`src/agent_names.py`** — Keep for now (may be useful for workspace display names), but remove any imports in the removed command methods.

- **`src/tool_registry.py`** — Remove tool definitions for `create_agent`, `delete_agent`, `pause_agent`, `resume_agent` if they're registered there.

---

## Phase 2: Update Scheduler to Use Workspace-Based Capacity

Remove the need for an `agents` list in the scheduler. Available capacity is determined by workspace availability.

**Files to modify:**

- **`src/scheduler.py`**:
  - Remove `agents` field from `SchedulerState`
  - The scheduler already tracks `workspace_available` per project and `active_agents_per_project` — these are sufficient
  - Currently the scheduler matches IDLE agents to tasks. Instead, it should determine how many more agents each project can run (= available workspaces, minus any per-project cap) and create `AssignAction` entries without a real agent_id
  - `AssignAction.agent_id` can be set to a placeholder (e.g., `"ephemeral"`) since the real identity comes from workspace acquisition. Or generate a UUID-based ID.
  - The key constraint becomes: a project can run N concurrent tasks where N = min(available_workspaces, max_concurrent_agents)
  - Remove any logic that iterates over idle agents to find matches

- **`src/orchestrator.py` `_schedule()` method**:
  - Stop fetching `agents = await self.db.list_agents()`
  - Build `SchedulerState` without agents
  - The number of "idle agents" is effectively the total available workspaces across all projects (or can be computed per-project)

---

## Phase 3: Update Orchestrator Task Execution Pipeline

Refactor the execution pipeline to work without the `agents` table. The "agent" is an ephemeral context created when a workspace is acquired.

**Files to modify:**

- **`src/orchestrator.py`**:
  - `_execute_task_safe()` / `_execute_task()`: Instead of using a pre-existing `agent_id` from the agents table, generate an ephemeral agent identifier from the workspace (e.g., `f"ws-{workspace.id}"` or `f"{project_id}-{workspace.name}"`)
  - Remove calls to `db.assign_task_to_agent()` — replace with direct task status update + workspace acquisition
  - `self._adapters` dict: Key by `task_id` instead of `agent_id` (simplifies stop-task lookups)
  - `_free_agent()`: Replace with workspace release + task status update. No agent state to update.
  - Remove agent heartbeat/liveness checks — workspace lock staleness can be checked instead if needed
  - Token recording in `token_ledger`: Use the workspace-derived agent_id string (this is just for record-keeping, no FK constraint needed)
  - Ensure `/stop-task` admin command looks up adapter by `task_id`

- **`src/database.py`**:
  - `assign_task_to_agent()`: Simplify to just update task status (READY → ASSIGNED/IN_PROGRESS) and set `assigned_agent_id` to the workspace-derived ID. Don't update any agents table.
  - Or rename to `assign_task(task_id, agent_label)` to clarify it no longer touches an agents row.
  - Remove `create_agent()`, `get_agent()`, `list_agents()`, `update_agent()`, `delete_agent()` methods
  - Drop the `agents` table from the schema (add migration)
  - Update `workspaces` table: `locked_by_agent_id` becomes a plain text column (no FK to agents table). Keep the column name for compatibility but it stores the workspace-derived agent label.
  - `token_ledger.agent_id` and `task_results.agent_id`: Remove FK constraint, keep as plain text

---

## Phase 4: Update `/agents` Display and Discord Formatting

Make the agents view project-scoped and workspace-based.

**Files to modify:**

- **`src/command_handler.py`** — The rewritten `_cmd_list_agents` from Phase 1. Ensure it:
  - Requires project context (from active project in chat, or explicit project_id parameter)
  - Shows each workspace as an "agent slot" with status
  - For busy agents, shows task ID, title, and brief description
  - Example output format:
    ```
    Agents for "my-app" (3 slots):
      my-app-1 (ws-main)     BUSY  — "Add rate limiting" (keen-harbor)
      my-app-2 (ws-clone-1)  BUSY  — "Fix login bug" (swift-peak)
      my-app-3 (ws-clone-2)  IDLE  — Available
    ```

- **`src/discord/formatters.py`** — Update agent list formatting if it has agent-specific formatting functions. Adapt to `WorkspaceAgent` data shape.

- **Supervisor tool registry** — Update the `list_agents` tool to document it's project-scoped and describe the new output format.

---

## Phase 5: Clean Up References, Tests, and Documentation

Remove all remaining references to the old `Agent` model and global agent pool.

**Files to modify:**
- **`src/orchestrator.py`** — Remove any remaining `Agent` imports, agent state enum references
- **`src/command_handler.py`** — Final cleanup of any agent references
- **`tests/`** — Update all tests:
  - Remove test fixtures that create `Agent` objects
  - Update scheduler tests to not provide agents in `SchedulerState`
  - Update orchestrator tests to work without agent creation
  - Update command handler tests for removed commands and updated `/agents`
  - Update database tests: remove agent CRUD tests, add workspace-agent view tests
- **`specs/`** — Update any specs referencing the global agent model (likely `specs/orchestrator.md`, `specs/command-handler.md`, `specs/database.md`)
- **`profile.md`** — Update architecture description
- **`README.md`** — Remove the "create agent claude-1 and assign it to my-app" example. Update with new flow where workspaces define agent capacity.
- **`CLAUDE.md`** — Update if it references agent commands
- **`setup.sh`** — Remove any agent creation steps from the setup wizard
- **`src/setup_wizard.py`** — Remove agent creation from the setup wizard flow if present
- **`src/agent_names.py`** — Can be removed if workspace names or indices are used instead. If kept, update imports.
