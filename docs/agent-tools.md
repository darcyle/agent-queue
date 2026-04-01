# Agent Tools Reference

Complete reference for all tools available to the Agent Queue supervisor and agents. Tools are organized into **core tools** (always available) and **categorized tools** (loaded on demand via `browse_tools` / `load_tools`).

## How Tool Loading Works

To keep the LLM's context window small, only ~11 core tools are loaded at conversation start. When the supervisor needs specialized tools it:

1. Calls **`browse_tools`** to see available categories
2. Calls **`load_tools`** with a category name to inject those tools into the active set

All tools route through `CommandHandler.execute()` regardless of loading state — loading is purely a context-window optimization.

---

## Core Tools (Always Loaded)

These tools are always available without loading a category.

### `browse_tools`
List available tool categories with descriptions and tool counts. Use this to discover what specialized tools exist.

**Parameters:** None

---

### `load_tools`
Load all tools from a specific category into the active tool set.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `category` | string | ✅ | Category name (e.g. `git`, `project`, `hooks`) |

---

### `create_task`
Create a new task. Inherits the active project if `project_id` is omitted.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | | Project ID (inferred from active project) |
| `title` | string | ✅ | Short task title |
| `description` | string | | Detailed description for the agent |
| `priority` | integer | | Lower = higher priority (default 100) |
| `requires_approval` | boolean | | If true, creates PR instead of auto-merging |
| `task_type` | string | | One of: feature, bugfix, refactor, test, docs, chore, research, plan |
| `profile_id` | string | | Agent profile ID for specific tools/capabilities |
| `preferred_workspace_id` | string | | Force task to run in a specific workspace |
| `attachments` | array[string] | | Absolute file paths to images/files for the agent |
| `auto_approve_plan` | boolean | | Auto-approve any plan this task generates |

---

### `list_tasks`
List tasks with filtering and display options. By default only active tasks are shown.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | | Filter by project (required for tree/compact modes) |
| `status` | string | | Filter by exact status (DEFINED, READY, IN_PROGRESS, etc.) |
| `show_all` | boolean | | Include all statuses |
| `include_completed` | boolean | | Alias for show_all |
| `completed_only` | boolean | | Only show completed/failed/blocked tasks |
| `display_mode` | string | | `flat` (default), `tree` (hierarchical), or `compact` (progress bars) |
| `show_dependencies` | boolean | | Annotate tasks with dependency info |

---

### `edit_task`
Modify any task property including title, description, priority, status, profile, and more.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | ✅ | Task ID |
| `project_id` | string | | Move task to different project |
| `title` | string | | New title |
| `description` | string | | New description |
| `priority` | integer | | New priority |
| `task_type` | string/null | | New type or null to clear |
| `status` | string | | Admin override — bypasses state machine |
| `max_retries` | integer | | Max retry attempts |
| `profile_id` | string/null | | Agent profile or null to clear |
| `auto_approve_plan` | boolean | | Auto-approve plans |

---

### `get_task`
Get full details of a specific task including its description.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | ✅ | Task ID |

---

### `memory_search`
Search project memory for semantically similar past task results, notes, and knowledge-base entries.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✅ | Project ID |
| `query` | string | ✅ | Semantic search query |
| `top_k` | integer | | Number of results (default 10) |

---

### `reply_to_user`
Deliver the final response to the user. **Must** be called when done processing a request.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `message` | string | ✅ | Complete response to the user |

---

### `send_message`
Post a message to a Discord channel.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `channel_id` | string | ✅ | Discord channel ID |
| `content` | string | ✅ | Message content |

---

### `list_rules`
List all automation rules for the current project and globals.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | | Project ID (defaults to active project) |

---

### `save_rule`
Create or update an automation rule. This is the **only** way to create automation — never create hooks directly. Include `# Title`, `## Trigger`, and `## Logic` sections in the content.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `id` | string | | Rule ID (auto-generated if omitted) |
| `project_id` | string | | Project ID (null = global rule) |
| `type` | string | ✅ | `active` (triggered automation) or `passive` (reasoning guidance) |
| `content` | string | ✅ | Markdown with # Title, ## Trigger, ## Logic |

