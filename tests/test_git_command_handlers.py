"""Unit tests for git-related command handler methods.

Tests each new command handler (_cmd_create_branch, _cmd_checkout_branch,
_cmd_commit_changes, _cmd_push_branch, _cmd_merge_branch, _cmd_git_log,
_cmd_git_diff) with mocked GitManager and Database to test command handler
logic in isolation.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.command_handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.git.manager import GitError, GitManager
from src.models import Project, RepoConfig, RepoSourceType, TaskStatus
from src.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    """Create a real in-memory database for tests."""
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "test.db"),
    )


@pytest.fixture
def mock_git():
    """Create a mock GitManager with sensible defaults."""
    git = MagicMock(spec=GitManager)
    git.validate_checkout.return_value = True
    git.get_current_branch.return_value = "main"
    git.get_recent_commits.return_value = "abc1234 Initial commit"
    git.get_diff.return_value = "diff --git a/file.py b/file.py"
    git._run.return_value = ""
    git.create_branch.return_value = None
    git.checkout_branch.return_value = None
    git.commit_all.return_value = True
    git.push_branch.return_value = None
    git.merge_branch.return_value = True
    return git


@pytest.fixture
async def handler(db, config, mock_git):
    """Create a CommandHandler with a mocked GitManager."""
    orchestrator = Orchestrator(config)
    orchestrator.db = db
    orchestrator.git = mock_git
    return CommandHandler(orchestrator, config)


@pytest.fixture
async def project_with_repo(db, tmp_path):
    """Create a test project and linked repo in the database.

    Returns (project_id, repo_id, checkout_path).
    """
    project_id = "test-proj"
    repo_id = "test-repo"
    checkout_path = str(tmp_path / "workspaces" / "test-proj")

    # Create the directory so _resolve_repo_path doesn't fail the isdir check
    import os
    os.makedirs(checkout_path, exist_ok=True)

    await db.create_project(Project(
        id=project_id,
        name="Test Project",
        workspace_path=checkout_path,
    ))
    await db.create_repo(RepoConfig(
        id=repo_id,
        project_id=project_id,
        source_type=RepoSourceType.LINK,
        source_path=checkout_path,
        default_branch="main",
    ))
    return project_id, repo_id, checkout_path


# ---------------------------------------------------------------------------
# test_create_branch
# ---------------------------------------------------------------------------


class TestCreateBranch:
    """Tests for _cmd_create_branch."""

    async def test_success(self, handler, mock_git, project_with_repo):
        project_id, repo_id, checkout_path = project_with_repo

        result = await handler.execute("create_branch", {
            "project_id": project_id,
            "branch_name": "feature/new-thing",
        })

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["branch"] == "feature/new-thing"
        assert result["status"] == "created"
        mock_git.create_branch.assert_called_once_with(
            checkout_path, "feature/new-thing",
        )

    async def test_missing_branch_name(self, handler, project_with_repo):
        project_id, repo_id, _ = project_with_repo

        result = await handler.execute("create_branch", {
            "project_id": project_id,
        })

        assert result == {"error": "branch_name is required"}

    async def test_invalid_project(self, handler):
        result = await handler.execute("create_branch", {
            "project_id": "nonexistent",
            "branch_name": "feature/x",
        })

        assert "error" in result
        assert "not found" in result["error"]

    async def test_git_error(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.create_branch.side_effect = GitError("branch already exists")

        result = await handler.execute("create_branch", {
            "project_id": project_id,
            "branch_name": "feature/dup",
        })

        assert "error" in result
        assert "branch already exists" in result["error"]


# ---------------------------------------------------------------------------
# test_checkout_branch
# ---------------------------------------------------------------------------


class TestCheckoutBranch:
    """Tests for _cmd_checkout_branch."""

    async def test_success(self, handler, mock_git, project_with_repo):
        project_id, repo_id, checkout_path = project_with_repo

        result = await handler.execute("checkout_branch", {
            "project_id": project_id,
            "branch_name": "feature/existing",
        })

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["branch"] == "feature/existing"
        assert result["status"] == "checked_out"
        mock_git.checkout_branch.assert_called_once_with(
            checkout_path, "feature/existing",
        )

    async def test_branch_not_found(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.checkout_branch.side_effect = GitError(
            "error: pathspec 'no-such-branch' did not match any file(s) known to git"
        )

        result = await handler.execute("checkout_branch", {
            "project_id": project_id,
            "branch_name": "no-such-branch",
        })

        assert "error" in result
        assert "no-such-branch" in result["error"]

    async def test_missing_branch_name(self, handler, project_with_repo):
        project_id, _, _ = project_with_repo

        result = await handler.execute("checkout_branch", {
            "project_id": project_id,
        })

        assert result == {"error": "branch_name is required"}

    async def test_invalid_project(self, handler):
        result = await handler.execute("checkout_branch", {
            "project_id": "nonexistent",
            "branch_name": "main",
        })

        assert "error" in result
        assert "not found" in result["error"]

    async def test_warns_if_tasks_in_progress(self, handler, db, mock_git, project_with_repo):
        """When tasks are IN_PROGRESS, result should include a warning."""
        project_id, _, _ = project_with_repo

        # Create an IN_PROGRESS task for this project
        from src.models import Task
        task = Task(
            id="task-1",
            project_id=project_id,
            title="Running task",
            description="A task that is running",
            status=TaskStatus.IN_PROGRESS,
        )
        await db.create_task(task)

        result = await handler.execute("checkout_branch", {
            "project_id": project_id,
            "branch_name": "feature/switch",
        })

        assert "error" not in result
        assert result["status"] == "checked_out"
        assert "warning" in result
        assert "IN_PROGRESS" in result["warning"]


# ---------------------------------------------------------------------------
# test_commit_changes
# ---------------------------------------------------------------------------


class TestCommitChanges:
    """Tests for _cmd_commit_changes."""

    async def test_success(self, handler, mock_git, project_with_repo):
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.commit_all.return_value = True

        result = await handler.execute("commit_changes", {
            "project_id": project_id,
            "message": "feat: add new feature",
        })

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["commit_message"] == "feat: add new feature"
        assert result["status"] == "committed"
        mock_git.commit_all.assert_called_once_with(
            checkout_path, "feat: add new feature",
        )

    async def test_nothing_to_commit(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.commit_all.return_value = False  # nothing to commit

        result = await handler.execute("commit_changes", {
            "project_id": project_id,
            "message": "empty commit",
        })

        assert "error" not in result
        assert result["status"] == "nothing_to_commit"
        assert "No changes" in result["message"]

    async def test_missing_message(self, handler, project_with_repo):
        project_id, _, _ = project_with_repo

        result = await handler.execute("commit_changes", {
            "project_id": project_id,
        })

        assert result == {"error": "message is required"}

    async def test_invalid_project(self, handler):
        result = await handler.execute("commit_changes", {
            "project_id": "nonexistent",
            "message": "some message",
        })

        assert "error" in result
        assert "not found" in result["error"]

    async def test_git_error(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.commit_all.side_effect = GitError("failed to commit")

        result = await handler.execute("commit_changes", {
            "project_id": project_id,
            "message": "a commit",
        })

        assert "error" in result
        assert "failed to commit" in result["error"]

    async def test_warns_if_tasks_in_progress(self, handler, db, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.commit_all.return_value = True

        from src.models import Task
        task = Task(
            id="task-running",
            project_id=project_id,
            title="Active task",
            description="Running",
            status=TaskStatus.IN_PROGRESS,
        )
        await db.create_task(task)

        result = await handler.execute("commit_changes", {
            "project_id": project_id,
            "message": "fix: something",
        })

        assert "error" not in result
        assert result["status"] == "committed"
        assert "warning" in result
        assert "IN_PROGRESS" in result["warning"]


# ---------------------------------------------------------------------------
# test_push_branch
# ---------------------------------------------------------------------------


class TestPushBranch:
    """Tests for _cmd_push_branch."""

    async def test_success_with_explicit_branch(self, handler, mock_git, project_with_repo):
        project_id, repo_id, checkout_path = project_with_repo

        result = await handler.execute("push_branch", {
            "project_id": project_id,
            "branch_name": "feature/push-me",
        })

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["branch"] == "feature/push-me"
        assert result["status"] == "pushed"
        mock_git.push_branch.assert_called_once_with(
            checkout_path, "feature/push-me",
        )

    async def test_success_with_current_branch(self, handler, mock_git, project_with_repo):
        """When branch_name is not provided, uses current branch."""
        project_id, _, checkout_path = project_with_repo
        mock_git.get_current_branch.return_value = "feature/auto-detect"

        result = await handler.execute("push_branch", {
            "project_id": project_id,
        })

        assert "error" not in result
        assert result["branch"] == "feature/auto-detect"
        assert result["status"] == "pushed"
        mock_git.get_current_branch.assert_called_with(checkout_path)
        mock_git.push_branch.assert_called_once_with(
            checkout_path, "feature/auto-detect",
        )

    async def test_push_failure(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.push_branch.side_effect = GitError(
            "git push origin feature/x failed: rejected"
        )

        result = await handler.execute("push_branch", {
            "project_id": project_id,
            "branch_name": "feature/x",
        })

        assert "error" in result
        assert "rejected" in result["error"]

    async def test_cannot_determine_current_branch(self, handler, mock_git, project_with_repo):
        """When no branch_name given and current branch can't be determined."""
        project_id, _, _ = project_with_repo
        mock_git.get_current_branch.return_value = ""

        result = await handler.execute("push_branch", {
            "project_id": project_id,
        })

        assert "error" in result
        assert "Could not determine current branch" in result["error"]

    async def test_invalid_project(self, handler):
        result = await handler.execute("push_branch", {
            "project_id": "nonexistent",
            "branch_name": "main",
        })

        assert "error" in result
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# test_merge_branch
# ---------------------------------------------------------------------------


