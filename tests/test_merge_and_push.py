"""Tests for the _merge_and_push retry logic in the Orchestrator.

These tests verify that:
- A successful merge-and-push works as before.
- When the push is rejected (another agent pushed first), the orchestrator
  retries the merge-then-push cycle up to _max_retries times.
- After exhausting retries, a notification is sent.
- LINK repos (no remote) skip the push/retry entirely.
- Merge conflicts are reported immediately without retrying.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.git.manager import GitError, GitManager
from src.models import RepoConfig, RepoSourceType, Task, TaskStatus


def _make_task(**overrides) -> Task:
    defaults = dict(
        id="test-task",
        project_id="proj-1",
        title="Test Task",
        description="desc",
        branch_name="task/test-branch",
    )
    defaults.update(overrides)
    return Task(**defaults)


def _make_repo(source_type=RepoSourceType.CLONE, **overrides) -> RepoConfig:
    defaults = dict(
        id="repo-1",
        project_id="proj-1",
        source_type=source_type,
        url="https://github.com/test/repo.git",
        default_branch="main",
    )
    defaults.update(overrides)
    return RepoConfig(**defaults)


class _FakeOrchestrator:
    """Minimal stand-in for Orchestrator to test _merge_and_push in isolation.

    We import the real method and bind it, rather than instantiating the full
    Orchestrator (which requires DB, config, adapters, etc.).
    """

    def __init__(self, git: GitManager):
        self.git = git
        self._notifications: list[str] = []

    async def _notify_channel(self, message: str, *, project_id: str | None = None):
        self._notifications.append(message)

    # Bind the real method from Orchestrator
    from src.orchestrator import Orchestrator as _Orch
    _merge_and_push = _Orch._merge_and_push


class TestMergeAndPushRetry:
    """Tests for the retry loop in _merge_and_push."""

    @pytest.fixture
    def git(self):
        """Return a mock GitManager."""
        return MagicMock(spec=GitManager)

    @pytest.fixture
    def orch(self, git):
        return _FakeOrchestrator(git)

    @pytest.mark.asyncio
    async def test_successful_merge_and_push(self, orch, git):
        """Happy path: merge succeeds, push succeeds on first attempt."""
        git.merge_branch.return_value = True
        task = _make_task()
        repo = _make_repo()

        await orch._merge_and_push(task, repo, "/workspace")

        git.merge_branch.assert_called_once_with("/workspace", "task/test-branch", "main")
        git.push_branch.assert_called_once_with("/workspace", "main")
        git.delete_branch.assert_called_once()
        assert not orch._notifications

    @pytest.mark.asyncio
    async def test_merge_conflict_no_retry(self, orch, git):
        """Merge conflict should notify immediately without retrying push."""
        git.merge_branch.return_value = False
        task = _make_task()
        repo = _make_repo()

        await orch._merge_and_push(task, repo, "/workspace")

        git.merge_branch.assert_called_once()
        git.push_branch.assert_not_called()
        git.delete_branch.assert_not_called()
        assert len(orch._notifications) == 1
        assert "Merge Conflict" in orch._notifications[0]

    @pytest.mark.asyncio
    async def test_push_rejected_retries_and_succeeds(self, orch, git):
        """Push failure should trigger retry; second attempt succeeds."""
        git.merge_branch.return_value = True
        git.push_branch.side_effect = [GitError("rejected"), None]  # fail then succeed
        task = _make_task()
        repo = _make_repo()

        await orch._merge_and_push(task, repo, "/workspace")

        # merge_branch called twice (once per attempt)
        assert git.merge_branch.call_count == 2
        assert git.push_branch.call_count == 2
        # Checkout back to task branch between retries
        git.checkout_branch.assert_called_once_with("/workspace", "task/test-branch")
        # Should still clean up the branch after final success
        git.delete_branch.assert_called_once()
        # No failure notification since it eventually succeeded
        assert not orch._notifications

    @pytest.mark.asyncio
    async def test_push_exhausts_retries(self, orch, git):
        """After _max_retries push failures, a notification is sent."""
        git.merge_branch.return_value = True
        git.push_branch.side_effect = GitError("rejected")
        task = _make_task()
        repo = _make_repo()

        await orch._merge_and_push(task, repo, "/workspace", _max_retries=3)

        assert git.merge_branch.call_count == 3
        assert git.push_branch.call_count == 3
        git.delete_branch.assert_not_called()  # never succeeded
        assert len(orch._notifications) == 1
        assert "Push Failed" in orch._notifications[0]
        assert "3 attempts" in orch._notifications[0]

    @pytest.mark.asyncio
    async def test_link_repo_no_push_no_retry(self, orch, git):
        """LINK repos should merge locally without pushing or retrying."""
        git.merge_branch.return_value = True
        task = _make_task()
        repo = _make_repo(source_type=RepoSourceType.LINK)

        await orch._merge_and_push(task, repo, "/workspace")

        git.merge_branch.assert_called_once()
        git.push_branch.assert_not_called()
        # Branch cleanup with delete_remote=False for LINK repos
        git.delete_branch.assert_called_once_with(
            "/workspace", "task/test-branch", delete_remote=False,
        )
        assert not orch._notifications

    @pytest.mark.asyncio
    async def test_merge_conflict_on_retry_notifies(self, orch, git):
        """If push fails and the retry merge also conflicts, notify about the conflict."""
        # First attempt: merge succeeds but push fails
        # Second attempt: merge conflicts
        git.merge_branch.side_effect = [True, False]
        git.push_branch.side_effect = GitError("rejected")
        task = _make_task()
        repo = _make_repo()

        await orch._merge_and_push(task, repo, "/workspace", _max_retries=3)

        assert git.merge_branch.call_count == 2
        assert len(orch._notifications) == 1
        assert "Merge Conflict" in orch._notifications[0]

    @pytest.mark.asyncio
    async def test_retry_checks_out_task_branch_first(self, orch, git):
        """On retry, we switch back to the task branch before re-merging."""
        git.merge_branch.return_value = True
        # Fail twice, succeed on third
        git.push_branch.side_effect = [GitError("rejected"), GitError("rejected"), None]
        task = _make_task()
        repo = _make_repo()

        await orch._merge_and_push(task, repo, "/workspace", _max_retries=3)

        # Should have checked out task branch before each retry (attempts 1 and 2)
        assert git.checkout_branch.call_count == 2
        git.checkout_branch.assert_called_with("/workspace", "task/test-branch")
