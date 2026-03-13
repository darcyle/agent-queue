# Convert Direct subprocess.run() Calls in command_handler.py

**Task ID:** `nimble-rapids`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 102
**Branch:** `solid-current/if-something-restarts-agent-queue-make-the-command-to-restart-agent-queue-require-a-reason-and-log`
**Parent Task:** `solid-current`
**Plan Subtask:** Yes
**Plan Source:** `/mnt/d/Dev/agent-queue3/.claude/plans/solid-current-plan.md`
**Dependencies:** `sharp-crest`
**Archived:** 2026-03-13 16:38:10

## Description

**Convert Direct subprocess.run() Calls in command_handler.py**

## Background Context
---
auto_tasks: true
---


##

This task is part of the implementation plan from: **If something restarts agent queue, make the command to restart agent queue require a reason, and log**

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

**Summary:** Done. Here's a summary of the changes:

**Added two module-level async helper functions:**
- `_run_subprocess(*args, cwd, timeout)` — wraps `asyncio.create_subprocess_exec()` with `asyncio.wait_for()` for timeout handling, returns `(returncode, stdout, stderr)`
- `_run_subprocess_shell(command, cwd, timeout)` — same but uses `asyncio.create_subprocess_shell()` for shell commands

**Converted 15 call sites across 5 methods:**
1. `_cmd_find_merge_conflict_workspaces` — 8 git subprocess calls
2. `_cmd_sync_workspace_single` — 2 git status calls
3. `_cmd_update_and_restart` — git pull + pip install
4. `_cmd_run_command` — 1 shell command (uses `_run_subprocess_shell`)
5. `_cmd_search_files` — grep + find commands

All exception handlers updated from `subprocess.TimeoutExpired` to `asyncio.TimeoutError`. All 1674 tests pass.
The background task notification confirms the tests completed successfully (exit code 0) — I already retrieved those results earlier. The work is committed and ready.

**Tokens Used:** 4,102
