# Fix Task Status Pipeline: Plan-Generated Tasks Stuck in DEFINED

## Problem Statement

When the orchestrator generates follow-up tasks from implementation plans (`_generate_tasks_from_plan` in `src/orchestrator.py:472-560`), these tasks are created with `TaskStatus.DEFINED` and chained dependencies. Five interrelated issues prevent or significantly delay these tasks from being promoted to `READY`, causing entire plan-generated task chains to appear permanently stuck.

**Affected code paths:**
- Task creation: `src/orchestrator.py:530-548`
- Dependency checking: `src/orchestrator.py:266-276`, `src/database.py:452-461`
- Approval polling: `src/orchestrator.py:562-612`
- Failure handling: `src/orchestrator.py:908-913`
- Cycle ordering: `src/orchestrator.py:172-208`

---

## Step 1: Fix Inherited Approval Blocking Dependency Chains (P0 — High Impact, Low Effort)

**Root Cause:** When `auto_task.inherit_approval=True` (default in `src/config.py:54`) and the parent task has `requires_approval=True`, ALL generated subtasks inherit `requires_approval=True`. Combined with `chain_dependencies=True` (default in `src/config.py:56`), this creates a cascading bottleneck:

- Plan generates tasks A → B → C (chained)
- All inherit `requires_approval=True`
- Task A completes work → goes to `AWAITING_APPROVAL` (not `COMPLETED`)
- `are_dependencies_met()` in `src/database.py:461` checks `status == COMPLETED` — A is `AWAITING_APPROVAL`
- Task B stays DEFINED indefinitely, blocking C, and the entire chain

**Concrete scenario:** A 10-step plan requires 10 sequential manual PR merges, each blocking the next task.

**Implementation:**

Modify `_generate_tasks_from_plan()` in `src/orchestrator.py:530-542` so that when `chain_dependencies=True`, only the **last** task in the chain inherits `requires_approval`. All intermediate tasks get `requires_approval=False` to allow the chain to flow automatically.

```python
# In _generate_tasks_from_plan(), around line 530-542
is_last_step = (step == plan.steps[-1])
new_task = Task(
    ...
    requires_approval=(
        task.requires_approval if config.inherit_approval and is_last_step
        else False
    ),
)
```

Also add a new config option `approve_only_last_step: bool = True` in `AutoTaskConfig` (`src/config.py:44-56`) to make this behavior configurable.

**Files to modify:**
- `src/orchestrator.py` — `_generate_tasks_from_plan()` (lines 530-542)
- `src/config.py` — `AutoTaskConfig` class (lines 44-56)
- `tests/test_orchestrator.py` — Add test for intermediate steps not requiring approval

**Verification:** Create a plan with 3 chained tasks where parent has `requires_approval=True`. Confirm only the last task has `requires_approval=True` and tasks A, B flow through to COMPLETED without manual intervention.

---

## Step 2: Handle AWAITING_APPROVAL Tasks Without PR URLs (P0 — High Impact, Medium Effort)

**Root Cause:** Tasks with `requires_approval=True` but no PR URL (LINK repos working in source directory) are silently skipped in `_check_awaiting_approval()`:

```python
# src/orchestrator.py:570-572
for task in tasks:
    if not task.pr_url:
        continue  # ← silently skipped forever
```

These tasks can only be completed via manual `approve_task` command. If the operator doesn't know to do this, the task and its entire downstream chain stay stuck permanently.

**Implementation:**

Two changes in `_check_awaiting_approval()` (`src/orchestrator.py:562-612`):

1. **Send periodic reminder notifications** for tasks stuck in `AWAITING_APPROVAL` without `pr_url` for more than a configurable threshold (default 5 minutes). Track last-notified timestamp to avoid spam.

2. **Add auto-approve option** for LINK repos: new config `auto_approve_link_repos: bool = False` in `AutoTaskConfig`. When enabled, tasks from LINK repos without PR URLs are automatically moved to COMPLETED after their work is committed.

Replace the `continue` at line 572 with:

```python
if not task.pr_url:
    # Notify about tasks needing manual approval
    if self._should_notify_stuck_approval(task):
        await self._notify_channel(
            f"**Manual Approval Needed:** Task `{task.id}` — {task.title} "
            f"is AWAITING_APPROVAL but has no PR. Use `approve_task {task.id}` to complete it.",
            project_id=task.project_id,
        )
    continue
```

**Files to modify:**
- `src/orchestrator.py` — `_check_awaiting_approval()` (lines 562-612), add `_should_notify_stuck_approval()` helper
- `src/config.py` — Add `auto_approve_link_repos` and `stuck_approval_notify_seconds` to `AutoTaskConfig`
- `tests/test_orchestrator.py` — Add test for notification of PR-less AWAITING_APPROVAL tasks

**Verification:** Create a LINK-repo task with `requires_approval=True` and no PR. Confirm notification fires after threshold and manual `approve_task` unblocks the chain.

---

## Step 3: Add Dependency Chain Health Monitoring for BLOCKED Tasks (P1 — Medium Impact, Medium Effort)

**Root Cause:** When a task exhausts max retries (`src/orchestrator.py:910-913`), it transitions to `BLOCKED`. Since `are_dependencies_met()` (`src/database.py:461`) requires `COMPLETED` status, all downstream tasks in the chain remain DEFINED forever with zero visibility:

```python
# src/database.py:461
return all(r["status"] == TaskStatus.COMPLETED.value for r in rows)
```

**Implementation:**

Three changes:

