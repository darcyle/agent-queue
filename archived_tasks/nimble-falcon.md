# Review specs for task sharp-crest changes

**Task ID:** `nimble-falcon`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 100
**Type:** docs
**Branch:** `nimble-falcon/review-specs-for-task-sharp-crest-changes`
**Archived:** 2026-03-13 16:38:10

## Description

Task sharp-crest (commit on branch `solid-current/if-something-restarts-agent-queue-make-the-command-to-restart-agent-queue-require-a-reason-and-log-has-async`) completed with the following code changes:

**Files changed:**
- `src/command_handler.py` - Refactored to use async GitManager methods instead of subprocess, improved error handling
- `src/discord/bot.py` - Made `_save_notes_threads` async with helper `_save_notes_threads_sync`
- `src/discord/commands.py` - Replaced subprocess calls with async `_async_git_output` helper
- `tests/test_smart_workspace_assignment.py` - Added real GitManager fixture

**Key changes:**
1. Replaced `asyncio.to_thread(subprocess.run, ...)` with `git._arun()` and `git._arun_subprocess()` methods
2. Better error handling using `GitError` exceptions instead of checking return codes
3. Consolidated uncommitted changes + merge conflict checks into single `git status --porcelain` call
4. Made notes thread saving async to avoid blocking
5. New `_async_git_output()` helper function for non-blocking git operations

**Workspace path:** /home/jkern/agent-queue-workspaces/agent-queue/

**Your task:**
1. Review the specs directory (`specs/`) to identify any documentation that describes the behavior of these changed components
2. Check specs related to:
   - Git operations and workspace management
   - The `/restart-daemon` command
   - The bot's notes thread handling
   - Command handler architecture
3. Update any specs that are now outdated or incomplete due to these async refactorings
4. If specs are missing entirely for these features, create new spec files
5. The goal is to ensure specs accurately describe the current async-based implementation

## Result

**Summary:** The specs have been updated. Here's a summary of all changes made:

### `specs/git/git.md`
- **Section 1 (Overview)**: Updated from "thin synchronous wrapper" to describe the dual sync/async API architecture
- **Section 2 (Error Handling)**: Added `_arun` and `_arun_subprocess` method documentation alongside existing `_run`
- **Section 6 (`commit_all`)**: Updated to describe both sync and async implementations
- **Section 7 (GitHub PR Operations)**: Updated to mention `_arun_subprocess` for async counterparts
- **New Section 10 (Async API Reference)**: Complete mapping table of all 35 sync→async method pairs with usage examples
- **Renumbered Sections 10→11, 11→12** (Design Principles, Known Gaps)

### `specs/command-handler.md`
- **`restart_daemon`**: Added `reason` parameter documentation and notification logging behavior
- **New `update_and_restart`**: Documented the git pull + pip install + restart command
- **New "Workspace Maintenance Commands" section**: Added `find_merge_conflict_workspaces` and `sync_workspaces` with full parameter/behavior/return documentation
- **`run_command`**: Clarified `asyncio.to_thread(subprocess.run, ...)` usage
- **`search_files`**: Clarified `asyncio.to_thread(subprocess.run, ...)` usage

### `specs/discord/discord.md`
- **`/restart`**: Updated from no-params to required `reason` parameter, documented git context gathering (commit hash, commits behind origin)
- **New `/update`**: Documented the pull+install+restart slash command with before/after commit comparison

**Tokens Used:** 14,010
