# When agent-queue's chat agent is processing any kind of request from the user, it seems that the orc

**Task ID:** `clear-lantern`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 100
**Branch:** `clear-lantern/when-agent-queues-chat-agent-is-processing-any-kind-of-request-from-the-user-it-seems-that-the-orc`
**Archived:** 2026-03-13 16:38:09

## Description

When agent-queue's chat agent is processing any kind of request from the user, it seems that the orchistrator is unable to respond to commands from the user. Please analyze our architechure, understand why this is the case and plan a refactor of the system that will fix this problem so that commands can still be responded to immediately even when the orchistrator is doing some work with the chat agent.

## Result

**Summary:** The plan has been committed. Here's a summary of the analysis and plan:

## Root Cause Analysis

The orchestrator and Discord bot run as concurrent `asyncio` tasks in a **single-threaded event loop**. When the chat agent processes a user request, it calls git operations through `GitManager._run()` which uses **synchronous `subprocess.run()`** — this blocks the entire event loop for up to 120 seconds. During that time:

1. **Slash commands** queue up and can't be responded to (Discord's 3s interaction timeout expires)
2. **The orchestrator's 5-second cycle** can't run (task scheduling, PR monitoring, etc.)
3. **All other async tasks** are frozen

While the LLM API calls are properly async, the git subprocess calls are the bottleneck.

## 5-Phase Refactoring Plan

| Phase | What | Impact |
|-------|------|--------|
| **1** | Make `GitManager._run()` async using `asyncio.create_subprocess_exec()` | **Highest** — all git ops flow through this one method |
| **2** | Convert direct `subprocess.run()` calls in `command_handler.py` | **High** — `_cmd_find_merge_conflict_workspaces` has 8+ blocking calls |
| **3** | Convert synchronous file I/O in `discord/bot.py` | **Medium** — `_load_notes_threads`/`_save_notes_threads` block briefly |
| **4** | Update all callers to use the new async git methods | **Required** — connects phases 1-2 to the rest of the codebase |
| **5** | (Optional) Run chat agent as detached background task | **Enhancement** — prevents even slow LLM calls from serializing channels |

**Tokens Used:** 4,995
