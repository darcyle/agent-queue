# Make GitManager Non-Blocking

**Task ID:** `sharp-crest`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 101
**Branch:** `solid-current/if-something-restarts-agent-queue-make-the-command-to-restart-agent-queue-require-a-reason-and-log`
**Parent Task:** `solid-current`
**Plan Subtask:** Yes
**Plan Source:** `/mnt/d/Dev/agent-queue3/.claude/plans/solid-current-plan.md`
**Dependencies:** `solid-cascade`
**Archived:** 2026-03-13 16:38:10

## Description

**Make GitManager Non-Blocking**

## Background Context
---
auto_tasks: true
---


##

This task is part of the implementation plan from: **If something restarts agent queue, make the command to restart agent queue require a reason, and log**

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

**Summary:** Done. The main async GitManager work was already completed and merged to `origin/main` by the upstream dependency task. I rebased this branch onto main and fixed 3 pre-existing test failures that were broken by recent changes:

1. **`src/git/manager.py`** — Added `--yes` flag to both `create_github_repo()` and `acreate_github_repo()` to prevent interactive prompts (matching the existing test expectation).

2. **`tests/test_hook_events.py`** and **`tests/test_hooks.py`** — Fixed mock orchestrator fixtures to use `AsyncMock()` for `_notify_channel`, which was being `await`ed after the hook logging feature was added.

All 1674 tests pass with 40 skipped.

**Tokens Used:** 6,035
