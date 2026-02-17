import subprocess
import pytest
from src.git.manager import GitManager


@pytest.fixture
def git_repo(tmp_path):
    """Create a bare remote + working clone for testing."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                   capture_output=True)
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
