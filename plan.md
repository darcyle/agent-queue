# Plan: Additional Discord Commands for Git Management

## Overview

The agent-queue system already has a solid `GitManager` class (`src/git/manager.py`) with methods for creating branches, checking out branches, committing, pushing, merging, diffing, and creating PRs. However, the Discord interface only exposes **two** git-related commands today:

- `/git-status` — shows branch, working tree status, and recent commits for a project's repos
- `/task-diff` — shows the git diff for a specific task's branch

All other git operations (branch creation, checkout, commit, push, merge, PR creation, log viewing) are only accessible **indirectly** via the LLM chat agent's `run_command` tool (which runs raw shell commands) or happen **automatically** inside the orchestrator during task lifecycle. There are no dedicated commands for users to perform ad-hoc git operations on project repos from Discord.

This plan adds **7 new git management commands** exposed as both:
1. **CommandHandler backend methods** (`_cmd_*`) — the shared business logic
2. **LLM chat tools** (in `TOOLS` list) — so the chat agent can call them
3. **Discord slash commands** (`/git-*`) — for direct user interaction

---

## Step 1: Add New Command Handler Methods to `src/command_handler.py`

Add the following methods to the `CommandHandler` class, in the existing `# Git commands` section (after `_cmd_get_git_status`). Each method resolves the repository path from a `project_id` and optional `repo_id`, validates the checkout, and delegates to `self.orchestrator.git`.

### Helper: Resolve repo path

Add a private helper `_resolve_repo_path(project_id, repo_id=None)` that:
1. Looks up the project (returns error if not found)
2. If `repo_id` is given, looks up that specific repo
3. If no `repo_id`, picks the first repo for the project (or falls back to `project.workspace_path`)
4. Validates the path is a git checkout
5. Returns `(checkout_path, repo, error_dict)` — error_dict is None on success

This avoids duplicating repo-resolution logic across every new command.

```python
async def _resolve_repo_path(self, args: dict) -> tuple[str | None, RepoConfig | None, dict | None]:
    """Resolve the git checkout path for a project/repo pair.

    Returns (checkout_path, repo_config, error_dict).
    On success error_dict is None. On failure checkout_path is None.
    """
    project_id = args.get("project_id")
    if not project_id:
        return None, None, {"error": "project_id is required"}
    project = await self.db.get_project(project_id)
    if not project:
        return None, None, {"error": f"Project '{project_id}' not found"}

    repo_id = args.get("repo_id")
    git = self.orchestrator.git

    if repo_id:
        repo = await self.db.get_repo(repo_id)
        if not repo:
            return None, None, {"error": f"Repo '{repo_id}' not found"}
    else:
        repos = await self.db.list_repos(project_id=project_id)
        repo = repos[0] if repos else None

    if repo:
        if repo.source_type == RepoSourceType.LINK and repo.source_path:
            checkout_path = repo.source_path
        elif repo.source_type == RepoSourceType.CLONE and repo.checkout_base_path:
            checkout_path = repo.checkout_base_path
        else:
            return None, repo, {"error": f"Repo '{repo.id}' has no usable path"}
    else:
        checkout_path = project.workspace_path
        if not checkout_path or not os.path.isdir(checkout_path):
            return None, None, {"error": f"Project '{project_id}' has no repos and no valid workspace"}

    if not os.path.isdir(checkout_path):
        return None, repo, {"error": f"Path not found: {checkout_path}"}
    if not git.validate_checkout(checkout_path):
        return None, repo, {"error": f"Not a valid git repository: {checkout_path}"}

    return checkout_path, repo, None
```

### 1a. `_cmd_create_branch`

Creates a new branch in a project's repo. Uses `git checkout -b <branch_name>`.

