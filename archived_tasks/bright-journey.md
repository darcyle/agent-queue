# Add a command in discord that allows you to browse the repo files and directories, and add a command

**Task ID:** `bright-journey`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 100
**Branch:** `bright-journey/add-a-command-in-discord-that-allows-you-to-browse-the-repo-files-and-directories-and-add-a-command`
**Archived:** 2026-03-13 16:38:09

## Description

Add a command in discord that allows you to browse the repo files and directories, and add a command that opens a dialog with a text window that lets you edit any text file and either save or discard your changes.

## Result

**Summary:** Done. Here's a summary of what was implemented:

## Changes Made

### `src/command_handler.py` — Two new backend commands:
- **`_cmd_list_directory`** — Lists files and directories at a given path within a project workspace, returning directory names and file names with sizes. Uses `_validate_path` for sandbox security.
- **`_cmd_write_file`** — Writes content to a file within the workspace, also sandbox-validated. Used by the edit modal's save flow.

### `src/discord/commands.py` — Two new slash commands + interactive UI:

**`/browse [path]`** — Interactive file/directory browser:
- Shows an embed with the current path, project ID, and content counts
- **Directory navigation** via a select dropdown menu (📁 icons)
- **File viewing** via a select dropdown menu (📄 icons with file sizes)
- **Parent directory** button (⬆) to go up
- **Pagination** for both directories and files (20 items per page with prev/next buttons)
- Selecting a file shows its content in an ephemeral message with an **Edit** button

**`/edit-file <path>`** — Direct file editor:
- Opens a Discord modal with the file's current content pre-filled in a text area
- User edits the content and submits to save, or dismisses to discard
- Shows success/error feedback after save

Both commands auto-resolve the project from the channel context (no need to specify project_id manually).

**Tokens Used:** 8,652
