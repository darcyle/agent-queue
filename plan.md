# Investigation: Tasks Created from Plans Stuck in DEFINED State

## Summary

When the orchestrator generates follow-up tasks from implementation plans (`_generate_tasks_from_plan`), these tasks are created with `TaskStatus.DEFINED` and (by default) chained dependencies. Several issues prevent or significantly delay these tasks from being promoted to `READY`.

**Root Causes Identified:**

1. **Inherited approval blocks the dependency chain** (Primary — High Impact)
2. **AWAITING_APPROVAL tasks without PR URLs are silently stuck** (Secondary — High Impact)
3. **BLOCKED tasks permanently freeze the dependency chain** (Tertiary — Medium Impact)
4. **One-cycle delay before first task is promotable** (Minor — Low Impact)
5. **No monitoring or alerting for stuck DEFINED tasks** (Observability gap)

---

## Detailed Findings

### Finding 1: Inherited Approval Requirement Blocks Dependency Chains

**Location:** `src/orchestrator.py:536-548`, `src/config.py:60`, `src/database.py:452-461`

**The Problem:**

When `auto_task.inherit_approval` is `True` (the default) and the parent task has `requires_approval=True`, ALL generated subtasks inherit `requires_approval=True`. Combined with `chain_dependencies=True` (also the default), this creates a cascading bottleneck:

1. Plan generates tasks A → B → C (chained dependencies)
2. All inherit `requires_approval=True` from parent
3. Task A gets promoted to READY (no deps), gets scheduled, agent completes work
4. Task A goes to `AWAITING_APPROVAL` (not `COMPLETED`) because it requires approval
5. `are_dependencies_met()` checks `r["status"] == TaskStatus.COMPLETED.value` — task A is `AWAITING_APPROVAL`, not `COMPLETED`
6. **Task B stays DEFINED indefinitely** until someone manually merges A's PR or runs `approve_task`
7. Task C stays DEFINED until B completes, which is blocked by A

**Impact:** With a 3-step plan, each step requires a manual PR merge before the next task can even begin. For a 10-step plan, this means 10 sequential manual interventions, during which all downstream tasks appear "stuck" in DEFINED.

**Config defaults that cause this:**
```python
# src/config.py
class AutoTaskConfig:
    inherit_approval: bool = True       # Subtasks inherit parent's requires_approval
    chain_dependencies: bool = True     # Tasks depend on previous step
```

**Fix Options:**
- Option A: Default `inherit_approval` to `False` so subtasks auto-complete without PR review
- Option B: Add an `inherit_approval` override per-plan or per-task
- Option C: When `chain_dependencies=True`, automatically set `requires_approval=False` for intermediate steps and only require approval on the final step

### Finding 2: AWAITING_APPROVAL Tasks Without PR URLs Are Silently Stuck

**Location:** `src/orchestrator.py:562-611`

**The Problem:**

When a task requires approval but no PR is created (e.g., LINK repos that work directly in the source directory), the task transitions to `AWAITING_APPROVAL` with `pr_url=None` (line 848-852). The `_check_awaiting_approval()` method skips tasks without a `pr_url`:

```python
# src/orchestrator.py:570-572
for task in tasks:
    if not task.pr_url:
        continue  # ← silently skipped, never auto-completes
```

These tasks can **only** be completed via manual `approve_task` command. If the user doesn't know to do this, the task stays in `AWAITING_APPROVAL` forever, and any downstream DEFINED tasks in the dependency chain remain stuck.

**Impact:** For LINK-repo projects with `requires_approval=True`, every plan-generated subtask that completes its work gets silently stuck, blocking the entire chain.

**Fix Options:**
- Option A: Add periodic reminders/notifications for tasks stuck in `AWAITING_APPROVAL` without `pr_url`
- Option B: Auto-approve LINK repo tasks after a configurable timeout
- Option C: Don't inherit `requires_approval` for subtasks on LINK repos

### Finding 3: BLOCKED Tasks Permanently Freeze the Dependency Chain

**Location:** `src/orchestrator.py:910-913`, `src/database.py:452-461`

**The Problem:**

When a task in a dependency chain exhausts its max retries, it transitions to `BLOCKED`. The `are_dependencies_met()` check requires `COMPLETED` status, so all downstream tasks remain DEFINED forever with no recovery path:

```python
# src/database.py:461
return all(r["status"] == TaskStatus.COMPLETED.value for r in rows)
```