```python
async def _cmd_create_branch(self, args: dict) -> dict:
    branch_name = args.get("branch_name")
    if not branch_name:
        return {"error": "branch_name is required"}

    checkout_path, repo, err = await self._resolve_repo_path(args)
    if err:
        return err

    git = self.orchestrator.git
    try:
        git.create_branch(checkout_path, branch_name)
    except Exception as e:
        return {"error": f"Failed to create branch: {e}"}

    return {
        "project_id": args["project_id"],
        "repo_id": repo.id if repo else "(workspace)",
        "branch": branch_name,
        "status": "created",
    }
```

### 1b. `_cmd_checkout_branch`

Checks out an existing branch. Uses `git checkout <branch_name>`.

```python
async def _cmd_checkout_branch(self, args: dict) -> dict:
    branch_name = args.get("branch_name")
    if not branch_name:
        return {"error": "branch_name is required"}

    checkout_path, repo, err = await self._resolve_repo_path(args)
    if err:
        return err

    git = self.orchestrator.git
    try:
        git.checkout(checkout_path, branch_name)
    except Exception as e:
        return {"error": f"Failed to checkout branch: {e}"}

    return {
        "project_id": args["project_id"],
        "repo_id": repo.id if repo else "(workspace)",
        "branch": branch_name,
        "status": "checked_out",
    }
```

### 1c. `_cmd_commit_changes`

Stages all changes and commits with a message. Uses the existing `git.commit_all()`.

```python
async def _cmd_commit_changes(self, args: dict) -> dict:
    message = args.get("message")
    if not message:
        return {"error": "message is required"}

    checkout_path, repo, err = await self._resolve_repo_path(args)
    if err:
        return err

    git = self.orchestrator.git
    try:
        committed = git.commit_all(checkout_path, message)
    except Exception as e:
        return {"error": f"Failed to commit: {e}"}

    if not committed:
        return {
            "project_id": args["project_id"],
            "repo_id": repo.id if repo else "(workspace)",
            "status": "nothing_to_commit",
            "message": "No changes to commit",
        }

    return {
        "project_id": args["project_id"],
        "repo_id": repo.id if repo else "(workspace)",
        "commit_message": message,
        "status": "committed",
    }
```

### 1d. `_cmd_push_branch`

Pushes the current (or specified) branch to origin. Uses `git.push_branch()`.

```python
async def _cmd_push_branch(self, args: dict) -> dict:
    checkout_path, repo, err = await self._resolve_repo_path(args)
    if err:
        return err

    git = self.orchestrator.git
    branch_name = args.get("branch_name")
    if not branch_name:
        branch_name = git.get_current_branch(checkout_path)
        if not branch_name:
            return {"error": "Could not determine current branch"}

    try:
        git.push_branch(checkout_path, branch_name)
    except Exception as e:
        return {"error": f"Failed to push: {e}"}

    return {
        "project_id": args["project_id"],
        "repo_id": repo.id if repo else "(workspace)",
        "branch": branch_name,
        "status": "pushed",
    }
```

### 1e. `_cmd_merge_branch`

Merges a branch into the default branch. Uses `git.merge_branch()`.

```python
async def _cmd_merge_branch(self, args: dict) -> dict:
    branch_name = args.get("branch_name")
    if not branch_name:
        return {"error": "branch_name is required"}

    checkout_path, repo, err = await self._resolve_repo_path(args)
    if err:
        return err

    git = self.orchestrator.git
    default_branch = repo.default_branch if repo else "main"

    try:
        success = git.merge_branch(checkout_path, branch_name, default_branch)
    except Exception as e:
        return {"error": f"Failed to merge: {e}"}

    if not success:
        return {
            "project_id": args["project_id"],
            "repo_id": repo.id if repo else "(workspace)",
            "branch": branch_name,
            "target": default_branch,
            "status": "conflict",
            "message": "Merge conflict — merge was aborted",
        }

    return {
        "project_id": args["project_id"],
        "repo_id": repo.id if repo else "(workspace)",
        "branch": branch_name,
        "target": default_branch,
        "status": "merged",
    }
```

### 1f. `_cmd_git_log`

