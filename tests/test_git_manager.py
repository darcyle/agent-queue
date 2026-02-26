import pathlib
import subprocess
import pytest
from src.git.manager import GitManager


def _git(args: list[str], cwd: str) -> str:
    """Run a git command in the given directory, returning stdout."""
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


def _current_branch(cwd: str) -> str:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)


def _head_sha(cwd: str) -> str:
    return _git(["rev-parse", "HEAD"], cwd=cwd)


@pytest.fixture
def git_repo(tmp_path):
    """Create a bare remote + working clone for testing."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   check=True, capture_output=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(remote), str(clone)], check=True,
                   capture_output=True)
    # Create initial commit
    (clone / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=str(clone), check=True,
                   capture_output=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=t@t.com",
                     "commit", "-m", "init"], cwd=str(clone), check=True,
                   capture_output=True)
    subprocess.run(["git", "push"], cwd=str(clone), check=True,
                   capture_output=True)
    return {"remote": str(remote), "clone": str(clone)}


class TestGitManager:
    def test_create_checkout(self, git_repo, tmp_path):
        mgr = GitManager()
        checkout_path = str(tmp_path / "agent-1" / "repo")
        mgr.create_checkout(git_repo["remote"], checkout_path)
        assert (tmp_path / "agent-1" / "repo" / "README.md").exists()

    def test_create_branch(self, git_repo):
        mgr = GitManager()
        mgr.create_branch(git_repo["clone"], "task-1/do-thing")
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=git_repo["clone"], capture_output=True, text=True,
        )
        assert result.stdout.strip() == "task-1/do-thing"

    def test_prepare_for_task(self, git_repo):
        mgr = GitManager()
        mgr.prepare_for_task(git_repo["clone"], "task-1/new-feature")
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=git_repo["clone"], capture_output=True, text=True,
        )
        assert result.stdout.strip() == "task-1/new-feature"

    def test_validate_checkout(self, git_repo):
        mgr = GitManager()
        assert mgr.validate_checkout(git_repo["clone"])
        assert not mgr.validate_checkout("/nonexistent/path")

    def test_slugify(self):
        mgr = GitManager()
        assert mgr.slugify("Implement OAuth Login!") == "implement-oauth-login"
        assert mgr.slugify("fix  multiple   spaces") == "fix-multiple-spaces"

    def test_is_worktree(self, git_repo, tmp_path):
        """Test worktree detection."""
        mgr = GitManager()
        # Regular clone should not be detected as worktree
        assert not mgr._is_worktree(git_repo["clone"])

        # Create a worktree
        worktree_path = str(tmp_path / "worktree-test")
        mgr.create_worktree(git_repo["clone"], worktree_path, "wt-branch")

        # Worktree should be detected as worktree
        assert mgr._is_worktree(worktree_path)

    def test_prepare_for_task_worktree(self, git_repo, tmp_path):
        """Test prepare_for_task works correctly in worktree context."""
        mgr = GitManager()

        # Create a worktree
        worktree_path = str(tmp_path / "worktree-task")
        mgr.create_worktree(git_repo["clone"], worktree_path, "initial-branch")

        # Verify the worktree was created and is on the initial branch
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=worktree_path, capture_output=True, text=True,
        )
        assert result.stdout.strip() == "initial-branch"

        # Now prepare for a new task - this should not fail even though
        # 'main' is checked out in the source repo
        mgr.prepare_for_task(worktree_path, "task-2/another-feature")

        # Verify we're now on the new task branch
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=worktree_path, capture_output=True, text=True,
        )
        assert result.stdout.strip() == "task-2/another-feature"


class TestPrepareForTaskHardReset:
    """Tests for the hard-reset path in prepare_for_task().

    Verifies that local main is always reset to match origin/main,
    even when a previous merge-and-push left local main diverged.
    """

    def test_hard_reset_recovers_from_diverged_main(self, git_repo, tmp_path):
        """If local main has un-pushed merge commits, hard reset brings it back in sync."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Simulate a diverged local main: create a local-only commit on main
        # that was never pushed (as if _merge_and_push merged but push failed).
        _git_commit(clone, "local-only.txt", "diverged", "local merge commit")
        local_main_sha = _head_sha(clone)

        # The remote main should NOT have this commit
        origin_main_sha = _git(["rev-parse", "origin/main"], cwd=clone)
        assert local_main_sha != origin_main_sha

        # prepare_for_task should hard-reset main to origin/main
        mgr.prepare_for_task(clone, "task/after-diverge")

        # After creating the task branch, switch back to main to verify it was reset
        _git(["checkout", "main"], cwd=clone)
        assert _head_sha(clone) == origin_main_sha

    def test_task_branch_starts_from_origin_main(self, git_repo, tmp_path):
        """New task branches should always start from origin/main, not stale local main."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Push a new commit to remote via a second clone so the first clone's
        # local main is behind origin/main.
        clone2 = str(tmp_path / "clone2")
        subprocess.run(["git", "clone", git_repo["remote"], clone2],
                       check=True, capture_output=True)
        _git_commit(clone2, "new-file.txt", "from clone2", "advance main")
        _git(["push", "origin", "main"], cwd=clone2)

        # Now prepare_for_task in the original clone — it should fetch the new
        # commit and start the task branch from the latest origin/main.
        mgr.prepare_for_task(clone, "task/should-be-latest")

        # The task branch should include the commit from clone2
        log = _git(["log", "--oneline"], cwd=clone)
        assert "advance main" in log


class TestPrepareForTaskRebaseOnRetry:
    """Tests for the rebase-on-retry behavior in prepare_for_task().

    When a task is retried and the branch already exists, prepare_for_task()
    should switch to it and rebase onto origin/<default_branch>.
    """

    def test_existing_branch_gets_rebased(self, git_repo, tmp_path):
        """Retried task rebases existing branch onto latest origin/main."""
        mgr = GitManager()
        clone = git_repo["clone"]
        branch = "task/retry-me"

        # First run: create the task branch normally
        mgr.prepare_for_task(clone, branch)
        _git_commit(clone, "work.txt", "agent work", "agent commit")

        # Advance origin/main via a second clone
        clone2 = str(tmp_path / "clone2")
        subprocess.run(["git", "clone", git_repo["remote"], clone2],
                       check=True, capture_output=True)
        _git_commit(clone2, "upstream.txt", "upstream", "upstream commit")
        _git(["push", "origin", "main"], cwd=clone2)

        # Second run (retry): prepare_for_task finds existing branch, rebases it
        mgr.prepare_for_task(clone, branch)

        assert _current_branch(clone) == branch
        # The branch should now contain both the agent's work and the upstream commit
        log = _git(["log", "--oneline"], cwd=clone)
        assert "agent commit" in log
        assert "upstream commit" in log

    def test_rebase_conflict_aborts_gracefully(self, git_repo, tmp_path):
        """If rebase has conflicts, it aborts and leaves the branch as-is."""
        mgr = GitManager()
        clone = git_repo["clone"]
        branch = "task/conflict-retry"

        # First run: create task branch and modify README.md
        mgr.prepare_for_task(clone, branch)
        _git_commit(clone, "README.md", "agent version", "agent changes README")

        # Advance origin/main with a conflicting change to README.md
        clone2 = str(tmp_path / "clone2")
        subprocess.run(["git", "clone", git_repo["remote"], clone2],
                       check=True, capture_output=True)
        _git_commit(clone2, "README.md", "upstream version", "upstream changes README")
        _git(["push", "origin", "main"], cwd=clone2)

        # Record state before retry
        pre_retry_sha = _head_sha(clone)

        # Second run (retry): rebase will conflict, should abort gracefully
        # and NOT raise an exception.
        _git(["checkout", "main"], cwd=clone)  # switch away first
        mgr.prepare_for_task(clone, branch)

        # Should still be on the task branch
        assert _current_branch(clone) == branch
        # Branch should still have the agent's commit (rebase was aborted)
        assert _head_sha(clone) == pre_retry_sha

    def test_worktree_existing_branch_gets_rebased(self, git_repo, tmp_path):
        """Retried task in worktree context also rebases existing branch."""
        mgr = GitManager()
        clone = git_repo["clone"]
        branch = "task/wt-retry"

        # Create a worktree
        worktree_path = str(tmp_path / "worktree-retry")
        mgr.create_worktree(clone, worktree_path, "wt-setup-branch")

        # First run: create the task branch in the worktree
        mgr.prepare_for_task(worktree_path, branch)
        _git_commit(worktree_path, "work.txt", "agent work", "agent commit")

        # Advance origin/main via a second clone
        clone2 = str(tmp_path / "clone2")
        subprocess.run(["git", "clone", git_repo["remote"], clone2],
                       check=True, capture_output=True)
        _git_commit(clone2, "upstream.txt", "upstream", "upstream commit")
        _git(["push", "origin", "main"], cwd=clone2)

        # Second run (retry): prepare_for_task finds existing branch, rebases it
        mgr.prepare_for_task(worktree_path, branch)

        assert _current_branch(worktree_path) == branch
        log = _git(["log", "--oneline"], cwd=worktree_path)
        assert "agent commit" in log
        assert "upstream commit" in log


class TestPullLatestMain:
    """Tests for the pull_latest_main() convenience method."""

    def test_pull_latest_main_syncs_with_remote(self, git_repo, tmp_path):
        """pull_latest_main() should hard-reset local main to origin/main."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Advance origin/main via a second clone
        clone2 = str(tmp_path / "clone2")
        subprocess.run(["git", "clone", git_repo["remote"], clone2],
                       check=True, capture_output=True)
        new_sha = _git_commit(clone2, "new.txt", "new", "advance main")
        _git(["push", "origin", "main"], cwd=clone2)

        # Local clone is behind — pull_latest_main should sync it
        mgr.pull_latest_main(clone)

        assert _current_branch(clone) == "main"
        assert _head_sha(clone) == new_sha

    def test_pull_latest_main_discards_local_divergence(self, git_repo):
        """pull_latest_main() discards local-only commits on main."""
        mgr = GitManager()
        clone = git_repo["clone"]

        origin_sha = _git(["rev-parse", "origin/main"], cwd=clone)

        # Create a local-only commit (simulating a failed push after merge)
        _git_commit(clone, "local-only.txt", "diverged", "local merge")
        assert _head_sha(clone) != origin_sha

        # pull_latest_main should discard the local commit
        mgr.pull_latest_main(clone)
        assert _head_sha(clone) == origin_sha


