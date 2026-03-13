# I'm getting a lot of "This interaction failed" when selecting files in the browse menu. A couple wor

**Task ID:** `smart-crest`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 100
**Branch:** `smart-crest/im-getting-a-lot-of-this-interaction-failed-when-selecting-files-in-the-browse-menu-a-couple-wor`
**Archived:** 2026-03-13 16:38:09

## Description

I'm getting a lot of "This interaction failed" when selecting files in the browse menu. A couple worked but I'm not sure why its failing. Please try to look at the logs, understand whats going wrong and fix the issue.

---
**Reopen Feedback:**
Now I don't get the error message anymore, but opening larger files still doesn't work. It seems only very small files load in time to display. Please reconsider our approach and find something that will work instead.

---
**Reopen Feedback:**
The system is still not working. Files over 1kb just do not load at all. Can we have a more immediate response and then present the dialog immediately rather than trying to post the whole file to chat? Maybe that would work better?

## Result

**Summary:** Done. Here's what was changed:

**Problem:** Selecting files in the browse menu caused "interaction failed" errors because `_view_file` was reading the entire file content before responding to Discord's interaction. Even with `defer()` and `asyncio.to_thread()`, files over ~1KB were too slow, causing timeouts.

**Fix:** Completely restructured the file selection flow:

1. **`_view_file` now responds instantly** — When a file is selected, it immediately sends an ephemeral embed with file metadata (name, path, size) and action buttons. No file I/O happens at this point, so the response is effectively instant.

2. **New `_FileInfoView`** replaces the old `_FileViewActions` with two buttons:
   - **"👁️ View Content"** — Defers, reads the file in a thread, then sends it as both a Discord file attachment (for full content) and an inline preview (first 1800 chars). Using attachments avoids Discord's message size limits.
   - **"✏️ Edit File"** — Reads up to 4000 chars directly (skipping handler/DB overhead), then opens the edit modal.

3. **Edit button also skips handler overhead** — reads the file directly with `asyncio.to_thread()` instead of going through `handler.execute()`.

**Tokens Used:** 5,414