Shows the git log for a project's repo. Uses `git.get_recent_commits()`.

```python
async def _cmd_git_log(self, args: dict) -> dict:
    checkout_path, repo, err = await self._resolve_repo_path(args)
    if err:
        return err

    git = self.orchestrator.git
    count = args.get("count", 10)

    log_output = git.get_recent_commits(checkout_path, count=count)
    branch = git.get_current_branch(checkout_path)

    return {
        "project_id": args["project_id"],
        "repo_id": repo.id if repo else "(workspace)",
        "branch": branch,
        "log": log_output or "(no commits)",
    }
```

### 1g. `_cmd_git_diff`

Shows the diff of the working tree or against a base branch. Complements the existing task-scoped `_cmd_get_task_diff`.

```python
async def _cmd_git_diff(self, args: dict) -> dict:
    checkout_path, repo, err = await self._resolve_repo_path(args)
    if err:
        return err

    git = self.orchestrator.git
    base = args.get("base_branch")

    try:
        if base:
            diff = git.get_diff(checkout_path, base)
        else:
            # Working tree diff (unstaged changes)
            diff = git._run(["diff"], cwd=checkout_path)
    except Exception as e:
        return {"error": f"Failed to get diff: {e}"}

    return {
        "project_id": args["project_id"],
        "repo_id": repo.id if repo else "(workspace)",
        "base_branch": base or "(working tree)",
        "diff": diff or "(no changes)",
    }
```

---

## Step 2: Add LLM Tool Definitions to `src/chat_agent.py`

Add the following entries to the `TOOLS` list in `src/chat_agent.py`. These mirror the command handler methods and let the LLM chat agent invoke them via tool use.

```python
{
    "name": "create_branch",
    "description": "Create a new git branch in a project's repository.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID"},
            "branch_name": {"type": "string", "description": "Name for the new branch"},
            "repo_id": {"type": "string", "description": "Specific repo ID (optional — uses first repo if omitted)"},
        },
        "required": ["project_id", "branch_name"],
    },
},
{
    "name": "checkout_branch",
    "description": "Switch to an existing git branch in a project's repository.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID"},
            "branch_name": {"type": "string", "description": "Branch name to check out"},
            "repo_id": {"type": "string", "description": "Specific repo ID (optional — uses first repo if omitted)"},
        },
        "required": ["project_id", "branch_name"],
    },
},
{
    "name": "commit_changes",
    "description": "Stage all changes and create a git commit in a project's repository.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID"},
            "message": {"type": "string", "description": "Commit message"},
            "repo_id": {"type": "string", "description": "Specific repo ID (optional — uses first repo if omitted)"},
        },
        "required": ["project_id", "message"],
    },
},
{
    "name": "push_branch",
    "description": "Push a branch to the remote origin in a project's repository.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID"},
            "branch_name": {"type": "string", "description": "Branch to push (optional — pushes current branch if omitted)"},
            "repo_id": {"type": "string", "description": "Specific repo ID (optional — uses first repo if omitted)"},
        },
        "required": ["project_id"],
    },
},
{
    "name": "merge_branch",
    "description": "Merge a branch into the default branch (e.g., main). Aborts if there are conflicts.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID"},
            "branch_name": {"type": "string", "description": "Branch to merge into the default branch"},
            "repo_id": {"type": "string", "description": "Specific repo ID (optional — uses first repo if omitted)"},
        },
        "required": ["project_id", "branch_name"],
    },
},
{
    "name": "git_log",
    "description": "Show recent git commits for a project's repository.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID"},
            "count": {"type": "integer", "description": "Number of commits to show (default 10)", "default": 10},
            "repo_id": {"type": "string", "description": "Specific repo ID (optional — uses first repo if omitted)"},
        },
        "required": ["project_id"],
    },
},
{
    "name": "git_diff",
    "description": "Show the git diff for a project's repository. Without base_branch shows working tree changes; with base_branch shows diff against that branch.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID"},
            "base_branch": {"type": "string", "description": "Base branch to diff against (optional — shows working tree diff if omitted)"},
            "repo_id": {"type": "string", "description": "Specific repo ID (optional — uses first repo if omitted)"},
        },
        "required": ["project_id"],
    },
},
```

