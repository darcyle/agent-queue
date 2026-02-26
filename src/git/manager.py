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

        This is the canonical way to sync a workspace's default branch with
        ``origin``.  A hard reset (rather than pull) ensures we always match
        the remote even if a previous ``_merge_and_push`` left local main
        diverged from origin (e.g. un-pushed merge commits).

        The caller must have the *default_branch* checked out before calling
        this method, or use it only for the fetch+reset side-effect on a
        detached HEAD scenario.
        """
        self._run(["fetch", "origin"], cwd=checkout_path)
        self._run(["checkout", default_branch], cwd=checkout_path)
        self._run(["reset", "--hard", f"origin/{default_branch}"], cwd=checkout_path)

    def prepare_for_task(
        self, checkout_path: str, branch_name: str,
        default_branch: str = "main",
    ) -> None:
        """Fetch latest and create a task branch off the default branch.

        Two code paths depending on whether the checkout is a worktree:
        - **Normal repo:** checkout default branch, hard-reset to
          ``origin/<default_branch>``, then create the task branch.  The hard
          reset (instead of pull) ensures we always match the remote even if a
          previous ``_merge_and_push`` left local main diverged.
        - **Worktree:** Can't checkout the default branch (it's already checked
          out in the main working tree), so we create the task branch directly
          from ``origin/<default_branch>`` in a single step.

        In both cases, if the branch already exists (e.g. task retried after a
        restart), we switch to it and rebase onto ``origin/<default_branch>``
        so the agent works on the latest code rather than a stale snapshot.
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
                # onto latest origin so the agent isn't working on stale code.
                self._run(["checkout", branch_name], cwd=checkout_path)
                try:
                    self._run(["rebase", f"origin/{default_branch}"], cwd=checkout_path)
                except GitError:
                    # Rebase conflict — abort and let the agent work with
                    # whatever is on the branch as-is.
                    try:
                        self._run(["rebase", "--abort"], cwd=checkout_path)
                    except GitError:
                        pass  # rebase may not be in progress
        else:
            # Normal checkout flow: hard-reset default branch to match remote,
            # then create the task branch.
            self._run(["checkout", default_branch], cwd=checkout_path)
            # Hard reset instead of pull — always matches remote even if local
            # main diverged (e.g. un-pushed merge commits from a prior task).
            self._run(["reset", "--hard", f"origin/{default_branch}"], cwd=checkout_path)
            try:
                self._run(["checkout", "-b", branch_name], cwd=checkout_path)
            except GitError:
                # Branch already exists (e.g. task retried after restart) —
                # switch to it and rebase onto latest origin/<default_branch>.
                self._run(["checkout", branch_name], cwd=checkout_path)
                try:
                    self._run(["rebase", f"origin/{default_branch}"], cwd=checkout_path)
                except GitError:
                    # Rebase conflict — abort and let the agent work with
                    # whatever is on the branch as-is.
                    try:
                        self._run(["rebase", "--abort"], cwd=checkout_path)
                    except GitError:
                        pass  # rebase may not be in progress

    def switch_to_branch(
        self, checkout_path: str, branch_name: str,
        default_branch: str = "main",
    ) -> None:
        """Switch to an existing branch, pulling latest and rebasing onto main.

        Used for subtask branch reuse: when a plan generates multiple subtasks
        that should share a branch, this lets the second task pick up where the
        first left off rather than creating a new branch.

        After switching, we rebase onto ``origin/<default_branch>`` so that
        subtask chains stay closer to main and don't accumulate drift that
        would only surface as conflicts at merge time.

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

        # Rebase onto latest origin/<default_branch> so subtask chains stay
        # close to main and conflicts are discovered early rather than at
        # final merge time.
        try:
            self._run(["rebase", f"origin/{default_branch}"], cwd=checkout_path)
        except GitError:
            # Rebase conflict — abort and keep the branch as-is.
            # The agent can still work; conflicts will be handled at merge time.
            try:
                self._run(["rebase", "--abort"], cwd=checkout_path)
            except GitError:
                pass  # rebase may not be in progress

    def push_branch(
        self, checkout_path: str, branch_name: str,
        *, force_with_lease: bool = False,
    ) -> None:
        """Push a branch to origin.

        Args:
            checkout_path: Path to the local git checkout.
            branch_name: The branch to push.
            force_with_lease: If True, use ``--force-with-lease`` so the push
                succeeds even when the remote branch has been previously pushed
                (e.g. task retries or subtask chains that push intermediate
                results).  This is safe because task branches are owned by a
                single agent and are never concurrently updated by others.
        """
        args = ["push", "origin", branch_name]
        if force_with_lease:
            args.insert(2, "--force-with-lease")
        self._run(args, cwd=checkout_path)

    def rebase_onto(
        self, checkout_path: str, branch_name: str,
        onto: str = "main",
    ) -> bool:
        """Rebase *branch_name* onto *onto*.  Returns True if successful.

        Checks out the task branch, rebases it onto the target, then returns
        to the original branch.  If the rebase fails (conflicts), it is
        aborted and False is returned.  The caller is responsible for
        ensuring ``onto`` is up-to-date (e.g. via fetch + hard-reset).
        """
        original_branch = self.get_current_branch(checkout_path)
        self._run(["checkout", branch_name], cwd=checkout_path)
        try:
            self._run(["rebase", onto], cwd=checkout_path)
        except GitError:
            # Rebase conflict — abort and restore original branch
            try:
                self._run(["rebase", "--abort"], cwd=checkout_path)
            except GitError:
                pass  # rebase may not be in progress
            try:
                self._run(["checkout", original_branch], cwd=checkout_path)
            except GitError:
                pass
            return False
        # Rebase succeeded — return to original branch
        try:
            self._run(["checkout", original_branch], cwd=checkout_path)
        except GitError:
            pass
        return True

    def merge_branch(
        self, checkout_path: str, branch_name: str,
        default_branch: str = "main",
    ) -> bool:
        """Merge branch into default. Returns True if successful, False if conflict.

        Fetches from origin and hard-resets the default branch to
        ``origin/<default_branch>`` before merging.  This ensures we always
        merge into the latest remote state, preventing stale-main problems
        when multiple agents push concurrently.  If the fetch fails (e.g. no
        remote configured for LINK repos), we fall through and merge against
        whatever local state we have.
        """
        try:
            self._run(["fetch", "origin"], cwd=checkout_path)
        except GitError:
            pass  # no remote configured (LINK repos) — merge against local state
        self._run(["checkout", default_branch], cwd=checkout_path)
        try:
            self._run(["reset", "--hard", f"origin/{default_branch}"], cwd=checkout_path)
        except GitError:
            pass  # no remote tracking branch — use local state as-is
        try:
            self._run(["merge", branch_name], cwd=checkout_path)
            return True
        except GitError:
            self._run(["merge", "--abort"], cwd=checkout_path)
            return False

    def sync_and_merge(
        self, checkout_path: str, branch_name: str,
        default_branch: str = "main", max_retries: int = 1,
    ) -> tuple[bool, str]:
        """Pull latest default branch, merge task branch, and push.

        Encapsulates the full sync-merge-push flow that the orchestrator
        uses after an agent finishes work on a task branch.  The steps:

        1. Fetch from origin so we have the latest remote state.
        2. Checkout the default branch and hard-reset it to
           ``origin/<default_branch>`` — this discards any stale local
           state (e.g. un-pushed merge commits from a prior attempt).
        3. Merge the task branch into the default branch.
        3b. **Rebase fallback:** If the direct merge fails with conflicts,
           rebase the task branch onto ``origin/<default_branch>`` and
           retry the merge.  This resolves conflicts that arise from the
           task branch being based on a stale snapshot of main — the
           rebase replays commits on top of the latest code, and if it
           succeeds the subsequent merge is a clean fast-forward.
        4. Push the default branch to origin, retrying up to
           *max_retries* times if the push is rejected (e.g. another
           agent pushed between our fetch and push).

        Returns:
            A ``(success, error_msg)`` tuple.  On success, *error_msg*
            is the empty string.  On failure, it is one of:

            - ``"merge_conflict"`` — the task branch conflicts with
              the default branch (even after rebase attempt).
            - ``"push_failed: <details>"`` — all push attempts failed.
        """
        # 1. Fetch latest remote state
        self._run(["fetch", "origin"], cwd=checkout_path)

        # 2. Checkout default branch and hard-reset to origin
        self._run(["checkout", default_branch], cwd=checkout_path)
        self._run(["reset", "--hard", f"origin/{default_branch}"], cwd=checkout_path)

        # 3. Attempt merge
        try:
            self._run(["merge", branch_name], cwd=checkout_path)
        except GitError:
            self._run(["merge", "--abort"], cwd=checkout_path)

            # 3b. Rebase fallback: rebase the task branch onto the latest
            # default branch and retry the merge.  If the task branch was
            # simply based on a stale main, the rebase resolves the drift
            # and the retry merge becomes a fast-forward.
            rebased = self.rebase_onto(
                checkout_path, branch_name, f"origin/{default_branch}",
            )
            if not rebased:
                return (False, "merge_conflict")

            # Retry merge after successful rebase
            self._run(["checkout", default_branch], cwd=checkout_path)
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
                    # Re-pull (rebase) to incorporate whatever was pushed
                    # in the meantime, then retry the push.
                    self._run(
                        ["pull", "--rebase", "origin", default_branch],
                        cwd=checkout_path,
                    )
                else:
                    return (False, f"push_failed: {e}")

        # Unreachable, but satisfies the type checker.
        return (False, "push_failed_exhausted")  # pragma: no cover

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