There is no mechanism to:
- Notify that downstream tasks are now permanently stuck
- Automatically cancel or re-parent downstream tasks
- Allow an operator to "skip" a blocked task and unblock the chain

**Impact:** A single failed task can permanently orphan the rest of a plan's task chain.

**Fix Options:**
- Option A: When a task becomes BLOCKED, notify about affected downstream tasks
- Option B: Add a `skip_task` command that marks BLOCKED tasks as a special "skipped" completion state that satisfies dependency checks
- Option C: Consider BLOCKED tasks as "dependency met" with a warning, so downstream tasks can still proceed

### Finding 4: One-Cycle Delay Before First Task Is Promotable

**Location:** `src/orchestrator.py:172-208`

**The Problem:**

In `run_one_cycle()`, the order of operations is:
1. `_check_awaiting_approval()` — check PR merge status
2. `_resume_paused_tasks()` — resume paused tasks
3. **`_check_defined_tasks()`** — promote DEFINED → READY
4. `_schedule()` + launch background task coroutines — tasks execute here

Plan subtasks are generated during step 4 (inside `_execute_task_safe` → `_generate_tasks_from_plan`). Since `_check_defined_tasks()` already ran in step 3, newly generated subtasks won't be evaluated until the **next** cycle.

**Impact:** The first task in a plan chain (which has no dependencies) experiences a one-cycle delay before promotion to READY. With a typical 30-second cycle interval, this means a 30-second delay — minor but noticeable.

**Fix Option:** Call `_check_defined_tasks()` again at the end of `run_one_cycle()`, or trigger a check after plan generation completes.

### Finding 5: No Monitoring for Stuck DEFINED Tasks

**Location:** `src/orchestrator.py:266-276`

**The Problem:**

The `_check_defined_tasks()` method silently checks and promotes tasks, but there's no logging, notification, or alerting when tasks have been in DEFINED state for an extended period. An operator has no way to know that plan-generated tasks are stuck unless they manually query task status.

**Fix Options:**
- Option A: Log a warning when a DEFINED task has been stuck for more than N cycles
- Option B: Send a Discord notification when DEFINED tasks with unmet dependencies are detected for more than a configurable threshold
- Option C: Add a dashboard/status command that shows the dependency chain health

---

## Additional Observation: State Machine Function Is Unused

**Location:** `src/state_machine.py:33-37`

The `task_transition()` function defines the formal state machine but is **never called** in production code (only in tests). All state transitions in the orchestrator use direct `db.update_task(status=...)` calls, bypassing validation. This means invalid transitions could theoretically occur without detection.

---

## Recommended Fix Priority

| Priority | Finding | Effort | Impact |
|----------|---------|--------|--------|
| P0 | Finding 1: Inherited approval blocks chain | Low | High |
| P0 | Finding 2: AWAITING_APPROVAL without PR stuck | Medium | High |
| P1 | Finding 3: BLOCKED tasks freeze chain | Medium | Medium |
| P2 | Finding 5: No monitoring for stuck tasks | Low | Medium |
| P3 | Finding 4: One-cycle delay | Low | Low |

## Proposed Implementation Plan

### Step 1: Fix inherited approval default behavior
Change the default behavior so that intermediate plan steps don't require approval. Only the final step (or none) should require approval when `chain_dependencies=True`. This is the highest-impact change with minimal code modification.

**Files:** `src/orchestrator.py` (in `_generate_tasks_from_plan`), possibly `src/config.py`

### Step 2: Handle AWAITING_APPROVAL tasks without PR URLs
Add monitoring and notification for tasks stuck in `AWAITING_APPROVAL` without a `pr_url`. Consider adding an auto-complete mechanism for these tasks.

**Files:** `src/orchestrator.py` (in `_check_awaiting_approval`)

### Step 3: Add dependency chain health monitoring
When a task transitions to BLOCKED, identify and notify about all downstream DEFINED tasks that are now permanently stuck. Add a `skip_task` command to unblock chains.

**Files:** `src/orchestrator.py`, `src/command_handler.py`

### Step 4: Add stuck task monitoring
Log warnings and send notifications when DEFINED tasks have been waiting for promotion beyond a configurable threshold.

**Files:** `src/orchestrator.py` (in `_check_defined_tasks`)

### Step 5: Re-check DEFINED tasks after plan generation
Add an additional `_check_defined_tasks()` call after task execution completes to eliminate the one-cycle delay for the first promotable task.

**Files:** `src/orchestrator.py` (in `run_one_cycle` or `_execute_task_safe`)
