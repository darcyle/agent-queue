# Make GitManager Non-Blocking

**Task ID:** `bold-orbit`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 101
**Branch:** `smart-ridge/when-a-projects-hook-executes-it-should-log-that-it-is-running-in-the-projects-chat`
**Parent Task:** `smart-ridge`
**Plan Subtask:** Yes
**Plan Source:** `/mnt/d/Dev/agent-queue2/.claude/plans/smart-ridge-plan.md`
**Dependencies:** `vivid-lantern`
**Archived:** 2026-03-13 16:38:10

## Description

**Make GitManager Non-Blocking**

## Background Context
---
auto_tasks: true
---


##

This task is part of the implementation plan from: **When a project’s hook executes it should log that it is running in the project’s chat**

## Task Details
Convert `GitManager._run()` from synchronous `subprocess.run()` to async using
`asyncio.create_subprocess_exec()`. This is the highest-impact change since ALL git
operations flow through this single method.

**Files to modify:**
- `src/git/manager.py` — Add `async def _arun()` method using `asyncio.create_subprocess_exec()`,
  then convert all public methods that call `_run()` to have async counterparts (or convert
  them directly to async). The synchronous `_run()` should be kept for backward compatibility
  in non-async contexts (e.g., if any caller is synchronous), but all callers from
  `command_handler.py` and `chat_agent.py` should switch to the async versions.

**Key changes:**
- Add `async def _arun(self, args, cwd=None, timeout=None) -> str` that uses
  `asyncio.create_subprocess_exec()`
- Convert public methods to async: `async def get_current_branch()`,
  `async def get_status()`, `async def validate_checkout()`, etc.
- Keep sync `_run()` for use in non-async contexts (orchestrator task execution
  subprocess launching, etc.)

## Result

**Summary:** All 1674 tests pass (40 skipped). The task is already fully implemented — `_arun()`, `_arun_subprocess()`, and all 30+ async public methods are already in `src/git/manager.py`, and synchronous `_run()` is preserved for backward compatibility. There are no uncommitted changes, so there's nothing new to commit.

The work was completed in commit `5b6c62a` ("Make all git subprocess calls async to unblock the event loop") which is already part of this branch's history.

**Tokens Used:** 2,052
