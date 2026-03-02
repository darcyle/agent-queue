# Discord Commands

Agent Queue gives you two ways to interact through Discord:

| Method | How it works | Best for |
|--------|-------------|----------|
| **Slash commands** | Type `/` and pick from the autocomplete menu | Structured operations with clear parameters |
| **Natural language chat** | Just type a message in the bot's channel | Quick requests, complex descriptions, multi-step workflows |

Both methods call the same underlying logic — slash commands are just a structured shortcut for what you can always ask the chat agent to do in plain English.

**Example — creating a task both ways:**

```
Slash:  /add-task description: Fix the login timeout bug in auth.py

Chat:   hey, can you add a task to fix the login timeout bug in auth.py?
        the error is on line 47, JWT expires too early
```

The chat approach lets you include richer context naturally.

---

## Getting Oriented

These commands give you a quick picture of what's happening across your system.

| Command | Description |
|---------|-------------|
| `/status` | System overview — active/ready/completed task counts, agent states, queued work |
| `/projects` | List all projects with their status, credit weight, and linked channels |
| `/agents` | List all agents and what they're currently working on |
| `/budget` | Token usage breakdown by project |
| `/events` | Recent system events (task completions, failures, etc.) |

### `/status`

Shows a dashboard-style overview: task counts by state, each agent's current assignment, and the next tasks in queue. This is usually the first command you run.

### `/events`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `limit` | No | Number of events to show (default: 10) |

---

## Managing Projects

### Creating and editing

| Command | Description |
|---------|-------------|
| `/create-project` | Create a new project |
| `/edit-project` | Change a project's name, weight, or max agents |
| `/delete-project` | Delete a project and all its data |

#### `/create-project`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `name` | Yes | Project name |
| `credit_weight` | No | Scheduling weight (default: 1.0) — higher weight gets more agent time |
| `max_concurrent_agents` | No | Max agents working simultaneously (default: 2) |
| `auto_create_channels` | No | Auto-create a dedicated Discord channel for this project |

#### `/edit-project`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `project_id` | Yes | Project ID |
| `name` | No | New name |
| `credit_weight` | No | New scheduling weight |
| `max_concurrent_agents` | No | New max agents |

#### `/delete-project`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `project_id` | Yes | Project ID to delete |
| `archive_channels` | No | Archive the project's Discord channels instead of leaving them |

Deletion removes all associated tasks, repos, results, and token records. Cannot delete a project with tasks currently in progress.

### Pausing and resuming

| Command | Description |
|---------|-------------|
| `/pause` | Pause a project — no new tasks will be scheduled |
| `/resume` | Resume a paused project |

Both commands accept an optional `project_id` parameter. If omitted, the project is auto-detected from the channel you're in.

### Setting the active project

| Command | Description |
|---------|-------------|
| `/set-project` | Set or clear the active project for the chat agent |

When an active project is set, commands that need a project will default to it. Leave `project_id` empty to clear.

### Channel setup

Each project can have a dedicated Discord channel. Commands run in that channel automatically target the right project — no need to specify `project_id` every time.

| Command | Description |
|---------|-------------|
| `/set-channel` | Link an existing Discord channel to a project |
| `/set-control-interface` | Link a channel by name (alias for `/set-channel`) |
| `/create-channel` | Create a new Discord channel and link it to a project |
| `/channel-map` | Show all project-to-channel mappings |

#### `/set-channel`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `project_id` | Yes | Project ID |
| `channel` | Yes | The Discord channel to link (use channel picker) |

#### `/create-channel`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `project_id` | Yes | Project ID |
| `channel_name` | No | Name for the new channel (defaults to project ID) |
| `category` | No | Category to create the channel in |

---

## Working with Tasks

### Creating tasks

#### `/add-task`

The quickest way to create a task from Discord.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `description` | Yes | What the task should do |

The project is auto-detected from the channel, or falls back to the active project. Returns an error if no project can be resolved.

**Via chat** — you can provide much richer context:

```
Create a task for my-app to refactor the database connection pooling.
Currently it creates a new connection per request which is causing
timeouts under load. Use SQLAlchemy's built-in pool with max 20
connections. The relevant code is in src/db/connection.py.
```

### Monitoring tasks

| Command | Description |
|---------|-------------|
| `/tasks` | List tasks grouped by status, with interactive expand/collapse |
| `/task` | Show full details of a specific task |
| `/task-result` | View a task's output: summary, files changed, tokens used |
| `/task-diff` | Show the git diff for a task's branch |
| `/agent-error` | Inspect the last error for a failed task |
| `/chain-health` | Check dependency chains for stuck tasks |

#### `/tasks`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `project_id` | No | Filter by project (auto-detected from channel) |

Returns an interactive view with collapsible status sections and a dropdown to inspect individual tasks.