class TestSwitchToBranchRebase:
    """Tests for rebase-onto-main behavior in switch_to_branch()."""

    def test_switch_to_branch_rebases_onto_main(self, git_repo, tmp_path):
        """switch_to_branch() should rebase onto origin/main after switching."""
        mgr = GitManager()
        clone = git_repo["clone"]
        branch = "subtask/chain-branch"

        # Create a branch with some work
        mgr.create_branch(clone, branch)
        _git_commit(clone, "subtask.txt", "subtask work", "subtask commit")
        _git(["checkout", "main"], cwd=clone)

        # Advance origin/main via a second clone
        clone2 = str(tmp_path / "clone2")
        subprocess.run(["git", "clone", git_repo["remote"], clone2],
                       check=True, capture_output=True)
        _git_commit(clone2, "upstream.txt", "upstream", "upstream commit")
        _git(["push", "origin", "main"], cwd=clone2)

        # switch_to_branch should switch and rebase onto latest origin/main
        mgr.switch_to_branch(clone, branch)

        assert _current_branch(clone) == branch
        log = _git(["log", "--oneline"], cwd=clone)
        assert "subtask commit" in log
        assert "upstream commit" in log

    def test_switch_to_branch_rebase_conflict_aborts(self, git_repo, tmp_path):
        """If rebase conflicts during switch_to_branch, it aborts gracefully."""
        mgr = GitManager()
        clone = git_repo["clone"]
        branch = "subtask/conflict-branch"

        # Create a branch that modifies README.md
        mgr.create_branch(clone, branch)
        _git_commit(clone, "README.md", "branch version", "branch changes README")
        branch_sha = _head_sha(clone)
        _git(["checkout", "main"], cwd=clone)

        # Advance origin/main with conflicting change
        clone2 = str(tmp_path / "clone2")
        subprocess.run(["git", "clone", git_repo["remote"], clone2],
                       check=True, capture_output=True)
        _git_commit(clone2, "README.md", "upstream version", "upstream changes README")
        _git(["push", "origin", "main"], cwd=clone2)

        # switch_to_branch should not raise even if rebase conflicts
        mgr.switch_to_branch(clone, branch)

        assert _current_branch(clone) == branch
        # Branch should retain its original commit since rebase was aborted
        assert _head_sha(clone) == branch_sha

    def test_switch_to_branch_creates_new_if_missing(self, git_repo):
        """switch_to_branch() creates a new branch if it doesn't exist anywhere."""
        mgr = GitManager()
        clone = git_repo["clone"]

        mgr.switch_to_branch(clone, "brand-new-branch")
        assert _current_branch(clone) == "brand-new-branch"


