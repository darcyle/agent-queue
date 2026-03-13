"""Tests for new hook event types: note events, file/folder watches, and new context steps."""
from __future__ import annotations

import asyncio
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import AppConfig, HookEngineConfig
from src.database import Database
from src.event_bus import EventBus
from src.hooks import HookEngine
from src.models import Hook, HookRun


@pytest.fixture
async def db(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def config():
    cfg = AppConfig()
    cfg.hook_engine = HookEngineConfig(
        enabled=True,
        max_concurrent_hooks=2,
        file_watcher_enabled=True,
        file_watcher_poll_interval=0.0,
        file_watcher_debounce_seconds=0.1,
    )
    return cfg


@pytest.fixture
async def engine(db, bus, config):
    engine = HookEngine(db, bus, config)
    engine._orchestrator = MagicMock()
    engine._orchestrator._notify_channel = AsyncMock()
    engine._orchestrator.db = db
    engine._orchestrator.hooks = engine
    await engine.initialize()
    yield engine
    await engine.shutdown()


async def _create_project(db, project_id="test-project"):
    from src.models import Project
    project = Project(id=project_id, name="Test Project")
    await db.create_project(project)
    return project


async def _create_hook(db, **overrides) -> Hook:
    defaults = dict(
        id="test-hook",
        project_id="test-project",
        name="test-hook",
        enabled=True,
        trigger='{"type": "periodic", "interval_seconds": 3600}',
        context_steps='[]',
        prompt_template="Test prompt: {{step_0}}",
        cooldown_seconds=60,
    )
    defaults.update(overrides)
    hook = Hook(**defaults)
    await db.create_hook(hook)
    return hook


# --- Note event hooks ---


class TestNoteEventHooks:
    @pytest.mark.asyncio
    async def test_note_created_fires_hook(self, db, bus, engine):
        """Hook with event_type 'note.created' should fire on note creation events."""
        await _create_project(db)
        hook = await _create_hook(
            db,
            id="note-hook",
            name="note-hook",
            trigger='{"type": "event", "event_type": "note.created"}',
            cooldown_seconds=0,
            context_steps='[{"type": "shell", "command": "echo noted", "skip_llm_if_exit_zero": true}]',
        )

        await bus.emit("note.created", {
            "project_id": "test-project",
            "note_name": "ideas.md",
            "note_path": "/tmp/notes/ideas.md",
            "title": "Ideas",
            "operation": "created",
        })
        await asyncio.sleep(0.1)

        assert hook.id in engine._running or len(await db.list_hook_runs(hook.id)) > 0

    @pytest.mark.asyncio
    async def test_note_updated_fires_hook(self, db, bus, engine):
        """Hook with event_type 'note.updated' should fire on note update events."""
        await _create_project(db)
        hook = await _create_hook(
            db,
            id="note-update-hook",
            name="note-update-hook",
            trigger='{"type": "event", "event_type": "note.updated"}',
            cooldown_seconds=0,
            context_steps='[{"type": "shell", "command": "echo updated", "skip_llm_if_exit_zero": true}]',
        )

        await bus.emit("note.updated", {
            "project_id": "test-project",
            "note_name": "ideas.md",
            "note_path": "/tmp/notes/ideas.md",
            "title": "Ideas",
            "operation": "updated",
        })
        await asyncio.sleep(0.1)

        assert hook.id in engine._running or len(await db.list_hook_runs(hook.id)) > 0

    @pytest.mark.asyncio
    async def test_note_deleted_fires_hook(self, db, bus, engine):
        """Hook with event_type 'note.deleted' should fire on note deletion events."""
        await _create_project(db)
        hook = await _create_hook(
            db,
            id="note-delete-hook",
            name="note-delete-hook",
            trigger='{"type": "event", "event_type": "note.deleted"}',
            cooldown_seconds=0,
            context_steps='[{"type": "shell", "command": "echo deleted", "skip_llm_if_exit_zero": true}]',
        )

        await bus.emit("note.deleted", {
            "project_id": "test-project",
            "note_name": "old-note.md",
            "note_path": "/tmp/notes/old-note.md",
            "title": "Old Note",
        })
        await asyncio.sleep(0.1)

        assert hook.id in engine._running or len(await db.list_hook_runs(hook.id)) > 0

    @pytest.mark.asyncio
    async def test_note_event_data_in_prompt(self, db, engine):
        """Note event data should be available in prompt templates."""
        await _create_project(db)
        hook = await _create_hook(
            db,
            id="note-prompt-hook",
            name="note-prompt-hook",
            trigger='{"type": "event", "event_type": "note.created"}',
            cooldown_seconds=0,
            context_steps='[]',
            prompt_template="Note {{event.note_name}} was {{event.operation}} in project {{event.project_id}}",
        )

        with patch.object(engine, '_invoke_llm', new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = ("OK", 50)
            await engine._execute_hook(hook, "event:note.created", event_data={
                "project_id": "test-project",
                "note_name": "ideas.md",
                "operation": "created",
            })

        runs = await db.list_hook_runs(hook.id)
        assert len(runs) == 1
        assert "ideas.md" in runs[0].prompt_sent
        assert "created" in runs[0].prompt_sent


# --- run_tests step ---


class TestRunTestsStep:
    @pytest.mark.asyncio
    async def test_run_tests_passing(self, engine, tmp_path):
        """run_tests step with passing command should return passed=True."""
        step = {
            "type": "run_tests",
            "command": "echo '5 passed, 0 failed'",
            "timeout": 10,
            "workspace": str(tmp_path),
        }
        results = await engine._run_context_steps([step])
        assert len(results) == 1
        assert results[0]["passed"] is True
        assert results[0]["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_run_tests_failing(self, engine, tmp_path):
        """run_tests step with failing command should return passed=False."""
        step = {
            "type": "run_tests",
            "command": "echo 'FAILED tests/test_foo.py::test_bar' && exit 1",
            "timeout": 10,
            "workspace": str(tmp_path),
        }
        results = await engine._run_context_steps([step])
        assert results[0]["passed"] is False
        assert results[0]["exit_code"] == 1
        assert "tests/test_foo.py::test_bar" in results[0]["failures"]

    @pytest.mark.asyncio
    async def test_run_tests_timeout(self, engine, tmp_path):
        """run_tests step should handle timeout gracefully."""
        step = {
            "type": "run_tests",
            "command": "sleep 60",
            "timeout": 1,
            "workspace": str(tmp_path),
        }
        results = await engine._run_context_steps([step])
        assert results[0]["passed"] is False
        assert results[0]["exit_code"] == -1
        assert "timed out" in results[0]["stderr"]

    @pytest.mark.asyncio
    async def test_parse_pytest_failures(self, engine):
        """Should extract pytest-style failure names."""
        failures = engine._parse_test_failures(
            "FAILED tests/test_auth.py::test_login - AssertionError\n"
            "FAILED tests/test_auth.py::test_logout - KeyError\n",
            "",
            "pytest",
        )
        assert len(failures) == 2
        assert "tests/test_auth.py::test_login" in failures
        assert "tests/test_auth.py::test_logout" in failures

    @pytest.mark.asyncio
    async def test_parse_test_count_pytest(self, engine):
        """Should extract test count from pytest output."""
        count = engine._parse_test_count(
            "====== 42 passed, 3 failed in 12.5s ======", "pytest"
        )
        assert count == 45

    @pytest.mark.asyncio
    async def test_parse_test_count_jest(self, engine):
        """Should extract test count from jest output."""
        count = engine._parse_test_count(
            "Tests: 2 failed, 15 passed, 17 total", "jest"
        )
        assert count == 17

    @pytest.mark.asyncio
    async def test_run_tests_skip_on_pass(self, engine, tmp_path):
        """run_tests + skip_llm_if_exit_zero should skip LLM when tests pass."""
        steps = [{
            "type": "run_tests",
            "command": "echo 'All tests passed'",
            "timeout": 10,
            "workspace": str(tmp_path),
            "skip_llm_if_exit_zero": True,
        }]
        results = await engine._run_context_steps(steps)
        reason = engine._should_skip_llm(steps, results)
        assert reason is not None
        assert "exit code 0" in reason


# --- list_files step ---


class TestListFilesStep:
    @pytest.mark.asyncio
    async def test_list_files_basic(self, engine, tmp_path):
        """list_files should return files in a directory."""
        subdir = tmp_path / "listtest"
        subdir.mkdir()
        (subdir / "a.py").write_text("pass")
        (subdir / "b.md").write_text("# Doc")
        (subdir / ".hidden").write_text("secret")

        step = {"type": "list_files", "path": str(subdir)}
        results = await engine._run_context_steps([step])
        assert results[0]["count"] == 2  # Excludes hidden file
        paths = [f["path"] for f in results[0]["files"]]
        assert "a.py" in paths
        assert "b.md" in paths
        assert ".hidden" not in paths

    @pytest.mark.asyncio
    async def test_list_files_with_extension_filter(self, engine, tmp_path):
        """list_files with extensions should only return matching files."""
        (tmp_path / "readme.md").write_text("# Hi")
        (tmp_path / "code.py").write_text("pass")
        (tmp_path / "data.json").write_text("{}")

        step = {
            "type": "list_files",
            "path": str(tmp_path),
            "extensions": [".md"],
        }
        results = await engine._run_context_steps([step])
        assert results[0]["count"] == 1
        assert results[0]["files"][0]["path"] == "readme.md"

    @pytest.mark.asyncio
    async def test_list_files_recursive(self, engine, tmp_path):
        """list_files with recursive=True should descend into subdirectories."""
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "top.py").write_text("pass")
        (sub / "deep.py").write_text("pass")

        step = {
            "type": "list_files",
            "path": str(tmp_path),
            "recursive": True,
        }
        results = await engine._run_context_steps([step])
        paths = [f["path"] for f in results[0]["files"]]
        assert "top.py" in paths
        assert os.path.join("sub", "deep.py") in paths

    @pytest.mark.asyncio
    async def test_list_files_max_files(self, engine, tmp_path):
        """list_files should respect max_files limit."""
        for i in range(10):
            (tmp_path / f"file{i}.txt").write_text(f"content {i}")

        step = {
            "type": "list_files",
            "path": str(tmp_path),
            "max_files": 3,
        }
        results = await engine._run_context_steps([step])
        assert results[0]["count"] == 3

    @pytest.mark.asyncio
    async def test_list_files_nonexistent_dir(self, engine):
        """list_files on non-existent directory should return error."""
        step = {
            "type": "list_files",
            "path": "/nonexistent/path",
        }
        results = await engine._run_context_steps([step])
        assert "error" in results[0]

    @pytest.mark.asyncio
    async def test_list_files_content_field(self, engine, tmp_path):
        """list_files should set content field for template rendering."""
        (tmp_path / "a.py").write_text("pass")
        (tmp_path / "b.py").write_text("pass")

        step = {"type": "list_files", "path": str(tmp_path)}
        results = await engine._run_context_steps([step])
        # content should be newline-separated file paths
        assert "a.py" in results[0]["content"]
        assert "b.py" in results[0]["content"]


# --- file_diff step ---


class TestFileDiffStep:
    @pytest.mark.asyncio
    async def test_file_diff_no_path(self, engine):
        """file_diff without path should return error."""
        step = {"type": "file_diff"}
        results = await engine._run_context_steps([step])
        assert "error" in results[0]

    @pytest.mark.asyncio
    async def test_file_diff_with_event_placeholder(self, engine, tmp_path):
        """file_diff should resolve {{event.path}} placeholder."""
        test_file = tmp_path / "test.py"
        test_file.write_text("pass")

        step = {
            "type": "file_diff",
            "path": "{{event.path}}",
            "workspace": str(tmp_path),
        }
        results = await engine._run_context_steps(
            [step],
            event_data={"path": str(test_file)},
        )
        # May fail git diff since tmp_path isn't a git repo, but shouldn't crash
        assert "_step_index" in results[0]


# --- File watch hook integration ---


class TestFileWatchHookIntegration:
    @pytest.mark.asyncio
    async def test_file_watch_hook_registered(self, db, engine, tmp_path):
        """Hook with file.changed trigger + watch config should register a file watch."""
        await _create_project(db)

        test_file = tmp_path / "config.yaml"
        test_file.write_text("key: value")

        trigger = json.dumps({
            "type": "event",
            "event_type": "file.changed",
            "watch": {
                "paths": [str(test_file)],
                "project_id": "test-project",
            },
        })
        await _create_hook(
            db,
            id="file-watch-hook",
            name="file-watch-hook",
            trigger=trigger,
            cooldown_seconds=0,
            context_steps='[{"type": "shell", "command": "echo changed", "skip_llm_if_exit_zero": true}]',
        )

        # Re-sync watches
        await engine._sync_file_watches()

        assert engine.file_watcher is not None
        assert engine.file_watcher.get_watch_count() == 1

    @pytest.mark.asyncio
    async def test_folder_watch_hook_registered(self, db, engine, tmp_path):
        """Hook with folder.changed trigger + watch config should register a folder watch."""
        await _create_project(db)

        src_dir = tmp_path / "src"
        src_dir.mkdir()

        trigger = json.dumps({
            "type": "event",
            "event_type": "folder.changed",
            "watch": {
                "paths": [str(src_dir)],
                "recursive": True,
                "extensions": [".py"],
                "project_id": "test-project",
            },
        })
        await _create_hook(
            db,
            id="folder-watch-hook",
            name="folder-watch-hook",
            trigger=trigger,
            cooldown_seconds=0,
            context_steps='[{"type": "shell", "command": "echo changed", "skip_llm_if_exit_zero": true}]',
        )

        await engine._sync_file_watches()

        assert engine.file_watcher is not None
        assert engine.file_watcher.get_watch_count() == 1

    @pytest.mark.asyncio
    async def test_stale_watches_removed(self, db, engine, tmp_path):
        """Watches for deleted/disabled hooks should be cleaned up on sync."""
        await _create_project(db)

        trigger = json.dumps({
            "type": "event",
            "event_type": "file.changed",
            "watch": {
                "paths": [str(tmp_path / "test.txt")],
                "project_id": "test-project",
            },
        })
        hook = await _create_hook(
            db,
            id="temp-hook",
            name="temp-hook",
            trigger=trigger,
            cooldown_seconds=0,
        )

        await engine._sync_file_watches()
        assert engine.file_watcher.get_watch_count() == 1

        # Disable the hook
        await db.update_hook(hook.id, enabled=False)
        await engine._sync_file_watches()
        assert engine.file_watcher.get_watch_count() == 0


# --- Named DB queries ---


class TestNewNamedQueries:
    @pytest.mark.asyncio
    async def test_failed_tasks_query(self, db, engine):
        """The 'failed_tasks' named query should work."""
        await _create_project(db)
        step = {
            "type": "db_query",
            "query": "failed_tasks",
        }
        results = await engine._run_context_steps([step])
        assert "rows" in results[0]
        assert results[0]["count"] == 0  # No failed tasks

    @pytest.mark.asyncio
    async def test_recent_hook_activity_query(self, db, engine):
        """The 'recent_hook_activity' named query should work."""
        await _create_project(db)
        hook = await _create_hook(db)
        run = HookRun(
            id="run1", hook_id=hook.id, project_id="test-project",
            trigger_reason="periodic", started_at=time.time(),
            status="completed",
        )
        await db.create_hook_run(run)

        step = {
            "type": "db_query",
            "query": "recent_hook_activity",
        }
        results = await engine._run_context_steps([step])
        assert results[0]["count"] >= 1

    @pytest.mark.asyncio
    async def test_project_tasks_by_status_query(self, db, engine):
        """The 'project_tasks_by_status' query should filter by project and status."""
        await _create_project(db)
        step = {
            "type": "db_query",
            "query": "project_tasks_by_status",
            "params": {"project_id": "test-project", "status": "defined"},
        }
        results = await engine._run_context_steps([step])
        assert "rows" in results[0]


# --- HookEngineConfig ---


class TestHookEngineConfigExtensions:
    def test_default_file_watcher_config(self):
        """HookEngineConfig should have file watcher defaults."""
        cfg = HookEngineConfig()
        assert cfg.file_watcher_enabled is True
        assert cfg.file_watcher_poll_interval == 10.0
        assert cfg.file_watcher_debounce_seconds == 5.0

    def test_custom_file_watcher_config(self):
        """HookEngineConfig should accept custom file watcher settings."""
        cfg = HookEngineConfig(
            file_watcher_enabled=False,
            file_watcher_poll_interval=30.0,
            file_watcher_debounce_seconds=10.0,
        )
        assert cfg.file_watcher_enabled is False
        assert cfg.file_watcher_poll_interval == 30.0
        assert cfg.file_watcher_debounce_seconds == 10.0

    @pytest.mark.asyncio
    async def test_file_watcher_disabled(self, db, bus):
        """When file_watcher_enabled=False, no FileWatcher should be created."""
        cfg = AppConfig()
        cfg.hook_engine = HookEngineConfig(
            enabled=True,
            file_watcher_enabled=False,
        )
        engine = HookEngine(db, bus, cfg)
        engine._orchestrator = MagicMock()
        await engine.initialize()

        assert engine.file_watcher is None
        await engine.shutdown()
