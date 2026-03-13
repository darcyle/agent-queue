# Add the ability to specify what a project's default branch is, for example in some projects we may w

**Task ID:** `steady-nexus`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 100
**Branch:** `steady-nexus/add-the-ability-to-specify-what-a-projects-default-branch-is-for-example-in-some-projects-we-may-w`
**Archived:** 2026-03-13 16:38:09

## Description

Add the ability to specify what a project's default branch is, for example in some projects we may want branch off of and merge into a dev branch as the default. In addition to specifying it when you create a new project, there should also be a command in discord to set the default branch, and it should create one if it doesn't exist yet.

## Result

**Summary:** Done. Here's a summary of what was implemented:

### Changes Made

1. **`src/command_handler.py`** — Added `_cmd_set_default_branch()` method that:
   - Validates the project exists and branch name is provided
   - Fetches latest from the remote to check if the branch exists
   - If the branch doesn't exist on the remote, creates it from the current default branch (or HEAD as fallback) and pushes it
   - Updates the project's `repo_default_branch` in the database
   - Returns the old and new branch names, plus whether the branch was created
   - Also added `repo_default_branch` support to `_cmd_edit_project()` for inline editing

2. **`src/discord/commands.py`** — Added `/set-default-branch` Discord slash command with `project_id` and `branch` parameters. Uses deferred response since the git operations may take time. Shows success/error with rich embeds and indicates if the branch was newly created.

3. **`src/chat_agent.py`** — Added `set_default_branch` tool definition for the LLM chat agent, and updated the `edit_project` tool schema to include `repo_default_branch` as an editable field.

**Tokens Used:** 8,566
