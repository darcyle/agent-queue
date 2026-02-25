"""Integration tests for CommandHandler._resolve_repo_path.

Tests the resolution logic that maps (project_id, repo_id) to a filesystem
checkout path.  Covers linked repos, cloned repos, init repos, workspace
fallback, and various error conditions.
"""

import os

import pytest

from src.command_handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.git.manager import GitManager
from src.models import Project, RepoConfig, RepoSourceType
from src.orchestrator import Orchestrator
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    """Create a real test database."""
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
    """Mock GitManager that treats all directories as valid git repos."""
    git = MagicMock(spec=GitManager)
    git.validate_checkout.return_value = True
    return git


@pytest.fixture
async def handler(db, config, mock_git):
    """Create a CommandHandler wired to real DB and mocked git."""
    orchestrator = Orchestrator(config)
    orchestrator.db = db
    orchestrator.git = mock_git
    return CommandHandler(orchestrator, config)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dir(path: str) -> str:
    """Create a directory on disk so isdir checks pass."""
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Happy-path resolution
# ---------------------------------------------------------------------------


class TestLinkedRepo:
    """Project with a LINK repo → returns checkout_base_path."""

    async def test_resolve_linked_repo_by_project_id(self, handler, db, tmp_path, mock_git):
        checkout = _make_dir(str(tmp_path / "linked-checkout"))

        await db.create_project(Project(id="p-link", name="Linked Project"))
        await db.create_repo(RepoConfig(
            id="r-link",
            project_id="p-link",
            source_type=RepoSourceType.LINK,
            source_path=checkout,
        ))

        path, repo, err = await handler._resolve_repo_path({"project_id": "p-link"})

        assert err is None
        assert path == checkout
        assert repo is not None
        assert repo.id == "r-link"
        assert repo.source_type == RepoSourceType.LINK
        mock_git.validate_checkout.assert_called_once_with(checkout)

    async def test_resolve_linked_repo_by_repo_id(self, handler, db, tmp_path, mock_git):
        """repo_id alone (without project_id) should still resolve."""
        checkout = _make_dir(str(tmp_path / "linked-only"))

        await db.create_project(Project(id="p-link2", name="Link2"))
        await db.create_repo(RepoConfig(
            id="r-link2",
            project_id="p-link2",
            source_type=RepoSourceType.LINK,
            source_path=checkout,
        ))

        path, repo, err = await handler._resolve_repo_path({"repo_id": "r-link2"})

        assert err is None
        assert path == checkout
        assert repo.id == "r-link2"

    async def test_resolve_linked_repo_with_both_ids(self, handler, db, tmp_path):
        """When both project_id and repo_id are given, repo_id takes precedence."""
        checkout = _make_dir(str(tmp_path / "linked-both"))

        await db.create_project(Project(id="p-both", name="Both IDs"))
        await db.create_repo(RepoConfig(
            id="r-both",
            project_id="p-both",
            source_type=RepoSourceType.LINK,
            source_path=checkout,
        ))

        path, repo, err = await handler._resolve_repo_path({
            "project_id": "p-both",
            "repo_id": "r-both",
        })

        assert err is None
        assert path == checkout
        assert repo.id == "r-both"


class TestClonedRepo:
    """Project with a CLONE repo → returns checkout_base_path."""

    async def test_resolve_cloned_repo(self, handler, db, tmp_path, mock_git):
        checkout = _make_dir(str(tmp_path / "clone-checkout"))

        await db.create_project(Project(id="p-clone", name="Cloned Project"))
        await db.create_repo(RepoConfig(
            id="r-clone",
            project_id="p-clone",
            source_type=RepoSourceType.CLONE,
            url="https://github.com/example/repo.git",
            checkout_base_path=checkout,
        ))

        path, repo, err = await handler._resolve_repo_path({"project_id": "p-clone"})

        assert err is None
        assert path == checkout
        assert repo is not None
        assert repo.id == "r-clone"
        assert repo.source_type == RepoSourceType.CLONE
        mock_git.validate_checkout.assert_called_once_with(checkout)

    async def test_resolve_cloned_repo_by_repo_id(self, handler, db, tmp_path):
        checkout = _make_dir(str(tmp_path / "clone-by-id"))

        await db.create_project(Project(id="p-clone2", name="Clone2"))
        await db.create_repo(RepoConfig(
            id="r-clone2",
            project_id="p-clone2",
            source_type=RepoSourceType.CLONE,
            url="https://github.com/example/repo.git",
            checkout_base_path=checkout,
        ))

        path, repo, err = await handler._resolve_repo_path({"repo_id": "r-clone2"})

        assert err is None
        assert path == checkout
        assert repo.source_type == RepoSourceType.CLONE


