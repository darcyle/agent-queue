# Git Sync for Agent Workspaces — Identified Gaps

**Source files:** `src/git/manager.py`, `src/orchestrator.py`
**Companion doc:** [Current State](git-sync-current-state.md)

This document catalogues the gaps in the current git sync workflow that can
cause merge conflicts, failed pushes, and stale code when multiple agents work
concurrently on the same repository. Each gap includes the relevant code
locations, a description of the failure mode, and the impact on the system.

---

## Gap 1 — `_merge_and_push()` Never Pulls Main Before Merging

**Files:** `src/orchestrator.py` (`_merge_and_push`, line 866),
`src/git/manager.py` (`merge_branch`, line 162)

### What happens today

```
_merge_and_push()
  └─ merge_branch()
       ├─ git checkout main
       ├─ git merge <task-branch>     ← merges into *local* main
       └─ (no fetch or pull of origin/main)
  └─ push_branch(main)               ← may fail with non-fast-forward
```

`merge_branch()` checks out the local `main` and merges the task branch into
it, then `_merge_and_push()` pushes `main` to origin. There is no
`git fetch origin` or `git pull origin main` before the merge.

### Failure mode

If another agent (or a human) pushed to `origin/main` since this workspace last
pulled, the local `main` is behind the remote. The merge succeeds locally but
the subsequent push fails with a **non-fast-forward** error:

```
! [rejected]  main -> main (non-fast-forward)
```

The user is notified, but the merge is **not** reverted.

### Impact

- **Frequency:** Increases linearly with the number of concurrent agents.
  With 3+ agents completing tasks within minutes of each other, this is
  virtually guaranteed.
- **Severity:** High — the push failure blocks code from reaching the remote,
  and leaves the workspace in a diverged state (see Gap 2).

---

## Gap 2 — Push Failures Leave Workspace in a Dirty State

**File:** `src/orchestrator.py` (`_merge_and_push`, lines 877–884)

### What happens today

```python
if repo.source_type == RepoSourceType.CLONE:
    try:
        self.git.push_branch(workspace, repo.default_branch)
    except Exception as e:
        await self._notify_channel(...)
        # ← local main still has the merge commit; no rollback
```

When the push to `origin/main` fails, the error is caught and the user is
notified, but the local `main` branch retains the merge commit that could not
be pushed. No `git reset` or rollback is performed.

### Failure mode

The next task assigned to this agent inherits a local `main` that has diverged
from `origin/main`. `prepare_for_task()` will `git pull origin main` at the
start, but this pull itself may fail or produce a merge commit, compounding the
divergence. Subsequent merges and pushes from this workspace become
increasingly likely to fail.

### Impact

- **Cascading:** One push failure poisons the workspace for all future tasks.
- **Hard to diagnose:** The user sees repeated push failures from the same
  agent without an obvious root cause.
- **Recovery:** Currently requires manual `git reset --hard origin/main` in
  the workspace.

---

## Gap 3 — No Merge Conflict Recovery Strategy

**Files:** `src/git/manager.py` (`merge_branch`, lines 162–173),
`src/orchestrator.py` (`_merge_and_push`, lines 868–875)

### What happens today

```python
def merge_branch(self, checkout_path, branch_name, default_branch="main"):
    self._run(["checkout", default_branch], cwd=checkout_path)
    try:
        self._run(["merge", branch_name], cwd=checkout_path)
        return True
    except GitError:
        self._run(["merge", "--abort"], cwd=checkout_path)
        return False  # ← caller notifies user, no retry
```

When a merge conflict is detected, the merge is aborted and the user is
notified with a "manual resolution needed" message. There is no automated
attempt to rebase the task branch onto the latest main, no retry loop, and no
escalation path beyond the single notification.

### Failure mode

Many merge conflicts are **trivially resolvable** — for example, when two
agents edited different files but both touched a lockfile or auto-generated
file. A rebase onto updated `main` would resolve these without human
intervention. Instead, the task's code changes are stranded on an unmerged
branch until a human acts.

### Impact

- **Operational burden:** Every merge conflict requires manual intervention,
  even when the conflict is trivial.
- **Delayed delivery:** Code that is otherwise correct sits unmerged until a
  human notices the notification and resolves the conflict.
- **Scales poorly:** With N agents, the probability of at least one merge
  conflict per cycle grows quickly.

---

## Gap 4 — Retried Tasks Don't Rebase onto Latest Main

**File:** `src/git/manager.py` (`prepare_for_task`, lines 86–128)

### What happens today

```python
# Normal repo path (line 116-128):
try:
    self._run(["checkout", "-b", branch_name], cwd=checkout_path)
except GitError:
    # Branch already exists (e.g. task retried after restart) —
    # switch to it instead of failing.
    self._run(["checkout", branch_name], cwd=checkout_path)

# Worktree path (line 107-115):
try:
    self._run(["checkout", "-b", branch_name, f"origin/{default_branch}"], ...)
except GitError:
    self._run(["checkout", branch_name], cwd=checkout_path)
```

When a task retries and the branch already exists, both code paths fall back to
a bare `git checkout <branch>`. The branch is **not** rebased onto the latest
`origin/main`, even though `git fetch origin` has already been run (line 105).

### Failure mode

A task fails and is retried hours later. In the meantime, other agents have
pushed changes to `main`. The retried task resumes work on a branch that was
forked from a now-stale version of `main`. The agent may:

1. Work with outdated code (e.g. calling a function that was renamed).
2. Produce changes that conflict with work that has landed since.
3. Fail at merge time due to accumulated drift.

