# Orchestrator Specification

This document describes the design and behaviour of the orchestrator subsystem in sufficient
detail to reimplement it from scratch.  It covers the event bus, task-name generation,
orchestrator initialisation, the scheduling cycle, every major internal operation, and the
external callback hooks that wire it to Discord.

---

## 1. Overview

The `Orchestrator` class is the central brain of the system.  Its responsibilities are:

- Maintaining the authoritative state of every task and agent via a SQLite database.
- Running a repeating scheduling cycle (typically every ~5 seconds from the outer loop)
  that drives the complete task lifecycle from DEFINED through to COMPLETED.
- Delegating actual agent execution to pluggable adapter objects, while managing
  workspace preparation, result handling, and post-completion git operations itself.
- Notifying operators through Discord via injected callbacks.

**Deterministic orchestration principle.**  The orchestrator makes zero LLM calls for
scheduling or coordination.  All promotion, assignment, and retry decisions are rule-based
and derive purely from database state.  LLM calls occur only inside agent adapters (doing
real work) and, optionally, inside the plan parser when `use_llm_parser` is enabled.

**Concurrency model.**  Everything runs inside a single asyncio event loop.  Each executing
task is launched as an `asyncio.Task` background coroutine.  The orchestrator keeps a
`_running_tasks` dict mapping `task_id -> asyncio.Task` so it can detect completion and
avoid double-launching.  There are no threads and no multiprocessing.

---

## Source Files

- `src/orchestrator.py`
- `src/event_bus.py`
- `src/task_names.py`

---

## 2. Event Bus

### Purpose

`EventBus` is a lightweight in-process pub/sub mechanism used by the hook engine and any
other component that needs to react to lifecycle events without a direct dependency on the
emitting component.

### Internal structure

```
_handlers: dict[str, list[Callable]]   # event_type -> ordered list of handlers
```

The dict is a `defaultdict(list)` so subscribing to a new event type never requires
pre-registration.

### Subscribing

```python
bus.subscribe(event_type: str, handler: Callable) -> None
```

`handler` is appended to the list for `event_type`.  Multiple handlers for the same event
are called in subscription order.  A single callable may be subscribed to multiple event
types by calling `subscribe` once per type.

### Wildcard subscription

Subscribing with the string `"*"` registers a catch-all handler.  On every `emit` call,
after the specific-type handlers are collected, the list at key `"*"` is appended.  A
wildcard handler therefore receives every event regardless of type.

### Emitting

```python
await bus.emit(event_type: str, data: dict | None = None) -> None
```

1. `data` is replaced via `data = data or {}`, so `None` or any other falsy value becomes
   an empty dict `{}`.
2. The key `"_event_type"` is injected into `data` so handlers can inspect the type even
   when they are registered as wildcard subscribers.
3. The specific-type handler list is snapshotted with `list(self._handlers.get(event_type, []))`;
   the wildcard list (`self._handlers.get("*", [])`) is then appended to that snapshot.
   Iterating over this pre-built combined list means a handler modifying subscriptions
   mid-emit cannot affect the current pass.
4. Each handler is called in order.  If `inspect.iscoroutinefunction(handler)` is true the
   handler is `await`-ed; otherwise it is called synchronously.  There is no timeout or
   exception isolation — a crashing handler will propagate to the caller.

### Where it is used

The `EventBus` instance lives on `Orchestrator.bus`.  The `HookEngine` receives a
reference to it during `initialize()` and subscribes its own handlers to task lifecycle
events.

---

## 3. Task Name Generation

### Purpose

Tasks are identified by human-readable IDs of the form `adjective-noun` (e.g.
`swift-falcon`, `bold-harbor`).  These IDs are used everywhere: database primary keys,
Discord messages, CLI arguments.

### Word lists

Two fixed lists are defined at module level:

- `ADJECTIVES`: 28 words (swift, bright, calm, bold, keen, wise, fair, sharp, clear,
  eager, fresh, grand, prime, quick, smart, sound, solid, stark, steady, noble, crisp,
  fleet, nimble, brisk, vivid, agile, amber, azure).
- `NOUNS`: 32 words (falcon, horizon, cascade, ember, summit, ridge, beacon, current,
  delta, forge, glacier, harbor, impact, journey, lantern, meadow, nexus, orbit, pinnacle,
  quest, rapids, stone, torrent, vault, willow, zenith, apex, bridge, crest, dune, flare,
  grove).

This gives 28 × 32 = 896 base combinations.

### Algorithm

```python
async def generate_task_id(db) -> str
```

1. Attempt up to `_MAX_RETRIES` (10) times:
   a. Pick a random adjective and a random noun, join with a hyphen.
   b. Call `db.get_task(name)` — an async database lookup.
   c. If the result is `None` (no collision), return the name immediately.

2. If all 10 attempts collide (extremely unlikely), enter an infinite fallback loop:
   a. Construct `adjective-noun-NN` where `NN` is a random integer in [10, 99].
   b. Check for collision as above.
   c. Return on first non-collision.