Also update the `SYSTEM_PROMPT_TEMPLATE` in the same file, adding to the git-related capabilities list:

```
- Create branches with `create_branch`, switch branches with `checkout_branch`
- Commit changes with `commit_changes`, push branches with `push_branch`
- Merge branches with `merge_branch`
- View commit history with `git_log`, see diffs with `git_diff`
```

---

## Step 3: Add Discord Slash Commands to `src/discord/commands.py`

Add the following slash commands in a new `# GIT MANAGEMENT COMMANDS` section, right after the existing `git-status` command. Each command follows the established pattern: thin formatting wrapper → `handler.execute()` → format response.

### 3a. `/create-branch`

```python
@bot.tree.command(name="create-branch", description="Create a new git branch in a project's repo")
@app_commands.describe(
    project_id="Project ID",
    branch_name="Name for the new branch",
    repo_id="Specific repo ID (optional)",
)
async def create_branch_command(
    interaction: discord.Interaction,
    project_id: str,
    branch_name: str,
    repo_id: str | None = None,
):
    args = {"project_id": project_id, "branch_name": branch_name}
    if repo_id:
        args["repo_id"] = repo_id
    result = await handler.execute("create_branch", args)
    if "error" in result:
        await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
        return
    await interaction.response.send_message(
        f"🌿 Branch `{branch_name}` created in `{result.get('repo_id', project_id)}`"
    )
```

### 3b. `/checkout-branch`

```python
@bot.tree.command(name="checkout-branch", description="Switch to an existing git branch")
@app_commands.describe(
    project_id="Project ID",
    branch_name="Branch name to check out",
    repo_id="Specific repo ID (optional)",
)
async def checkout_branch_command(
    interaction: discord.Interaction,
    project_id: str,
    branch_name: str,
    repo_id: str | None = None,
):
    args = {"project_id": project_id, "branch_name": branch_name}
    if repo_id:
        args["repo_id"] = repo_id
    result = await handler.execute("checkout_branch", args)
    if "error" in result:
        await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
        return
    await interaction.response.send_message(
        f"🔀 Switched to branch `{branch_name}` in `{result.get('repo_id', project_id)}`"
    )
```

### 3c. `/commit`

```python
@bot.tree.command(name="commit", description="Stage all changes and commit")
@app_commands.describe(
    project_id="Project ID",
    message="Commit message",
    repo_id="Specific repo ID (optional)",
)
async def commit_command(
    interaction: discord.Interaction,
    project_id: str,
    message: str,
    repo_id: str | None = None,
):
    args = {"project_id": project_id, "message": message}
    if repo_id:
        args["repo_id"] = repo_id
    result = await handler.execute("commit_changes", args)
    if "error" in result:
        await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
        return
    if result.get("status") == "nothing_to_commit":
        await interaction.response.send_message(
            f"ℹ️ Nothing to commit in `{result.get('repo_id', project_id)}` — working tree clean."
        )
        return
    await interaction.response.send_message(
        f"✅ Committed in `{result.get('repo_id', project_id)}`: {message}"
    )
```

### 3d. `/push`

```python
@bot.tree.command(name="push", description="Push a branch to the remote")
@app_commands.describe(
    project_id="Project ID",
    branch_name="Branch to push (optional — pushes current branch)",
    repo_id="Specific repo ID (optional)",
)
async def push_command(
    interaction: discord.Interaction,
    project_id: str,
    branch_name: str | None = None,
    repo_id: str | None = None,
):
    args: dict = {"project_id": project_id}
    if branch_name:
        args["branch_name"] = branch_name
    if repo_id:
        args["repo_id"] = repo_id
    result = await handler.execute("push_branch", args)
    if "error" in result:
        await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
        return
    pushed_branch = result.get("branch", branch_name or "current")
    await interaction.response.send_message(
        f"🚀 Pushed `{pushed_branch}` in `{result.get('repo_id', project_id)}`"
    )
```

