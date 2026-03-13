# Make GitManager Non-Blocking

**Task ID:** `calm-crest`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 101
**Branch:** `clear-lantern/when-agent-queues-chat-agent-is-processing-any-kind-of-request-from-the-user-it-seems-that-the-orc`
**Parent Task:** `clear-lantern`
**Plan Subtask:** Yes
**Plan Source:** `/mnt/d/Dev/agent-queue2/.claude/plans/clear-lantern-plan.md`
**Dependencies:** `vivid-current`
**Archived:** 2026-03-13 16:38:10

## Description

**Make GitManager Non-Blocking**

## Background Context
---
auto_tasks: true
---


##

This task is part of the implementation plan from: **When agent-queue's chat agent is processing any kind of request from the user, it seems that the orc**

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

**Summary:** Done. Here's a summary of what was implemented:

**`src/git/manager.py`** — Added async API to GitManager:
- **`_arun()`** — Core async method using `asyncio.create_subprocess_exec()` with `asyncio.wait_for()` for timeout handling. Kills the subprocess on timeout to avoid orphaned processes.
- **`_arun_subprocess()`** — Async helper for non-git commands (e.g., `gh` CLI) that returns a `CompletedProcess`-compatible result.
- **All public methods have async counterparts** prefixed with `a`:
  - `acreate_checkout`, `avalidate_checkout`, `ahas_remote`, `acreate_branch`, `acheckout_branch`, `alist_branches`
  - `apull_latest_main`, `aprepare_for_task`, `aswitch_to_branch`, `amid_chain_sync`
  - `apull_branch`, `apush_branch`, `arebase_onto`, `amerge_branch`, `async_and_merge`
  - `arecover_workspace`, `adelete_branch`, `acreate_worktree`, `aremove_worktree`, `ainit_repo`
  - `aget_diff`, `aget_changed_files`, `acommit_all`, `acreate_pr`, `acheck_pr_merged`
  - `aget_status`, `aget_current_branch`, `ahas_non_plan_changes`, `aget_default_branch`, `aget_recent_commits`
  - `acheck_gh_auth`, `acreate_github_repo`
- All synchronous methods preserved for backward compatibility.

**`tests/test_git_manager_async.py`** — 26 new tests covering `_arun`, `_arun_subprocess`, and all key async public methods using real git repos.
The background task notification is for a task that already completed earlier — the async tests passed (26/26) and the commit has already been made. No further action needed.

**Tokens Used:** 17,461