1. **Notify about orphaned downstream tasks** when a task becomes BLOCKED. In the failure handling path (`src/orchestrator.py:910-913`), after marking a task BLOCKED, query the dependency graph to find all tasks that directly or transitively depend on the blocked task, and send a notification listing them.

2. **Add `skip_task` command** in `src/command_handler.py` that marks a BLOCKED task with a special status that satisfies dependency checks. Implementation: transition the task to COMPLETED with a `skipped=True` flag (or add a metadata field), so downstream tasks can proceed.

3. **Add `are_dependencies_satisfiable()` check** in `src/database.py` that returns `False` if any dependency is in a terminal failure state (BLOCKED). Use this in `_check_defined_tasks()` to proactively notify about stuck chains rather than silently skipping them.

```python
# New method in database.py
async def are_dependencies_satisfiable(self, task_id: str) -> bool:
    """Return False if any dependency is BLOCKED (permanently unsatisfiable)."""
    cursor = await self._db.execute(
        "SELECT d.depends_on_task_id, t.status "
        "FROM task_dependencies d "
        "JOIN tasks t ON t.id = d.depends_on_task_id "
        "WHERE d.task_id = ?",
        (task_id,),
    )
    rows = await cursor.fetchall()
    return not any(r["status"] == TaskStatus.BLOCKED.value for r in rows)
```

**Files to modify:**
- `src/orchestrator.py` — failure handler (lines 908-913), `_check_defined_tasks()` (lines 266-276)
- `src/database.py` — Add `are_dependencies_satisfiable()` method
- `src/command_handler.py` — Add `skip_task` command
- `src/chat_agent.py` — Add `skip_task` tool definition
- `tests/test_orchestrator.py` — Add tests for BLOCKED chain notification
- `tests/test_database.py` — Add test for `are_dependencies_satisfiable()`

**Verification:** Create a 3-task chain A→B→C. Force A to BLOCKED. Confirm notification lists B and C as affected. Use `skip_task A` and confirm B gets promoted to READY.

---

## Step 4: Add Stuck Task Monitoring and Alerting (P2 — Medium Impact, Low Effort)

**Root Cause:** `_check_defined_tasks()` (`src/orchestrator.py:266-276`) silently iterates DEFINED tasks with no logging or alerting. Operators have zero visibility into stuck chains without manual `list-tasks` queries.

**Implementation:**

1. **Track how long tasks stay in DEFINED** by comparing `created_at` timestamp against current time in `_check_defined_tasks()`.

2. **Log warnings** for tasks stuck in DEFINED beyond a configurable threshold (default: 300 seconds / 5 minutes).

3. **Send Discord notification** (throttled to once per task per hour) for tasks stuck in DEFINED with unsatisfied dependencies.

```python
# In _check_defined_tasks(), after the existing logic
stuck_threshold = self.config.auto_task.stuck_defined_alert_seconds  # default 300
now = time.time()
for task in defined:
    if task.created_at and (now - task.created_at) > stuck_threshold:
        if task.id not in self._stuck_defined_notified:
            print(f"Warning: Task {task.id} has been DEFINED for "
                  f"{int(now - task.created_at)}s with unmet dependencies")
            await self._notify_channel(
                f"**Stuck Task:** `{task.id}` — {task.title} has been DEFINED "
                f"for {int((now - task.created_at) / 60)} minutes. Dependencies not met.",
                project_id=task.project_id,
            )
            self._stuck_defined_notified.add(task.id)
```

**Files to modify:**
- `src/orchestrator.py` — `_check_defined_tasks()` (lines 266-276), add `_stuck_defined_notified` set to `__init__`
- `src/config.py` — Add `stuck_defined_alert_seconds: int = 300` to `AutoTaskConfig`
- `tests/test_orchestrator.py` — Add test for stuck DEFINED notification

**Verification:** Create a DEFINED task with an unsatisfiable dependency. Confirm warning log appears after threshold and Discord notification is sent.

---

## Step 5: Eliminate One-Cycle Delay for First Promotable Task (P3 — Low Impact, Low Effort)

**Root Cause:** In `run_one_cycle()` (`src/orchestrator.py:172-208`), `_check_defined_tasks()` runs at step 2, but plan subtasks are generated during step 4 (inside `_execute_task_safe`). Newly generated subtasks won't be evaluated until the next cycle, causing a ~30-second delay for the first task in a chain.

**Implementation:**

Add a second `_check_defined_tasks()` call at the end of `run_one_cycle()`, after step 4 completes, to immediately promote any newly created DEFINED tasks that have no dependencies:

```python
# In run_one_cycle(), after step 4 (line ~204)
# 5. Run hook engine tick
if self.hooks:
    await self.hooks.tick()

# 6. Re-check DEFINED tasks to catch newly generated plan tasks
await self._check_defined_tasks()
```

**Files to modify:**
- `src/orchestrator.py` — `run_one_cycle()` (around line 204)
- `tests/test_orchestrator.py` — Add test verifying immediate promotion of first plan task

**Verification:** Generate a plan with tasks during a cycle. Confirm the first task (no deps) is promoted to READY within the same cycle, not the next one.

---

## Additional Recommendation: Enforce State Machine Validation

**Observation:** The `task_transition()` function in `src/state_machine.py:33-37` defines the formal state machine but is never called in production. All transitions use direct `db.update_task(status=...)`, bypassing validation.

**Suggestion (future work):** Wrap `db.update_task()` calls that change status through `task_transition()` to catch invalid transitions early. This is lower priority but would improve robustness. Not included as a step in this plan to keep scope focused.
