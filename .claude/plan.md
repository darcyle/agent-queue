---
auto_tasks: true
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

## Phase 1: Make GitManager Non-Blocking

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

## Phase 2: Convert Direct subprocess.run() Calls in command_handler.py

Convert all remaining synchronous `subprocess.run()` calls in `command_handler.py` that
are not going through GitManager to use `asyncio.create_subprocess_exec()` or
`asyncio.to_thread()`.

**Files to modify:**
- `src/command_handler.py` — Focus on `_cmd_find_merge_conflict_workspaces()` which has
  8+ direct `subprocess.run()` calls. Convert each to use `asyncio.create_subprocess_exec()`.

**Key changes:**
- Replace all `subprocess.run(["git", ...])` calls with async subprocess equivalents
- Ensure timeout handling is preserved (use `asyncio.wait_for()` around `proc.communicate()`)

## Phase 3: Convert Synchronous File I/O in Discord Bot

Convert the synchronous file I/O in `discord/bot.py` to non-blocking.

**Files to modify:**
- `src/discord/bot.py` — `_load_notes_threads()` and `_save_notes_threads()` use
  synchronous `open()`/`json.load()`/`json.dump()`. Wrap with `asyncio.to_thread()` or
  use `aiofiles`.

## Phase 4: Update All Callers to Use Async Git Methods

Update all callers of the now-async GitManager methods throughout the codebase.

**Files to modify:**
- `src/command_handler.py` — Update all command handlers that call git methods to await
  the async versions
- `src/discord/commands.py` — Update any direct git calls
- `src/orchestrator.py` — Update any direct git calls (note: orchestrator task execution
  already runs in background tasks, so these may already be in async context)

**Approach:**
- Search for all `.get_current_branch()`, `.get_status()`, `.validate_checkout()`,
  `.get_recent_commits()` etc. calls
- Ensure each call site is in an async context and uses `await`

## Phase 5: Run Chat Agent in Background Task (Optional Enhancement)

As an additional safeguard, consider running the chat agent's processing as a detached
background task rather than holding the channel lock for the full duration. This would
prevent even slow (but non-blocking) LLM API calls from serializing all interactions
on a channel.

**Current pattern:**
```python
async with lock:
    response = await self.agent.chat(...)  # holds lock for entire multi-turn conversation
```

**Improved pattern:**
```python
# Immediately acknowledge, then process in background
asyncio.create_task(self._process_chat(message, user_text, ...))
```

This is a larger architectural change and should be evaluated separately — the async
subprocess fix in Phases 1-4 addresses the primary blocking issue.
