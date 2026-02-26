# Workspace Sync: Keeping Agent Workspaces in Sync

## Problem Statement

When multiple agents work concurrently on the same repository (each in their own
workspace clone), their local copies of `main` can drift out of sync with `origin/main`.
This leads to merge conflicts, failed pushes, and stale code. The current system has
several gaps in its git sync workflow that need to be addressed.

## Current State Analysis

### What works well today
- **Workspace isolation**: Each `(agent, project)` pair gets its own cloned directory
- **Branch-per-task**: `prepare_for_task()` creates fresh branches off `default_branch`
- **Pre-task fetch**: `prepare_for_task()` does `git fetch origin` + `git pull` on main
  before creating a new task branch
- **Post-completion commit**: `_complete_workspace()` always commits agent work
- **PR and auto-merge workflows**: Both paths (PR creation, direct merge+push) exist

### Gaps identified

1. **`_merge_and_push()` never pulls main before merging** — it does
   `checkout main → merge branch → push main`, but skips pulling remote changes
   first. If another agent pushed to main since the last pull, the push fails with
   a non-fast-forward error. The failure is notified but not recovered from.

2. **Push failures leave workspace in a dirty state** — after a failed push, the
   local `main` has the merge commit but `origin/main` doesn't. Subsequent tasks
   from the same agent start from this diverged state.

3. **No merge conflict recovery strategy** — when `merge_branch()` detects conflicts
   it aborts the merge and notifies, but there's no automated attempt to rebase onto
   the latest main or retry.

4. **Retried tasks don't rebase onto latest main** — when a task retries (branch
   already exists), `prepare_for_task()` just checks out the existing branch without
   rebasing it onto the latest `origin/main`. The agent works on stale code.

5. **No pre-push pull for PR branches** — `push_branch` could fail on retry if the
   branch was previously pushed. Should use `--force-with-lease` or similar.

6. **Subtask chains accumulate drift** — plan subtasks share a branch and commit
   sequentially. Over a long chain, the branch drifts far from main, increasing
   merge conflict risk at the final merge.

7. **LINK repos with shared filesystem** — multiple agents on the same LINK repo
   without worktrees can clobber each other's branch state (no file-level locking).

## Design Decisions

- **Rebase over merge for sync**: Task branches should be rebased onto the latest
  main before merging, to maintain linear history and reduce conflicts.
- **Automatic retry on push failure**: If push fails (non-fast-forward), pull latest
  and retry once before giving up.
- **Hard reset local main**: Local `main` should always be a faithful mirror of
  `origin/main` — use `git reset --hard origin/main` rather than merge/pull to avoid
  divergence.
- **Agent instructions**: Add "pull from main before starting" to the system context
  injected into agents, so agents themselves also stay in sync.

---

## Phase 1: Harden pre-task workspace sync (`prepare_for_task` improvements)

**Files**: `src/git/manager.py`

Modify `prepare_for_task()` (normal clone path) to use a hard reset instead of pull:

```python
# Current (can diverge if local main has un-pushed merge commits):
self._run(["checkout", default_branch], cwd=checkout_path)
self._run(["pull", "origin", default_branch], cwd=checkout_path)  # may fail

# New (always matches remote):
self._run(["checkout", default_branch], cwd=checkout_path)
self._run(["reset", "--hard", f"origin/{default_branch}"], cwd=checkout_path)
```

This ensures that even if a previous `_merge_and_push` left local main diverged
from origin, the next task starts clean.

For **retried tasks** where the branch already exists, add a rebase step:

```python
# After switching to existing branch:
self._run(["checkout", branch_name], cwd=checkout_path)
try:
    self._run(["rebase", f"origin/{default_branch}"], cwd=checkout_path)
except GitError:
    self._run(["rebase", "--abort"], cwd=checkout_path)
    # Branch stays as-is; agent can work with it
```

Also add a new `pull_latest_main()` convenience method that encapsulates the
fetch + hard-reset pattern for reuse.

Update `switch_to_branch()` to also rebase onto `origin/<default_branch>` after
switching, so subtask chains stay closer to main.

**Tests**: Add tests for the hard-reset path and rebase-on-retry behavior in
`tests/test_git_manager.py`.

## Phase 2: Fix merge-and-push to pull before merging

**Files**: `src/git/manager.py`, `src/orchestrator.py`

### 2a. Add a `sync_and_merge()` method to GitManager

Create a new higher-level method that encapsulates the full sync-merge-push flow:

```python
def sync_and_merge(self, checkout_path, branch_name, default_branch="main",
                   max_retries=1) -> tuple[bool, str]:
    """Pull latest main, merge branch, push. Returns (success, error_msg)."""
    # 1. Fetch latest
    self._run(["fetch", "origin"], cwd=checkout_path)

    # 2. Checkout and hard-reset main to origin
    self._run(["checkout", default_branch], cwd=checkout_path)
    self._run(["reset", "--hard", f"origin/{default_branch}"], cwd=checkout_path)

    # 3. Attempt merge
    try:
        self._run(["merge", branch_name], cwd=checkout_path)
    except GitError:
        self._run(["merge", "--abort"], cwd=checkout_path)
        return (False, "merge_conflict")

    # 4. Push with retry
    for attempt in range(max_retries + 1):
        try:
            self._run(["push", "origin", default_branch], cwd=checkout_path)
            return (True, "")
        except GitError as e:
            if attempt < max_retries:
                # Pull and retry
                self._run(["pull", "--rebase", "origin", default_branch],
                          cwd=checkout_path)
            else:
                return (False, f"push_failed: {e}")

    return (False, "push_failed_exhausted")
```

