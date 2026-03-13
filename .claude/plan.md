---
auto_tasks: false
---

# Plan: Fix Orchestrator Responsiveness During Chat Agent Processing

## Problem Analysis

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

## Implementation Status

### Phase 1: Make GitManager Non-Blocking — COMPLETED

`src/git/manager.py` now has:
- `async def _arun()` — Core async method using `asyncio.create_subprocess_exec()` with
  `asyncio.wait_for()` for timeout handling. Kills subprocess on timeout.
- `async def _arun_subprocess()` — Async helper for non-git commands (e.g. `gh` CLI)
  returning `CompletedProcess`-compatible result.
- All public methods have async counterparts prefixed with `a`: `avalidate_checkout`,
  `aget_current_branch`, `aget_status`, `acommit_all`, `acreate_pr`, `acheck_pr_merged`,
  `apush_branch`, `apull_branch`, `amerge_branch`, `async_and_merge`, `arebase_onto`,
  `adelete_branch`, `acreate_worktree`, `aremove_worktree`, `ainit_repo`, `aget_diff`,
  `aget_changed_files`, `ahas_non_plan_changes`, `aget_default_branch`,
  `aget_recent_commits`, `acheck_gh_auth`, `acreate_github_repo`, etc.
- Synchronous methods preserved for backward compatibility.

### Phase 2: Convert Direct subprocess.run() Calls — COMPLETED

All direct `subprocess.run()` calls in async contexts are now non-blocking:
- `src/command_handler.py` — All `subprocess.run()` calls (in `_cmd_find_merge_conflict_workspaces`,
  `_cmd_sync_workspace_single`, `_cmd_update_and_restart`, `_cmd_run_command`,
  `_cmd_search_files`) are wrapped with `asyncio.to_thread()`.
- `src/discord/commands.py` — All subprocess calls wrapped with `asyncio.to_thread()`.

### Phase 3: Convert Synchronous File I/O in Discord Bot — NOT STARTED (Low Priority)

`src/discord/bot.py` — `_load_notes_threads()` and `_save_notes_threads()` use
synchronous `open()`/`json.load()`/`json.dump()`. These are small local file operations
that complete in microseconds, so the blocking impact is negligible. Could be wrapped
with `asyncio.to_thread()` for correctness but is very low priority.

### Phase 4: Update All Callers to Use Async Git Methods — COMPLETED

All callers in the main async code paths use the async git API:
- `src/orchestrator.py` — All git calls use `await self.git.a*()` methods.
- `src/command_handler.py` — All git calls use `await git.a*()` or `await git._arun()`.
- `src/discord/commands.py` — Subprocess calls wrapped with `asyncio.to_thread()`.

### Phase 5: Run Chat Agent in Background Task — NOT STARTED (Optional Enhancement)

This is a larger architectural change. The async subprocess fixes in Phases 1-4 address
the primary blocking issue. The chat agent lock serialization is a separate concern that
should be evaluated independently.

## Summary

**The primary blocking issue is resolved.** Phases 1, 2, and 4 are complete. The event loop
is no longer blocked by git subprocess calls. Remaining items (Phase 3 file I/O, Phase 5
background chat) are low-priority enhancements that do not affect responsiveness in practice.