### 3e. `/merge`

```python
@bot.tree.command(name="merge", description="Merge a branch into the default branch")
@app_commands.describe(
    project_id="Project ID",
    branch_name="Branch to merge",
    repo_id="Specific repo ID (optional)",
)
async def merge_command(
    interaction: discord.Interaction,
    project_id: str,
    branch_name: str,
    repo_id: str | None = None,
):
    args = {"project_id": project_id, "branch_name": branch_name}
    if repo_id:
        args["repo_id"] = repo_id
    result = await handler.execute("merge_branch", args)
    if "error" in result:
        await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
        return
    if result.get("status") == "conflict":
        await interaction.response.send_message(
            f"⚠️ Merge conflict when merging `{branch_name}` → `{result.get('target', 'main')}`. "
            f"Merge was aborted."
        )
        return
    await interaction.response.send_message(
        f"✅ Merged `{branch_name}` → `{result.get('target', 'main')}` in `{result.get('repo_id', project_id)}`"
    )
```

### 3f. `/git-log`

```python
@bot.tree.command(name="git-log", description="Show recent git commits")
@app_commands.describe(
    project_id="Project ID",
    count="Number of commits to show (default 10)",
    repo_id="Specific repo ID (optional)",
)
async def git_log_command(
    interaction: discord.Interaction,
    project_id: str,
    count: int = 10,
    repo_id: str | None = None,
):
    args: dict = {"project_id": project_id, "count": count}
    if repo_id:
        args["repo_id"] = repo_id
    result = await handler.execute("git_log", args)
    if "error" in result:
        await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
        return
    branch = result.get("branch", "?")
    log = result.get("log", "(no commits)")
    repo_label = result.get("repo_id", project_id)
    msg = f"## Git Log: `{repo_label}` (branch: `{branch}`)\n```\n{log}\n```"
    await _send_long(interaction, msg, followup=False)
```

### 3g. `/git-diff`

```python
@bot.tree.command(name="git-diff", description="Show git diff for a project's repo")
@app_commands.describe(
    project_id="Project ID",
    base_branch="Base branch to diff against (optional — shows working tree diff)",
    repo_id="Specific repo ID (optional)",
)
async def git_diff_command(
    interaction: discord.Interaction,
    project_id: str,
    base_branch: str | None = None,
    repo_id: str | None = None,
):
    args: dict = {"project_id": project_id}
    if base_branch:
        args["base_branch"] = base_branch
    if repo_id:
        args["repo_id"] = repo_id
    result = await handler.execute("git_diff", args)
    if "error" in result:
        await interaction.response.send_message(f"Error: {result['error']}", ephemeral=True)
        return

    diff = result.get("diff", "(no changes)")
    base_label = result.get("base_branch", "working tree")
    repo_label = result.get("repo_id", project_id)
    header = f"**Repo:** `{repo_label}` | **Diff against:** `{base_label}`\n"

    if len(diff) > 1800:
        await interaction.response.defer()
        await interaction.followup.send(
            content=f"{header}*Diff attached ({len(diff):,} chars)*",
            file=discord.File(
                fp=io.BytesIO(diff.encode("utf-8")),
                filename=f"diff-{project_id}.patch",
            ),
        )
    else:
        await interaction.response.send_message(
            f"{header}```diff\n{diff}\n```"
        )
```

---

## Step 4: Add `checkout` Method to `GitManager` (`src/git/manager.py`)

The `GitManager` currently has `create_branch()` (which does `checkout -b`) but no plain `checkout` method for switching to an existing branch. Add one:

