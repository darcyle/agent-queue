# Problem Analysis

**Task ID:** `fair-lantern`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 100
**Branch:** `steady-delta/explore-if-its-possible-to-get-usage-information-from-claude-code-percentage-used-per-session-whe`
**Parent Task:** `steady-delta`
**Plan Subtask:** Yes
**Plan Source:** `/mnt/d/Dev/agent-queue2/.claude/plans/steady-delta-plan.md`
**Archived:** 2026-03-13 16:38:10

## Description

**Problem Analysis**

## Background Context
---
auto_tasks: true
---


##

This task is part of the implementation plan from: **Explore if it's possible to get usage information from claude code (percentage used per session, whe**

## High-Level Steps
- Primary Blocker: `GitManager._run()` uses `subprocess.run()`
- Secondary Blocker: Direct `subprocess.run()` in `command_handler.py`
- Blocking Flow
- Impact

## Task Details
When the chat agent is processing a user request, the orchestrator cannot respond to
Discord slash commands or run its scheduled cycle. Both the Discord bot and the orchestrator
scheduler run as concurrent `asyncio.create_task()` coroutines in a **single-threaded event
loop** (see `src/main.py` lines 109-110).

The root cause is **synchronous blocking calls** that freeze the event loop:

### Primary Blocker: `GitManager._run()` uses `subprocess.run()`

`src/git/manager.py` line 90-108 — The `_run()` method calls `subprocess.run()` which is
synchronous and blocks the entire asyncio event loop for up to 120 seconds (the default
`_GIT_TIMEOUT`). Every git operation goes through this method:
- `validate_checkout()`, `get_current_branch()`, `get_status()`, `get_recent_commits()`
- All commit, push, pull, merge, checkout, diff operations

### Secondary Blocker: Direct `subprocess.run()` in `command_handler.py`

`src/command_handler.py` contains direct `subprocess.run()` calls in:
- `_cmd_find_merge_conflict_workspaces()` (lines ~2923-3013) — 8+ sequential subprocess calls
  per workspace, each with 10-30s timeouts
- Various git-related commands that call GitManager synchronous methods

### Blocking Flow

```
Event loop (single-threaded)
├─ Discord bot task
│  └─ on_message() → chat_agent.chat() → command_handler.execute()
│     └─ git_manager._run() → subprocess.run()  ← BLOCKS EVENT LOOP
│
└─ Scheduler task ← CANNOT RUN while event loop is blocked
   └─ orch.run_one_cycle() — stuck waiting
```

While the LLM API calls (`provider.create_message()`) are properly async and yield control
back to the event loop, the git subprocess calls are synchronous and block everything.

### Impact

When a chat request triggers git operations:
1. Discord slash commands queue up and don't respond until blocking completes
2. The orchestrator's 5-second cycle is delayed (task scheduling, PR monitoring, etc.)
3. Discord's interaction timeout (3 seconds) can expire, causing "interaction failed" errors

---

## Result

**Summary:** The commit is done. Here's a summary of what was implemented:

## Changes Made

### `src/command_handler.py`
- **`_cmd_find_merge_conflict_workspaces`**: Replaced 8 `asyncio.to_thread(subprocess.run, ...)` calls with `git._arun()` and `git._arun_subprocess()` — native async subprocess execution without thread pool overhead
- **`_cmd_run_command`**: Replaced `asyncio.to_thread(subprocess.run, ..., shell=True)` with `asyncio.create_subprocess_shell()` with proper timeout via `asyncio.wait_for()`
- **`_cmd_search_files`**: Replaced 2 `asyncio.to_thread(subprocess.run, ...)` calls with `asyncio.create_subprocess_exec()`
- **`_cmd_update_and_restart`**: Replaced 2 `asyncio.to_thread(subprocess.run, ...)` calls with `git._arun_subprocess()`
- Removed `import subprocess` (no longer needed)
- **`_sync_single_workspace`**: Consolidated duplicate `git status --porcelain` calls into a single `_arun_subprocess` call

### `src/discord/commands.py`
- Added `_async_git_output()` helper using `asyncio.create_subprocess_exec()`
- Converted 5 subprocess calls in restart/update commands to use the new helper
- Removed `import subprocess`
- Updated `_save_notes_threads` and `register_notes_thread` callers to use `await`

### `src/discord/bot.py`
- Made `_save_notes_threads` async (uses `asyncio.to_thread` for file I/O)
- Added `_save_notes_threads_sync` helper for the sync callback path
- Made `register_notes_thread` async
- `clear_project_channels` (sync callback) uses the sync helper directly

### Tests
- `tests/test_smart_workspace_assignment.py`: Use real `GitManager` instead of `MagicMock`
- `tests/test_bot_channel_cache.py`: Updated `BotCaches` mock for new method signatures
The earlier background test run also completed successfully. The work is done and committed.
All background tasks have completed successfully. The work is committed and ready — all 1691 tests pass with no regressions from the changes.
All done — the final full test run also confirmed 1691 passed, 0 new failures. The commit is ready.

**Tokens Used:** 24,790