class TestMergeBranchPullsBeforeMerge:
    """Tests for the fetch+reset behavior in merge_branch().

    Verifies that merge_branch() fetches and hard-resets the default branch
    to origin/<default_branch> before merging, so concurrent agents always
    merge against the latest remote state.
    """

    def test_merge_fetches_latest_before_merging(self, git_repo, tmp_path):
        """merge_branch should incorporate remote changes pushed by another agent."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Create a task branch with some work
        mgr.create_branch(clone, "task/merge-test")
        _git_commit(clone, "task-file.txt", "task work", "task commit")
        _git(["checkout", "main"], cwd=clone)

        # Advance origin/main via a second clone (simulating another agent)
        clone2 = str(tmp_path / "clone2")
        subprocess.run(["git", "clone", git_repo["remote"], clone2],
                       check=True, capture_output=True)
        other_sha = _git_commit(clone2, "other-agent.txt", "other work", "other agent commit")
        _git(["push", "origin", "main"], cwd=clone2)

        # Local clone's main is behind origin — merge_branch should fetch first
        result = mgr.merge_branch(clone, "task/merge-test")
        assert result is True

        # The merge should include the other agent's commit
        log = _git(["log", "--oneline"], cwd=clone)
        assert "other agent commit" in log
        assert "task commit" in log

    def test_merge_discards_stale_local_main(self, git_repo, tmp_path):
        """merge_branch should reset local main even if it has un-pushed commits."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Create a task branch with work
        mgr.create_branch(clone, "task/stale-main")
        _git_commit(clone, "task-file.txt", "task work", "task commit")

        # Go back to main and create a local-only commit (simulating a
        # previous failed merge-and-push that left main diverged)
        _git(["checkout", "main"], cwd=clone)
        _git_commit(clone, "stale-local.txt", "stale", "stale local commit")

        # merge_branch should hard-reset main to origin, discarding the
        # stale local commit, then merge the task branch
        result = mgr.merge_branch(clone, "task/stale-main")
        assert result is True

        log = _git(["log", "--oneline"], cwd=clone)
        assert "task commit" in log
        assert "stale local commit" not in log

    def test_merge_conflict_after_fetch(self, git_repo, tmp_path):
        """merge_branch should still return False on conflict even with fresh fetch."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Create a task branch that modifies README.md
        mgr.create_branch(clone, "task/conflict-merge")
        _git_commit(clone, "README.md", "task version", "task changes README")
        _git(["checkout", "main"], cwd=clone)

        # Advance origin/main with a conflicting change
        clone2 = str(tmp_path / "clone2")
        subprocess.run(["git", "clone", git_repo["remote"], clone2],
                       check=True, capture_output=True)
        _git_commit(clone2, "README.md", "other version", "other changes README")
        _git(["push", "origin", "main"], cwd=clone2)

        # merge_branch should fetch, reset, then fail on the conflicting merge
        result = mgr.merge_branch(clone, "task/conflict-merge")
        assert result is False

        # Should still be on main after abort
        assert _current_branch(clone) == "main"

    def test_merge_works_without_remote(self, tmp_path):
        """merge_branch should still work for repos without a remote (LINK repos)."""
        mgr = GitManager()
        local_repo = str(tmp_path / "local-repo")
        mgr.init_repo(local_repo)

        # Create a branch with work
        mgr.create_branch(local_repo, "task/local-only")
        _git_commit(local_repo, "work.txt", "work", "local work")
        _git(["checkout", "master"], cwd=local_repo)

        # merge_branch should succeed even though fetch fails (no remote)
        result = mgr.merge_branch(local_repo, "task/local-only", default_branch="master")
        assert result is True

        log = _git(["log", "--oneline"], cwd=local_repo)
        assert "local work" in log
