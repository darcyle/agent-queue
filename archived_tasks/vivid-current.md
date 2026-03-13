# Problem Analysis

**Task ID:** `vivid-current`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 100
**Branch:** `clear-lantern/when-agent-queues-chat-agent-is-processing-any-kind-of-request-from-the-user-it-seems-that-the-orc`
**Parent Task:** `clear-lantern`
**Plan Subtask:** Yes
**Plan Source:** `/mnt/d/Dev/agent-queue2/.claude/plans/clear-lantern-plan.md`
**Archived:** 2026-03-13 16:38:10

## Description

**Problem Analysis**

## Background Context
---
auto_tasks: true
---


##

This task is part of the implementation plan from: **When agent-queue's chat agent is processing any kind of request from the user, it seems that the orc**

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

**Key changes:**

1. **`src/git/manager.py`** — The core `_run()` method now uses `asyncio.create_subprocess_exec()` instead of `subprocess.run()`, with `asyncio.wait_for()` for timeout handling. Added `_run_subprocess()` helper for non-git commands (like `gh` CLI) that returns a `CompletedProcess`-compatible result. All methods that were synchronous are now properly async.

2. **`src/command_handler.py`** — All direct `subprocess.run()` calls (in `_cmd_find_merge_conflict_workspaces`, `_cmd_run_command`, `_cmd_search_files`, `_cmd_self_update`, etc.) are wrapped with `asyncio.to_thread()` so they run in a thread pool instead of blocking the event loop.

3. **`src/orchestrator.py`** — Updated all callers to match the new async git method signatures.

4. **Tests (10 files)** — Updated all test files to properly mock async subprocess calls and match the new async API.

All 350 tests pass (232 + 118), with 39 skipped (pre-existing).

**Tokens Used:** 3,397
