# Add a command to re-open a completed task with an additional comment from the user. This should be f

**Task ID:** `quick-nexus`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 100
**Branch:** `quick-nexus/add-a-command-to-re-open-a-completed-task-with-an-additional-comment-from-the-user-this-should-be-f`
**Archived:** 2026-03-13 16:38:09

## Description

Add a command to re-open a completed task with an additional comment from the user. This should be for restarting tasks where there are issues found with the task that need to be improved and we still want the context from the original ticket.

## Result

**Summary:** Done. Here's a summary of what was implemented:

## Changes Made

The `reopen_with_feedback` command already existed for reopening tasks with user comments. I improved it to better serve the described use case:

### `src/command_handler.py`
- **Structured feedback storage**: Feedback is now stored as a `task_context` entry (type: `reopen_feedback`) in addition to being appended to the task description. This allows agents and tooling to access individual reopen comments programmatically.
- **PR URL cleared**: When reopening a task, `pr_url` is set to `None` so the agent creates a fresh PR on re-execution.
- **Response includes `feedback_added: True`** for confirmation.
- Updated separator text from "QA Feedback" to "Reopen Feedback" (more general).

### `src/chat_agent.py`
- Updated the tool description to reflect the new capabilities (structured context storage, PR URL clearing).

### `src/discord/commands.py`
- Updated Discord command description to be more general.

### `tests/test_reopen_with_feedback.py` (new)
- 11 comprehensive tests covering:
  - Reopening completed and failed tasks
  - Feedback appended to description
  - Feedback stored as task_context entry
  - PR URL cleared on reopen
  - Error cases (missing task_id, missing feedback, task not found, in-progress task)
  - Multiple reopens accumulate feedback
  - Audit event logging

**Tokens Used:** 9,372
