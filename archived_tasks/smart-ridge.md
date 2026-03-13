# When a project’s hook executes it should log that it is running in the project’s chat

**Task ID:** `smart-ridge`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 100
**Branch:** `smart-ridge/when-a-projects-hook-executes-it-should-log-that-it-is-running-in-the-projects-chat`
**Archived:** 2026-03-13 16:38:10

## Description

When a project’s hook executes it should log that it is running in the project’s chat

## Result

**Summary:** The change is committed. Here's what was done:

**Change:** In `src/hooks.py`, added a notification to the project's Discord chat channel when a hook starts executing.

**How it works:** After the `HookRun` record is created in the database (marking the hook as "running"), a message is sent via the orchestrator's `_notify_channel` method to the project's chat channel. The message includes the hook name and trigger reason (e.g., "periodic" or "event:task.completed"). Since `_notify_channel` accepts a `project_id`, it automatically routes to the project's dedicated Discord channel if one is configured, or falls back to the global channel with a project tag prefix.

**Tokens Used:** 2,969
