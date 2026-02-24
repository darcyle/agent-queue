# Plan: Add 7 New Discord Git Management Commands

## Background

The agent-queue system has a capable `GitManager` class (`src/git/manager.py`) with methods for branching, committing, pushing, merging, diffing, PR creation, and log viewing. However, only **2 git commands** are currently exposed to users:

- `/git-status` → `_cmd_get_git_status` — shows branch, working tree status, and recent commits
- `/task-diff` → `_cmd_get_task_diff` — shows the git diff for a specific task's branch

All other git operations (branch creation, checkout, commit, push, merge, PR creation, log viewing) are only accessible **indirectly** via the LLM chat agent's `run_command` tool or happen **automatically** inside the orchestrator during task lifecycle.

This plan adds **7 new git management commands**, each exposed as:
1. **CommandHandler `_cmd_*` method** — shared business logic in `src/command_handler.py`
2. **LLM chat tool** — entry in the `TOOLS` list in `src/chat_agent.py`
3. **Discord slash command** — `/git-*` command in `src/discord/commands.py`

### The 7 New Commands

| # | Command | GitManager method(s) used | Purpose |
|---|---------|--------------------------|---------|
| 1 | `/git-log` | `get_recent_commits()` | View commit history for a project repo |
| 2 | `/git-branch` | `create_branch()`, new `list_branches()`, `get_current_branch()` | List branches or create a new one |
| 3 | `/git-checkout` | new `checkout_branch()` | Switch to an existing branch |
| 4 | `/git-commit` | `commit_all()` | Stage all changes and commit with a message |
| 5 | `/git-push` | `push_branch()`, `get_current_branch()` | Push current branch to origin |
| 6 | `/git-merge` | `merge_branch()` | Merge a branch into the default branch |
| 7 | `/git-diff` | `get_diff()`, `get_changed_files()` | Show diff for a repo (not task-specific) |

---

## Shared Pattern: Repo Resolution Helper

All 7 commands need to resolve a `checkout_path` from a `project_id` and optional `repo_id`. Currently `_cmd_get_git_status` does this inline. We should extract a reusable helper.

### Files Modified
- `src/command_handler.py`

### Implementation

Add a private helper method to `CommandHandler`:

```python
async def _resolve_repo_path(self, project_id: str, repo_id: str | None = None) -> dict:
    """Resolve a project+repo to a checkout path.

    Returns {"path": str, "repo": RepoConfig | None, "project": Project}
    on success, or {"error": str} on failure.
    """
    project = await self.db.get_project(project_id)
    if not project:
        return {"error": f"Project '{project_id}' not found"}

    repos = await self.db.list_repos(project_id=project_id)

    if repo_id:
        # Find specific repo
        repo = next((r for r in repos if r.id == repo_id), None)
        if not repo:
            return {"error": f"Repo '{repo_id}' not found in project '{project_id}'"}
        repos = [repo]

    if repos:
        repo = repos[0]  # Use first repo if no repo_id specified
        if repo.source_type == RepoSourceType.LINK and repo.source_path:
            repo_path = repo.source_path
        elif repo.source_type in (RepoSourceType.CLONE, RepoSourceType.INIT) and repo.checkout_base_path:
            repo_path = repo.checkout_base_path
        else:
            return {"error": f"Repo '{repo.id}' has no valid path configured"}

        if not os.path.isdir(repo_path):
            return {"error": f"Path not found: {repo_path}"}
        if not self.orchestrator.git.validate_checkout(repo_path):
            return {"error": f"Not a valid git repo: {repo_path}"}

        return {"path": repo_path, "repo": repo, "project": project}

    # Fallback to project workspace
    workspace = project.workspace_path
    if not workspace or not os.path.isdir(workspace):
        return {"error": f"Project '{project_id}' has no repos and no valid workspace path"}
    if not self.orchestrator.git.validate_checkout(workspace):
        return {"error": f"Project workspace is not a git repository"}

    return {"path": workspace, "repo": None, "project": project}
```

