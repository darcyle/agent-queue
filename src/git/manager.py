"""GitManager -- wraps git CLI commands for the orchestrator's workspace management.

All operations are synchronous subprocess calls.  Git is fast enough for the
operations we need (clone, branch, commit, push) that async would add
complexity without meaningful benefit.

Key workflows:
  - **Clone repos:** ``create_checkout`` clones a project's repository.
  - **Prepare task branches:** ``prepare_for_task`` fetches latest, creates a
    fresh branch off the default branch (handling both normal repos and
    worktrees).
  - **Commit agent work:** ``commit_all`` stages everything and commits if
    there are changes.
  - **Push and PR:** ``push_branch`` pushes to origin; ``create_pr`` and
    ``check_pr_merged`` delegate to the ``gh`` CLI for GitHub PR operations.

Design strengths (see specs/git/git.md §10 for the full list):
  - **Fresh starting point:** ``prepare_for_task`` always fetches remote state
    before creating a task branch, so agents start from recent code.
  - **Worktree-aware:** Detects worktrees and avoids default-branch checkout
    conflicts automatically.
  - **Retry-resilient:** Existing branches are reused on task retry, never
    fail with "branch already exists".
  - **Graceful degradation:** Operations that may legitimately fail (no remote,
    no upstream) are caught and suppressed rather than propagated.
  - **Atomic commits:** ``commit_all`` uses add-then-check-staged to avoid
    race conditions between status checks and staging.

Resolved gaps:
  - **G1 (resolved):** ``merge_branch`` now fetches and hard-resets
    ``origin/<default_branch>`` before merging, and ``_merge_and_push``
    resets local main on push failure to avoid diverged state.
  - **G2 (resolved):** ``recover_workspace`` resets the local default branch
    to ``origin/<default_branch>`` after any failed merge-and-push, ensuring
    the workspace is clean for the next task.
  - **G4 (resolved):** ``prepare_for_task`` now uses hard-reset on the normal
    path and rebases existing branches on retry. ``switch_to_branch`` also
    rebases onto ``origin/<default_branch>`` after switching.

Resolved gaps (continued):
  - **G3 (resolved):** ``sync_and_merge`` now attempts rebase-before-merge
    when a direct merge fails with conflicts.  The task branch is rebased
    onto ``origin/<default_branch>`` and the merge retried.  If the rebase
    itself conflicts, the original ``merge_conflict`` error is returned.

Resolved gaps (continued):
  - **G5 (resolved):** ``push_branch`` now accepts a ``force_with_lease``
    keyword argument.  When ``True``, uses ``--force-with-lease`` for
    idempotent retries of PR branches.  The orchestrator passes this flag
    when pushing task branches for PR creation.

Resolved gaps (continued):
  - **G6 (resolved):** ``mid_chain_sync`` pushes intermediate subtask work
    to the remote and rebases the chain branch onto ``origin/<default_branch>``
    between subtask completions.  The orchestrator calls this after each
    non-final subtask when ``auto_task.rebase_between_subtasks`` is enabled,
    reducing drift and providing crash safety for long chains.

See specs/git/git.md for the full behavioral specification.
"""

from __future__ import annotations

import json
import os
import re
import subprocess


class GitError(Exception):
    pass


