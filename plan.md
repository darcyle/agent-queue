---
auto_tasks: true
---

# Fix Hook Timeout Behavior for Task-Spawning Hooks

## Root Cause Analysis

When a hook (e.g., `Periodic Project Review`) spawns a task via the Supervisor's tool-use loop, the following happens:

1. Hook triggers → `_execute_hook_inner()` runs
2. LLM calls `create_task` tool → task created in DB with status `DEFINED`
3. LLM returns its response text (e.g., "✅ Healthy — No Issues Detected")
4. Hook immediately marks `HookRun` as `status="completed"` and posts Discord notification
5. The spawned task hasn't even started yet — it sits in the queue until the orchestrator assigns it on a future cycle

**The core issue:** `_execute_hook_inner()` treats the LLM response as the final outcome. It has no awareness that the LLM spawned tasks that haven't completed yet. The hook's completion notification and report are based on stale/premature information.

**Why this matters:**
- Discord messages like "✅ Healthy" are posted before tests even run
- The hook's `llm_response` field contains the LLM's prediction, not actual results
- There's no mechanism to correlate spawned tasks back to the hook that created them

### Key Code Locations

| File | Lines | What |
|------|-------|------|
| `src/hooks.py` | 410-504 | `_execute_hook_inner()` — main pipeline, marks complete immediately |
| `src/hooks.py` | 368-385 | `_launch_hook()` — fire-and-forget asyncio task creation |
| `src/hooks.py` | 609-696 | `_invoke_llm()` — delegates to Supervisor, returns when LLM finishes |
| `src/supervisor.py` | 545-559 | `process_hook_llm()` — thin wrapper around `chat()` |
| `src/supervisor.py` | 259-420 | `chat()` — multi-turn tool-use loop, executes `create_task` |
| `src/orchestrator.py` | 3681-3684 | `task.completed` event emission |
| `src/command_handler.py` | ~2088 | `_cmd_create_task()` — creates task in DB |

---

## Proposed Solutions

### Option A: Post-LLM Task Tracking with Await (Recommended)

**Concept:** After the LLM finishes its tool-use loop, check if any tasks were created during the hook's execution. If so, wait for them to complete (with timeout), then update the hook's final status and notification.

**How it works:**

1. **Track tasks created during hook execution.** Before invoking the LLM, snapshot the current task list for the project. After the LLM returns, diff to find newly created tasks. Alternatively, thread a `hook_run_id` through the Supervisor → CommandHandler → `create_task` call so tasks are tagged with their originating hook run.

2. **Add a `wait_for_tasks` option to Hook config.** Not all hooks spawn tasks — many just read data and report. Only hooks with `"wait_for_tasks": true` in their trigger config would enter the wait phase.

3. **Wait phase in `_execute_hook_inner()`.** After `_invoke_llm()` returns, if `wait_for_tasks` is enabled and tasks were spawned:
   - Set HookRun status to `"waiting"` (new status)
   - Poll spawned task statuses every 30 seconds
   - When all spawned tasks reach a terminal state (completed/failed/cancelled), proceed
   - Timeout after a configurable duration (e.g., `wait_timeout_seconds`, default 3600)

4. **Post-wait LLM invocation (optional).** After spawned tasks complete, optionally re-invoke the LLM with the task results so it can write an accurate report. This is the "second pass" — the hook prompt could include a `{{task_results}}` placeholder.

5. **Update final notification.** The Discord completion message now reflects actual results.

**Trade-offs:**
- ✅ Accurate reporting — hook waits for real results before posting
- ✅ Backward compatible — only hooks with `wait_for_tasks: true` are affected
- ✅ Configurable timeout prevents hooks from blocking forever
- ⚠️ Increases hook execution duration significantly (minutes to hours)
- ⚠️ Holds a slot in `_running` dict, counting against `max_concurrent_hooks`
- ⚠️ Polling adds minor DB load (one query every 30s per waiting hook)

### Option B: Event-Chained Two-Phase Hooks

**Concept:** Split hook execution into two phases. Phase 1 spawns tasks and completes immediately. Phase 2 is an event-driven hook triggered by `task.completed` that collects results and posts the final report.

**How it works:**

1. **Phase 1 hook** (periodic, existing): Runs on schedule, spawns tasks, completes immediately. Tags spawned tasks with a correlation ID (e.g., `hook_run_id`).

2. **Phase 2 hook** (event-driven, new): Listens for `task.completed` events. Checks if the completed task was spawned by a hook. If all tasks for that hook run are done, runs the reporting LLM pass and posts to Discord.

3. **Correlation tracking:** Add `source_hook_run_id` column to tasks table. When `create_task` is called during a hook execution, stamp the task with the current hook run ID.

**Trade-offs:**
- ✅ No long-running hook slots — Phase 1 completes fast
- ✅ Naturally event-driven, fits the existing architecture
- ✅ No polling overhead
- ⚠️ More complex — requires two hooks per workflow (or auto-generated Phase 2)
- ⚠️ User has to understand the two-phase model or the system auto-creates Phase 2 hooks
- ⚠️ Phase 2 hook needs to aggregate across multiple task completions (partial completion handling)
- ⚠️ If a spawned task never completes, Phase 2 never fires (needs a timeout mechanism anyway)

---

## Recommended Approach: Option A (Post-LLM Task Tracking with Await)

