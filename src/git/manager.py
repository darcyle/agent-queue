from __future__ import annotations

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

    def create_branch(self, checkout_path: str, branch_name: str) -> None:
        self._run(["checkout", "-b", branch_name], cwd=checkout_path)

    def prepare_for_task(
        self, checkout_path: str, branch_name: str,
        default_branch: str = "main",
    ) -> None:
        self._run(["fetch", "origin"], cwd=checkout_path)
        self._run(["checkout", default_branch], cwd=checkout_path)
        try:
            self._run(["pull", "origin", default_branch], cwd=checkout_path)
        except GitError:
            pass  # may fail if no upstream tracking
        self._run(["checkout", "-b", branch_name], cwd=checkout_path)

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
