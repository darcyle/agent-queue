import pathlib
import subprocess
import pytest
from src.git.manager import GitManager, GitError


def _git(args: list[str], cwd: str) -> str:
    """Run a git command in the given directory, returning stdout."""
    result = subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _commit_file(clone: str, filename: str, content: str, message: str) -> str:
    """Create/overwrite a file, commit it, and return the new commit SHA."""
    pathlib.Path(clone, filename).write_text(content)
    _git(["add", filename], cwd=clone)
    _git(["-c", "user.name=Test", "-c", "user.email=t@t.com",
          "commit", "-m", message], cwd=clone)
    return _git(["rev-parse", "HEAD"], cwd=clone)


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
    """Tests for the hard-reset path in prepare_for_task (normal clone)."""

    def test_hard_reset_matches_remote_after_local_divergence(self, git_repo, tmp_path):
        """When local main has diverged (e.g. un-pushed merge commit),
        prepare_for_task should still succeed and create a branch from
        the remote state of main."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Simulate local divergence: make a local-only commit on main
        _commit_file(clone, "local-only.txt", "diverged", "local divergence")
        local_main_sha = _head_sha(clone)

        # The remote main doesn't have this commit
        remote_main_sha = _git(["rev-parse", "origin/main"], cwd=clone)
        assert local_main_sha != remote_main_sha

        # prepare_for_task should hard-reset main and create branch from remote
        mgr.prepare_for_task(clone, "task-1/test-reset")

        assert _current_branch(clone) == "task-1/test-reset"

        # The task branch should be based on origin/main, not the diverged local
        branch_base = _git(["merge-base", "origin/main", "HEAD"], cwd=clone)
        assert branch_base == remote_main_sha

    def test_hard_reset_picks_up_new_remote_commits(self, git_repo, tmp_path):
        """If origin/main advances after initial clone, prepare_for_task
        should create the new branch from the updated remote state."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Make a second clone to push a new commit to origin/main
        pusher = str(tmp_path / "pusher")
        subprocess.run(["git", "clone", git_repo["remote"], pusher],
                       check=True, capture_output=True)
        new_sha = _commit_file(pusher, "new-file.txt", "from pusher", "advance main")
        _git(["push", "origin", "main"], cwd=pusher)

        # The original clone's local main is now behind origin
        old_sha = _head_sha(clone)
        assert old_sha != new_sha

        # prepare_for_task should fetch and hard-reset to the new remote HEAD
        mgr.prepare_for_task(clone, "task-2/after-advance")
        assert _current_branch(clone) == "task-2/after-advance"

        # Branch parent should be the new remote commit
        branch_base = _git(["merge-base", "origin/main", "HEAD"], cwd=clone)
        assert branch_base == new_sha