The fallback loop is guaranteed to terminate because there are 896 × 90 = 80,640
suffixed combinations.

---

## 4. Initialization

### `Orchestrator.__init__`

The constructor creates all sub-objects but performs no I/O:

| Field | Type | Purpose |
|---|---|---|
| `config` | `AppConfig` | Full application config |
| `db` | `Database` | SQLite persistence layer |
| `bus` | `EventBus` | In-process pub/sub |
| `budget` | `BudgetManager` | Global daily token budget |
| `git` | `GitManager` | Git operations wrapper |
| `_adapter_factory` | optional | Factory for creating agent adapters |
| `_adapters` | `dict[str, adapter]` | `agent_id -> running adapter` |
| `_running_tasks` | `dict[str, asyncio.Task]` | `task_id -> background coroutine` |
| `_notify` | `NotifyCallback \| None` | Discord notification callback |
| `_create_thread` | `CreateThreadCallback \| None` | Discord thread creation callback |
| `_paused` | `bool` | Global scheduling pause flag |
| `_last_approval_check` | `float` | Unix timestamp of last approval poll |
| `_chat_provider` | optional | LLM provider for plan parsing |
| `_no_pr_reminded_at` | `dict[str, float]` | Rate-limit tracker for no-PR reminders |
| `_stuck_notified_at` | `dict[str, float]` | Rate-limit tracker for stuck-DEFINED alerts |
| `hooks` | `HookEngine \| None` | Hook subsystem |

If `config.auto_task.use_llm_parser` is true, the constructor attempts to instantiate a
`ChatProvider` via `create_chat_provider(config.chat_provider)`.  Failure is silently
swallowed — the system falls back to regex parsing.

### `async initialize()`

Called once before the scheduling loop starts:

1. `await self.db.initialize()` — opens the SQLite connection, runs migrations.
2. `await self._recover_stale_state()` — repairs in-flight state from a previous run
   (see section 4a below).
3. If `config.hook_engine.enabled` is true:
   - Instantiate `HookEngine(db, bus, config)`.
   - Call `hooks.set_orchestrator(self)`.
   - `await hooks.initialize()`.

### 4a. Stale state recovery (`_recover_stale_state`)

After a daemon restart, no real agents are running.  Any database records that say
otherwise must be cleaned up:

1. List all agents.  For each agent whose state is `BUSY` or `STARTING`:
   - Log a recovery message to stdout.
   - Call `db.update_agent(id, state=IDLE, current_task_id=None)`.

2. List all tasks with status `IN_PROGRESS`.  For each:
   - Log a recovery message to stdout.
   - Call `db.transition_task(id, READY, context="recovery", assigned_agent_id=None)`.

This ensures that tasks which were interrupted mid-run are re-queued from READY rather
than left stuck in IN_PROGRESS forever.

---

## 5. Orchestration Cycle

`run_one_cycle()` is the top-level method called by the outer loop on every tick.  It
executes the following steps in strict order, wrapped in a single broad `try/except` that
logs unexpected errors with a full traceback but does not crash the loop.

```
Step 0  _check_awaiting_approval       — poll PR merge status (rate-limited to 60s)
Step 1  _resume_paused_tasks           — promote PAUSED tasks whose resume_after has elapsed
Step 2  _check_defined_tasks           — promote DEFINED tasks whose deps are all COMPLETED
Step 2b _check_stuck_defined_tasks     — alert on DEFINED tasks stuck beyond threshold
Step 3  _schedule                      — ask Scheduler for assignment actions (skipped if paused)
Step 4  Launch background executions  — start new asyncio.Tasks for each AssignAction
Step 5  hooks.tick()                   — run hook engine tick (if enabled)
```

**Pause behaviour.**  When `self._paused` is true, step 3 is skipped and `actions` is set
to an empty list.  All other steps (approval checks, promotion, etc.) continue running
because those represent state maintenance, not new work assignment.

**Background task cleanup.**  At the start of step 4, the `_running_tasks` dict is
scanned for entries whose `asyncio.Task.done()` returns true; those entries are removed.
This prevents unbounded growth of the dict.

**Double-launch guard.**  Before launching a new `asyncio.Task` for an `AssignAction`,
the orchestrator checks whether `action.task_id` is already in `_running_tasks`.  If it
is, the action is silently skipped.

---

## 6. Task Promotion (DEFINED -> READY)

### `_check_defined_tasks`

Runs every cycle.  For each task currently in status `DEFINED`:

1. Fetch the task's declared dependencies via `db.get_dependencies(task.id)`.
2. If the dependency list is empty: call `db.transition_task(id, READY, context="deps_met_no_deps")`.
3. If the dependency list is non-empty: call `db.are_dependencies_met(task.id)`.
   - This returns `True` only when every upstream task has status `COMPLETED`.
   - If met: call `db.transition_task(id, READY, context="deps_met")`.

