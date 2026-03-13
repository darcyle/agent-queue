# Check if specs need updating after /browse command fix

**Task ID:** `agile-forge`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 100
**Type:** docs
**Branch:** `agile-forge/check-if-specs-need-updating-after-browse-command-fix`
**Archived:** 2026-03-13 16:38:10

## Description

**Context:**
The task `prime-summit` just completed with code changes to the `/browse` command.

**Changed Files:**
- `src/command_handler.py` - `_cmd_list_directory` method
- `src/discord/commands.py` - `_FileBrowserView` class

**Changes Summary:**
- Added `os.path.realpath()` to resolve workspace paths to absolute paths
- Replaced inline `_refresh` filesystem listing with a call to `handler.execute("list_directory", ...)`
- Updated `_refresh` to pass workspace name for correct workspace resolution
- Added workspace path in embed footer
- Improved embed layout

**Your Task:**
1. Review the code changes in the git history (commit e909102)
2. Locate the relevant spec file(s) in the `specs/` directory that describe the `/browse` command or file browsing functionality
3. Check if the specs accurately describe the current behavior:
   - Do they mention the workspace path resolution behavior?
   - Do they describe the unified refresh logic?
   - Do they mention the embed footer showing workspace path?
4. If specs are outdated or missing information, update them to match the code
5. If specs are already accurate, report that no changes were needed

**Workspace:** `/home/jkern/agent-queue-workspaces/agent-queue/`

**Git Reference:**
```
e909102 Fix /browse to resolve workspace paths to absolute and unify refresh logic
3c6f147 Fix /browse to use project workspaces instead of agent-queue directory
```

## Result

**Summary:** Done. Here's what I found and did:

**Finding:** The `/browse` command and `list_directory` command handler were completely undocumented in the specs — neither `specs/command-handler.md` nor `specs/discord/discord.md` had any mention of them.

**Changes made:**

1. **`specs/command-handler.md`** — Added `list_directory` command documentation:
   - Parameters: `project_id`, `workspace` (name/ID lookup), `path`
   - Workspace path resolution via `os.path.realpath()` to prevent CWD-relative issues
   - Return format including `workspace_path`, `workspace_name`, `directories`, `files`
   - All error cases

2. **`specs/discord/discord.md`** — Added new section "3.10 File Browsing Commands":
   - `/browse` command with full documentation of the `_FileBrowserView` interactive UI
   - Embed layout (title, description, contents field, workspace path footer)
   - Unified refresh logic through `handler.execute("list_directory", ...)`
   - Workspace autocomplete behavior
   - `/edit-file` stub reference
   - Renumbered subsequent sections (3.10→3.11, 3.11→3.12)

**Tokens Used:** 6,291
