# Convert Direct subprocess.run() Calls in command_handler.py

**Task ID:** `fresh-ridge`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 102
**Branch:** `steady-delta/explore-if-its-possible-to-get-usage-information-from-claude-code-percentage-used-per-session-whe`
**Parent Task:** `steady-delta`
**Plan Subtask:** Yes
**Plan Source:** `/mnt/d/Dev/agent-queue2/.claude/plans/steady-delta-plan.md`
**Dependencies:** `calm-rapids`
**Archived:** 2026-03-13 16:38:10

## Description

**Convert Direct subprocess.run() Calls in command_handler.py**

## Background Context
---
auto_tasks: true
---


##

This task is part of the implementation plan from: **Explore if it's possible to get usage information from claude code (percentage used per session, whe**

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

**Summary:** Done. Here's a summary of what was implemented:

**Added `_async_run()` helper function** — a module-level async utility that replaces `asyncio.to_thread(subprocess.run, ...)` with native `asyncio.create_subprocess_exec()` (or `asyncio.create_subprocess_shell()` for shell commands), using `asyncio.wait_for()` for timeout handling. Also added `_SubprocessTimeout` exception to replace `subprocess.TimeoutExpired`.

**Converted 15 blocking subprocess calls** across 5 methods:
- `_cmd_find_merge_conflict_workspaces` — 8 git subprocess calls (fetch, rev-parse, status, branch list, merge-base, merge-tree, rev-list)
- `_sync_single_workspace` — 2 git status calls
- `_cmd_update_and_restart` — git pull + pip install
- `_cmd_run_command` — shell command execution
- `_cmd_search_files` — grep and find commands

**Removed `import subprocess`** — no longer needed since all calls now use native async subprocess APIs.

All 158 command handler tests pass.

**Tokens Used:** 8,408