Tasks are promoted on the same cycle they become eligible.  There is no one-cycle delay
(the re-check at the end of plan generation, step 4 of task execution, explicitly calls
`_check_defined_tasks` again for freshly created subtasks).

---

## 7. Stuck Task Detection

### `_check_stuck_defined_tasks`

Runs every cycle after `_check_defined_tasks`.

**Configuration.**  `config.monitoring.stuck_task_threshold_seconds` controls the
threshold.  A value of `<= 0` disables this feature entirely.

**Process:**

1. Call `db.get_stuck_defined_tasks(threshold)` which returns all DEFINED tasks whose age
   exceeds the threshold.
2. If the result is empty, return immediately.
3. Clean up `_stuck_notified_at` by removing entries for task IDs that are no longer in
   the stuck list (they have since been promoted or deleted).
4. For each stuck task, apply a per-task rate limit: skip if `now - last_notified < threshold`.
5. For tasks that pass the rate limit:
   a. Call `db.get_blocking_dependencies(task.id)` to get a list of `(dep_id, dep_title, dep_status)` tuples.
   b. Call `db.get_task_created_at(task.id)` to compute `stuck_hours`.
   c. Format the notification with `format_stuck_defined_task(task, blocking, stuck_hours)` and send via `_notify_channel`.
   d. Log a `"stuck_defined_task"` event in the database with `stuck_hours` and the IDs of up to 10 blocking deps.
   e. Print a summary line to stdout.
   f. Update `_stuck_notified_at[task.id] = now`.

### Downstream chain sticking (`_notify_stuck_chain` / `_find_stuck_downstream`)

Called from `stop_task`, task timeout handling, PR-closed handling, and FAILED-past-max-retries
handling.

`_find_stuck_downstream(blocked_task_id)` performs a breadth-first traversal of the
forward dependency graph:

1. Start a queue with `[blocked_task_id]`.
2. For each dequeued ID, call `db.get_dependents(id)` to get the direct downstream tasks.
3. For each downstream task whose status is `DEFINED`, append it to `stuck` and enqueue it
   for further traversal.  Tasks in any other status are ignored (they have already escaped
   the dependency gate).
4. A `visited` set prevents infinite loops in cyclic graphs.
5. Returns the full list of transitively stuck DEFINED tasks.

`_notify_stuck_chain(blocked_task)` calls `_find_stuck_downstream`, and if the result is
non-empty formats and sends a `format_chain_stuck` notification and logs a `"chain_stuck"`
event.

---

## 8. Scheduling Integration

### `_schedule` -> `list[AssignAction]`

Collects all state needed by `Scheduler.schedule` and delegates to it:

1. `db.list_projects()`, `db.list_tasks()`, `db.list_agents()` — full snapshots.
2. For each project, compute token usage in the rolling window:
   `window_start = now - (config.scheduling.rolling_window_hours * 3600)`
   `project_usage[p.id] = db.get_project_token_usage(p.id, since=window_start)`
3. Count active agents per project by iterating agents whose state is `BUSY` or `STARTING`
   and looking up their `current_task_id`.
4. Sum all per-project usage to get `total_used`.
5. Build a `SchedulerState` dataclass and call `Scheduler.schedule(state)`.

### `Scheduler.schedule` logic (summary)

- Immediately returns `[]` if the global daily budget is set and already exhausted.
- Finds idle agents.
- Groups READY tasks by project; sorts within each group by `(priority asc, id asc)`.
- Filters to active projects that have at least one READY task.
- For each idle agent, picks the project with the highest scheduling priority using a
  two-component sort key:
  1. Whether the project has received a minimum-task guarantee (projects with zero
     completions in the window are sorted first).
  2. Token-usage deficit: `(actual_token_ratio - target_token_ratio)` — lower deficit
     (more underfunded) sorts first.
- Within the chosen project, picks the first (highest priority) available READY task that
  hasn't already been assigned in this round.
- Skips projects that have hit their per-project `budget_limit` or their
  `max_concurrent_agents` limit.
- Returns a `list[AssignAction(agent_id, task_id, project_id)]`.

---

## 9. Task Execution

Task execution is driven by `_execute_task(action: AssignAction)`, wrapped by
`_execute_task_safe` which applies an overall timeout and catches unexpected exceptions.

### 9a. `_execute_task_safe`

If `config.agents_config.stuck_timeout_seconds > 0`, wraps `_execute_task` in
`asyncio.wait_for(...)`.  On `TimeoutError`:

1. Stop the adapter via `adapter.stop()` (best-effort).
2. Transition task to `BLOCKED` with `context="timeout"`.
3. Set agent to `IDLE`.
4. Remove the adapter from `_adapters`.
5. Notify the channel.
6. Call `_notify_stuck_chain` for the now-blocked task.

On any other unexpected exception:

1. Transition task back to `READY` (so it will be retried next cycle) — best-effort,
   errors ignored.