---

### `load_rule`
Load a specific rule's full content and metadata, including generated hook IDs.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `id` | string | ✅ | Rule ID |

---

### `delete_rule`
Remove an automation rule and all its generated hooks.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `id` | string | ✅ | Rule ID |

---

### `refresh_hooks`
Force reconciliation of all rules and hooks. Re-reads rule files, regenerates hooks, cleans orphans. Normally not needed — auto-reconciles on file change.

**Parameters:** None

---

## Git Category (11 tools)

*Branch, commit, push, PR, and merge operations for project repositories.*

All git tools accept optional `project_id` (defaults to active project) and `workspace` (defaults to first workspace).

### `get_git_status`
Get git status showing current branch, working tree status, and recent commits across all workspaces.

### `git_commit`
Stage all changes and create a commit.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `message` | string | ✅ | Commit message |

### `git_pull`
Pull (fetch + merge) from remote origin.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `branch` | string | | Branch to pull (defaults to current) |

### `git_push`
Push a branch to remote origin.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `branch` | string | | Branch to push (defaults to current) |

### `git_create_branch`
Create and switch to a new branch.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `branch_name` | string | ✅ | Name for the new branch |

### `git_merge`
Merge a branch into the default branch. Auto-aborts on conflicts.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `branch_name` | string | ✅ | Branch to merge |
| `default_branch` | string | | Target branch (defaults to repo default) |

### `git_create_pr`
Create a GitHub pull request using `gh` CLI.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `title` | string | ✅ | PR title |
| `body` | string | | PR description |
| `branch` | string | | Head branch (defaults to current) |
| `base` | string | | Base branch (defaults to repo default) |

### `git_changed_files`
List files changed compared to a base branch. Lighter than a full diff.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `base_branch` | string | | Branch to compare against (defaults to repo default) |

### `git_log`
Show recent git commits.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `count` | integer | | Number of commits (default 10) |

### `git_diff`
Show git diff. Without `base_branch` shows working tree changes; with it shows diff against that branch.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `base_branch` | string | | Branch to diff against (omit for working tree) |

### `checkout_branch`
Switch to an existing branch.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `branch_name` | string | ✅ | Branch to check out |

---

## Project Category (16 tools)

*Project CRUD, workspace management, channel configuration.*

### `list_projects`
List all projects in the system.

**Parameters:** None

### `create_project`
Create a new project with optional auto-created Discord channel.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | ✅ | Project name |
| `credit_weight` | number | | Scheduling weight (default 1.0) |
| `max_concurrent_agents` | integer | | Max simultaneous agents (default 2) |
| `repo_url` | string | | Git repository URL |
| `default_branch` | string | | Default branch (default: main) |
| `auto_create_channels` | boolean | | Auto-create Discord channels |

### `pause_project` / `resume_project`
Pause or resume task scheduling for a project.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✅ | Project ID |

### `edit_project`
Edit project properties: name, weight, concurrent agents, budget, channel, profile, branch.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✅ | Project ID |
| `name` | string | | New name |
| `credit_weight` | number | | New scheduling weight |
| `max_concurrent_agents` | integer | | New max concurrent agents |
| `budget_limit` | integer/null | | Token budget (null to clear) |
| `discord_channel_id` | string/null | | Discord channel (null to unlink) |
| `default_profile_id` | string/null | | Default agent profile (null to clear) |
| `repo_default_branch` | string | | Default git branch |

### `set_default_branch`
Set the default git branch for a project. Creates the branch on remote if it doesn't exist.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✅ | Project ID |
| `branch` | string | ✅ | Branch name |

### `get_project_channels`
Get the Discord channel ID configured for a project.

### `get_project_for_channel`
Reverse lookup: find which project is linked to a Discord channel.

