# Problem Analysis

**Task ID:** `solid-cascade`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 100
**Branch:** `solid-current/if-something-restarts-agent-queue-make-the-command-to-restart-agent-queue-require-a-reason-and-log`
**Parent Task:** `solid-current`
**Plan Subtask:** Yes
**Plan Source:** `/mnt/d/Dev/agent-queue3/.claude/plans/solid-current-plan.md`
**Archived:** 2026-03-13 16:38:10

## Description

**Problem Analysis**

## Background Context
---
auto_tasks: true
---


##

This task is part of the implementation plan from: **If something restarts agent queue, make the command to restart agent queue require a reason, and log**

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

### `src/git/manager.py` — Core async infrastructure
- Added `_arun()` — async version of `_run()` using `asyncio.create_subprocess_exec()` with `asyncio.wait_for()` for timeout handling. Kills subprocess on timeout to prevent orphaned processes.
- Added `_arun_subprocess()` — async helper for non-git commands (e.g., `gh` CLI) returning a `CompletedProcess`-compatible result.
- Added **30+ async public methods** prefixed with `a` (e.g., `acreate_checkout`, `avalidate_checkout`, `acommit_all`, `acreate_pr`, `async_and_merge`, etc.)
- All original synchronous methods preserved for backward compatibility.

### `src/orchestrator.py` — Updated all git callers
- Added `_aget_default_branch()` async version of `_get_default_branch()`
- Converted all git calls in `_prepare_workspace()`, `_complete_workspace()`, `_merge_and_push()`, `_create_pr_for_task()`, `_task_has_code_changes()`, `_phase_commit()`, `_phase_merge()`, `_pipeline_merge_and_push()`, and PR polling to use async git methods.

### `src/command_handler.py` — Updated all git callers
- All git method calls converted to async versions
- `_cmd_find_merge_conflict_workspaces()`: extracted sync subprocess logic into a helper function and wrapped with `asyncio.to_thread()`
- `_cmd_sync_workspace_single()`: converted all `git._run()` calls to `git._arun()`
- `_cmd_update_and_restart()`: wrapped `subprocess.run` calls with `asyncio.to_thread()`

### `src/discord/commands.py` — Wrapped subprocess calls
- All 5 `subprocess.run()` calls in `/restart` and `/update` commands wrapped with `asyncio.to_thread()`

### Tests (6 files updated)
- Updated mock fixtures to include `AsyncMock` counterparts for all async git methods
- Updated all assertions to check async method names
- All 1673 tests pass (40 pre-existing skips)
The background task notification is for the test run that already completed earlier — all 1673 tests passed. The commit has already been made. No further action needed.

**Tokens Used:** 62,838