This eliminates duplicated repo-resolution logic across all 7 commands and the existing `_cmd_get_git_status`.

---

## Step 1: Add GitManager Helper Methods

### Files Modified
- `src/git/manager.py`

### New Methods

Two new methods needed that don't exist yet:

#### 1a. `checkout_branch(checkout_path, branch_name)` — switch to existing branch

```python
def checkout_branch(self, checkout_path: str, branch_name: str) -> None:
    """Switch to an existing branch."""
    self._run(["checkout", branch_name], cwd=checkout_path)
```

#### 1b. `list_branches(checkout_path)` — list all local branches

```python
def list_branches(self, checkout_path: str) -> list[str]:
    """Return a list of local branch names. Current branch is prefixed with '*'."""
    try:
        output = self._run(["branch", "--list"], cwd=checkout_path)
        return [line.strip() for line in output.split("\n") if line.strip()]
    except GitError:
        return []
```

These are the only additions needed to `GitManager` — all other required operations are already implemented.

---

## Step 2: Add CommandHandler `_cmd_*` Methods

### Files Modified
- `src/command_handler.py`

All 7 new methods go in the `# Git commands` section (after `_cmd_get_git_status`, around line 728). Each follows the existing pattern: `async def _cmd_{name}(self, args: dict) -> dict`.

### 2a. `_cmd_git_log`

```python
async def _cmd_git_log(self, args: dict) -> dict:
    """Get commit history for a project's repository."""
    resolved = await self._resolve_repo_path(args["project_id"], args.get("repo_id"))
    if "error" in resolved:
        return resolved

    count = args.get("count", 20)
    repo_path = resolved["path"]
    git = self.orchestrator.git

    branch = git.get_current_branch(repo_path)
    commits = git.get_recent_commits(repo_path, count=count)

    return {
        "project_id": args["project_id"],
        "branch": branch,
        "commits": commits or "(no commits)",
    }
```

### 2b. `_cmd_git_branch`

```python
async def _cmd_git_branch(self, args: dict) -> dict:
    """List branches or create a new branch."""
    resolved = await self._resolve_repo_path(args["project_id"], args.get("repo_id"))
    if "error" in resolved:
        return resolved

    repo_path = resolved["path"]
    git = self.orchestrator.git
    new_branch = args.get("name")

    if new_branch:
        git.create_branch(repo_path, new_branch)
        return {
            "project_id": args["project_id"],
            "created": new_branch,
            "message": f"Created and switched to branch '{new_branch}'",
        }
    else:
        branches = git.list_branches(repo_path)
        current = git.get_current_branch(repo_path)
        return {
            "project_id": args["project_id"],
            "current_branch": current,
            "branches": branches,
        }
```

### 2c. `_cmd_git_checkout`

```python
async def _cmd_git_checkout(self, args: dict) -> dict:
    """Switch to an existing branch."""
    resolved = await self._resolve_repo_path(args["project_id"], args.get("repo_id"))
    if "error" in resolved:
        return resolved

    repo_path = resolved["path"]
    branch = args["branch"]
    git = self.orchestrator.git

    old_branch = git.get_current_branch(repo_path)
    git.checkout_branch(repo_path, branch)
    new_branch = git.get_current_branch(repo_path)

    return {
        "project_id": args["project_id"],
        "old_branch": old_branch,
        "new_branch": new_branch,
        "message": f"Switched from '{old_branch}' to '{new_branch}'",
    }
```

### 2d. `_cmd_git_commit`

```python
async def _cmd_git_commit(self, args: dict) -> dict:
    """Stage all changes and commit."""
    resolved = await self._resolve_repo_path(args["project_id"], args.get("repo_id"))
    if "error" in resolved:
        return resolved

    repo_path = resolved["path"]
    message = args["message"]
    git = self.orchestrator.git

    committed = git.commit_all(repo_path, message)
    if not committed:
        return {
            "project_id": args["project_id"],
            "committed": False,
            "message": "Nothing to commit — working tree clean",
        }

    branch = git.get_current_branch(repo_path)
    return {
        "project_id": args["project_id"],
        "committed": True,
        "branch": branch,
        "commit_message": message,
    }
```