2. Set agent to `IDLE` — best-effort, errors ignored.
3. Notify the channel with the error.

In both cases, remove the task from `_running_tasks` in a `finally` block.

### 9b. `_execute_task` — step by step

**Precondition check.**  If `_adapter_factory` is `None`, notify and return immediately.

**Step 1 — Assign.**
`db.assign_task_to_agent(task_id, agent_id)` — records the assignment in the database.

**Step 2 — Mark IN_PROGRESS.**
`db.transition_task(task_id, IN_PROGRESS, context="agent_started")`
`db.update_agent(agent_id, state=BUSY)`

**Step 3 — Fetch current records.**
`task = db.get_task(task_id)`, `agent = db.get_agent(agent_id)`.

**Step 4 — Prepare workspace.**
`project = db.get_project(project_id)`.
Call `_prepare_workspace(task, agent)` inside a try/except.  `_prepare_workspace` always
returns a path.  On exception, send a "Workspace Error" Discord notification via
`_notify_channel` and fall back to `project.workspace_path or config.workspace_dir`.
Re-fetch `task` and `agent` after workspace preparation because `_prepare_workspace` may
have updated `branch_name`.

**Step 5 — Notify start.**
Send a "Task Started" message to `_notify_channel` including the task ID, title, agent
name, and (if set) the branch name.

**Step 6 — Create Discord thread.**
If `_create_thread` callback is set, call it with `(thread_name, start_msg, project_id)`.
- `thread_name` is `"{task.id} | {task.title}"` truncated to 100 characters.
- Returns a tuple `(send_to_thread, notify_main)` or `None` on failure.
- `thread_send` — callable that streams content into the thread.
- `thread_main_notify` — callable that posts a brief reply to the thread-root in the
  notifications channel.

**Step 7 — Create adapter.**
`adapter = _adapter_factory.create("claude")`
Store in `_adapters[agent_id]`.

**Step 8 — Build system context.**
Construct a multi-line string injected ahead of the task description:

```
## System Context
- Workspace directory: {workspace}
- Global workspaces root: {config.workspace_dir}
- Project: {project.name} (id: {project.id})
- Git branch: {task.branch_name}   (if set)

## Important: Execution Rules
...

## Important: Committing Your Work
...
```

For plan subtasks (`task.is_plan_subtask = True`) the execution rules:
- Forbid plan mode (`EnterPlanMode`) and writing plan files.
- Forbid pushing (the system handles pushing and PR creation).
- Require the agent to `git add` and `git commit` its changes when done.

For root tasks, the execution rules:
- Also forbid plan mode and pushing.
- Also require committing when done.
- Additionally instruct the agent that *if* the task is to produce an implementation plan,
  it must write the plan to `.claude/plan.md` or `plan.md` in the workspace root (not any
  other path), using `## Section` headings for each step.

The full task description is appended as `## Task\n{task.description}`.

**Step 9 — Start adapter.**
Build `TaskContext(description=full_description, checkout_path=workspace, branch_name=...)`.
`await adapter.start(ctx)`.

**Step 10 — Define message forwarder.**
```python
async def forward_agent_message(text: str) -> None
```
If `thread_send` is available, forward to the thread.  Otherwise prepend
`` `{task.id}` | **{agent.name}**\n `` and send to `_notify_channel`.

**Step 11 — Rate-limit retry loop.**
Enter a `while True` loop:
1. `output = await adapter.wait(on_message=forward_agent_message)` — blocks until the
   agent produces a result.
2. If `output.result != PAUSED_RATE_LIMIT`: break.
3. Increment `_rl_attempt`.  If `_rl_attempt > _rl_max_retries` (from config): break.
4. Compute exponential backoff: `min(base * 2^(attempt-1), max_backoff)`.
5. Notify "rate-limited, retrying in Ns".
6. `asyncio.sleep(backoff)`.
7. Notify "rate limit cleared, resuming".
8. Re-`await adapter.start(ctx)` to reinitialise the adapter.
9. Loop again.

Configuration values:
- `config.pause_retry.rate_limit_backoff_seconds` — base backoff (doubles each attempt)
- `config.pause_retry.rate_limit_max_backoff_seconds` — cap
- `config.pause_retry.rate_limit_max_retries` — maximum retries before giving up

**Step 12 — Record tokens.**
If `output.tokens_used > 0`: `db.record_token_usage(project_id, agent_id, task_id, tokens)`.

**Step 13 — Persist task result.**
`db.save_task_result(task_id, agent_id, output)` (best-effort, errors logged).

**Step 14 — Re-fetch task** (retry_count may have changed in the DB).

**Step 15 — Handle result.**

*`COMPLETED`:*
- Transition task to `VERIFYING` (`context="agent_completed"`).
- Call `_complete_workspace(task, agent)` (best-effort; git errors posted to thread/channel).
- If a `pr_url` was returned:
  - Transition to `AWAITING_APPROVAL` (`context="pr_created"`, `pr_url=pr_url`).
  - Log a `"pr_created"` event.
  - Post PR-created notification to thread and main channel.