class TestPrepareForTaskRebaseOnRetry:
    """Tests for the rebase-on-retry behavior when branch already exists."""

    def test_retry_rebases_existing_branch_onto_latest_main(self, git_repo, tmp_path):
        """When a task branch already exists (retry), prepare_for_task should
        switch to it and rebase it onto origin/main."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # First call: create the task branch and make a commit on it
        mgr.prepare_for_task(clone, "task-retry/feature")
        assert _current_branch(clone) == "task-retry/feature"
        original_base = _git(["merge-base", "origin/main", "HEAD"], cwd=clone)
        _commit_file(clone, "work.txt", "agent work", "agent commit")

        # Advance origin/main via a second clone
        pusher = str(tmp_path / "pusher")
        subprocess.run(["git", "clone", git_repo["remote"], pusher],
                       check=True, capture_output=True)
        new_main_sha = _commit_file(pusher, "upstream.txt", "upstream", "upstream commit")
        _git(["push", "origin", "main"], cwd=pusher)

        # Second call (retry): should switch to existing branch and rebase
        mgr.prepare_for_task(clone, "task-retry/feature")
        assert _current_branch(clone) == "task-retry/feature"

        # After rebase, the branch should be based on the new main
        new_base = _git(["merge-base", "origin/main", "HEAD"], cwd=clone)
        assert new_base == new_main_sha

        # The agent's work commit should still be present
        log = _git(["log", "--oneline"], cwd=clone)
        assert "agent commit" in log

    def test_retry_with_conflict_leaves_branch_intact(self, git_repo, tmp_path):
        """When rebase on retry conflicts, the branch should be left as-is
        (rebase aborted) and the agent can still work with it."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # First call: create branch and modify README.md (will conflict)
        mgr.prepare_for_task(clone, "task-conflict/feature")
        _commit_file(clone, "README.md", "agent version", "agent edits README")
        agent_sha = _head_sha(clone)

        # Advance origin/main with a conflicting change to the same file
        pusher = str(tmp_path / "pusher")
        subprocess.run(["git", "clone", git_repo["remote"], pusher],
                       check=True, capture_output=True)
        _commit_file(pusher, "README.md", "upstream version", "upstream edits README")
        _git(["push", "origin", "main"], cwd=pusher)

        # Second call (retry): rebase will conflict and should be aborted
        mgr.prepare_for_task(clone, "task-conflict/feature")
        assert _current_branch(clone) == "task-conflict/feature"

        # Branch should still have the agent's commit (rebase aborted)
        assert _head_sha(clone) == agent_sha
        content = pathlib.Path(clone, "README.md").read_text()
        assert content == "agent version"

    def test_retry_worktree_rebases_existing_branch(self, git_repo, tmp_path):
        """Worktree retry path should also rebase onto origin/main."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Create a worktree and prepare a task branch
        worktree_path = str(tmp_path / "worktree-retry")
        mgr.create_worktree(clone, worktree_path, "wt-init")
        mgr.prepare_for_task(worktree_path, "task-wt/retry-test")
        assert _current_branch(worktree_path) == "task-wt/retry-test"
        _commit_file(worktree_path, "wt-work.txt", "worktree work", "wt agent commit")

        # Advance origin/main
        pusher = str(tmp_path / "pusher")
        subprocess.run(["git", "clone", git_repo["remote"], pusher],
                       check=True, capture_output=True)
        new_main_sha = _commit_file(pusher, "upstream2.txt", "upstream", "upstream advance")
        _git(["push", "origin", "main"], cwd=pusher)

        # Retry: should rebase the existing branch
        mgr.prepare_for_task(worktree_path, "task-wt/retry-test")
        assert _current_branch(worktree_path) == "task-wt/retry-test"

        new_base = _git(["merge-base", "origin/main", "HEAD"], cwd=worktree_path)
        assert new_base == new_main_sha

        log = _git(["log", "--oneline"], cwd=worktree_path)
        assert "wt agent commit" in log


class TestPullLatestMain:
    """Tests for the pull_latest_main() convenience method."""

    def test_pull_latest_main_resets_to_remote(self, git_repo, tmp_path):
        """pull_latest_main should fetch and hard-reset to origin/main."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Advance origin/main via a second clone
        pusher = str(tmp_path / "pusher")
        subprocess.run(["git", "clone", git_repo["remote"], pusher],
                       check=True, capture_output=True)
        new_sha = _commit_file(pusher, "new.txt", "new", "push new")
        _git(["push", "origin", "main"], cwd=pusher)

        # Local clone is behind
        assert _head_sha(clone) != new_sha

        mgr.pull_latest_main(clone)
        assert _head_sha(clone) == new_sha

    def test_pull_latest_main_overwrites_local_divergence(self, git_repo):
        """pull_latest_main should overwrite local-only commits on main."""
        mgr = GitManager()
        clone = git_repo["clone"]

        remote_sha = _git(["rev-parse", "origin/main"], cwd=clone)

        # Create a local-only commit
        _commit_file(clone, "local.txt", "local", "local only")
        assert _head_sha(clone) != remote_sha

        mgr.pull_latest_main(clone)
        assert _head_sha(clone) == remote_sha


