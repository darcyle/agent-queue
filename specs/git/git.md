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

### `pull_latest_main(checkout_path, default_branch="main")`

Fetches from origin and hard-resets the default branch to match remote. This is the canonical way to sync a workspace's default branch with `origin`.

1. Runs `git fetch origin` to get the latest remote state.
2. Runs `git checkout <default_branch>` to switch to the default branch.
3. Runs `git reset --hard origin/<default_branch>` to discard any local divergence.

A hard reset (rather than pull) ensures the local default branch always matches the remote even if a previous `_merge_and_push` left it diverged (e.g. un-pushed merge commits). The caller must have the default branch checked out or be prepared for the checkout side-effect.

### `prepare_for_task(checkout_path, branch_name, default_branch="main")`

Full pre-task branch setup. Handles both normal clones and git worktrees differently.

1. Detects whether `checkout_path` is a worktree via `_is_worktree`.
2. Runs `git fetch origin` unconditionally.
3. **Worktree path:**
   - Creates the task branch directly from `origin/<default_branch>` with `git checkout -b <branch_name> origin/<default_branch>`.
   - If that fails (branch already exists — task is being retried), switches to `git checkout <branch_name>` and rebases onto `origin/<default_branch>` so the agent works on the latest code rather than a stale snapshot.
   - If the rebase encounters conflicts, it is aborted silently — the agent works with whatever is on the branch as-is.
   - Avoids checking out the default branch locally because it may already be checked out in the main working tree, which git forbids.
4. **Normal clone path:**
   - Checks out `default_branch` locally.
   - Hard-resets to `origin/<default_branch>` to discard any stale local state (e.g. un-pushed merge commits from a prior task). This replaces the previous `git pull` approach.
   - Creates the task branch with `git checkout -b <branch_name>`.
   - If that fails (task is being retried after a restart), switches to `git checkout <branch_name>` and rebases onto `origin/<default_branch>` so the agent starts from the latest code.
   - If the rebase encounters conflicts, it is aborted silently — the agent works with whatever is on the branch as-is.

### `switch_to_branch(checkout_path, branch_name, default_branch="main", rebase=False)`

Switches to an existing branch and pulls the latest remote state. Used for subtask branch reuse: when a plan generates multiple subtasks that share a branch, this lets the next task pick up where the previous one left off.

1. Attempts `git fetch origin`. Silently ignores `GitError` (no remote configured).
2. Attempts `git checkout <branch_name>`.
   - If that fails (branch exists only on the remote), tries `git checkout -b <branch_name> origin/<branch_name>` to create a local tracking branch.
   - If that also fails (no remote branch either, e.g. LINK repos), creates a fresh local branch with `git checkout -b <branch_name>`.
3. Attempts `git pull origin <branch_name>`. Silently ignores `GitError` (no upstream tracking).
4. **Rebase (when `rebase=True`):** Rebases the branch onto `origin/<default_branch>` so subtask chains stay close to main and conflicts are discovered early rather than at final merge time. If the rebase encounters conflicts, it is aborted silently — the agent works with the branch as-is and conflicts will be handled at merge time.

The `rebase` parameter is controlled by `config.auto_task.rebase_between_subtasks` and is passed by `_prepare_workspace()` when switching to a shared subtask branch.

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

### `push_branch(checkout_path, branch_name, *, force_with_lease=False)`

Pushes a local branch to the `origin` remote.

- Constructs the command as `git push origin <branch_name>`.
- When `force_with_lease=True`, inserts `--force-with-lease` into the command: `git push origin --force-with-lease <branch_name>`. This allows the push to succeed even when the remote branch has been previously pushed (e.g. task retries or subtask chains that push intermediate results). It is safe for task branches because they are owned by a single agent and are never concurrently updated by others.
- The `force_with_lease` parameter is keyword-only to prevent accidental positional use.
- Raises `GitError` on failure (e.g. non-fast-forward without the flag, authentication error, or remote ref updated by another clone when using `--force-with-lease`).

### `merge_branch(checkout_path, branch_name, default_branch="main")`