### 2e. `_cmd_git_push`

```python
async def _cmd_git_push(self, args: dict) -> dict:
    """Push current branch to origin."""
    resolved = await self._resolve_repo_path(args["project_id"], args.get("repo_id"))
    if "error" in resolved:
        return resolved

    repo_path = resolved["path"]
    git = self.orchestrator.git

    branch = args.get("branch") or git.get_current_branch(repo_path)
    git.push_branch(repo_path, branch)

    return {
        "project_id": args["project_id"],
        "pushed": branch,
        "message": f"Pushed branch '{branch}' to origin",
    }
```

### 2f. `_cmd_git_merge`

```python
async def _cmd_git_merge(self, args: dict) -> dict:
    """Merge a branch into the default branch."""
    resolved = await self._resolve_repo_path(args["project_id"], args.get("repo_id"))
    if "error" in resolved:
        return resolved

    repo_path = resolved["path"]
    git = self.orchestrator.git
    branch = args["branch"]

    # Determine default branch from repo config or fallback
    default_branch = "main"
    if resolved.get("repo") and resolved["repo"].default_branch:
        default_branch = resolved["repo"].default_branch
    target = args.get("target_branch", default_branch)

    success = git.merge_branch(repo_path, branch, default_branch=target)

    if success:
        return {
            "project_id": args["project_id"],
            "merged": branch,
            "into": target,
            "message": f"Successfully merged '{branch}' into '{target}'",
        }
    else:
        return {
            "project_id": args["project_id"],
            "merged": False,
            "branch": branch,
            "into": target,
            "message": f"Merge conflict — merge of '{branch}' into '{target}' was aborted",
        }
```

### 2g. `_cmd_git_diff`

```python
async def _cmd_git_diff(self, args: dict) -> dict:
    """Show diff for a project repo against a base branch."""
    resolved = await self._resolve_repo_path(args["project_id"], args.get("repo_id"))
    if "error" in resolved:
        return resolved

    repo_path = resolved["path"]
    git = self.orchestrator.git

    # Determine base branch
    default_branch = "main"
    if resolved.get("repo") and resolved["repo"].default_branch:
        default_branch = resolved["repo"].default_branch
    base = args.get("base_branch", default_branch)

    current_branch = git.get_current_branch(repo_path)
    diff = git.get_diff(repo_path, base)
    changed_files = git.get_changed_files(repo_path, base)

    return {
        "project_id": args["project_id"],
        "branch": current_branch,
        "base_branch": base,
        "diff": diff or "(no changes)",
        "changed_files": changed_files,
        "file_count": len(changed_files),
    }
```

---

## Step 3: Add LLM Chat Tool Definitions

### Files Modified
- `src/chat_agent.py`

Add 7 new entries to the `TOOLS` list, placed after the existing `get_git_status` tool definition (around line 590). Also update the system prompt section that lists capabilities (around line 659-660).

### Tool Definitions