#### `/task`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID (e.g., `bold-falcon`) |

Shows title, status, project, priority, assigned agent, retry count, and description.

#### `/task-result`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID |

Shows the agent's summary, list of files changed, token usage, and any error messages.

#### `/task-diff`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID |

Shows the git diff of the task's branch against the base branch. Large diffs are attached as `.patch` files.

#### `/agent-error`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID |

Shows error classification, error detail, suggested fix, and the agent's summary of what went wrong.

#### `/chain-health`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | No | Check a specific blocked task's downstream |
| `project_id` | No | Check all blocked chains in a project (auto-detected from channel) |

Use this when tasks seem stuck in DEFINED — it reveals which blocked or failed task is holding up the chain.

### Controlling tasks

| Command | Description |
|---------|-------------|
| `/edit-task` | Change a task's title, description, or priority |
| `/stop-task` | Cancel an in-progress task (marks it BLOCKED) |
| `/restart-task` | Reset a completed/failed/blocked task back to READY |
| `/delete-task` | Delete a task (can't delete in-progress tasks) |
| `/approve-task` | Approve a task that's AWAITING_APPROVAL |
| `/skip-task` | Skip a blocked/failed task to unblock its dependents |
| `/set-status` | Manually override a task's status |

#### `/edit-task`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID |
| `title` | No | New title |
| `description` | No | New description |
| `priority` | No | New priority (lower number = higher priority) |

#### `/stop-task`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID |

Cancels the agent working on the task and marks it as BLOCKED.

#### `/restart-task`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID |

Resets the task to READY so it gets picked up by an agent again. Works on completed, failed, or blocked tasks.

#### `/approve-task`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID |

For tasks in AWAITING_APPROVAL status — marks them as COMPLETED so downstream dependents can proceed.

#### `/skip-task`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID |

Marks a BLOCKED or FAILED task as COMPLETED without actually doing the work. Use this to unblock dependency chains when a task is no longer needed. Shows how many downstream tasks were unblocked.

#### `/set-status`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID |
| `status` | Yes | New status: DEFINED, READY, IN_PROGRESS, COMPLETED, FAILED, or BLOCKED |

Bypasses the normal state machine — use to unstick tasks or force a status change when normal commands don't apply.

---

## Repository & Agent Setup

### Repositories

| Command | Description |
|---------|-------------|
| `/repos` | List registered repositories |
| `/add-repo` | Register a repository for a project |

#### `/repos`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `project_id` | No | Filter by project (auto-detected from channel) |

#### `/add-repo`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `source` | Yes | How to set up the repo: `clone` (git URL), `link` (existing directory), or `init` (new empty repo) |
| `project_id` | No | Project ID (auto-detected from channel) |
| `url` | No | Git URL (required for `clone`) |
| `path` | No | Existing directory path (required for `link`) |
| `name` | No | Repo name (derived from URL or path if omitted) |
| `default_branch` | No | Default branch name (default: `main`) |

### Agents

| Command | Description |
|---------|-------------|
| `/agents` | List all agents and their states |
| `/create-agent` | Register a new agent |

#### `/create-agent`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `name` | Yes | Agent display name |
| `agent_type` | No | Agent type: `claude`, `codex`, `cursor`, or `aider` (default: `claude`) |
| `repo_id` | No | Repository ID to assign as the agent's workspace |

---

## Git Operations

All git commands auto-detect the project from the channel and default to the project's first repository. You can override with `project_id` and `repo_id` parameters.

### Browsing

| Command | Description |
|---------|-------------|
| `/git-status` | Current branch, working tree status, recent commits |
| `/git-branches` | List branches, or create a new one by providing `name` |
| `/git-log` | Show recent commits |
| `/git-diff` | Show working tree diff, or diff against a base branch |
| `/git-files` | List files changed compared to a base branch |

#### `/git-log`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `project_id` | No | Project ID (auto-detected) |
| `count` | No | Number of commits to show (default: 10) |

#### `/git-diff`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `project_id` | No | Project ID (auto-detected) |
| `base_branch` | No | Branch to diff against (omit for working tree diff) |

### Branching

| Command | Description | Aliases |
|---------|-------------|---------|
| `/create-branch` | Create and switch to a new branch | `/git-branch`, `/project-create-branch` |
| `/git-checkout` | Switch to an existing branch | `/checkout-branch` |

All accept `branch_name` (required), optional `project_id`, and optional `repo_id`.

### Committing and pushing

| Command | Description | Aliases |
|---------|-------------|---------|
| `/commit` | Stage all changes and commit | `/project-commit`, `/git-commit` |
| `/push` | Push a branch to the remote | `/project-push`, `/git-push` |
| `/merge` | Merge a branch into the default branch | `/project-merge`, `/git-merge` |
| `/git-pr` | Create a GitHub pull request | — |

#### `/commit`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `message` | Yes | Commit message |
| `project_id` | No | Project ID (auto-detected) |
| `repo_id` | No | Specific repo (uses first repo if omitted) |

#### `/push`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `project_id` | No | Project ID (auto-detected) |
| `branch_name` | No | Branch to push (defaults to current branch) |

#### `/git-pr`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `title` | Yes | PR title |
| `project_id` | No | Project ID (auto-detected) |
| `body` | No | PR description |
| `branch` | No | Head branch (defaults to current) |
| `base` | No | Base branch (defaults to repo default) |

Requires the `gh` CLI to be authenticated on the host machine.

---

## Automation (Hooks)

Hooks run automated workflows — they trigger on events or schedules, gather context, and send prompts to an LLM.

| Command | Description |
|---------|-------------|
| `/hooks` | List configured hooks |
| `/create-hook` | Create a new automation hook |
| `/edit-hook` | Edit a hook's settings (enable/disable, prompt, cooldown) |
| `/delete-hook` | Delete a hook and its run history |
| `/hook-runs` | Show recent execution history for a hook |
| `/fire-hook` | Manually trigger a hook immediately, ignoring cooldown |

#### `/create-hook`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `name` | Yes | Hook name |
| `trigger_type` | Yes | `periodic` or `event` |
| `trigger_value` | Yes | Interval in seconds (periodic) or event type (event) |
| `prompt_template` | Yes | Prompt with `{{step_N}}` and `{{event}}` placeholders |
| `project_id` | No | Project ID (auto-detected) |
| `cooldown_seconds` | No | Min seconds between runs (default: 3600) |

**Via chat** — managing hooks is often easier through natural language:

```
Create a hook for my-app that runs every 30 minutes
and checks if there are any failing tests. If tests
are failing, create a task to fix them.
```

```
Disable the test-monitor hook
```

```
Show me the last 5 runs of hook deploy-checker
```

#### `/edit-hook`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `hook_id` | Yes | Hook ID |
| `enabled` | No | Enable or disable the hook |
| `prompt_template` | No | New prompt template |
| `cooldown_seconds` | No | New cooldown in seconds |

#### `/hook-runs`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `hook_id` | Yes | Hook ID |
| `limit` | No | Number of runs to show (default: 10) |

---

## Project Notes

Notes are markdown documents stored per-project.

| Command | Description |
|---------|-------------|
| `/notes` | List notes for a project (opens an interactive thread) |
| `/write-note` | Create or update a project note |
| `/delete-note` | Delete a project note |

#### `/notes`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `project_id` | No | Project ID (auto-detected from channel) |

Lists all notes and opens a thread where you can ask the bot to read, create, or edit notes interactively.

#### `/write-note`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `title` | Yes | Note title |
| `content` | Yes | Note content (markdown) |
| `project_id` | No | Project ID (auto-detected from channel) |

#### `/delete-note`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `title` | Yes | Note title |
| `project_id` | No | Project ID (auto-detected from channel) |

---

## System Control

| Command | Description |
|---------|-------------|
| `/orchestrator` | Pause, resume, or check status of task scheduling |
| `/restart` | Restart the agent-queue daemon (brief disconnect) |

#### `/orchestrator`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `action` | Yes | `pause`, `resume`, or `status` |

When paused, no new tasks will be assigned to agents. Running tasks continue to completion.

---

## Chat-Only Capabilities

These features are only available through natural language chat:

### File operations

```
Read the file src/auth.py in my-app
Search for "def connect" in the my-app workspace
Find all .yaml files in the config directory
```

### Shell commands

```
Run "pytest tests/" in my-app
Run "npm test" in the frontend project
```

Commands run with a 30-second default timeout (max 120 seconds).

### Token usage

```
How many tokens has my-app used?
What's the token breakdown for task bold-falcon?
```

---

## Tips & Tricks

**Channel context saves typing.** Run commands from a project's dedicated channel and you won't need to specify `project_id` on every command. Use `/channel-map` to see which channels are linked.

**Task IDs are human-readable.** Tasks get names like `bold-falcon` or `quiet-owl` — easy to remember and type. Use them with any task command.

**Dependency chains.** When an agent produces a plan file, the orchestrator automatically creates chained subtasks with dependencies. Use `/chain-health` to diagnose stuck chains and `/skip-task` to unblock them.

**Just ask.** If you're not sure which command to use, just describe what you want in plain English. The chat agent has access to all the same operations as slash commands, plus file reading, shell commands, and more.

**Git command aliases.** Many git commands have multiple names: `/commit` and `/project-commit` and `/git-commit` all do the same thing. Use whichever feels natural.