### `delete_project`
Delete a project and all associated data. Cannot delete if any task is IN_PROGRESS.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✅ | Project ID |
| `archive_channels` | boolean | | Archive Discord channels instead of leaving as-is |

### `set_active_project`
Set or clear the active project. When set, commands default to this project.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | | Project ID (empty/null to clear) |

### `add_workspace`
Add a workspace directory. Use `clone` to auto-clone from repo URL, or `link` to use an existing directory.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✅ | Project ID |
| `source` | string | ✅ | `clone` or `link` |
| `path` | string | | Directory path (required for link) |
| `name` | string | | Workspace name |

### `list_workspaces`
List workspaces with lock status showing which agent/task holds each.

### `find_merge_conflict_workspaces`
Scan workspaces for branches with merge conflicts against the default branch. Use before creating conflict-resolution tasks.

### `release_workspace`
Force-release a stuck workspace lock.

### `remove_workspace`
Delete a workspace record from the database (does not delete files on disk).

### `queue_sync_workspaces`
Queue a high-priority sync task that pauses the project, waits for active tasks, merges all feature branches, then resumes.

---

## Agent Category (12 tools)

*Agent management, agent profiles, profile import/export.*

### `list_agents`
List agent slots for a project. Locked workspaces are busy, unlocked are idle.

### `get_agent_error`
Get the last error recorded for a task including classification and suggested fix.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | ✅ | Task ID |

### `list_profiles`
List all agent profiles (capability bundles with tools, MCP servers, system prompts).

### `create_profile`
Create a new agent profile.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `id` | string | ✅ | Profile slug ID (e.g. `reviewer`) |
| `name` | string | ✅ | Display name |
| `description` | string | | What the profile is for |
| `model` | string | | Model override |
| `permission_mode` | string | | Permission mode override |
| `allowed_tools` | array[string] | | Tool whitelist (e.g. `['Read', 'Glob', 'Bash']`) |
| `mcp_servers` | object | | MCP server configs (name → {command, args}) |
| `system_prompt_suffix` | string | | Text appended to agent's system prompt |

### `get_profile`
Get details of a specific agent profile.

### `edit_profile`
Edit an existing profile's properties (same fields as create_profile).

### `delete_profile`
Delete a profile. References in tasks/projects are cleared.

### `list_available_tools`
Discover available Claude Code tools and well-known MCP servers for use in profiles.

### `check_profile`
Validate a profile's install dependencies (commands, npm, pip packages).

### `install_profile`
Install missing npm/pip dependencies for a profile.

### `export_profile`
Export a profile as YAML. Optionally create a public GitHub gist.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `profile_id` | string | ✅ | Profile ID |
| `create_gist` | boolean | | Create a public gist |

### `import_profile`
Import a profile from YAML text or GitHub gist URL.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `source` | string | ✅ | YAML text or gist URL |
| `id` | string | | Override profile ID |
| `name` | string | | Override profile name |
| `overwrite` | boolean | | Overwrite existing profile |

---

## Hooks Category (8 tools)

*Hook management — list, execute, schedule hooks. Hooks are generated from rules; use `save_rule` to create automation.*

### `list_hooks`
List hooks (generated from rules), optionally filtered by project.

### `list_hook_runs`
Show recent execution history for a hook.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `hook_id` | string | ✅ | Hook ID |
| `limit` | integer | | Number of runs (default 10) |

### `fire_hook`
Manually trigger a hook immediately, ignoring cooldown.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `hook_id` | string | ✅ | Hook ID |

### `hook_schedules`
Show upcoming hook executions with next-run times and schedule constraints.

### `fire_all_scheduled_hooks`
Manually trigger all enabled periodic hooks. Useful for testing.

