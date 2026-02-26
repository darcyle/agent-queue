# Git Manager Specification

**Source:** `src/git/manager.py`

## 1. Overview

`GitManager` is a thin synchronous wrapper around the `git` CLI and the `gh` (GitHub CLI) tool. All git operations are executed as subprocesses via `subprocess.run`. There are no direct calls to any git library (e.g. GitPython or libgit2).

- Standard repository operations (clone, branch, commit, push, merge, worktree) use `git` subcommands.
- GitHub-specific operations (creating pull requests, checking PR status) use the `gh` CLI and are therefore only available in environments where `gh` is installed and authenticated.
- The class is instantiated with no arguments and holds no state. All methods accept an explicit `checkout_path` (or `source_path`) to identify which repository to operate on.

## Source Files
- `src/git/manager.py`

---

## 2. Error Handling

### `GitError`

`GitError` is the single exception type raised by this module. It inherits directly from `Exception`.

Any `git` subprocess that exits with a non-zero return code causes `_run` to raise:

```
GitError("git <args> failed: <stderr>")
```

`gh` subcommands (`create_pr`, `check_pr_merged`) are not routed through `_run`. They call `subprocess.run` directly and raise `GitError` manually when `returncode != 0`.

### `_run` (internal)

```python
def _run(self, args: list[str], cwd: str | None = None) -> str
```

Executes `["git"] + args` in the given working directory. Captures both stdout and stderr. On success returns `stdout.strip()`. On failure raises `GitError` with the stderr content.

Several public methods intentionally catch `GitError` and suppress it (e.g. a failed `pull` that has no upstream tracking is silently ignored). Each such suppression is documented in the relevant method section below.

---

## 3. Repository Setup

### `create_checkout(repo_url, checkout_path)`

Clones a remote repository to a local path.

- Creates all intermediate parent directories with `os.makedirs(..., exist_ok=True)`.
- Runs `git clone <repo_url> <checkout_path>`.
- Raises `GitError` on clone failure.

### `validate_checkout(checkout_path)`

Returns `True` if `checkout_path` is a valid git repository, `False` otherwise.

- Returns `False` immediately if the path is not an existing directory.
- Runs `git rev-parse --git-dir` inside the directory. Returns `True` if it succeeds, `False` if it raises `GitError`.
- Does not raise exceptions; always returns a boolean.

### `init_repo(path)`

Initializes a brand-new git repository with an empty initial commit.

- Creates the directory with `os.makedirs(path, exist_ok=True)`.
- Runs `git init`.
- Runs `git commit --allow-empty -m "Initial commit"` to create a HEAD reference immediately (avoids detached-HEAD edge cases on first branch creation).
- Raises `GitError` on failure.

---

## 4. Branching

### `create_branch(checkout_path, branch_name)`

Creates a new branch and checks it out.

- Attempts `git checkout -b <branch_name>`.
- If that fails (branch already exists), falls back to `git checkout <branch_name>`.
- Does not update the branch from the remote; use `prepare_for_task` for a full pre-task setup.

### `checkout_branch(checkout_path, branch_name)`

Switches to an existing local branch.

- Runs `git checkout <branch_name>`.
- Raises `GitError` if the branch does not exist locally.

### `list_branches(checkout_path)`

Returns all local branch names as a list of strings.

- Runs `git branch --list`.
- Each entry in the returned list has leading/trailing whitespace stripped. The current branch retains the `*` prefix that git adds (e.g. `"* main"`).
- Returns an empty list on `GitError` rather than raising.

### `prepare_for_task(checkout_path, branch_name, default_branch="main")`

Full pre-task branch setup. Handles both normal clones and git worktrees differently.

1. Detects whether `checkout_path` is a worktree via `_is_worktree`.
2. Runs `git fetch origin` unconditionally.
3. **Worktree path:**
   - Creates the task branch directly from `origin/<default_branch>` with `git checkout -b <branch_name> origin/<default_branch>`.
   - If that fails (branch already exists), falls back to `git checkout <branch_name>`.
   - Avoids checking out the default branch locally because it may already be checked out in the main working tree, which git forbids.
4. **Normal clone path:**
   - Checks out `default_branch` locally.
   - Attempts `git pull origin <default_branch>`. Silently ignores `GitError` (e.g. no upstream tracking configured).
   - Creates the task branch with `git checkout -b <branch_name>`.
   - If that fails (task is being retried after a restart), falls back to `git checkout <branch_name>`.

### `switch_to_branch(checkout_path, branch_name)`

Switches to a branch and pulls the latest remote state.

1. Attempts `git fetch origin`. Silently ignores `GitError` (no remote configured).
2. Attempts `git checkout <branch_name>`.
   - If that fails (branch exists only on the remote), tries `git checkout -b <branch_name> origin/<branch_name>` to create a local tracking branch.
3. Attempts `git pull origin <branch_name>`. Silently ignores `GitError` (no upstream tracking).

### `delete_branch(checkout_path, branch_name, *, delete_remote=True)`