class TestInitRepo:
    """Project with an INIT repo → returns checkout_base_path."""

    async def test_resolve_init_repo(self, handler, db, tmp_path, mock_git):
        checkout = _make_dir(str(tmp_path / "init-checkout"))

        await db.create_project(Project(id="p-init", name="Init Project"))
        await db.create_repo(RepoConfig(
            id="r-init",
            project_id="p-init",
            source_type=RepoSourceType.INIT,
            checkout_base_path=checkout,
        ))

        path, repo, err = await handler._resolve_repo_path({"project_id": "p-init"})

        assert err is None
        assert path == checkout
        assert repo.id == "r-init"
        assert repo.source_type == RepoSourceType.INIT
        mock_git.validate_checkout.assert_called_once_with(checkout)


class TestWorkspaceFallback:
    """Project with no repos → falls back to workspace_path."""

    async def test_falls_back_to_workspace_path(self, handler, db, tmp_path, mock_git):
        workspace = _make_dir(str(tmp_path / "workspace-fallback"))

        await db.create_project(Project(
            id="p-norepo",
            name="No Repo Project",
            workspace_path=workspace,
        ))

        path, repo, err = await handler._resolve_repo_path({"project_id": "p-norepo"})

        assert err is None
        assert path == workspace
        assert repo is None  # No repo was resolved
        mock_git.validate_checkout.assert_called_once_with(workspace)

    async def test_workspace_path_missing_on_disk(self, handler, db, tmp_path):
        """workspace_path is set but directory doesn't exist → error."""
        nonexistent = str(tmp_path / "does-not-exist")

        await db.create_project(Project(
            id="p-nodir",
            name="No Dir",
            workspace_path=nonexistent,
        ))

        path, repo, err = await handler._resolve_repo_path({"project_id": "p-nodir"})

        assert path is None
        assert err is not None
        assert "no repos" in err["error"].lower() or "no valid workspace" in err["error"].lower()

    async def test_workspace_path_not_set(self, handler, db):
        """Project has no workspace_path and no repos → error."""
        await db.create_project(Project(
            id="p-empty",
            name="Empty Project",
            workspace_path=None,
        ))

        path, repo, err = await handler._resolve_repo_path({"project_id": "p-empty"})

        assert path is None
        assert err is not None
        assert "no repos" in err["error"].lower() or "no valid workspace" in err["error"].lower()


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestInvalidProject:
    """Invalid / non-existent project_id → returns error."""

    async def test_nonexistent_project(self, handler):
        path, repo, err = await handler._resolve_repo_path({
            "project_id": "nonexistent-project",
        })

        assert path is None
        assert repo is None
        assert err is not None
        assert "not found" in err["error"].lower()
        assert "nonexistent-project" in err["error"]

    async def test_empty_project_id_treated_as_missing(self, handler):
        """Empty string project_id with no repo_id → error."""
        path, repo, err = await handler._resolve_repo_path({
            "project_id": "",
        })

        assert path is None
        assert err is not None
        assert "project_id" in err["error"].lower() or "required" in err["error"].lower()


class TestInvalidRepoId:
    """Invalid / non-existent repo_id → returns error."""

    async def test_nonexistent_repo_id(self, handler, db):
        """repo_id that doesn't exist in DB → error."""
        await db.create_project(Project(id="p-exists", name="Exists"))

        path, repo, err = await handler._resolve_repo_path({
            "project_id": "p-exists",
            "repo_id": "nonexistent-repo",
        })

        assert path is None
        assert repo is None
        assert err is not None
        assert "not found" in err["error"].lower()
        assert "nonexistent-repo" in err["error"]

    async def test_repo_id_only_nonexistent(self, handler):
        """repo_id alone and it doesn't exist → error."""
        path, repo, err = await handler._resolve_repo_path({
            "repo_id": "ghost-repo",
        })

        assert path is None
        assert repo is None
        assert err is not None
        assert "not found" in err["error"].lower()
        assert "ghost-repo" in err["error"]


class TestMissingArgs:
    """Neither project_id nor repo_id supplied."""

    async def test_no_ids_at_all(self, handler):
        path, repo, err = await handler._resolve_repo_path({})

        assert path is None
        assert repo is None
        assert err is not None
        assert "project_id" in err["error"].lower() or "required" in err["error"].lower()

    async def test_empty_dict(self, handler):
        """Explicit empty dict."""
        path, repo, err = await handler._resolve_repo_path({})

        assert path is None
        assert err is not None


# ---------------------------------------------------------------------------
# Repo path validation
# ---------------------------------------------------------------------------