### 2b. Update `_merge_and_push()` in orchestrator

Replace the current `merge_branch` + `push_branch` calls with the new
`sync_and_merge()` method. Handle the structured return value to provide
better notifications:

- On `merge_conflict`: Notify with conflict details and suggest manual resolution
- On `push_failed`: Notify that push failed after retries, workspace may be diverged
- On success: Clean up branch as before

### 2c. Workspace recovery on failure

After a failed merge-and-push, reset local main back to origin/main so the
workspace is clean for the next task:

```python
if not success:
    try:
        self.git._run(["checkout", repo.default_branch], cwd=workspace)
        self.git._run(["reset", "--hard", f"origin/{repo.default_branch}"],
                      cwd=workspace)
    except Exception:
        pass  # best-effort recovery
```

**Tests**: Add integration tests with two "agent" clones pushing concurrently to
verify the retry logic works correctly.

## Phase 3: Conflict resolution via rebase-before-merge

**Files**: `src/git/manager.py`, `src/orchestrator.py`

When a direct merge fails with conflicts, attempt a rebase of the task branch
onto the latest main before giving up:

### 3a. Add `rebase_onto()` method to GitManager

```python
def rebase_onto(self, checkout_path, branch_name, target_branch="main") -> bool:
    """Rebase branch onto target. Returns True on success, False on conflict."""
    self._run(["checkout", branch_name], cwd=checkout_path)
    try:
        self._run(["rebase", f"origin/{target_branch}"], cwd=checkout_path)
        return True
    except GitError:
        self._run(["rebase", "--abort"], cwd=checkout_path)
        return False
```

### 3b. Update sync_and_merge to try rebase on conflict

When the merge in `sync_and_merge()` fails:
1. Checkout the task branch
2. Try rebasing onto `origin/<default_branch>`
3. If rebase succeeds, retry the merge (which should now be fast-forward)
4. If rebase also fails, give up with a conflict notification

This gives the system two chances to resolve: direct merge, then rebase.

**Tests**: Test with intentional conflicting changes between two branches.

## Phase 4: Update agent system context instructions

**Files**: `src/orchestrator.py`

Add instructions to the system context injected into agents so they also
participate in keeping their workspace in sync:

For **both subtask and root task prompts**, add after the committing instructions:

```
## Important: Keeping Your Workspace in Sync
Before starting work, pull the latest changes from the main branch:
1. `git fetch origin`
2. `git rebase origin/main` (if on a task branch)
This ensures you're working with the latest code and reduces merge conflicts.
If a rebase has conflicts you cannot resolve, proceed with your work anyway —
the system will handle conflicts during the merge phase.
```

This is a safety net — the orchestrator handles sync at the system level, but
having agents also pull reduces the chance of stale-code issues for long-running tasks.

## Phase 5: Add force-with-lease for PR branch pushes

**Files**: `src/git/manager.py`, `src/orchestrator.py`

When pushing a task branch for PR creation (not merging to main), use
`--force-with-lease` to handle cases where:
- A task was retried and the branch was previously pushed
- A subtask chain pushed intermediate results

```python
def push_branch(self, checkout_path, branch_name, *, force_with_lease=False):
    args = ["push", "origin", branch_name]
    if force_with_lease:
        args.insert(2, "--force-with-lease")
    self._run(args, cwd=checkout_path)
```

Update `_create_pr_for_task()` to use `force_with_lease=True` since task branches
are owned by the agent and safe to force-push.

**Tests**: Test push with `force_with_lease` on a previously-pushed branch.

## Phase 6: Subtask chain sync (optional mid-chain rebase)

**Files**: `src/git/manager.py`, `src/orchestrator.py`

For long subtask chains, add an optional mid-chain rebase to reduce drift:

### 6a. Add config option

In `config.py`, add to `auto_task` section:
```python
rebase_between_subtasks: bool = False  # Rebase onto main between subtasks
```

### 6b. Implement in `switch_to_branch()`

When `rebase_between_subtasks` is enabled and `switch_to_branch()` is called
for a subtask:

```python
def switch_to_branch(self, checkout_path, branch_name,
                     default_branch="main", rebase=False):
    # ... existing fetch + checkout logic ...
    if rebase:
        try:
            self._run(["rebase", f"origin/{default_branch}"], cwd=checkout_path)
        except GitError:
            self._run(["rebase", "--abort"], cwd=checkout_path)
            # Continue without rebase — drift is acceptable
```

### 6c. Wire into `_prepare_workspace()`

Pass the config flag through when calling `switch_to_branch` for subtasks:

```python
if reuse_branch:
    self.git.switch_to_branch(
        workspace, branch_name,
        default_branch=repo.default_branch,
        rebase=self.config.auto_task.rebase_between_subtasks,
    )
```

**Tests**: Test subtask chain with and without mid-chain rebase.

## Phase 7: Spec and test updates

**Files**: `specs/git/git.md`, `specs/orchestrator.md`, `tests/test_git_manager.py`,
`tests/test_orchestrator.py` (new or expanded)

- Update the git spec with the new methods (`sync_and_merge`, `rebase_onto`,
  `pull_latest_main`) and the changed behavior of existing methods
- Update the orchestrator spec to document the new sync workflow in
  `_merge_and_push()` and the agent instruction additions
- Add comprehensive tests covering:
  - Concurrent push race conditions
  - Merge conflict detection and rebase recovery
  - Workspace reset after failed merge-and-push
  - Retried task branch rebase
  - `force-with-lease` push behavior
  - Subtask chain drift and mid-chain rebase