- Else if `task.requires_approval` and no PR (e.g. LINK repo):
  - Transition to `AWAITING_APPROVAL` (`context="approval_required_no_pr"`).
  - Post "awaiting manual approval" notification.
- Else:
  - Transition to `COMPLETED` (`context="completed_no_approval"`).
  - Log a `"task_completed"` event.
  - Post full completion summary to thread (or `_notify_channel`); post brief to main.
- After any of the above paths: call `_generate_tasks_from_plan(task, workspace)`.
  If subtasks were created, call `_check_defined_tasks()` immediately, then post
  an auto-generated-tasks notice to thread and main channel.

*`FAILED`:*
- Increment `retry_count`.
- If `retry_count >= max_retries`: transition to `BLOCKED` (`context="max_retries"`);
  call `_notify_stuck_chain(task)`.
- Otherwise: transition back to `READY` (`context="retry"`, incremented `retry_count`).
- Post failure details to thread (or `_notify_channel`) and a brief to main channel.

*`PAUSED_TOKENS` or `PAUSED_RATE_LIMIT`* (after rate-limit auto-retries are exhausted):
- Compute `retry_secs`:
  - `PAUSED_RATE_LIMIT` → `config.pause_retry.rate_limit_backoff_seconds`
  - `PAUSED_TOKENS` → `config.pause_retry.token_exhaustion_retry_seconds`
- Transition to `PAUSED` (`context="tokens_exhausted"`, `resume_after=now+retry_secs`).
- Post "Task Paused" notice with the reason and retry delay.

**Step 16 — Free agent.**
`db.update_agent(agent_id, state=IDLE, current_task_id=None)`.

---

## 10. Workspace Preparation

### Design Invariants

The workspace sync workflow preserves these invariants across all code paths.
See `specs/git/git.md` §10 for the full design principles reference.

| Invariant | Guarantee |
|---|---|
| **Per-agent isolation** | Each `(agent, project)` pair gets its own filesystem directory; concurrent agents never share a working tree. |
| **Branch-per-task** | Every task gets a unique `<task-id>/<slug>` branch. Subtasks accumulate on the parent's branch. |
| **Fresh starting point** | `prepare_for_task` always fetches from origin before creating a task branch, so agents start from recent code. |
| **Atomic commit** | `commit_all` stages everything then checks the staging area, avoiding race conditions. Agent work is never silently lost. |
| **Graceful degradation** | Git errors during workspace setup are caught and logged; a valid workspace path is always returned so the agent can start work. |
| **Retry resilience** | Existing branches are reused on task retry rather than causing errors. |

### Known Gaps

The workspace sync workflow has several identified gaps that affect correctness
under concurrent multi-agent operation. See `specs/git/git.md` §11 for the full
gap catalogue (G1–G7). The most critical gaps relative to this spec are:

| Gap | Location in this spec | Issue |
|-----|----------------------|-------|
| **G1** | §11 `_merge_and_push` | No `git pull` before merge — push fails if another agent advanced `main`. |
| **G2** | §11 `_merge_and_push` | Failed push leaves local `main` diverged; no rollback to clean state. |
| **G3** | §11 `_merge_and_push` | Merge conflict triggers notify-and-stop; no automated rebase-and-retry. |
| **G4** | §10 `_prepare_workspace` | Retried tasks check out existing branch without rebasing onto latest `origin/main`. |
| **G6** | §10 `_prepare_workspace` | Subtask chains use `switch_to_branch` without periodic rebase, accumulating drift. |
| **G7** | §10 `_prepare_workspace` | LINK repos share a single directory across agents — no file-level isolation. |

`_prepare_workspace(task, agent) -> str`

Always returns the absolute path to the workspace directory (never None).

**Workspace resolution chain:**

1. **agent_workspaces lookup** — `db.get_agent_workspace(agent.id, task.project_id)`.
   If found, use the cached `workspace_path`.
2. **Auto-populate from repo config** — If no cached workspace:
   - Use `task.repo_id` if set, otherwise the project's first repo.
   - Compute workspace path from repo source type:
     - LINK: `workspace = repo.source_path`
     - CLONE/INIT: `workspace = {config.workspace_dir}/{project_id}/{agent.name}/{repo_name}`
   - Save to `agent_workspaces` for future lookups.
3. **Fallback** — No repo at all: use `project.workspace_path` or `config.workspace_dir`.

**Branch name.**
- For plan subtasks that have a parent task: reuse the parent's `branch_name` (to
  accumulate all subtask commits on the same branch).  If the parent has no branch name,
  generate one from the subtask ID and title.
- For all other tasks: generate a fresh branch name with `GitManager.make_branch_name(task.id, task.title)`.

**`reuse_branch` flag.**  True when `task.is_plan_subtask and task.parent_task_id` is set.