### Impact

- **Stale code:** The agent works against an outdated view of the repository.
- **Wasted compute:** The agent may complete work that cannot merge cleanly.
- **Increases with retry delay:** The longer between the original attempt and
  the retry, the more stale the branch becomes.

---

## Gap 5 — No `--force-with-lease` for PR Branch Pushes — **RESOLVED**

**Resolution:** `push_branch()` now accepts a `force_with_lease` keyword argument.
When `True`, `--force-with-lease` is added to the push command.  The orchestrator's
`_create_pr_for_task()` passes `force_with_lease=True` when pushing task branches
for PR creation, making retries idempotent while preventing accidental overwrites
of other people's changes.

**Previously:** `push_branch` used a plain `git push origin <branch>` without
`--force-with-lease`, causing retry failures when the branch had already been
pushed in a previous attempt.

---

## Gap 6 — Subtask Chains Accumulate Drift from Main

**Files:** `src/git/manager.py` (`switch_to_branch`, lines 130–157),
`src/orchestrator.py` (`_prepare_workspace`, line 759)

### What happens today

Plan subtasks share a branch and commit sequentially. Each subtask calls
`switch_to_branch()`, which:

```python
def switch_to_branch(self, checkout_path, branch_name):
    self._run(["fetch", "origin"], cwd=checkout_path)
    self._run(["checkout", branch_name], cwd=checkout_path)
    self._run(["pull", "origin", branch_name], cwd=checkout_path)
    # ← no rebase onto origin/main
```

The branch is fetched and pulled (to pick up the previous subtask's commits),
but it is **never rebased onto `origin/main`**. Over a chain of N subtasks,
the branch drifts further from main with each step.

### Failure mode

A plan with 5–10 subtasks takes 30–60 minutes to complete. During that time,
other agents may push multiple changes to `main`. By the time the final subtask
attempts to merge, the shared branch has diverged significantly from `main`,
causing:

1. Merge conflicts that would not have occurred if the branch had been
   periodically rebased.
2. The agent working with stale dependencies, APIs, or generated code.
3. Larger, harder-to-review diffs at PR time.

### Impact

- **Conflict probability scales with chain length:** Longer plans → more drift
  → higher conflict rate.
- **Late discovery:** Conflicts are only detected at the very end of the chain,
  after all subtasks have completed. All the subtask work may need to be redone.
- **No incremental feedback:** Intermediate subtasks have no signal that their
  base is drifting.

---

## Gap 7 — LINK Repos with Shared Filesystem (No Agent Isolation)

**File:** `src/orchestrator.py` (`_compute_workspace_path`, lines 655–677)

### What happens today

```python
def _compute_workspace_path(self, agent, project_id, repo):
    if repo.source_type == RepoSourceType.LINK:
        return repo.source_path  # ← same path for ALL agents
    ...
```

For LINK repos, every agent is given the same `workspace_path`. Unlike CLONE
repos (which get per-agent directories), LINK repos share a single filesystem
directory. There is no file-level locking, no worktree isolation, and no
mechanism to prevent concurrent access.

### Failure mode

Two agents assigned tasks on the same LINK repo will:

1. **Clobber each other's branch state:** Agent A checks out branch-A, then
   Agent B checks out branch-B. Agent A's working tree now has branch-B's
   files — any subsequent commit by Agent A goes to the wrong branch.
2. **Corrupt the index:** Concurrent `git add -A` + `git commit` operations
   can produce corrupt or mixed commits.
3. **Race on checkout:** `prepare_for_task()` does `checkout main → pull →
   checkout -b <branch>`. If two agents interleave these steps, one may create
   its branch from the other's task branch rather than from main.

### Impact

- **Data loss:** Agent work can be committed to the wrong branch or lost
  entirely.
- **Silent corruption:** The system won't detect that agents are interfering
  with each other — commits simply end up in unexpected places.
- **Workaround exists but isn't used:** `GitManager` already has
  `create_worktree()` / `remove_worktree()` methods, but the workspace
  preparation code never uses them for LINK repos.

---

## Summary of Gaps

| # | Gap | Root Cause | Severity |
|---|-----|-----------|----------|
| 1 | No pull before merge in `_merge_and_push()` | Missing `fetch` + `pull` before `merge_branch()` | **High** |
| 2 | Push failure leaves diverged local main | No rollback after failed push | **High** |
| 3 | No merge conflict recovery | Abort + notify only, no rebase/retry | **Medium** |
| 4 | Retried tasks work on stale branches | No rebase on retry, just `checkout` | **Medium** |
| 5 | ~~PR branch push lacks `--force-with-lease`~~ | ~~Plain `git push` without safety flags~~ | **RESOLVED** |
| 6 | Subtask chains drift from main | No periodic rebase during subtask chain | **Medium** |
| 7 | LINK repos share filesystem across agents | `_compute_workspace_path` returns same path | **High** |

### Priority Assessment

**Must fix for reliable multi-agent operation (Gaps 1, 2, 7):**
These gaps cause outright failures or data corruption in the most common
multi-agent scenario (several agents completing tasks on the same repo within
a short window).

**Should fix for operational efficiency (Gaps 3, 4, 5):**
These gaps cause avoidable manual intervention and wasted agent compute. They
become more painful as the number of agents and task throughput increases.

**Should fix for long-running plans (Gap 6):**
This gap specifically affects plan-based workflows with many subtasks. It's
less urgent if most plans have ≤3 subtasks, but becomes critical for longer
chains.