Deletes a branch locally and optionally on the remote.

1. Attempts `git branch -d <branch_name>` (safe delete — only if fully merged).
2. If that fails, attempts `git branch -D <branch_name>` (force delete — handles squash-merged PRs).
3. If both local deletes fail, silently suppresses the error (branch may not exist locally).
4. If `delete_remote=True`, runs `git push origin --delete <branch_name>`. Silently ignores `GitError` (branch may not exist on the remote).

---

## 5. Worktrees

Git worktrees allow multiple working trees to share a single `.git` directory. This is used for agent isolation when multiple agents operate on the same repository simultaneously.

### `_is_worktree(checkout_path)` (internal)

Returns `True` if `checkout_path` is a linked worktree (not the main working tree).

- Runs `git rev-parse --git-dir`.
- In a linked worktree the git-dir resolves to `.git/worktrees/<name>`, so the string `"worktrees"` appears in the output.
- In the main working tree the git-dir is simply `.git`.
- Returns `False` on `GitError`.

### `create_worktree(source_path, worktree_path, branch)`

Creates a new linked worktree with a new branch.

- Creates all intermediate parent directories for `worktree_path` with `os.makedirs`.
- Runs `git worktree add -b <branch> <worktree_path>` from `source_path`.
- The new branch is created at the current HEAD of `source_path`.
- Raises `GitError` on failure.

### `remove_worktree(source_path, worktree_path)`

Removes a linked worktree.

- Attempts `git worktree remove <worktree_path>` from `source_path`.
- If that fails (e.g. the worktree has untracked or modified files), retries with `git worktree remove --force <worktree_path>`.
- Raises `GitError` only if the force removal also fails.

---

## 6. Committing and Pushing

### `commit_all(checkout_path, message)`

Stages all changes and creates a commit. Returns `True` if a commit was made, `False` if the working tree was clean.

1. Runs `git add -A` to stage all tracked and untracked changes.
2. Runs `git diff --cached --quiet` directly via `subprocess.run` (bypassing `_run`) to check whether anything is staged. Exit code `0` means nothing staged.
3. If nothing is staged, returns `False` without creating a commit.
4. Otherwise runs `git commit -m <message>` and returns `True`.
5. Raises `GitError` if the commit fails.

### `push_branch(checkout_path, branch_name)`

Pushes a local branch to the `origin` remote.

- Runs `git push origin <branch_name>`.
- Raises `GitError` on failure (e.g. non-fast-forward, authentication error).

### `merge_branch(checkout_path, branch_name, default_branch="main")`

Merges a feature branch into the default branch. Returns `True` on success, `False` on conflict.

1. Checks out `default_branch`.
2. Attempts `git merge <branch_name>`.
3. If the merge raises `GitError` (conflict), runs `git merge --abort` to restore the working tree and returns `False`.
4. Returns `True` on a clean merge.

---

## 7. GitHub PR Operations

These methods use the `gh` CLI rather than `git`. They require `gh` to be installed and authenticated with appropriate repository access. Both methods call `subprocess.run` directly and raise `GitError` manually on non-zero exit codes.

### `create_pr(checkout_path, branch, title, body, base="main")`

Creates a GitHub pull request. Returns the PR URL as a string.

- Runs: `gh pr create --title <title> --body <body> --base <base> --head <branch>`
- The command is executed with `cwd=checkout_path` so that `gh` can determine the correct GitHub repository from the local git config.
- Returns `stdout.strip()` which is the PR URL (e.g. `https://github.com/owner/repo/pull/123`).
- Raises `GitError` if `gh` exits with a non-zero code.

### `check_pr_merged(checkout_path, pr_url)`

Polls the state of a pull request. Returns one of three values:

| Return value | Meaning |
|---|---|
| `True` | PR has been merged |
| `False` | PR is still open |
| `None` | PR was closed without merging |

- Runs: `gh pr view <pr_url> --json state,mergedAt`
- Parses the JSON response. Checks `state` (uppercased) and `mergedAt`.
- `True` if `state == "MERGED"` or `mergedAt` is non-null.
- `False` if `state == "OPEN"`.
- `None` for all other states (e.g. `"CLOSED"`).
- Raises `GitError` if `gh` exits with a non-zero code.

---

## 8. Inspection

All inspection methods return an empty string or empty list (never raise) when the underlying `git` command fails.

### `get_diff(checkout_path, base_branch="main")`

Returns the full unified diff of the working tree against `base_branch`.

- Runs `git diff <base_branch>`.
- Returns the raw diff string, or `""` on `GitError`.

### `get_changed_files(checkout_path, base_branch="main")`

Returns a list of file paths changed relative to `base_branch`.

- Runs `git diff --name-only <base_branch>`.
- Splits on newlines. Returns `[]` if output is empty or on `GitError`.

### `get_status(checkout_path)`

Returns the output of `git status` as a string.

- Returns `""` on `GitError`.

### `get_current_branch(checkout_path)`

Returns the name of the currently checked-out branch.

