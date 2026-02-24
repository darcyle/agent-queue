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

    def prepare_for_task(
        self, checkout_path: str, branch_name: str,
        default_branch: str = "main",
    ) -> None:
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
                # If branch creation fails, try to just switch to existing branch
                self._run(["checkout", branch_name], cwd=checkout_path)
        else:
            # Normal checkout flow: update default branch then create task branch
            self._run(["checkout", default_branch], cwd=checkout_path)
            try:
                self._run(["pull", "origin", default_branch], cwd=checkout_path)
            except GitError:
                pass  # may fail if no upstream tracking
            try:
                self._run(["checkout", "-b", branch_name], cwd=checkout_path)
            except GitError:
                # Branch already exists (e.g. task retried after restart) —
                # switch to it instead of failing.
                self._run(["checkout", branch_name], cwd=checkout_path)

    def switch_to_branch(self, checkout_path: str, branch_name: str) -> None:
        """Switch to an existing branch, pulling latest if available on remote."""
        try:
            self._run(["fetch", "origin"], cwd=checkout_path)
        except GitError:
            pass  # may fail if no remote configured
        try:
            self._run(["checkout", branch_name], cwd=checkout_path)
        except GitError:
            # Branch may not exist locally yet — try tracking remote
            self._run(["checkout", "-b", branch_name, f"origin/{branch_name}"],
                       cwd=checkout_path)
        try:
            self._run(["pull", "origin", branch_name], cwd=checkout_path)
        except GitError:
            pass  # may fail if no upstream tracking

    def push_branch(self, checkout_path: str, branch_name: str) -> None:
        self._run(["push", "origin", branch_name], cwd=checkout_path)

    def merge_branch(
        self, checkout_path: str, branch_name: str,
        default_branch: str = "main",
    ) -> bool:
        """Merge branch into default. Returns True if successful, False if conflict."""
        self._run(["checkout", default_branch], cwd=checkout_path)
        try:
            self._run(["merge", branch_name], cwd=checkout_path)
            return True
        except GitError:
            self._run(["merge", "--abort"], cwd=checkout_path)
            return False

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
        """Stage all changes and commit. Returns True if a commit was made, False if nothing to commit."""
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
        """Create a GitHub PR using the gh CLI. Returns the PR URL."""
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
        """Check if a PR has been merged.

        Returns True (merged), False (still open), None (closed without merge).
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
        return f"{task_id}/{GitManager.slugify(title)}"