### `schedule_hook`
Schedule a one-shot hook to fire at a specific time or after a delay. Runs once, then auto-deletes.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✅ | Project ID |
| `prompt_template` | string | ✅ | Prompt to execute when fired |
| `name` | string | | Descriptive name |
| `fire_at` | number/string | | Epoch timestamp or ISO-8601 datetime |
| `delay` | string | | Delay string (e.g. `30m`, `2h`, `1d`) |

### `list_scheduled`
List all pending one-shot scheduled hooks with countdown times.

### `cancel_scheduled`
Cancel a scheduled hook before it fires.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `hook_id` | string | ✅ | Hook ID |

---

## Memory Category (13 tools)

*Memory operations — notes, project profiles, compaction, reindexing.*

### `memory_search`
*(Also a core tool)* Search project memory semantically.

### `memory_stats`
Get memory index statistics: enabled status, collection name, embedding provider, auto-recall settings.

### `memory_reindex`
Force full reindex of project memory from markdown files.

### `view_profile`
View the project profile — synthesized understanding of architecture, conventions, decisions. Evolves as tasks complete.

### `edit_profile` (memory context)
Replace the project profile with new content for manual correction.

### `regenerate_profile`
Force LLM regeneration of the project profile from full task history.

### `compact_memory`
Trigger memory compaction: groups by age, summarizes medium-age into weekly digests, deletes old after digesting.

### `list_notes`
List all notes for a project (name, title, size).

### `write_note`
Create or overwrite a project note (markdown).

### `delete_note`
Delete a project note by title.

### `read_note`
Read a note's full contents.

### `append_note`
Append content to an existing note (or create new). No need to read first.

### `promote_note`
Incorporate a note into the project profile using LLM integration.

### `compare_specs_notes`
List spec files and note files side-by-side for gap analysis.

---

## Files Category (7 tools)

*Filesystem tools — read, write, edit files, glob, grep.*

### `read_file`
Read file contents from a workspace. Supports offset/limit for large files.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | ✅ | File path (absolute or relative to workspaces root) |
| `max_lines` | integer | | Max lines to return (default 2000) |
| `offset` | integer | | Start line (1-based, default 1) |
| `limit` | integer | | Number of lines (overrides max_lines) |

### `write_file`
Write content to a file. Creates parent directories if needed.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | ✅ | File path |
| `content` | string | ✅ | Content to write |

### `edit_file`
Targeted string replacement. `old_string` must be unique unless `replace_all=true`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | ✅ | File path |
| `old_string` | string | ✅ | Text to find |
| `new_string` | string | ✅ | Replacement text |
| `replace_all` | boolean | | Replace all occurrences (default false) |

### `glob_files`
Find files matching a glob pattern (e.g. `**/*.py`). Sorted by modification time.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `pattern` | string | ✅ | Glob pattern |
| `path` | string | ✅ | Directory to search |

### `grep`
Search file contents using regex (ripgrep-style).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `pattern` | string | ✅ | Regex pattern |
| `path` | string | ✅ | File or directory |
| `context` | integer | | Lines of context around matches |
| `case_insensitive` | boolean | | Case-insensitive search |
| `glob` | string | | File filter (e.g. `*.py`) |
| `output_mode` | string | | `content`, `files_with_matches`, or `count` |
| `max_results` | integer | | Max result lines (default 100) |

### `search_files`
Search files or content. `grep` mode for content, `find` mode for filenames.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `pattern` | string | ✅ | Search pattern |
| `path` | string | ✅ | Directory to search |
| `mode` | string | | `grep` (default) or `find` |

### `list_directory`
List files and directories at a path within a workspace.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✅ | Project ID |
| `path` | string | | Relative path (default: root) |
| `workspace` | string | | Workspace name or ID |

---

## System Category (28+ tools)

*Token usage, config, diagnostics, advanced task operations, prompt management, daemon control.*

### Task Operations

#### `list_active_tasks_all_projects`
Cross-project overview of all active tasks grouped by project.

#### `get_task_tree`
Get subtask hierarchy for a parent task as a tree with box-drawing characters.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | ✅ | Root task ID |
| `compact` | boolean | | Show only root with progress bar |
| `max_depth` | integer | | Max nesting depth (default 4) |

