# Convert Direct subprocess.run() Calls in command_handler.py

**Task ID:** `keen-delta`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 102
**Branch:** `clear-lantern/when-agent-queues-chat-agent-is-processing-any-kind-of-request-from-the-user-it-seems-that-the-orc`
**Parent Task:** `clear-lantern`
**Plan Subtask:** Yes
**Plan Source:** `/mnt/d/Dev/agent-queue2/.claude/plans/clear-lantern-plan.md`
**Dependencies:** `calm-crest`
**Archived:** 2026-03-13 16:38:10

## Description

**Convert Direct subprocess.run() Calls in command_handler.py**

## Background Context
---
auto_tasks: true
---


##

This task is part of the implementation plan from: **When agent-queue's chat agent is processing any kind of request from the user, it seems that the orc**

## Task Details
Convert all remaining synchronous `subprocess.run()` calls in `command_handler.py` that
are not going through GitManager to use `asyncio.create_subprocess_exec()` or
`asyncio.to_thread()`.

**Files to modify:**
- `src/command_handler.py` — Focus on `_cmd_find_merge_conflict_workspaces()` which has
  8+ direct `subprocess.run()` calls. Convert each to use `asyncio.create_subprocess_exec()`.

**Key changes:**
- Replace all `subprocess.run(["git", ...])` calls with async subprocess equivalents
- Ensure timeout handling is preserved (use `asyncio.wait_for()` around `proc.communicate()`)

## Result

**Summary:** Done. All direct `subprocess.run()` calls in `command_handler.py` have been converted to async equivalents:

1. **`_cmd_find_merge_conflict_workspaces`** — 8 `asyncio.to_thread(subprocess.run, ...)` → `git._arun()` and `git._arun_subprocess()` 
2. **`_sync_single_workspace`** — 2 duplicate status calls consolidated into 1 `git._arun_subprocess()` call
3. **`_cmd_update_and_restart`** — 2 calls → `git._arun_subprocess()`
4. **`_cmd_run_command`** — `asyncio.create_subprocess_shell()` with `asyncio.wait_for()` for timeout
5. **`_cmd_search_files`** — 2 calls → `asyncio.create_subprocess_exec()` with timeout

All 94 related tests pass. No `asyncio.to_thread(subprocess.run, ...)` patterns remain in the file.

**Tokens Used:** 9,082
