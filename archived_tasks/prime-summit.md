# The /browse command is currently always loading the agent-queue directory, instead of one of the wor

**Task ID:** `prime-summit`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 100
**Branch:** `prime-summit/the-browse-command-is-currently-always-loading-the-agent-queue-directory-instead-of-one-of-the-wor`
**Archived:** 2026-03-13 16:38:10

## Description

The /browse command is currently always loading the agent-queue directory, instead of one of the workspaces in the project channel it was called in. Please fix this. The command should default to the first workspace but be able to target any workspace

---
**Reopen Feedback:**
This fixed the embed for the command, but the folders in the dropdown and the files dropdown both still have the contents of the agent-queue workspace instead of the project the command was run in

## Result

**Summary:** The changes have been committed. Here's a summary of what was fixed:

### Changes Made

**`src/command_handler.py`** — `_cmd_list_directory`:
- Added `os.path.realpath()` to resolve workspace paths to absolute before using them. This prevents CWD-relative resolution issues — if a workspace path was stored as a relative path, `os.path.realpath()` would previously resolve it relative to the bot's working directory (the agent-queue source dir), showing agent-queue files instead of the project's files.
- Added debug logging to trace project ID, workspace name, and resolved path for diagnosing future issues.

**`src/discord/commands.py`** — `_FileBrowserView`:
- Added `os.path.realpath()` in the view constructor for the same absolute-path guarantee.
- **Replaced the inline `_refresh` filesystem listing** with a call to `handler.execute("list_directory", ...)`. Previously, navigating directories in the dropdown used a completely separate code path (direct `os.listdir`) that bypassed workspace resolution. Now it goes through the same `_cmd_list_directory` handler, ensuring consistent workspace resolution with the workspace name passed through.
- Updated `_refresh` to pass the `workspace` name so the correct workspace is always re-resolved from the database.
- Added workspace path in the embed footer for transparency — the user can now see exactly which directory is being browsed.
- Improved embed layout (workspace name on its own line).

**Tokens Used:** 43,274