#### `stop_task`
Stop an in-progress task. Cancels agent, marks BLOCKED.

#### `restart_task`
Reset completed/failed/blocked task back to READY.

#### `reopen_with_feedback`
Reopen a task with feedback explaining what needs fixing. Feedback is appended to description.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | ✅ | Task ID |
| `feedback` | string | ✅ | What went wrong / what to fix |

#### `delete_task`
Delete a task (cannot delete if IN_PROGRESS).

#### `skip_task`
Skip a BLOCKED/FAILED task to unblock downstream dependents.

### Archive Operations

#### `archive_tasks`
Archive completed tasks to clear from active lists. Optionally include failed/blocked.

#### `archive_task`
Archive a single task (must be in terminal status).

#### `list_archived`
List previously archived tasks.

#### `restore_task`
Restore an archived task back to active (restored as DEFINED).

### Approval & Plan Operations

#### `approve_task`
Manually approve/complete a task AWAITING_APPROVAL.

#### `approve_plan`
Approve a plan — creates subtasks from the stored plan.

#### `reject_plan`
Reject a plan with feedback — reopens for revision.

#### `delete_plan`
Delete a plan without creating subtasks — completes the task.

#### `process_plan`
Manually scan workspaces for plan.md files.

#### `process_task_completion`
Internal: Process task completion to discover plan files.

### Dependency Operations

#### `get_task_dependencies`
Get full dependency graph: what a task depends on (upstream) and blocks (downstream).

#### `add_dependency`
Add a dependency between tasks with cycle detection.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | ✅ | Downstream task (waits) |
| `depends_on` | string | ✅ | Upstream task (must complete first) |

#### `remove_dependency`
Remove a dependency between tasks.

#### `get_chain_health`
Check dependency chain health — find stuck downstream tasks.

### Diagnostics & Control

#### `get_status`
High-level system overview: projects, agents, task counts.

#### `get_recent_events`
Recent system events (completions, failures, etc.).

#### `get_task_result`
Task output: summary, files changed, error, tokens used.

#### `get_task_diff`
Git diff for a task's branch against base.

#### `get_token_usage`
Token usage breakdown by project or task.

#### `get_agent_error`
Last error for a task with classification and suggested fix.

#### `run_command`
Execute a shell command in a workspace directory.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `command` | string | ✅ | Shell command |
| `working_dir` | string | ✅ | Working directory (path or project ID) |
| `timeout` | integer | | Timeout in seconds (default 30, max 120) |

#### `restart_daemon`
Restart the agent-queue daemon (brief disconnect/reconnect).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `reason` | string | ✅ | Why the restart is needed |

#### `orchestrator_control`
Pause, resume, or check status of the task scheduler.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `action` | string | ✅ | `pause`, `resume`, or `status` |

### Prompt Management

#### `list_prompts`
List all prompt templates for a project, filtered by category or tag.

#### `read_prompt`
Read a prompt template's full content and metadata.

#### `render_prompt`
Render a prompt template with `{{variable}}` substitution.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project_id` | string | ✅ | Project ID |
| `name` | string | ✅ | Template name |
| `variables` | object | | Key-value pairs for substitution |

---

## Tool Count Summary

| Category | Tools | Description |
|----------|-------|-------------|
| **Core** | ~15 | Always loaded — tasks, memory search, rules, messaging |
| **git** | 11 | Branch, commit, push, PR, merge |
| **project** | 16 | Project CRUD, workspaces, channels |
| **agent** | 12 | Profiles, dependencies, import/export |
| **hooks** | 8 | Hook execution, scheduling, history |
| **memory** | 13 | Notes, profiles, compaction, reindexing |
| **files** | 7 | Read, write, edit, glob, grep |
| **system** | 28+ | Diagnostics, archives, dependencies, prompts, daemon |
| **Total** | ~110+ | |