```python
{
    "name": "git_log",
    "description": "Show the recent commit log for a project's repository.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID"},
            "repo_id": {"type": "string", "description": "Repo ID (optional — uses first repo if omitted)"},
            "count": {"type": "integer", "description": "Number of commits to show (default 20)", "default": 20},
        },
        "required": ["project_id"],
    },
},
{
    "name": "git_branch",
    "description": "List branches or create a new branch on a project's repository. Omit 'name' to list branches; provide 'name' to create and switch to a new branch.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID"},
            "repo_id": {"type": "string", "description": "Repo ID (optional)"},
            "name": {"type": "string", "description": "New branch name to create (omit to list branches)"},
        },
        "required": ["project_id"],
    },
},
{
    "name": "git_checkout",
    "description": "Switch to an existing branch on a project's repository.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID"},
            "repo_id": {"type": "string", "description": "Repo ID (optional)"},
            "branch": {"type": "string", "description": "Branch name to switch to"},
        },
        "required": ["project_id", "branch"],
    },
},
{
    "name": "git_commit",
    "description": "Stage all changes and commit them with a message on a project's repository.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID"},
            "repo_id": {"type": "string", "description": "Repo ID (optional)"},
            "message": {"type": "string", "description": "Commit message"},
        },
        "required": ["project_id", "message"],
    },
},
{
    "name": "git_push",
    "description": "Push a branch to the remote origin for a project's repository.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID"},
            "repo_id": {"type": "string", "description": "Repo ID (optional)"},
            "branch": {"type": "string", "description": "Branch to push (defaults to current branch)"},
        },
        "required": ["project_id"],
    },
},
{
    "name": "git_merge",
    "description": "Merge a branch into the default (or specified target) branch. Automatically aborts if there are conflicts.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID"},
            "repo_id": {"type": "string", "description": "Repo ID (optional)"},
            "branch": {"type": "string", "description": "Branch to merge"},
            "target_branch": {"type": "string", "description": "Target branch to merge into (defaults to repo's default branch)"},
        },
        "required": ["project_id", "branch"],
    },
},
{
    "name": "git_diff",
    "description": "Show the diff for a project's repository against a base branch. More flexible than get_task_diff — works on any repo, not tied to a specific task.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID"},
            "repo_id": {"type": "string", "description": "Repo ID (optional)"},
            "base_branch": {"type": "string", "description": "Base branch to diff against (defaults to repo's default branch)"},
        },
        "required": ["project_id"],
    },
},
```

### System Prompt Update

Update the capabilities list in the system prompt (around line 659) to include:

```
- View commit logs with `git_log`
- Create branches or list branches with `git_branch`
- Switch branches with `git_checkout`
- Commit changes with `git_commit`
- Push branches to origin with `git_push`
- Merge branches with `git_merge`
- View diffs against base branches with `git_diff`
```

---

## Step 4: Add Discord Slash Commands

### Files Modified
- `src/discord/commands.py`

Add 7 new slash commands in the `# GIT COMMANDS` section (after `/git-status`, around line 925). Each command follows the established pattern: defer → `handler.execute()` → format/send.

### 4a. `/git-log`

```python
@bot.tree.command(name="git-log", description="Show commit history for a project's repository")
@app_commands.describe(
    project_id="Project ID",
    repo_id="Repo ID (optional — uses first repo if omitted)",
    count="Number of commits to show (default 20)",
)
async def git_log_command(
    interaction: discord.Interaction,
    project_id: str,
    repo_id: str | None = None,
    count: int = 20,
):
    await interaction.response.defer()
    args = {"project_id": project_id, "count": count}
    if repo_id:
        args["repo_id"] = repo_id
    result = await handler.execute("git_log", args)
    if "error" in result:
        await interaction.followup.send(f"Error: {result['error']}")
        return
    branch = result.get("branch", "?")
    commits = result.get("commits", "(no commits)")
    text = f"## Commit Log: `{project_id}`\n**Branch:** `{branch}`\n```\n{commits}\n```"
    await _send_long(interaction, text, followup=True)
```

### 4b. `/git-branch`

```python
@bot.tree.command(name="git-branch", description="List branches or create a new branch")
@app_commands.describe(
    project_id="Project ID",
    repo_id="Repo ID (optional)",
    name="New branch name to create (omit to list branches)",
)
async def git_branch_command(
    interaction: discord.Interaction,
    project_id: str,
    repo_id: str | None = None,
    name: str | None = None,
):
    await interaction.response.defer()
    args = {"project_id": project_id}
    if repo_id:
        args["repo_id"] = repo_id
    if name:
        args["name"] = name
    result = await handler.execute("git_branch", args)
    if "error" in result:
        await interaction.followup.send(f"Error: {result['error']}")
        return

    if "created" in result:
        await interaction.followup.send(
            f"Created branch `{result['created']}` on `{project_id}`"
        )
    else:
        current = result.get("current_branch", "?")
        branches = result.get("branches", [])
        branch_list = "\n".join(branches) if branches else "(no branches)"
        text = (
            f"## Branches: `{project_id}`\n"
            f"**Current:** `{current}`\n```\n{branch_list}\n```"
        )
        await _send_long(interaction, text, followup=True)
```