**By source type:**

*CLONE repos:*
- If `validate_checkout(workspace)` fails: call `git.create_checkout(repo.url, workspace)`
  (which `git clone`s the repo into `workspace`, creating parent directories as needed).
- If `reuse_branch`: call `git.switch_to_branch(workspace, branch_name)` — fetches from
  origin and checks out the existing branch, pulling latest if available.
- Otherwise: call `git.prepare_for_task(workspace, branch_name, repo.default_branch)` —
  fetches from origin, checks out `default_branch`, pulls latest, then creates a new branch
  named `branch_name` (or switches to it if it already exists from a previous attempt).

*LINK repos:*
- If `workspace` does not exist as a directory: send a Discord warning notification
  via `_notify_channel` and return the path as-is.
- If the directory is a git repo (`validate_checkout` passes): apply the same branch logic
  as CLONE (`switch_to_branch` or `prepare_for_task`).
- If not a git repo: use the directory as-is (no git operations).

*INIT repos:*
- If `validate_checkout(workspace)` fails: call `git.init_repo(workspace)` to initialise
  a new repository.
- If `reuse_branch`: call `git.switch_to_branch(workspace, branch_name)`.
- Otherwise: call `git.create_branch(workspace, branch_name)` — runs `git checkout -b`,
  switching to the branch instead if it already exists.

**Database updates.**  After the git operations:
`db.update_task(task.id, branch_name=branch_name)`

---

## 11. Workspace Completion

`_complete_workspace(task, agent) -> str | None`

Called after the adapter signals `COMPLETED`.  Returns a PR URL if one was created,
otherwise `None`.

**Preconditions.**  Look up the workspace via `db.get_agent_workspace(agent.id, task.project_id)`.
If no workspace is found or it is not a valid git checkout, or if `task.branch_name` is not set,
return `None` immediately.

**Commit.**  Call `git.commit_all(workspace, "agent: {title}\n\nTask-Id: {id}")`.  If
nothing was committed, log a message (not an error).

**Repo config.**  Resolve `repo_id` from task then agent; fetch `RepoConfig`.

**Plan subtask path.**  If `task.is_plan_subtask`:
- Call `_is_last_subtask(task)`.
  - `_is_last_subtask` fetches all sibling subtasks (same `parent_task_id`) via
    `db.get_subtasks(parent_task_id)` and returns `True` only when every sibling other
    than this task has status `COMPLETED`.
- If not the last subtask: return `None` (commit is sufficient; PR/merge waits for final).
- If last subtask and repo exists: fetch the parent task record.
  - If parent task exists and has `requires_approval`: return `await _create_pr_for_task(...)`,
    which may return a PR URL or `None`.
  - Otherwise: call `_merge_and_push`.
- Return `None`.

**Root task path.**
- If repo exists and `requires_approval`: call `_create_pr_for_task`, return the URL.
- If repo exists and no approval needed: call `_merge_and_push`, return `None`.
- If no repo: changes remain committed on the branch but nothing is pushed, return `None`.

### `_merge_and_push(task, repo, workspace)`

1. `git.merge_branch(workspace, branch_name, default_branch)`.
2. On conflict: notify channel with a "Merge Conflict" message; return.
3. If repo is CLONE: `git.push_branch(workspace, default_branch)`.  On push failure: notify.
4. Best-effort: `git.delete_branch(workspace, branch_name, delete_remote=(repo is CLONE))`.

> **Gaps G1–G3 apply here.** Step 1 merges into a potentially stale local
> `main` (G1). Step 3 notifies but does not roll back the local merge on push
> failure (G2). Step 2 aborts the merge on conflict without attempting a rebase
> (G3). See `specs/git/git.md` §11 for details.

### `_create_pr_for_task(task, repo, workspace) -> str | None`

For LINK repos: notify "Approval Required" with manual-review instructions and return `None`.

For CLONE repos:
1. `git.push_branch(workspace, branch_name, force_with_lease=True)`.  On failure: notify and return `None`.
   Uses `--force-with-lease` so retries don't fail if the branch was previously
   pushed (G5 fix). Task branches are agent-owned and safe to force-push.
2. `git.create_pr(workspace, branch, title, body, base=default_branch)`.
   - PR body: `"Automated PR for task \`{id}\`.\n\n{description[:500]}"`.
3. On PR creation failure: notify and return `None`.
4. On success: return the PR URL.

---

## 12. Plan-Generated Tasks

`_generate_tasks_from_plan(task, workspace) -> list[Task]`

Called immediately after any successful COMPLETED path in `_execute_task`.

**Guards:**
- If `config.auto_task.enabled` is false: return `[]`.
- If `task.is_plan_subtask` is true: return `[]` (prevent recursive explosion).

**Plan file discovery.**
Call `find_plan_file(workspace, config.auto_task.plan_file_patterns)`.
If no file found, log to stdout and return `[]`.