class TestPathValidation:
    """Path exists on disk but isn't a valid git repo, etc."""

    async def test_repo_path_not_on_disk(self, handler, db, tmp_path):
        """Repo configured with a path that doesn't exist on disk."""
        nonexistent = str(tmp_path / "vanished-checkout")

        await db.create_project(Project(id="p-vanish", name="Vanished"))
        await db.create_repo(RepoConfig(
            id="r-vanish",
            project_id="p-vanish",
            source_type=RepoSourceType.LINK,
            source_path=nonexistent,
        ))

        path, repo, err = await handler._resolve_repo_path({"project_id": "p-vanish"})

        assert path is None
        assert err is not None
        assert "path not found" in err["error"].lower() or "not found" in err["error"].lower()

    async def test_repo_path_not_a_git_repo(self, handler, db, tmp_path, mock_git):
        """Path exists but git.validate_checkout returns False."""
        checkout = _make_dir(str(tmp_path / "not-a-git-repo"))
        mock_git.validate_checkout.return_value = False

        await db.create_project(Project(id="p-nogit", name="Not Git"))
        await db.create_repo(RepoConfig(
            id="r-nogit",
            project_id="p-nogit",
            source_type=RepoSourceType.LINK,
            source_path=checkout,
        ))

        path, repo, err = await handler._resolve_repo_path({"project_id": "p-nogit"})

        assert path is None
        assert err is not None
        assert "not a valid git repository" in err["error"].lower()

    async def test_clone_repo_missing_checkout_base_path(self, handler, db, tmp_path):
        """CLONE repo with empty checkout_base_path → error."""
        await db.create_project(Project(id="p-nobase", name="No Base"))
        await db.create_repo(RepoConfig(
            id="r-nobase",
            project_id="p-nobase",
            source_type=RepoSourceType.CLONE,
            url="https://github.com/example/repo.git",
            checkout_base_path="",  # not set
        ))

        path, repo, err = await handler._resolve_repo_path({"project_id": "p-nobase"})

        assert path is None
        assert repo is not None
        assert repo.id == "r-nobase"
        assert err is not None
        assert "no usable path" in err["error"].lower()

    async def test_link_repo_missing_source_path(self, handler, db, tmp_path):
        """LINK repo with empty source_path → error."""
        await db.create_project(Project(id="p-nosrc", name="No Src"))
        await db.create_repo(RepoConfig(
            id="r-nosrc",
            project_id="p-nosrc",
            source_type=RepoSourceType.LINK,
            source_path="",  # not set
        ))

        path, repo, err = await handler._resolve_repo_path({"project_id": "p-nosrc"})

        assert path is None
        assert repo is not None
        assert repo.id == "r-nosrc"
        assert err is not None
        assert "no usable path" in err["error"].lower()


# ---------------------------------------------------------------------------
# Multiple repos per project
# ---------------------------------------------------------------------------


class TestMultipleRepos:
    """When a project has multiple repos, the first is used by default."""

    async def test_first_repo_used_when_no_repo_id(self, handler, db, tmp_path, mock_git):
        """Without explicit repo_id, list_repos returns repos and first is used."""
        checkout1 = _make_dir(str(tmp_path / "repo1"))
        checkout2 = _make_dir(str(tmp_path / "repo2"))

        await db.create_project(Project(id="p-multi", name="Multi Repo"))
        await db.create_repo(RepoConfig(
            id="r-first",
            project_id="p-multi",
            source_type=RepoSourceType.LINK,
            source_path=checkout1,
        ))
        await db.create_repo(RepoConfig(
            id="r-second",
            project_id="p-multi",
            source_type=RepoSourceType.LINK,
            source_path=checkout2,
        ))

        path, repo, err = await handler._resolve_repo_path({"project_id": "p-multi"})

        assert err is None
        assert repo is not None
        # Should have used the first repo returned by list_repos
        assert path in (checkout1, checkout2)

    async def test_explicit_repo_id_overrides_default(self, handler, db, tmp_path, mock_git):
        """When repo_id is given, that specific repo is used regardless of order."""
        checkout1 = _make_dir(str(tmp_path / "first"))
        checkout2 = _make_dir(str(tmp_path / "second"))

        await db.create_project(Project(id="p-pick", name="Pick Repo"))
        await db.create_repo(RepoConfig(
            id="r-pick-1",
            project_id="p-pick",
            source_type=RepoSourceType.LINK,
            source_path=checkout1,
        ))
        await db.create_repo(RepoConfig(
            id="r-pick-2",
            project_id="p-pick",
            source_type=RepoSourceType.LINK,
            source_path=checkout2,
        ))

        path, repo, err = await handler._resolve_repo_path({
            "project_id": "p-pick",
            "repo_id": "r-pick-2",
        })

        assert err is None
        assert path == checkout2
        assert repo.id == "r-pick-2"