### 4c. `/git-checkout`

```python
@bot.tree.command(name="git-checkout", description="Switch to an existing branch")
@app_commands.describe(
    project_id="Project ID",
    branch="Branch name to switch to",
    repo_id="Repo ID (optional)",
)
async def git_checkout_command(
    interaction: discord.Interaction,
    project_id: str,
    branch: str,
    repo_id: str | None = None,
):
    await interaction.response.defer()
    args = {"project_id": project_id, "branch": branch}
    if repo_id:
        args["repo_id"] = repo_id
    result = await handler.execute("git_checkout", args)
    if "error" in result:
        await interaction.followup.send(f"Error: {result['error']}")
        return
    await interaction.followup.send(
        f"Switched from `{result['old_branch']}` to `{result['new_branch']}` on `{project_id}`"
    )
```

### 4d. `/git-commit`

```python
@bot.tree.command(name="git-commit", description="Stage all changes and commit")
@app_commands.describe(
    project_id="Project ID",
    message="Commit message",
    repo_id="Repo ID (optional)",
)
async def git_commit_command(
    interaction: discord.Interaction,
    project_id: str,
    message: str,
    repo_id: str | None = None,
):
    await interaction.response.defer()
    args = {"project_id": project_id, "message": message}
    if repo_id:
        args["repo_id"] = repo_id
    result = await handler.execute("git_commit", args)
    if "error" in result:
        await interaction.followup.send(f"Error: {result['error']}")
        return
    if result.get("committed"):
        branch = result.get("branch", "?")
        await interaction.followup.send(
            f"Committed on `{branch}`: {result.get('commit_message', message)}"
        )
    else:
        await interaction.followup.send("Nothing to commit — working tree clean")
```

### 4e. `/git-push`

```python
@bot.tree.command(name="git-push", description="Push a branch to origin")
@app_commands.describe(
    project_id="Project ID",
    branch="Branch to push (defaults to current branch)",
    repo_id="Repo ID (optional)",
)
async def git_push_command(
    interaction: discord.Interaction,
    project_id: str,
    branch: str | None = None,
    repo_id: str | None = None,
):
    await interaction.response.defer()
    args = {"project_id": project_id}
    if branch:
        args["branch"] = branch
    if repo_id:
        args["repo_id"] = repo_id
    result = await handler.execute("git_push", args)
    if "error" in result:
        await interaction.followup.send(f"Error: {result['error']}")
        return
    await interaction.followup.send(
        f"Pushed `{result['pushed']}` to origin on `{project_id}`"
    )
```

### 4f. `/git-merge`

```python
@bot.tree.command(name="git-merge", description="Merge a branch into the default branch")
@app_commands.describe(
    project_id="Project ID",
    branch="Branch to merge",
    target_branch="Target branch to merge into (defaults to repo's default branch)",
    repo_id="Repo ID (optional)",
)
async def git_merge_command(
    interaction: discord.Interaction,
    project_id: str,
    branch: str,
    target_branch: str | None = None,
    repo_id: str | None = None,
):
    await interaction.response.defer()
    args = {"project_id": project_id, "branch": branch}
    if target_branch:
        args["target_branch"] = target_branch
    if repo_id:
        args["repo_id"] = repo_id
    result = await handler.execute("git_merge", args)
    if "error" in result:
        await interaction.followup.send(f"Error: {result['error']}")
        return

    if result.get("merged") is False:
        await interaction.followup.send(
            f"Merge conflict — merge of `{branch}` into `{result['into']}` was aborted"
        )
    else:
        await interaction.followup.send(
            f"Merged `{result['merged']}` into `{result['into']}` on `{project_id}`"
        )
```

