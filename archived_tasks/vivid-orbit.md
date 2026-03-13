# Problem Analysis

**Task ID:** `vivid-orbit`
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

### `src/git/manager.py` — Core async infrastructure
- Added `_arun()` — async version of `_run()` using `asyncio.create_subprocess_exec()` with `asyncio.wait_for()` for timeout handling. Kills the subprocess on timeout to avoid orphaned processes.
- Added `_arun_subprocess()` — async helper for non-git commands (e.g., `gh` CLI) returning a `CompletedProcess`-compatible result.
- Added **27 async public methods** (all prefixed with `a`): `acreate_checkout`, `avalidate_checkout`, `ahas_remote`, `acreate_branch`, `acheckout_branch`, `alist_branches`, `apull_latest_main`, `aprepare_for_task`, `aswitch_to_branch`, `amid_chain_sync`, `apull_branch`, `apush_branch`, `arebase_onto`, `amerge_branch`, `async_and_merge`, `arecover_workspace`, `adelete_branch`, `acreate_worktree`, `aremove_worktree`, `ainit_repo`, `aget_diff`, `aget_changed_files`, `acommit_all`, `acreate_pr`, `acheck_pr_merged`, `aget_status`, `aget_current_branch`, `ahas_non_plan_changes`, `aget_default_branch`, `aget_recent_commits`, `acheck_gh_auth`, `acreate_github_repo`.

### `src/orchestrator.py` — 44 git calls converted to async
All synchronous git method calls replaced with their async counterparts throughout `_prepare_workspace`, `_complete_workspace`, `_merge_and_push`, `_create_pr_for_task`, `_generate_tasks_from_plan`, `_phase_commit`, `_phase_merge`, `_pipeline_merge_and_push`, `_check_pr_status`, and `_get_default_branch` (which was also made async).

### `src/command_handler.py` — Git calls + subprocess wrapping
- All git method calls converted to async counterparts
- 12 direct `subprocess.run()` calls wrapped with `asyncio.to_thread()` in `_cmd_find_merge_conflict_workspaces`, `_cmd_sync_workspace`, and `_cmd_update_and_restart`

### `src/discord/commands.py` — 5 subprocess calls wrapped with `asyncio.to_thread()`

### Tests (7 files) — Updated mock fixtures to include `AsyncMock` counterparts

All **1696 tests pass** (393 skipped).

**Tokens Used:** 21,384
