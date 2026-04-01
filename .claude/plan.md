---
auto_tasks: true
---

# Fix: Planning Tasks Blocking Agents + Wrong Subtask Count

## Background & Root Cause Analysis

### Bug 1: Planning tasks block agents from starting other tasks

**Root cause:** The `_plan_processing_locks` mechanism in the orchestrator blocks ALL READY tasks in a project during plan subtask creation.

When `_cmd_process_plan` or the legacy path in `_cmd_approve_plan` runs, it acquires `_plan_processing_locks[project_id]`. While this lock is held (during `supervisor.break_plan_into_tasks()` LLM call, which can take 30-60+ seconds):

- `_schedule()` (orchestrator.py:1557-1562): Filters out ALL READY tasks in the project
- `_check_defined_tasks()` (orchestrator.py:1223): Skips ALL DEFINED tasks in the project

This means during plan processing, the **entire project is frozen** — no new tasks can be scheduled, even if they're completely unrelated to the plan.

The lock exists because `create_task` always creates tasks as `TaskStatus.READY` (command_handler.py:2237). Without the lock, the scheduler could assign a newly-created plan subtask before `break_plan_into_tasks` finishes demoting it to DEFINED and wiring up the blocking dependency on the parent.

### Bug 2: "Plan approved for grand-nexus. 0 subtask(s) created."

**Root cause:** The auto-detection path (plan detected during task completion pipeline) does NOT pre-create draft subtasks. It only stores the raw plan content in `plan_raw` task context.

When the user clicks "Approve":
1. `_cmd_approve_plan` looks for `plan_draft_subtasks` context → None (not created by auto-detection)
2. Falls to legacy path → calls `supervisor.break_plan_into_tasks()`
3. If the LLM call fails (rate limit, timeout, etc.), it returns `[]` silently
4. `_cmd_approve_plan` returns `subtask_count: 0` with **no error** — the plan is "approved" and the parent task is completed with 0 subtasks

The `_cmd_process_plan` command (manual Discord command) DOES pre-create subtasks, but it's never called automatically during the auto-detection flow.

### Key code locations

- **Lock usage:** `orchestrator.py:262` (init), `orchestrator.py:1223` (check_defined), `orchestrator.py:1557` (schedule)
- **Auto-detection:** `orchestrator.py:2562` (`_phase_plan_discover`), `orchestrator.py:3779` (result handling)
- **Subtask pre-creation:** `command_handler.py:3740-3792` (`_cmd_process_plan` supervisor call)
- **Approval flow:** `command_handler.py:3143-3310` (`_cmd_approve_plan`)
- **Task creation:** `command_handler.py:2237` (always creates as READY)
- **Subtask demotion:** `supervisor.py:844-848` (READY→DEFINED post-processing)

---

## Phase 1: Create plan subtasks directly as DEFINED to eliminate the need for project-wide locks

Add a `_plan_subtask_creation_mode` flag on the `CommandHandler`. When set to `True`, `_cmd_create_task` creates tasks with `TaskStatus.DEFINED` instead of `TaskStatus.READY`. This eliminates the race condition that `_plan_processing_locks` was designed to prevent.

**Changes:**
- `src/command_handler.py`: Add `self._plan_subtask_creation_mode: bool = False` attribute in `__init__`
- `src/command_handler.py` (`_cmd_create_task`): Check `self._plan_subtask_creation_mode` — if True, create task as `TaskStatus.DEFINED` instead of `TaskStatus.READY`
- `src/supervisor.py` (`break_plan_into_tasks`): Set `self.handler._plan_subtask_creation_mode = True` before the LLM call, reset in a `finally` block. Remove the READY→DEFINED demotion loop (lines 844-848) since tasks are already DEFINED
- `src/command_handler.py` (`_cmd_process_plan`): Remove `_plan_processing_locks.add()` / `.discard()` calls (lines 3743, 3792)
- `src/command_handler.py` (`_cmd_approve_plan`): Remove `_plan_processing_locks.add()` / `.discard()` calls (lines 3231, 3259)
- `src/orchestrator.py`: Remove `_plan_processing_locks` attribute (line 262) and the two filter blocks that use it (lines 1222-1224, lines 1557-1562)

## Phase 2: Auto pre-create draft subtasks during plan detection

After the auto-detection path detects a plan and transitions to AWAITING_PLAN_APPROVAL, automatically call the supervisor to break the plan into draft subtasks (same logic as `_cmd_process_plan`). This ensures:
- Approval always uses the fast path (draft_ctx exists)
- No LLM call needed at approval time
- The approval embed shows the parsed subtask breakdown

**Changes:**
- `src/orchestrator.py` (`_execute_task`, after line 3790): After transitioning to AWAITING_PLAN_APPROVAL and logging the event, add a new code block that:
  1. Gets the supervisor via `self._supervisor`
  2. Gets the raw plan content from `plan_raw` context (already stored by `_cmd_process_task_completion`)
  3. Gets workspace info for the task
  4. Calls `supervisor.break_plan_into_tasks()` with the raw plan, task.id as parent, project_id, workspace_id, and config settings
  5. If subtasks were created: adds blocking dependency from first subtask to parent, stores `plan_draft_subtasks` context (JSON list of {id, title})
  6. Populates `parsed_steps` from `created_info` (instead of hardcoded empty list at line 3832)
  7. All wrapped in try/except — failure is non-fatal (approval can still use legacy path)

## Phase 3: Handle 0 subtasks as an error in the approval flow

In `_cmd_approve_plan`, when the legacy path is used and `break_plan_into_tasks` returns an empty list, treat this as an error condition instead of silently completing the plan.

**Changes:**
- `src/command_handler.py` (`_cmd_approve_plan`, legacy path after line 3243): After `break_plan_into_tasks()` returns, check if `created_info` is empty. If so:
  - Log a warning
  - Return `{"error": "Supervisor failed to create subtasks from the plan. The plan has not been approved. Please retry or use /process-plan to manually trigger subtask creation."}` instead of proceeding
  - Do NOT transition the parent to COMPLETED
- Also add the same check for the draft_ctx path: if `created_info` is loaded from context but is empty (shouldn't happen normally, but defense-in-depth), return an appropriate warning