### 4g. `/git-diff`

```python
@bot.tree.command(name="git-diff", description="Show diff against a base branch")
@app_commands.describe(
    project_id="Project ID",
    base_branch="Base branch to diff against (defaults to repo's default branch)",
    repo_id="Repo ID (optional)",
)
async def git_diff_command(
    interaction: discord.Interaction,
    project_id: str,
    base_branch: str | None = None,
    repo_id: str | None = None,
):
    await interaction.response.defer()
    args = {"project_id": project_id}
    if base_branch:
        args["base_branch"] = base_branch
    if repo_id:
        args["repo_id"] = repo_id
    result = await handler.execute("git_diff", args)
    if "error" in result:
        await interaction.followup.send(f"Error: {result['error']}")
        return

    branch = result.get("branch", "?")
    base = result.get("base_branch", "main")
    diff = result.get("diff", "(no changes)")
    file_count = result.get("file_count", 0)
    header = f"**Branch:** `{branch}` vs `{base}` — {file_count} file(s) changed\n"

    if len(diff) > 1800:
        file = discord.File(
            fp=io.BytesIO(diff.encode("utf-8")),
            filename=f"diff-{project_id}.patch",
        )
        await interaction.followup.send(
            f"{header}*Diff attached ({len(diff):,} chars)*",
            file=file,
        )
    else:
        await _send_long(
            interaction,
            f"{header}```diff\n{diff}\n```",
            followup=True,
        )
```

---

## Step 5: Refactor Existing `_cmd_get_git_status` to Use Helper

### Files Modified
- `src/command_handler.py`

Optionally refactor the existing `_cmd_get_git_status` method to use the new `_resolve_repo_path` helper for its single-repo cases, while keeping its multi-repo iteration logic for the full status view. This is a minor cleanup — the existing code works fine, but using the shared helper would reduce duplication in the single-repo fallback path.

This step is **optional** and can be skipped if it adds unnecessary risk.

---

## Summary of All File Changes

| File | Changes |
|------|---------|
| `src/git/manager.py` | Add `checkout_branch()` and `list_branches()` methods (~12 lines) |
| `src/command_handler.py` | Add `_resolve_repo_path()` helper + 7 new `_cmd_*` methods (~180 lines) |
| `src/chat_agent.py` | Add 7 new entries to `TOOLS` list + update system prompt capabilities (~100 lines) |
| `src/discord/commands.py` | Add 7 new `/git-*` slash commands (~230 lines) |

**Total estimated new code: ~520 lines across 4 files.**

## Implementation Order

The steps should be implemented in order (1→2→3→4) since each layer depends on the previous:
1. GitManager methods (foundation)
2. CommandHandler `_cmd_*` methods (business logic, uses GitManager)
3. Chat agent TOOLS entries (references _cmd_* names)
4. Discord slash commands (calls handler.execute which calls _cmd_*)

Each step can be implemented as a separate task/PR, or all together in a single change.

## Error Handling Notes

- All `_cmd_*` methods wrap GitManager calls in try/except for `GitError`, returning `{"error": str(e)}` on failure
- The `_resolve_repo_path` helper handles all project/repo validation centrally
- Discord commands check for `"error"` in result and display user-friendly messages
- GitManager's `_run()` already raises `GitError` with the stderr message on any non-zero exit code
- Merge conflicts are handled gracefully (auto-abort, return status) rather than raising errors

## Testing Notes

- Each `_cmd_*` method can be unit-tested by mocking `self.db` and `self.orchestrator.git`
- Discord slash commands are thin wrappers and don't need separate testing beyond integration
- Manual testing via Discord: create a test project with a linked repo, then exercise each command
- Edge cases to test: no repos on project, invalid repo_id, worktree paths, merge conflicts, empty diffs, nothing-to-commit