class TestSwitchToBranchRebase:
    """Tests for the rebase-onto-default behavior in switch_to_branch."""

    def test_switch_to_branch_rebases_onto_main(self, git_repo, tmp_path):
        """After switching to a branch, it should be rebased onto origin/main."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Create a feature branch with a commit
        _git(["checkout", "-b", "feature/subtask"], cwd=clone)
        _commit_file(clone, "feature.txt", "feature", "feature work")
        _git(["checkout", "main"], cwd=clone)

        # Advance origin/main
        pusher = str(tmp_path / "pusher")
        subprocess.run(["git", "clone", git_repo["remote"], pusher],
                       check=True, capture_output=True)
        new_main_sha = _commit_file(pusher, "upstream.txt", "upstream", "advance main")
        _git(["push", "origin", "main"], cwd=pusher)

        # switch_to_branch should rebase feature onto new main
        mgr.switch_to_branch(clone, "feature/subtask")
        assert _current_branch(clone) == "feature/subtask"

        new_base = _git(["merge-base", "origin/main", "HEAD"], cwd=clone)
        assert new_base == new_main_sha

        log = _git(["log", "--oneline"], cwd=clone)
        assert "feature work" in log

    def test_switch_to_branch_conflict_leaves_branch_intact(self, git_repo, tmp_path):
        """If rebase during switch conflicts, branch should be left as-is."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Create a feature branch that edits README.md
        _git(["checkout", "-b", "feature/conflict"], cwd=clone)
        _commit_file(clone, "README.md", "feature version", "feature edits README")
        feature_sha = _head_sha(clone)
        _git(["checkout", "main"], cwd=clone)

        # Advance origin/main with conflicting README.md change
        pusher = str(tmp_path / "pusher")
        subprocess.run(["git", "clone", git_repo["remote"], pusher],
                       check=True, capture_output=True)
        _commit_file(pusher, "README.md", "upstream version", "upstream edits README")
        _git(["push", "origin", "main"], cwd=pusher)

        # switch_to_branch: rebase will conflict, should abort gracefully
        mgr.switch_to_branch(clone, "feature/conflict")
        assert _current_branch(clone) == "feature/conflict"
        # Branch should still have the feature commit (rebase aborted)
        assert _head_sha(clone) == feature_sha


class TestMergeBranchPullBeforeMerge:
    """Tests for merge_branch pulling latest main before merging (G1 fix)."""

    def test_merge_succeeds_when_remote_main_advanced(self, git_repo, tmp_path):
        """merge_branch should fetch + reset to origin/main before merging,
        so the merge incorporates the latest remote changes even if the
        local default branch was behind."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Create a task branch with a commit
        mgr.prepare_for_task(clone, "task-merge/feature")
        _commit_file(clone, "feature.txt", "feature work", "add feature")

        # Advance origin/main via a second clone (simulates another agent)
        pusher = str(tmp_path / "pusher")
        subprocess.run(["git", "clone", git_repo["remote"], pusher],
                       check=True, capture_output=True)
        new_main_sha = _commit_file(pusher, "other.txt", "other work", "other agent")
        _git(["push", "origin", "main"], cwd=pusher)

        # Local main is now behind origin/main. merge_branch should
        # pull the latest before merging.
        result = mgr.merge_branch(clone, "task-merge/feature")
        assert result is True
        assert _current_branch(clone) == "main"

        # main should now contain both the remote advance and the feature
        log = _git(["log", "--oneline"], cwd=clone)
        assert "other agent" in log
        assert "add feature" in log

    def test_merge_conflict_after_pull(self, git_repo, tmp_path):
        """When the freshly-pulled main conflicts with the task branch,
        merge_branch should abort and return False."""
        mgr = GitManager()
        clone = git_repo["clone"]

        # Create a task branch that modifies README.md
        mgr.prepare_for_task(clone, "task-conflict/merge")
        _commit_file(clone, "README.md", "branch version", "branch edits README")

        # Advance origin/main with a conflicting change to same file
        pusher = str(tmp_path / "pusher")
        subprocess.run(["git", "clone", git_repo["remote"], pusher],
                       check=True, capture_output=True)
        _commit_file(pusher, "README.md", "remote version", "remote edits README")
        _git(["push", "origin", "main"], cwd=pusher)

        # merge_branch should pull, hit conflict, abort, and return False
        result = mgr.merge_branch(clone, "task-conflict/merge")
        assert result is False
        assert _current_branch(clone) == "main"

        # main should match remote (the hard-reset state, not the merge)
        content = pathlib.Path(clone, "README.md").read_text()
        assert content == "remote version"

    def test_merge_without_remote_advance_still_works(self, git_repo):
        """Normal case: merge when local main is already up to date
        should still succeed (fetch + reset is a no-op)."""
        mgr = GitManager()
        clone = git_repo["clone"]

        mgr.prepare_for_task(clone, "task-normal/merge")
        _commit_file(clone, "normal.txt", "normal work", "normal commit")

        result = mgr.merge_branch(clone, "task-normal/merge")
        assert result is True
        assert _current_branch(clone) == "main"

        log = _git(["log", "--oneline"], cwd=clone)
        assert "normal commit" in log