- Runs `git rev-parse --abbrev-ref HEAD`.
- Returns `""` on `GitError` (e.g. repository has no commits yet).

### `get_recent_commits(checkout_path, count=5)`

Returns the last `count` commits in one-line format.

- Runs `git log --oneline -<count>`.
- Returns `""` on `GitError`.
- Default `count` is `5`.

---

## 9. Utilities

Both utility methods are `@staticmethod` and do not require a `GitManager` instance.

### `slugify(text)`

Converts arbitrary text into a string safe for use in a branch name.

Transformation steps applied in order:

1. Lowercase and strip leading/trailing whitespace.
2. Remove all characters that are not word characters (`\w`), whitespace, or hyphens.
3. Replace whitespace and underscores (`[\s_]+`) with a single hyphen.
4. Collapse consecutive hyphens (`-+`) into one.
5. Strip leading and trailing hyphens.

Examples:
- `"Fix the OAuth2 bug!"` → `"fix-the-oauth2-bug"`
- `"  update_user_profile  "` → `"update-user-profile"`

### `make_branch_name(task_id, title)`

Combines a task ID and a title into a full branch name.

- Format: `"<task_id>/<slugify(title)>"`
- Example: `make_branch_name("clever-fox", "Fix login timeout")` → `"clever-fox/fix-login-timeout"`
- The slash separator creates a namespaced branch, which most git hosts display as a grouped branch hierarchy.

---

## 10. Design Principles — Workspace Sync

These principles govern how `GitManager` and the orchestrator's workspace methods
interact to keep agent workspaces synchronized. They serve as invariants that must
be preserved when modifying the git sync workflow.

See also: `docs/git-sync-current-state.md` for a prose description of current
strengths with rationale.

### P1. Per-Agent Workspace Isolation

Each `(agent, project)` pair receives its own filesystem-level workspace. Two agents
working on the same project never share a working tree. This eliminates file-level
conflicts (dirty index, mixed staged changes) and makes branch operations safe for
concurrent execution.

**Maintained by:** `_prepare_workspace` in the orchestrator, via
`_compute_workspace_path` and the `agent_workspaces` SQLite cache.

### P2. Branch-per-Task Naming

Every task gets a unique branch named `<task-id>/<slugified-title>`. The task ID
prefix makes branches trivially traceable to their originating task. Plan subtasks
reuse the parent task's branch to accumulate commits sequentially.

**Maintained by:** `make_branch_name`, `prepare_for_task`, `switch_to_branch`.

### P3. Fresh Starting Point Before Each Task

Before creating a task branch, `prepare_for_task` fetches the latest remote state
(`git fetch origin`) and synchronizes the local default branch. Agents always start
from a reasonably recent version of the codebase.

**Maintained by:** `prepare_for_task` (fetch + pull/reset on default branch).

### P4. Atomic Post-Completion Commit

After every task, the orchestrator commits all agent work before any merge, push,
or PR operation. The `commit_all` method uses an add-all-then-check-staged pattern
(`git add -A` followed by `git diff --cached --quiet`) to avoid race conditions
between status checks and staging.

**Maintained by:** `commit_all`, called from `_complete_workspace`.

### P5. Graceful Degradation on Git Errors

Git operations that may legitimately fail (no remote configured, no upstream tracking
branch, network errors during fetch) are caught and suppressed. The outer
`_prepare_workspace` wraps all git operations in a catch-all that logs but still
returns a valid workspace path. An agent can always start work even if branch setup
fails.

**Maintained by:** `try/except GitError: pass` patterns in `prepare_for_task`,
`switch_to_branch`, and the catch-all in `_prepare_workspace`.

### P6. Dual Completion Paths (PR vs Direct Merge)

Tasks requiring approval push the branch and create a GitHub PR. Tasks without
approval merge the branch into the default branch and push. Both paths include
error handling with user-facing notifications. The orchestrator polls PR merge
status for approval-gated tasks.

**Maintained by:** `_complete_workspace`, `_merge_and_push`,
`_create_pr_for_task`, `check_pr_merged`.

### P7. Worktree-Aware Branching

When a checkout is a git worktree (not the main working tree), `prepare_for_task`
avoids checking out the default branch locally (which would conflict with the main
working tree) and instead creates the task branch directly from
`origin/<default_branch>`.

**Maintained by:** `_is_worktree`, worktree branch in `prepare_for_task`.

### P8. Retry Resilience

Both `prepare_for_task` and `switch_to_branch` handle existing branches gracefully
(e.g. after a crash or restart mid-task). Instead of failing, they switch to the
existing branch so work can resume without manual cleanup.

**Maintained by:** fallback `checkout` calls in `prepare_for_task`,
`create_branch`, and `switch_to_branch`.

### P9. Best-Effort Branch Cleanup

After successful merge or PR completion, task branches are deleted locally and
remotely. Cleanup failures are silently ignored to avoid blocking the workflow when
a branch was already deleted (e.g. by GitHub's "delete branch after merge" setting).

**Maintained by:** `delete_branch` (called from `_merge_and_push`).