**Plan reading.**
`raw = read_plan_file(plan_path)`.  On I/O error, log and return `[]`.

**Parsing.**
If `config.auto_task.use_llm_parser` and `_chat_provider` is set:
- Call `parse_plan_with_llm(raw, provider, source_file, max_steps)`.
- On failure, log and fall back to `parse_plan(raw, source_file, max_steps)`.

Otherwise: call `parse_plan(raw, source_file, max_steps)`.

If `plan.steps` is empty, log and return `[]`.

**Plan archiving.**
Move the plan file to `.claude/plans/{task.id}-plan.md` inside the workspace.  This
prevents the file from being re-processed if the workspace is reused.  Any `OSError` is
silently ignored.

**Preamble extraction.**
Extract text from `plan.raw_content` before the first step title as `plan_context`.  Strip
a leading `# Title` heading if present.  This context is prepended to every subtask
description.

**Subtask creation loop.**  Iterate `plan.steps` with an index:

For each step:
1. `new_id = await generate_task_id(db)`.
2. `description = build_task_description(step, parent_task=task, plan_context=plan_context)`.
3. Determine `requires_approval`:
   - If `inherit_approval` and `chain_dependencies`: only the final step inherits the parent's
     `requires_approval`; intermediate steps get `False`.
   - If `inherit_approval` and not `chain_dependencies`: every step inherits.
   - Otherwise: `False`.
4. Construct a `Task` dataclass:
   - `status = DEFINED`
   - `parent_task_id = task.id`
   - `repo_id = task.repo_id if inherit_repo else None`
   - `priority = config.base_priority + step.priority_hint`
   - `plan_source = archived_path`
   - `is_plan_subtask = True`
5. `db.create_task(new_task)`.
6. If `chain_dependencies` and `prev_task_id` is set: `db.add_dependency(new_id, depends_on=prev_task_id)`.
7. Record `prev_task_id = new_id` for the next iteration.

**After creation.**  Return the list of created tasks.  The caller (`_execute_task`) then
calls `_check_defined_tasks()` immediately so that any subtask with no dependencies (or
whose only dependency is already COMPLETED) is promoted to READY in the same cycle.

---

## 13. Approval Checking

### `_check_awaiting_approval`

Rate-limited to once per 60 seconds using `_last_approval_check`.

1. List all tasks with status `AWAITING_APPROVAL`.
2. Clean up `_no_pr_reminded_at` for task IDs no longer in the list.
3. For each task:
   - If `task.pr_url` is absent: call `_handle_awaiting_no_pr(task, now)`.
   - Otherwise: call `_check_pr_status(task)`.

### `_handle_awaiting_no_pr(task, now)`

Compute `updated_at = db.get_task_updated_at(task.id)` and
`age = (now - updated_at) if updated_at else 0`.

**Auto-complete path** (when `task.requires_approval` is false):
- If `age >= _NO_PR_AUTO_COMPLETE_GRACE` (120 seconds default): transition to COMPLETED
  (`context="auto_complete_no_pr"`), log a `"task_completed"` event, notify, and clear the
  reminder tracker.  Return immediately after (the manual-approval path below is skipped).

**Manual-approval path** (when `task.requires_approval` is true):
- Check the reminder interval: skip if `now - _no_pr_reminded_at[task.id] < _NO_PR_REMINDER_INTERVAL` (3600s).
- Update `_no_pr_reminded_at[task.id] = now`.
- If `age >= _NO_PR_ESCALATION_THRESHOLD` (86400s = 24h): send a high-visibility escalation
  warning with the age in hours and log `"approval_stuck"`.
- Otherwise: send a standard "awaiting manual approval" reminder.

### `_check_pr_status(task)`

Resolves a checkout path by checking `db.get_agent_workspace(agent_id, project_id)`,
then falling back to `task.repo_id -> repo.source_path`.  If no path is found, return.

Call `git.check_pr_merged(checkout_path, task.pr_url)`:
- Returns `True` if merged.
- Returns `None` if closed without merge.
- Returns `False` if still open.

**Merged (`True`):**
Transition to COMPLETED, log `"task_completed"`, notify.
Best-effort: delete the task branch locally and remotely.

**Closed without merge (`None`):**
Transition to BLOCKED, context `"pr_closed"`, notify.
Call `_notify_stuck_chain(task)`.

**Still open (`False`):** no action.

---

## 14. Pause and Resume

### PAUSED task resume (`_resume_paused_tasks`)

Runs every cycle.  Lists all PAUSED tasks.  For each task where
`task.resume_after <= time.time()`:
`db.transition_task(id, READY, context="resume_paused", assigned_agent_id=None, resume_after=None)`.

### How tasks become PAUSED

Inside `_execute_task`, when `output.result` is `PAUSED_TOKENS` or `PAUSED_RATE_LIMIT`
(and rate-limit auto-retries have been exhausted):

```
resume_after = now + retry_secs
db.transition_task(task_id, PAUSED, context="tokens_exhausted", resume_after=...)
```

