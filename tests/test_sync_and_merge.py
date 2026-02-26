"""Tests for GitManager.sync_and_merge().

Verifies the full sync-merge-push flow:
  - Happy path: fetch, reset, merge, push all succeed.
  - Merge conflict: detected and reported without attempting push.
  - Push rejection with retry: pull --rebase then push again.
  - Push retries exhausted: returns failure with message.
  - Default branch is synced to origin before merging.
"""

import pathlib
import subprocess

import pytest

from src.git.manager import GitError, GitManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: str) -> str:
    """Run a git command, returning stdout."""
    result = subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _git_commit(cwd: str, filename: str, content: str, message: str) -> str:
    """Create/update a file, commit it, and return the commit SHA."""
    pathlib.Path(cwd, filename).write_text(content)
    _git(["add", filename], cwd=cwd)
    _git(["-c", "user.name=Test", "-c", "user.email=t@t.com",
          "commit", "-m", message], cwd=cwd)
    return _git(["rev-parse", "HEAD"], cwd=cwd)


def _head_sha(cwd: str) -> str:
    return _git(["rev-parse", "HEAD"], cwd=cwd)


def _current_branch(cwd: str) -> str:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def git_repo(tmp_path):
    """Create a bare remote + working clone with an initial commit."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True, capture_output=True,
    )
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(remote), str(clone)],
        check=True, capture_output=True,
    )
    # Initial commit
    (clone / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=str(clone), check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=t@t.com",
         "commit", "-m", "init"],
        cwd=str(clone), check=True, capture_output=True,
    )
    subprocess.run(["git", "push"], cwd=str(clone), check=True,
                   capture_output=True)
    return {"remote": str(remote), "clone": str(clone)}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSyncAndMerge:
    """Integration tests for the sync_and_merge() method."""

    def test_happy_path(self, git_repo):
        """Fetch, merge, push all succeed on the first attempt."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Create a task branch with work
        mgr.create_branch(clone, "task/happy")
        _git_commit(clone, "feature.txt", "feature", "add feature")
        _git(["checkout", "main"], cwd=clone)

        success, err = mgr.sync_and_merge(clone, "task/happy")

        assert success is True
        assert err == ""
        # The merge should be on main
        assert _current_branch(clone) == "main"
        log = _git(["log", "--oneline"], cwd=clone)
        assert "add feature" in log

    def test_push_lands_on_remote(self, git_repo, tmp_path):
        """After sync_and_merge, the remote has the merged commit."""
        mgr = GitManager()
        clone = git_repo["clone"]

        mgr.create_branch(clone, "task/push-check")
        _git_commit(clone, "pushed.txt", "pushed", "pushed commit")
        _git(["checkout", "main"], cwd=clone)

        success, _ = mgr.sync_and_merge(clone, "task/push-check")
        assert success is True

        # Verify via a fresh clone that the commit reached the remote
        clone2 = str(tmp_path / "verify-clone")
        subprocess.run(
            ["git", "clone", git_repo["remote"], clone2],
            check=True, capture_output=True,
        )
        log = _git(["log", "--oneline"], cwd=clone2)
        assert "pushed commit" in log

    def test_merge_conflict_returns_error(self, git_repo, tmp_path):
        """When the task branch conflicts with main, returns (False, 'merge_conflict')."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Create a task branch that modifies README.md
        mgr.create_branch(clone, "task/conflict")
        _git_commit(clone, "README.md", "task version", "task changes README")
        _git(["checkout", "main"], cwd=clone)

        # Advance origin/main with a conflicting change
        clone2 = str(tmp_path / "clone2")
        subprocess.run(
            ["git", "clone", git_repo["remote"], clone2],
            check=True, capture_output=True,
        )
        _git_commit(clone2, "README.md", "other version", "other changes README")
        _git(["push", "origin", "main"], cwd=clone2)

        success, err = mgr.sync_and_merge(clone, "task/conflict")

        assert success is False
        assert err == "merge_conflict"
        # main should still be in a clean state (merge aborted)
        assert _current_branch(clone) == "main"

    def test_syncs_before_merging(self, git_repo, tmp_path):
        """sync_and_merge fetches latest origin/main before merging."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Create a task branch
        mgr.create_branch(clone, "task/sync-test")
        _git_commit(clone, "task-file.txt", "task", "task commit")
        _git(["checkout", "main"], cwd=clone)

        # Advance origin/main via a second clone
        clone2 = str(tmp_path / "clone2")
        subprocess.run(
            ["git", "clone", git_repo["remote"], clone2],
            check=True, capture_output=True,
        )
        _git_commit(clone2, "other.txt", "other", "other agent commit")
        _git(["push", "origin", "main"], cwd=clone2)

        # Local clone's main is behind — sync_and_merge should fetch first
        success, err = mgr.sync_and_merge(clone, "task/sync-test")

        assert success is True
        assert err == ""
        log = _git(["log", "--oneline"], cwd=clone)
        assert "task commit" in log
        assert "other agent commit" in log

    def test_discards_stale_local_main(self, git_repo):
        """sync_and_merge hard-resets main, discarding un-pushed local commits."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Create a task branch with work
        mgr.create_branch(clone, "task/stale")
        _git_commit(clone, "feature.txt", "feature", "feature commit")

        # Go back to main and create a local-only commit (stale state)
        _git(["checkout", "main"], cwd=clone)
        _git_commit(clone, "stale.txt", "stale", "stale local commit")

        success, err = mgr.sync_and_merge(clone, "task/stale")

        assert success is True
        log = _git(["log", "--oneline"], cwd=clone)
        assert "feature commit" in log
        assert "stale local commit" not in log

    def test_max_retries_zero_single_push_attempt(self, git_repo, tmp_path):
        """With max_retries=0, only one push attempt is made."""
        mgr = GitManager()
        clone = git_repo["clone"]

        mgr.create_branch(clone, "task/single-try")
        _git_commit(clone, "f.txt", "f", "single try commit")
        _git(["checkout", "main"], cwd=clone)

        # Should succeed with a single attempt when remote is reachable
        success, err = mgr.sync_and_merge(clone, "task/single-try", max_retries=0)
        assert success is True
        assert err == ""

    def test_already_on_task_branch(self, git_repo):
        """sync_and_merge works even if we start on the task branch."""
        mgr = GitManager()
        clone = git_repo["clone"]

        mgr.create_branch(clone, "task/on-branch")
        _git_commit(clone, "work.txt", "work", "work commit")
        # Don't switch back to main — stay on task branch

        success, err = mgr.sync_and_merge(clone, "task/on-branch")

        assert success is True
        assert err == ""
        assert _current_branch(clone) == "main"
        log = _git(["log", "--oneline"], cwd=clone)
        assert "work commit" in log


class TestSyncAndMergeRetry:
    """Unit tests for the push-retry logic using mocked _run."""

    def test_push_retry_succeeds_on_second_attempt(self):
        """If push fails once and then succeeds, returns (True, '')."""
        mgr = GitManager()
        calls = []

        original_run = mgr._run

        def mock_run(args, cwd=None):
            calls.append(args)
            if args[:3] == ["push", "origin", "main"]:
                if sum(1 for c in calls if c[:3] == ["push", "origin", "main"]) == 1:
                    raise GitError("rejected")
            # For all other commands (and second push), just record but don't execute
            return ""

        mgr._run = mock_run

        success, err = mgr.sync_and_merge("/fake/path", "task/branch", max_retries=2)

        assert success is True
        assert err == ""
        # Should have two push attempts
        push_calls = [c for c in calls if c[:3] == ["push", "origin", "main"]]
        assert len(push_calls) == 2
        # Should have a pull --rebase between them
        rebase_calls = [c for c in calls if "pull" in c and "--rebase" in c]
        assert len(rebase_calls) == 1

    def test_push_retries_exhausted(self):
        """When all push attempts fail, returns (False, 'push_failed: ...')."""
        mgr = GitManager()

        def mock_run(args, cwd=None):
            if args[:3] == ["push", "origin", "main"]:
                raise GitError("rejected")
            return ""

        mgr._run = mock_run

        success, err = mgr.sync_and_merge("/fake/path", "task/branch", max_retries=2)

        assert success is False
        assert err.startswith("push_failed:")

    def test_merge_conflict_no_push_attempted(self):
        """On merge conflict, push is never attempted."""
        mgr = GitManager()
        calls = []

        def mock_run(args, cwd=None):
            calls.append(args)
            if args[:2] == ["merge", "task/branch"]:
                raise GitError("conflict")
            return ""

        mgr._run = mock_run

        success, err = mgr.sync_and_merge("/fake/path", "task/branch")

        assert success is False
        assert err == "merge_conflict"
        # Verify merge --abort was called
        abort_calls = [c for c in calls if c == ["merge", "--abort"]]
        assert len(abort_calls) == 1
        # Verify push was never called
        push_calls = [c for c in calls if c[0] == "push"]
        assert len(push_calls) == 0