class TestMergeBranch:
    """Tests for _cmd_merge_branch."""

    async def test_success(self, handler, mock_git, project_with_repo):
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.merge_branch.return_value = True

        result = await handler.execute("merge_branch", {
            "project_id": project_id,
            "branch_name": "feature/done",
        })

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["branch"] == "feature/done"
        assert result["target"] == "main"
        assert result["status"] == "merged"
        mock_git.merge_branch.assert_called_once_with(
            checkout_path, "feature/done", "main",
        )

    async def test_conflict_scenario(self, handler, mock_git, project_with_repo):
        project_id, _, checkout_path = project_with_repo
        mock_git.merge_branch.return_value = False  # conflict

        result = await handler.execute("merge_branch", {
            "project_id": project_id,
            "branch_name": "feature/conflicting",
        })

        assert "error" not in result  # conflict is not an error, just a status
        assert result["status"] == "conflict"
        assert "conflict" in result["message"].lower()
        assert result["branch"] == "feature/conflicting"
        assert result["target"] == "main"

    async def test_missing_branch_name(self, handler, project_with_repo):
        project_id, _, _ = project_with_repo

        result = await handler.execute("merge_branch", {
            "project_id": project_id,
        })

        assert result == {"error": "branch_name is required"}

    async def test_invalid_project(self, handler):
        result = await handler.execute("merge_branch", {
            "project_id": "nonexistent",
            "branch_name": "feature/x",
        })

        assert "error" in result
        assert "not found" in result["error"]

    async def test_git_error(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.merge_branch.side_effect = GitError("fatal: not a git repo")

        result = await handler.execute("merge_branch", {
            "project_id": project_id,
            "branch_name": "feature/bad",
        })

        assert "error" in result
        assert "not a git repo" in result["error"]

    async def test_uses_repo_default_branch(self, handler, db, mock_git, tmp_path):
        """Merge should use the repo's configured default branch, not hardcoded 'main'."""
        import os

        project_id = "proj-develop"
        checkout_path = str(tmp_path / "workspaces" / "proj-develop")
        os.makedirs(checkout_path, exist_ok=True)

        await db.create_project(Project(
            id=project_id,
            name="Develop Project",
            workspace_path=checkout_path,
        ))
        await db.create_repo(RepoConfig(
            id="repo-develop",
            project_id=project_id,
            source_type=RepoSourceType.LINK,
            source_path=checkout_path,
            default_branch="develop",
        ))

        mock_git.merge_branch.return_value = True

        result = await handler.execute("merge_branch", {
            "project_id": project_id,
            "branch_name": "feature/custom-default",
        })

        assert "error" not in result
        assert result["target"] == "develop"
        mock_git.merge_branch.assert_called_once_with(
            checkout_path, "feature/custom-default", "develop",
        )

    async def test_warns_if_tasks_in_progress(self, handler, db, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.merge_branch.return_value = True

        from src.models import Task
        await db.create_task(Task(
            id="task-active",
            project_id=project_id,
            title="Active",
            description="Running",
            status=TaskStatus.IN_PROGRESS,
        ))

        result = await handler.execute("merge_branch", {
            "project_id": project_id,
            "branch_name": "feature/merge-warn",
        })

        assert "error" not in result
        assert result["status"] == "merged"
        assert "warning" in result
        assert "IN_PROGRESS" in result["warning"]


# ---------------------------------------------------------------------------
# test_git_log
# ---------------------------------------------------------------------------


class TestGitLog:
    """Tests for _cmd_git_log."""

    async def test_success(self, handler, mock_git, project_with_repo):
        project_id, repo_id, checkout_path = project_with_repo
        mock_git.get_recent_commits.return_value = (
            "abc1234 feat: add feature\n"
            "def5678 fix: bug fix"
        )
        mock_git.get_current_branch.return_value = "feature/test"

        result = await handler.execute("git_log", {
            "project_id": project_id,
        })

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["branch"] == "feature/test"
        assert "abc1234" in result["log"]
        assert "def5678" in result["log"]
        # Default count is 10
        mock_git.get_recent_commits.assert_called_once_with(
            checkout_path, count=10,
        )

    async def test_custom_count(self, handler, mock_git, project_with_repo):
        project_id, _, checkout_path = project_with_repo
        mock_git.get_recent_commits.return_value = "abc1234 commit"

        result = await handler.execute("git_log", {
            "project_id": project_id,
            "count": 3,
        })

        assert "error" not in result
        mock_git.get_recent_commits.assert_called_once_with(
            checkout_path, count=3,
        )

    async def test_empty_log(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git.get_recent_commits.return_value = ""

        result = await handler.execute("git_log", {
            "project_id": project_id,
        })

        assert "error" not in result
        assert result["log"] == "(no commits)"

    async def test_invalid_project(self, handler):
        result = await handler.execute("git_log", {
            "project_id": "nonexistent",
        })

        assert "error" in result
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# test_git_diff
# ---------------------------------------------------------------------------


class TestGitDiff:
    """Tests for _cmd_git_diff."""

    async def test_working_tree_diff(self, handler, mock_git, project_with_repo):
        """Without base_branch, shows working tree diff (unstaged changes)."""
        project_id, repo_id, checkout_path = project_with_repo
        mock_git._run.return_value = "diff --git a/file.py b/file.py\n+new line"

        result = await handler.execute("git_diff", {
            "project_id": project_id,
        })

        assert "error" not in result
        assert result["project_id"] == project_id
        assert result["base_branch"] == "(working tree)"
        assert "new line" in result["diff"]
        mock_git._run.assert_called_once_with(["diff"], cwd=checkout_path)

    async def test_diff_against_base_branch(self, handler, mock_git, project_with_repo):
        project_id, _, checkout_path = project_with_repo
        mock_git.get_diff.return_value = (
            "diff --git a/src/app.py b/src/app.py\n"
            "+import new_module"
        )

        result = await handler.execute("git_diff", {
            "project_id": project_id,
            "base_branch": "main",
        })

        assert "error" not in result
        assert result["base_branch"] == "main"
        assert "new_module" in result["diff"]
        mock_git.get_diff.assert_called_once_with(checkout_path, "main")

    async def test_no_changes(self, handler, mock_git, project_with_repo):
        project_id, _, checkout_path = project_with_repo
        mock_git._run.return_value = ""

        result = await handler.execute("git_diff", {
            "project_id": project_id,
        })

        assert "error" not in result
        assert result["diff"] == "(no changes)"

    async def test_git_error(self, handler, mock_git, project_with_repo):
        project_id, _, _ = project_with_repo
        mock_git._run.side_effect = GitError("fatal: bad revision")

        result = await handler.execute("git_diff", {
            "project_id": project_id,
        })

        assert "error" in result
        assert "bad revision" in result["error"]

    async def test_invalid_project(self, handler):
        result = await handler.execute("git_diff", {
            "project_id": "nonexistent",
        })

        assert "error" in result
        assert "not found" in result["error"]

    async def test_diff_against_base_branch_git_error(self, handler, mock_git, project_with_repo):
        """GitError when diffing against a base branch."""
        project_id, _, _ = project_with_repo
        mock_git.get_diff.side_effect = GitError("fatal: bad object 'develop'")

        result = await handler.execute("git_diff", {
            "project_id": project_id,
            "base_branch": "develop",
        })

        assert "error" in result
        assert "bad object" in result["error"]


# ---------------------------------------------------------------------------
# test_resolve_repo_path edge cases
# ---------------------------------------------------------------------------


class TestResolveRepoPath:
    """Edge cases for _resolve_repo_path used by all commands."""

    async def test_missing_project_id(self, handler):
        """Commands that require project_id should error without it."""
        result = await handler.execute("create_branch", {
            "branch_name": "feature/orphan",
        })

        assert "error" in result
        assert "project_id" in result["error"].lower() or "required" in result["error"].lower()

    async def test_with_specific_repo_id(self, handler, db, mock_git, project_with_repo, tmp_path):
        """Commands should work when repo_id is explicitly specified."""
        project_id, repo_id, checkout_path = project_with_repo

        result = await handler.execute("create_branch", {
            "project_id": project_id,
            "repo_id": repo_id,
            "branch_name": "feature/via-repo-id",
        })

        assert "error" not in result
        assert result["status"] == "created"
        mock_git.create_branch.assert_called_once_with(
            checkout_path, "feature/via-repo-id",
        )