`retry_secs` comes from:
- `PAUSED_RATE_LIMIT`: `config.pause_retry.rate_limit_backoff_seconds`
- `PAUSED_TOKENS`: `config.pause_retry.token_exhaustion_retry_seconds`

A brief notification is sent to the task thread or notifications channel.

### Global pause (`pause()` / `resume()`)

`orchestrator.pause()` sets `_paused = True`.  The scheduling step (step 3) in
`run_one_cycle` is skipped, so no new tasks are assigned.  All other cycle steps
continue running.  `orchestrator.resume()` sets `_paused = False`.

---

## 15. Admin Operations

### `skip_task(task_id) -> (error | None, list[Task])`

Allowed states: BLOCKED or FAILED only.  Any other state returns an error string.

1. `db.transition_task(task_id, COMPLETED, context="skip_task")`.
2. `db.log_event("task_skipped", ...)`.
3. Fetch `db.get_dependents(task_id)`.  For each dependent in status DEFINED whose
   dependencies are all now met: add to `unblocked` list.
4. Notify the channel with a summary, including the unblock count.
5. Return `(None, unblocked)`.

The actual promotion of unblocked tasks from DEFINED to READY happens in the next
`_check_defined_tasks` cycle, not immediately in this method.

### `stop_task(task_id) -> error | None`

Allowed state: IN_PROGRESS only.  Any other state returns an error string.

1. Fetch `agent_id` from the task record.
2. If `agent_id` is set and an adapter exists for it: call `adapter.stop()` (best-effort;
   exceptions are logged and swallowed).
3. `db.transition_task(task_id, BLOCKED, context="stop_task", assigned_agent_id=None)`.
4. If `agent_id` is set: `db.update_agent(agent_id, state=IDLE, current_task_id=None)` and
   remove the adapter from `_adapters`.
5. Notify the channel.
6. Call `_notify_stuck_chain(task)`.
7. Return `None`.

---

## 16. Shutdown

`async shutdown()`

1. `await wait_for_running_tasks(timeout=10)` — waits up to 10 seconds for all background
   task-execution coroutines to finish.  Tasks still running after the timeout are
   abandoned (the process is exiting).
2. If `hooks` is set: `await hooks.shutdown()`.
3. `await db.close()`.

`wait_for_running_tasks(timeout)` collects the values of `_running_tasks` into a list and
calls either `asyncio.wait(tasks, timeout=timeout)` (if a timeout is provided) or
`asyncio.gather(*tasks, return_exceptions=True)` (if no timeout).  Returns immediately
if `_running_tasks` is empty.

---

## 17. Callbacks

The orchestrator is wired to Discord by injecting two callbacks after construction but
before the scheduling loop starts.  Neither callback is required — the orchestrator runs
without them (notifications are silently dropped).

### `set_notify_callback(callback: NotifyCallback)`

```python
NotifyCallback = Callable[[str, str | None], Awaitable[None]]
```

Arguments: `(message: str, project_id: str | None)`.

`_notify_channel(message, project_id)` is the internal wrapper.  It calls the callback
inside a try/except, logging errors to stdout.  When `project_id` is provided, the Discord
bot uses it to route the message to the project's dedicated channel, falling back to the
global notifications channel if none is configured.

### `set_create_thread_callback(callback: CreateThreadCallback)`

```python
ThreadSendCallback = Callable[[str], Awaitable[None]]
CreateThreadCallback = Callable[
    [str, str, str | None],
    Awaitable[tuple[ThreadSendCallback, ThreadSendCallback] | None],
]
```

Arguments to the callback: `(thread_name: str, initial_message: str, project_id: str | None)`.

Returns `(send_to_thread, notify_main)` or `None` if thread creation fails.

- `send_to_thread(text)` — appends content to the Discord thread for this task.  Used to
  stream all agent output and post completion/failure summaries.
- `notify_main(text)` — posts a brief message to the thread-root reply in the main
  notifications channel.  Used for completion/failure one-liners so operators see a summary
  without having to open the thread.

When `_create_thread` is not set, all output falls back to `_notify_channel`.

---

## Appendix: Key Constants

| Constant | Default | Location | Purpose |
|---|---|---|---|
| `_MAX_RETRIES` | 10 | `task_names.py` | Max random attempts before using suffixed fallback |
| `_NO_PR_REMINDER_INTERVAL` | 3600s | `Orchestrator` | Min gap between no-PR approval reminders |
| `_NO_PR_ESCALATION_THRESHOLD` | 86400s | `Orchestrator` | Age at which no-PR reminder escalates |
| `_NO_PR_AUTO_COMPLETE_GRACE` | 120s | `Orchestrator` | Grace period before auto-completing non-approval tasks with no PR |
| Approval poll interval | 60s | `_check_awaiting_approval` | Rate limit on PR status checks |
| Shutdown timeout | 10s | `shutdown` | Max wait for running tasks before close |