Merges a feature branch into the default branch. Returns `True` on success, `False` on conflict.

1. Attempts `git fetch origin`. Silently ignores `GitError` (no remote configured, e.g. LINK repos).
2. Checks out `default_branch`.
3. Attempts `git reset --hard origin/<default_branch>` to sync with the latest remote state. Silently ignores `GitError` (no remote tracking branch — uses local state as-is).
4. Attempts `git merge <branch_name>`.
5. If the merge raises `GitError` (conflict), runs `git merge --abort` to restore the working tree and returns `False`.
6. Returns `True` on a clean merge.

The fetch-and-hard-reset step (added for workspace sync) ensures that concurrent agents always merge against the latest remote state, preventing stale-main problems. For repos without a remote (LINK repos), the method falls through to merge against whatever local state is available.

### `rebase_onto(checkout_path, branch_name, onto="main")`

Rebases a task branch onto a target branch. Returns `True` if the rebase succeeded, `False` if it was aborted due to conflicts.

1. Records the currently checked-out branch.
2. Checks out `branch_name`.
3. Runs `git rebase <onto>`.
4. On conflict (`GitError`): runs `git rebase --abort` to restore the branch, then attempts to return to the original branch. Returns `False`.
5. On success: attempts to return to the original branch. Returns `True`.

The caller is responsible for ensuring `onto` is up-to-date (e.g. via a prior fetch + hard-reset). Used as a fallback in `sync_and_merge` when a direct merge fails — rebasing the task branch onto the latest default branch resolves drift-related conflicts before retrying the merge.

### `mid_chain_rebase(checkout_path, branch_name, default_branch="main", *, push=False)`

Rebases a task branch onto the latest `origin/<default_branch>` between subtask completions. Designed for long subtask chains to catch conflicts early and keep the branch close to main, preventing large conflict surfaces at final merge time.

Steps:
1. `git fetch origin` to get the latest remote state. Returns `False` if fetch fails (no remote).
2. `git checkout <branch_name>` to ensure we're on the task branch. Returns `False` if checkout fails.
3. `git rebase origin/<default_branch>` to replay task commits on top of the latest main.
4. If rebase conflicts: runs `git rebase --abort` and returns `False`. The branch is left unchanged.
5. If `push=True` and rebase succeeded: pushes the rebased branch with `--force-with-lease` via `push_branch(force_with_lease=True)`. Push failures are silently ignored (rebase still succeeded).

The `push` parameter is keyword-only. When enabled, intermediate progress is backed up on the remote.

Returns `True` if the rebase (and optional push) succeeded, `False` if the rebase had conflicts or the fetch/checkout failed.

### `sync_and_merge(checkout_path, branch_name, default_branch="main", max_retries=1)`

Comprehensive sync-merge-push flow used by the orchestrator after an agent finishes work on a task branch. Encapsulates the full workflow for CLONE repos:

1. **Fetch:** `git fetch origin` to get the latest remote state.
2. **Reset:** Checkout `default_branch` and `git reset --hard origin/<default_branch>` — discards any stale local state (e.g. un-pushed merge commits from a prior attempt).
3. **Merge:** Attempt `git merge <branch_name>`.
   - On conflict: run `git merge --abort`.
   - **Rebase fallback:** Call `rebase_onto(branch_name, "origin/<default_branch>")` to replay the task branch's commits on top of the latest main. If the rebase succeeds, re-checkout `default_branch` and retry the merge. If either the rebase or the retry merge fails, return `(False, "merge_conflict")`.
4. **Push with retry:** Attempt `git push origin <default_branch>` up to `max_retries + 1` times total.
   - On push rejection (another agent pushed between our fetch and push): run `git pull --rebase origin <default_branch>` to incorporate the new commits, then retry.
   - If all attempts fail: return `(False, "push_failed: <details>")`.
5. On success: return `(True, "")`.

Returns a `(success: bool, error_msg: str)` tuple. Error messages are one of:
- `""` — success
- `"merge_conflict"` — the task branch conflicts with the default branch (even after rebase attempt)
- `"push_failed: <details>"` — all push attempts were rejected

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
