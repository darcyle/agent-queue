# Problem Analysis

**Task ID:** `grand-beacon`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 100
**Branch:** `nimble-falcon/review-specs-for-task-sharp-crest-changes`
**Parent Task:** `nimble-falcon`
**Plan Subtask:** Yes
**Plan Source:** `/mnt/d/Dev/agent-queue3/.claude/plans/nimble-falcon-plan.md`
**Archived:** 2026-03-13 16:38:10

## Description

**Problem Analysis**

## Background Context
---
auto_tasks: true
---


##

This task is part of the implementation plan from: **Review specs for task sharp-crest changes**

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

**Summary:** Done. Here's a summary of what was accomplished:

**Review findings:**
- **Phase 1 (GitManager async)** ✅ Already complete — `_arun()`, `_arun_subprocess()`, and all 25+ async public methods (`a*` prefix) exist in `src/git/manager.py`
- **Phase 2 (command_handler subprocess)** ✅ Already complete — All `subprocess.run()` calls in `command_handler.py` and `discord/commands.py` are wrapped with `asyncio.to_thread()`
- **Phase 3 (file I/O)** ⬜ Not started but negligible impact — `_load_notes_threads`/`_save_notes_threads` are tiny local file ops
- **Phase 4 (caller migration)** ✅ Already complete — `orchestrator.py` uses `await self.git.a*()` throughout, `command_handler.py` uses async git API
- **Phase 5 (background chat)** ⬜ Optional enhancement, separate concern

**Action taken:** Updated `.claude/plan.md` to reflect the current implementation status and set `auto_tasks: false` to prevent generating redundant tasks for already-completed work.

**Tokens Used:** 9,697
