# Make GitManager Non-Blocking

**Task ID:** `smart-rapids`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 101
**Branch:** `steady-delta/explore-if-its-possible-to-get-usage-information-from-claude-code-percentage-used-per-session-whe`
**Parent Task:** `steady-delta`
**Plan Subtask:** Yes
**Plan Source:** `/mnt/d/Dev/agent-queue2/.claude/plans/steady-delta-plan.md`
**Dependencies:** `fair-lantern`
**Archived:** 2026-03-13 16:38:10

## Description

**Make GitManager Non-Blocking**

## Background Context
---
auto_tasks: true
---


##

This task is part of the implementation plan from: **Explore if it's possible to get usage information from claude code (percentage used per session, whe**

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

**Summary:** No git calls in `chat_agent.py`. The task is already fully complete — the upstream work implemented everything:

1. ✅ `_arun()` — async subprocess execution with timeout
2. ✅ `_arun_subprocess()` — async helper for non-git commands
3. ✅ 30+ async public methods (all prefixed with `a`)
4. ✅ Sync `_run()` preserved for backward compatibility
5. ✅ All callers in `orchestrator.py` and `command_handler.py` use async variants

There are no new changes needed, so there's nothing to commit. The task was completed as part of prior upstream work.

**Tokens Used:** 1,631