class GitManager:
    def _run(self, args: list[str], cwd: str | None = None) -> str:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def create_checkout(self, repo_url: str, checkout_path: str) -> None:
        os.makedirs(os.path.dirname(checkout_path), exist_ok=True)
        self._run(["clone", repo_url, checkout_path])

    def validate_checkout(self, checkout_path: str) -> bool:
        if not os.path.isdir(checkout_path):
            return False
        try:
            self._run(["rev-parse", "--git-dir"], cwd=checkout_path)
            return True
        except GitError:
            return False

    def _is_worktree(self, checkout_path: str) -> bool:
        """Check if the given path is a git worktree (not the main working tree)."""
        try:
            # In a worktree, git-dir points to .git/worktrees/<name>
            # In a normal repo, git-dir is just .git
            git_dir = self._run(["rev-parse", "--git-dir"], cwd=checkout_path)
            return "worktrees" in git_dir
        except GitError:
            return False

    def create_branch(self, checkout_path: str, branch_name: str) -> None:
        try:
            self._run(["checkout", "-b", branch_name], cwd=checkout_path)
        except GitError:
            # Branch already exists — switch to it
            self._run(["checkout", branch_name], cwd=checkout_path)

    def checkout_branch(self, checkout_path: str, branch_name: str) -> None:
        """Switch to an existing branch."""
        self._run(["checkout", branch_name], cwd=checkout_path)

    def list_branches(self, checkout_path: str) -> list[str]:
        """Return a list of local branch names. Current branch is prefixed with '*'."""
        try:
            output = self._run(["branch", "--list"], cwd=checkout_path)
            return [line.strip() for line in output.split("\n") if line.strip()]
        except GitError:
            return []

    def pull_latest_main(
        self, checkout_path: str, default_branch: str = "main",
    ) -> None:
        """Fetch from origin and hard-reset the default branch to match remote.

        Encapsulates the fetch + hard-reset pattern so callers can ensure their
        local default branch exactly matches ``origin/<default_branch>``, even
        if previous merge commits or failed operations left it diverged.

        This is safer than ``git pull`` because pull can fail when the local
        branch has diverged (e.g. from un-pushed merge commits left by
        ``_merge_and_push``). A hard reset unconditionally moves the branch
        pointer to match the remote.

        Must be called while the default branch is checked out (for normal
        repos) or used in worktree-aware callers that skip checkout.
        """
        self._run(["fetch", "origin"], cwd=checkout_path)
        self._run(
            ["reset", "--hard", f"origin/{default_branch}"], cwd=checkout_path
        )

    def _rebase_onto_default(
        self, checkout_path: str, default_branch: str = "main",
    ) -> None:
        """Attempt to rebase the current branch onto ``origin/<default_branch>``.

        If the rebase encounters conflicts, it is aborted and the branch is
        left as-is. The agent can still work with the branch in its current
        state — it just won't have the latest main changes incorporated.
        """
        try:
            self._run(
                ["rebase", f"origin/{default_branch}"], cwd=checkout_path
            )
        except GitError:
            # Conflicts during rebase — abort and leave branch as-is.
            # The agent can still work with the branch; it just won't
            # have the latest main changes incorporated.
            try:
                self._run(["rebase", "--abort"], cwd=checkout_path)
            except GitError:
                pass  # rebase may not be in progress if it failed early

    def prepare_for_task(
        self, checkout_path: str, branch_name: str,
        default_branch: str = "main",
    ) -> None:
        """Fetch latest and create a task branch off the default branch.

        Two code paths depending on whether the checkout is a worktree:
        - **Normal repo:** checkout default branch, hard-reset to
          ``origin/<default_branch>``, then create the task branch. The hard
          reset ensures we always match remote even if a previous
          ``_merge_and_push`` left local main diverged.
        - **Worktree:** Can't checkout the default branch (it's already checked
          out in the main working tree), so we create the task branch directly
          from ``origin/<default_branch>`` in a single step.

        In both cases, if the branch already exists (e.g. task retried after a
        restart), we switch to it and rebase onto ``origin/<default_branch>``
        so the agent starts with the latest upstream changes.
        """
        # Check if this is a worktree
        is_worktree = self._is_worktree(checkout_path)

        self._run(["fetch", "origin"], cwd=checkout_path)

        if is_worktree:
            # In a worktree, we can't checkout the default branch if it's already
            # checked out in the source repo. Instead, fetch updates and create
            # the new branch directly from the remote default branch.
            try:
                self._run(["checkout", "-b", branch_name, f"origin/{default_branch}"], cwd=checkout_path)
            except GitError:
                # Branch already exists (retry) — switch to it and rebase
                # onto latest origin/<default_branch> so agent has fresh code.
                self._run(["checkout", branch_name], cwd=checkout_path)
                self._rebase_onto_default(checkout_path, default_branch)
        else:
            # Normal checkout flow: hard-reset default branch to match remote,
            # then create task branch. Hard reset is used instead of pull
            # because pull can fail when local main has diverged (e.g. from
            # un-pushed merge commits left by _merge_and_push).
            self._run(["checkout", default_branch], cwd=checkout_path)
            self._run(
                ["reset", "--hard", f"origin/{default_branch}"],
                cwd=checkout_path,
            )
            try:
                self._run(["checkout", "-b", branch_name], cwd=checkout_path)
            except GitError:
                # Branch already exists (e.g. task retried after restart) —
                # switch to it and rebase onto latest main so the agent
                # doesn't work on stale code from the previous attempt.
                self._run(["checkout", branch_name], cwd=checkout_path)
                self._rebase_onto_default(checkout_path, default_branch)

    def switch_to_branch(
        self, checkout_path: str, branch_name: str,
        default_branch: str = "main",
    ) -> None:
        """Switch to an existing branch, pulling latest and rebasing onto main.

        Used for subtask branch reuse: when a plan generates multiple subtasks
        that should share a branch, this lets the second task pick up where the
        first left off rather than creating a new branch.

        After switching, the branch is rebased onto ``origin/<default_branch>``
        so subtask chains stay closer to main and reduce the chance of merge
        conflicts when the work is eventually merged back.

        If the branch doesn't exist locally or on the remote (e.g. LINK repos
        with no remote), creates it as a new local branch.
        """
        try:
            self._run(["fetch", "origin"], cwd=checkout_path)
        except GitError:
            pass  # may fail if no remote configured
        try:
            self._run(["checkout", branch_name], cwd=checkout_path)
        except GitError:
            # Branch doesn't exist locally — try tracking remote
            try:
                self._run(["checkout", "-b", branch_name, f"origin/{branch_name}"],
                           cwd=checkout_path)
            except GitError:
                # No remote branch either (e.g. LINK repo) — create fresh
                self._run(["checkout", "-b", branch_name], cwd=checkout_path)
        try:
            self._run(["pull", "origin", branch_name], cwd=checkout_path)
        except GitError:
            pass  # may fail if no upstream tracking

        # Rebase onto origin/<default_branch> so subtask chains stay close
        # to main and reduce merge conflicts later.
        self._rebase_onto_default(checkout_path, default_branch)

    def mid_chain_sync(
        self, checkout_path: str, branch_name: str,
        default_branch: str = "main",
    ) -> bool:
        """Push intermediate subtask work and rebase onto latest main.

        Called between subtask completions in a chained plan to:

        1. **Push** current commits to remote — saves intermediate work so
           it survives agent crashes and is visible to other clones.
        2. **Rebase** the branch onto ``origin/<default_branch>`` — keeps
           the subtask chain close to main and reduces the chance of large
           merge conflicts when the final subtask merges the accumulated
           work.
        3. **Force-push** the rebased branch — updates the remote ref to
           match the rewritten (rebased) history.

        This resolves **Gap G6** for long subtask chains where drift from
        ``main`` would otherwise accumulate across multiple sequential
        subtask executions.

        Returns ``True`` if the full sync (push + rebase + force-push)
        succeeded.  Returns ``False`` if the rebase conflicted — the branch
        is left in its original pre-rebase state and the initial push may
        still have saved the intermediate work to the remote.

        All failures are non-fatal: callers should catch exceptions and
        continue — the next subtask can still work on the branch as-is.
        """
        # 1. Push current branch commits to remote (saves intermediate work).
        #    First push may fail if the branch hasn't been pushed before or
        #    if a previous mid-chain sync already pushed + rebased, so fall
        #    back to --force-with-lease which is safe for agent-owned branches.
        try:
            self._run(["push", "origin", branch_name], cwd=checkout_path)
        except GitError:
            try:
                self._run(
                    ["push", "--force-with-lease", "origin", branch_name],
                    cwd=checkout_path,
                )
            except GitError:
                pass  # Push failed — continue with rebase anyway

        # 2. Fetch latest remote state so rebase target is up to date.
        self._run(["fetch", "origin"], cwd=checkout_path)

        # 3. Rebase onto origin/<default_branch>.
        try:
            self._run(
                ["rebase", f"origin/{default_branch}"], cwd=checkout_path,
            )
        except GitError:
            # Rebase conflicts — abort and leave branch as-is.
            try:
                self._run(["rebase", "--abort"], cwd=checkout_path)
            except GitError:
                pass
            return False

        # 4. Force-push the rebased branch so remote matches local.
        try:
            self._run(
                ["push", "--force-with-lease", "origin", branch_name],
                cwd=checkout_path,
            )
        except GitError:
            pass  # Rebased locally but push failed — next subtask will try

        return True

    def push_branch(
        self, checkout_path: str, branch_name: str, *,
        force_with_lease: bool = False,
    ) -> None:
        """Push a local branch to the ``origin`` remote.

        When *force_with_lease* is ``True``, uses ``--force-with-lease`` so the
        push is safe for retries: if the branch was already pushed in a
        previous attempt, a second push with amended/additional commits will
        succeed as long as no *other* user pushed to the same branch in the
        meantime.  This resolves **Gap G5** for PR branch pushes.

        Plain push (default) is used for the ``sync_and_merge`` flow where
        only the default branch is pushed and force-push is never appropriate.
        """
        args = ["push", "origin", branch_name]
        if force_with_lease:
            args.insert(2, "--force-with-lease")
        self._run(args, cwd=checkout_path)

    def merge_branch(
        self, checkout_path: str, branch_name: str,
        default_branch: str = "main",
    ) -> bool:
        """Merge branch into default. Returns True if successful, False if conflict.

        Checks out the default branch, fetches from origin, and hard-resets
        to ``origin/<default_branch>`` before merging.  This ensures the
        local default branch matches the remote even when other agents have
        pushed since the last fetch (resolves **Gap G1**).

        .. note:: For rebase-before-merge conflict resolution, use
           :meth:`sync_and_merge` which attempts a rebase of the task branch
           onto ``origin/<default_branch>`` when the direct merge fails.
        """
        self._run(["checkout", default_branch], cwd=checkout_path)
        # Pull latest remote state before merging so we don't merge into
        # a stale local copy of the default branch (fixes G1).
        self._run(["fetch", "origin"], cwd=checkout_path)
        self._run(
            ["reset", "--hard", f"origin/{default_branch}"], cwd=checkout_path,
        )
        try:
            self._run(["merge", branch_name], cwd=checkout_path)
            return True
        except GitError:
            self._run(["merge", "--abort"], cwd=checkout_path)
            return False

    def rebase_onto(
        self, checkout_path: str, branch_name: str,
        target_branch: str = "main",
    ) -> bool:
        """Rebase branch onto target. Returns True on success, False on conflict.

        Switches to *branch_name*, then rebases it onto
        ``origin/<target_branch>``.  If the rebase encounters conflicts it is
        aborted and the method returns ``False`` — the branch is left in its
        original pre-rebase state.

        Used by :meth:`sync_and_merge` for its rebase-before-merge conflict
        resolution (Gap G3), and available as a public API for callers that
        need to rebase an arbitrary branch onto any target.
        """
        self._run(["checkout", branch_name], cwd=checkout_path)
        try:
            self._run(["rebase", f"origin/{target_branch}"], cwd=checkout_path)
            return True
        except GitError:
            try:
                self._run(["rebase", "--abort"], cwd=checkout_path)
            except GitError:
                pass  # rebase may not be in progress if it failed early
            return False

    def sync_and_merge(
        self, checkout_path: str, branch_name: str,
        default_branch: str = "main", max_retries: int = 1,
    ) -> tuple[bool, str]:
        """Pull latest main, merge branch, push. Returns (success, error_msg).

        Encapsulates the full sync-merge-push flow as a single higher-level
        operation.  Callers (e.g. the orchestrator) no longer need to
        coordinate fetch / checkout / reset / merge / push individually.

        Steps:
          1. Fetch latest remote state.
          2. Checkout the default branch and hard-reset to ``origin/<default_branch>``.
          3. Attempt the merge; on conflict, try rebasing the task branch
             onto ``origin/<default_branch>`` and retry the merge once.
             If the rebase itself conflicts or the retry merge still fails,
             return ``merge_conflict``.
          4. Push with up to *max_retries* retries.  On push failure (e.g.
             another agent pushed in the meantime), pull --rebase and retry.
             If all retries are exhausted, return a push failure message.
        """
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

            # 3a. Direct merge failed — attempt rebase-before-merge.
            # Rebase the task branch onto origin/<default_branch> so it
            # incorporates upstream changes, then retry the merge.
            rebased = self.rebase_onto(
                checkout_path, branch_name, default_branch,
            )
            if not rebased:
                # Rebase itself conflicted — give up
                # Switch back to default branch for a clean state
                self._run(["checkout", default_branch], cwd=checkout_path)
                return (False, "merge_conflict")

            # 3b. Rebase succeeded — retry merge on a fresh default branch
            self._run(["checkout", default_branch], cwd=checkout_path)
            self._run(
                ["reset", "--hard", f"origin/{default_branch}"],
                cwd=checkout_path,
            )
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
                    self._run(
                        ["pull", "--rebase", "origin", default_branch],
                        cwd=checkout_path,
                    )
                else:
                    return (False, f"push_failed: {e}")

        return (False, "push_failed_exhausted")

    def recover_workspace(
        self, checkout_path: str, default_branch: str = "main",
    ) -> None:
        """Reset workspace to a clean state after a failed merge-and-push.

        Checks out the default branch and hard-resets it to
        ``origin/<default_branch>`` so the workspace is ready for the
        next task.  This undoes any local merge commit left behind by a
        failed push.

        Best-effort: callers should wrap in try/except if they cannot
        tolerate failures here (e.g. the workspace is in a broken git
        state that even checkout cannot recover from).
        """
        self._run(["checkout", default_branch], cwd=checkout_path)
        self._run(
            ["reset", "--hard", f"origin/{default_branch}"],
            cwd=checkout_path,
        )

    def delete_branch(
        self, checkout_path: str, branch_name: str, *, delete_remote: bool = True,
    ) -> None:
        """Delete a branch locally and optionally on the remote."""
        try:
            self._run(["branch", "-d", branch_name], cwd=checkout_path)
        except GitError:
            # Force-delete if not fully merged (e.g. squash-merged PR)
            try:
                self._run(["branch", "-D", branch_name], cwd=checkout_path)
            except GitError:
                pass  # branch may not exist locally
        if delete_remote:
            try:
                self._run(["push", "origin", "--delete", branch_name], cwd=checkout_path)
            except GitError:
                pass  # branch may not exist on remote (already deleted)

    def create_worktree(self, source_path: str, worktree_path: str, branch: str) -> None:
        """Create a git worktree for agent isolation on linked repos."""
        os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
        self._run(["worktree", "add", "-b", branch, worktree_path], cwd=source_path)

    def remove_worktree(self, source_path: str, worktree_path: str) -> None:
        """Remove a git worktree."""
        try:
            self._run(["worktree", "remove", worktree_path], cwd=source_path)
        except GitError:
            # Force remove if normal remove fails
            self._run(["worktree", "remove", "--force", worktree_path], cwd=source_path)

    def init_repo(self, path: str) -> None:
        """Initialize a new git repo with an empty initial commit."""
        os.makedirs(path, exist_ok=True)
        self._run(["init"], cwd=path)
        self._run(["commit", "--allow-empty", "-m", "Initial commit"], cwd=path)

    def get_diff(self, checkout_path: str, base_branch: str = "main") -> str:
        """Return the full diff against base branch."""
        try:
            return self._run(["diff", base_branch], cwd=checkout_path)
        except GitError:
            return ""

    def get_changed_files(self, checkout_path: str, base_branch: str = "main") -> list[str]:
        try:
            output = self._run(
                ["diff", "--name-only", base_branch], cwd=checkout_path
            )
            return output.split("\n") if output else []
        except GitError:
            return []

    def commit_all(self, checkout_path: str, message: str) -> bool:
        """Stage all changes and commit. Returns True if a commit was made, False if nothing to commit.

        Uses add-all-then-check-staged pattern: ``git add -A`` stages
        everything (including untracked files the agent created), then
        ``git diff --cached --quiet`` checks whether anything is actually
        staged.  This avoids the race condition of checking status before
        staging.
        """
        self._run(["add", "-A"], cwd=checkout_path)
        # git diff --cached --quiet exits 1 if there are staged changes
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=checkout_path,
            capture_output=True,
        )
        if result.returncode == 0:
            return False  # Nothing to commit
        self._run(["commit", "-m", message], cwd=checkout_path)
        return True

    def create_pr(
        self, checkout_path: str, branch: str, title: str, body: str,
        base: str = "main",
    ) -> str:
        """Create a GitHub PR using the ``gh`` CLI. Returns the PR URL.

        Delegates to ``gh pr create`` rather than the GitHub API directly,
        so the user's existing gh authentication is reused.
        """
        result = subprocess.run(
            ["gh", "pr", "create", "--title", title, "--body", body,
             "--base", base, "--head", branch],
            cwd=checkout_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise GitError(f"gh pr create failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def check_pr_merged(self, checkout_path: str, pr_url: str) -> bool | None:
        """Check if a PR has been merged via the ``gh`` CLI.

        Returns True (merged), False (still open), None (closed without merge).
        The orchestrator polls this for AWAITING_APPROVAL tasks to detect when
        a human merges the PR and the task can be marked COMPLETED.
        """
        result = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", "state,mergedAt"],
            cwd=checkout_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise GitError(f"gh pr view failed: {result.stderr.strip()}")
        data = json.loads(result.stdout)
        state = data.get("state", "").upper()
        if state == "MERGED" or data.get("mergedAt"):
            return True
        if state == "OPEN":
            return False
        # CLOSED without merge
        return None

    def get_status(self, checkout_path: str) -> str:
        """Return the output of `git status` for the given repository path."""
        try:
            return self._run(["status"], cwd=checkout_path)
        except GitError:
            return ""

    def get_current_branch(self, checkout_path: str) -> str:
        """Return the current branch name."""
        try:
            return self._run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=checkout_path)
        except GitError:
            return ""

    def get_recent_commits(self, checkout_path: str, count: int = 5) -> str:
        """Return recent commit log (one-line format)."""
        try:
            return self._run(
                ["log", f"--oneline", f"-{count}"], cwd=checkout_path
            )
        except GitError:
            return ""

    @staticmethod
    def slugify(text: str) -> str:
        text = text.lower().strip()
        text = re.sub(r"[^\w\s-]", "", text)
        text = re.sub(r"[\s_]+", "-", text)
        text = re.sub(r"-+", "-", text)
        return text.strip("-")

    @staticmethod
    def make_branch_name(task_id: str, title: str) -> str:
        """Build a branch name in ``<task-id>/<slug>`` format.

        Examples: ``brave-fox/add-retry-logic``, ``calm-river/fix-auth-bug``.
        The task ID prefix makes branches easy to trace back to their task,
        and the slug suffix provides human-readable context.
        """
        return f"{task_id}/{GitManager.slugify(title)}"