Option A is simpler, self-contained, and doesn't require users to understand a two-phase model. The key insight is that most task-spawning hooks already expect to wait — the `Periodic Project Review` hook *wants* to report test results, not just "I queued a test run."

The `max_concurrent_hooks` concern is manageable: hooks that wait for tasks are long-running by nature, so bump the default or add a separate `max_waiting_hooks` counter that doesn't compete with the regular concurrency cap.

---

## Phase 1: Task-to-Hook Correlation Tracking

Add infrastructure to track which tasks were spawned by which hook run.

**Changes:**

1. **`src/models.py`** — Add `source_hook_run_id: str | None = None` field to `Task` dataclass
2. **`src/database.py`** — Add `source_hook_run_id` column to tasks table (migration), update `create_task()` and query methods to handle the new field. Add a `get_tasks_by_hook_run(hook_run_id)` query method.
3. **`src/command_handler.py`** — In `_cmd_create_task()`, read the contextvar `current_hook_run_id` and pass it to the DB layer when creating tasks.
4. **`src/hooks.py`** — Define a `contextvars.ContextVar` named `current_hook_run_id`. Set it in `_execute_hook_inner()` before invoking the LLM, clear it after.
5. **`src/supervisor.py`** — No changes needed (contextvars propagate implicitly through the async call stack).

**Key design decision:** Use `contextvars.ContextVar` to implicitly pass the hook run ID through the call stack without modifying every function signature. The `_execute_hook_inner()` sets it before `_invoke_llm()`, and `_cmd_create_task()` reads it when creating tasks.

## Phase 2: Wait-for-Tasks Mechanism in Hook Engine

Add the ability for hooks to wait for their spawned tasks to complete before finalizing.

**Changes:**

1. **`src/models.py`** — Add `"waiting"` to HookRun status options (documentation/validation only — status is a free-form string)
2. **`src/hooks.py` — `_execute_hook_inner()`:**
   - After `_invoke_llm()` returns, query DB for tasks with `source_hook_run_id = run.id`
   - If tasks found AND hook has `wait_for_tasks` enabled in trigger config:
     - Update HookRun status to `"waiting"`
     - Notify Discord: "🪝 Hook **{name}** waiting for {n} spawned task(s)..."
     - Enter poll loop: check task statuses every 30s via `get_tasks_by_hook_run()`
     - Break when all tasks are terminal (completed/failed/cancelled) OR timeout reached
     - Collect task results (summary, status, output)
   - If timeout reached, mark which tasks timed out
   - Update HookRun with final aggregated status
3. **`src/hooks.py` — `_running` dict handling:**
   - Waiting hooks stay in `_running` but should NOT count against `max_concurrent_hooks` for launching new hooks
   - Add `_waiting: set[str]` to track which hook IDs are in the wait phase
   - Modify concurrency check in `tick()` and `_on_event()`: `active = len(self._running) - len(self._waiting)`
4. **Hook trigger config schema** — Add optional fields to the trigger JSON:
   - `wait_for_tasks: bool` (default `false`)
   - `wait_timeout_seconds: int` (default `3600`)
   - `wait_poll_interval_seconds: int` (default `30`)

## Phase 3: Post-Wait Reporting

After spawned tasks complete, generate an accurate report and post to Discord.

**Changes:**

1. **`src/hooks.py` — `_execute_hook_inner()`:**
   - After wait phase completes, build a task results summary
   - Optionally re-invoke LLM with results context for a synthesized report (controlled by `report_after_wait: bool` in trigger config, default `true`)
   - Update HookRun `llm_response` with the final report (append to or replace the initial response)
   - Post accurate Discord notification with real results
2. **`src/hooks.py` — New method `_build_task_results_summary(tasks: list[Task])`:**
   - For each completed task: title, status, duration, output summary (first 500 chars of task output)
   - For failed tasks: include error info
   - Return formatted string suitable for LLM context or direct posting
3. **Discord notifications:**
   - Waiting: "🪝 Hook **{name}** waiting for {n} spawned task(s)..."
   - Progress (optional, every 5 min): "🪝 Hook **{name}** still waiting — {completed}/{total} tasks done"
   - Completion: "🪝 Hook **{name}** completed. {summary}" with task results
   - Timeout: "🪝 Hook **{name}** timed out after {duration} — {completed}/{total} tasks finished"

## Phase 4: Tests

Add comprehensive tests for the new behavior.

**Changes:**

1. **`tests/test_hooks.py`:**
   - Test: hook with `wait_for_tasks=false` (default) completes immediately after LLM returns (backward compat)
   - Test: hook with `wait_for_tasks=true` enters waiting state after LLM spawns tasks
   - Test: waiting hook completes when all spawned tasks reach terminal state
   - Test: waiting hook times out after `wait_timeout_seconds`
   - Test: waiting hooks don't count against `max_concurrent_hooks` for new hook launches
   - Test: hook with `wait_for_tasks=true` but no tasks spawned completes immediately
   - Test: `source_hook_run_id` contextvar is set during hook LLM invocation
   - Test: post-wait LLM re-invocation includes task results
   - Test: Discord notifications sent at each phase (running, waiting, completed/timed-out)
2. **`tests/test_database.py`:**
   - Test: `source_hook_run_id` column exists and is queryable
   - Test: `get_tasks_by_hook_run()` returns correct tasks
   - Test: migration adds column without breaking existing data
3. **`tests/test_command_handler.py`:**
   - Test: `_cmd_create_task()` picks up `current_hook_run_id` contextvar and stores it