```python
def checkout(self, checkout_path: str, branch_name: str) -> None:
    """Switch to an existing branch."""
    self._run(["checkout", branch_name], cwd=checkout_path)
```

Then update `_cmd_checkout_branch` in the command handler to call `git.checkout()` instead of the raw `git._run()` call, keeping the public API clean.

---

## Step 5: Safety Considerations

### 5a. Authorization Guard

All new commands should respect the existing authorization model. The Discord bot already checks `authorized_users` in the config for messages. Slash commands should follow the same pattern. No additional auth changes needed since the existing bot-level authorization applies to all slash commands equally.

### 5b. Prevent Destructive Operations During Active Tasks

Add a safety check to `_cmd_checkout_branch`, `_cmd_merge_branch`, and `_cmd_commit_changes`: if any task is `IN_PROGRESS` for the same project and repo, warn the user that switching branches or merging could disrupt the running agent. The command should still execute (the user may know what they're doing), but the response should include a warning.

```python
# In each potentially-disruptive command handler:
in_progress_tasks = await self.db.list_tasks(
    project_id=args["project_id"], status=TaskStatus.IN_PROGRESS
)
warning = None
if in_progress_tasks:
    warning = f"⚠️ {len(in_progress_tasks)} task(s) currently IN_PROGRESS for this project"

# Include warning in result dict if present:
if warning:
    result["warning"] = warning
```

The slash command formatter then appends the warning to the Discord message.

### 5c. Git Error Handling

All `GitManager` operations can raise `GitError`. The command handler methods wrap them in try/except and return structured `{"error": ...}` dicts. The slash commands display errors with `ephemeral=True` so only the command issuer sees them.

---

## Step 6: Testing

### 6a. Unit Tests for New Command Handler Methods

Add tests in `tests/` for each new command handler method:

- `test_create_branch` — success, missing branch_name, invalid project
- `test_checkout_branch` — success, branch not found, invalid project
- `test_commit_changes` — success, nothing to commit, missing message
- `test_push_branch` — success with explicit branch, success with current branch, push failure
- `test_merge_branch` — success, conflict scenario, missing branch_name
- `test_git_log` — success, custom count
- `test_git_diff` — working tree diff, diff against base branch

These tests should mock the `GitManager` and `Database` to test command handler logic in isolation.

### 6b. Integration Test for `_resolve_repo_path`

Test the resolution logic:
- Project with linked repo → returns source_path
- Project with cloned repo → returns checkout_base_path
- Project with no repos → falls back to workspace_path
- Invalid project → returns error
- Invalid repo_id → returns error

---

## Summary of Changes by File

| File | Changes |
|------|---------|
| `src/git/manager.py` | Add `checkout()` method |
| `src/command_handler.py` | Add `_resolve_repo_path()` helper + 7 new `_cmd_*` methods |
| `src/chat_agent.py` | Add 7 tool definitions to `TOOLS` list + update system prompt |
| `src/discord/commands.py` | Add 7 new slash commands with formatting |
| `tests/test_git_commands.py` | New test file for git command handler methods |

### New Discord Slash Commands Summary

| Command | Description | Required Params | Optional Params |
|---------|-------------|-----------------|-----------------|
| `/create-branch` | Create a new branch | `project_id`, `branch_name` | `repo_id` |
| `/checkout-branch` | Switch to existing branch | `project_id`, `branch_name` | `repo_id` |
| `/commit` | Stage all + commit | `project_id`, `message` | `repo_id` |
| `/push` | Push branch to remote | `project_id` | `branch_name`, `repo_id` |
| `/merge` | Merge branch into default | `project_id`, `branch_name` | `repo_id` |
| `/git-log` | Show recent commits | `project_id` | `count`, `repo_id` |
| `/git-diff` | Show working tree or branch diff | `project_id` | `base_branch`, `repo_id` |

All commands integrate seamlessly with the existing three-layer architecture (CommandHandler → LLM Tools → Discord Slash Commands) and require no database schema changes.
